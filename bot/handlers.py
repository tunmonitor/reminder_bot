"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import sqlite3
from typing import Any

from aiogram import F, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

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

    async def run_registry_command(message: Message, operation) -> None:
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
        for chunk in _telegram_text_chunks(response):
            await message.answer(chunk, parse_mode=None)

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
                return

            lines = ["Служения:"]
            for service in visible:
                state = "включено" if service["enabled"] else "выключено"
                lines.append(
                    f"{service['id']} — {service['name']}: {state}"
                )
                if is_super_admin:
                    admins = registry.list_service_admins(service["id"])
                    lines.append(
                        "  чат: "
                        f"{service['group_chat_id'] or 'не задан'}; "
                        f"админы: {', '.join(map(str, admins)) or 'нет'}"
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
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer(
                "Формат: /service_settings <service_id>", parse_mode=None
            )
            return

        service_id = parts[1]

        def show_settings(actor_id: int) -> str:
            registry = service_registry()
            if registry is None or not is_service_admin(actor_id, service_id):
                raise PermissionError(
                    "Настройки доступны только администратору этого служения."
                )
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError(f"Неизвестное служение: {service_id}")
            lines = [
                f"{service['name']} ({service['id']})",
                f"Статус: {'включено' if service['enabled'] else 'выключено'}",
                f"Групповой чат: {service['group_chat_id'] or 'не задан'}",
                f"Напоминание: за {service['reminder_days']} дн. "
                f"в {service['reminder_time']}",
                "Шаблон группы: "
                f"{service['group_template'] or 'не задан'}",
                "Личные шаблоны: "
                + (
                    json.dumps(
                        service["personal_templates"], ensure_ascii=False
                    )
                    if service["personal_templates"]
                    else "не заданы"
                ),
            ]
            if registry.is_super_admin(actor_id):
                admins = registry.list_service_admins(service_id)
                contacts = registry.list_responsible_contacts(
                    actor_id, service_id
                )
                lines.append(
                    f"Администраторы: {', '.join(map(str, admins)) or 'нет'}"
                )
                lines.append("Привязки ответственных:")
                lines.extend(
                    f"  {item['task']} — {item['name']}: "
                    f"{item['telegram_id']}"
                    + (f" (@{item['username']})" if item["username"] else "")
                    for item in contacts
                )
                if not contacts:
                    lines.append("  нет")
            return "\n".join(lines)

        await run_registry_command(message, show_settings)

    @admin_router.message(Command("service_admin_add"))
    async def on_service_admin_add(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer(
                "Формат: /service_admin_add <service_id> <telegram_id>",
                parse_mode=None,
            )
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
        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer(
                "Формат: /service_admin_remove <service_id> <telegram_id>",
                parse_mode=None,
            )
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
        parts = (message.text or "").split()
        if len(parts) != 4:
            await message.answer(
                "Формат: /service_chat <service_id> <chat_id> <on|off>",
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
        payload = (message.text or "").partition(" ")[2].strip()
        parts = payload.split("|")
        prefix_parts = parts[0].strip().split(maxsplit=1) if parts else []
        details = [field.strip() for field in parts[1:]]
        if len(prefix_parts) != 2 or len(details) not in {2, 3}:
            await message.answer(
                "Формат: /service_contact <service_id> "
                "<задача> | <имя> | <telegram_id> [| <username>]",
                parse_mode=None,
            )
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
        payload = (message.text or "").partition(" ")[2]
        fields = [field.strip() for field in payload.split("|", 2)]
        if len(fields) != 3:
            await message.answer(
                "Формат: /service_notify <service_id> <дни> <HH:MM> | "
                "<текст группы> | <JSON личных шаблонов>",
                parse_mode=None,
            )
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

    @router.message(Command("test_notify"))
    async def on_test_notify(message: Message) -> None:
        if not message.from_user or message.from_user.id != context.admin_id:
            return
        if message.chat.type != ChatType.PRIVATE:
            return

        group_result = "✅ отправлено"
        try:
            await context.bot.send_message(
                context.group_chat_id,
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
            message.from_user.id,
            group_result,
            "готов" if previews else "ошибка",
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
        await _persist_confirmation(context, debug)

        if cb.message and cb.message.text:
            try:
                await cb.message.edit_text(
                    cb.message.text + "\n\n✅ Подтверждено",
                    parse_mode=None,
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось отредактировать ЛС")

    if not participant.get("group_update_sent"):
        try:
            await context.bot.send_message(
                context.group_chat_id,
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
    else:
        await cb.answer(
            "Подтверждение сохранено, но уведомление не отправлено. "
            "Нажмите кнопку повторно для повтора."
        )
