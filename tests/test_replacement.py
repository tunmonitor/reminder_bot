"""Этап 3.3: замена ответственного — опрос, итог, /change, приоритет графика."""
import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.handlers import register
from bot.replacement import (
    finish_replacement,
    parse_change_args,
    poll_answer_choice,
    poll_deadline,
    start_replacement_search,
)
from bot.presentation import apply_replacements
from bot.service_registry import ServiceRegistry

TZ = ZoneInfo("Europe/Moscow")
SUPER = 67890
ADMIN = 12345
OLD = 4004
MEMBER = 5005
OUTSIDER = 6006
EVENT = date(2099, 1, 4)


class Base(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "r.sqlite3")
        self.registry.initialize(SUPER, -100)
        self.registry.set_group_chat(SUPER, "sound", -1005, enabled=True)
        self.registry.add_service_admin(SUPER, "sound", ADMIN, "boss_admin")
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Алексей", OLD, "alex_one"
        )
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Борис", MEMBER, "boris_b"
        )
        self.poll_counter = 0

        async def send_poll(chat_id, question, **kwargs):
            self.poll_counter += 1
            return SimpleNamespace(
                message_id=200 + self.poll_counter,
                poll=SimpleNamespace(id=f"poll{self.poll_counter}"),
            )

        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
            send_poll=AsyncMock(side_effect=send_poll),
            stop_poll=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        self.schedule_close = MagicMock()

    def start(self, now=None):
        return asyncio.run(start_replacement_search(
            self.bot, self.registry, "sound", EVENT, "звук", "Алексей", OLD,
            admin_actor_id=SUPER, tz=TZ, logger=MagicMock(),
            schedule_close=self.schedule_close,
            now=now or datetime(2098, 12, 29, 10, 0, tzinfo=timezone.utc),
        ))

    def finish(self, poll, substitute=None):
        return asyncio.run(finish_replacement(
            self.bot, self.registry, poll, substitute,
            source="admin" if substitute else "poll", actor_id=None,
            admin_actor_id=SUPER, tz=TZ, logger=MagicMock(), report_limit=4096,
        ))

    def sent_to(self, chat_id):
        return [
            call.args[1] for call in self.bot.send_message.await_args_list
            if call.args[0] == chat_id
        ]


class PureRulesTest(unittest.TestCase):
    def test_deadline_rules(self):
        event = date(2026, 10, 18)
        monday = datetime(2026, 10, 12, 9, 0, tzinfo=TZ)
        self.assertEqual(poll_deadline(monday, event, TZ), monday + timedelta(hours=12))
        saturday = datetime(2026, 10, 17, 2, 0, tzinfo=TZ)
        self.assertEqual(
            poll_deadline(saturday, event, TZ),
            datetime(2026, 10, 17, 12, 0, tzinfo=TZ),
        )
        evening = datetime(2026, 10, 17, 20, 0, tzinfo=TZ)
        self.assertEqual(poll_deadline(evening, event, TZ), evening + timedelta(hours=1))

    def test_apply_replacements(self):
        assignments = {
            "звук": {"names": ["Алексей", "Вера"], "is_informational": False},
            "заметка": {"names": ["Алексей"], "is_informational": True},
        }
        result = apply_replacements(assignments, [
            {"task": "звук", "old_name": "Алексей", "new_name": "Борис"},
            {"task": "звук", "old_name": "Борис", "new_name": "Глеб"},
            {"task": "заметка", "old_name": "Алексей", "new_name": "Борис"},
        ])
        self.assertEqual(result["звук"]["names"], ["Глеб", "Вера"])
        self.assertEqual(result["заметка"]["names"], ["Алексей"])
        self.assertEqual(assignments["звук"]["names"], ["Алексей", "Вера"])
        # Заменённого в графике уже нет — замена всё равно действует.
        moved = apply_replacements(
            {"звук": ["Вера"]},
            [{"task": "звук", "old_name": "Алексей", "new_name": "Борис"}],
        )
        self.assertEqual(moved["звук"], ["Вера", "Борис"])

    def test_already_replaced_follows_chain(self):
        from bot.presentation import is_replaced as _already_replaced

        chain = [
            {"task": "звук", "old_name": "А", "new_name": "Б"},
            {"task": "звук", "old_name": "Б", "new_name": "А"},
        ]
        self.assertFalse(_already_replaced(chain, "звук", "А"))
        self.assertTrue(_already_replaced(chain[:1], "звук", "А"))
        self.assertFalse(_already_replaced(chain[:1], "сборка", "А"))

    def test_parsers(self):
        today = date(2026, 10, 21)
        self.assertEqual(
            parse_change_args("звук @alex", today), ("звук", "alex", None)
        )
        self.assertEqual(
            parse_change_args("свет и сцена @alex 25.10", today),
            ("свет и сцена", "alex", date(2026, 10, 25)),
        )
        self.assertEqual(
            parse_change_args("звук @alex 04.01", today)[2], date(2027, 1, 4)
        )
        self.assertEqual(
            parse_change_args("звук @alex 04.01.27", today)[2], date(2027, 1, 4)
        )
        for wrong in ("", "звук", "@alex", "звук alex", "звук @",
                      "звук @alex 32.10", "звук @alex 1.1 x"):
            with self.assertRaises(ValueError):
                parse_change_args(wrong)
        self.assertTrue(poll_answer_choice([0]))
        self.assertFalse(poll_answer_choice([1]))
        self.assertIsNone(poll_answer_choice([]))


