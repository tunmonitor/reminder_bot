import asyncio
import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui import (
    callback_text,
    code,
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


if __name__ == "__main__":
    unittest.main()
