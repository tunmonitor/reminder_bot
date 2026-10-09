import importlib
import logging
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


class ReminderBotEntrypointTest(unittest.TestCase):
    def test_reminder_bot_entrypoint_imports(self):
        module = importlib.import_module("bot.reminder_bot")
        self.assertTrue(hasattr(module, "main"))

    def test_register_includes_router_in_dispatcher(self):
        from bot import handlers

        dp = MagicMock()
        context = SimpleNamespace(
            dispatcher=dp,
            interaction_logger=logging.getLogger("interaction"),
            debug_logger=logging.getLogger("debug"),
            state_cache=SimpleNamespace(state={}),
        )

        handlers.register(context)

        dp.include_router.assert_called_once_with(handlers.router)

    def test_next_sunday_in_tz_skips_current_sunday(self):
        from bot.app import _next_sunday_in_tz

        tz = ZoneInfo("Europe/Moscow")

        with patch("bot.app.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 11, 12, 0, 0, tzinfo=tz)
            self.assertEqual(_next_sunday_in_tz(tz), date(2026, 10, 18))

        with patch("bot.app.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 12, 12, 0, 0, tzinfo=tz)
            self.assertEqual(_next_sunday_in_tz(tz), date(2026, 10, 18))


if __name__ == "__main__":
    unittest.main()
