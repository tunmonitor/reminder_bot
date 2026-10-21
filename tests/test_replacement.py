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

    def test_parsers(self):
        self.assertEqual(parse_change_args("звук @alex"), ("звук", "alex"))
        self.assertEqual(
            parse_change_args("свет и сцена @alex"), ("свет и сцена", "alex")
        )
        for wrong in ("", "звук", "@alex", "звук alex", "звук @"):
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

    def test_without_group_chat_admins_are_told(self):
        self.registry.set_group_chat(SUPER, "sound", None, enabled=False)
        self.assertIsNone(self.start())
        self.bot.send_poll.assert_not_awaited()
        self.assertIn("Чат служения не привязан", self.sent_to(ADMIN)[0])

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

    def test_no_volunteers_tells_admins(self):
        poll = self.start()
        self.bot.send_message.reset_mock()
        self.finish(poll)
        self.assertEqual(self.registry.list_replacements("sound", EVENT.isoformat()), [])
        self.assertIn("Замена не найдена", self.sent_to(ADMIN)[0])


class HandlersTest(Base):
    def setUp(self):
        super().setUp()
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(), debug_logger=MagicMock(),
            admin_id=SUPER, group_chat_id=-100, timezone=TZ,
            service_registry=self.registry, bot=self.bot,
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        routers = [call.args[0] for call in dispatcher.include_router.call_args_list]
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

    def test_change_command(self):
        none = self.message(ADMIN, "/change звук @boris_b")
        asyncio.run(self.change(none))
        self.assertIn("Нет открытого поиска", none.answer.await_args.args[0])

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


if __name__ == "__main__":
    unittest.main()
