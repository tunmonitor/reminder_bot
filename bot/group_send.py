"""Отправка в чат служения с учётом выбранной темы (/here)."""
from __future__ import annotations

from aiogram.exceptions import TelegramAPIError


def thread_kwargs(service: dict | None) -> dict:
    """message_thread_id темы чата служения, если тема выбрана (/here)."""
    thread_id = (service or {}).get("group_thread_id")
    return {"message_thread_id": thread_id} if thread_id else {}


async def send_to_service_group(
    send, chat_id: int, text: str, service: dict | None,
    registry=None, logger=None, **kwargs,
):
    """Отправка в чат служения с учётом темы.

    Если тема удалена или закрыта, сообщение уходит в общую тему; удалённая
    тема сбрасывается (иначе служение перестало бы получать напоминания),
    закрытая остаётся выбранной.
    """
    thread = thread_kwargs(service)
    try:
        return await send(chat_id, text, **kwargs, **thread)
    except TelegramAPIError as exc:
        error = str(exc).lower()
        # «message thread not found», TOPIC_CLOSED/TOPIC_DELETED/... —
        # тема недоступна, сообщение уходит в общую тему.
        if not thread or ("thread" not in error and "topic" not in error):
            raise
        if logger is not None:
            logger.warning(
                "Тема %s чата служения %s недоступна (%s); отправка в общую тему",
                thread["message_thread_id"], service["id"], exc,
            )
    # Закрытую тему могут открыть снова — она остаётся выбранной.
    if registry is not None and "closed" not in error:
        try:
            registry.reset_group_thread(
                service["id"], thread["message_thread_id"]
            )
        except Exception:  # noqa: BLE001
            if logger is not None:
                logger.exception("Не удалось сбросить тему чата служения")
    return await send(chat_id, text, **kwargs)
