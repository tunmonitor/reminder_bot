"""Веерная рассылка: общее сообщение в группу + персональные ЛС с кнопкой."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RATE_LIMIT_DELAY = 0.05         # согласно ТЗ — защита от HTTP 429
CONFIRM_PREFIX = "cfm"          # короткий префикс для callback_data


def build_confirm_kb(role: str, sunday_iso: str) -> InlineKeyboardMarkup:
    data = f"{CONFIRM_PREFIX}:{sunday_iso}:{role}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Принял, буду", callback_data=data),
    ]])


async def _notify_admin(bot: Bot, admin_id: int, text: str,
                        debug_logger: logging.Logger) -> None:
    try:
        await bot.send_message(admin_id, f"⚠️ {text}")
    except Exception:  # noqa: BLE001
        debug_logger.exception("Не удалось уведомить администратора")


async def send_assignments(
    bot: Bot,
    group_chat_id: int,
    admin_id: int,
    assignments: dict[str, dict],
    state: dict,
    templates: dict,
    sunday_str: str,
    sunday_iso: str,
    interaction_logger: logging.Logger,
    debug_logger: logging.Logger,
) -> None:
    """Отправляет общее сообщение и ЛС. Обновляет state на месте."""
    # --- 1) Общее сообщение в группу ---
    general_text = templates["general"].format(
        sunday_date=sunday_str,
        tg_responsible=assignments["Ответственный"]["username"],
        tg_leader=assignments["Ведущий"]["username"],
        tg_preacher=assignments["Проповедник"]["username"],
    )
    try:
        general_msg = await bot.send_message(group_chat_id, general_text)
    except TelegramAPIError as exc:
        debug_logger.exception("Не удалось отправить сообщение в группу")
        await _notify_admin(bot, admin_id,
                            f"Ошибка отправки в группу: {exc}", debug_logger)
        raise

    state["general_message_id"] = general_msg.message_id
    interaction_logger.info(
        "Общее уведомление отправлено в чат %s (msg_id=%s)",
        group_chat_id, general_msg.message_id,
    )

    # --- 2) Персональные ЛС ---
    for role, info in assignments.items():
        text = templates["roles"][role].format(
            name=info["name"], sunday_date=sunday_str,
        )
        try:
            msg = await bot.send_message(
                info["tg_id"], text,
                reply_markup=build_confirm_kb(role, sunday_iso),
            )
            state["participants"][role]["message_id"] = msg.message_id
            state["participants"][role]["chat_id"] = msg.chat.id
            interaction_logger.info(
                "ЛС отправлено: role=%s tg_id=%s username=@%s",
                role, info["tg_id"], info["username"],
            )
        except TelegramAPIError as exc:
            debug_logger.exception(
                "Не удалось отправить ЛС роли '%s' (tg_id=%s)", role, info["tg_id"],
            )
            await _notify_admin(
                bot, admin_id,
                f"Не удалось отправить ЛС {info['name']} ({role}): {exc}",
                debug_logger,
            )

        await asyncio.sleep(RATE_LIMIT_DELAY)   # защита от 429
