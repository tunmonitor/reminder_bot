"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sqlite3
import time
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
from .presentation import (
    format_task_assignments,
    task_assignment_names,
    task_emoji,
    task_is_informational,
)
from .ui import code as ui_code
from .service_registry import (
    GROUP_TEMPLATE_FIELDS,
    PERSONAL_TEMPLATE_FIELDS,
    validate_notification_template,
)
from .ui import (
    callback_text,
    clean_previous_menu,
    clean_previous_menu_by_id,
    layout_buttons,
    nav_row,
    render_screen_chunks,
    validate_inline_keyboard,
)

router = Router()
PERSONAL_TASK_CACHE_TTL_SECONDS = 300


async def _answer_callback(
    callback: CallbackQuery, text: str | None = None, **kwargs
) -> None:
    normalized = callback_text(text)
    if normalized is None:
        await callback.answer(**kwargs)
    else:
        await callback.answer(normalized, **kwargs)


def _status_text(text: str, icon: str) -> str:
    if text.startswith(("✅", "❌", "⚠️", "ℹ️", "⏳")):
        return text
    return f"{icon} {text}"


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
    pending_dialog_pages: dict[str, tuple[int, str, InlineKeyboardMarkup, int]] = {}
    pending_personal_actions: dict[
        str, tuple[int, str, str, str, dict[str, Any]]
    ] = {}

    async def _send_dialog(
        target: Message,
        text: str,
        *,
        _dialog_page: int = 0,
        clean_previous: bool = True,
        **kwargs,
    ) -> Message | None:
        kwargs.pop("parse_mode", None)
        keyboard_limit = kwargs.pop("_dialog_keyboard_limit", 6)
        markup = kwargs.pop("reply_markup", None)
        actor_id = getattr(getattr(target, "chat", None), "id", None)
        pending = (
            pending_inputs.get(actor_id)
            if getattr(getattr(target, "chat", None), "type", None)
            == ChatType.PRIVATE
            else None
        )
        for token, page_state in list(pending_dialog_pages.items()):
            if page_state[0] == actor_id:
                pending_dialog_pages.pop(token, None)
        if (
            clean_previous
            and pending is not None
            and pending.get("menu_message_id") is not None
            and pending.get("menu_chat_id") is not None
        ):
            await clean_previous_menu_by_id(
                context.bot,
                pending.get("menu_chat_id"),
                pending.get("menu_message_id"),
                debug,
            )
        if isinstance(markup, InlineKeyboardMarkup):
            buttons = [
                button
                for row in markup.inline_keyboard
                for button in row
            ]
            for button in buttons:
                label = button.text
                if not label.startswith(
                    (
                        "✅", "❌", "⚠️", "ℹ️", "⏳", "🟢", "🔴", "⚪",
                        "⚙️", "🔗", "👥", "👤", "📡", "🔔", "📝", "📊",
                        "📋", "📅", "🕒", "🎤", "📖", "🍞", "🧪", "👁",
                        "⏰", "📷", "🎛️", "💻", "🎬", "🎚️", "🖥️",
                        "🍽️", "☕", "📌", "🔕",
                        "🔧", "🧹", "🔄", "⬅️", "➡️", "🏠", "✖️", "➕",
                        "➖", "🗑", "✏️", "1️⃣", "2️⃣", "3️⃣", "4️⃣",
                    )
                ):
                    button.text = f"📋 {label}"

            if len(buttons) > keyboard_limit:
                page_size = 4
                pinned_buttons = [
                    button
                    for button in buttons
                    if button.text.startswith(("⬅️ К ", "↩️ К "))
                ]
                if pinned_buttons:
                    page_size = 3
                pageable_buttons = [
                    button for button in buttons
                    if button not in pinned_buttons
                ]
                page_count = max(
                    1, (len(pageable_buttons) + page_size - 1) // page_size
                )
                _dialog_page = min(max(_dialog_page, 0), page_count - 1)
                page_buttons = pageable_buttons[
                    _dialog_page * page_size:(_dialog_page + 1) * page_size
                ]
                navigation = []
                for action, next_page, label in (
                    ("prev", _dialog_page - 1, "⬅️ Назад"),
                    ("next", _dialog_page + 1, "Далее ➡️"),
                ):
                    if 0 <= next_page < page_count:
                        token = secrets.token_urlsafe(8)
                        if actor_id is not None:
                            pending_dialog_pages[token] = (
                                actor_id, text, markup, next_page
                            )
                        navigation.append(
                            InlineKeyboardButton(
                                text=label,
                                callback_data=f"ui_page:{token}",
                            )
                        )
                navigation = nav_row(
                    back=next(
                        (
                            button for button in navigation
                            if button.text.startswith("⬅️")
                        ),
                        None,
                    ),
                    save=next(
                        (
                            button for button in navigation
                            if button.text.endswith("➡️")
                        ),
                        None,
                    ),
                )
                rows = layout_buttons(
                    [*page_buttons, *navigation, *pinned_buttons]
                )
                validate_inline_keyboard(rows, max_buttons=keyboard_limit)
                markup = InlineKeyboardMarkup(inline_keyboard=rows)
                text = f"{text}\n📊 Страница {_dialog_page + 1}/{page_count}"
            else:
                rows = layout_buttons(buttons)
                validate_inline_keyboard(rows, max_buttons=keyboard_limit)
                markup = InlineKeyboardMarkup(inline_keyboard=rows)

        chunks = render_screen_chunks(text)
        sent_message = None
        for index, chunk in enumerate(chunks):
            sent_message = await target.answer(
                chunk,
                parse_mode="HTML",
                reply_markup=(
                    markup if index == len(chunks) - 1 else None
                ),
                **kwargs,
            )
        if sent_message is not None and pending is not None:
            pending["menu_message_id"] = sent_message.message_id
            pending["menu_chat_id"] = target.chat.id
        return sent_message

    @admin_router.callback_query(F.data.startswith("ui_page:"))
    async def on_dialog_page(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        _, token = callback.data.split(":", 1)
        page_state = pending_dialog_pages.pop(token, None)
        if page_state is None or page_state[0] != callback.from_user.id:
            await _answer_callback(callback, "Выбор устарел")
            return
        _, text, markup, page = page_state
        await clean_previous_menu(callback.message, debug)
        await _send_dialog(
            callback.message,
            text,
            reply_markup=markup,
            _dialog_page=page,
            clean_previous=False,
        )
        await _answer_callback(callback)

    def new_share_request_id() -> int:
        registry = service_registry()
        if registry is None:
            raise ValueError("Реестр служений временно недоступен.")
        return registry.next_share_request_id()

    def clear_pending_actor(actor_id: int) -> None:
        pending_inputs.pop(actor_id, None)
        for token, action in list(pending_personal_actions.items()):
            if action[0] == actor_id:
                pending_personal_actions.pop(token, None)
        for token, page in list(pending_dialog_pages.items()):
            if page[0] == actor_id:
                pending_dialog_pages.pop(token, None)
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

    async def clean_and_clear_pending_actor(actor_id: int) -> None:
        pending = pending_inputs.get(actor_id)
        if pending is not None:
            if pending.get("operation", "").startswith("my_notifications"):
                await clean_previous_personal_menu(pending)
            else:
                await clean_previous_menu_by_id(
                    context.bot,
                    pending.get("menu_chat_id"),
                    pending.get("menu_message_id"),
                    debug,
                )
        clear_pending_actor(actor_id)

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
        enabled_status = "🟢 включено" if service["enabled"] else "🔴 выключено"
        group_template_status = (
            "🟢 задан"
            if service["group_template"]
            else "⚪ свой текст не задан · используется шаблон по умолчанию"
        )
        personal_templates = service["personal_templates"]
        personal_template_status = (
            f"🟢 заданы ({len(personal_templates)})"
            if personal_templates
            else "⚪ не заданы"
        )
        lines = [
            f"⚙️ {service_label(service)} · {enabled_status}",
            "",
            "📡 Настройки",
            "Групповой чат · "
            + (
                ui_code(service["group_chat_id"])
                if service["group_chat_id"] is not None
                else ui_code(None)
            ),
            f"🔔 Напоминание · за {service['reminder_days']} дн. "
            f"в {service['reminder_time']}",
            f"📝 Свой текст для группы · {group_template_status}",
            f"📝 Личные шаблоны · {personal_template_status}",
        ]
        if service["group_template"]:
            lines.extend(
                ["", "Текущий групповой шаблон:", service["group_template"]]
            )
        if personal_templates:
            lines.extend(
                [
                    "",
                    "Текущие личные шаблоны:",
                    *(
                        f"{task}: {template}"
                        for task, template in sorted(personal_templates.items())
                    ),
                ]
            )
        lines.extend(
            [
                "",
                "ℹ️ Выберите параметр для изменения.",
            ]
        )
        return "\n".join(lines)

    async def request_service_input(
        message: Message,
        operation: str,
        service_id: str | None = None,
        actor_id: int | None = None,
    ) -> None:
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return
        if actor_id is None:
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
                        success_icon=None,
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
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось загрузить список служений")
            await _send_dialog(message, "Не удалось загрузить список служений.", parse_mode=None
            )
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            f"{'🟢' if service['enabled'] else '🔴'} "
                            f"{service_label(service)}"
                        ),
                        callback_data=f"service:{operation}:{service['id']}",
                    )
                ]
                for service in services
            ]
        )
        descriptions = {
            "service_settings": "ℹ️ Выберите служение для просмотра настроек.",
            "service_admin_add": "ℹ️ Выберите служение для назначения администратора.",
            "service_admin_remove": "ℹ️ Выберите служение для снятия администратора.",
            "service_chat": "ℹ️ Выберите служение для настройки группового чата.",
            "service_contact": "ℹ️ Выберите служение для привязки ответственных.",
            "service_sync": "ℹ️ Выберите служение для сверки привязок.",
            "service_notify": "ℹ️ Выберите служение для настройки уведомлений.",
        }
        await _send_dialog(message, descriptions[operation],
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
            pending_inputs[actor_id] = {
                "operation": "service_chat",
                "service_id": service_id,
                "step": "await_chat",
            }
            request_id = new_share_request_id()
            pending_shares[request_id] = (
                actor_id, "service_chat", service_id, None, None
            )
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="📡 Выбрать групповой чат",
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
            await _send_dialog(message, f"ℹ️ {label}: нажмите кнопку и выберите группу, в которой "
                "бот уже состоит. После выбора служение будет включено.",
                reply_markup=keyboard,
                parse_mode=None,
            )
            return

        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен.", parse_mode=None
            )
            return
        prompts = {
            "service_admin_add": (
                f"ℹ️ {label}: выберите пользователя кнопкой Telegram."
            ),
            "service_admin_remove": (
                f"ℹ️ {label}: введите Telegram ID администратора, которого "
                "нужно снять."
            ),
            "service_chat": (
                f"ℹ️ {label}: выберите групповой чат кнопкой Telegram."
            ),
            "service_contact": f"ℹ️ {label}: выберите или добавьте задачу.",
            "service_notify": (
                f"ℹ️ {label}: за сколько дней до воскресенья отправлять "
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
                "session_id": secrets.token_urlsafe(9),
                "step": "choose_task",
            }
            if operation == "service_sync":
                await _send_dialog(message, f"⏳ {label}: загружаю и проверяю актуальный график...",
                    parse_mode=None,
                )
                await offer_contact_task_choices(
                    message,
                    actor_id,
                    service_id,
                )
                return
            await offer_contact_task_choices(message, actor_id, service_id)
            return
        await _send_dialog(message, prompts[operation], parse_mode=None)

    async def run_registry_command(
        message: Message,
        operation,
        reply_markup: InlineKeyboardMarkup | None = None,
        success_icon: str | None = "✅",
    ) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        if service_registry() is None:
            await _send_dialog(message, "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return
        try:
            response = operation(message.from_user.id)
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Ошибка выполнения команды управления служениями")
            await _send_dialog(message, "Не удалось выполнить команду из-за ошибки базы настроек.",
                parse_mode=None,
            )
            return
        formatted_response = (
            _status_text(response, success_icon)
            if success_icon is not None
            else response
        )
        chunks = _telegram_text_chunks(formatted_response)
        for index, chunk in enumerate(chunks):
            await _send_dialog(message, chunk,
                parse_mode=None,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )

    def settings_keyboard(service_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔔 Изменить настройки уведомлений",
                        callback_data=f"service:edit_notify:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К списку служений",
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
            text=(
                f"{task_emoji(task)} {task}"
                if len(task) <= 44
                else f"{task_emoji(task)} {task[:41]}..."
            ),
            callback_data=f"task:{operation}:{token}",
        )

    def contact_navigation_button(
        actor_id: int, service_id: str, action: str
    ) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(8)
        pending_contact_navigation[token] = (actor_id, service_id, action)
        label = {
            "tasks": "⬅️ К задачам",
            "services": "⬅️ К служениям",
            "finish": "✅ Завершить привязку",
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
            await _send_dialog(message, "Выбор задач устарел. Запустите команду заново.",
                parse_mode=None,
            )
            return

        tasks = pending.get("schedule_tasks", [])
        if operation == "service_contact":
            tasks = [
                task for task in tasks
                if not task_is_informational(
                    pending.get("schedule_assignments", {}).get(task, [])
                )
            ]
        page_size = 3
        page_count = max(1, (len(tasks) + page_size - 1) // page_size)
        page = min(max(page, 0), page_count - 1)
        page_tasks = tasks[page * page_size:(page + 1) * page_size]
        registry = service_registry()
        service = (
            service_by_id(registry, service_id) if registry is not None else None
        )
        service_context = (
            service_label(service) if service is not None else service_id.upper()
        )
        rows = []
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
                    actor_id, service_id, "services"
                )
            ])
        else:
            for task in page_tasks:
                token = secrets.token_urlsafe(8)
                pending_notify_task_choices[token] = (
                    actor_id, service_id, task
                )
                label = task if len(task) <= 48 else task[:45] + "..."
                if task_is_informational(
                    pending.get("schedule_assignments", {}).get(task, [])
                ):
                    label = f"ℹ️ {label} · инфо"
                rows.append([
                    InlineKeyboardButton(
                        text=(
                            label
                            if label.startswith("ℹ️ ")
                            else f"{task_emoji(task)} {label}"
                        ),
                        callback_data=f"notify_task:{token}",
                    )
                ])
            token = secrets.token_urlsafe(8)
            pending_notify_task_choices[token] = (actor_id, service_id, None)
            rows.append([
                InlineKeyboardButton(
                    text="⬅️ К настройкам",
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
                    text="⬅️ Назад", callback_data=f"task_page:{token}"
                )
            )
        if page + 1 < page_count:
            token = secrets.token_urlsafe(8)
            pending_task_pages[token] = (
                actor_id, operation, service_id, page + 1
            )
            navigation.append(
                InlineKeyboardButton(
                    text="➡️ Далее", callback_data=f"task_page:{token}"
                )
            )
        if navigation:
            rows.append(navigation)

        if operation == "service_contact":
            prompt = (
                f"🔗 Привязка ответственных · "
                f"{service_context}"
                f"\n📊 Задачи {page * page_size + 1}-"
                f"{min((page + 1) * page_size, len(tasks))} из {len(tasks)}"
                "\nℹ️ Выберите задачу из графика."
                if tasks
                else "🔗 Привязка ответственных\n"
                "⚪ В графике нет задач с ответственными."
            )
        else:
            prompt = (
                "📝 Личный шаблон\n"
                f"📊 Задачи {page * page_size + 1}-"
                f"{min((page + 1) * page_size, len(tasks))} из {len(tasks)}"
                "\nℹ️ Выберите задачу из графика."
                if tasks
                else "📝 Личный шаблон\n"
                "⚪ В графике нет задач с ответственными."
            )
        await _send_dialog(message, prompt,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def offer_contact_name_choices(
        message: Message,
        actor_id: int,
        service_id: str,
        task: str,
        notice: str | None = None,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or task not in pending.get("schedule_assignments", {})
        ):
            await _send_dialog(message, "Задача отсутствует в актуальном файле расписания.",
                parse_mode=None,
            )
            return

        if task_is_informational(
            pending["schedule_assignments"].get(task, [])
        ):
            debug.debug(
                "Отклонён выбор информационной записи для привязки: %s",
                task,
            )
            await _send_dialog(
                message,
                "Эта запись — не задача, а пометка о дне служения.",
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
        bound_count = 0
        names = task_assignment_names(
            pending["schedule_assignments"][task]
        )
        for person_name in names:
            contact = contacts_by_name.get(person_name)
            if contact:
                bound_count += 1
                status = "✅"
                suffix = (
                    f" · @{contact['username']}"
                    if contact["username"]
                    else " · привязан"
                )
            else:
                status = "⚪"
                suffix = " · не привязан"
            token = secrets.token_urlsafe(8)
            pending_contact_person_choices[token] = (
                actor_id, service_id, task, person_name
            )
            label = f"{status} {person_name}{suffix}"
            if len(label) > 60:
                label = label[:57] + "..."
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"contact_person:{token}",
                )
            ])
        rows.append([
            contact_navigation_button(actor_id, service_id, "tasks")
        ])
        text = (
            f"👥 Ответственные · {task_emoji(task)} {task}\n"
            f"📊 Привязано {bound_count} из {len(names)} участников\n"
            "ℹ️ Выберите имя для привязки или изменения."
        )
        if notice:
            text = notice + "\n\n" + text
        await _send_dialog(message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def offer_contact_task_choices(
        message: Message,
        actor_id: int,
        service_id: str,
    ) -> bool:
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен.", parse_mode=None
            )
            return False
        task_loader = getattr(context, "load_service_task_assignments", None)
        if not callable(task_loader):
            await _send_dialog(message, "Загрузка задач из файла расписания недоступна.",
                parse_mode=None,
            )
            return False
        current_pending = pending_inputs.get(actor_id)
        if (
            current_pending is None
            or current_pending.get("operation") != "service_contact"
            or current_pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Сценарий выбора служения отменён или устарел.",
                parse_mode=None,
            )
            return False
        session_id = current_pending.get("session_id")
        try:
            assignments = await task_loader(service_id)
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return False
        except Exception:
            debug.exception("Не удалось прочитать задачи из графика служений")
            await _send_dialog(message, "Не удалось прочитать файл графика служений. "
                "Проверьте доступность расписания и попробуйте снова.",
                parse_mode=None,
            )
            return False
        try:
            contacts = registry.list_responsible_contacts(actor_id, service_id)
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return False
        except sqlite3.Error:
            debug.exception("Не удалось прочитать привязки ответственных")
            await _send_dialog(message, "Не удалось прочитать список привязанных пользователей.",
                parse_mode=None,
            )
            return False
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await _send_dialog(message, "Сценарий выбора служения был отменён во время загрузки графика.",
                parse_mode=None,
            )
            return False
        pending["schedule_assignments"] = assignments
        pending["schedule_tasks"] = sorted(
            task
            for task, task_info in assignments.items()
            if not task_is_informational(task_info)
        )
        informational_tasks = sorted(
            task
            for task, task_info in assignments.items()
            if task_is_informational(task_info)
        )
        if informational_tasks:
            debug.debug(
                "Информационные записи исключены из привязок для %s: %s",
                service_id,
                informational_tasks,
            )
        pending["registered_contacts"] = {}
        for contact in contacts:
            pending["registered_contacts"].setdefault(
                contact["task"], []
            ).append(contact)
        await send_task_choices_page(
            message, actor_id, service_id, "service_contact"
        )
        return True

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
            await _send_dialog(message, "Пошаговая настройка уведомлений завершена или недоступна.",
                parse_mode=None,
            )
            return False
        if "schedule_assignments" in pending:
            assignments = pending["schedule_assignments"]
        else:
            task_loader = getattr(context, "load_service_task_assignments", None)
            if not callable(task_loader):
                await _send_dialog(message, "Загрузка задач из файла расписания недоступна.",
                    parse_mode=None,
                )
                return False
            try:
                assignments = await task_loader(service_id)
            except Exception:
                debug.exception("Не удалось прочитать задачи из графика служений")
                await _send_dialog(message, "Не удалось прочитать файл графика служений. "
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
            await _send_dialog(message, "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        service = service_by_id(service_registry(), service_id)
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        pending["step"] = "menu"
        reminders = service_registry().list_group_reminders(service_id)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            f"⏰ Напоминания "
                            f"({len(reminders)})"
                        ),
                        callback_data=(
                            f"group_reminder:list:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📝 Шаблон по умолчанию",
                        callback_data=(
                            f"group_reminder:default:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=(
                            "📝 Личные шаблоны "
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
                        text="✅ Завершить и сохранить",
                        callback_data=(
                            f"notify_finish:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✖️ Закрыть настройку",
                        callback_data=(
                            f"notify_cancel:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        await _send_dialog(message, f"🔔 {service_label(service)}\n"
            "ℹ️ Выберите, что изменить. Текущие значения сохраняются, "
            "пока вы их не измените.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def show_reminder_rules_menu(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        pending["step"] = "reminder_menu"
        registry = service_registry()
        if registry is None:
            await _send_dialog(
                message, "Реестр настроек служений недоступен.",
                parse_mode=None,
            )
            return
        rows = []
        for reminder in registry.list_group_reminders(service_id):
            label = (
                f"{reminder['reminder_days']} дн. · "
                f"{reminder['reminder_time']} · "
                f"{'свой текст' if reminder['body'] else 'по умолчанию'}"
            )
            rows.append([
                InlineKeyboardButton(
                    text=f"📝 {label}",
                    callback_data=(
                        f"group_reminder:open:{service_id}:"
                        f"{reminder['id']}:{pending['session_id']}"
                    ),
                ),
            ])
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Добавить напоминание",
                        callback_data=(
                            f"group_reminder:add:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К настройкам",
                        callback_data=(
                            f"group_reminder:root_back:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        service = service_by_id(registry, service_id)
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        await _send_dialog(message, f"⏰ {service_label(service)} · напоминания\n"
            "У каждого напоминания своё расписание и текст.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def enter_group_reminder_card(
        message: Message,
        actor_id: int,
        service_id: str,
        reminder_id: int,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or registry is None
        ):
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        reminder = registry.get_group_reminder(reminder_id)
        service = registry.get_service(service_id)
        if reminder is None or reminder["service_id"] != service_id:
            await _send_dialog(
                message, "Групповое напоминание больше не существует.",
                parse_mode=None,
            )
            return
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        pending["step"] = "group_reminder_card"
        body_status = (
            "собственный" if reminder["body"] else
            "используется шаблон по умолчанию"
        )
        current_text = reminder["body"] or registry.resolve_group_text(
            reminder, service
        )
        text = (
            f"📝 Напоминание · {service_label(service)}\n\n"
            f"📅 Срок: за {reminder['reminder_days']} дн.\n"
            f"🕒 Время: {reminder['reminder_time']}\n"
            f"📝 Текст: {body_status}\n"
            f"🔔 Статус: {'включено' if reminder['enabled'] else 'выключено'}"
            f"\n\nТекущий текст напоминания:\n{current_text}"
        )
        buttons = [
            [
                InlineKeyboardButton(
                    text=(
                        "✏️ Изменить текст"
                        if reminder["body"]
                        else "✏️ Задать свой текст"
                    ),
                    callback_data=(
                        f"group_reminder:edit_text:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Изменить срок",
                    callback_data=(
                        f"group_reminder:edit_days:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🕒 Изменить время",
                    callback_data=(
                        f"group_reminder:edit_time:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Выключить" if reminder["enabled"] else "🔄 Включить",
                    callback_data=(
                        f"group_reminder:toggle:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=(
                        f"group_reminder:delete_prompt:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
            ],
        ]
        if reminder["body"]:
            buttons.insert(
                1,
                [
                    InlineKeyboardButton(
                        text="🔄 Вернуть шаблон по умолчанию",
                        callback_data=(
                            f"group_reminder:reset_text:{service_id}:"
                            f"{reminder_id}:{pending['session_id']}"
                        ),
                    )
                ],
            )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⬅️ К напоминаниям",
                    callback_data=(
                        f"group_reminder:back:{service_id}:"
                        f"{pending['session_id']}"
                    ),
                )
            ]
        )
        await _send_dialog(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=None,
        )

    async def enter_default_template(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or service is None
        ):
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        pending["step"] = "default_template"
        group_template = pending.get(
            "group_template", service["group_template"]
        )
        current_template = (
            group_template or "Используется встроенный шаблон."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить",
                        callback_data=(
                            f"group_reminder:edit_default:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🔄 Вернуть встроенный",
                        callback_data=(
                            f"group_reminder:reset_default:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К настройкам",
                        callback_data=(
                            f"group_reminder:root_back:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        await _send_dialog(
            message,
            f"📝 {service_label(service)} · шаблон по умолчанию\n\n"
            "ℹ️ Используется напоминаниями, у которых не задан свой текст.\n\n"
            f"Текущий текст:\n{current_template}",
            reply_markup=keyboard,
            parse_mode=None,
        )

    def notification_edit_controls(
        service_id: str, session_id: str
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="⬅️ Назад без изменения",
                    callback_data=f"notify_back:{service_id}:{session_id}",
                )
            ]]
        )

    @admin_router.callback_query(F.data.startswith("group_reminder:"))
    async def on_group_reminder_action(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) not in {4, 5}:
            await _answer_callback(callback, "⚠️ Настройка устарела.")
            return
        _, action, service_id, *tail = parts
        reminder_id = None
        if len(tail) == 2:
            try:
                reminder_id = int(tail[0])
            except ValueError:
                await _answer_callback(callback, "⚠️ Некорректный идентификатор.")
                return
            session_id = tail[1]
        else:
            session_id = tail[0]
        actor_id = callback.from_user.id
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or registry is None
        ):
            await _answer_callback(
                callback, "⚠️ Настройка устарела. Запустите /service_notify заново."
            )
            return
        try:
            if not is_service_admin(actor_id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
        except sqlite3.Error:
            debug.exception("Не удалось проверить права на групповые напоминания")
            await _answer_callback(callback, "❌ Не удалось проверить права.")
            return

        async def refresh_jobs() -> None:
            refresh = getattr(context, "refresh_service_reminder_jobs", None)
            if callable(refresh):
                await refresh()

        try:
            if action == "default":
                if pending.get("step") != "menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await enter_default_template(callback.message, actor_id, service_id)
            elif action == "root_back":
                if pending.get("step") not in {
                    "default_template",
                    "reminder_menu",
                }:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_notification_settings_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "list":
                if pending.get("step") != "menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "edit_default":
                if pending.get("step") != "default_template":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "group_default_text"
                await _send_dialog(
                    callback.message,
                    "Введите шаблон по умолчанию. Доступные поля: "
                    "{service_name}, {sunday_date}, {task_assignments}.",
                    reply_markup=notification_edit_controls(
                        service_id, session_id
                    ),
                    parse_mode=None,
                )
            elif action == "reset_default":
                if pending.get("step") != "default_template":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["group_template"] = None
                await enter_default_template(
                    callback.message, actor_id, service_id
                )
            elif action == "add":
                if pending.get("step") != "reminder_menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                if any(
                    item["reminder_days"] == 1
                    and item["reminder_time"] == "18:00"
                    for item in registry.list_group_reminders(service_id)
                ):
                    pending["step"] = "group_reminder_duplicate_add"
                    confirm = InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text="✅ Создать повторное",
                                callback_data=(
                                    f"group_reminder:confirm_add:{service_id}:"
                                    f"{session_id}"
                                ),
                            ),
                            InlineKeyboardButton(
                                text="⬅️ Отмена",
                                callback_data=(
                                    f"group_reminder:cancel_add:{service_id}:"
                                    f"{session_id}"
                                ),
                            ),
                        ]]
                    )
                    await _send_dialog(
                        callback.message,
                        "Уже есть напоминание за 1 день в 18:00. "
                        "Создать ещё одно? Оно будет отправляться отдельным "
                        "сообщением со своим текстом.",
                        reply_markup=confirm,
                        parse_mode=None,
                    )
                else:
                    reminder_id = registry.add_group_reminder(
                        actor_id, service_id, 1, "18:00"
                    )
                    await refresh_jobs()
                    await enter_group_reminder_card(
                        callback.message, actor_id, service_id, reminder_id
                    )
            elif action == "confirm_add":
                if pending.get("step") != "group_reminder_duplicate_add":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                reminder_id = registry.add_group_reminder(
                    actor_id, service_id, 1, "18:00", allow_duplicate=True
                )
                await refresh_jobs()
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action == "cancel_add":
                if pending.get("step") != "group_reminder_duplicate_add":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "back":
                if pending.get("step") not in {
                    "group_reminder_card",
                    "group_reminder_delete",
                }:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                if pending["step"] == "group_reminder_delete":
                    await enter_group_reminder_card(
                        callback.message, actor_id, service_id, reminder_id
                    )
                else:
                    await show_reminder_rules_menu(
                        callback.message, actor_id, service_id
                    )
            elif action == "open":
                if pending.get("step") != "reminder_menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action in {"edit_days", "edit_time", "edit_text"}:
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                reminder = registry.get_group_reminder(reminder_id)
                if reminder is None or reminder["service_id"] != service_id:
                    raise ValueError("Групповое напоминание больше не существует.")
                field = action.removeprefix("edit_")
                pending["group_reminder_input"] = {
                    "reminder_id": reminder_id,
                    "field": field,
                }
                pending["step"] = f"group_reminder_{field}"
                prompts = {
                    "days": f"Введите срок до события от 0 до 6. Сейчас: "
                    f"{reminder['reminder_days']}.",
                    "time": f"Введите локальное время HH:MM. Сейчас: "
                    f"{reminder['reminder_time']}.",
                    "text": "Введите собственный текст напоминания. "
                    "Можно использовать {service_name}, {sunday_date}, "
                    "{task_assignments}.",
                }
                await _send_dialog(
                    callback.message,
                    prompts[field],
                    reply_markup=notification_edit_controls(
                        service_id, session_id
                    ),
                    parse_mode=None,
                )
            elif action in {"toggle", "reset_text"}:
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                reminder = registry.get_group_reminder(reminder_id)
                if reminder is None or reminder["service_id"] != service_id:
                    raise ValueError("Групповое напоминание больше не существует.")
                if action == "toggle":
                    registry.update_group_reminder(
                        actor_id, reminder_id,
                        enabled=not reminder["enabled"],
                    )
                    await refresh_jobs()
                else:
                    registry.update_group_reminder(
                        actor_id, reminder_id, body=None
                    )
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action == "delete_prompt":
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "group_reminder_delete"
                confirm = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🗑 Да, удалить",
                            callback_data=(
                                f"group_reminder:delete:{service_id}:"
                                f"{reminder_id}:{session_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text="⬅️ Отмена",
                            callback_data=(
                                f"group_reminder:back:{service_id}:"
                                f"{reminder_id}:{session_id}"
                            ),
                        ),
                    ]]
                )
                await _send_dialog(
                    callback.message,
                    "Удалить это групповое напоминание?",
                    reply_markup=confirm,
                    parse_mode=None,
                )
            elif action == "delete":
                if pending.get("step") != "group_reminder_delete":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                registry.delete_group_reminder(actor_id, reminder_id)
                await refresh_jobs()
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "confirm_update":
                if pending.get("step") != "group_reminder_duplicate_update":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                change = pending.pop("group_reminder_change")
                registry.update_group_reminder(
                    actor_id, change["reminder_id"],
                    days=change["days"],
                    time=change["time"],
                    allow_duplicate=True,
                )
                pending.pop("group_reminder_input", None)
                await refresh_jobs()
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id,
                    change["reminder_id"],
                )
            elif action == "cancel_input":
                input_info = pending.pop("group_reminder_input", None)
                pending.pop("group_reminder_change", None)
                if input_info is None:
                    await _answer_callback(callback, "⚠️ Действие устарело.")
                    return
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id,
                    input_info["reminder_id"],
                )
            elif action == "cancel_delete":
                if pending.get("step") != "group_reminder_delete":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            else:
                await _answer_callback(callback, "⚠️ Неизвестное действие.")
                return
        except (PermissionError, ValueError) as exc:
            await _answer_callback(callback, _status_text(str(exc), "⚠️"))
            return
        except sqlite3.Error:
            debug.exception("Ошибка управления групповым напоминанием")
            await _notify_admin(
                context.bot,
                context.admin_id,
                "Не удалось сохранить групповое напоминание из-за ошибки базы.",
                debug,
            )
            await _answer_callback(callback, "❌ Ошибка базы данных.")
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось обновить задания групповых напоминаний")
            await _notify_admin(
                context.bot,
                context.admin_id,
                "Не удалось обновить задания групповых напоминаний.",
                debug,
            )
            await _answer_callback(callback, "❌ Не удалось обновить расписание.")
            return
        await _answer_callback(callback)

    def service_result_keyboard(service_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚙️ К настройкам служения",
                        callback_data=f"service:service_settings:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 К списку служений",
                        callback_data="service:back_to_services:list",
                    )
                ],
            ]
        )

    def test_result_keyboard(service_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🧪 Ещё одна проверка",
                        callback_data=f"service:test_notify:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 К списку служений",
                        callback_data="service:back_to_services:list",
                    )
                ],
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
                f"Пример текста для ввода:\n"
                f"{previews['example_group_template']}\n\n"
                f"Так будет выглядеть сообщение на "
                f"{previews['sunday_date']:%d.%m.%Y}:\n"
                f"{previews['example_group_text']}\n\n"
                f"Результат текущего шаблона на "
                f"{previews['sunday_date']:%d.%m.%Y}:\n"
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
            f"{item['name']}:\n{item['text']}" for item in examples
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
            await _send_dialog(message, "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        if field == "reminders":
            pending["step"] = "menu"
            await show_reminder_rules_menu(message, actor_id, service_id)
            return
        if field == "personal":
            if "schedule_assignments" not in pending:
                task_loader = getattr(
                    context, "load_service_task_assignments", None
                )
                if not callable(task_loader):
                    await _send_dialog(message, "Загрузка задач из файла расписания недоступна.",
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
                    await _send_dialog(message, "Не удалось прочитать файл графика. "
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
                "Введите новый текст для группы. Доступные поля:\n"
                "• {service_name} — название служения\n"
                "• {sunday_date} — дата воскресенья\n"
                "• {task_assignments} — список задач и ответственных.\n"
                "Пустое значение не меняет текущий текст."
            ),
        }
        if field not in prompts:
            await _send_dialog(message, "Неизвестный параметр.", parse_mode=None)
            return
        pending["step"] = field
        text = prompts[field]
        if field == "group_template":
            registry = service_registry()
            service = service_by_id(registry, service_id) if registry else None
            current_template = service["group_template"] if service else None
            if current_template:
                try:
                    validate_notification_template(
                        current_template,
                        GROUP_TEMPLATE_FIELDS,
                        "Шаблон группового уведомления",
                    )
                except ValueError:
                    text += (
                        "\n\n⚠️ В сохранённом шаблоне есть неподдерживаемые "
                        "поля (например, {tg_responsible}). Они не подставляются. "
                        "Замените их на {task_assignments}."
                    )
            text += "\n\n" + await notification_template_preview(
                actor_id, service_id
            )
        chunks = _telegram_text_chunks(text)
        for index, chunk in enumerate(chunks):
            await _send_dialog(message, chunk,
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
                        text=                        "📡 Выбрать или изменить групповой чат",
                        callback_data=f"service:service_chat_set:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔴 Отключить служение",
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
            await _send_dialog(message, "Запрос настройки устарел. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        pending["active_task"] = task
        pending["step"] = "notify_template"
        is_informational = task_is_informational(
            pending.get("schedule_assignments", {}).get(task, [])
        )
        display_task = (
            f"ℹ️ {task} · инфо" if is_informational else task
        )
        text = (
            f"Введите индивидуальный текст напоминания для задачи "
            f"«{display_task}». Шаблоны: {{name}}, {{task}}, {{service_name}}, "
            f"{{sunday_date}}, {{telegram_id}}, {{username}}.\n\n"
            + await notification_template_preview(
                actor_id, service_id, task
            )
        )
        chunks = _telegram_text_chunks(text)
        for index, chunk in enumerate(chunks):
            await _send_dialog(message, chunk,
                reply_markup=(
                    notification_edit_controls(
                        service_id, pending["session_id"]
                    )
                    if index == len(chunks) - 1
                    else None
                ),
                parse_mode=None,
            )

    async def save_notification_setup(
        message: Message, actor_id: int
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or registry is None
        ):
            await _send_dialog(message, "Пошаговая настройка уведомлений устарела.",
                parse_mode=None,
            )
            return
        service = service_by_id(registry, pending["service_id"])
        if service is None:
            await _send_dialog(message, "Служение больше не существует.", parse_mode=None)
            return
        try:
            registry.set_service_default_template(
                actor_id,
                pending["service_id"],
                pending["group_template"],
            )
            registry.set_service_personal_templates(
                actor_id,
                pending["service_id"],
                pending["personal_templates"],
            )
            refresh_reminders = getattr(
                context, "refresh_service_reminder_jobs", None
            )
            if callable(refresh_reminders):
                await refresh_reminders()
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить настройки уведомлений")
            await _send_dialog(message, "Не удалось сохранить настройки уведомлений из-за ошибки базы.",
                parse_mode=None,
            )
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось обновить задания уведомлений")
            await _send_dialog(
                message,
                "Не удалось обновить расписание уведомлений. Попробуйте позже.",
                parse_mode=None,
            )
            return
        pending_inputs.pop(actor_id, None)
        await clean_previous_menu_by_id(
            context.bot,
            pending.get("menu_chat_id"),
            pending.get("menu_message_id"),
            debug,
        )
        await _send_dialog(
            message,
            f"✅ Настройки уведомлений служения "
            f"«{service_label(service)}» "
            "сохранены.",
            reply_markup=service_result_keyboard(pending["service_id"]),
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("task:"))
    async def on_task_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await _answer_callback(callback,"❌ Некорректный выбор задачи.")
            return
        _, operation, token = parts
        selection = pending_task_choices.pop(token, None)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or selection[1] != operation
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните команду заново.")
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
            await _answer_callback(callback,"⚠️ Запрос задачи устарел.")
            return
        await offer_contact_name_choices(
            callback.message, callback.from_user.id, service_id, task
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_task:"))
    async def on_notify_task_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
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
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните настройку заново.")
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
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_task_new:"))
    async def on_notify_new_task(callback: CallbackQuery) -> None:
        await _answer_callback(callback,
            "Добавление произвольных задач отключено. "
            "Используйте задачи из файла расписания."
        )

    async def offer_user_selection(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.setdefault(actor_id, {})
        pending.update(
            {
                "operation": "service_admin_add",
                "service_id": service_id,
                "step": "await_user",
            }
        )
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
        await _send_dialog(message, "Нажмите кнопку и выберите пользователя в Telegram. "
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
            await _send_dialog(message, "Сценарий привязки устарел. Запустите /service_contact заново.",
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
        await _send_dialog(message, f"Нажмите кнопку и выберите пользователя Telegram для имени "
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
        if operation == "service_admin_add":
            pending = pending_inputs.setdefault(actor_id, {})
            pending.update(
                {
                    "operation": operation,
                    "service_id": service_id,
                    "step": "confirm",
                }
            )
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
                        text="✅ Подтвердить",
                        callback_data=(
                            f"{callback_prefix}_confirm:{service_id}:"
                            f"{user_id}:{confirmation_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="✖️ Отмена",
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
        await _send_dialog(message, f"ℹ️ Подтвердите: {title}\n"
            f"👤 Пользователь · {user_display}"
            + (f" · @{username}" if username else "")
            + (
                f"\n🆔 Telegram ID · {ui_code(user_id)}"
                if display_name is None
                else ""
            ),
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
            await _send_dialog(message, "Реестр настроек служений недоступен. "
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
            service_order = (
                "assembly", "broadcast", "slides", "order",
                "kitchen", "coffee", "sound", "rehearsal",
            )
            service_rank = {
                service_id: index
                for index, service_id in enumerate(service_order)
            }
            visible.sort(
                key=lambda service: service_rank.get(
                    service["id"], len(service_rank)
                )
            )
            if not visible:
                await _send_dialog(message, "ℹ️ Вы не назначены администратором служений.",
                    parse_mode=None,
                )
                return
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=(
                                f"{'🟢' if service['enabled'] else '🔴'} "
                                f"{service_label(service)}"
                            ),
                            callback_data=(
                                f"service:service_settings:{service['id']}"
                            ),
                        )
                    ]
                    for service in visible
                ]
            )
        except sqlite3.Error:
            debug.exception("Не удалось прочитать список служений")
            await _send_dialog(message, "Не удалось прочитать реестр служений.", parse_mode=None
            )
            return
        await _send_dialog(
            message,
            "📋 Служения\nℹ️ Выберите служение для просмотра настроек.",
            reply_markup=keyboard,
            _dialog_keyboard_limit=8,
        )

    @admin_router.message(Command("service_settings"))
    async def on_service_settings(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
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
        await clean_and_clear_pending_actor(message.from_user.id)
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
            await _send_dialog(message, "Укажите ID пользователя и необязательный username.",
                parse_mode=None,
            )
            return
        service_id = parts[1]
        try:
            user_id, username = parse_manual_user(" ".join(parts[2:]))
            registry = service_registry()
            if registry is None:
                raise ValueError("Реестр настроек служений недоступен.")
            if not registry.is_super_admin(message.from_user.id):
                raise PermissionError(
                    "Эта команда доступна только супер-администратору."
                )
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось проверить прямое назначение администратора")
            await _send_dialog(message, "Не удалось проверить служение и права из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        await ask_user_assignment_confirmation(
            message,
            message.from_user.id,
            service_id,
            "service_admin_add",
            user_id,
            username,
        )

    @admin_router.message(Command("service_admin_remove"))
    async def on_service_admin_remove(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
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
            await _send_dialog(message, "Лишние параметры. Запустите команду ещё раз.", parse_mode=None)
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
        await clean_and_clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_chat")
            return
        if len(parts) == 2:
            if parts[1] == "off":
                await _send_dialog(message, "Выберите служение в команде /service_chat и нажмите "
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
            await _send_dialog(message, "Используйте /service_chat, чтобы выбрать служение и "
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
        await clean_and_clear_pending_actor(message.from_user.id)
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
            await _send_dialog(message, "Ручной ввод задач и ответственных отключён. "
                "Укажите только служение, например /service_contact sound; "
                "задачу и имя нужно выбрать из файла расписания.",
                parse_mode=None,
            )
            return

    @admin_router.message(Command("service_notify"))
    async def on_service_notify(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await invalidate_personal_session(message.from_user.id)
        payload = (message.text or "").partition(" ")[2]
        fields = [field.strip() for field in payload.split("|", 2)]
        if not payload.strip():
            await request_service_input(message, "service_notify")
            return
        if len(fields) == 1:
            service_parts = fields[0].split()
            if len(service_parts) != 1:
                await _send_dialog(message, "ℹ️ Настройки вводятся пошагово; индивидуальные задачи "
                    "выбираются из файла расписания. Используйте "
                    "/service_notify <service_id>.",
                    parse_mode=None,
                )
                return
            await request_service_input(
                message, "service_notify", service_parts[0]
            )
            return
        await _send_dialog(message, "ℹ️ Настройки вводятся пошагово; индивидуальные задачи "
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
        previous = pending_inputs.get(message.from_user.id)
        had_request = previous is not None
        if previous is not None:
            await clean_and_clear_pending_actor(message.from_user.id)
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
            await _send_dialog(message, "ℹ️ Нет активного запроса параметров.", parse_mode=None
            )
            return
        await _send_dialog(message, "✅ Текущий запрос отменён.",
            parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )

    @admin_router.message(Command("start"))
    async def on_start(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        await _send_dialog(message, "👋 Здравствуйте! Для управления служениями используйте команды "
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
            await _answer_callback(callback,)
            return
        callback_parts = callback.data.split(":", 2)
        if len(callback_parts) != 3:
            await _answer_callback(callback,"❌ Некорректный выбор служения.")
            return
        _, operation, service_id = callback_parts
        if callback.from_user.id in pending_inputs:
            await clean_and_clear_pending_actor(callback.from_user.id)
        else:
            await clean_previous_menu(callback.message, debug)
        if operation == "back_to_services":
            registry = service_registry()
            if registry is None:
                await _send_dialog(callback.message, "Реестр настроек служений недоступен.",
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
                                    text=(
                                        f"{'🟢' if service['enabled'] else '🔴'} "
                                        f"{service_label(service)}"
                                    ),
                                    callback_data=(
                                        "service:service_settings:"
                                        f"{service['id']}"
                                    ),
                                )
                            ]
                            for service in services
                        ]
                    )
                    await _send_dialog(callback.message, "📋 Служения\nℹ️ Выберите служение для просмотра настроек.",
                        reply_markup=keyboard,
                        parse_mode=None,
                    )
                except sqlite3.Error:
                    debug.exception(
                        "Не удалось загрузить список служений для возврата"
                    )
                    await _send_dialog(callback.message, "Не удалось прочитать реестр служений.",
                        parse_mode=None,
                    )
            await _answer_callback(callback,)
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
            await _answer_callback(callback,"❌ Неизвестное действие.")
            return
        registry = service_registry()
        if registry is None:
            await _send_dialog(callback.message, "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            await _answer_callback(callback,)
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
                    await _send_dialog(callback.message, chunk,
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
                await _send_dialog(callback.message, f"{service_label(service)}: выберите действие.",
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
                await _send_dialog(
                    callback.message,
                    f"{service_label(service)} отключено. "
                    "Сохранённый чат не изменён.",
                    reply_markup=service_result_keyboard(service_id),
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
                await _send_dialog(callback.message, f"{service_label(service)}: выберите проверку.",
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
            await _send_dialog(callback.message, _status_text(str(exc), "❌"), parse_mode=None
            )
        except sqlite3.Error:
            debug.exception("Не удалось обработать выбор служения")
            await _send_dialog(callback.message, "Не удалось прочитать реестр служений.", parse_mode=None
            )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_rule:"))
    async def on_notification_rule_action(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":")
        if len(parts) not in {4, 5}:
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        _, action, service_id, session_id, *raw_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        step = pending.get("step")
        allowed_steps = {
            "root_back": {"reminder_menu"},
            "back": {"reminder_options", "reminder_delete"},
            "add": {"reminder_menu"},
            "edit": {"reminder_menu"},
            "days": {"reminder_options"},
            "time": {"reminder_options"},
            "delete_prompt": {"reminder_menu"},
            "delete": {"reminder_delete"},
        }
        if step not in allowed_steps.get(action, set()):
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        try:
            if not is_service_admin(callback.from_user.id, service_id):
                await _answer_callback(callback,"❌ Недостаточно прав.")
                return
        except sqlite3.Error:
            debug.exception("Не удалось проверить права на расписание уведомлений")
            await _answer_callback(callback,"❌ Не удалось проверить права.")
            return

        reminder_id = None
        if raw_id:
            try:
                reminder_id = int(raw_id[0])
            except ValueError:
                await _answer_callback(callback,"❌ Некорректный идентификатор.")
                return
        reminder = next(
            (
                item for item in pending["reminders"]
                if item.get("id") == reminder_id
                or item.get("draft_id") == reminder_id
            ),
            None,
        )
        if action == "root_back":
            await show_notification_settings_menu(
                callback.message, callback.from_user.id, service_id
            )
        elif action == "back":
            await show_reminder_rules_menu(
                callback.message, callback.from_user.id, service_id
            )
        elif action == "add":
            if any(
                item.get("id") is None and not item.get("complete")
                for item in pending["reminders"]
            ):
                await _answer_callback(callback,
                    "Сначала завершите настройку нового уведомления."
                )
                return
            pending["reminders"].append(
                {
                    "id": None,
                    "draft_id": min(
                        (
                            item["draft_id"]
                            for item in pending["reminders"]
                            if item.get("id") is None
                        ),
                        default=0,
                    ) - 1,
                    "reminder_days": 1,
                    "reminder_time": "18:00",
                    "complete": False,
                }
            )
            pending["active_reminder_id"] = pending["reminders"][-1][
                "draft_id"
            ]
            pending["reminder_operation"] = "add"
            pending["step"] = "reminder_days"
            await _send_dialog(callback.message, "Введите число дней до события для нового уведомления "
                "(от 0 до 6).",
                reply_markup=notification_edit_controls(
                    service_id, session_id
                ),
                parse_mode=None,
            )
        elif action == "edit" and reminder is not None:
            pending["step"] = "reminder_options"
            buttons = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"📅 Дни: {reminder['reminder_days']}",
                            callback_data=(
                                f"notify_rule:days:{service_id}:"
                                f"{session_id}:{reminder_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text=f"🕒 Время: {reminder['reminder_time']}",
                            callback_data=(
                                f"notify_rule:time:{service_id}:"
                                f"{session_id}:{reminder_id}"
                            ),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ К списку",
                            callback_data=(
                                f"notify_rule:back:{service_id}:{session_id}"
                            ),
                        )
                    ],
                ]
            )
            await _send_dialog(callback.message, "Выберите параметр уведомления для изменения.",
                reply_markup=buttons,
                parse_mode=None,
            )
        elif action in {"days", "time"} and reminder is not None:
            pending["active_reminder_id"] = reminder_id
            pending["reminder_operation"] = "edit"
            pending["active_reminder_original"] = {
                "reminder_days": reminder["reminder_days"],
                "reminder_time": reminder["reminder_time"],
            }
            pending["step"] = f"reminder_{action}"
            prompt = (
                f"Введите число дней до события от 0 до 6. "
                f"Текущее значение: {reminder['reminder_days']}."
                if action == "days"
                else "Введите время в формате HH:MM, например 09:00. "
                f"Текущее значение: {reminder['reminder_time']}."
            )
            await _send_dialog(callback.message, prompt,
                reply_markup=notification_edit_controls(
                    service_id, session_id
                ),
                parse_mode=None,
            )
        elif action == "delete_prompt" and reminder is not None:
            pending["step"] = "reminder_delete"
            buttons = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Да, удалить",
                            callback_data=(
                                f"notify_rule:delete:{service_id}:"
                                f"{session_id}:{reminder_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text="Отмена",
                            callback_data=(
                                f"notify_rule:back:{service_id}:{session_id}"
                            ),
                        ),
                    ]
                ]
            )
            await _send_dialog(callback.message, f"Удалить уведомление за {reminder['reminder_days']} дн. "
                f"в {reminder['reminder_time']}?",
                reply_markup=buttons,
                parse_mode=None,
            )
        elif action == "delete" and reminder is not None:
            pending["reminders"].remove(reminder)
            pending["step"] = "menu"
            await show_reminder_rules_menu(
                callback.message, callback.from_user.id, service_id
            )
        else:
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_back:"))
    async def on_notification_edit_back(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback, "⚠️ Настройка устарела.")
            return
        _, service_id, session_id = parts
        actor_id = callback.from_user.id
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await _answer_callback(callback, "⚠️ Настройка устарела.")
            return
        try:
            if not is_service_admin(actor_id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
        except sqlite3.Error:
            debug.exception("Не удалось проверить доступ к настройкам")
            await _answer_callback(callback, "❌ Не удалось проверить права.")
            return

        step = pending.get("step")
        if step in {
            "group_reminder_days",
            "group_reminder_time",
            "group_reminder_text",
        }:
            input_info = pending.get("group_reminder_input")
            if input_info is None:
                await _answer_callback(callback, "⚠️ Нет активного шага для отмены.")
                return
            pending.pop("group_reminder_input", None)
            pending.pop("group_reminder_change", None)
            await enter_group_reminder_card(
                callback.message,
                actor_id,
                service_id,
                input_info["reminder_id"],
            )
        elif step == "group_default_text":
            pending["step"] = "default_template"
            await enter_default_template(
                callback.message, actor_id, service_id
            )
        elif step in {"reminder_days", "reminder_time"}:
            active_id = pending.get("active_reminder_id")
            reminder = next(
                (
                    item for item in pending["reminders"]
                    if item.get("id") == active_id
                    or item.get("draft_id") == active_id
                ),
                None,
            )
            if pending.get("reminder_operation") == "add":
                if reminder is not None:
                    pending["reminders"].remove(reminder)
            elif reminder is not None:
                original = pending.get("active_reminder_original", {})
                reminder.update(original)
            pending.pop("active_reminder_original", None)
            pending.pop("active_reminder_id", None)
            pending.pop("reminder_operation", None)
            pending["step"] = "menu"
            await show_reminder_rules_menu(callback.message, actor_id, service_id)
        elif step == "notify_template":
            pending.pop("active_task", None)
            pending["step"] = "choose_notify_task"
            await send_task_choices_page(
                callback.message, actor_id, service_id, "service_notify"
            )
        elif step in {"days", "time", "group_template"}:
            pending["step"] = "menu"
            await show_notification_settings_menu(
                callback.message, actor_id, service_id
            )
        else:
            await _answer_callback(callback, "⚠️ Нет активного шага для отмены.")
            return
        await _answer_callback(callback, "Изменения шага отменены")

    @admin_router.callback_query(F.data.startswith("notify_edit:"))
    async def on_notification_setting_edit(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        _, field, service_id, session_id = parts
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback,"❌ Реестр настроек недоступен.")
            return
        try:
            has_access = is_service_admin(
                callback.from_user.id, service_id
            )
        except sqlite3.Error:
            debug.exception(
                "Не удалось проверить права на изменение настроек служения"
            )
            await _answer_callback(callback,"❌ Не удалось проверить права.")
            return
        if not has_access:
            await _answer_callback(callback,"❌ Недостаточно прав.")
            return
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or pending.get("step") != "menu"
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        await begin_notification_edit(
            callback.message, callback.from_user.id, service_id, field
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_finish:"))
    async def on_notification_setting_finish(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        _, service_id, session_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        try:
            has_access = is_service_admin(
                callback.from_user.id, service_id
            )
        except sqlite3.Error:
            debug.exception(
                "Не удалось проверить права на сохранение настроек служения"
            )
            await _answer_callback(callback,"❌ Не удалось проверить права.")
            return
        if not has_access:
            await _answer_callback(callback,"❌ Недостаточно прав.")
            return
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or pending.get("step") != "menu"
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        await save_notification_setup(
            callback.message, callback.from_user.id
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_cancel:"))
    async def on_notification_setting_cancel(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        _, service_id, session_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        if (
            callback.message.chat.type != ChatType.PRIVATE
            or pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or pending.get("step") != "menu"
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        await clean_and_clear_pending_actor(callback.from_user.id)
        await _send_dialog(
            callback.message,
            "Настройка закрыта. Изменения групповых напоминаний, "
            "сохранённые до закрытия, остаются в силе.",
            reply_markup=service_result_keyboard(service_id),
            parse_mode=None,
        )
        await _answer_callback(callback,)

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
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":")
        if len(parts) != 4:
            await _answer_callback(callback,"❌ Некорректное подтверждение.")
            return
        action, service_id, raw_user_id, confirmation_id = parts
        action_type = action.rsplit("_", 1)[1]
        try:
            user_id = int(raw_user_id)
        except ValueError:
            await _answer_callback(callback,"❌ Некорректный Telegram ID.")
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
            await _answer_callback(callback,"⚠️ Запрос устарел. Начните команду заново.")
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
            if operation == "service_admin_add":
                await _send_dialog(
                    callback.message,
                    "Изменение отменено. Выберите пользователя заново.",
                    parse_mode=None,
                )
                await offer_user_selection(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                )
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
                    notice="✅ Назначение отменено.",
                )
            await _answer_callback(callback,)
            return
        registry = service_registry()
        if registry is None:
            if operation == "service_admin_add":
                await _send_dialog(callback.message, "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
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
                    notice="Реестр настроек служений недоступен.",
                )
            await _answer_callback(callback,)
            return
        response = ""
        assignment_saved = False
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
            response = str(exc)
        except sqlite3.Error:
            debug.exception("Не удалось сохранить выбор пользователя")
            response = (
                "Не удалось сохранить выбор из-за ошибки базы данных."
            )
        else:
            assignment_saved = True
            display_name = selected_display_name or f"Пользователь {user_id}"
            if operation == "service_admin_add":
                response = (
                    f"{display_name}"
                    + (f" (@{username})" if username else "")
                    + " назначен администратором служения."
                )
            else:
                response = (
                    f"Ответственный «{contact_name}» из файла расписания "
                    f"связан с {display_name}"
                    + (f" (@{username})" if username else "")
                    + f" для задачи «{task}»."
                )
        if operation == "service_admin_add":
            await clean_and_clear_pending_actor(callback.from_user.id)
            await _send_dialog(callback.message, _status_text(
                    response, "✅" if assignment_saved else "❌"
                ),
                reply_markup=(
                    service_result_keyboard(service_id)
                    if assignment_saved
                    else None
                ),
                parse_mode=None,
            )
        else:
            contact_pending = pending_inputs.get(callback.from_user.id)
            if contact_pending is not None:
                if assignment_saved:
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
                contact_pending["step"] = "choose_contact_person"
                contact_pending.pop("active_contact_name", None)
            if task:
                await offer_contact_name_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    task,
                    notice=response,
                )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("task_page:"))
    async def on_task_page(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        token = callback.data.partition(":")[2]
        selection = pending_task_pages.pop(token, None)
        if selection is None or selection[0] != callback.from_user.id:
            await _answer_callback(callback,"⚠️ Список устарел. Запустите команду заново.")
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
            await _answer_callback(callback,"⚠️ Список устарел. Запустите команду заново.")
            return
        await send_task_choices_page(
            callback.message,
            callback.from_user.id,
            service_id,
            operation,
            page,
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("contact_person:"))
    async def on_contact_person_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
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
            not in task_assignment_names(
                pending.get("schedule_assignments", {}).get(selection[2], [])
            )
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните команду заново.")
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
            await _send_dialog(callback.message, _status_text(str(exc), "❌"), parse_mode=None
            )
        except sqlite3.Error:
            debug.exception(
                "Не удалось создать запрос выбора ответственного"
            )
            await _send_dialog(callback.message, "Не удалось открыть выбор пользователя из-за ошибки базы.",
                parse_mode=None,
            )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("contact_nav:"))
    async def on_contact_navigation(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
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
            or pending.get("step")
            != (
                "choose_task"
                if selection[2] == "services"
                else "choose_contact_person"
            )
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните команду заново.")
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
        elif action == "services":
            await clean_and_clear_pending_actor(callback.from_user.id)
            await request_service_input(
                callback.message,
                "service_contact",
                actor_id=callback.from_user.id,
            )
        await _answer_callback(callback,)

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
            await _send_dialog(message, "Выбор пользователя не соответствует активному запросу. "
                "Запустите команду назначения заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if len(message.users_shared.users) != 1:
            await _send_dialog(message, "Нужно выбрать ровно одного пользователя.",
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
            await _send_dialog(message, "Выбор пользователя устарел. Выберите задачу и имя заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if operation == "service_contact" and (not task or not contact_name):
            await _send_dialog(message, "Не удалось определить задачу. Запустите привязку заново.",
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
            await _send_dialog(message, "Выбор чата не соответствует активному запросу. "
                "Запустите /service_chat заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен.",
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
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить выбранный групповой чат")
            await _send_dialog(message, "Не удалось сохранить групповой чат из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        if pending[2] == "assembly":
            context.group_chat_id = shared_chat.chat_id
        await clean_and_clear_pending_actor(message.from_user.id)
        title = shared_chat.title or "Групповой чат"
        await _send_dialog(
            message,
            f"✅ Чат сохранён · {title}\n"
            f"📡 ID чата · {ui_code(shared_chat.chat_id)}\n"
            "🟢 Служение включено",
            parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )
        await _send_dialog(
            message,
            "Выберите следующий шаг.",
            reply_markup=service_result_keyboard(pending[2]),
            parse_mode=None,
        )

    def personal_action_button(
        actor_id: int,
        state: dict[str, Any],
        label: str,
        action: str,
        **payload: Any,
    ) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(6)
        pending_personal_actions[token] = (
            actor_id,
            state["session_id"],
            state["screen_id"],
            action,
            payload,
        )
        return InlineKeyboardButton(
            text=label, callback_data=f"pmn:{state['session_id']}:{token}"
        )

    def push_personal_level(
        state: dict[str, Any], level: dict[str, Any]
    ) -> None:
        state["levels"].append({**level, "page": 0})

    def pop_personal_level(state: dict[str, Any]) -> None:
        if len(state["levels"]) > 1:
            state["levels"].pop()

    async def clean_previous_personal_menu(
        state: dict[str, Any]
    ) -> None:
        for token, action in list(pending_personal_actions.items()):
            if action[1] == state.get("session_id"):
                pending_personal_actions.pop(token, None)
        message_id = state.get("menu_message_id")
        chat_id = state.get("menu_chat_id")
        await clean_previous_menu_by_id(
            context.bot, chat_id, message_id, debug
        )

    async def finish_personal_session(actor_id: int) -> None:
        state = pending_inputs.get(actor_id)
        if state and state.get("operation", "").startswith("my_notifications"):
            await clean_previous_personal_menu(state)
        clear_pending_actor(actor_id)

    async def invalidate_personal_session(actor_id: int) -> None:
        state = pending_inputs.get(actor_id)
        if state and state.get("operation", "").startswith("my_notifications"):
            await clean_previous_personal_menu(state)
        clear_pending_actor(actor_id)

    async def current_personal_tasks(
        registry: Any,
        actor_id: int,
        service_id: str,
        state: dict[str, Any],
    ) -> list[str]:
        task_cache = state.setdefault("task_cache", {})
        task_cache_updated = state.setdefault("task_cache_updated", {})
        now = time.monotonic()
        if (
            service_id not in task_cache
            or now - task_cache_updated.get(service_id, 0)
            >= PERSONAL_TASK_CACHE_TTL_SECONDS
        ):
            assigned = set(registry.list_user_tasks(actor_id, service_id))
            loader = getattr(context, "load_service_task_assignments", None)
            if loader is None:
                raise ValueError("Не удалось проверить актуальный график служения.")
            current_assignments = await loader(service_id)
            task_cache[service_id] = [
                task
                for task, task_info in current_assignments.items()
                if task in assigned and not task_is_informational(task_info)
            ]
            informational_tasks = sorted(
                task
                for task, task_info in current_assignments.items()
                if task_is_informational(task_info)
            )
            if informational_tasks:
                debug.debug(
                    "Информационные задачи исключены из личных уведомлений "
                    "для пользователя %s: %s",
                    actor_id,
                    informational_tasks,
                )
            task_cache_updated[service_id] = time.monotonic()
        assigned_now = set(registry.list_user_tasks(actor_id, service_id))
        return [task for task in task_cache[service_id] if task in assigned_now]

    async def render_current_personal_level(
        target: Message,
        actor_id: int,
        state: dict[str, Any],
        *,
        clean_previous: bool = True,
    ) -> None:
        registry = service_registry()
        if registry is None:
            await _send_dialog(
                target,
                "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        if clean_previous:
            await clean_previous_personal_menu(state)
        state["screen_id"] = secrets.token_urlsafe(8)

        level = state["levels"][-1]
        name = level["name"]
        buttons: list[InlineKeyboardButton] = []
        if name == "summary":
            summary = registry.get_user_notifications_state(actor_id)
            services = summary["per_service"]
            lines = [
                "🔔 Мои личные уведомления",
                f"Включено: {summary['enabled_count']} · "
                f"выключено: {summary['disabled_count']}",
                "",
                "Выберите служение или измените все настройки:",
            ]
            for item in services:
                service = service_by_id(registry, item["service_id"])
                label = service_label(service) if service else item["service_id"]
                buttons.append(
                    personal_action_button(
                        actor_id,
                        state,
                        f"{'🟢' if item['enabled'] else '⚪'} {label} · "
                        f"правил {item['schedules_count']}",
                        "open_service",
                        service_id=item["service_id"],
                    )
                )
            text = "\n".join(lines)
            if not services:
                text = (
                    "ℹ️ Вы пока не привязаны ни к одной задаче. "
                    "Обратитесь к администратору служения."
                )
            if services:
                buttons.extend(
                    [
                        personal_action_button(
                            actor_id, state, "✅ Включить все", "bulk",
                            enabled=True,
                        ),
                        personal_action_button(
                            actor_id, state, "🔕 Выключить все", "bulk",
                            enabled=False,
                        ),
                    ]
                )
                buttons.append(
                    personal_action_button(
                        actor_id, state, "✖️ Закрыть", "finish"
                    )
                )
        elif name == "service":
            service_id = level["service_id"]
            if service_id not in registry.list_user_services(actor_id):
                raise ValueError("Вы больше не привязаны к этому служению.")
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            enabled = registry.get_user_notifications_enabled(
                actor_id, service_id
            )
            tasks = await current_personal_tasks(
                registry, actor_id, service_id, state
            )
            text = (
                f"🔔 {service_label(service)}\n\n"
                f"Личные уведомления: {'🟢 включены' if enabled else '⚪ выключены'}\n"
                f"Доступно задач: {len(tasks)}"
            )
            buttons = [
                personal_action_button(
                    actor_id, state,
                    "🔕 Выключить" if enabled else "✅ Включить",
                    "toggle_service", service_id=service_id,
                ),
                personal_action_button(
                    actor_id, state, "📝 Мои тексты", "open_texts",
                    service_id=service_id,
                ),
                personal_action_button(
                    actor_id, state, "⬅️ К служениям", "back"
                ),
            ]
        elif name == "texts":
            service_id = level["service_id"]
            tasks = await current_personal_tasks(
                registry, actor_id, service_id, state
            )
            available_tasks = set(tasks)
            texts = {
                item["task"]: item
                for item in registry.list_user_texts(actor_id, service_id)
            }
            tasks.extend(task for task in texts if task not in tasks)
            text = "📝 Личные тексты\nВыберите задачу для настройки:"
            for task in tasks:
                item = texts.get(task)
                schedules = (
                    registry.list_schedules(item["id"]) if item else []
                )
                status = "свой текст" if item and item["body"] else "по умолчанию"
                unavailable = (
                    " · задача недоступна"
                    if task not in available_tasks
                    else ""
                )
                buttons.append(
                    personal_action_button(
                        actor_id,
                        state,
                        f"{task[:36]}{unavailable} · {status} · "
                        f"правил {len(schedules)}",
                        "open_task",
                        service_id=service_id,
                        task=task,
                    )
                )
            buttons.append(
                personal_action_button(actor_id, state, "⬅️ Назад", "back")
            )
        elif name in {"task", "delete_text"}:
            service_id, task = level["service_id"], level["task"]
            text_entry = registry.get_user_text(actor_id, service_id, task)
            if name == "delete_text" and text_entry is None:
                raise ValueError("Личный текст больше не существует.")
            body = text_entry["body"] if text_entry else None
            if name == "delete_text":
                text = f"Удалить личный текст и его правила для задачи «{task}»?"
                buttons = [
                    personal_action_button(
                        actor_id, state, "🗑 Да, удалить", "confirm_delete_text",
                        service_id=service_id, task=task,
                    ),
                    personal_action_button(
                        actor_id, state, "⬅️ Отмена", "back"
                    ),
                ]
            else:
                schedule_count = (
                    len(registry.list_schedules(text_entry["id"]))
                    if text_entry else 0
                )
                shown_body = body or "Используется стандартный текст."
                text = (
                    f"📝 {task}\n\n{shown_body}\n\n"
                    f"Правил личного расписания: {schedule_count}"
                )
                buttons = [
                    personal_action_button(
                        actor_id, state, "✏️ Задать / изменить текст",
                        "edit_text", service_id=service_id, task=task,
                    ),
                    personal_action_button(
                        actor_id, state, "⏰ Расписание", "open_schedules",
                        service_id=service_id, task=task,
                    ),
                ]
                if body:
                    buttons.append(
                        personal_action_button(
                            actor_id, state, "🔄 Вернуть стандартный",
                            "reset_text", service_id=service_id, task=task,
                        )
                    )
                if text_entry is not None:
                    buttons.append(
                        personal_action_button(
                            actor_id, state, "🗑 Удалить текст",
                            "delete_text", service_id=service_id, task=task,
                        )
                    )
                buttons.append(
                    personal_action_button(actor_id, state, "⬅️ Назад", "back")
                )
        elif name == "schedules":
            service_id, task = level["service_id"], level["task"]
            text_entry = registry.get_user_text(actor_id, service_id, task)
            schedules = (
                registry.list_schedules(text_entry["id"]) if text_entry else []
            )
            text = f"⏰ Расписание · {task}\nВыберите правило или добавьте новое:"
            for schedule in schedules:
                buttons.append(
                    personal_action_button(
                        actor_id,
                        state,
                        f"{'🟢' if schedule['enabled'] else '⚪'} "
                        f"за {schedule['reminder_days']} дн. · "
                        f"{schedule['reminder_time']}",
                        "toggle_schedule",
                        service_id=service_id,
                        task=task,
                        schedule_id=schedule["id"],
                    )
                )
                buttons.append(
                    personal_action_button(
                        actor_id, state, f"✏️ Изменить · {schedule['id']}",
                        "edit_schedule", service_id=service_id, task=task,
                        schedule_id=schedule["id"],
                    )
                )
                buttons.append(
                    personal_action_button(
                        actor_id, state, f"🗑 Удалить · {schedule['id']}",
                        "delete_schedule", service_id=service_id, task=task,
                        schedule_id=schedule["id"],
                    )
                )
            buttons.extend(
                [
                    personal_action_button(
                        actor_id, state, "➕ Добавить правило", "add_schedule",
                        service_id=service_id, task=task,
                    ),
                    personal_action_button(
                        actor_id, state, "⬅️ Назад", "back"
                    ),
                ]
            )
        else:
            raise ValueError("Экран личных уведомлений устарел.")

        page_size = 4
        page_count = max(1, (len(buttons) + page_size - 1) // page_size)
        page = min(max(level.get("page", 0), 0), page_count - 1)
        visible = buttons[page * page_size:(page + 1) * page_size]
        rows = [[button] for button in visible]
        navigation = []
        for next_page, label in (
            (page - 1, "⬅️ Предыдущая"),
            (page + 1, "Следующая ➡️"),
        ):
            if 0 <= next_page < page_count:
                navigation.append(
                    personal_action_button(
                        actor_id, state, label, "page", page=next_page
                    )
                )
        if navigation:
            rows.append(navigation)
        if page_count > 1:
            text = f"{text}\nСтраница {page + 1}/{page_count}"
        sent = await _send_dialog(
            target,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            clean_previous=False,
        )
        if sent is not None:
            state["menu_message_id"] = sent.message_id
            state["menu_chat_id"] = target.chat.id

    @admin_router.message(Command("my_notifications"))
    async def on_my_notifications(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await invalidate_personal_session(message.from_user.id)
        registry = service_registry()
        if registry is None:
            await _send_dialog(
                message,
                "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        try:
            if not registry.list_user_services(message.from_user.id):
                await _send_dialog(
                    message,
                    "ℹ️ Вы пока не привязаны ни к одной задаче. "
                    "Обратитесь к администратору служения.",
                    parse_mode=None,
                )
                return
            state = {
                "operation": "my_notifications",
                "session_id": secrets.token_urlsafe(8),
                "levels": [{"name": "summary", "page": 0}],
            }
            pending_inputs[message.from_user.id] = state
            await render_current_personal_level(
                message, message.from_user.id, state, clean_previous=False
            )
        except (sqlite3.Error, ValueError):
            debug.exception("Не удалось открыть личные уведомления")
            await _send_dialog(
                message, "Не удалось открыть настройки личных уведомлений.",
                parse_mode=None,
            )

    @admin_router.callback_query(F.data.startswith("pmn:"))
    async def on_personal_notifications_callback(
        callback: CallbackQuery,
    ) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await _answer_callback(callback, "⚠️ Действие устарело.")
            return
        _, session_id, token = parts
        action = pending_personal_actions.pop(token, None)
        actor_id = callback.from_user.id
        state = pending_inputs.get(actor_id)
        if (
            action is None
            or action[0] != actor_id
            or action[1] != session_id
            or state is None
            or action[2] != state.get("screen_id")
            or state.get("operation") not in {
                "my_notifications", "my_notifications_input"
            }
            or state.get("session_id") != session_id
        ):
            await _answer_callback(callback, "⚠️ Меню устарело. Откройте /my_notifications.")
            return
        _, _, _, action_name, payload = action
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback, "❌ Реестр настроек недоступен.")
            return
        try:
            if action_name == "finish":
                await finish_personal_session(actor_id)
                await _answer_callback(callback, "Меню закрыто")
                return
            if state.get("operation") == "my_notifications_input":
                state["operation"] = "my_notifications"
                state.pop("input", None)
            current = state["levels"][-1]
            if action_name == "page":
                current["page"] = payload["page"]
            elif action_name == "back":
                pop_personal_level(state)
            elif action_name == "open_service":
                service_id = payload["service_id"]
                if service_id not in registry.list_user_services(actor_id):
                    raise ValueError("Вы не привязаны к этому служению.")
                push_personal_level(state, {"name": "service", "service_id": service_id})
            elif action_name == "toggle_service":
                service_id = payload["service_id"]
                if service_id not in registry.list_user_services(actor_id):
                    raise ValueError("Вы не привязаны к этому служению.")
                enabled = registry.get_user_notifications_enabled(actor_id, service_id)
                registry.set_user_notifications_enabled(
                    actor_id, service_id, not enabled
                )
            elif action_name == "bulk":
                registry.set_user_notifications_all(actor_id, payload["enabled"])
            elif action_name == "open_texts":
                service_id = payload["service_id"]
                if service_id not in registry.list_user_services(actor_id):
                    raise ValueError("Вы не привязаны к этому служению.")
                push_personal_level(
                    state, {"name": "texts", "service_id": service_id}
                )
            elif action_name == "open_task":
                service_id, task = payload["service_id"], payload["task"]
                text_entry = registry.get_user_text(actor_id, service_id, task)
                if text_entry is None and task not in await current_personal_tasks(
                    registry, actor_id, service_id, state
                ):
                    raise ValueError("Вы больше не привязаны к этой задаче.")
                push_personal_level(
                    state, {"name": "task", "service_id": service_id, "task": task}
                )
            elif action_name == "edit_text":
                if payload["task"] not in await current_personal_tasks(
                    registry,
                    actor_id,
                    payload["service_id"],
                    state,
                ):
                    raise ValueError(
                        "Эта задача недоступна для личных уведомлений."
                    )
                state["operation"] = "my_notifications_input"
                state["screen_id"] = secrets.token_urlsafe(8)
                state["input"] = {
                    "type": "text", "service_id": payload["service_id"],
                    "task": payload["task"],
                }
                await clean_previous_personal_menu(state)
                sent = await _send_dialog(
                    callback.message,
                    "Отправьте личный текст. Можно использовать поля "
                    "{name}, {task}, {service_name}, {sunday_date}, "
                    "{telegram_id}, {username}.",
                    parse_mode=None,
                    clean_previous=False,
                )
                if sent is not None:
                    state["menu_message_id"] = sent.message_id
                    state["menu_chat_id"] = callback.message.chat.id
                await _answer_callback(callback)
                return
            elif action_name == "reset_text":
                if registry.get_user_text(
                    actor_id, payload["service_id"], payload["task"]
                ) is None:
                    raise ValueError("Эта задача недоступна для личных уведомлений.")
                registry.reset_user_text(
                    actor_id, payload["service_id"], payload["task"]
                )
            elif action_name == "delete_text":
                if registry.get_user_text(
                    actor_id, payload["service_id"], payload["task"]
                ) is None:
                    raise ValueError("Эта задача недоступна для личных уведомлений.")
                push_personal_level(
                    state,
                    {
                        "name": "delete_text",
                        "service_id": payload["service_id"],
                        "task": payload["task"],
                    },
                )
            elif action_name == "confirm_delete_text":
                if registry.get_user_text(
                    actor_id, payload["service_id"], payload["task"]
                ) is None:
                    raise ValueError("Эта задача недоступна для личных уведомлений.")
                registry.delete_user_text(
                    actor_id, payload["service_id"], payload["task"]
                )
                pop_personal_level(state)
            elif action_name == "open_schedules":
                service_id, task = payload["service_id"], payload["task"]
                if task not in await current_personal_tasks(
                    registry,
                    actor_id,
                    service_id,
                    state,
                ):
                    raise ValueError("Вы больше не привязаны к этой задаче.")
                registry.ensure_user_text(actor_id, service_id, task)
                push_personal_level(
                    state,
                    {"name": "schedules", "service_id": service_id, "task": task},
                )
            elif action_name in {"add_schedule", "edit_schedule"}:
                state["operation"] = "my_notifications_input"
                state["screen_id"] = secrets.token_urlsafe(8)
                state["input"] = {
                    "type": action_name,
                    "service_id": payload["service_id"],
                    "task": payload["task"],
                    "schedule_id": payload.get("schedule_id"),
                }
                await clean_previous_personal_menu(state)
                prompt = (
                    "Введите количество дней до события и время HH:MM, "
                    "например: 2 18:30."
                )
                sent = await _send_dialog(
                    callback.message,
                    prompt,
                    parse_mode=None,
                    clean_previous=False,
                )
                if sent is not None:
                    state["menu_message_id"] = sent.message_id
                    state["menu_chat_id"] = callback.message.chat.id
                await _answer_callback(callback)
                return
            elif action_name in {"toggle_schedule", "delete_schedule"}:
                service_id, task = payload["service_id"], payload["task"]
                text_entry = registry.get_user_text(actor_id, service_id, task)
                schedules = (
                    registry.list_schedules(text_entry["id"]) if text_entry else []
                )
                schedule_id = payload["schedule_id"]
                schedule = next(
                    (item for item in schedules if item["id"] == schedule_id), None
                )
                if schedule is None:
                    raise ValueError("Правило расписания больше не существует.")
                if action_name == "toggle_schedule":
                    registry.update_schedule(
                        schedule_id, enabled=not bool(schedule["enabled"])
                    )
                else:
                    registry.delete_schedule(schedule_id)
            else:
                raise ValueError("Неизвестное действие личных уведомлений.")
            await render_current_personal_level(
                callback.message, actor_id, state
            )
        except PermissionError:
            await _answer_callback(callback, "❌ Недостаточно прав.")
            return
        except ValueError as exc:
            await _answer_callback(callback, f"⚠️ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Ошибка базы данных в личных уведомлениях")
            await _answer_callback(callback, "❌ Не удалось сохранить изменение.")
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось сверить задачи с актуальным графиком")
            await _answer_callback(
                callback,
                "❌ Не удалось загрузить актуальный график. Попробуйте позже.",
            )
            return
        await _answer_callback(callback)

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
        if pending.get("operation") == "my_notifications_input":
            registry = service_registry()
            if registry is None:
                await _send_dialog(
                    message, "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
                return
            input_state = pending.get("input", {})
            try:
                if input_state.get("type") == "text":
                    if input_state["task"] not in await current_personal_tasks(
                        registry,
                        message.from_user.id,
                        input_state["service_id"],
                        pending,
                    ):
                        raise ValueError(
                            "Эта задача недоступна для личных уведомлений."
                        )
                    registry.upsert_user_text(
                        message.from_user.id,
                        input_state["service_id"],
                        input_state["task"],
                        message.text.strip(),
                    )
                else:
                    fields = message.text.strip().split()
                    if (
                        len(fields) != 2
                        or not fields[0].isdigit()
                        or not re.fullmatch(
                            r"(?:[01]\d|2[0-3]):[0-5]\d", fields[1]
                        )
                    ):
                        raise ValueError(
                            "Введите количество дней и время HH:MM, например: 2 18:30."
                        )
                    days, time = int(fields[0]), fields[1]
                    service_id, task = (
                        input_state["service_id"], input_state["task"]
                    )
                    if task not in await current_personal_tasks(
                        registry,
                        message.from_user.id,
                        service_id,
                        pending,
                    ):
                        raise ValueError("Вы больше не привязаны к этой задаче.")
                    text_entry_id = registry.ensure_user_text(
                        message.from_user.id, service_id, task
                    )
                    if input_state["type"] == "add_schedule":
                        registry.add_schedule(text_entry_id, days, time)
                    else:
                        text_entry = registry.get_user_text(
                            message.from_user.id, service_id, task
                        )
                        schedules = (
                            registry.list_schedules(text_entry["id"])
                            if text_entry else []
                        )
                        schedule_id = input_state.get("schedule_id")
                        if not any(
                            item["id"] == schedule_id for item in schedules
                        ):
                            raise ValueError("Правило расписания больше не существует.")
                        registry.update_schedule(schedule_id, days, time)
                pending["operation"] = "my_notifications"
                pending.pop("input", None)
                await render_current_personal_level(
                    message, message.from_user.id, pending
                )
            except ValueError as exc:
                await _send_dialog(message, f"⚠️ {exc}", parse_mode=None)
            except sqlite3.Error:
                debug.exception("Не удалось сохранить настройки личного уведомления")
                await _send_dialog(
                    message, "❌ Не удалось сохранить настройки. Попробуйте позже.",
                    parse_mode=None,
                )
            except (OSError, RuntimeError):
                debug.exception(
                    "Не удалось сверить задачи с актуальным графиком"
                )
                await _send_dialog(
                    message,
                    "Не удалось сверить задачи с актуальным графиком. "
                    "Попробуйте позже.",
                    parse_mode=None,
                )
            return
        operation = pending["operation"]
        service_id = pending["service_id"]
        registry = service_registry()
        if registry is None:
            await clean_previous_menu_by_id(
                context.bot,
                pending.get("menu_chat_id"),
                pending.get("menu_message_id"),
                debug,
            )
            pending_inputs.pop(message.from_user.id, None)
            await _send_dialog(message, "Реестр настроек служений недоступен. Попробуйте позже.",
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
                if step in {
                    "group_reminder_days",
                    "group_reminder_time",
                    "group_reminder_text",
                }:
                    input_info = pending.get("group_reminder_input")
                    if input_info is None:
                        raise ValueError(
                            "Ввод устарел. Откройте напоминание заново."
                        )
                    reminder = registry.get_group_reminder(
                        input_info["reminder_id"]
                    )
                    if (
                        reminder is None
                        or reminder["service_id"] != service_id
                    ):
                        raise ValueError(
                            "Групповое напоминание больше не существует."
                        )
                    changes = {}
                    if step == "group_reminder_days":
                        try:
                            days = int(text)
                        except ValueError as exc:
                            raise ValueError(
                                "Введите целое число от 0 до 6."
                            ) from exc
                        if not 0 <= days <= 6:
                            raise ValueError(
                                "Число дней должно быть от 0 до 6."
                            )
                        changes["days"] = days
                    elif step == "group_reminder_time":
                        if not re.fullmatch(
                            r"(?:[01]\d|2[0-3]):[0-5]\d", text
                        ):
                            raise ValueError(
                                "Укажите время в формате HH:MM, например 17:15."
                            )
                        changes["time"] = text
                    else:
                        if not text:
                            raise ValueError(
                                "Текст группового напоминания не может быть пустым."
                            )
                        validate_notification_template(
                            text,
                            GROUP_TEMPLATE_FIELDS,
                            "Текст группового напоминания",
                        )
                        changes["body"] = text
                    try:
                        registry.update_group_reminder(
                            message.from_user.id,
                            input_info["reminder_id"],
                            **changes,
                        )
                    except ValueError as exc:
                        if (
                            "уже задано" not in str(exc)
                            or "days" not in changes
                            and "time" not in changes
                        ):
                            raise
                        updated_days = changes.get(
                            "days", reminder["reminder_days"]
                        )
                        updated_time = changes.get(
                            "time", reminder["reminder_time"]
                        )
                        pending["group_reminder_change"] = {
                            "reminder_id": input_info["reminder_id"],
                            "days": updated_days,
                            "time": updated_time,
                        }
                        pending["step"] = "group_reminder_duplicate_update"
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[[
                                InlineKeyboardButton(
                                    text="✅ Сохранить всё равно",
                                    callback_data=(
                                        f"group_reminder:confirm_update:"
                                        f"{service_id}:"
                                        f"{input_info['reminder_id']}:"
                                        f"{pending['session_id']}"
                                    ),
                                ),
                                InlineKeyboardButton(
                                    text="⬅️ Отмена",
                                    callback_data=(
                                        f"group_reminder:cancel_input:"
                                        f"{service_id}:"
                                        f"{input_info['reminder_id']}:"
                                        f"{pending['session_id']}"
                                    ),
                                ),
                            ]]
                        )
                        await _send_dialog(
                            message,
                            "Уже есть напоминание с таким сроком и временем. "
                            "При совпадении расписания оба напоминания будут "
                            "отправлены отдельными сообщениями. Сохранить?",
                            reply_markup=keyboard,
                            parse_mode=None,
                        )
                        return
                    has_schedule_change = bool(
                        {"days", "time"} & changes.keys()
                    )
                    pending.pop("group_reminder_input", None)
                    pending["step"] = "group_reminder_card"
                    if has_schedule_change:
                        refresh = getattr(
                            context, "refresh_service_reminder_jobs", None
                        )
                        if callable(refresh):
                            await refresh()
                    await enter_group_reminder_card(
                        message,
                        message.from_user.id,
                        service_id,
                        input_info["reminder_id"],
                    )
                    return
                if step == "group_default_text":
                    if not text:
                        raise ValueError(
                            "Шаблон группового уведомления не может быть пустым."
                        )
                    validate_notification_template(
                        text,
                        GROUP_TEMPLATE_FIELDS,
                        "Шаблон группового уведомления",
                    )
                    pending["group_template"] = text
                    pending["step"] = "default_template"
                    await enter_default_template(
                        message, message.from_user.id, service_id
                    )
                    return
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
                if step == "reminder_days":
                    try:
                        days = int(text)
                    except ValueError as exc:
                        raise ValueError(
                            "Введите целое число от 0 до 6."
                        ) from exc
                    if not 0 <= days <= 6:
                        raise ValueError("Число дней должно быть от 0 до 6.")
                    reminder = next(
                        (
                            item for item in pending["reminders"]
                            if item.get("id")
                            == pending.get("active_reminder_id")
                            or item.get("draft_id")
                            == pending.get("active_reminder_id")
                        ),
                        None,
                    )
                    if reminder is None:
                        raise ValueError(
                            "Выбранное уведомление устарело. Откройте список заново."
                        )
                    reminder["reminder_days"] = days
                    if pending.get("reminder_operation") == "add":
                        pending["step"] = "reminder_time"
                        await _send_dialog(message, "Введите локальное время отправки в формате HH:MM, "
                            "например 09:00.",
                            reply_markup=notification_edit_controls(
                                service_id, pending["session_id"]
                            ),
                            parse_mode=None,
                        )
                    else:
                        pending.pop("active_reminder_original", None)
                        pending.pop("active_reminder_id", None)
                        pending.pop("reminder_operation", None)
                        pending["step"] = "menu"
                        await show_reminder_rules_menu(
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
                if step == "reminder_time":
                    if (
                        len(text) != 5
                        or text[2] != ":"
                        or not text[:2].isdigit()
                        or not text[3:].isdigit()
                        or not 0 <= int(text[:2]) <= 23
                        or not 0 <= int(text[3:]) <= 59
                    ):
                        raise ValueError(
                            "Укажите время в формате HH:MM, например 09:00."
                        )
                    reminder = next(
                        (
                            item for item in pending["reminders"]
                            if item.get("id")
                            == pending.get("active_reminder_id")
                            or item.get("draft_id")
                            == pending.get("active_reminder_id")
                        ),
                        None,
                    )
                    if reminder is None:
                        raise ValueError(
                            "Выбранное уведомление устарело. Откройте список заново."
                        )
                    reminder["reminder_time"] = text
                    reminder["complete"] = True
                    pending.pop("active_reminder_original", None)
                    pending.pop("active_reminder_id", None)
                    pending["reminder_operation"] = None
                    pending["step"] = "menu"
                    await show_reminder_rules_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "group_template":
                    if not text:
                        raise ValueError("Текст для группы не может быть пустым.")
                    validate_notification_template(
                        text,
                        GROUP_TEMPLATE_FIELDS,
                        "Шаблон группового уведомления",
                    )
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
                    validate_notification_template(
                        text,
                        PERSONAL_TEMPLATE_FIELDS,
                        f"Личный шаблон для задачи «{active_task}»",
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
                await clean_previous_menu_by_id(
                    context.bot,
                    pending.get("menu_chat_id"),
                    pending.get("menu_message_id"),
                    debug,
                )
                pending_inputs.pop(message.from_user.id, None)
                return
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Ошибка сохранения настроек служения")
            await _send_dialog(message, "Не удалось сохранить настройки из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось обновить расписание уведомлений")
            await _send_dialog(
                message,
                "Не удалось обновить расписание уведомлений. "
                "Изменение сохранено; попробуйте позже.",
                parse_mode=None,
            )
            return

        await clean_previous_menu_by_id(
            context.bot,
            pending.get("menu_chat_id"),
            pending.get("menu_message_id"),
            debug,
        )
        pending_inputs.pop(message.from_user.id, None)
        await _send_dialog(
            message,
            _status_text(response, "✅"),
            reply_markup=service_result_keyboard(service_id),
            parse_mode=None,
        )

    @admin_router.message(Command("service_sync"))
    async def on_service_sync(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        payload = (message.text or "").partition(" ")[2].strip()
        if payload:
            await _send_dialog(message, "Служение нужно выбрать кнопкой. Используйте /service_sync "
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
                await _send_dialog(message, "Для этого служения не настроен групповой чат.",
                    reply_markup=test_result_keyboard(service["id"]),
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
                await _send_dialog(message, f"Не удалось отправить тест в группу: {type(exc).__name__}.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
            else:
                await _send_dialog(message, f"Тестовое сообщение службы «{service['name']}» отправлено "
                    "в настроенную группу. Рассылка участникам не запускалась.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
            return

        prepare_previews = getattr(
            context, "prepare_service_test_previews", None
        )
        if not callable(prepare_previews):
            await _send_dialog(message, "Предпросмотр сообщений для служений недоступен.",
                reply_markup=test_result_keyboard(service["id"]),
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
            await _send_dialog(message, f"Не удалось подготовить предпросмотр: {type(exc).__name__}.",
                reply_markup=test_result_keyboard(service["id"]),
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
                await _send_dialog(message, "Не удалось доставить администратору полный предпросмотр.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
                return
        await _send_dialog(message, "Предпросмотр выбранной службы отправлен вам в личные сообщения. "
            "Сообщения в группу и участникам не отправлялись.",
            reply_markup=test_result_keyboard(service["id"]),
            parse_mode=None,
        )

    @router.message(Command("test_notify"))
    async def on_test_notify(message: Message) -> None:
        if not message.from_user or message.from_user.id != context.admin_id:
            return
        if message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр служений недоступен; нельзя выбрать чат проверки.",
                parse_mode=None,
            )
            return
        try:
            services = registry.list_services()
        except sqlite3.Error:
            debug.exception("Не удалось загрузить служения для теста доставки")
            await _send_dialog(message, "Не удалось прочитать список служений.", parse_mode=None
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
        await _send_dialog(message, "🧪 Сначала выберите служение, затем — вид проверки.",
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
            await _answer_callback(callback,)
            return
        _, action, service_id = callback.data.split(":", 2)
        await clean_previous_menu(callback.message, debug)
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback,"❌ Реестр служений недоступен.")
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
            await _send_dialog(callback.message, _status_text(str(exc), "❌"), parse_mode=None
            )
        await _answer_callback(callback,)

    @router.callback_query(F.data.startswith(f"{CONFIRM_PREFIX}:"))
    async def on_confirm(cb: CallbackQuery) -> None:  # noqa: D401
        parts = cb.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(cb,"❌ Некорректные данные кнопки")
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

    @router.callback_query(F.data.startswith("scf:"))
    async def on_service_confirm(cb: CallbackQuery) -> None:
        if not cb.from_user:
            await _answer_callback(cb,"❌ Не удалось определить пользователя")
            return
        parts = cb.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(cb,"❌ Некорректные данные кнопки")
            return
        _, event_date, participant_key = parts
        if (
            len(event_date) != 10
            or event_date[4] != "-"
            or event_date[7] != "-"
            or not participant_key.startswith("service_")
        ):
            await _answer_callback(cb,"❌ Некорректные данные кнопки")
            return
        locks = getattr(context, "confirmation_locks", None)
        if locks is None:
            locks = {}
            context.confirmation_locks = locks
        lock_key = (event_date, participant_key)
        lock = locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            await _confirm_locked(
                cb, context, event_date, participant_key, interaction, debug
            )

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
        await _answer_callback(cb,"⚠️ Это уведомление устарело")
        return

    participant = state.get("participants", {}).get(role)
    if not participant or participant.get("tg_id") != cb.from_user.id:
        await _answer_callback(cb,"❌ Вы не назначены на эту роль")
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
            await _answer_callback(cb,
                "❌ Не удалось сохранить подтверждение. "
                "Нажмите кнопку повторно."
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
                f"🔔 Участник {participant['name']} подтвердил "
                f"{'задачу' if participant.get('task') else 'роль'} "
                f"«{participant.get('task', role)}»",
                parse_mode=None,
            )
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось отправить апдейт в группу")
        else:
            participant["group_chat_id"] = group_chat_id
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
        await _answer_callback(cb,
            "✅ Участие подтверждено"
            if newly_persisted
            else "ℹ️ Вы уже подтвердили участие"
        )
    elif participant.get("group_update_sent"):
        await _answer_callback(cb,
            "⚠️ Уведомление отправлено, но состояние не удалось сохранить. "
            "Нажмите кнопку повторно для повтора сохранения."
        )
    elif group_update_blocked_reason:
        await _answer_callback(cb,
            "⚠️ Подтверждение сохранено, но уведомление в группу не отправлено: "
            f"{group_update_blocked_reason}."
        )
    else:
        await _answer_callback(cb,
            "⚠️ Подтверждение сохранено, но уведомление не отправлено. "
            "Нажмите кнопку повторно для повтора."
        )
