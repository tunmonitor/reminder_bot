import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.notifier import (
    _notify_admin,
    build_general_message,
    build_personal_message,
    send_assignments,
)


class PartialNotificationTest(unittest.TestCase):
    def test_admin_warning_uses_plain_text_parse_mode(self):
        async def notify():
            bot = SimpleNamespace(send_message=AsyncMock())

            await _notify_admin(
                bot,
                12345,
                "Ошибка: <untrusted diagnostic>",
                MagicMock(),
            )

            bot.send_message.assert_awaited_once_with(
                12345,
                "⚠️ Ошибка: <untrusted diagnostic>",
                parse_mode=None,
            )

        asyncio.run(notify())

    def test_dynamic_html_fields_are_escaped(self):
        templates = {
            "general": "Ответственный: @{tg_responsible}",
            "roles": {"Ответственный": "{name}"},
        }

        group_text = build_general_message(
            {"Ответственный": {"username": "<b>alex</b>"}},
            templates,
            "11.10.2026",
        )
        personal_text = build_personal_message(
            "Ответственный",
            {"name": "<b>Алексей</b>"},
            templates,
            "11.10.2026",
        )

        self.assertEqual(group_text, "Ответственный: @&lt;b&gt;alex&lt;/b&gt;")
        self.assertEqual(personal_text, "&lt;b&gt;Алексей&lt;/b&gt;")

    def test_sends_group_and_available_participant_with_missing_roles(self):
        async def send():
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                    ]
                )
            )
            assignments = {
                "Ответственный": {
                    "name": "Алексей",
                    "username": "alex",
                    "tg_id": 12345,
                }
            }
            state = {
                "participants": {
                    "Ответственный": {
                        "message_id": None,
                        "chat_id": None,
                    }
                }
            }
            debug_logger = MagicMock()
            templates = {
                "general": (
                    "Дата {sunday_date}: "
                    "ответственный @{tg_responsible}; "
                    "ведущий @{tg_leader}; проповедник @{tg_preacher}"
                ),
                "roles": {
                    "Ответственный": "{name}, {sunday_date}",
                },
            }

            with patch("bot.notifier.asyncio.sleep", new=AsyncMock()):
                await send_assignments(
                    bot=bot,
                    group_chat_id=-100,
                    admin_id=67890,
                    assignments=assignments,
                    state=state,
                    templates=templates,
                    sunday_str="11.10.2026",
                    sunday_iso="2026-10-11",
                    interaction_logger=MagicMock(),
                    debug_logger=debug_logger,
                )

            first_call = bot.send_message.await_args_list[0]
            group_text = first_call.args[1]
            self.assertIn("@alex", group_text)
            self.assertIn("ведущий не найден", group_text)
            self.assertEqual(len(bot.send_message.await_args_list), 2)
            self.assertEqual(
                bot.send_message.await_args_list[1].args,
                (12345, "Алексей, 11.10.2026"),
            )
            self.assertEqual(state["participants"]["Ответственный"]["message_id"], 11)
            debug_messages = [
                call.args[0] for call in debug_logger.debug.call_args_list
            ]
            self.assertIn("Подготовлено сообщение в групповой чат:\n%s", debug_messages)
            self.assertIn("Подготовлено личное сообщение: роль=%s, tg_id=%s, username=@%s\n%s", debug_messages)

        asyncio.run(send())

    def test_failed_personal_delivery_retries_without_resending_successes(self):
        from aiogram.exceptions import TelegramAPIError

        async def send():
            assignments = {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                },
                "Ведущий": {
                    "name": "Павел", "username": "pavel", "tg_id": 23456,
                },
            }
            state = {
                "general_sent": False,
                "general_message_id": None,
                "general_text": None,
                "participants": {
                    role: {"tg_id": info["tg_id"], "delivered": False}
                    for role, info in assignments.items()
                },
            }
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                        TelegramAPIError(method=MagicMock(), message="temporary"),
                        SimpleNamespace(message_id=12, chat=SimpleNamespace(id=23456)),
                    ]
                ),
                edit_message_text=AsyncMock(),
            )
            persist = MagicMock()
            templates = {
                "general": "Ответственный: @{tg_responsible}; ведущий: @{tg_leader}; "
                "проповедник: @{tg_preacher}",
                "roles": {
                    "Ответственный": "{name}",
                    "Ведущий": "{name}",
                },
            }
            common = dict(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignments,
                state=state,
                templates=templates,
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                save_state=persist,
            )

            with patch("bot.notifier._notify_admin", new=AsyncMock()), patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                await send_assignments(**common)
                self.assertTrue(state["participants"]["Ответственный"]["delivered"])
                self.assertFalse(state["participants"]["Ведущий"]["delivered"])

                await send_assignments(**common)

            self.assertEqual(bot.send_message.await_count, 4)
            self.assertEqual(
                bot.send_message.await_args_list[3].args,
                (23456, "Павел"),
            )
            bot.edit_message_text.assert_not_awaited()
            self.assertTrue(state["participants"]["Ведущий"]["delivered"])
            self.assertGreaterEqual(persist.call_count, 3)

        asyncio.run(send())

    def test_group_fallback_is_not_rendered_as_a_fake_mention(self):
        message = build_general_message(
            {"Ответственный": {"username": ""}},
            {"general": "Ответственный: @{tg_responsible}"},
            "11.10.2026",
        )

        self.assertEqual(message, "Ответственный: не найден")

    def test_group_failure_is_retried_before_private_deliveries(self):
        from aiogram.exceptions import TelegramAPIError

        async def send():
            assignments = {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                }
            }
            state = {
                "general_sent": False,
                "participants": {
                    "Ответственный": {"tg_id": 12345, "delivered": False}
                },
            }
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        TelegramAPIError(method=MagicMock(), message="temporary"),
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                    ]
                )
            )
            common = dict(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignments,
                state=state,
                templates={
                    "general": "Ответственный: @{tg_responsible}",
                    "roles": {"Ответственный": "{name}"},
                },
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
            )
            with patch("bot.notifier._notify_admin", new=AsyncMock()), patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                with self.assertRaises(TelegramAPIError):
                    await send_assignments(**common)
                self.assertFalse(state["general_sent"])
                self.assertFalse(state["participants"]["Ответственный"]["delivered"])

                await send_assignments(**common)

            self.assertEqual(bot.send_message.await_count, 3)
            self.assertEqual(bot.send_message.await_args_list[1].args[0], -100)
            self.assertEqual(bot.send_message.await_args_list[2].args[0], 12345)
            self.assertTrue(state["general_sent"])
            self.assertTrue(state["participants"]["Ответственный"]["delivered"])

        asyncio.run(send())


if __name__ == "__main__":
    unittest.main()
