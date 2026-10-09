"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .notifier import CONFIRM_PREFIX

router = Router()


def register(context: Any) -> None:
    """Регистрирует хэндлер, замыкая его на context."""

    interaction: logging.Logger = context.interaction_logger
    debug: logging.Logger = context.debug_logger
    dispatcher = getattr(context, "dispatcher", None)

    @router.callback_query(F.data.startswith(f"{CONFIRM_PREFIX}:"))
    async def on_confirm(cb: CallbackQuery) -> None:  # noqa: D401
        parts = cb.data.split(":", 2)
        if len(parts) != 3:
            await cb.answer("Некорректные данные кнопки")
            return
        _, sunday_iso, role = parts

        state = context.state_cache.state
        if not state or state.get("sunday_date") != sunday_iso:
            await cb.answer("Это уведомление устарело")
            return

        participant = state.get("participants", {}).get(role)
        if not participant or participant.get("tg_id") != cb.from_user.id:
            await cb.answer("Вы не назначены на эту роль")
            return

        # --- защита от двойного клика ---
        if participant.get("confirmed"):
            await cb.answer("Вы уже подтвердили участие!")
            return

        participant["confirmed"] = True
        context.state_cache.save()

        # --- правим исходное сообщение в ЛС ---
        if cb.message and cb.message.text:
            try:
                await cb.message.edit_text(
                    cb.message.text + "\n\n✅ Подтверждено",
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось отредактировать ЛС")

        # --- апдейт в группу ---
        try:
            await context.bot.send_message(
                context.group_chat_id,
                f"🔔 Участник {participant['name']} подтвердил роль {role}",
            )
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось отправить апдейт в группу")

        interaction.info(
            "Подтверждение: role=%s tg_id=%s username=@%s name=%s",
            role, cb.from_user.id, cb.from_user.username, participant["name"],
        )
        await cb.answer("Принято!")

    if dispatcher is not None:
        dispatcher.include_router(router)

