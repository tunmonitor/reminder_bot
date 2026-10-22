"""Этап 4.2: напоминание на период «с — по» по датам воскресений."""
import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.handlers import _parse_period, period_text, register
from bot.scheduler import (
    in_reminder_period,
    next_reminder_occurrence,
    reminder_period_status,
)
from bot.service_registry import ServiceRegistry

TZ = ZoneInfo("UTC")
SUPER = 67890


class PeriodRulesTest(unittest.TestCase):
    def test_occurrence_respects_period(self):
        now = datetime(2026, 10, 21, 12, 0, tzinfo=TZ)  # среда
        self.assertEqual(
            next_reminder_occurrence(now, 1, "18:00"),
            (date(2026, 10, 25), date(2026, 10, 24)),
        )
        self.assertEqual(
            next_reminder_occurrence(
                now, 1, "18:00", active_from="2026-11-08"
            ),
            (date(2026, 11, 8), date(2026, 11, 7)),
        )
        self.assertIsNone(
            next_reminder_occurrence(now, 1, "18:00", active_until="2026-10-18")
        )
        # Отправка на последнее воскресенье периода уже прошла.
        late = datetime(2026, 10, 24, 19, 0, tzinfo=TZ)
        self.assertIsNone(
            next_reminder_occurrence(late, 1, "18:00", active_until="2026-10-25")
        )

    def test_status_and_membership(self):
        reminder = {"active_from": "2026-11-01", "active_until": "2026-11-29"}
        self.assertEqual(reminder_period_status(reminder, date(2026, 10, 20)), "pending")
        self.assertEqual(reminder_period_status(reminder, date(2026, 11, 10)), "active")
        self.assertEqual(reminder_period_status(reminder, date(2026, 11, 30)), "ended")
        self.assertIsNone(reminder_period_status({}, date(2026, 11, 30)))
        self.assertTrue(in_reminder_period(date(2026, 11, 29), reminder))
        self.assertFalse(in_reminder_period(date(2026, 12, 6), reminder))
        self.assertEqual(
            period_text(reminder, date(2026, 11, 10)),
            "с 01.11.2026 по 29.11.2026 · 🟢 активно",
        )
        self.assertEqual(period_text({}, date(2026, 11, 10)), "без ограничений")

    def test_parse_period(self):
        self.assertEqual(
            _parse_period("01.11.2026 — 29.11.2026"),
            (date(2026, 11, 1), date(2026, 11, 29)),
        )
        self.assertEqual(_parse_period("- 29.11.2026"), (None, date(2026, 11, 29)))
        with self.assertRaises(ValueError):
            _parse_period("01.11.2026")
        with self.assertRaises(ValueError):
            _parse_period("1 ноября -")


class PeriodRegistryTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "r.sqlite3")
        self.registry.initialize(SUPER, -100)
        self.reminder_id = self.registry.add_group_reminder(
            SUPER, "sound", 2, "07:15"
        )

    def test_validation_and_expiry(self):
        today = date(2026, 10, 20)
        with self.assertRaisesRegex(ValueError, "воскресений"):
            self.registry.set_group_reminder_period(
                SUPER, self.reminder_id, date(2026, 11, 2), None, today=today
            )
        with self.assertRaisesRegex(ValueError, "позже"):
            self.registry.set_group_reminder_period(
                SUPER, self.reminder_id, date(2026, 11, 29), date(2026, 11, 1),
                today=today,
            )
        with self.assertRaisesRegex(ValueError, "прошло"):
            self.registry.set_group_reminder_period(
                SUPER, self.reminder_id, None, date(2026, 10, 18), today=today
            )
        self.registry.set_group_reminder_period(
            SUPER, self.reminder_id, date(2026, 11, 1), date(2026, 11, 29),
            today=today,
        )
        reminder = self.registry.get_group_reminder(self.reminder_id)
        self.assertEqual(
            (reminder["active_from"], reminder["active_until"]),
            ("2026-11-01", "2026-11-29"),
        )
        self.assertEqual(
            self.registry.disable_expired_group_reminders(date(2026, 11, 29)), []
        )
        self.assertEqual(
            self.registry.disable_expired_group_reminders(date(2026, 11, 30)),
            [self.reminder_id],
        )
        self.assertFalse(self.registry.get_group_reminder(self.reminder_id)["enabled"])
        self.registry.set_group_reminder_period(
            SUPER, self.reminder_id, None, None, today=today
        )
        reminder = self.registry.get_group_reminder(self.reminder_id)
        self.assertIsNone(reminder["active_until"])


