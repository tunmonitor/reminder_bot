import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.handlers import register, router


class ConfirmationHandlerTest(unittest.TestCase):
    def test_failed_group_update_can_be_retried_on_duplicate_callback(self):
        async def run():
            participant = {
                "name": "<Алексей>",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[RuntimeError("temporary"), object()]
                    )
                ),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await handler(callback)
            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])

            await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(context.bot.send_message.await_count, 2)
            self.assertEqual(
                context.bot.send_message.await_args.kwargs,
                {"parse_mode": None},
            )
            self.assertIn(
                "Нажмите кнопку повторно",
                callback.answer.await_args_list[0].args[0],
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
