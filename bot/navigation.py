"""Нижняя клавиатура навигации «↩️ Отмена» / «🏠 В начало».

Показывается под строкой ввода, пока открыт экран второго уровня и
глубже (у экрана есть кнопка «⬅️ …»), и убирается на корневых экранах.
«Отмена» повторяет кнопку «⬅️ …» последнего экрана, «В начало» — /start.
"""
from __future__ import annotations

from typing import Any

from aiogram.methods import SendMessage
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

NAV_CANCEL_TEXT = "↩️ Отмена"
NAV_HOME_TEXT = "🏠 В начало"
NAV_TEXTS = frozenset({NAV_CANCEL_TEXT, NAV_HOME_TEXT})
NAV_HINT = (
    "⌨️ Внизу: «↩️ Отмена» — шаг назад, «🏠 В начало» — главное меню."
)
NAV_REMOVED_TEXT = "🏠"
# ID нажатия, которое бот имитирует сам: на него не отвечают в Telegram.
NAV_CALLBACK_ID = "nav-cancel"
# «⬅️ Назад» листания страниц — не шаг назад по диалогу.
_PAGE_PREFIXES = ("ui_page:", "task_page:")
# В навигации по ответственным возврат — «⬅️ К …», а «⬅️ Назад» — листание
# списка участников.
_CONTACT_NAV_PREFIX = "contact_nav:"
# Выход из настройки к её родителю — тоже шаг назад. «✖️ Закрыть» корня
# «Моих уведомлений» сюда не входит: это корневой экран.
_CLOSE_TEXTS = ("✖️ Закрыть настройку", "✖️ Отмена")
# Шаг назад по диалогу; «⬅️ Стр. N», «⬅️ Предыдущее», «⬅️ дд.мм» —
# листание, а не возврат.
_BACK_PREFIXES = ("⬅️ К ", "↩️ К ", "⬅️ Назад", "⬅️ Отмена")


def nav_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=NAV_CANCEL_TEXT),
            KeyboardButton(text=NAV_HOME_TEXT),
        ]],
        resize_keyboard=True,
        is_persistent=True,
    )


def back_callback(markup: InlineKeyboardMarkup) -> str | None:
    """callback_data последней кнопки «⬅️ К …»/«⬅️ Назад»; если таких нет —
    последней «✖️ Закрыть настройку» / «✖️ Отмена»: с такого экрана тоже
    есть куда вернуться, это не корневой экран."""
    back = close = None
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data
            if not data or data.startswith(_PAGE_PREFIXES):
                continue
            if button.text.startswith(_BACK_PREFIXES) and not (
                data.startswith(_CONTACT_NAV_PREFIX)
                and not button.text.startswith("⬅️ К ")
            ):
                back = data
            elif button.text.startswith(_CLOSE_TEXTS):
                close = data
    return back or close


def is_nav_keyboard(markup: Any) -> bool:
    return isinstance(markup, ReplyKeyboardMarkup) and any(
        button.text in NAV_TEXTS
        for row in markup.keyboard
        for button in row
    )


def replaces_nav_keyboard(method: Any) -> int | None:
    """Чат, где отправка заменила или убрала клавиатуру навигации."""
    if not isinstance(method, SendMessage):
        return None
    markup = method.reply_markup
    if isinstance(markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)) and not (
        is_nav_keyboard(markup)
    ):
        return method.chat_id if isinstance(method.chat_id, int) else None
    return None


class ScreenMessage:
    """Последний экран диалога для имитации нажатия «⬅️ …»: новые экраны
    уходят в тот же чат, удаление и правка — по ID экрана."""

    text = None
    message_thread_id = None
    users_shared = None
    chat_shared = None

    def __init__(self, source: Any, message_id: int | None, bot: Any, date):
        self.chat = source.chat
        self.from_user = source.from_user
        self.message_id = message_id
        self.date = date
        self._source = source
        self._bot = bot
        self.sent = 0

    async def answer(self, *args: Any, **kwargs: Any) -> Any:
        self.sent += 1
        return await self._source.answer(*args, **kwargs)

    async def notify(self, text: str) -> Any:
        """Предупреждение вместо всплывающего ответа; не считается экраном."""
        return await self._source.answer(text, parse_mode=None)

    async def delete(self) -> Any:
        return await self._bot.delete_message(
            chat_id=self.chat.id, message_id=self.message_id
        )

    async def edit_text(self, text: str, **kwargs: Any) -> Any:
        return await self._bot.edit_message_text(
            text, chat_id=self.chat.id, message_id=self.message_id, **kwargs
        )

    async def edit_reply_markup(self, **kwargs: Any) -> Any:
        return await self._bot.edit_message_reply_markup(
            chat_id=self.chat.id, message_id=self.message_id, **kwargs
        )