class PeriodOverlapTest(PeriodRegistryTest):
    def test_same_slot_allowed_for_disjoint_periods_only(self):
        today = date(2026, 10, 20)
        self.registry.set_group_reminder_period(
            SUPER, self.reminder_id, date(2026, 11, 1), date(2026, 11, 29),
            today=today,
        )
        other = self.registry.add_group_reminder(
            SUPER, "sound", 2, "07:15", allow_duplicate=True
        )
        self.registry.update_group_reminder(SUPER, other, enabled=False)
        self.registry.set_group_reminder_period(
            SUPER, other, date(2026, 12, 6), date(2026, 12, 27), today=today
        )
        self.registry.update_group_reminder(SUPER, other, enabled=True)
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.set_group_reminder_period(
                SUPER, other, date(2026, 11, 22), date(2026, 12, 27), today=today
            )
        with self.assertRaisesRegex(ValueError, "разные периоды"):
            self.registry.merge_group_reminder(
                SUPER, self.reminder_id, expected_slot=(2, "07:15"),
                source_id=other, source_slot=(2, "07:15"),
            )


class PeriodScreenTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "r.sqlite3")
        self.registry.initialize(SUPER, -100)
        self.reminder_id = self.registry.add_group_reminder(
            SUPER, "sound", 2, "07:15"
        )
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(), debug_logger=MagicMock(),
            admin_id=SUPER, group_chat_id=-100, timezone=TZ,
            service_registry=self.registry,
            bot=SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock()),
            refresh_service_reminder_jobs=AsyncMock(),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        self.admin_router = dispatcher.include_router.call_args_list[0].args[0]

    def handler(self, name, kind="callback_query"):
        return [
            item.callback for item in getattr(self.admin_router, kind).handlers
            if item.callback.__name__ == name
        ][-1]

    def press(self, data):
        callback = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=SUPER),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=SUPER),
                answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_group_reminder_action")(callback))
        return callback

    @staticmethod
    def button(source, prefix):
        answer = getattr(source, "message", source).answer
        markup = answer.await_args.kwargs["reply_markup"]
        return next(
            item for row in markup.inline_keyboard for item in row
            if item.callback_data.startswith(prefix)
        )

    def test_pick_period_by_buttons_and_manual_input(self):
        command = SimpleNamespace(
            from_user=SimpleNamespace(id=SUPER),
            chat=SimpleNamespace(type="private", id=SUPER),
            text="/service_notify", users_shared=None, chat_shared=None,
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_service_notify", "message")(command))
        service = SimpleNamespace(
            data="service:service_notify:sound",
            from_user=SimpleNamespace(id=SUPER),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=SUPER), answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_service_selected")(service))
        listing = self.press(self.button(service, "group_reminder:list:sound:").callback_data)
        card = self.press(self.button(
            listing, f"group_reminder:open:sound:{self.reminder_id}:"
        ).callback_data)
        self.assertIn("🗓 Период: без ограничений", card.message.answer.await_args.args[0])
        period = self.press(self.button(card, "group_reminder:period_menu:").callback_data)
        start = self.press(self.button(
            period, f"group_reminder:period_from:sound:{self.reminder_id * 100 + 2}:"
        ).callback_data)
        end = self.press(self.button(
            start, f"group_reminder:period_until:sound:{self.reminder_id * 100 + 3}:"
        ).callback_data)
        reminder = self.registry.get_group_reminder(self.reminder_id)
        start_day = date.fromisoformat(reminder["active_from"])
        until_day = date.fromisoformat(reminder["active_until"])
        self.assertEqual(start_day.weekday(), 6)
        self.assertEqual((until_day - start_day).days, 14)
        self.assertIn("🟢 активно" if start_day <= date.today() else "⏳ ещё не началось",
                      end.message.answer.await_args.args[0])
        self.context.refresh_service_reminder_jobs.assert_awaited()

        period = self.press(self.button(end, "group_reminder:period_menu:").callback_data)
        self.press(self.button(period, "group_reminder:period_input:").callback_data)
        text = SimpleNamespace(
            from_user=SimpleNamespace(id=SUPER),
            chat=SimpleNamespace(type="private", id=SUPER),
            text=f"- {until_day:%d.%m.%Y}", users_shared=None, chat_shared=None,
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_service_parameter", "message")(text))
        reminder = self.registry.get_group_reminder(self.reminder_id)
        self.assertIsNone(reminder["active_from"])
        self.assertEqual(reminder["active_until"], until_day.isoformat())

        period = self.press(self.button(
            text, "group_reminder:period_menu:"
        ).callback_data)
        self.press(self.button(period, "group_reminder:period_clear:").callback_data)
        self.assertIsNone(self.registry.get_group_reminder(self.reminder_id)["active_until"])

    def test_period_button_of_other_reminder_is_stale(self):
        other = self.registry.add_group_reminder(SUPER, "sound", 3, "07:15")
        command = SimpleNamespace(
            from_user=SimpleNamespace(id=SUPER),
            chat=SimpleNamespace(type="private", id=SUPER),
            text="/service_notify", users_shared=None, chat_shared=None,
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_service_notify", "message")(command))
        service = SimpleNamespace(
            data="service:service_notify:sound",
            from_user=SimpleNamespace(id=SUPER),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=SUPER), answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_service_selected")(service))
        listing = self.press(self.button(service, "group_reminder:list:sound:").callback_data)
        card = self.press(self.button(
            listing, f"group_reminder:open:sound:{self.reminder_id}:"
        ).callback_data)
        period = self.press(self.button(card, "group_reminder:period_menu:").callback_data)
        session = period.data.rsplit(":", 1)[1]
        stale = self.press(
            f"group_reminder:period_from:sound:{other * 100 + 1}:{session}"
        )
        self.assertIn("устарел", stale.answer.await_args.args[0])
        stale = self.press(f"group_reminder:period_clear:sound:{other}:{session}")
        self.assertIn("устарел", stale.answer.await_args.args[0])

    def test_expired_reminder_cannot_be_enabled_by_toggle(self):
        self.registry.set_group_reminder_period(
            SUPER, self.reminder_id, None, date(2026, 10, 25),
            today=date(2026, 10, 20),
        )
        self.registry.disable_expired_group_reminders(date(2026, 10, 26))
        with patch("bot.handlers.datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 27, 12, 0, tzinfo=TZ)
            fake.strptime = datetime.strptime
            fake.combine = datetime.combine
            command = SimpleNamespace(
                from_user=SimpleNamespace(id=SUPER),
                chat=SimpleNamespace(type="private", id=SUPER),
                text="/service_notify", users_shared=None, chat_shared=None,
                answer=AsyncMock(),
            )
            asyncio.run(self.handler("on_service_notify", "message")(command))
            service = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=SUPER),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=SUPER),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            asyncio.run(self.handler("on_service_selected")(service))
            listing = self.press(
                self.button(service, "group_reminder:list:sound:").callback_data
            )
            card = self.press(self.button(
                listing, f"group_reminder:open:sound:{self.reminder_id}:"
            ).callback_data)
            self.assertIn("⏸ период завершён", card.message.answer.await_args.args[0])
            toggle = self.press(
                self.button(card, "group_reminder:toggle:").callback_data
            )
        self.assertIn("Период напоминания завершён", toggle.answer.await_args.args[0])
        self.assertFalse(self.registry.get_group_reminder(self.reminder_id)["enabled"])


if __name__ == "__main__":
    unittest.main()
