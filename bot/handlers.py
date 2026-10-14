"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
from typing import Any

from aiogram import F, Dispatcher, Router
from aiogram.enums import ChatType
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
    pending_shares: dict[
        int, tuple[int, str, str, str | None, str | None]
    ] = {}
    pending_admin_confirmations: dict[
        tuple[int, str, int],
        tuple[str, str | None, str | None, str | None, str, str],
    ] = {}
    pending_task_choices: dict[str, tuple[int, str, str, str | None]] = {}
    pending_task_pages: dict[str, tuple[int, str, str, int]] = {}
    pending_contact_person_choices: dict[
        str, tuple[int, str, str, str]
    ] = {}
    pending_contact_navigation: dict[str, tuple[int, str, str]] = {}
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
        for key in list(pending_task_pages):
            if pending_task_pages[key][0] == actor_id:
                pending_task_pages.pop(key, None)
        for key in list(pending_contact_person_choices):
            if pending_contact_person_choices[key][0] == actor_id:
                pending_contact_person_choices.pop(key, None)
        for key in list(pending_contact_navigation):
            if pending_contact_navigation[key][0] == actor_id:
                pending_contact_navigation.pop(key, None)
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
            admins = registry.list_service_admin_details(service_id)
            admins_text = ", ".join(
                f"{admin['telegram_id']}"
                + (
                    f" (@{admin['username']})"
                    if admin["username"]
                    else ""
                )
                for admin in admins
            )
            contacts = registry.list_responsible_contacts(
                actor_id, service_id
            )
            lines.append(
                f"Администраторы (Telegram ID и username): "
                f"{admins_text or 'не назначены'}"
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
            elif operation in {"service_admin_add", "service_sync"}:
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
            "service_sync": "Скачайте актуальный график выбранной службы и проверьте привязки ответственных.",
            "service_notify": "Выберите служение и укажите, какой параметр уведомлений изменить.",
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
                actor_id, "service_chat", service_id, None, None
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
            if service is None:
                raise ValueError("Служение не найдено.")
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "session_id": secrets.token_urlsafe(9),
                "step": "menu",
                "reminder_days": service["reminder_days"],
                "reminder_time": service["reminder_time"],
                "group_template": service["group_template"],
                "personal_templates": dict(
                    service["personal_templates"] if service else {}
                ),
            }
            await show_notification_settings_menu(message, actor_id, service_id)
            return
        elif operation == "service_admin_remove":
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "step": "text",
            }
        elif operation in {"service_contact", "service_sync"}:
            pending_inputs[actor_id] = {
                "operation": "service_contact",
                "service_id": service_id,
                "step": "choose_task",
            }
            prompt = (
                f"{label}: актуальный файл графика загружен. "
                "Сверьте задачи и ответственных с показанными привязками; "
                "выберите имя, если требуется добавить или изменить Telegram."
                if operation == "service_sync"
                else (
                    f"{label}: выберите задачу и ответственного из "
                    "актуального файла графика, чтобы привязать пользователя "
                    "Telegram."
                )
            )
            await message.answer(
                prompt,
                parse_mode=None,
            )
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
            text=task if len(task) <= 48 else task[:45] + "...",
            callback_data=f"task:{operation}:{token}",
        )

    def contact_navigation_button(
        actor_id: int, service_id: str, action: str
    ) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(8)
        pending_contact_navigation[token] = (actor_id, service_id, action)
        label = {
            "tasks": "К списку задач",
            "finish": "Завершить привязку",
        }[action]
        return InlineKeyboardButton(
            text=label,
            callback_data=f"contact_nav:{token}",
        )

    async def send_task_choices_page(
        message: Message,
        actor_id: int,
        service_id: str,
        operation: str,
        page: int = 0,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != operation
            or pending.get("service_id") != service_id
        ):
            await message.answer(
                "Выбор задач устарел. Запустите команду заново.",
                parse_mode=None,
            )
            return

        tasks = pending.get("schedule_tasks", [])
        page_size = 10
        page_count = max(1, (len(tasks) + page_size - 1) // page_size)
        page = min(max(page, 0), page_count - 1)
        page_tasks = tasks[page * page_size:(page + 1) * page_size]
        rows = []
        if operation == "service_contact":
            assignments = pending.get("schedule_assignments", {})
            registered = pending.get("registered_contacts", {})
            details = []
            for task in page_tasks:
                schedule_names = ", ".join(assignments.get(task, []))
                saved_contacts = registered.get(task, [])
                saved_text = (
                    ", ".join(
                        f"{contact['name']} (ID {contact['telegram_id']}"
                        + (
                            f", @{contact['username']}"
                            if contact["username"]
                            else ""
                        )
                        + ")"
                        for contact in saved_contacts
                    )
                    if saved_contacts
                    else "не привязаны"
                )
                details.append(
                    f"• {task} — по расписанию: {schedule_names}; "
                    f"в боте: {saved_text}"
                )
            if details:
                intro = "Задачи из графика и привязанные в боте ответственные:\n"
                for chunk in _telegram_text_chunks(intro + "\n".join(details)):
                    await message.answer(chunk, parse_mode=None)
            elif not tasks:
                await message.answer(
                    "В расписании нет задач с назначенными ответственными "
                    "для этого служения.",
                    parse_mode=None,
                )

        if operation == "service_contact":
            rows.extend(
                [
                    _task_button(
                        actor_id,
                        service_id,
                        task,
                        operation=operation,
                        task_choices=pending_task_choices,
                    )
                ]
                for task in page_tasks
            )
            rows.append([
                contact_navigation_button(
                    actor_id, service_id, "finish"
                )
            ])
        else:
            for task in page_tasks:
                token = secrets.token_urlsafe(8)
                pending_notify_task_choices[token] = (
                    actor_id, service_id, task
                )
                label = task if len(task) <= 48 else task[:45] + "..."
                rows.append([
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"notify_task:{token}",
                    )
                ])
            token = secrets.token_urlsafe(8)
            pending_notify_task_choices[token] = (actor_id, service_id, None)
            rows.append([
                InlineKeyboardButton(
                    text="Вернуться к настройкам",
                    callback_data=f"notify_task:{token}",
                )
            ])

        navigation = []
        if page > 0:
            token = secrets.token_urlsafe(8)
            pending_task_pages[token] = (
                actor_id, operation, service_id, page - 1
            )
            navigation.append(
                InlineKeyboardButton(
                    text="← Назад", callback_data=f"task_page:{token}"
                )
            )
        if page + 1 < page_count:
            token = secrets.token_urlsafe(8)
            pending_task_pages[token] = (
                actor_id, operation, service_id, page + 1
            )
            navigation.append(
                InlineKeyboardButton(
                    text="Далее →", callback_data=f"task_page:{token}"
                )
            )
        if navigation:
            rows.append(navigation)

        if operation == "service_contact":
            prompt = (
                f"Выберите задачу из графика (страница {page + 1}/{page_count})."
                if tasks
                else "Для этого служения пока нет задач с ответственными в графике."
            )
        else:
            prompt = (
                "Выберите задачу из графика для индивидуального текста "
                f"(страница {page + 1}/{page_count})."
                if tasks
                else "В расписании пока нет задач с назначенными "
                "ответственными. Можно завершить и сохранить настройки."
            )
        await message.answer(
            prompt,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def offer_contact_name_choices(
        message: Message,
        actor_id: int,
        service_id: str,
        task: str,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or task not in pending.get("schedule_assignments", {})
        ):
            await message.answer(
                "Задача отсутствует в актуальном файле расписания.",
                parse_mode=None,
            )
            return

        pending["step"] = "choose_contact_person"
        pending["active_task"] = task
        for token, choice in list(pending_contact_person_choices.items()):
            if choice[:3] == (actor_id, service_id, task):
                pending_contact_person_choices.pop(token, None)
        for token, choice in list(pending_contact_navigation.items()):
            if choice[:2] == (actor_id, service_id):
                pending_contact_navigation.pop(token, None)
        registered = pending.get("registered_contacts", {}).get(task, [])
        contacts_by_name = {
            contact["name"]: contact for contact in registered
        }
        rows = []
        details = []
        for person_name in pending["schedule_assignments"][task]:
            contact = contacts_by_name.get(person_name)
            if contact:
                status = (
                    f"уже привязан Telegram ID {contact['telegram_id']}"
                    + (
                        f" (@{contact['username']})"
                        if contact["username"]
                        else ""
                    )
                )
                action = "Изменить привязку"
            else:
                status = "данные Telegram не указаны"
                action = "Выбрать пользователя"
            details.append(f"• {person_name} — {status}")
            token = secrets.token_urlsafe(8)
            pending_contact_person_choices[token] = (
                actor_id, service_id, task, person_name
            )
            label = (
                f"{action}: {person_name}"
                if len(person_name) <= 40
                else f"{action}: {person_name[:37]}..."
            )
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"contact_person:{token}",
                )
            ])
        rows.extend(
            [
                [
                    contact_navigation_button(
                        actor_id, service_id, "tasks"
                    )
                ],
                [
                    contact_navigation_button(
                        actor_id, service_id, "finish"
                    )
                ],
            ]
        )
        await message.answer(
            "Ответственные за задачу «"
            + task
            + "» по файлу расписания:\n"
            + "\n".join(details)
            + "\nВыберите имя, чтобы привязать или изменить Telegram-пользователя. "
            "После каждого изменения список появится снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
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
        task_loader = getattr(context, "load_service_task_assignments", None)
        if not callable(task_loader):
            await message.answer(
                "Загрузка задач из файла расписания недоступна.",
                parse_mode=None,
            )
            return
        try:
            assignments = await task_loader(service_id)
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except Exception:
            debug.exception("Не удалось прочитать задачи из графика служений")
            await message.answer(
                "Не удалось прочитать файл графика служений. "
                "Проверьте доступность расписания и попробуйте снова.",
                parse_mode=None,
            )
            return
        try:
            contacts = registry.list_responsible_contacts(actor_id, service_id)
        except (PermissionError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось прочитать привязки ответственных")
            await message.answer(
                "Не удалось прочитать список привязанных пользователей.",
                parse_mode=None,
            )
            return
        pending = pending_inputs[actor_id]
        pending["schedule_assignments"] = assignments
        pending["schedule_tasks"] = sorted(assignments)
        pending["registered_contacts"] = {}
        for contact in contacts:
            pending["registered_contacts"].setdefault(
                contact["task"], []
            ).append(contact)
        await send_task_choices_page(
            message, actor_id, service_id, "service_contact"
        )

    async def offer_notify_task_choices(
        message: Message, actor_id: int, service_id: str
    ) -> bool:
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
            return False
        if "schedule_assignments" in pending:
            assignments = pending["schedule_assignments"]
        else:
            task_loader = getattr(context, "load_service_task_assignments", None)
            if not callable(task_loader):
                await message.answer(
                    "Загрузка задач из файла расписания недоступна.",
                    parse_mode=None,
                )
                return False
            try:
                assignments = await task_loader(service_id)
            except Exception:
                debug.exception("Не удалось прочитать задачи из графика служений")
                await message.answer(
                    "Не удалось прочитать файл графика служений. "
                    "Проверьте доступность расписания и попробуйте снова.",
                    parse_mode=None,
                )
                return False
        pending["schedule_assignments"] = assignments
        pending["schedule_tasks"] = sorted(assignments)
        pending["step"] = "choose_notify_task"
        await send_task_choices_page(
            message, actor_id, service_id, "service_notify"
        )
        return True

    async def show_notification_settings_menu(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
        ):
            await message.answer(
                "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        service = service_by_id(service_registry(), service_id)
        if service is None:
            await message.answer("Служение не найдено.", parse_mode=None)
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Дни до события: {pending['reminder_days']}",
                        callback_data=(
                            f"notify_edit:days:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"Время: {pending['reminder_time']}",
                        callback_data=(
                            f"notify_edit:time:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Текст группового сообщения",
                        callback_data=(
                            f"notify_edit:group_template:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=(
                            "Личные шаблоны "
                            f"({len(pending['personal_templates'])})"
                        ),
                        callback_data=(
                            f"notify_edit:personal:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Завершить и сохранить",
                        callback_data=(
                            f"notify_finish:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=(
                            f"notify_cancel:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        await message.answer(
            f"{service_label(service)} — выберите, что изменить. "
            "Текущие значения сохраняются, пока вы их не измените.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    def notification_edit_controls(
        service_id: str, session_id: str
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Завершить и сохранить",
                        callback_data=(
                            f"notify_finish:{service_id}:{session_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=(
                            f"notify_cancel:{service_id}:{session_id}"
                        ),
                    ),
                ]
            ]
        )

    async def notification_template_preview(
        actor_id: int, service_id: str, task: str | None = None
    ) -> str:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if pending is None or registry is None:
            return "Предпросмотр недоступен: настройки служения не найдены."
        service = service_by_id(registry, service_id)
        if service is None:
            return "Предпросмотр недоступен: служение не найдено."
        prepare_previews = getattr(
            context, "prepare_service_test_previews", None
        )
        if not callable(prepare_previews):
            return "Предпросмотр на предстоящее воскресенье недоступен."
        try:
            previews = await prepare_previews(
                service_id,
                group_template=pending["group_template"],
                personal_templates=pending["personal_templates"],
            )
        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            debug.exception(
                "Не удалось подготовить предпросмотр шаблона службы %s",
                service_id,
            )
            return (
                "Предпросмотр на предстоящее воскресенье недоступен "
                f"({type(exc).__name__})."
            )
        if task is None:
            current = pending["group_template"] or (
                "(не задан — используется текст по умолчанию)"
            )
            return (
                f"Текущий текст группового шаблона:\n{current}\n\n"
                f"Пример на {previews['sunday_date']:%d.%m.%Y}:\n"
                f"{previews['group_text']}"
            )

        default_template = (
            "{name}, вы назначены на задачу «{task}» службы "
            "«{service_name}» на {sunday_date}."
        )
        current = pending["personal_templates"].get(task, default_template)
        examples = [
            item
            for item in previews["personal_messages"]
            if item["task"] == task
        ]
        example_text = "\n\n".join(
            f"{item['name']} (ID {item['telegram_id']}):\n{item['text']}"
            for item in examples
        ) or "На предстоящее воскресенье для этой задачи нет назначенных пользователей."
        return (
            f"Текущий личный шаблон для задачи «{task}»:\n{current}\n\n"
            f"Пример на {previews['sunday_date']:%d.%m.%Y} "
            f"для текущих привязок:\n{example_text}"
        )

    async def begin_notification_edit(
        message: Message, actor_id: int, service_id: str, field: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
        ):
            await message.answer(
                "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        if field == "personal":
            if "schedule_assignments" not in pending:
                task_loader = getattr(
                    context, "load_service_task_assignments", None
                )
                if not callable(task_loader):
                    await message.answer(
                        "Загрузка задач из файла расписания недоступна.",
                        parse_mode=None,
                    )
                    return
                try:
                    pending["schedule_assignments"] = await task_loader(
                        service_id
                    )
                except Exception:
                    debug.exception(
                        "Не удалось прочитать задачи для личных шаблонов"
                    )
                    await message.answer(
                        "Не удалось прочитать файл графика. "
                        "Попробуйте выбрать этот пункт ещё раз.",
                        parse_mode=None,
                    )
                    return
                pending["schedule_tasks"] = sorted(
                    pending["schedule_assignments"]
                )
            pending["step"] = "choose_notify_task"
            await send_task_choices_page(
                message, actor_id, service_id, "service_notify"
            )
            return
        prompts = {
            "days": (
                "Введите число дней до события от 0 до 6. "
                f"Текущее значение: {pending['reminder_days']}."
            ),
            "time": (
                "Введите время в формате HH:MM. "
                f"Текущее значение: {pending['reminder_time']}."
            ),
            "group_template": (
                "Введите новый текст для группы. Шаблоны: {service_name}, "
                "{sunday_date}, {task_assignments}. "
                "Пустое значение не меняет текущий текст."
            ),
        }
        if field not in prompts:
            await message.answer("Неизвестный параметр.", parse_mode=None)
            return
        pending["step"] = field
        text = prompts[field]
        if field == "group_template":
            text += "\n\n" + await notification_template_preview(
                actor_id, service_id
            )
        chunks = _telegram_text_chunks(text)
        for index, chunk in enumerate(chunks):
            await message.answer(
                chunk,
                reply_markup=(
                    notification_edit_controls(
                        service_id, pending["session_id"]
                    )
                    if index == len(chunks) - 1
                    else None
                ),
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
        text = (
            f"Введите индивидуальный текст напоминания для задачи "
            f"«{task}». Шаблоны: {{name}}, {{task}}, {{service_name}}, "
            f"{{sunday_date}}, {{telegram_id}}, {{username}}.\n\n"
            + await notification_template_preview(
                actor_id, service_id, task
            )
        )
        chunks = _telegram_text_chunks(text)
        for index, chunk in enumerate(chunks):
            await message.answer(
                chunk,
                reply_markup=(
                    notification_edit_controls(
                        service_id, pending["session_id"]
                    )
                    if index == len(chunks) - 1
                    else None
                ),
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
            or task is None
            or task not in pending.get("schedule_tasks", [])
        ):
            await callback.answer("Запрос задачи устарел.")
            return
        await offer_contact_name_choices(
            callback.message, callback.from_user.id, service_id, task
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
            or (
                selection[2] is not None
                and selection[2] not in pending.get("schedule_tasks", [])
            )
        ):
            await callback.answer("Этот выбор устарел. Начните настройку заново.")
            return
        task = selection[2]
        if task is None:
            await show_notification_settings_menu(
                callback.message, callback.from_user.id, selection[1]
            )
        else:
            await begin_notify_template(
                callback.message, callback.from_user.id, selection[1], task
            )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("notify_task_new:"))
    async def on_notify_new_task(callback: CallbackQuery) -> None:
        await callback.answer(
            "Добавление произвольных задач отключено. "
            "Используйте задачи из файла расписания."
        )

    async def offer_user_selection(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending_inputs[actor_id] = {
            "operation": "service_admin_add",
            "service_id": service_id,
            "step": "await_user",
        }
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_admin_add", service_id, None, None
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
            "Либо отправьте его Telegram ID, при желании дополнив "
            "username через пробел: 123456789 @username. "
            "Администратор также может назначить самого себя. "
            "Назначение подтвердите на следующем шаге.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def offer_contact_user_selection(
        message: Message,
        actor_id: int,
        service_id: str,
        task: str,
        contact_name: str,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or pending.get("active_task") != task
        ):
            await message.answer(
                "Сценарий привязки устарел. Запустите /service_contact заново.",
                parse_mode=None,
            )
            return
        pending["step"] = "await_user"
        pending["active_contact_name"] = contact_name
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_contact", service_id, task, contact_name
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
            f"Нажмите кнопку и выберите пользователя Telegram для имени "
            f"«{contact_name}» (задача «{task}»). "
            "Или отправьте Telegram ID, при желании дополнив username "
            "через пробел: 123456789 @username. "
            "Проверьте выбор — назначение подтвердите на следующем шаге.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    def parse_manual_user(text: str) -> tuple[int, str]:
        parts = text.split()
        if len(parts) not in {1, 2}:
            raise ValueError(
                "Введите Telegram ID и при необходимости username "
                "через пробел, например: 123456789 @username."
            )
        try:
            user_id = int(parts[0])
        except ValueError as exc:
            raise ValueError(
                "Telegram ID должен быть положительным числом."
            ) from exc
        if user_id <= 0:
            raise ValueError("Telegram ID должен быть положительным числом.")
        username = ""
        if len(parts) == 2:
            username = parts[1]
            if username.startswith("@"):
                username = username[1:]
        if username and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{4,31}", username
        ):
            raise ValueError("Некорректный Telegram username.")
        return user_id, username

    async def ask_user_assignment_confirmation(
        message: Message,
        actor_id: int,
        service_id: str,
        operation: str,
        user_id: int,
        username: str,
        task: str | None = None,
        contact_name: str | None = None,
        display_name: str | None = None,
    ) -> None:
        key = (actor_id, service_id, user_id)
        confirmation_id = secrets.token_urlsafe(8)
        pending_admin_confirmations[key] = (
            operation,
            task,
            contact_name,
            display_name,
            username,
            confirmation_id,
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
                            f"{callback_prefix}_confirm:{service_id}:"
                            f"{user_id}:{confirmation_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=(
                            f"{callback_prefix}_cancel:{service_id}:"
                            f"{user_id}:{confirmation_id}"
                        ),
                    ),
                ]
            ]
        )
        title = (
            "назначить администратором служения"
            if operation == "service_admin_add"
            else (
                f"привязать к записи «{contact_name}» из файла "
                f"для задачи «{task}»"
            )
        )
        user_display = display_name or f"Пользователь {user_id}"
        await message.answer(
            f"Подтвердите действие: {title}: {user_display}"
            + (f" (@{username})" if username else "")
            + f" (ID {user_id})?",
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
            for index, service in enumerate(visible):
                state = "включено" if service["enabled"] else "выключено"
                lines.append(f"{service_label(service)} — {state}")
                if is_super_admin:
                    admins = registry.list_service_admin_details(
                        service["id"]
                    )
                    admin_text = ", ".join(
                        f"{admin['telegram_id']}"
                        + (
                            f" (@{admin['username']})"
                            if admin["username"]
                            else ""
                        )
                        for admin in admins
                    )
                    lines.append(
                        "  Групповой чат: "
                        f"{service['group_chat_id'] or 'не задан'}; "
                        "администраторы: "
                        f"{admin_text or 'не назначены'}"
                    )
                if index + 1 < len(visible):
                    lines.append("")
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
        if len(parts) not in {3, 4}:
            await message.answer(
                "Укажите ID пользователя и необязательный username.",
                parse_mode=None,
            )
            return
        service_id, raw_telegram_id = parts[1:3]
        username = parts[3].lstrip("@") if len(parts) == 4 else ""

        def add_admin(actor_id: int) -> str:
            telegram_id = int(raw_telegram_id)
            registry = service_registry()
            registry.add_service_admin(
                actor_id, service_id, telegram_id, username
            )
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
        if len(prefix_parts) == 1 and not details:
            await request_service_input(
                message, "service_contact", prefix_parts[0]
            )
            return
        if len(prefix_parts) != 1 or details:
            await message.answer(
                "Ручной ввод задач и ответственных отключён. "
                "Укажите только служение, например /service_contact sound; "
                "задачу и имя нужно выбрать из файла расписания.",
                parse_mode=None,
            )
            return

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
            if len(service_parts) != 1:
                await message.answer(
                    "Настройки нужно вводить пошагово; индивидуальные задачи "
                    "выбираются из файла расписания. Используйте "
                    "/service_notify <service_id>.",
                    parse_mode=None,
                )
                return
            await request_service_input(
                message, "service_notify", service_parts[0]
            )
            return
        await message.answer(
            "Настройки нужно вводить пошагово; индивидуальные задачи "
            "выбираются из файла расписания. Используйте /service_notify "
            "<service_id>.",
            parse_mode=None,
        )

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
        for key in list(pending_task_pages):
            if pending_task_pages[key][0] == message.from_user.id:
                pending_task_pages.pop(key, None)
                had_request = True
        for key in list(pending_contact_person_choices):
            if pending_contact_person_choices[key][0] == message.from_user.id:
                pending_contact_person_choices.pop(key, None)
                had_request = True
        for key in list(pending_contact_navigation):
            if pending_contact_navigation[key][0] == message.from_user.id:
                pending_contact_navigation.pop(key, None)
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
            "service_sync",
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
                rows = []
                if service["group_chat_id"] is not None:
                    rows.append([
                        InlineKeyboardButton(
                            text="Проверить отправку в групповой чат",
                            callback_data=(
                                f"test_action:group:{service_id}"
                            ),
                        )
                    ])
                rows.append([
                    InlineKeyboardButton(
                        text="Показать тексты админу",
                        callback_data=f"test_action:preview:{service_id}",
                    )
                ])
                await callback.message.answer(
                    f"{service_label(service)}: выберите проверку.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                    parse_mode=None,
                )
            elif operation == "service_sync":
                if not registry.is_super_admin(callback.from_user.id):
                    raise PermissionError(
                        "Синхронизация графика доступна только супер-администратору."
                    )
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_sync",
                    service_id,
                    service_label(service),
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

    @admin_router.callback_query(F.data.startswith("notify_edit:"))
    async def on_notification_setting_edit(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await callback.answer("Настройка устарела. Запустите её заново.")
            return
        _, field, service_id, session_id = parts
        registry = service_registry()
        if registry is None or not is_service_admin(
            callback.from_user.id, service_id
        ):
            await callback.answer("Недостаточно прав.")
            return
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await callback.answer("Настройка устарела. Запустите её заново.")
            return
        await begin_notification_edit(
            callback.message, callback.from_user.id, service_id, field
        )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("notify_finish:"))
    async def on_notification_setting_finish(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("Настройка устарела. Запустите её заново.")
            return
        _, service_id, session_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        if not is_service_admin(callback.from_user.id, service_id):
            await callback.answer("Недостаточно прав.")
            return
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await callback.answer("Настройка устарела. Запустите её заново.")
            return
        await save_notification_setup(
            callback.message, callback.from_user.id
        )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("notify_cancel:"))
    async def on_notification_setting_cancel(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer()
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("Настройка устарела.")
            return
        _, service_id, session_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        if (
            callback.message.chat.type != ChatType.PRIVATE
            or pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await callback.answer("Настройка устарела.")
            return
        clear_pending_actor(callback.from_user.id)
        await callback.message.answer(
            "Изменения отменены, сохранённые настройки не изменены.",
            parse_mode=None,
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
        if len(parts) != 4:
            await callback.answer("Некорректное подтверждение.")
            return
        action, service_id, raw_user_id, confirmation_id = parts
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
        if (
            selected_user is None
            or selected_user[0] not in {"service_admin_add", "service_contact"}
            or selected_user[5] != confirmation_id
            or action.rsplit("_", 1)[0] != expected_prefix
        ):
            await callback.answer("Запрос устарел. Начните команду заново.")
            return
        pending_admin_confirmations.pop(key, None)
        (
            operation,
            task,
            contact_name,
            selected_display_name,
            username,
            _,
        ) = selected_user
        if action_type == "cancel":
            await callback.message.answer("Назначение отменено.", parse_mode=None)
            if operation == "service_admin_add":
                pending_inputs.pop(callback.from_user.id, None)
            else:
                contact_pending = pending_inputs.get(callback.from_user.id)
                if contact_pending is not None:
                    contact_pending["step"] = "choose_contact_person"
                    contact_pending.pop("active_contact_name", None)
                await offer_contact_name_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    task or "",
                )
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
            if operation == "service_admin_add":
                registry.add_service_admin(
                    callback.from_user.id, service_id, user_id, username or ""
                )
            else:
                registry.set_responsible_contact(
                    callback.from_user.id,
                    service_id,
                    task or "",
                    contact_name or f"Пользователь {user_id}",
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
            display_name = selected_display_name or f"Пользователь {user_id}"
            if operation == "service_admin_add":
                response = (
                    f"{display_name} (ID {user_id}"
                    + (f", @{username}" if username else "")
                    + ") назначен администратором служения."
                )
            else:
                response = (
                    f"Ответственный «{contact_name}» из файла расписания "
                    f"связан с {display_name} (ID {user_id}"
                    + (f", @{username}" if username else "")
                    + f") для задачи «{task}»."
                )
            await callback.message.answer(response, parse_mode=None)
            if operation == "service_contact":
                contact_pending = pending_inputs.get(callback.from_user.id)
                if contact_pending is not None:
                    registered = contact_pending.setdefault(
                        "registered_contacts", {}
                    ).setdefault(task or "", [])
                    registered[:] = [
                        item for item in registered
                        if (
                            item["name"] != contact_name
                            and item["telegram_id"] != user_id
                        )
                    ]
                    registered.append(
                        {
                            "task": task,
                            "name": contact_name,
                            "telegram_id": user_id,
                            "username": username or "",
                        }
                    )
        if operation == "service_admin_add":
            pending_inputs.pop(callback.from_user.id, None)
        else:
            contact_pending = pending_inputs.get(callback.from_user.id)
            if contact_pending is not None:
                contact_pending["step"] = "choose_contact_person"
                contact_pending.pop("active_contact_name", None)
            if task:
                await offer_contact_name_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    task,
                )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("task_page:"))
    async def on_task_page(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        token = callback.data.partition(":")[2]
        selection = pending_task_pages.pop(token, None)
        if selection is None or selection[0] != callback.from_user.id:
            await callback.answer("Этот список устарел. Запустите команду заново.")
            return
        _, operation, service_id, page = selection
        pending = pending_inputs.get(callback.from_user.id)
        expected_step = {
            "service_contact": "choose_task",
            "service_notify": "choose_notify_task",
        }.get(operation)
        if (
            expected_step is None
            or pending is None
            or pending.get("operation") != operation
            or pending.get("service_id") != service_id
            or pending.get("step") != expected_step
        ):
            await callback.answer("Этот список устарел. Запустите команду заново.")
            return
        await send_task_choices_page(
            callback.message,
            callback.from_user.id,
            service_id,
            operation,
            page,
        )
        await callback.answer()

    @admin_router.callback_query(F.data.startswith("contact_person:"))
    async def on_contact_person_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        token = callback.data.partition(":")[2]
        selection = pending_contact_person_choices.pop(token, None)
        pending = pending_inputs.get(callback.from_user.id)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != selection[1]
            or pending.get("step") != "choose_contact_person"
            or pending.get("active_task") != selection[2]
            or selection[3]
            not in pending.get("schedule_assignments", {}).get(
                selection[2], []
            )
        ):
            await callback.answer("Этот выбор устарел. Начните команду заново.")
            return
        try:
            await offer_contact_user_selection(
                callback.message,
                callback.from_user.id,
                selection[1],
                selection[2],
                selection[3],
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

    @admin_router.callback_query(F.data.startswith("contact_nav:"))
    async def on_contact_navigation(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        token = callback.data.partition(":")[2]
        selection = pending_contact_navigation.pop(token, None)
        pending = pending_inputs.get(callback.from_user.id)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != selection[1]
            or pending.get("step") != "choose_contact_person"
        ):
            await callback.answer("Этот выбор устарел. Начните команду заново.")
            return
        _, service_id, action = selection
        if action == "tasks":
            pending["step"] = "choose_task"
            pending.pop("active_task", None)
            pending.pop("active_contact_name", None)
            await send_task_choices_page(
                callback.message,
                callback.from_user.id,
                service_id,
                "service_contact",
            )
        else:
            clear_pending_actor(callback.from_user.id)
            await callback.message.answer(
                "Привязка ответственных завершена.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
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
        contact_name = pending[4]
        active = pending_inputs.get(message.from_user.id)
        if (
            active is None
            or active.get("operation") != operation
            or active.get("service_id") != pending[2]
            or active.get("step") != "await_user"
            or (
                operation == "service_contact"
                and (
                    active.get("active_task") != task
                    or active.get("active_contact_name") != contact_name
                )
            )
        ):
            await message.answer(
                "Выбор пользователя устарел. Выберите задачу и имя заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if operation == "service_contact" and (not task or not contact_name):
            await message.answer(
                "Не удалось определить задачу. Запустите привязку заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        selected_display_name = " ".join(
            part.strip()
            for part in (
                selected_user.first_name or "",
                getattr(selected_user, "last_name", "") or "",
            )
            if part and part.strip()
        )
        await ask_user_assignment_confirmation(
            message,
            message.from_user.id,
            pending[2],
            operation,
            user_id,
            selected_user.username or "",
            task,
            contact_name,
            selected_display_name,
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
                if pending.get("step") != "await_user":
                    raise ValueError(
                        "Сначала выберите пользователя или введите "
                        "его Telegram ID."
                    )
                user_id, username = parse_manual_user(text)
                await ask_user_assignment_confirmation(
                    message,
                    message.from_user.id,
                    service_id,
                    operation,
                    user_id,
                    username,
                )
                return
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
                if pending.get("step") != "await_user":
                    raise ValueError(
                        "Сначала выберите задачу и ответственного кнопками."
                    )
                task = pending.get("active_task")
                contact_name = pending.get("active_contact_name")
                if not task or not contact_name:
                    raise ValueError(
                        "Выбор ответственного устарел. Выберите его заново."
                    )
                user_id, username = parse_manual_user(text)
                await ask_user_assignment_confirmation(
                    message,
                    message.from_user.id,
                    service_id,
                    operation,
                    user_id,
                    username,
                    task,
                    contact_name,
                )
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
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
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
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "group_template":
                    if not text:
                        raise ValueError("Текст для группы не может быть пустым.")
                    pending["group_template"] = text
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "notify_template":
                    if not text:
                        raise ValueError(
                            "Текст индивидуального напоминания не может быть пустым."
                        )
                    active_task = pending.get("active_task")
                    if not active_task:
                        raise ValueError(
                            "Выбор задачи устарел. Запустите /service_notify заново."
                        )
                    pending["personal_templates"][active_task] = text
                    pending.pop("active_task", None)
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
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

    @admin_router.message(Command("service_sync"))
    async def on_service_sync(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        clear_pending_actor(message.from_user.id)
        payload = (message.text or "").partition(" ")[2].strip()
        if payload:
            await message.answer(
                "Служение нужно выбрать кнопкой. Используйте /service_sync "
                "без параметров.",
                parse_mode=None,
            )
            return
        await request_service_input(message, "service_sync")

    async def execute_test_notification(
        message: Message, service: dict, action: str
    ) -> None:
        if action == "group":
            group_chat_id = service["group_chat_id"]
            if group_chat_id is None:
                await message.answer(
                    "Для этого служения не настроен групповой чат.",
                    parse_mode=None,
                )
                return
            try:
                await context.bot.send_message(
                    group_chat_id,
                    f"🔧 ТЕСТ службы «{service['name']}»: проверка доставки. "
                    "Обычная рассылка не запускалась.",
                    parse_mode=None,
                )
            except TelegramAPIError as exc:
                debug.exception("Тестовая отправка в группу не удалась")
                await message.answer(
                    f"Не удалось отправить тест в группу: {type(exc).__name__}.",
                    parse_mode=None,
                )
            else:
                await message.answer(
                    f"Тестовое сообщение службы «{service['name']}» отправлено "
                    "в настроенную группу. Рассылка участникам не запускалась.",
                    parse_mode=None,
                )
            return

        prepare_previews = getattr(
            context, "prepare_service_test_previews", None
        )
        if not callable(prepare_previews):
            await message.answer(
                "Предпросмотр сообщений для служений недоступен.",
                parse_mode=None,
            )
            return
        try:
            previews = await prepare_previews(service["id"])
        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            debug.exception(
                "Не удалось подготовить предпросмотр службы %s",
                service["id"],
            )
            await message.answer(
                f"Не удалось подготовить предпросмотр: {type(exc).__name__}.",
                parse_mode=None,
            )
            return

        output = [
            f"👁 Предпросмотр службы «{service['name']}» "
            f"на {previews['sunday_date']:%d.%m.%Y}:",
            "",
            previews["group_text"],
        ]
        output.extend(
            f"\nЛичное сообщение для {item['name']} "
            f"(задача «{item['task']}»):\n{item['text']}"
            for item in previews["personal_messages"]
        )
        if previews["issues"]:
            output.extend(("", "Замечания:", *previews["issues"]))
        for chunk in _telegram_text_chunks("\n".join(output)):
            try:
                await context.bot.send_message(
                    context.admin_id, chunk, parse_mode=None
                )
            except TelegramAPIError:
                debug.exception(
                    "Не удалось отправить администратору предпросмотр службы %s",
                    service["id"],
                )
                await message.answer(
                    "Не удалось доставить администратору полный предпросмотр.",
                    parse_mode=None,
                )
                return
        await message.answer(
            "Предпросмотр выбранной службы отправлен вам в личные сообщения. "
            "Сообщения в группу и участникам не отправлялись.",
            parse_mode=None,
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
            ]
        )
        await message.answer(
            "Сначала выберите служение, затем — вид проверки.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    @router.callback_query(F.data.startswith("test_action:"))
    async def on_test_notify_action(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or callback.from_user.id != context.admin_id
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        _, action, service_id = callback.data.split(":", 2)
        registry = service_registry()
        if registry is None:
            await callback.answer("Реестр служений недоступен.")
            return
        try:
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            if action not in {"group", "preview"}:
                raise ValueError("Неизвестный вид проверки.")
            await execute_test_notification(
                callback.message, service, action
            )
        except (ValueError, sqlite3.Error) as exc:
            await callback.message.answer(str(exc), parse_mode=None)
        await callback.answer()

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
    needs_confirmation_persist = (
        first_confirmation
        or participant.get("confirmation_persisted") is False
    )
    newly_persisted = needs_confirmation_persist
    if needs_confirmation_persist:
        participant.pop("confirmation_persisted", None)
        confirmation_persisted = await _persist_confirmation(context, debug)
        if not confirmation_persisted:
            participant["confirmation_persisted"] = False
            await cb.answer(
                "Не удалось сохранить подтверждение. Нажмите кнопку повторно."
            )
            return

    if newly_persisted:
        if cb.message and cb.message.text:
            try:
                await cb.message.edit_text(
                    cb.message.text + "\n\n✅ Подтверждено",
                    parse_mode=None,
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось отредактировать ЛС")

    group_update_blocked_reason = None
    active_group_chat_id = None
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
            elif assembly.get("group_chat_id") is None:
                group_update_blocked_reason = "групповой чат не настроен"
            else:
                active_group_chat_id = assembly.get("group_chat_id")

    if not participant.get("group_update_sent") and not group_update_blocked_reason:
        group_chat_id = (
            active_group_chat_id
            if active_group_chat_id is not None
            else participant.get(
                "group_chat_id",
                state.get("group_chat_id", context.group_chat_id),
            )
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
            "Принято!" if newly_persisted else "Вы уже подтвердили участие!"
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
