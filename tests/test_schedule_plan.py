import asyncio
import sqlite3
from contextlib import closing
import logging
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from bot import readiness, schedule_plan
from bot.presentation import task_emoji
from bot.excel_parser import (
    RosterMember,
    ServiceAssignment,
    ServiceEvent,
    parse_service_events,
)
from bot.service_registry import ServiceRegistry

TZ = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger("test-schedule-plan")
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


def event(day: date, *pairs: tuple[str, str]) -> ServiceEvent:
    return ServiceEvent(
        "sound", day,
        tuple(ServiceAssignment(task, name) for task, name in pairs),
    )


class SchedulePlanTest(unittest.TestCase):
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
        self.registry.set_responsible_contact(
            1001, "sound", "сборка", "Глеб", 503
        )
        self.bot = fake_bot()
        self.run = asyncio.run(readiness.launch_run(
            self.bot, self.registry, self.service(), 2026, 11,
            datetime(2026, 10, 22, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))
        record = self.registry.record_readiness_answer
        record("poll-1", 501, "alex", "Алексей", ["2026-11-01", "2026-11-08"])
        record("poll-1", 502, "", "Борис", ["2026-11-01", "2026-11-15"])
        record("poll-1", 503, "", "Глеб", ["2026-11-01", "2026-11-08"])
        self.registry.close_readiness_run(self.run["id"])
        self.run = self.registry.get_readiness_run_by_id(self.run["id"])

    def service(self):
        return self.registry.get_service("sound")

    def people(self):
        return self.registry.readiness_people("sound")

    def draft(self, loader=None):
        return asyncio.run(
            schedule_plan.build_draft(self.registry, self.run, loader, LOG)
        )

    def test_last_served_from_excel_and_approved_plan(self):
        loader = AsyncMock(return_value=[
            event(date(2026, 10, 18), ("звук", "Алексей")),
            event(date(2026, 10, 25), ("звук", "алексей"), ("сборка", "Глеб")),
            event(date(2026, 11, 1), ("звук", "Борис")),
        ])
        served, note = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertIsNone(note)
        self.assertEqual(served, {"Алексей": "2026-10-25", "Глеб": "2026-10-25"})
        # Утверждённый график прошлого месяца главнее Excel.
        self.registry.save_schedule_draft(
            "sound", "2026-10", run_id=None, dates=["2026-10-18"],
            tasks=[], slots={"2026-10-18": {"звук": ["Борис"]}}, last_served={},
        )
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-10", {"2026-10-18": {"звук": ["Борис"]}},
            chat_id=None, message_id=None,
        )
        served, _ = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertEqual(served, {"Борис": "2026-10-18"})

    def test_last_served_failure_is_noted(self):
        loader = AsyncMock(side_effect=OSError("disk"))
        served, note = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertEqual(served, {})
        self.assertIn("не учтён", note)

    def test_previous_month_pushes_person_later(self):
        # Алексей служил 25.10 — 01.11 ставится в последнюю очередь.
        loader = AsyncMock(return_value=[
            event(date(2026, 10, 25), ("звук", "Алексей")),
        ])
        plan = self.draft(loader)
        self.assertEqual(plan["slots"]["2026-11-01"]["звук"], ["Борис"])
        self.assertEqual(plan["last_served"], {"Алексей": "2026-10-25"})

    def test_draft_edit_approve_and_recalculate(self):
        plan = self.draft()
        self.assertEqual(plan["run_id"], self.run["id"])
        self.assertIsNone(plan["approved"])
        self.assertTrue(plan["dirty"])
        self.assertEqual(
            [task["name"] for task in plan["tasks"]], ["звук", "сборка"]
        )
        # ensure_plan не пересчитывает сохранённый график того же опроса.
        slots = plan["slots"]
        slots["2026-11-15"]["сборка"] = ["Дмитрий"]
        edited = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertEqual(edited["edited"], 1)
        again = asyncio.run(
            schedule_plan.ensure_plan(self.registry, self.run, None, LOG)
        )
        self.assertEqual(again["slots"]["2026-11-15"]["сборка"], ["Дмитрий"])
        with self.assertRaises(PermissionError):
            self.registry.set_schedule_slots(9999, "sound", "2026-11", slots)

        approved = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), again, 1001, LOG, 4096,
        ))
        self.assertFalse(approved["dirty"])
        self.assertEqual(approved["message_id"], 11)
        chat, text = self.bot.send_message.await_args.args[:2]
        self.assertEqual(chat, -1005555)
        self.assertIn("График Звук на ноябрь 2026 утверждён", text)
        self.assertIn('href="tg://user?id=501"', text)

        # Правка после утверждения — публикация «обновлён» со списком.
        slots = approved["slots"]
        slots["2026-11-15"]["сборка"] = ["Глеб"]
        changed = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertTrue(changed["dirty"])
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("обновлён", text)
        self.assertIn("15.11 «сборка»: Дмитрий → Глеб", text)

        # Пересчёт сбрасывает правки, утверждённая копия остаётся.
        recalculated = self.draft()
        self.assertEqual(recalculated["edited"], 0)
        self.assertEqual(
            recalculated["approved"]["2026-11-15"]["сборка"], ["Глеб"]
        )

    def test_member_edits_and_removal_keep_plan_and_contacts_consistent(self):
        plan = self.draft()
        slots = plan["slots"]
        slots["2026-11-01"]["звук"] = ["Борис"]
        self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        # Задачи Алексея заданы в боте: график не возвращает снятую задачу.
        self.registry.set_service_member_tasks(1001, "sound", "Алексей", ["сборка"])
        self.registry.ensure_member_contacts("sound", {"звук": ["Алексей"]})
        alex = next(
            person for person in self.people() if person["name"] == "Алексей"
        )
        self.assertEqual(alex["tasks"], ["сборка"])
        # Человек только из графика получает запись состава.
        self.registry.set_service_member_tasks(1001, "sound", "Олег", ["звук"])
        self.assertIn(
            "Олег",
            [item["name"] for item in self.registry.list_service_members("sound")],
        )
        # Убранный из состава уходит и из рабочей копии графика — вместе
        # с разовым днём.
        self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-01", "Борис", -1
        )
        self.registry.remove_service_member(1001, "sound", "Борис")
        self.assertEqual(
            self.registry.get_schedule_plan("sound", "2026-11")["days"], {}
        )
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertNotIn(
            "Борис",
            [key for tasks in plan["slots"].values()
             for people in tasks.values() for key in people],
        )

    def test_service_without_tasks_is_one_task(self):
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
            RosterMember("Галя", ()),
        ]})
        name = self.registry.get_service("office_cleaning")["name"]
        people = self.registry.readiness_people("office_cleaning")
        self.assertEqual({tuple(item["tasks"]) for item in people}, {(name,)})
        # Пропавший из «Списков» в служение без задач не попадает.
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
        ]})
        gone = next(
            item for item in self.registry.readiness_people("office_cleaning")
            if item["name"] == "Галя"
        )
        self.assertEqual(gone["tasks"], [])
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
            RosterMember("Галя", ()),
        ]})
        self.registry.set_readiness_settings(
            1001, "office_cleaning", people_needed=1, people_max=2
        )
        tasks = self.registry.plan_tasks("office_cleaning")
        self.assertEqual(
            [(task["name"], task["needed"], task["max"], task["position"])
             for task in tasks],
            [(name, 1, 2, "s")],
        )
        plan = asyncio.run(schedule_plan.build_draft(
            self.registry, schedule_plan.free_run("office_cleaning", 2026, 11),
            None, LOG,
        ))
        # Служение без задач: люди сразу под датой, без названия служения.
        summary = schedule_plan.plan_summary(self.registry, plan, None)
        text = schedule_plan.plan_text("УБОРКА", plan, summary, TZ)
        self.assertNotIn(f"   {task_emoji(name)} {name}", text)
        self.assertIn("🗓 01.11\n   • ", text)
        published = schedule_plan.publication_text(
            self.registry.get_service("office_cleaning"), plan,
            self.registry.readiness_people("office_cleaning"),
        )
        self.assertNotIn(f"{name}:", published)
        counts = [len(plan["slots"][day][name]) for day in plan["dates"]]
        # Без опроса свободны все — места до максимума заполнены.
        self.assertEqual(counts, [2] * 5)
        load = [len(days) for days in schedule_plan.served_dates(plan["slots"]).values()]
        self.assertLessEqual(max(load) - min(load), 1)
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "office_cleaning", people_needed=3, people_max=2
            )
        # Выпавший из «Списков» и возвращённый «➕ В состав» — снова в нём.
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
        ]})
        self.registry.bind_service_member(
            1001, "office_cleaning", "Галя", 909, from_readiness=True
        )
        galya = next(
            item for item in self.registry.readiness_people("office_cleaning")
            if item["name"] == "Галя"
        )
        self.assertEqual(galya["tasks"], [name])
        # Переименование служения — задача графика вслед, правки целы.
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE services SET name = 'Уборка' WHERE id = 'office_cleaning'"
            )
            connection.commit()
        self.registry.approve_schedule_plan(
            1001, "office_cleaning", plan["month"], plan["slots"],
            chat_id=None, message_id=None,
        )
        renamed = schedule_plan.sync_implicit_task(self.registry, plan)
        self.assertEqual(renamed["tasks"][0]["name"], "Уборка")
        # Опубликован со старым названием — смена названия публикуется
        # одной строкой.
        self.assertTrue(renamed["dirty"])
        self.assertEqual(
            schedule_plan.plan_changes(renamed["approved"], renamed["slots"], {}),
            [f"• Задача «{name}» теперь называется «Уборка»"],
        )
        # Несколько задач: выключили одну, включили другую — не
        # переименование.
        swap = schedule_plan.plan_changes(
            {"2026-11-01": {"звук": ["А"], "сборка": ["Б"]}},
            {"2026-11-01": {"звук": ["А"], "спевка": ["Б"]}},
            {},
        )
        self.assertNotIn("теперь называется", " ".join(swap))
        self.assertEqual(
            renamed["slots"][plan["dates"][0]]["Уборка"],
            plan["slots"][plan["dates"][0]][name],
        )

    @unittest.mock.patch(
        "bot.service_registry._current_month", return_value="2026-10"
    )
    def test_day_window_and_personal_day_in_texts(self, _month):
        self.assertEqual(
            schedule_plan.day_label("2026-11-01", (-1, 1)),
            "01.11 (сб 31.10 – пн 02.11)",
        )
        self.assertEqual(schedule_plan.day_label("2026-11-01"), "01.11")
        self.registry.set_readiness_settings(1001, "sound", day_from=-1, day_to=1)
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", day_from=1)
        self.registry.set_service_member_day(1001, "sound", "Алексей", -3)
        plan = self.draft()
        plan["slots"]["2026-11-01"] = {"звук": ["Алексей"], "сборка": []}
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        text = schedule_plan.plan_text(
            "ЗВУК", plan, summary, TZ, schedule_plan.window_of(self.registry, "sound")
        )
        # У единственного на дату свой день — общий период не показывается.
        self.assertIn("🗓 01.11\n   🎚️ звук\n      • Алексей — чт 29.10", text)
        # Рядом служит Глеб без своего дня — период для него показывается.
        self.assertIn("🗓 08.11 (сб 07.11 – пн 09.11)", text)
        published = schedule_plan.publication_text(
            self.service(), plan, self.people(), (-1, 1)
        )
        self.assertIn("\x00 — чт 29.10", published)
        self.registry.set_service_member_day(1001, "sound", "Алексей", None)
        alex = next(p for p in self.people() if p["name"] == "Алексей")
        self.assertIsNone(alex["day_offset"])
        # Утверждён, затем сменились дни — «с правками», в публикации строка.
        plan = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", plan["slots"]
        )
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(self.registry.get_schedule_plan("sound", "2026-11")["dirty"])
        self.registry.set_readiness_settings(1001, "sound", day_from=0, day_to=0)
        changed = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertTrue(changed["dirty"])
        self.assertIn(
            "• Изменены дни служения",
            schedule_plan.publication_text(self.service(), changed, self.people()),
        )
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.assertFalse(self.registry.get_schedule_plan("sound", "2026-11")["dirty"])
        self.registry.set_service_member_day(1001, "sound", "Борис", 0)
        self.assertTrue(self.registry.get_schedule_plan("sound", "2026-11")["dirty"])

    def test_day_for_one_date(self):
        plan = self.draft()
        plan["slots"]["2026-11-15"]["звук"] = ["Борис"]
        plan = self.registry.set_schedule_slots(1001, "sound", "2026-11", plan["slots"])
        plan = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(plan["dirty"])
        plan = self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-15", "Борис", -2
        )
        self.assertTrue(plan["dirty"])
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        self.assertIn(
            "• Борис — пт 13.11",
            schedule_plan.plan_text("ЗВУК", plan, summary, TZ),
        )
        published = schedule_plan.publication_text(
            self.service(), plan, self.people()
        )
        self.assertIn("• 15.11 Борис: пт 13.11", published)
        plan = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(plan["dirty"])
        plan = self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-15", "Борис", None
        )
        self.assertEqual(plan["days"], {})
        self.assertTrue(plan["dirty"])
        # Пересчёт сбрасывает разовые дни, как и другие правки.
        self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-15", "Борис", -2
        )
        self.assertEqual(self.draft()["days"], {})

    def test_order_only_change_is_not_dirty(self):
        plan = self.draft()
        slots = plan["slots"]
        slots["2026-11-01"]["звук"] = ["Алексей", "Борис"]
        plan = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", plan["slots"], chat_id=None,
            message_id=None,
        )
        slots["2026-11-01"]["звук"] = ["Борис", "Алексей"]
        plan = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertFalse(plan["dirty"])

    def test_save_failure_after_publish_is_reported(self):
        plan = self.draft()
        with unittest.mock.patch.object(
            self.registry, "approve_schedule_plan",
            side_effect=RuntimeError("locked"),
        ):
            with self.assertRaises(schedule_plan.PublishedNotSaved):
                asyncio.run(schedule_plan.publish(
                    self.bot, self.registry, self.service(), plan, 1001, LOG,
                    4096,
                ))

    def test_publish_without_chat_is_refused(self):
        plan = self.draft()
        self.registry.set_group_chat(1001, "sound", -1005555, enabled=False)
        with self.assertRaises(ValueError):
            asyncio.run(schedule_plan.publish(
                self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
            ))
        self.assertIsNone(
            self.registry.get_schedule_plan("sound", "2026-11")["approved"]
        )

    def test_plan_text_candidates_and_notes(self):
        plan = self.draft()
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        text = schedule_plan.plan_text("ЗВУК", plan, summary, TZ)
        self.assertIn("📋 График · ЗВУК · ноябрь 2026", text)
        self.assertIn("📝 Черновик", text)
        self.assertIn("Ответили 3 из 4", text)
        self.assertIn("🗓 01.11", text)
        self.assertIn("👥 Нагрузка (воскресений в месяце):", text)
        self.assertIn("   3 — Дмитрий (без Telegram)", text)
        self.assertIn("   🎚️ звук\n      • Борис", text)
        self.assertIn("Дмитрий", text)

        day = "2026-11-08"
        plan["slots"][day] = {"звук": ["Алексей"], "сборка": []}
        free, quiet, rest = schedule_plan.candidates(plan, summary, day, "сборка")
        # Алексей занят на «звук» — не предлагается (одна задача в день).
        self.assertNotIn("Алексей", free + quiet + rest)
        self.assertEqual(free, ["Глеб"])
        self.assertEqual(quiet, ["Дмитрий"])
        self.assertEqual(rest, ["Борис"])
        self.assertEqual(
            schedule_plan.busy_on(plan, day, "сборка"), {"Алексей": "звук"}
        )
        plan["slots"]["2026-11-01"]["звук"] = ["Борис"]
        notes = schedule_plan.choice_notes(plan, summary, "Борис", day, "сборка")
        self.assertIn("⚠️ не отмечал(а) 08.11", notes)
        self.assertIn("⚠️ не приписан(а) к «сборка»", notes)
        self.assertIn("ℹ️ служил(а) 01.11", notes)
        notes = schedule_plan.choice_notes(plan, summary, "Дмитрий", day, "сборка")
        self.assertEqual(notes[0], "⚪ не отвечал(а) на опрос")
        self.assertIn("ℹ️ служит 15.11", notes)
        plan["slots"]["2026-11-15"] = {"звук": [], "сборка": []}
        self.assertIn("⚠️ 15.11 «звук»: никого", schedule_plan.gaps(plan))

    def test_excel_block_and_file(self):
        plan = self.draft()
        plan["slots"]["2026-11-01"] = {"звук": ["Алексей", "Борис"], "сборка": []}
        people = self.people()
        rows = schedule_plan.excel_rows(self.registry, plan, people)
        self.assertEqual(rows[0][:3], ["звук", "", "спевка"])
        self.assertEqual(rows[1][0], date(2026, 11, 1))
        self.assertEqual(rows[2][:2], ["звук", "Алексей"])
        self.assertEqual(rows[3][:2], ["", "Борис"])
        text = schedule_plan.excel_text(self.registry, self.service(), plan, people)
        self.assertIn("лист «НОЯБРЬ26»", text)
        self.assertIn("01.11: звук — Алексей, Борис; сборка — пусто", text)
        data = schedule_plan.excel_file(self.registry, self.service(), plan, people)
        sheet = load_workbook(BytesIO(data)).active
        self.assertEqual(sheet.title, "НОЯБРЬ26")
        self.assertEqual(sheet["A2"].number_format, "DD.MM.YYYY")
        # Файл читается тем же парсером, что и лист графика.
        path = Path(self.temp_dir.name) / "plan.xlsx"
        path.write_bytes(data)
        events = parse_service_events(
            path, date(2026, 11, 1), LOG, self.registry.service_catalog()
        )["sound"]
        by_day = {
            item.event_date: sorted(
                (row.task, row.name) for row in item.assignments
            )
            for item in events
        }
        self.assertEqual(
            by_day[date(2026, 11, 1)], [("звук", "Алексей"), ("звук", "Борис")]
        )
        self.assertEqual(
            by_day[date(2026, 11, 8)],
            sorted(
                (task, name)
                for task, keys in plan["slots"]["2026-11-08"].items()
                for name in keys
            ),
        )

    def test_excel_rows_put_own_task_in_its_column(self):
        plan = {
            "service_id": "broadcast", "dates": ["2026-11-01"],
            "tasks": [
                {"name": "Пульт", "needed": 1, "max": None},
                {"name": "Слайды", "needed": 1, "max": None},
            ],
            "slots": {"2026-11-01": {"Пульт": ["Ира"], "Слайды": ["Оля"]}},
        }
        rows = schedule_plan.excel_rows(self.registry, plan, [])
        self.assertEqual(rows[0], ["трансляция", "", "слайды"])
        self.assertEqual(rows[2], ["Пульт", "Ира", "Оля"])

    def test_excel_filled(self):
        events = [event(date(2026, 11, 1), ("звук", "Борис"))]
        self.assertTrue(schedule_plan.excel_filled(events, "sound", 2026, 11))
        self.assertFalse(schedule_plan.excel_filled(events, "sound", 2026, 12))
        self.assertFalse(schedule_plan.excel_filled(events, "kitchen", 2026, 11))

    def test_finish_run_sends_plan_with_button(self):
        bot = fake_bot()
        run = self.registry.get_readiness_run_by_id(self.run["id"])
        asyncio.run(readiness.finish_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        admin_call = next(
            call for call in bot.send_message.await_args_list
            if call.args[0] == 1001
        )
        self.assertIn("Опрос завершён", admin_call.args[1])
        self.assertIn("📋 График · Звук", admin_call.args[1])
        buttons = [
            button.callback_data
            for row in admin_call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("rdy:plan:sound:202611", buttons)
        self.assertIsNotNone(self.registry.get_schedule_plan("sound", "2026-11"))


    def test_failed_draft_is_retried_without_repeating_notice(self):
        bot = fake_bot()
        run = self.registry.get_readiness_run_by_id(self.run["id"])
        with unittest.mock.patch(
            "bot.schedule_plan.ensure_plan", side_effect=RuntimeError("db")
        ):
            for _ in range(2):
                asyncio.run(readiness.finish_run(
                    bot, self.registry, run, self.service(), TZ, 1001, LOG,
                    4096,
                ))
        admin_texts = [
            call.args[1] for call in bot.send_message.await_args_list
            if call.args[0] == 1001
        ]
        self.assertEqual(len(admin_texts), 1)
        self.assertIn("график составить пока не удалось", admin_texts[0])
        self.assertFalse(
            self.registry.get_readiness_run_by_id(run["id"])["result_sent"]
        )
        asyncio.run(readiness.finish_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertTrue(
            self.registry.get_readiness_run_by_id(run["id"])["result_sent"]
        )
        self.assertIn("📋 График", bot.send_message.await_args.args[1])

    def test_partial_publication_is_removed(self):
        plan = self.draft()
        bot = fake_bot()
        bot.delete_message = AsyncMock()
        bot.send_message.side_effect = [
            SimpleNamespace(message_id=31), RuntimeError("network"),
        ]
        with unittest.mock.patch(
            "bot.schedule_plan.publication_text", return_value="x " * 3000
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(schedule_plan.publish(
                    bot, self.registry, self.service(), plan, 1001, LOG, 4096,
                ))
        bot.delete_message.assert_awaited_once_with(-1005555, 31)
        self.assertIsNone(
            self.registry.get_schedule_plan("sound", "2026-11")["approved"]
        )


if __name__ == "__main__":
    unittest.main()
