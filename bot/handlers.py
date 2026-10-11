"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import F, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .notifier import CONFIRM_PREFIX, _notify_admin

router = Router()


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