class RegistryTest(Base):
    def test_poll_answers_and_single_close(self):
        self.registry.create_replacement_poll(
            "p1", "sound", EVENT.isoformat(), "звук", "Алексей", OLD,
            -1005, 201, 100, "2099-01-01T00:00:00+00:00",
        )
        self.assertTrue(self.registry.record_poll_answer("p1", 7, "a", "A", True))
        self.registry.record_poll_answer("p1", 8, "b", "B", False)
        self.registry.record_poll_answer("p1", 8, "b", "B", None)
        self.assertEqual(
            [(item["telegram_id"], item["can"]) for item in
             self.registry.list_poll_answers("p1")],
            [(7, True)],
        )
        self.assertEqual(
            self.registry.replacement_poll_by_message(-1005, 100)["poll_id"], "p1"
        )
        self.assertTrue(self.registry.close_replacement_poll("p1"))
        self.assertFalse(self.registry.close_replacement_poll("p1"))
        self.assertFalse(self.registry.record_poll_answer("p1", 9, "", "", True))

    def test_replacement_creates_contact_and_lookup(self):
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Гость", OUTSIDER,
            "@guest_user", "poll", None,
        )
        # Участник чата не из состава — только по имени, без привязки.
        self.assertIsNone(self.registry.service_person("sound", OUTSIDER, "звук"))
        self.registry.set_responsible_contact(
            SUPER, "sound", "сборка", "Борис", MEMBER, "boris_b"
        )
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Вера", "Борис", MEMBER,
            "boris_b", "admin", ADMIN,
        )
        self.assertIsNotNone(
            self.registry.service_person("sound", MEMBER, "звук")
        )
        self.assertEqual(
            self.registry.find_service_person_by_username("sound", "@BORIS_B")["telegram_id"],
            MEMBER,
        )
        self.assertIn(MEMBER, self.registry.task_member_ids("sound", "звук"))
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Гость",
        )


