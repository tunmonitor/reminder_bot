import asyncio
import importlib
import logging
import os
import signal
import stat
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo


class ReminderBotEntrypointTest(unittest.TestCase):
    def test_shutdown_runs_once_when_signal_and_finally_race(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir()
                shutdown_handlers = {}
                loop = asyncio.get_running_loop()
                bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
                dispatcher = SimpleNamespace()
                scheduler = SimpleNamespace(start=MagicMock(), shutdown=MagicMock())

                async def start_polling(_bot):
                    shutdown_handlers[signal.SIGTERM]()
                    await asyncio.sleep(0)

                dispatcher.start_polling = start_polling
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(app, "setup_loggers", return_value=(MagicMock(), MagicMock()))
                    )
                    stack.enter_context(patch.object(
                        app,
                        "load_credentials",
                        return_value=SimpleNamespace(timezone="UTC", bot_token="token", group_chat_id=1),
                    ))
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(app, "Bot", return_value=bot))
                    stack.enter_context(patch.object(app, "Dispatcher", return_value=dispatcher))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(app, "build_scheduler", return_value=scheduler))
                    stack.enter_context(
                        patch.object(
                            loop,
                            "add_signal_handler",
                            side_effect=lambda sig, handler: shutdown_handlers.update({sig: handler}),
                        )
                    )
                    await app.run(SimpleNamespace(shm_dir=str(shm_dir), log_dir=str(log_dir)))

                scheduler.shutdown.assert_called_once_with(wait=False)
                bot.session.close.assert_awaited_once()
                self.assertFalse((shm_dir / "reminder_bot_bot.pid").exists())

        asyncio.run(run_bot())

    def test_write_pid_creates_private_file(self):
        from bot.app import _current_process_starttime, _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_path = Path(tmp_dir) / "bot.pid"
            _write_pid(pid_path)

            pid, starttime = pid_path.read_text(encoding="utf-8").split(":")
            self.assertEqual(pid, str(os.getpid()))
            self.assertTrue(starttime.isdigit())
            self.assertEqual(starttime, _current_process_starttime())
            self.assertEqual(stat.S_IMODE(pid_path.stat().st_mode), 0o600)

    def test_write_pid_does_not_follow_symlink(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            pid_path = Path(tmp_dir) / "bot.pid"
            pid_path.symlink_to(target_path)

            with self.assertRaises(FileExistsError):
                _write_pid(pid_path)

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_write_pid_does_not_overwrite_existing_hardlink(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            pid_path = Path(tmp_dir) / "bot.pid"
            os.link(target_path, pid_path)

            with self.assertRaises(FileExistsError):
                _write_pid(pid_path)

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_write_pid_rejects_group_writable_directory(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chmod(tmp_dir, 0o770)
            pid_path = Path(tmp_dir) / "bot.pid"

            with self.assertRaises(PermissionError):
                _write_pid(pid_path)

            self.assertFalse(pid_path.exists())

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

        handlers.register(dp, context)

        dp.include_router.assert_called_once_with(handlers.router)

    def test_register_requires_dispatcher(self):
        from bot import handlers

        context = SimpleNamespace(
            interaction_logger=logging.getLogger("interaction"),
            debug_logger=logging.getLogger("debug"),
            state_cache=SimpleNamespace(state={}),
        )

        with self.assertRaises(TypeError):
            handlers.register(context)

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
