import asyncio
import html
import logging
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui import (
    callback_text,
    clean_previous_menu,
    clean_previous_menu_by_id,
    code,
    layout_buttons,
    nav_row,
    render_screen_chunks,
    send_telegram_screen,
    validate_inline_keyboard,
)


class DialogRenderingTest(unittest.TestCase):
    def test_render_escapes_untrusted_text_and_bolds_title(self):
        self.assertEqual(
            render_screen_chunks("Заголовок\n<a & b>"),
            ["<b>Заголовок</b>\n&lt;a &amp; b&gt;"],
        )

    def test_code_marker_is_safe_and_retains_code_formatting(self):
        self.assertEqual(
            render_screen_chunks(f"ID\n{code('<123&>')}"),
            ["<b>ID</b>\n<code>&lt;123&amp;&gt;</code>"],
        )

    def test_long_unicode_text_is_split_at_utf16_limit(self):
        chunks = render_screen_chunks("Заголовок\n" + "😀" * 300, limit=64)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            plain_text = re.sub(r"</?b>|</?code>", "", html.unescape(chunk))
            self.assertLessEqual(len(plain_text.encode("utf-16-le")) // 2, 64)

    def test_callback_toast_is_plain_and_limited(self):
        self.assertEqual(callback_text("✅ Готово"), "Готово")
        self.assertEqual(len(callback_text("x" * 250)), 200)
        self.assertIsNone(callback_text("✅"))

    def test_inline_keyboard_limits(self):
        valid = [[InlineKeyboardButton(text="OK", callback_data="ok")]]
        validate_inline_keyboard(valid)
        with self.assertRaises(ValueError):
            validate_inline_keyboard(
                [[
                    InlineKeyboardButton(text=str(index), callback_data=str(index))
                    for index in range(4)
                ]]
            )
        with self.assertRaises(ValueError):
            validate_inline_keyboard([
                [InlineKeyboardButton(text=str(index), callback_data=str(index))]
                for index in range(7)
            ])

    def test_layout_buttons_keeps_long_buttons_on_separate_rows(self):
        buttons = [
            InlineKeyboardButton(
                text="📝 Текст группового сообщения", callback_data="group"
            )
        ]

        self.assertEqual(layout_buttons(buttons), [buttons])

    def test_layout_buttons_pairs_short_buttons(self):
        buttons = [
            InlineKeyboardButton(text="✅ Включить", callback_data="on"),
            InlineKeyboardButton(text="✖️ Выключить", callback_data="off"),
        ]

        self.assertEqual(layout_buttons(buttons), [buttons])

    def test_layout_buttons_places_three_very_short_buttons_together(self):
        buttons = [
            InlineKeyboardButton(text=f"{day} дн.", callback_data=str(day))
            for day in range(3)
        ]

        self.assertEqual(layout_buttons(buttons), [buttons])

    def test_layout_buttons_wraps_four_medium_buttons_as_pairs(self):
        buttons = [
            InlineKeyboardButton(
                text=f"Средняя кнопка {index}", callback_data=str(index)
            )
            for index in range(4)
        ]

        self.assertEqual(
            [[button.text for button in row] for row in layout_buttons(buttons)],
            [
                ["Средняя кнопка 0", "Средняя кнопка 1"],
                ["Средняя кнопка 2", "Средняя кнопка 3"],
            ],
        )

    def test_layout_buttons_moves_navigation_to_end_even_if_given_first(self):
        buttons = [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="back"),
            InlineKeyboardButton(text="Задача А", callback_data="a"),
            InlineKeyboardButton(text="Задача Б", callback_data="b"),
        ]

        rows = layout_buttons(buttons)

        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [["Задача А", "Задача Б"], ["⬅️ Назад"]],
        )

    def test_layout_buttons_keeps_semantic_action_buttons_separate(self):
        buttons = [
            InlineKeyboardButton(
                text="⏰ Напоминания (3)", callback_data="reminders"
            ),
            InlineKeyboardButton(
                text="📝 Групповой текст", callback_data="group"
            ),
            InlineKeyboardButton(text="Задача A", callback_data="task"),
        ]

        self.assertEqual(
            [[button.text for button in row] for row in layout_buttons(buttons)],
            [
                ["⏰ Напоминания (3)"],
                ["📝 Групповой текст"],
                ["Задача A"],
            ],
        )

    def test_nav_row_omits_missing_controls_and_keeps_declared_order(self):
        back = InlineKeyboardButton(text="⬅️ Назад", callback_data="back")
        cancel = InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")

        self.assertEqual(nav_row(back=back, cancel=cancel), [back, cancel])

    def test_pagination_attaches_markup_only_to_last_screen(self):
        async def send():
            send_message = AsyncMock(
                return_value=SimpleNamespace(message_id=2)
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Next", callback_data="next")
            ]])

            result = await send_telegram_screen(
                send_message,
                42,
                "Заголовок\n" + "x" * 700,
                reply_markup=markup,
            )

            self.assertEqual(result.message_id, 2)
            self.assertEqual(send_message.await_count, 3)
            self.assertTrue(
                all(
                    call.kwargs["reply_markup"] is None
                    for call in send_message.await_args_list[:-1]
                )
            )
            self.assertIs(send_message.await_args_list[-1].kwargs["reply_markup"], markup)
            self.assertEqual(
                [call.kwargs["parse_mode"] for call in send_message.await_args_list],
                ["HTML", "HTML", "HTML"],
            )

        asyncio.run(send())

    def test_custom_telegram_limit_keeps_scheduled_message_atomic(self):
        async def send():
            send_message = AsyncMock(
                return_value=SimpleNamespace(message_id=2)
            )

            await send_telegram_screen(
                send_message,
                42,
                "Заголовок\n" + "x" * 700,
                limit=4096,
            )

            send_message.assert_awaited_once()
            self.assertLessEqual(
                len(send_message.await_args.args[1].replace("<b>", "").replace("</b>", "")),
                4096,
            )

        asyncio.run(send())