class SearchFlowTest(Base):
    def test_start_sends_notice_and_poll_once(self):
        poll = self.start()
        self.assertEqual(poll["status"], "open")
        self.assertEqual(poll["notice_message_id"], 100)
        kwargs = self.bot.send_poll.await_args.kwargs
        self.assertEqual(kwargs["options"], ["Могу", "Не смогу"])
        self.assertFalse(kwargs["is_anonymous"])
        self.assertIn("«звук»", self.bot.send_poll.await_args.args[1])
        self.schedule_close.assert_called_once()
        self.assertIn("/change звук @username", self.sent_to(ADMIN)[0])
        self.assertIsNone(self.start())
        self.assertEqual(self.bot.send_poll.await_count, 1)

    def test_without_group_chat_request_waits_for_change(self):
        self.registry.set_group_chat(SUPER, "sound", None, enabled=False)
        request = self.start()
        self.assertEqual((request["status"], request["filled"]), ("closed", 0))
        self.bot.send_poll.assert_not_awaited()
        self.assertIn("Чат служения не привязан", self.sent_to(ADMIN)[0])
        self.assertIsNone(self.start())
        substitute = {"name": "Борис", "telegram_id": MEMBER, "username": ""}
        self.assertTrue(self.finish(request, substitute))
        self.bot.stop_poll.assert_not_awaited()
        self.assertFalse(self.finish(request, substitute))
        self.assertIsNone(self.registry.find_active_replacement_request(
            "sound", EVENT.isoformat(), "звук", "Алексей"
        ))
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Борис",
        )

    def test_poll_failure_and_storage_failure(self):
        self.bot.send_poll.side_effect = RuntimeError("no rights")
        request = self.start()
        self.assertTrue(request["poll_id"].startswith("manual-"))
        self.assertIn("отправить не удалось", self.sent_to(ADMIN)[-1])

    def test_storage_failure_after_poll_sent_stops_poll(self):
        with patch.object(
            self.registry, "create_replacement_poll",
            side_effect=[RuntimeError("disk"), None],
        ):
            self.assertIsNone(self.start())
        self.bot.stop_poll.assert_awaited_once()
        self.assertIn("не запущен из-за ошибки", self.sent_to(ADMIN)[-1])

    def test_task_member_wins_over_earlier_outsider(self):
        poll = self.start()
        self.registry.record_poll_answer(poll["poll_id"], OLD, "alex_one", "Алексей", True)
        self.registry.record_poll_answer(poll["poll_id"], OUTSIDER, "guest_user", "Гость Г", True)
        self.registry.record_poll_answer(poll["poll_id"], MEMBER, "boris_b", "Борис Б", True)
        self.bot.send_message.reset_mock()
        self.assertTrue(self.finish(poll))
        self.assertFalse(self.finish(poll))
        self.bot.stop_poll.assert_awaited_once_with(-1005, poll["message_id"])
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Борис",
        )
        self.assertIn("Борис (@boris_b) вместо Алексей", self.sent_to(-1005)[0])
        self.assertIn("Вы назначены", self.sent_to(MEMBER)[0])
        self.assertEqual(
            self.registry.get_confirmation("sound", EVENT.isoformat(), "звук", MEMBER),
            "confirmed",
        )

    def test_outsider_used_when_no_task_member(self):
        poll = self.start()
        self.registry.record_poll_answer(poll["poll_id"], OUTSIDER, "guest_user", "Гость Г", True)
        self.finish(poll)
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Гость Г",
        )

    def test_failed_assignment_tells_admins(self):
        poll = self.start()
        self.bot.send_message.reset_mock()
        with patch.object(
            self.registry, "set_replacement", side_effect=ValueError("x")
        ), self.assertRaises(ValueError):
            self.finish(poll, {"name": "Борис", "telegram_id": MEMBER, "username": ""})
        self.assertIn("Не удалось назначить замену", self.sent_to(ADMIN)[0])

    def test_votes_do_not_trigger_backup_refresh(self):
        poll = self.start()
        self.registry.on_change = MagicMock()
        self.registry.record_poll_answer(poll["poll_id"], MEMBER, "", "", True)
        self.registry.on_change.assert_not_called()

    def test_no_volunteers_tells_admins_and_change_still_works(self):
        poll = self.start()
        self.bot.send_message.reset_mock()
        self.finish(poll)
        self.assertFalse(self.finish(poll))
        self.assertEqual(self.registry.list_replacements("sound", EVENT.isoformat()), [])
        self.assertIn("Замена не найдена", self.sent_to(ADMIN)[0])
        self.assertTrue(self.finish(
            poll, {"name": "Борис", "telegram_id": MEMBER, "username": ""}
        ))
        self.assertEqual(self.bot.stop_poll.await_count, 1)

    def test_report_follows_changed_replacement(self):
        from bot.admin_report import send_admin_reports

        raw = {"звук": {"names": ["Алексей"], "is_informational": False}}
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "", "admin", ADMIN,
        )
        service = self.registry.get_service("sound")
        asyncio.run(send_admin_reports(
            self.bot, self.registry, service, EVENT, raw, SUPER, MagicMock(), 4096
        ))
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Глеб", None,
            "", "admin", ADMIN,
        )
        asyncio.run(send_admin_reports(
            self.bot, self.registry, service, EVENT, raw, SUPER, MagicMock(), 4096
        ))
        text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("Глеб", text)
        self.assertNotIn("Борис", text)


