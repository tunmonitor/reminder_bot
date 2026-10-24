import asyncio
import sqlite3
from contextlib import closing
import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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
        with self.assertRaises(ValueError):
            readiness.manual_deadline(
                datetime(2026, 10, 31, 23, 30, tzinfo=TZ), settings
            )

    def test_reminder_line(self):
        run = {
            "deadline": datetime(2026, 10, 22, 20, 0, tzinfo=TZ).isoformat(),
            "created_at": datetime(2026, 10, 19, 10, 0, tzinfo=TZ).isoformat(),
        }
        settings = {"remind_days": 1}
        before = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
        self.assertEqual(
            readiness.reminder_line(run, settings, before, TZ),
            "🔔 Напоминание: 21.10 20:00 — лично не ответившим и общее в "
            "чат служения",
        )
        after = datetime(2026, 10, 21, 21, 0, tzinfo=TZ)
        self.assertIn("отправлено 21.10 20:00",
                      readiness.reminder_line(run, settings, after, TZ))
        self.assertIn("выключено", readiness.reminder_line(
            run, {"remind_days": 0}, before, TZ
        ))
        self.assertIn("не отправлено", readiness.reminder_line(
            run, settings, after, TZ, sent=False
        ))
        late = {**run, "created_at": after.isoformat()}
        self.assertIn("не будет", readiness.reminder_line(
            late, settings, after, TZ
        ))

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
        # Лично не ответившим и общее — в чат служения, ответом на опрос.
        self.assertEqual(reminded, {502, 503, -1005555})
        chat = next(
            call for call in bot.send_message.await_args_list
            if call.args[0] == -1005555
        )
        self.assertIn("Кто ещё не отметил воскресенья", chat.args[1])
        self.assertNotIn("Борис", chat.args[1])
        self.assertEqual(chat.kwargs["reply_parameters"].message_id, 12)
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 21, 20, 1, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

        tick(datetime(2026, 10, 22, 20, 0, tzinfo=TZ))
        bot.stop_poll.assert_awaited_once_with(-1005555, 12)
        announced = [
            call for call in bot.send_message.await_args_list
            if call.args[0] == -1005555
        ]
        self.assertIn("завершён. Спасибо всем", announced[-1].args[1])
        self.assertEqual(
            announced[-1].kwargs["reply_parameters"].message_id, 12
        )
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

    def set_enabled_at(self, moment):
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE readiness_settings SET enabled_at = ?",
                (moment.astimezone(timezone.utc).isoformat(),),
            )
            connection.commit()

    def tick(self, bot, moment):
        asyncio.run(readiness.run_readiness_tick(
            bot, self.registry, moment, TZ, 1001, LOG, 4096,
        ))

    def test_missed_window_alert_only_when_bot_was_down(self):
        bot = fake_bot()
        # Автозапуск включён после времени запуска — не пропуск.
        self.set_enabled_at(datetime(2026, 10, 25, 12, 0, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 25, 12, 5, tzinfo=TZ))
        bot.send_message.assert_not_awaited()
        # Был включён заранее, бот лежал всё окно — одно предупреждение.
        self.set_enabled_at(datetime(2026, 10, 1, 12, 0, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 25, 12, 5, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 25, 12, 6, tzinfo=TZ))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn("не был запущен вовремя", bot.send_message.await_args.args[1])
        bot.send_poll.assert_not_awaited()

    def test_missed_alert_survives_month_rollover(self):
        bot = fake_bot()
        self.set_enabled_at(datetime(2026, 10, 1, 12, 0, tzinfo=TZ))
        # Бот поднялся уже 1 ноября: ноябрьский опрос пропущен.
        self.tick(bot, datetime(2026, 11, 1, 9, 0, tzinfo=TZ))
        texts = [call.args[1] for call in bot.send_message.await_args_list]
        self.assertTrue(any("ноябрь 2026" in text for text in texts))

    def test_no_missed_alert_after_failed_launch(self):
        bot = fake_bot()
        self.set_enabled_at(datetime(2026, 10, 1, 12, 0, tzinfo=TZ))
        bot.send_poll.side_effect = RuntimeError("no rights")
        self.tick(bot, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        bot.send_message.reset_mock()
        self.tick(bot, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

    def test_reminder_retried_after_transient_failure(self):
        bot = fake_bot()
        self.tick(bot, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE readiness_runs SET created_at = ?",
                (datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc).isoformat(),),
            )
            connection.commit()
        bot.send_message.reset_mock()
        bot.send_message.side_effect = [
            RuntimeError("network"), SimpleNamespace(message_id=1),
            SimpleNamespace(message_id=2), RuntimeError("network"),
        ]
        self.tick(bot, datetime(2026, 10, 21, 20, 0, tzinfo=TZ))
        bot.send_message.side_effect = None
        self.tick(bot, datetime(2026, 10, 21, 20, 1, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 21, 20, 2, tzinfo=TZ))
        recipients = [call.args[0] for call in bot.send_message.await_args_list]
        # 501 и чат — сбой и повтор; доставленным второй раз не пишем.
        self.assertEqual(
            sorted(recipients), [-1005555, -1005555, 501, 501, 502, 503]
        )

    def test_no_chat_reminder_when_everyone_answered(self):
        bot = fake_bot()
        run = self.launch(bot)
        for telegram_id in (501, 502, 503):
            self.registry.record_readiness_answer(
                "poll-1", telegram_id, "", "", ["2026-11-01"]
            )
        bot.send_message.reset_mock()
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        bot.send_message.assert_not_awaited()

    def test_unreachable_chat_reminder_is_not_reported_sent(self):
        from aiogram.exceptions import TelegramForbiddenError

        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.side_effect = TelegramForbiddenError(
            method=SimpleNamespace(), message="bot was kicked"
        )
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        self.assertFalse(readiness.reminder_sent(self.registry, run))
        bot.send_message.side_effect = None
        bot.send_message.reset_mock()
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        bot.send_message.assert_not_awaited()

    def test_close_retries_stop_and_result(self):
        bot = fake_bot()
        run = self.launch(bot)
        bot.stop_poll.side_effect = RuntimeError("network")
        bot.send_message.side_effect = RuntimeError("network")
        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertEqual(
            [item["id"] for item in self.registry.list_unfinished_readiness_runs()],
            [run["id"]],
        )
        bot.stop_poll.side_effect = None
        bot.send_message.side_effect = None
        self.tick(bot, datetime.now(TZ))
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])
        self.assertIn("Опрос завершён", bot.send_message.await_args.args[1])

    def test_unreachable_admin_is_not_retried(self):
        from aiogram.exceptions import TelegramBadRequest

        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.side_effect = TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: chat not found"
        )
        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])

    def test_result_retried_only_for_failed_admin(self):
        self.registry.add_service_admin(1001, "sound", 2002)
        self.registry.add_service_admin(1001, "sound", 2003)
        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.reset_mock()

        def send(chat_id, *args, **kwargs):
            if chat_id == 2003 and send.fail:
                raise RuntimeError("network")
            return SimpleNamespace(message_id=1)

        send.fail = True
        bot.send_message.side_effect = send
        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertEqual(len(self.registry.list_unfinished_readiness_runs()), 1)
        send.fail = False
        bot.send_message.reset_mock()
        self.tick(bot, datetime.now(TZ))
        # Повтор — только тому, кому не дошло.
        self.assertEqual(
            [call.args[0] for call in bot.send_message.await_args_list], [2003]
        )
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])

    def test_other_bad_request_is_retried(self):
        from aiogram.exceptions import TelegramBadRequest

        self.assertTrue(readiness.unreachable(TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: chat not found"
        )))
        self.assertFalse(readiness.unreachable(TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: can't parse entities"
        )))

    def test_parked_run_is_settled(self):
        run = self.launch(fake_bot())
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        self.assertEqual(self.registry.list_open_readiness_runs(), [])
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 0, tzinfo=TZ))
        restored = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual(restored["id"], run["id"])
        self.assertEqual(restored["status"], "closed")
        # Пропуск месяца не стирает ответы отложенного опроса.
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        self.registry.create_readiness_run("sound", "2026-11", status="skipped")
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 1, tzinfo=TZ))
        self.assertEqual(
            self.registry.get_readiness_run("sound", "2026-11")["id"], run["id"]
        )
        # Запись нового опроса без опроса в чате — не новый опрос.
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        orphan = self.registry.create_readiness_run(
            "sound", "2026-11", status="open"
        )
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 2, tzinfo=TZ))
        self.assertEqual(
            self.registry.get_readiness_run("sound", "2026-11")["id"], run["id"]
        )
        self.assertIsNone(self.registry.get_readiness_run_by_id(orphan["id"]))
        # Новый опрос уже есть — отложенный удаляется.
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        new = self.registry.create_readiness_run(
            "sound", "2026-11", status="open"
        )
        self.registry.attach_readiness_poll(
            new["id"], poll_id="p2", chat_id=-1005555, message_id=20,
            intro_message_id=None,
        )
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 3, tzinfo=TZ))
        self.assertIsNone(self.registry.get_readiness_run_by_id(run["id"]))

    def test_add_member_does_not_take_over_bound_name(self):
        # «Глеб» привязан через «👥 Ответственные» к 503.
        with self.assertRaises(ValueError):
            self.registry.bind_service_member(
                1001, "sound", "Глеб", 777, tasks=["звук"], from_readiness=True
            )
        self.assertEqual(
            {
                person["telegram_id"]
                for person in self.registry.readiness_people("sound")
                if person["name"] == "Глеб"
            },
            {503},
        )

    def test_reserved_run_without_poll_is_cleaned(self):
        orphan = self.registry.create_readiness_run(
            "sound", "2026-11", status="open",
            deadline=datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc).isoformat(),
        )
        created = datetime.fromisoformat(orphan["created_at"])
        bot = fake_bot()
        self.tick(bot, created.astimezone(TZ) + timedelta(minutes=1))
        self.assertIsNotNone(self.registry.get_readiness_run_by_id(orphan["id"]))
        self.tick(bot, created.astimezone(TZ) + timedelta(minutes=6))
        self.assertIsNone(self.registry.get_readiness_run_by_id(orphan["id"]))
        bot.send_message.assert_not_awaited()

    def test_early_answer_is_replayed_after_attach(self):
        # Вне отправки опроса ответы на неизвестный опрос не придерживаются.
        self.assertFalse(
            readiness.hold_early_answer("other", 501, "", "", [0])
        )
        bot = fake_bot()

        async def send_poll(*args, **kwargs):
            # Буфер полон чужим ответом — вытесняется он, а не новый.
            readiness.hold_early_answer("foreign", 9, "", "", [0])
            # Голос пришёл раньше, чем бот записал ID опроса.
            self.assertTrue(readiness.hold_early_answer(
                "poll-1", 501, "alex", "Алексей", [0, 1]
            ))
            return SimpleNamespace(
                message_id=12, poll=SimpleNamespace(id="poll-1")
            )

        bot.send_poll = AsyncMock(side_effect=send_poll)
        with patch.object(readiness, "_EARLY_LIMIT", 1):
            run = self.launch(bot)
        answers = self.registry.list_readiness_answers(run["id"])
        self.assertEqual(answers[0]["dates"], run["dates"][:2])

    def test_lost_poll_response_keeps_month_and_asks_to_check(self):
        from aiogram.exceptions import TelegramNetworkError

        bot = fake_bot()
        bot.send_poll.side_effect = TelegramNetworkError(
            method=SimpleNamespace(), message="timeout"
        )
        with self.assertRaises(ValueError) as caught:
            self.launch(bot)
        self.assertIn("проверьте", str(caught.exception))
        run = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual(run["status"], "closed")
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])

    def test_unattached_poll_that_cannot_be_stopped_is_kept(self):
        bot = fake_bot()
        bot.stop_poll.side_effect = RuntimeError("network")
        original = self.registry.attach_readiness_poll
        calls = []

        def attach(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk")
            return original(*args, **kwargs)

        self.registry.attach_readiness_poll = attach
        with self.assertRaises(RuntimeError):
            self.launch(bot)
        run = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual((run["status"], run["poll_id"]), ("closed", "poll-1"))
        # Ежеминутная проверка ещё раз попробует остановить опрос.
        self.assertEqual(
            [item["id"] for item in self.registry.list_unfinished_readiness_runs()],
            [run["id"]],
        )

    def test_same_names_stay_distinct(self):
        self.registry.set_responsible_contact(
            1001, "sound", "сборка", "Глеб", 503
        )
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "INSERT INTO responsible_contacts (service_id, task, name, "
                "telegram_id, username, updated_by, updated_at) "
                "VALUES ('sound', 'звук', 'Глеб', 599, '', 1001, 'x')"
            )
            connection.commit()
        people = [
            person for person in self.registry.readiness_people("sound")
            if person["name"] == "Глеб"
        ]
        self.assertEqual(len(people), 2)
        self.assertEqual(len({person["key"] for person in people}), 2)

    def test_custom_intro_and_reminder_templates(self):
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", intro_template="{name}"
            )
        self.registry.set_readiness_settings(
            1001, "sound",
            intro_template="Опрос {service_name} · {month} до {deadline}\n👥 {people}",
            reminder_template="Напоминаю: {month}, до {deadline}",
        )
        bot = fake_bot()
        run = self.launch(bot)
        intro = bot.send_message.await_args.args[1]
        self.assertIn("Опрос Звук · ноябрь 2026 до 22.10 20:00", intro)
        self.assertIn("Алексей", intro)
        self.assertEqual(
            readiness.reminder_text(
                self.service(), run, TZ,
                self.registry.get_readiness_settings("sound")["reminder_template"],
            ),
            "Напоминаю: ноябрь 2026, до 22.10 20:00",
        )
        self.registry.set_readiness_settings(
            1001, "sound", closed_template="Всё, {month} — спасибо!"
        )
        self.assertEqual(
            readiness.closed_text(
                self.service(), run,
                self.registry.get_readiness_settings("sound")["closed_template"],
            ),
            "Всё, ноябрь 2026 — спасибо!",
        )
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", closed_template="{deadline}"
            )
        # Без опрашиваемых строка «👥» не остаётся пустой.
        self.assertEqual(
            readiness.render_template("А\n👥 {people}", people=""), "А"
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
