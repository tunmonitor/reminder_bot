import asyncio
import importlib
import logging
import os
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
    def test_scheduler_initialization_failure_closes_bot_without_pid(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app,
                            "setup_loggers",
                            return_value=(MagicMock(), MagicMock()),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "load_credentials",
                            return_value=SimpleNamespace(
                                timezone="UTC", bot_token="token", group_chat_id=1
                            ),
                        )
                    )
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(app, "Bot", return_value=bot))
                    stack.enter_context(patch.object(app, "Dispatcher"))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(
                        patch.object(
                            app,
                            "build_scheduler",
                            side_effect=RuntimeError("invalid scheduler"),
                        )
                    )
                    with self.assertRaisesRegex(RuntimeError, "invalid scheduler"):
                        await app.run(
                            SimpleNamespace(
                                shm_dir=str(shm_dir), log_dir=str(log_dir)
                            )
                        )

                bot.session.close.assert_awaited_once()
                self.assertFalse((shm_dir / "reminder_bot_bot.pid").exists())

    def test_corrected_schedule_retries_only_new_deliveries(self):
        from bot import app
        from bot.state import StateCache

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                cache = StateCache(Path(tmp_dir) / "state.json")
                bot = SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[
                            SimpleNamespace(message_id=10),
                            SimpleNamespace(message_id=11, chat=SimpleNamespace(id=101)),
                            SimpleNamespace(message_id=12, chat=SimpleNamespace(id=102)),
                            SimpleNamespace(message_id=13, chat=SimpleNamespace(id=103)),
                        ]
                    ),
                    edit_message_text=AsyncMock(),
                )
                credentials = SimpleNamespace(
                    admin_tg_id=12345,
                    group_chat_id=-100,
                    yandex_public_url="https://example.invalid/schedule",
                )
                partial = {"Ответственный": "Алексей"}
                complete = {
                    "Ответственный": "Алексей",
                    "Ведущий": "Павел",
                    "Проповедник": "Иван",
                }
                contacts = {
                    "Ответственный": {
                        "Алексей": {"username": "alex", "tg_id": 101}
                    },
                    "Ведущий": {
                        "Павел": {"username": "pavel", "tg_id": 102}
                    },
                    "Проповедник": {
                        "Иван": {"username": "ivan", "tg_id": 103}
                    },
                }
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app, "_next_sunday_in_tz",
                            return_value=date(2026, 10, 11),
                        )
                    )
                    stack.enter_context(
                        patch.object(app, "download_public", new=AsyncMock())
                    )
                    parse_schedule = stack.enter_context(
                        patch.object(
                            app, "parse_schedule", side_effect=[partial, complete]
                        )
                    )
                    stack.enter_context(
                        patch.object(app, "parse_people", return_value=contacts)
                    )
                    stack.enter_context(
                        patch.object(app, "_notify_admin", new=AsyncMock())
                    )
                    stack.enter_context(
                        patch("bot.notifier.asyncio.sleep", new=AsyncMock())
                    )
                    for _ in range(3):
                        await app._process_upcoming_sunday(
                            bot=bot,
                            credentials=credentials,
                            templates={
                                "general": (
                                    "Ответственный: @{tg_responsible}; "
                                    "Ведущий: @{tg_leader}; "
                                    "Проповедник: @{tg_preacher}"
                                ),
                                "roles": {
                                    role: "{name}"
                                    for role in complete
                                },
                            },
                            state_cache=cache,
                            shm_dir=Path(tmp_dir),
                            interaction_logger=MagicMock(),
                            debug_logger=MagicMock(),
                            tz=ZoneInfo("UTC"),
                        )

                self.assertEqual(parse_schedule.call_count, 2)
                self.assertEqual(bot.send_message.await_count, 4)
                bot.edit_message_text.assert_awaited_once()
                self.assertTrue(cache.state["complete"])
                self.assertTrue(cache.matches_week("2026-10-11"))
                self.assertEqual(
                    cache.state["participants"]["Ответственный"]["message_id"],
                    11,
                )

        asyncio.run(process())

    def test_missing_assignment_data_notifies_admin_and_continues_partial_send(self):
        from bot import app

        async def process():
            bot = SimpleNamespace()
            credentials = SimpleNamespace(
                admin_tg_id=12345,
                yandex_public_url="https://example.invalid/schedule",
            )
            state_cache = SimpleNamespace(
                matches_week=MagicMock(return_value=False),
                state=None,
                replace=MagicMock(),
                save=MagicMock(),
            )
            debug_logger = MagicMock()

            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(app, "_next_sunday_in_tz", return_value=date(2026, 10, 11))
                )
                stack.enter_context(patch.object(app, "download_public", new=AsyncMock()))
                stack.enter_context(
                    patch.object(
                        app,
                        "parse_schedule",
                        return_value={
                            "Ответственный": "Найденный",
                            "Ведущий": "Без ID",
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        app,
                        "parse_people",
                        return_value={
                            "Ответственный": {
                                "Найденный": {"username": "found", "tg_id": 98765},
                            },
                            "Ведущий": {
                                "Без ID": {"username": "", "tg_id": None},
                            },
                        },
                    )
                )
                stack.enter_context(patch.object(app, "_notify_admin", new=AsyncMock()))
                stack.enter_context(patch.object(app, "send_assignments", new=AsyncMock()))

                await app._process_upcoming_sunday(
                    bot=bot,
                    credentials=credentials,
                    templates={},
                    state_cache=state_cache,
                    shm_dir=Path("/tmp"),
                    interaction_logger=MagicMock(),
                    debug_logger=debug_logger,
                    tz=ZoneInfo("UTC"),
                )

                app._notify_admin.assert_awaited_once()
                args = app._notify_admin.await_args.args
                self.assertIs(args[0], bot)
                self.assertEqual(args[1], credentials.admin_tg_id)
                self.assertIn("11.10.2026", args[2])
                self.assertIn("отсутствует Telegram_ID", args[2])
                app.send_assignments.assert_awaited_once()
                sent_assignments = app.send_assignments.await_args.kwargs["assignments"]
                self.assertEqual(
                    list(sent_assignments), ["Ответственный", "Ведущий"]
                )
                self.assertIsNone(sent_assignments["Ведущий"]["tg_id"])

    def test_existing_pid_file_stops_startup_with_clear_error(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                pid_file = shm_dir / "reminder_bot_bot.pid"
                from bot.app import _current_process_starttime
                pid_file.write_text(
                    f"{os.getpid()}:{_current_process_starttime()}",
                    encoding="utf-8",
                )
                debug_logger = MagicMock()

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app,
                            "setup_loggers",
                            return_value=(MagicMock(), debug_logger),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "load_credentials",
                            return_value=SimpleNamespace(
                                timezone="UTC",
                                bot_token="token",
                                group_chat_id=1,
                                admin_tg_id=2,
                            ),
                        )
                    )
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(
                        app,
                        "Bot",
                        return_value=SimpleNamespace(
                            session=SimpleNamespace(close=AsyncMock())
                        ),
                    ))
                    stack.enter_context(patch.object(app, "Dispatcher"))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(
                        app,
                        "build_scheduler",
                        return_value=SimpleNamespace(running=False),
                    ))
                    with self.assertRaisesRegex(FileExistsError, "PID-файл уже существует"):
                        await app.run(
                            SimpleNamespace(shm_dir=str(shm_dir), log_dir=str(log_dir))
                        )

                debug_logger.error.assert_called_once()
                self.assertIn("PID-файл уже существует", debug_logger.error.call_args.args[0])
                self.assertEqual(
                    pid_file.read_text(encoding="utf-8"),
                    f"{os.getpid()}:{_current_process_starttime()}",
                )

        asyncio.run(run_bot())

    def test_existing_pid_file_status_detects_stale_process(self):
        from bot.app import _existing_pid_process_status

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "bot.pid"
            pid_file.write_text("999999999:1", encoding="utf-8")
            os.chmod(pid_file, 0o600)

            self.assertEqual(
                _existing_pid_process_status(pid_file),
                ("999999999", False),
            )

    def test_existing_pid_file_status_detects_live_process(self):
        from bot.app import _current_process_starttime, _existing_pid_process_status

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "bot.pid"
            pid_file.write_text(
                f"{os.getpid()}:{_current_process_starttime()}",
                encoding="utf-8",
            )
            os.chmod(pid_file, 0o600)

            self.assertEqual(
                _existing_pid_process_status(pid_file),
                (str(os.getpid()), True),
            )

    def test_shutdown_runs_once_after_polling_stops(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
                dispatcher = SimpleNamespace()
                scheduler = SimpleNamespace(
                    running=True,
                    start=MagicMock(),
                    pause=MagicMock(),
                    shutdown=MagicMock(),
                )

                async def start_polling(_bot, **_kwargs):
                    return None

                dispatcher.start_polling = AsyncMock(side_effect=start_polling)
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(app, "setup_loggers", return_value=(MagicMock(), MagicMock()))
                    )
                    stack.enter_context(patch.object(
                        app,
                        "load_credentials",
                        return_value=SimpleNamespace(
                            timezone="UTC",
                            bot_token="token",
                            group_chat_id=1,
                            admin_tg_id=2,
                        ),
                    ))
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(app, "Bot", return_value=bot))
                    stack.enter_context(patch.object(app, "Dispatcher", return_value=dispatcher))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(app, "build_scheduler", return_value=scheduler))
                    await app.run(SimpleNamespace(shm_dir=str(shm_dir), log_dir=str(log_dir)))

                scheduler.pause.assert_called_once_with()
                scheduler.shutdown.assert_called_once_with(wait=False)
                bot.session.close.assert_awaited_once()
                dispatcher.start_polling.assert_awaited_once_with(
                    bot, close_bot_session=False
                )
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

    def test_write_pid_does_not_follow_symlinked_ancestor(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir) / "target"
            target_dir.mkdir()
            (target_dir / "nested").mkdir()
            linked_dir = Path(tmp_dir) / "linked"
            linked_dir.symlink_to(target_dir, target_is_directory=True)

            with self.assertRaises(OSError):
                _write_pid(linked_dir / "nested" / "bot.pid")

            self.assertFalse((target_dir / "nested" / "bot.pid").exists())

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

    def test_write_pid_does_not_overwrite_existing_file(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_path = Path(tmp_dir) / "bot.pid"
            pid_path.write_text("stale-pid", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                _write_pid(pid_path)

            self.assertEqual(pid_path.read_text(encoding="utf-8"), "stale-pid")

    def test_write_pid_rejects_group_writable_directory(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chmod(tmp_dir, 0o770)
            pid_path = Path(tmp_dir) / "bot.pid"

            with self.assertRaises(PermissionError):
                _write_pid(pid_path)

            self.assertFalse(pid_path.exists())

    def test_run_rejects_symlinked_runtime_directory(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                target = root / "target"
                target.mkdir(mode=0o700)
                linked = root / "linked"
                linked.symlink_to(target, target_is_directory=True)

                with self.assertRaises(OSError):
                    await app.run(
                        SimpleNamespace(
                            shm_dir=str(linked),
                            log_dir=str(root / "logs"),
                        )
                    )

                self.assertFalse((target / "reminder_bot_bot.pid").exists())

        asyncio.run(run_bot())

    def test_log_files_are_private(self):
        from bot.logger import setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            interaction_logger, debug_logger = setup_loggers(Path(tmp_dir))

            for log_name in ("reminder_bot_interaction.log", "reminder_bot_debug.log"):
                self.assertEqual(
                    stat.S_IMODE((Path(tmp_dir) / log_name).stat().st_mode),
                    0o600,
                )

            for logger in (interaction_logger, debug_logger):
                for handler in logger.handlers:
                    handler.close()

    def test_assignment_details_are_written_only_to_debug_log(self):
        from bot.logger import setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            interaction_logger, debug_logger = setup_loggers(Path(tmp_dir))
            try:
                debug_logger.debug(
                    "Назначение сопоставлено: роль=%s, имя=%s, username=@%s, tg_id=%s",
                    "Ответственный", "Участник", "user", 123456789,
                )
                for handler in debug_logger.handlers:
                    handler.flush()

                interaction_log = (
                    Path(tmp_dir) / "reminder_bot_interaction.log"
                ).read_text(encoding="utf-8")
                debug_log = (
                    Path(tmp_dir) / "reminder_bot_debug.log"
                ).read_text(encoding="utf-8")

                self.assertNotIn("123456789", interaction_log)
                self.assertIn("123456789", debug_log)
            finally:
                for logger in (interaction_logger, debug_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_logger_does_not_follow_symlink(self):
        from bot.logger import INTERACTION_LOG, setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            (Path(tmp_dir) / INTERACTION_LOG).symlink_to(target_path)

            with self.assertRaises(OSError):
                setup_loggers(Path(tmp_dir))

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_logger_rejects_hardlinked_log_file(self):
        from bot.logger import INTERACTION_LOG, setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            os.link(target_path, Path(tmp_dir) / INTERACTION_LOG)

            with self.assertRaises(PermissionError):
                setup_loggers(Path(tmp_dir))

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

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