class HandlersTest(Base):
    def setUp(self):
        super().setUp()
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(), debug_logger=MagicMock(),
            admin_id=SUPER, group_chat_id=-100, timezone=TZ,
            service_registry=self.registry, bot=self.bot,
            load_upcoming_service_events=AsyncMock(return_value=[]),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        routers = [call.args[0] for call in dispatcher.include_router.call_args_list]
        self.routers = routers
        self.change = next(
            item.callback for item in routers[0].message.handlers
            if item.callback.__name__ == "on_change_command"
        )
        self.poll_answer = [
            item.callback for item in routers[1].poll_answer.handlers
            if item.callback.__name__ == "on_replacement_poll_answer"
        ][-1]

    def message(self, user_id, text, chat_id=None, reply_to=None):
        chat_id = chat_id or user_id
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(
                id=chat_id, type="private" if chat_id > 0 else "supergroup"
            ),
            text=text, reply_to_message=reply_to, answer=AsyncMock(),
        )

    def test_hook_starts_search_and_poll_answers_are_recorded(self):
        asyncio.run(self.context.on_assignment_declined(
            "sound", EVENT, "звук", "Алексей", telegram_id=OLD
        ))
        poll = self.registry.list_open_replacement_polls()[0]
        asyncio.run(self.poll_answer(SimpleNamespace(
            poll_id=poll["poll_id"], option_ids=[0],
            user=SimpleNamespace(
                id=MEMBER, is_bot=False, username="boris_b",
                first_name="Борис", last_name=None,
            ),
        )))
        self.assertEqual(
            self.registry.list_poll_answers(poll["poll_id"])[0]["telegram_id"],
            MEMBER,
        )
        asyncio.run(self.context.close_replacement_poll(poll["poll_id"]))
        self.assertEqual(self.registry.list_open_replacement_polls(), [])

    def test_change_in_group_finds_request_without_poll(self):
        self.bot.send_poll.side_effect = RuntimeError("no rights")
        self.start()
        done = self.message(ADMIN, "/change звук @boris_b", -1005)
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])

    def test_change_command(self):
        none = self.message(ADMIN, "/change звук @boris_b")
        asyncio.run(self.change(none))
        self.assertIn("нет задачи «звук»", none.answer.await_args.args[0])

        poll = self.start()
        stranger = self.message(OUTSIDER, "/change звук @boris_b")
        asyncio.run(self.change(stranger))
        refusal = stranger.answer.await_args.args[0]
        self.assertIn("только админ служения", refusal)
        self.assertIn("@boss_admin", refusal)

        unknown = self.message(ADMIN, "/change звук @nobody")
        asyncio.run(self.change(unknown))
        self.assertIn("не найден", unknown.answer.await_args.args[0])

        self.context.cancel_replacement_close = MagicMock()
        reply = SimpleNamespace(message_id=poll["message_id"])
        done = self.message(ADMIN, "/change звук @Boris_B", -1005, reply)
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        replacement = self.registry.list_replacements("sound", EVENT.isoformat())[0]
        self.assertEqual(
            (replacement["new_name"], replacement["source"]), ("Борис", "admin")
        )
        self.assertEqual(self.registry.list_open_replacement_polls(), [])
        self.context.cancel_replacement_close.assert_called_once_with(
            poll["poll_id"]
        )
        again = self.message(ADMIN, "/change звук @boris_b", -1005, reply)
        asyncio.run(self.change(again))
        self.assertIn("уже завершён", again.answer.await_args.args[0])


