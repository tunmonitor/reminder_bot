"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import sqlite3
from typing import Any

from aiogram import F, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .notifier import CONFIRM_PREFIX, _notify_admin

router = Router()

def _telegram_text_chunks(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    current = []
    current_units = 0
    for character in text:
        character_units = len(character.encode("utf-16-le")) // 2
        if current and current_units + character_units > limit:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += character_units
    if current:
        chunks.append("".join(current))
    return chunks or [""]


async def _persist_confirmation(context: Any, debug: logging.Logger) -> bool:
    try:
        context.state_cache.save()
    except OSError as exc:
        debug.exception("Не удалось сохранить состояние подтверждения")
        await _notify_admin(
            context.bot,
            context.admin_id,
            f"Не удалось сохранить состояние подтверждения: {exc}",
            debug,
        )
        return False
    return True


def register(dispatcher: Dispatcher, context: Any) -> None:
    """Регистрирует хэндлер, замыкая его на context."""

    interaction: logging.Logger = context.interaction_logger
    debug: logging.Logger = context.debug_logger
    admin_router = Router()
    pending_inputs: dict[int, dict[str, Any]] = {}
    pending_shares: dict[int, tuple[int, str, str, str | None]] = {}
    pending_admin_confirmations: dict[
        tuple[int, str, int], tuple[str, str | None, str, str]
    ] = {}
    pending_task_choices: dict[str, tuple[int, str, str, str | None]] = {}
    pending_notify_task_choices: dict[str, tuple[int, str, str | None]] = {}
    def new_share_request_id() -> int:
        registry = service_registry()
        if registry is None:
            raise ValueError("Реестр служений временно недоступен.")
        return registry.next_share_request_id()

    def clear_pending_actor(actor_id: int) -> None:
        pending_inputs.pop(actor_id, None)
        for request_id, pending in list(pending_shares.items()):
            if pending[0] == actor_id:
                pending_shares.pop(request_id, None)
        for key in list(pending_admin_confirmations):
            if key[0] == actor_id:
                pending_admin_confirmations.pop(key, None)
        for key in list(pending_task_choices):
            if pending_task_choices[key][0] == actor_id:
                pending_task_choices.pop(key, None)
        for key in list(pending_notify_task_choices):
            if pending_notify_task_choices[key][0] == actor_id:
                pending_notify_task_choices.pop(key, None)

    def service_registry():
        registry = getattr(context, "service_registry", None)
        if registry is None:
            return None
        return registry

    def is_service_admin(actor_id: int, service_id: str) -> bool:
        registry = service_registry()
        return bool(
            registry
            and (
                registry.is_super_admin(actor_id)
                or registry.is_service_admin(actor_id, service_id)
            )
        )

    def service_by_id(registry: Any, service_id: str) -> dict | None:
        return next(
            (service for service in registry.list_services()
             if service["id"] == service_id),
            None,
        )

    def service_label(service: dict) -> str:
        return service["name"].upper()

    def settings_text(registry: Any, actor_id: int, service_id: str) -> str:
        if not is_service_admin(actor_id, service_id):
            raise PermissionError(
                "Настройки доступны только администратору этого служения."
            )
        service = service_by_id(registry, service_id)
        if service is None:
            raise ValueError("Служение не найдено.")
        lines = [
            service_label(service),
            f"Статус: {'включено' if service['enabled'] else 'выключено'}",
            f"Групповой чат: {service['group_chat_id'] or 'не задан'}",
            f"Напоминание: за {service['reminder_days']} дн. "
            f"в {service['reminder_time']}",
            f"Текст для группы: {service['group_template'] or 'не задан'}",
            "Тексты личных сообщений: "
            + (
                json.dumps(
                    service["personal_templates"], ensure_ascii=False
                )
                if service["personal_templates"]
                else "не заданы"
            ),
            "Чтобы изменить дни, время или тексты, нажмите кнопку "
            "«Изменить настройки уведомлений» ниже.",
        ]
        if registry.is_super_admin(actor_id):
            admins = registry.list_service_admins(service_id)
            contacts = registry.list_responsible_contacts(
                actor_id, service_id
            )
            lines.append(
                f"Администраторы (Telegram ID): "
                f"{', '.join(map(str, admins)) or 'не назначены'}"
            )
            lines.append("Ответственные (задача — имя — Telegram ID):")
            lines.extend(
                f"{item['task']} — {item['name']} — {item['telegram_id']}"
                + (f" (@{item['username']})" if item["username"] else "")
                for item in contacts
            )
            if not contacts:
                lines.append("Пока не добавлены.")
        return "\n".join(lines)

    async def request_service_input(
        message: Message, operation: str, service_id: str | None = None
    ) -> None:
        registry = service_registry()
        if registry is None:
            await message.answer(
                "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return
        actor_id = message.from_user.id
        try:
            if service_id:
                service = service_by_id(registry, service_id)
                if service is None:
                    raise ValueError("Служение не найдено.")
                if operation == "service_settings":
                    await run_registry_command(
                        message,
                        lambda user_id: settings_text(
                            registry, user_id, service_id
                        ),
                        reply_markup=settings_keyboard(service_id),
                    )
                    return
                if operation == "service_notify":
                    if not is_service_admin(actor_id, service_id):
                        raise PermissionError(
                            "Настройки доступны только администратору этого служения."
                        )
                elif not registry.is_super_admin(actor_id):
                    raise PermissionError(
                        "Эта команда доступна только супер-администратору."
                    )
                if operation == "service_admin_add":
                    await offer_user_selection(
                        message, actor_id, service_id
                    )
                    return
                await ask_for_service_parameters(
                    message,
                    actor_id,
                    operation,
                    service_id,
                    service_label(service),
                )
                return

            is_super_admin = registry.is_super_admin(actor_id)
            services = registry.list_services()
            if operation in {"service_settings", "service_notify"}:
                services = [
                    item for item in services
                    if is_super_admin
                    or registry.is_service_admin(actor_id, item["id"])
                ]
                if not services:
                    raise PermissionError(
                        "У вас нет прав администратора ни для одного служения."
                    )
            elif operation == "service_admin_add":
                if not is_super_admin:
                    raise PermissionError(
                        "Эта команда доступна только супер-администратору."
                    )
            elif not is_super_admin:
                raise PermissionError(
                    "Эта команда доступна только супер-администратору."
                )
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось загрузить список служений")
            await message.answer(
                "Не удалось загрузить список служений.", parse_mode=None
            )
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=service_label(service),
                        callback_data=f"service:{operation}:{service['id']}",
                    )
                ]
                for service in services
            ]
        )
        descriptions = {
            "service_settings": "Выберите служение, чтобы посмотреть его настройки. В сообщении настроек будет кнопка перехода к изменению.",
            "service_admin_add": "Выберите служение, затем выберите пользователя кнопкой Telegram и подтвердите назначение.",
            "service_admin_remove": "Выберите служение, затем укажите Telegram ID администратора для удаления.",
            "service_chat": "Выберите служение, затем выберите действие: настроить чат или отключить служение.",
            "service_contact": "Выберите служение, задачу, затем выберите ответственного кнопкой Telegram.",
            "service_notify": "Выберите служение и последовательно задайте дни, время и тексты уведомлений.",
        }
        await message.answer(
            descriptions[operation],
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def ask_for_service_parameters(
        message: Message,
        actor_id: int,
        operation: str,
        service_id: str,
        label: str,
    ) -> None:
        if operation == "service_chat":
            request_id = new_share_request_id()
            pending_shares[request_id] = (
                actor_id, "service_chat", service_id, None
            )
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="Выбрать групповой чат",
                            request_chat=KeyboardButtonRequestChat(
                                request_id=request_id,
                                chat_is_channel=False,
                                bot_is_member=True,
                                request_title=True,
                                request_username=True,
                            ),
                        )
                    ]
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await message.answer(
                f"{label}: нажмите кнопку и выберите группу, в которой "
                "бот уже состоит. После выбора служение будет включено.",
                reply_markup=keyboard,
                parse_mode=None,
            )
            return

        registry = service_registry()
        if registry is None:
            await message.answer(
                "Реестр настроек служений недоступен.", parse_mode=None
            )
            return
        prompts = {
            "service_admin_add": (
                f"{label}: выберите пользователя кнопкой Telegram."
            ),
            "service_admin_remove": (
                f"{label}: введите Telegram ID администратора, которого "
                "нужно снять."
            ),
            "service_chat": (
                f"{label}: выберите групповой чат кнопкой Telegram."
            ),
            "service_contact": f"{label}: выберите или добавьте задачу.",
            "service_notify": (
                f"{label}: за сколько дней до воскресенья отправлять "
                "напоминание? Введите число от 0 до 6."
            ),
        }
        if operation == "service_notify":
            service = service_by_id(registry, service_id)
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "step": "days",
                "reminder_days": None,
                "reminder_time": None,
                "group_template": None,
                "personal_templates": dict(
                    service["personal_templates"] if service else {}
                ),
            }
            prompts[operation] = (
                f"{label}: за сколько дней до воскресенья отправлять "
                "напоминание? Введите число от 0 до 6."
            )
        elif operation == "service_admin_remove":
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "step": "text",
            }
        elif operation == "service_contact":
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "step": "choose_task",
            }
            await offer_contact_task_choices(message, actor_id, service_id)
            return
        await message.answer(prompts[operation], parse_mode=None)

    async def run_registry_command(
        message: Message,
        operation,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        clear_pending_actor(message.from_user.id)
        if service_registry() is None:
            await message.answer(
                "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return
        try:
            response = operation(message.from_user.id)
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Ошибка выполнения команды управления служениями")
            await message.answer(
                "Не удалось выполнить команду из-за ошибки базы настроек.",
                parse_mode=None,
            )
            return
        chunks = _telegram_text_chunks(response)
        for index, chunk in enumerate(chunks):
            await message.answer(
                chunk,
                parse_mode=None,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )

    def settings_keyboard(service_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Изменить настройки уведомлений",
                        callback_data=f"service:edit_notify:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="К списку служений",
                        callback_data="service:back_to_services:list",
                    )
                ],
            ]
        )

    def _task_button(
        actor_id: int,
        service_id: str,
        task: str,
        *,
        operation: str,
        task_choices: dict[str, tuple[int, str, str, str | None]],
    ) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(8)
        task_choices[token] = (actor_id, operation, service_id, task)
        return InlineKeyboardButton(
            text=task,
            callback_data=f"task:{operation}:{token}",
        )

    async def offer_contact_task_choices(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        registry = service_registry()
        if registry is None:
            await message.answer(
                "Реестр настроек служений недоступен.", parse_mode=None
            )
            return
        try:
            contacts = registry.list_responsible_contacts(actor_id, service_id)
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось загрузить задачи служения")
            await message.answer(
                "Не удалось прочитать список задач служения.", parse_mode=None
            )
            return
        tasks = sorted({item["task"] for item in contacts})
        rows = [
            [
                _task_button(
                    actor_id,
                    service_id,
                    task,
                    operation="service_contact",
                    task_choices=pending_task_choices,
                )
            ]
            for task in tasks
        ]
        new_task_token = secrets.token_urlsafe(8)
        pending_task_choices[new_task_token] = (
            actor_id,
            "service_contact",
            service_id,
            None,
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Добавить новую задачу",
                    callback_data=f"task:service_contact:{new_task_token}",
                )
            ]
        )
        await message.answer(
            "Выберите существующую задачу или добавьте новую.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def offer_notify_task_choices(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or registry is None
        ):
            await message.answer(
                "Пошаговая настройка уведомлений завершена или недоступна.",
                parse_mode=None,
            )
            return
        tasks = sorted(pending["personal_templates"])
        rows = []
        for task in tasks:
            token = secrets.token_urlsafe(8)
            pending_notify_task_choices[token] = (actor_id, service_id, task)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=task,
                        callback_data=f"notify_task:{token}",
                    )
                ]
            )
        token = secrets.token_urlsafe(8)
        pending_notify_task_choices[token] = (actor_id, service_id, None)
        rows.append(
            [
                InlineKeyboardButton(
                    text="Завершить и сохранить",
                    callback_data=f"notify_task:{token}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Добавить задачу",
                    callback_data=f"notify_task_new:{service_id}",
                )
            ]
        )
        pending["step"] = "choose_notify_task"
        await message.answer(
            "Выберите задачу для индивидуального текста или добавьте новую.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    def service_chat_keyboard(service_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Выбрать или изменить групповой чат",
                        callback_data=f"service:service_chat_set:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отключить служение",
                        callback_data=f"service:service_chat_disable:{service_id}",
                    )
                ],
            ]
        )

    async def begin_notify_template(
        message: Message, actor_id: int, service_id: str, task: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if pending is None or pending.get("operation") != "service_notify":
            await message.answer(
                "Запрос настройки устарел. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        pending["active_task"] = task
        pending["step"] = "notify_template"
        await message.answer(
            f"Введите индивидуальный текст напоминания для задачи "
            f"«{task}». В шаблоне можно использовать {{name}} и "
            "{{sunday_date}}.",
            parse_mode=None,
        )

    async def save_notification_setup(message: Message, actor_id: int) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or registry is None
        ):
            await message.answer(
                "Пошаговая настройка уведомлений устарела.",
                parse_mode=None,
            )
            return
        try:
            registry.set_notification_settings(
                actor_id,
                pending["service_id"],
                reminder_days=pending["reminder_days"],
                reminder_time=pending["reminder_time"],
                group_template=pending["group_template"],
                personal_templates=pending["personal_templates"],
            )
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить настройки уведомлений")
            await message.answer(
                "Не удалось сохранить настройки уведомлений из-за ошибки базы.",
                parse_mode=None,
            )
            return
        pending_inputs.pop(actor_id, None)
        await message.answer("Настройки уведомлений сохранены.", parse_mode=None)

    @admin_router.callback_query(F.data.startswith("task:"))
    async def on_task_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("Некорректный выбор задачи.")
            return
        _, operation, token = parts
        selection = pending_task_choices.pop(token, None)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or selection[1] != operation
        ):
            await callback.answer("Этот выбор устарел. Начните команду заново.")
            return
        _, _, service_id, task = selection
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or pending.get("step") != "choose_task"
        ):
            await callback.answer("Запрос задачи устарел.")
            return
        if task is None:
            pending["step"] = "new_contact_task"
            await callback.message.answer(
                "Введите название новой задачи или /cancel для отмены.",
                parse_mode=None,
            )
        else:
            try:
                await offer_contact_user_selection(
                    callback.message, callback.from_user.id, service_id, task
                )
            except ValueError as exc:
                await callback.message.answer(str(exc), parse_mode=None)
            except sqlite3.Error:
                debug.exception(
                    "Не удалось создать запрос выбора ответственного"
                )
                await callback.message.answer(
                    "Не удалось открыть выбор пользователя из-за ошибки базы.",
                    parse_mode=None,
                )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("notify_task:"))
    async def on_notify_task_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        token = callback.data.partition(":")[2]
        selection = pending_notify_task_choices.pop(token, None)
        pending = pending_inputs.get(callback.from_user.id)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != selection[1]
            or pending.get("step") != "choose_notify_task"
        ):
            await callback.answer("Этот выбор устарел. Начните настройку заново.")
            return
        task = selection[2]
        if task is None:
            await save_notification_setup(
                callback.message, callback.from_user.id
            )
        else:
            await begin_notify_template(
                callback.message, callback.from_user.id, selection[1], task
            )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("notify_task_new:"))
    async def on_notify_new_task(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        service_id = callback.data.partition(":")[2]
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("step") != "choose_notify_task"
        ):
            await callback.answer("Этот запрос устарел.")
            return
        pending["step"] = "new_notify_task"
        await callback.message.answer(
            "Введите название задачи для нового индивидуального текста.",
            parse_mode=None,
        )
        await callback.answer()

    async def offer_user_selection(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_admin_add", service_id, None
        )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать пользователя",
                        request_users=KeyboardButtonRequestUsers(
                            request_id=request_id,
                            user_is_bot=False,
                            max_quantity=1,
                            request_name=True,
                            request_username=True,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(
            "Нажмите кнопку и выберите пользователя в Telegram. "
            "Проверьте выбор — назначение подтвердите на следующем шаге.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def offer_contact_user_selection(
        message: Message, actor_id: int, service_id: str, task: str
    ) -> None:
        pending_inputs.pop(actor_id, None)
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_contact", service_id, task
        )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать ответственного",
                        request_users=KeyboardButtonRequestUsers(
                            request_id=request_id,
                            user_is_bot=False,
                            max_quantity=1,
                            request_name=True,
                            request_username=True,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(
            f"Задача «{task}». Нажмите кнопку и выберите ответственного "
            "в Telegram.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    @admin_router.message(Command("services"))
    async def on_services(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        if service_registry() is None:
            await message.answer(
                "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return

        registry = service_registry()
        try:
            is_super_admin = registry.is_super_admin(message.from_user.id)
            services = registry.list_services()
            visible = [
                service for service in services
                if is_super_admin
                or registry.is_service_admin(
                    message.from_user.id, service["id"]
                )
            ]
            if not visible:
                await message.answer(
                    "Вы не назначены администратором служений.",
                    parse_mode=None,
                )
                return

            lines = ["Служения:"]
            for service in visible:
                state = "включено" if service["enabled"] else "выключено"
                lines.append(f"{service_label(service)} — {state}")
                if is_super_admin:
                    admins = registry.list_service_admins(service["id"])
                    lines.append(
                        "  Групповой чат: "
                        f"{service['group_chat_id'] or 'не задан'}; "
                        "администраторы: "
                        f"{', '.join(map(str, admins)) or 'не назначены'}"
                    )
        except sqlite3.Error:
            debug.exception("Не удалось прочитать список служений")
            await message.answer(
                "Не удалось прочитать реестр служений.", parse_mode=None
            )
            return
        for chunk in _telegram_text_chunks("\n".join(lines)):
            await message.answer(chunk, parse_mode=None)

    @admin_router.message(Command("service_settings"))
    async def on_service_settings(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_settings")
            return
        await request_service_input(
            message, "service_settings", parts[1]
        )

    @admin_router.message(Command("service_admin_add"))
    async def on_service_admin_add(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_admin_add")
            return
        if len(parts) == 2:
            await request_service_input(
                message, "service_admin_add", parts[1]
            )
            return
        if len(parts) != 3:
            await message.answer("Лишние параметры. Запустите команду ещё раз.", parse_mode=None)
            return
        service_id, raw_telegram_id = parts[1:]

        def add_admin(actor_id: int) -> str:
            telegram_id = int(raw_telegram_id)
            registry = service_registry()
            registry.add_service_admin(actor_id, service_id, telegram_id)
            return "Администратор служения добавлен (или уже был назначен)."

        await run_registry_command(message, add_admin)

    @admin_router.message(Command("service_admin_remove"))
    async def on_service_admin_remove(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_admin_remove")
            return
        if len(parts) == 2:
            await request_service_input(
                message, "service_admin_remove", parts[1]
            )
            return
        if len(parts) != 3:
            await message.answer("Лишние параметры. Запустите команду ещё раз.", parse_mode=None)
            return
        service_id, raw_telegram_id = parts[1:]

        def remove_admin(actor_id: int) -> str:
            telegram_id = int(raw_telegram_id)
            registry = service_registry()
            registry.remove_service_admin(actor_id, service_id, telegram_id)
            return "Администратор служения удалён (если он был назначен)."

        await run_registry_command(message, remove_admin)

    @admin_router.message(Command("service_chat"))
    async def on_service_chat(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_chat")
            return
        if len(parts) == 2:
            if parts[1] == "off":
                await message.answer(
                    "Выберите служение в команде /service_chat и нажмите "
                    "«Отключить служение».",
                    parse_mode=None,
                )
                return
            await request_service_input(message, "service_chat", parts[1])
            return
        if len(parts) == 3 and parts[2] == "off":
            service_id = parts[1]

            def disable_service(actor_id: int) -> str:
                registry = service_registry()
                registry.set_service_enabled(actor_id, service_id, False)
                if service_id == "assembly":
                    context.group_chat_id = registry.get_service(
                        service_id
                    )["group_chat_id"]
                return "Служение отключено; сохранённый групповой чат не изменён."

            await run_registry_command(message, disable_service)
            return
        if len(parts) != 4:
            await message.answer(
                "Используйте /service_chat, чтобы выбрать служение и "
                "действие кнопками.",
                parse_mode=None,
            )
            return
        service_id, raw_chat_id, raw_enabled = parts[1:]

        def update_chat(actor_id: int) -> str:
            if raw_enabled not in {"on", "off"}:
                raise ValueError("Последний параметр должен быть on или off.")
            chat_id = int(raw_chat_id)
            registry = service_registry()
            registry.set_group_chat(
                actor_id,
                service_id,
                chat_id,
                enabled=raw_enabled == "on",
            )
            if service_id == "assembly":
                context.group_chat_id = chat_id
            return "Групповой чат и статус служения сохранены."

        await run_registry_command(message, update_chat)

    @admin_router.message(Command("service_contact"))
    async def on_service_contact(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        payload = (message.text or "").partition(" ")[2].strip()
        parts = payload.split("|")
        prefix_parts = parts[0].strip().split(maxsplit=1) if parts else []
        details = [field.strip() for field in parts[1:]]
        if not payload:
            await request_service_input(message, "service_contact")
            return
        if len(prefix_parts) == 1:
            await request_service_input(
                message, "service_contact", prefix_parts[0]
            )
            return
        if len(prefix_parts) != 2 or len(details) not in {2, 3}:
            await message.answer("Не хватает параметров. Запустите команду ещё раз.", parse_mode=None)
            return
        service_id, task = prefix_parts
        name, raw_telegram_id, *username = details

        def set_contact(actor_id: int) -> str:
            registry = service_registry()
            registry.set_responsible_contact(
                actor_id,
                service_id,
                task,
                name,
                int(raw_telegram_id),
                username[0] if username else "",
            )
            return "Привязка ответственного сохранена."

        await run_registry_command(message, set_contact)

    @admin_router.message(Command("service_notify"))
    async def on_service_notify(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        payload = (message.text or "").partition(" ")[2]
        fields = [field.strip() for field in payload.split("|", 2)]
        if not payload.strip():
            await request_service_input(message, "service_notify")
            return
        if len(fields) == 1:
            service_parts = fields[0].split()
            await request_service_input(
                message, "service_notify", service_parts[0]
            )
            return
        if len(fields) != 3:
            await message.answer("Не хватает параметров. Запустите команду ещё раз.", parse_mode=None)
            return
        settings, group_template, raw_templates = fields
        settings_parts = settings.split(maxsplit=2)
        if len(settings_parts) != 3:
            await message.answer(
                "Перед первым | укажите service_id, число дней и время.",
                parse_mode=None,
            )
            return
        service_id, raw_days, reminder_time = settings_parts

        def set_notifications(actor_id: int) -> str:
            try:
                personal_templates = json.loads(raw_templates)
            except json.JSONDecodeError as exc:
                raise ValueError("Личные шаблоны должны быть корректным JSON.") from exc
            registry = service_registry()
            registry.set_notification_settings(
                actor_id,
                service_id,
                reminder_days=int(raw_days),
                reminder_time=reminder_time,
                group_template=group_template,
                personal_templates=personal_templates,
            )
            return "Настройки уведомлений сохранены."

        await run_registry_command(message, set_notifications)

    @admin_router.message(Command("cancel"))
    async def on_cancel_service_command(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        had_request = pending_inputs.pop(message.from_user.id, None) is not None
        for request_id, pending in list(pending_shares.items()):
            if pending[0] == message.from_user.id:
                pending_shares.pop(request_id, None)
                had_request = True
        for key in list(pending_admin_confirmations):
            if key[0] == message.from_user.id:
                pending_admin_confirmations.pop(key, None)
                had_request = True
        for key in list(pending_task_choices):
            if pending_task_choices[key][0] == message.from_user.id:
                pending_task_choices.pop(key, None)
                had_request = True
        for key in list(pending_notify_task_choices):
            if pending_notify_task_choices[key][0] == message.from_user.id:
                pending_notify_task_choices.pop(key, None)
                had_request = True
        if not had_request:
            await message.answer(
                "Нет активного запроса параметров.", parse_mode=None
            )
            return
        await message.answer(
            "Текущий запрос отменён.",
            parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )

    @admin_router.message(Command("start"))
    async def on_start(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        await message.answer(
            "Здравствуйте! Для управления служениями используйте команды "
            "из меню бота.",
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("service:"))
    async def on_service_selected(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        callback_parts = callback.data.split(":", 2)
        if len(callback_parts) != 3:
            await callback.answer("Некорректный выбор служения.")
            return
        _, operation, service_id = callback_parts
        clear_pending_actor(callback.from_user.id)
        if operation == "back_to_services":
            clear_pending_actor(callback.from_user.id)
            registry = service_registry()
            if registry is None:
                await callback.message.answer(
                    "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
            else:
                try:
                    is_super_admin = registry.is_super_admin(
                        callback.from_user.id
                    )
                    services = registry.list_services()
                    services = [
                        service for service in services
                        if is_super_admin
                        or registry.is_service_admin(
                            callback.from_user.id, service["id"]
                        )
                    ]
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=service_label(service),
                                    callback_data=(
                                        "service:service_settings:"
                                        f"{service['id']}"
                                    ),
                                )
                            ]
                            for service in services
                        ]
                    )
                    await callback.message.answer(
                        "Выберите служение, чтобы посмотреть его настройки.",
                        reply_markup=keyboard,
                        parse_mode=None,
                    )
                except sqlite3.Error:
                    debug.exception(
                        "Не удалось загрузить список служений для возврата"
                    )
                    await callback.message.answer(
                        "Не удалось прочитать реестр служений.",
                        parse_mode=None,
                    )
            await callback.answer()
            return
        if operation not in {
            "service_settings",
            "edit_notify",
            "service_admin_add",
            "service_admin_remove",
            "service_chat",
            "service_chat_set",
            "service_chat_disable",
            "service_contact",
            "service_notify",
            "test_notify",
        }:
            await callback.answer("Неизвестная команда.")
            return
        registry = service_registry()
        if registry is None:
            await callback.message.answer(
                "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            await callback.answer()
            return
        try:
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            if operation == "service_settings":
                response = settings_text(
                    registry, callback.from_user.id, service_id
                )
                chunks = _telegram_text_chunks(response)
                for index, chunk in enumerate(chunks):
                    await callback.message.answer(
                        chunk,
                        parse_mode=None,
                        reply_markup=(
                            settings_keyboard(service_id)
                            if index == len(chunks) - 1
                            else None
                        ),
                    )
            elif operation in {"edit_notify", "service_notify"}:
                if not is_service_admin(
                    callback.from_user.id, service_id
                ):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_notify",
                    service_id,
                    service_label(service),
                )
            elif operation == "service_chat":
                if not registry.is_super_admin(callback.from_user.id):
                    raise PermissionError(
                        "Эта команда доступна только супер-администратору."
                    )
                await callback.message.answer(
                    f"{service_label(service)}: выберите действие.",
                    reply_markup=service_chat_keyboard(service_id),
                    parse_mode=None,
                )
            elif operation == "service_chat_set":
                if not registry.is_super_admin(callback.from_user.id):
                    raise PermissionError(
                        "Эта команда доступна только супер-администратору."
                    )
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_chat",
                    service_id,
                    service_label(service),
                )
            elif operation == "service_chat_disable":
                if not registry.is_super_admin(callback.from_user.id):
                    raise PermissionError(
                        "Эта команда доступна только супер-администратору."
                    )
                registry.set_service_enabled(
                    callback.from_user.id, service_id, False
                )
                if service_id == "assembly":
                    context.group_chat_id = service["group_chat_id"]
                await callback.message.answer(
                    f"{service_label(service)} отключено. Сохранённый чат не изменён.",
                    parse_mode=None,
                )
            elif operation == "test_notify":
                if callback.from_user.id != context.admin_id:
                    raise PermissionError(
                        "Проверка доставки доступна только главному администратору."
                    )
                if service["group_chat_id"] is None:
                    await callback.message.answer(
                        f"Для {service_label(service)} сначала настройте групповой чат.",
                        parse_mode=None,
                    )
                else:
                    await execute_test_notification(
                        callback.message, service["group_chat_id"]
                    )
            else:
                if not registry.is_super_admin(callback.from_user.id):
                    raise PermissionError(
                        "Эта команда доступна только супер-администратору."
                    )
                if operation == "service_admin_add":
                    await offer_user_selection(
                        callback.message,
                        callback.from_user.id,
                        service_id,
                    )
                else:
                    await ask_for_service_parameters(
                        callback.message,
                        callback.from_user.id,
                        operation,
                        service_id,
                        service_label(service),
                    )
        except (PermissionError, ValueError) as exc:
            await callback.message.answer(str(exc), parse_mode=None)
        except sqlite3.Error:
            debug.exception("Не удалось обработать выбор служения")
            await callback.message.answer(
                "Не удалось прочитать реестр служений.", parse_mode=None
            )
        await callback.answer()

    @admin_router.callback_query(
        F.data.startswith("service_admin_confirm:")
        | F.data.startswith("service_admin_cancel:")
        | F.data.startswith("service_contact_confirm:")
        | F.data.startswith("service_contact_cancel:")
    )
    async def on_shared_admin_confirmation(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("Некорректное подтверждение.")
            return
        action, service_id, raw_user_id = parts
        action_type = action.rsplit("_", 1)[1]
        try:
            user_id = int(raw_user_id)
        except ValueError:
            await callback.answer("Некорректный Telegram ID.")
            return
        key = (callback.from_user.id, service_id, user_id)
        selected_user = pending_admin_confirmations.get(key)
        expected_prefix = (
            "service_admin"
            if selected_user and selected_user[0] == "service_admin_add"
            else "service_contact"
        )
        if selected_user is None or action.rsplit("_", 1)[0] != expected_prefix:
            await callback.answer("Запрос устарел. Начните команду заново.")
            return
        pending_admin_confirmations.pop(key, None)
        if action_type == "cancel":
            await callback.message.answer("Назначение отменено.", parse_mode=None)
            await callback.answer()
            return
        registry = service_registry()
        if registry is None:
            await callback.message.answer(
                "Реестр настроек служений недоступен.",
                parse_mode=None,
            )
            await callback.answer()
            return
        try:
            operation, task, first_name, username = selected_user
            if operation == "service_admin_add":
                registry.add_service_admin(
                    callback.from_user.id, service_id, user_id
                )
            else:
                registry.set_responsible_contact(
                    callback.from_user.id,
                    service_id,
                    task or "",
                    first_name or f"Пользователь {user_id}",
                    user_id,
                    username,
                )
        except (PermissionError, ValueError) as exc:
            await callback.message.answer(str(exc), parse_mode=None)
        except sqlite3.Error:
            debug.exception("Не удалось сохранить выбор пользователя")
            await callback.message.answer(
                "Не удалось сохранить выбор из-за ошибки базы данных.",
                parse_mode=None,
            )
        else:
            display_name = first_name or f"Пользователь {user_id}"
            if operation == "service_admin_add":
                response = (
                    f"{display_name} (ID {user_id}"
                    + (f", @{username}" if username else "")
                    + ") назначен администратором служения."
                )
            else:
                response = (
                    f"{display_name} (ID {user_id}"
                    + (f", @{username}" if username else "")
                    + f") сохранён ответственным за задачу «{task}»."
                )
            await callback.message.answer(response, parse_mode=None)
        await callback.answer()

    @admin_router.message(F.users_shared)
    async def on_users_shared(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.users_shared
        ):
            return
        request_id = message.users_shared.request_id
        pending = pending_shares.pop(request_id, None)
        if (
            pending is None
            or pending[0] != message.from_user.id
            or pending[1] not in {"service_admin_add", "service_contact"}
        ):
            await message.answer(
                "Выбор пользователя не соответствует активному запросу. "
                "Запустите команду назначения заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if len(message.users_shared.users) != 1:
            await message.answer(
                "Нужно выбрать ровно одного пользователя.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        selected_user = message.users_shared.users[0]
        user_id = selected_user.user_id
        operation = pending[1]
        task = pending[3]
        if operation == "service_contact" and not task:
            await message.answer(
                "Не удалось определить задачу. Запустите привязку заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        key = (message.from_user.id, pending[2], user_id)
        pending_admin_confirmations[key] = (
            operation,
            task,
            selected_user.first_name,
            selected_user.username or "",
        )
        display_name = selected_user.first_name or f"Пользователь {user_id}"
        username = (
            f" (@{selected_user.username})" if selected_user.username else ""
        )
        operation_title = (
            "назначить администратором"
            if operation == "service_admin_add"
            else f"назначить ответственным за задачу «{task}»"
        )
        callback_prefix = (
            "service_admin" if operation == "service_admin_add"
            else "service_contact"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить",
                        callback_data=(
                            f"{callback_prefix}_confirm:{pending[2]}:{user_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=(
                            f"{callback_prefix}_cancel:{pending[2]}:{user_id}"
                        ),
                    ),
                ]
            ]
        )
        await message.answer(
            f"{operation_title}: {display_name}"
            f"{username} (ID {user_id})?",
            reply_markup=keyboard,
            parse_mode=None,
        )

    @admin_router.message(F.chat_shared)
    async def on_chat_shared(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.chat_shared
        ):
            return
        pending = pending_shares.pop(message.chat_shared.request_id, None)
        if (
            pending is None
            or pending[0] != message.from_user.id
            or pending[1] != "service_chat"
        ):
            await message.answer(
                "Выбор чата не соответствует активному запросу. "
                "Запустите /service_chat заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        registry = service_registry()
        if registry is None:
            await message.answer(
                "Реестр настроек служений недоступен.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        shared_chat = message.chat_shared
        try:
            registry.set_group_chat(
                message.from_user.id,
                pending[2],
                shared_chat.chat_id,
                enabled=True,
            )
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить выбранный групповой чат")
            await message.answer(
                "Не удалось сохранить групповой чат из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        if pending[2] == "assembly":
            context.group_chat_id = shared_chat.chat_id
        title = shared_chat.title or str(shared_chat.chat_id)
        await message.answer(
            f"Для служения сохранён чат «{title}» "
            f"(ID {shared_chat.chat_id}); служение включено.",
            parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )

    @admin_router.message(F.text & ~F.text.startswith("/"))
    async def on_service_parameter(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.text
        ):
            return
        pending = pending_inputs.get(message.from_user.id)
        if pending is None:
            return
        operation = pending["operation"]
        service_id = pending["service_id"]
        registry = service_registry()
        if registry is None:
            pending_inputs.pop(message.from_user.id, None)
            await message.answer(
                "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        text = message.text.strip()
        try:
            if operation == "service_admin_add":
                registry.add_service_admin(
                    message.from_user.id, service_id, int(text)
                )
                response = "Администратор служения назначен."
            elif operation == "service_admin_remove":
                registry.remove_service_admin(
                    message.from_user.id, service_id, int(text)
                )
                response = "Администратор служения снят."
            elif operation == "service_chat":
                parts = text.split()
                if len(parts) != 2 or parts[1] not in {"on", "off"}:
                    raise ValueError(
                        "Введите ID чата и on или off, например: "
                        "-1001234567890 on."
                    )
                chat_id = int(parts[0])
                registry.set_group_chat(
                    message.from_user.id,
                    service_id,
                    chat_id,
                    enabled=parts[1] == "on",
                )
                if service_id == "assembly":
                    context.group_chat_id = chat_id
                response = "Групповой чат и статус служения сохранены."
            elif operation == "service_contact":
                if pending["step"] == "new_contact_task":
                    if not text:
                        raise ValueError("Название задачи не может быть пустым.")
                    await offer_contact_user_selection(
                        message, message.from_user.id, service_id, text
                    )
                    pending_inputs.pop(message.from_user.id, None)
                    return
                pending_inputs.pop(message.from_user.id, None)
                return
            elif operation == "service_notify":
                step = pending["step"]
                if step == "days":
                    try:
                        days = int(text)
                    except ValueError as exc:
                        raise ValueError(
                            "Введите целое число от 0 до 6."
                        ) from exc
                    if not 0 <= days <= 6:
                        raise ValueError("Число дней должно быть от 0 до 6.")
                    pending["reminder_days"] = days
                    pending["step"] = "time"
                    await message.answer(
                        "Укажите время отправки напоминания в формате HH:MM, "
                        "например 17:15.",
                        parse_mode=None,
                    )
                    return
                if step == "time":
                    if (
                        len(text) != 5
                        or text[2] != ":"
                        or not text[:2].isdigit()
                        or not text[3:].isdigit()
                        or not 0 <= int(text[:2]) <= 23
                        or not 0 <= int(text[3:]) <= 59
                    ):
                        raise ValueError(
                            "Укажите время в формате HH:MM, например 17:15."
                        )
                    pending["reminder_time"] = text
                    pending["step"] = "group_template"
                    await message.answer(
                        "Введите текст напоминания для группового чата. "
                        "Доступны шаблоны {sunday_date}, {tg_responsible}, "
                        "{tg_leader}, {tg_preacher}.",
                        parse_mode=None,
                    )
                    return
                if step == "group_template":
                    if not text:
                        raise ValueError("Текст для группы не может быть пустым.")
                    pending["group_template"] = text
                    await offer_notify_task_choices(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "new_notify_task":
                    if not text:
                        raise ValueError("Название задачи не может быть пустым.")
                    pending["active_task"] = text
                    pending["step"] = "notify_template"
                    await message.answer(
                        f"Введите индивидуальный текст для задачи «{text}». "
                        "В шаблоне можно использовать {name} и {sunday_date}.",
                        parse_mode=None,
                    )
                    return
                if step == "notify_template":
                    if not text:
                        raise ValueError(
                            "Текст индивидуального напоминания не может быть пустым."
                        )
                    pending["personal_templates"][pending["active_task"]] = text
                    pending.pop("active_task", None)
                    await offer_notify_task_choices(
                        message, message.from_user.id, service_id
                    )
                    return
                raise ValueError("Шаг настройки устарел. Запустите команду заново.")
            else:
                pending_inputs.pop(message.from_user.id, None)
                return
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Ошибка сохранения настроек служения")
            await message.answer(
                "Не удалось сохранить настройки из-за ошибки базы данных.",
                parse_mode=None,
            )
            return

        pending_inputs.pop(message.from_user.id, None)
        await message.answer(response, parse_mode=None)

    async def execute_test_notification(
        message: Message, group_chat_id: int
    ) -> None:
        group_result = "✅ отправлено"
        try:
            await context.bot.send_message(
                group_chat_id,
                "🔧 ТЕСТ бота: проверка доставки в групповой чат. "
                "Обычная рассылка участникам не запускалась.",
                parse_mode=None,
            )
        except TelegramAPIError as exc:
            group_result = f"❌ ошибка ({type(exc).__name__})"
            debug.exception("Тестовая отправка в группу не удалась")

        try:
            previews = await context.prepare_test_previews()
        except Exception as exc:  # noqa: BLE001
            debug.exception("Не удалось подготовить предпросмотр рассылки")
            previews = None
            schedule_result = (
                f"Предпросмотр расписания не подготовлен "
                f"({type(exc).__name__})."
            )
        else:
            schedule_result = (
                f"Предпросмотр расписания на "
                f"{previews['sunday_date']:%d.%m.%Y} подготовлен."
            )

        report_lines = [
            "🔧 Проверка доставки завершена.",
            f"Тестовое сообщение в группу: {group_result}.",
            schedule_result,
            "Личные сообщения участникам не отправлялись.",
        ]
        if previews and previews["issues"]:
            report_lines.append(
                "Предупреждения: " + "; ".join(previews["issues"])
            )
        report = "\n".join(report_lines)
        preview_failures = []
        try:
            await context.bot.send_message(
                context.admin_id, report, parse_mode=None
            )
        except TelegramAPIError:
            debug.exception("Не удалось отправить результат теста администратору")
            preview_failures.append("итоговый отчёт")

        if previews:
            preview_messages = [
                (
                    "👁 Предпросмотр сообщения для группы "
                    f"на {previews['sunday_date']:%d.%m.%Y}:\n\n"
                    f"{previews['group_text']}",
                    "сообщение для группы",
                ),
                *[
                    (
                        "👁 Предпросмотр личного сообщения "
                        f"({role}, {html.escape(name)}):\n\n{text}",
                        f"личное сообщение для роли «{role}»",
                    )
                    for role, name, text in previews["personal_messages"]
                ],
            ]
            for preview_text, description in preview_messages:
                try:
                    await context.bot.send_message(
                        context.admin_id,
                        preview_text,
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramAPIError:
                    debug.exception(
                        "Не удалось отправить предпросмотр: %s", description
                    )
                    preview_failures.append(description)

        if preview_failures:
            try:
                await message.answer(
                    "Не удалось отправить администратору: "
                    + ", ".join(preview_failures),
                    parse_mode=None,
                )
            except TelegramAPIError:
                debug.exception(
                    "Не удалось сообщить администратору об ошибке предпросмотра"
                )

        interaction.info(
            "Тест уведомлений запущен администратором tg_id=%s; "
            "группа: %s; предпросмотр: %s",
            context.admin_id,
            group_result,
            "готов" if previews else "ошибка",
        )

    @router.message(Command("test_notify"))
    async def on_test_notify(message: Message) -> None:
        if not message.from_user or message.from_user.id != context.admin_id:
            return
        if message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        registry = service_registry()
        if registry is None:
            await message.answer(
                "Реестр служений недоступен; нельзя выбрать чат проверки.",
                parse_mode=None,
            )
            return
        try:
            services = registry.list_services()
        except sqlite3.Error:
            debug.exception("Не удалось загрузить служения для теста доставки")
            await message.answer(
                "Не удалось прочитать список служений.", parse_mode=None
            )
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=service_label(service),
                        callback_data=f"service:test_notify:{service['id']}",
                    )
                ]
                for service in services
                if service["group_chat_id"] is not None
            ]
        )
        if not keyboard.inline_keyboard:
            await message.answer(
                "Нет служений с настроенным групповым чатом.",
                parse_mode=None,
            )
            return
        await message.answer(
            "Выберите служение, в групповой чат которого отправить тестовое "
            "сообщение. Личные сообщения участникам не отправляются.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    @router.callback_query(F.data.startswith(f"{CONFIRM_PREFIX}:"))
    async def on_confirm(cb: CallbackQuery) -> None:  # noqa: D401
        parts = cb.data.split(":", 2)
        if len(parts) != 3:
            await cb.answer("Некорректные данные кнопки")
            return
        _, sunday_iso, role = parts
        state = context.state_cache.state
        participant = (
            state.get("participants", {}).get(role)
            if state and state.get("sunday_date") == sunday_iso
            else None
        )
        if not participant or participant.get("tg_id") != cb.from_user.id:
            await _confirm_locked(cb, context, sunday_iso, role, interaction, debug)
            return

        locks = getattr(context, "confirmation_locks", None)
        if locks is None:
            locks = {}
            context.confirmation_locks = locks
        for key in tuple(locks):
            if key[0] != sunday_iso and not locks[key].locked():
                del locks[key]
        lock = locks.setdefault((sunday_iso, role), asyncio.Lock())
        async with lock:
            await _confirm_locked(cb, context, sunday_iso, role, interaction, debug)

    dispatcher.include_router(admin_router)
    dispatcher.include_router(router)


async def _confirm_locked(
    cb: CallbackQuery,
    context: Any,
    sunday_iso: str,
    role: str,
    interaction: logging.Logger,
    debug: logging.Logger,
) -> None:
    state = context.state_cache.state
    if not state or state.get("sunday_date") != sunday_iso:
        await cb.answer("Это уведомление устарело")
        return

    participant = state.get("participants", {}).get(role)
    if not participant or participant.get("tg_id") != cb.from_user.id:
        await cb.answer("Вы не назначены на эту роль")
        return

    first_confirmation = not participant.get("confirmed")
    if first_confirmation:
        participant["confirmed"] = True
    if first_confirmation or participant.get("confirmation_persisted") is False:
        confirmation_persisted = await _persist_confirmation(context, debug)
        if not confirmation_persisted:
            participant["confirmation_persisted"] = False
            await cb.answer(
                "Не удалось сохранить подтверждение. Нажмите кнопку повторно."
            )
            return
        participant.pop("confirmation_persisted", None)

    if first_confirmation:
        if cb.message and cb.message.text:
            try:
                await cb.message.edit_text(
                    cb.message.text + "\n\n✅ Подтверждено",
                    parse_mode=None,
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось отредактировать ЛС")

    group_update_blocked_reason = None
    registry = getattr(context, "service_registry", None)
    if not participant.get("group_update_sent") and registry is not None:
        try:
            assembly = registry.get_service("assembly")
        except sqlite3.Error:
            debug.exception(
                "Не удалось проверить состояние служения «Собрание»"
            )
            group_update_blocked_reason = (
                "не удалось проверить состояние служения"
            )
        else:
            if assembly is None or not assembly["enabled"]:
                group_update_blocked_reason = "служение отключено"

    if not participant.get("group_update_sent") and not group_update_blocked_reason:
        group_chat_id = participant.get(
            "group_chat_id",
            state.get("group_chat_id", context.group_chat_id),
        )
        try:
            await context.bot.send_message(
                group_chat_id,
                f"🔔 Участник {participant['name']} подтвердил роль {role}",
                parse_mode=None,
            )
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось отправить апдейт в группу")
        else:
            participant["group_update_sent"] = True
            participant["group_update_persisted"] = await _persist_confirmation(
                context, debug
            )
    elif not participant.get("group_update_persisted", True):
        participant["group_update_persisted"] = await _persist_confirmation(
            context, debug
        )

    interaction.info(
        "Подтверждение: role=%s tg_id=%s username=@%s name=%s",
        role, cb.from_user.id, cb.from_user.username, participant["name"],
    )
    if participant.get("group_update_sent") and participant.get(
        "group_update_persisted", True
    ):
        await cb.answer(
            "Принято!" if first_confirmation else "Вы уже подтвердили участие!"
        )
    elif participant.get("group_update_sent"):
        await cb.answer(
            "Уведомление отправлено, но состояние не удалось сохранить. "
            "Нажмите кнопку повторно для повтора сохранения."
        )
    elif group_update_blocked_reason:
        await cb.answer(
            "Подтверждение сохранено; уведомление в группу не отправлено: "
            f"{group_update_blocked_reason}."
        )
    else:
        await cb.answer(
            "Подтверждение сохранено, но уведомление не отправлено. "
            "Нажмите кнопку повторно для повтора."
        )
