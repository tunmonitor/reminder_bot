import asyncio
import sqlite3
from contextlib import closing
import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from bot import readiness
from bot.excel_parser import RosterMember
from bot.service_registry import ServiceRegistry

TZ = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger("test-readiness")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def fake_bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        send_poll=AsyncMock(return_value=SimpleNamespace(
            message_id=12, poll=SimpleNamespace(id="poll-1"),
        )),
        stop_poll=AsyncMock(),
    )


class ReadinessDatesTest(unittest.TestCase):
    def test_launch_is_monday_of_last_full_week(self):
        self.assertEqual(readiness.launch_date(2026, 11), date(2026, 10, 19))
        self.assertEqual(readiness.launch_date(2027, 1), date(2026, 12, 21))
        self.assertEqual(
            readiness.month_sundays(2026, 11),
            [date(2026, 11, day) for day in (1, 8, 15, 22, 29)],
        )

    def test_schedule_and_manual_deadline(self):
        settings = ServiceRegistry.READINESS_DEFAULTS
        start, deadline = readiness.schedule(settings, 2026, 11, TZ)
        self.assertEqual(start, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        self.assertEqual(deadline, datetime(2026, 10, 22, 20, 0, tzinfo=TZ))
        late = datetime(2026, 10, 30, 21, 0, tzinfo=TZ)
        # Не позже конца месяца и не раньше чем через час.
        self.assertEqual(
            readiness.manual_deadline(late, settings),
            datetime(2026, 10, 31, 20, 0, tzinfo=TZ),
        )
        last = datetime(2026, 10, 31, 21, 0, tzinfo=TZ)
        self.assertEqual(
            readiness.manual_deadline(last, settings), last + timedelta(hours=1)
        )

    def test_answer_dates(self):
        dates = ["2026-11-01", "2026-11-08"]
        self.assertIsNone(readiness.answer_dates([], dates))
        self.assertEqual(readiness.answer_dates([2], dates), [])
        self.assertEqual(
            readiness.answer_dates([1, 0, 2], dates), ["2026-11-01", "2026-11-08"]
        )


class ReadinessFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = ServiceRegistry(
            Path(self.temp_dir.name) / "registry.sqlite3"
        )
        self.registry.initialize(1001, -10001)
        self.registry.set_group_chat(1001, "sound", -1005555, enabled=True)
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук", "сборка")),
            RosterMember("Борис", ("звук",)),
            RosterMember("Дмитрий", ("сборка",)),
        ]})
        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Алексей", 501, "alex"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Борис", 502
        )
        # Добавлен в боте, в «Списках» его нет.
        self.registry.set_responsible_contact(
            1001, "sound", "сборка", "Глеб", 503
        )
        self.registry.set_readiness_settings(1001, "sound", enabled=True)

    def service(self):
        return self.registry.get_service("sound")

    def test_people_merge_lists_and_bot_bindings(self):
        people = {
            person["name"]: person
            for person in self.registry.readiness_people("sound")
        }
        self.assertEqual(people["Алексей"]["tasks"], ["звук", "сборка"])
        self.assertEqual(people["Глеб"]["tasks"], ["сборка"])
        self.assertIsNone(people["Дмитрий"]["telegram_id"])

    def test_settings_validation(self):
        with self.assertRaises(PermissionError):
            self.registry.set_readiness_settings(9999, "sound", enabled=False)
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", start_time="25:00")
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", duration_days=7)
        settings = self.registry.set_readiness_settings(
            1001, "sound", duration_days=2, remind_days=3
        )
        self.assertEqual(settings["remind_days"], 1)

    def launch(self, bot):
        return asyncio.run(readiness.launch_run(
            bot, self.registry, self.service(), 2026, 11,
            datetime(2026, 10, 22, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))

    def test_launch_sends_intro_and_poll_once(self):
        bot = fake_bot()
        run = self.launch(bot)
        self.assertEqual(run["status"], "open")
        self.assertEqual(len(run["dates"]), 5)
        poll = bot.send_poll.await_args
        self.assertEqual(poll.args[1], "Когда можешь служить в ноябре?")
        self.assertEqual(
            poll.kwargs["options"][-1], readiness.CANT_OPTION
        )
        self.assertTrue(poll.kwargs["allows_multiple_answers"])
        intro = bot.send_message.await_args.args[1]
        self.assertIn("Спасибо за твое служение в церкви! ❤️", intro)
        with self.assertRaises(ValueError):
            self.launch(bot)

    def test_launch_without_chat_is_refused(self):
        self.registry.set_group_chat(1001, "sound", -1005555, enabled=False)
        with self.assertRaises(ValueError):
            self.launch(fake_bot())

    def test_answers_summary_and_outside_voter(self):
        run = self.launch(fake_bot())
        record = self.registry.record_readiness_answer
        record("poll-1", 501, "alex", "Алексей", ["2026-11-01", "2026-11-08"])
        record("poll-1", 503, "", "Глеб", ["2026-11-01"])
        record("poll-1", 777, "serg", "Сергей", ["2026-11-15"])
        record("poll-1", 502, "", "Борис", [])
        self.assertIsNone(record("other", 501, "", "", []))
        summary = readiness.summarize(self.registry, run)
        self.assertEqual(set(summary.answered), {"Алексей", "Глеб", "Борис"})
        self.assertEqual([item["full_name"] for item in summary.outside], ["Сергей"])
        self.assertEqual(
            [person["name"] for person in summary.not_answered], ["Дмитрий"]
        )
        self.assertEqual(summary.plan.slots[(date(2026, 11, 1), "звук")], ["Алексей"])
        self.assertEqual(summary.plan.slots[(date(2026, 11, 1), "сборка")], ["Глеб"])
        text = readiness.progress_text("ЗВУК", run, summary, TZ)
        self.assertIn("Ответили 3 из 4 · вне состава: 1", text)
        self.assertIn("Свободны:  01 — 2 · 08 — 1 · 15 — 0", text)
        self.assertIn("⚪ Вне состава: Сергей (15)", text)
        self.assertIn("Дмитрий (без Telegram)", text)
        # Отзыв голоса.
        record("poll-1", 503, "", "Глеб", None)
        self.assertNotIn(
            "Глеб", readiness.summarize(self.registry, run).answered
        )

    def test_tick_preview_launch_reminder_and_close(self):
        bot = fake_bot()

        def tick(moment):
            asyncio.run(readiness.run_readiness_tick(
                bot, self.registry, moment, TZ, 1001, LOG, 4096,
            ))

        tick(datetime(2026, 10, 18, 10, 30, tzinfo=TZ))
        bot.send_poll.assert_not_awaited()
        preview = bot.send_message.await_args.args
        self.assertEqual(preview[0], 1001)
        self.assertIn("опрос готовности", preview[1])
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 18, 10, 31, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

        tick(datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        bot.send_poll.assert_awaited_once()
        # Время создания — по часам теста, а не по реальным.
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE readiness_runs SET created_at = ?",
                (datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc).isoformat(),),
            )
            connection.commit()
        tick(datetime(2026, 10, 19, 10, 1, tzinfo=TZ))
        bot.send_poll.assert_awaited_once()

        self.registry.record_readiness_answer(
            "poll-1", 501, "alex", "Алексей", ["2026-11-01"]
        )
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 21, 20, 0, tzinfo=TZ))
        reminded = {call.args[0] for call in bot.send_message.await_args_list}
        self.assertEqual(reminded, {502, 503})
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 21, 20, 1, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

        tick(datetime(2026, 10, 22, 20, 0, tzinfo=TZ))
        bot.stop_poll.assert_awaited_once_with(-1005555, 12)
        result = bot.send_message.await_args.args
        self.assertEqual(result[0], 1001)
        self.assertIn("Опрос завершён", result[1])
        run = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual(run["status"], "closed")
        self.assertFalse(self.registry.record_readiness_answer(
            "poll-1", 502, "", "Борис", ["2026-11-01"]
        ))

    def test_late_launch_gets_no_reminder_and_failed_launch_is_not_retried(self):
        settings = self.registry.get_readiness_settings("sound")
        run = {
            "deadline": datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc)
            .isoformat(),
            "created_at": datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
            .isoformat(),
        }
        self.assertFalse(readiness.reminder_due(
            run, settings, datetime(2026, 10, 21, 22, 0, tzinfo=TZ)
        ))
        bot = fake_bot()
        bot.send_poll.side_effect = RuntimeError("no rights")
        for minute in range(3):
            asyncio.run(readiness.run_readiness_tick(
                bot, self.registry,
                datetime(2026, 10, 19, 10, minute, tzinfo=TZ), TZ, 1001, LOG,
                4096,
            ))
        bot.send_poll.assert_awaited_once()
        self.assertIn(
            "отправить не удалось", bot.send_message.await_args.args[1]
        )

    def test_member_without_tasks_counts_as_outside(self):
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук", "сборка")),
            RosterMember("Борис", ("звук",)),
            RosterMember("Дмитрий", ("сборка",)),
            RosterMember("Евгений", ()),
        ]})
        self.registry.bind_service_member(1001, "sound", "Евгений", 504, "")
        run = self.launch(fake_bot())
        self.registry.record_readiness_answer(
            "poll-1", 504, "", "Женя", ["2026-11-01"]
        )
        summary = readiness.summarize(self.registry, run)
        self.assertNotIn("Евгений", summary.answered)
        self.assertEqual(
            [item["full_name"] for item in summary.outside], ["Евгений"]
        )

    def test_skipped_month_is_not_launched(self):
        self.registry.create_readiness_run("sound", "2026-11", status="skipped")
        bot = fake_bot()
        asyncio.run(readiness.run_readiness_tick(
            bot, self.registry, datetime(2026, 10, 19, 10, 0, tzinfo=TZ), TZ,
            1001, LOG, 4096,
        ))
        bot.send_poll.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
