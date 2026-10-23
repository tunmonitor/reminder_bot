import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.methods import SendMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.navigation import (
    ScreenMessage,
    back_callback,
    is_nav_keyboard,
    nav_keyboard,
    replaces_nav_keyboard,
)


def markup(*buttons):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=data)
        for text, data in buttons
    ]])


class NavigationTest(unittest.TestCase):
    def test_back_callback_takes_last_back_button_but_not_paging(self):
        self.assertEqual(
            back_callback(markup(
                ("⬅️ К напоминаниям", "a"),
                ("✏️ Изменить", "b"),
                ("↩️ К настройкам", "c"),
                ("⬅️ Назад", "ui_page:x"),
            )),
            "c",
        )
        self.assertIsNone(back_callback(markup(("⬅️ Назад", "ui_page:x"))))
        self.assertIsNone(back_callback(markup(("📋 Служения", "menu:s"))))
        self.assertIsNone(back_callback(markup(
            ("⬅️ Стр. 1", "p"), ("⬅️ Предыдущее", "q"), ("⬅️ 12.10", "r"),
        )))
        self.assertEqual(
            back_callback(markup(
                ("⬅️ К задачам", "contact_nav:a"),
                ("⬅️ Назад", "contact_nav:b"),
                ("⬅️ Назад", "task_page:c"),
            )),
            "contact_nav:a",
        )
        self.assertEqual(
            back_callback(markup(("⬅️ Назад", "pact:d"))), "pact:d"
        )

    def test_screen_message_counts_screens_but_not_warnings(self):
        source = SimpleNamespace(
            chat=SimpleNamespace(id=5), from_user=None, answer=AsyncMock()
        )
        screen = ScreenMessage(source, 10, SimpleNamespace(), None)

        async def run():
            await screen.notify("⚠️ Этот экран уже устарел.")
            self.assertEqual(screen.sent, 0)
            await screen.answer("Экран")
            self.assertEqual(screen.sent, 1)

        asyncio.run(run())

    def test_other_reply_keyboards_replace_navigation(self):
        self.assertTrue(is_nav_keyboard(nav_keyboard()))
        self.assertIsNone(replaces_nav_keyboard(
            SendMessage(chat_id=5, text="x", reply_markup=nav_keyboard())
        ))
        self.assertEqual(replaces_nav_keyboard(
            SendMessage(chat_id=5, text="x", reply_markup=ReplyKeyboardRemove())
        ), 5)
        self.assertIsNone(replaces_nav_keyboard(
            SendMessage(chat_id=5, text="x", reply_markup=markup(("a", "b")))
        ))


if __name__ == "__main__":
    unittest.main()