class DirectChangeTest(HandlersTest):
    """/change и экран «🔁 Замена» без отказа участника (этап 3.4)."""

    def setUp(self):
        super().setUp()
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        today = datetime.now(TZ).date()
        self.day = today + timedelta(days=3)
        self.events = [
            ServiceEvent("sound", self.day, (
                ServiceAssignment("звук", "Алексей"),
                ServiceAssignment("сборка", "Алексей"),
                ServiceAssignment("спевка", "Вера"),
                ServiceAssignment("спевка", "Глеб"),
            )),
        ]
        self.context.load_upcoming_service_events.return_value = self.events
        self.registry.set_responsible_contact(
            SUPER, "sound", "сборка", "Борис", MEMBER, "boris_b"
        )

    def test_change_command(self):
        self.skipTest("сценарий без графика проверяется в HandlersTest")

    def test_change_without_decline(self):
        done = self.message(ADMIN, f"/change звук @boris_b {self.day:%d.%m}")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        row = self.registry.list_replacements("sound", self.day.isoformat())[0]
        self.assertEqual((row["old_name"], row["new_name"]), ("Алексей", "Борис"))
        self.assertIn("Борис (@boris_b) вместо Алексей", self.sent_to(-1005)[0])

        many = self.message(ADMIN, "/change спевка @boris_b")
        asyncio.run(self.change(many))
        self.assertIn("несколько человек", many.answer.await_args.args[0])

        stranger = self.message(OUTSIDER, "/change звук @boris_b")
        asyncio.run(self.change(stranger))
        self.assertIn("только админ служения", stranger.answer.await_args.args[0])

    def _callback(self, button):
        return SimpleNamespace(
            data=button.callback_data,
            from_user=SimpleNamespace(id=ADMIN),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=ADMIN),
                answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            ),
            answer=AsyncMock(),
        )

    def _handler(self, name):
        return next(
            item.callback for item in self.routers[0].callback_query.handlers
            if item.callback.__name__ == name
        )

    async def _press(self, message, label):
        markup = message.answer.await_args.kwargs["reply_markup"]
        button = next(
            item for row in markup.inline_keyboard for item in row
            if label in item.text
        )
        callback = self._callback(button)
        await self._handler("on_replace_action")(callback)
        return callback.message

    def test_replace_screen_flow(self):
        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            dates = card.message
            self.assertIn("Выберите дату", dates.answer.await_args.args[0])
            people = await self._press(dates, f"{self.day:%d.%m}")
            self.assertIn("Кого нужно заменить", people.answer.await_args.args[0])
            tasks = await self._press(people, "Алексей · 2 задачи")
            self.assertIn("несколько задач", tasks.answer.await_args.args[0])
            candidates = await self._press(tasks, "сборка")
            labels = [
                item.text for row in
                candidates.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for item in row
            ]
            self.assertIn("✅ Борис", labels)
            self.assertIn("👤 Выбрать пользователя Telegram", labels)
            confirm = await self._press(candidates, "Борис")
            self.assertIn("Алексей → Борис", confirm.answer.await_args.args[0])
            result = await self._press(confirm, "Назначить")
            self.assertIn("✅ Замена назначена", result.answer.await_args.args[0])
            row = self.registry.list_replacements("sound", self.day.isoformat())[0]
            self.assertEqual((row["task"], row["new_name"]), ("сборка", "Борис"))

        asyncio.run(run())

    def test_replace_screen_closes_open_request(self):
        async def run():
            await self.context.on_assignment_declined(
                "sound", self.day, "звук", "Алексей", telegram_id=OLD
            )
            self.assertEqual(len(self.registry.list_open_replacement_polls()), 1)
            done = self.message(ADMIN, "/change звук @boris_b")
            await self.change(done)
            self.assertEqual(self.registry.list_open_replacement_polls(), [])
            self.bot.stop_poll.assert_awaited_once()

        asyncio.run(run())

    def test_undated_change_skips_date_with_several_people(self):
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        later = self.day + timedelta(days=7)
        self.events.append(ServiceEvent("sound", later, (
            ServiceAssignment("спевка", "Вера"),
        )))
        done = self.message(ADMIN, "/change спевка @boris_b")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        self.assertEqual(
            self.registry.list_replacements("sound", later.isoformat())[0]["old_name"],
            "Вера",
        )
        kwargs = self.context.load_upcoming_service_events.await_args.kwargs
        self.assertIn("until", kwargs)

    def test_stale_screen_does_not_override_replacement(self):
        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            people = await self._press(card.message, f"{self.day:%d.%m}")
            tasks = await self._press(people, "Алексей · 2 задачи")
            candidates = await self._press(tasks, "сборка")
            confirm = await self._press(candidates, "Борис")
            # Пока экран открыт, замену назначили иначе.
            self.registry.set_replacement(
                "sound", self.day.isoformat(), "сборка", "Алексей", "Глеб",
                None, "", "admin", SUPER,
            )
            result = await self._press(confirm, "Назначить")
            self.assertIn("уже назначили", result.answer.await_args.args[0])
            rows = self.registry.list_replacements("sound", self.day.isoformat())
            self.assertEqual([row["new_name"] for row in rows], ["Глеб"])

        asyncio.run(run())

    def test_failed_announcement_warns_acting_admin(self):
        async def send_message(chat_id, text, **kwargs):
            if chat_id < 0:
                raise RuntimeError("chat gone")
            return SimpleNamespace(message_id=1)

        self.bot.send_message.side_effect = send_message
        done = self.message(ADMIN, f"/change звук @boris_b {self.day:%d.%m}")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        warnings = [text for text in self.sent_to(ADMIN) if "не дошло" in text]
        self.assertEqual(len(warnings), 1)
        self.assertIn("чат служения", warnings[0])

    def test_repeat_decline_after_replacement_starts_nothing(self):
        self.registry.set_replacement(
            "sound", self.day.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "", "admin", ADMIN,
        )
        asyncio.run(self.context.on_assignment_declined(
            "sound", self.day, "звук", "Алексей", telegram_id=OLD
        ))
        self.bot.send_poll.assert_not_awaited()