class CleanPreviousMenuTest(unittest.TestCase):
    @staticmethod
    def api_error():
        return TelegramAPIError(
            method=SimpleNamespace(), message="Telegram API error"
        )

    def test_deletes_menu_when_delete_succeeds(self):
        async def run():
            message = SimpleNamespace(
                date=datetime.now(timezone.utc),
                message_id=10,
                delete=AsyncMock(),
                edit_text=AsyncMock(),
            )

            await clean_previous_menu(message, logging.getLogger(__name__))

            message.delete.assert_awaited_once()
            message.edit_text.assert_not_awaited()

        asyncio.run(run())

    def test_old_menu_uses_edit_fallback_without_delete_attempt(self):
        async def run():
            message = SimpleNamespace(
                date=datetime.now(timezone.utc) - timedelta(hours=49),
                message_id=11,
                delete=AsyncMock(),
                edit_text=AsyncMock(),
            )

            await clean_previous_menu(message, logging.getLogger(__name__))

            message.delete.assert_not_awaited()
            message.edit_text.assert_awaited_once()
            self.assertIsNone(
                message.edit_text.await_args.kwargs["reply_markup"]
            )

        asyncio.run(run())

    def test_failed_delete_falls_back_to_edit(self):
        async def run():
            message = SimpleNamespace(
                date=datetime.now(timezone.utc),
                message_id=12,
                delete=AsyncMock(side_effect=self.api_error()),
                edit_text=AsyncMock(),
            )

            await clean_previous_menu(message, logging.getLogger(__name__))

            message.delete.assert_awaited_once()
            message.edit_text.assert_awaited_once()

        asyncio.run(run())

    def test_delete_and_edit_failures_are_nonfatal(self):
        async def run():
            bot = SimpleNamespace(
                delete_message=AsyncMock(side_effect=self.api_error()),
                edit_message_text=AsyncMock(side_effect=self.api_error()),
            )

            await clean_previous_menu_by_id(
                bot, 42, 13, logging.getLogger(__name__)
            )

            bot.delete_message.assert_awaited_once_with(
                chat_id=42, message_id=13
            )
            bot.edit_message_text.assert_awaited_once()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