class MigrationTest(unittest.TestCase):
    def test_filled_backfilled_for_existing_replacements(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.sqlite3"
            registry = ServiceRegistry(path)
            registry.initialize(SUPER, -100)
            for poll_id in ("done", "waiting"):
                registry.create_replacement_poll(
                    poll_id, "sound", EVENT.isoformat(), "звук",
                    "Алексей" if poll_id == "done" else "Вера", None, -1, 1,
                    None, "2099-01-01T00:00:00+00:00", status="closed",
                )
            registry.set_replacement(
                "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", None,
                "", "admin", SUPER,
            )
            # База до появления колонки filled.
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE replacement_polls DROP COLUMN filled")
            ServiceRegistry(path).initialize(SUPER, -100)
            self.assertEqual(
                [item["poll_id"] for item in
                 registry.list_unfilled_replacement_requests("2000-01-01")],
                ["waiting"],
            )


class LoaderAppliesReplacementTest(Base):
    def test_schedule_loader_uses_replacement(self):
        from bot import app

        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "boris_b", "admin", ADMIN,
        )
        event = SimpleNamespace(
            event_date=EVENT, issues=[],
            assignments=[SimpleNamespace(
                task="звук", name="Алексей", is_informational=False
            )],
        )

        async def run():
            with tempfile.TemporaryDirectory() as shm, \
                    patch.object(app, "download_public", new=AsyncMock()), \
                    patch.object(
                        app, "parse_service_events",
                        return_value={"sound": [event]},
                    ), \
                    patch.object(app, "_sync_rosters_and_contacts"):
                return await app._load_service_task_assignments(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(shm), TZ, "sound", MagicMock(), only_date=EVENT,
                    registry=self.registry,
                )

        self.assertEqual(asyncio.run(run())["звук"]["names"], ["Борис"])

    def test_month_wide_loader_and_upcoming_events(self):
        from bot import app
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "boris_b", "admin", ADMIN,
        )
        events = [
            ServiceEvent("sound", EVENT, (ServiceAssignment("звук", "Алексей"),)),
            ServiceEvent(
                "sound", EVENT + timedelta(days=7),
                (ServiceAssignment("звук", "Алексей"),),
            ),
        ]
        shown = app._apply_event_replacements(events, self.registry)
        self.assertEqual(shown[0].assignments[0].name, "Борис")
        self.assertEqual(shown[1].assignments[0].name, "Алексей")
        self.assertEqual(events[0].assignments[0].name, "Алексей")

        async def run():
            with tempfile.TemporaryDirectory() as shm, \
                    patch.object(app, "download_public", new=AsyncMock()), \
                    patch.object(
                        app, "parse_service_events",
                        return_value={"sound": events},
                    ), \
                    patch.object(app, "_sync_rosters_and_contacts"):
                return await app._load_service_task_assignments(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(shm), TZ, "sound", MagicMock(), registry=self.registry,
                )

        # Без даты — имена за месяц: заменённый не пропадает из списков.
        self.assertEqual(asyncio.run(run())["звук"]["names"], ["Алексей"])


if __name__ == "__main__":
    unittest.main()
