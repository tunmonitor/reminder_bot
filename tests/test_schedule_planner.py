import time
import unittest
from datetime import date, timedelta

from bot.schedule_planner import PlanInput, build_plan, plan_cost

SUNDAYS = tuple(date(2026, 11, 1) + timedelta(weeks=week) for week in range(5))


def plan_input(free, tasks, needed=None, last_served=None, dates=SUNDAYS,
               extra=None, reluctant=(), limits=None):
    return PlanInput(
        dates=dates,
        needed=needed or {"звук": 1, "сборка": 1},
        free={person: frozenset(days) for person, days in free.items()},
        tasks={person: frozenset(names) for person, names in tasks.items()},
        last_served=last_served or {},
        extra=extra or {},
        reluctant=frozenset(reluctant),
        limits=limits or {},
    )


class SchedulePlannerTest(unittest.TestCase):
    def test_full_coverage_when_possible(self):
        # Жадный выбор «Алексей → звук» оставил бы сборку пустой.
        day = SUNDAYS[0]
        plan = build_plan(plan_input(
            {"Алексей": {day}, "Борис": {day}},
            {"Алексей": {"звук", "сборка"}, "Борис": {"звук"}},
            dates=(day,),
        ))
        self.assertEqual(plan.slots[(day, "звук")], ["Борис"])
        self.assertEqual(plan.slots[(day, "сборка")], ["Алексей"])
        self.assertEqual(plan.gaps(), [])

    def test_one_task_per_day_and_gap_reported(self):
        day = SUNDAYS[0]
        plan = build_plan(plan_input(
            {"Алексей": {day}},
            {"Алексей": {"звук", "сборка"}},
            dates=(day,),
        ))
        self.assertEqual(plan.covered(), 1)
        self.assertEqual(len(plan.gaps()), 1)

    def test_only_free_people_with_the_task_are_assigned(self):
        day = SUNDAYS[0]
        plan = build_plan(plan_input(
            {"Алексей": {SUNDAYS[1]}, "Сергей": {day}, "Борис": {day}},
            {"Алексей": {"звук"}, "Сергей": set(), "Борис": {"сборка"}},
            dates=(day,),
        ))
        self.assertEqual(plan.slots[(day, "звук")], [])
        self.assertEqual(plan.slots[(day, "сборка")], ["Борис"])

    def test_load_is_balanced(self):
        everyone = {"Алексей", "Борис", "Глеб"}
        plan = build_plan(plan_input(
            {person: set(SUNDAYS[:4]) for person in everyone},
            {person: {"звук", "сборка"} for person in everyone},
        ))
        loads = plan.load()
        self.assertEqual(plan.covered(), 8)
        self.assertLessEqual(max(loads.values()) - min(loads.values()), 1)

    def test_alternates_to_keep_interval(self):
        plan = build_plan(plan_input(
            {"Алексей": set(SUNDAYS[:4]), "Борис": set(SUNDAYS[:4])},
            {"Алексей": {"звук"}, "Борис": {"звук"}},
            needed={"звук": 1},
            dates=SUNDAYS[:4],
        ))
        order = [plan.slots[(day, "звук")][0] for day in SUNDAYS[:4]]
        self.assertTrue(all(a != b for a, b in zip(order, order[1:])))

    def test_previous_month_counts_for_interval(self):
        plan = build_plan(plan_input(
            {"Алексей": {SUNDAYS[0], SUNDAYS[1]}, "Борис": {SUNDAYS[0], SUNDAYS[1]}},
            {"Алексей": {"звук"}, "Борис": {"звук"}},
            needed={"звук": 1},
            last_served={"Алексей": SUNDAYS[0] - timedelta(weeks=1)},
            dates=SUNDAYS[:2],
        ))
        self.assertEqual(plan.slots[(SUNDAYS[0], "звук")], ["Борис"])
        self.assertEqual(plan.slots[(SUNDAYS[1], "звук")], ["Алексей"])

    def test_people_needed_two(self):
        day = SUNDAYS[0]
        plan = build_plan(plan_input(
            {"Алексей": {day}, "Борис": {day}, "Глеб": {day}},
            {name: {"в смене"} for name in ("Алексей", "Борис", "Глеб")},
            needed={"в смене": 2},
            dates=(day,),
        ))
        self.assertEqual(len(plan.slots[(day, "в смене")]), 2)
        self.assertEqual(plan.gaps(), [])

    def test_shortage_of_two(self):
        day = SUNDAYS[0]
        plan = build_plan(plan_input(
            {"Алексей": {day}},
            {"Алексей": {"в смене"}},
            needed={"в смене": 2},
            dates=(day,),
        ))
        self.assertEqual(plan.gaps(), [(day, "в смене", 1)])

    def test_deterministic_and_not_worse_than_first_pass(self):
        free = {
            "Алексей": set(SUNDAYS), "Борис": set(SUNDAYS[1:]),
            "Глеб": {SUNDAYS[0], SUNDAYS[2], SUNDAYS[4]}, "Виктор": set(),
        }
        tasks = {
            "Алексей": {"звук", "сборка"}, "Борис": {"звук"},
            "Глеб": {"сборка"}, "Виктор": {"звук"},
        }
        data = plan_input(free, tasks)
        first, second = build_plan(data), build_plan(data)
        self.assertEqual(first.slots, second.slots)
        self.assertEqual(first.covered(), 10)
        for (day, _), people in first.slots.items():
            self.assertEqual(len(people), len(set(people)))
        per_day = {}
        for (day, _), people in first.slots.items():
            for person in people:
                self.assertNotIn((day, person), per_day)
                per_day[(day, person)] = True
        self.assertGreaterEqual(plan_cost(first, data), 0)

    def test_answered_people_go_first_silent_fill_gaps(self):
        day = SUNDAYS[0]
        # Не ответивший свободен всегда, но ответивший — первым.
        plan = build_plan(plan_input(
            {"Алексей": {day}, "Борис": {day}},
            {"Алексей": {"звук"}, "Борис": {"звук"}},
            needed={"звук": 1}, dates=(day,), reluctant={"Борис"},
        ))
        self.assertEqual(plan.slots[(day, "звук")], ["Алексей"])
        # Ответивших не хватает — ставится не ответивший.
        plan = build_plan(plan_input(
            {"Борис": {day}},
            {"Борис": {"звук"}},
            needed={"звук": 1}, dates=(day,), reluctant={"Борис"},
        ))
        self.assertEqual(plan.slots[(day, "звук")], ["Борис"])
        # Сверх минимума не ответивший не ставится.
        plan = build_plan(plan_input(
            {"Алексей": {day}, "Борис": {day}},
            {"Алексей": {"звук"}, "Борис": {"звук"}},
            needed={"звук": 1}, extra={"звук": 1}, dates=(day,),
            reluctant={"Борис"},
        ))
        self.assertEqual(plan.slots[(day, "звук")], ["Алексей"])

    def test_interval_before_response(self):
        # Ответивший свободен все воскресенья, но служить подряд хуже,
        # чем поставить не ответившего (решение 12).
        plan = build_plan(plan_input(
            {"Алексей": set(SUNDAYS[:4]), "Борис": set(SUNDAYS[:4])},
            {"Алексей": {"звук"}, "Борис": {"звук"}},
            needed={"звук": 1}, dates=SUNDAYS[:4], reluctant={"Борис"},
        ))
        self.assertEqual(
            [plan.slots[(day, "звук")] for day in SUNDAYS[:4]],
            [["Алексей"], ["Борис"], ["Алексей"], ["Борис"]],
        )
        # Служила в последнее воскресенье октября — 01.11 не ставится,
        # хотя отметила больше дат.
        plan = build_plan(plan_input(
            {"Даша": set(SUNDAYS), "Катя": {SUNDAYS[0], SUNDAYS[1], SUNDAYS[3]}},
            {"Даша": {"уборка"}, "Катя": {"уборка"}},
            needed={"уборка": 1}, last_served={"Даша": date(2026, 10, 25)},
        ))
        self.assertEqual(plan.slots[(SUNDAYS[0], "уборка")], ["Катя"])
        # Заменить некем — подряд, но место не пустое.
        plan = build_plan(plan_input(
            {"Даша": {SUNDAYS[0]}}, {"Даша": {"уборка"}},
            needed={"уборка": 1}, dates=(SUNDAYS[0],),
            last_served={"Даша": date(2026, 10, 25)},
        ))
        self.assertEqual(plan.slots[(SUNDAYS[0], "уборка")], ["Даша"])

    def test_extra_seat_not_back_to_back(self):
        # Место сверх минимума не закрывается служением подряд.
        people = {name: set(SUNDAYS[:4]) for name in ("Анна", "Вера", "Глеб")}
        plan = build_plan(plan_input(
            people, {name: {"звук"} for name in people},
            needed={"звук": 1}, extra={"звук": 1}, dates=SUNDAYS[:4],
        ))
        served = {}
        for day in SUNDAYS[:4]:
            for person in plan.slots[(day, "звук")]:
                served.setdefault(person, []).append(day)
        for days in served.values():
            for earlier, later in zip(days, days[1:]):
                self.assertGreater((later - earlier).days, 7)

    def test_extra_seat_does_not_force_next_week(self):
        # Двое на две недели подряд: место сверх минимума 01.11 пустое,
        # иначе кто-то служил бы и 08.11.
        dates = SUNDAYS[:2]
        plan = build_plan(plan_input(
            {"Анна": set(dates), "Вера": set(dates)},
            {"Анна": {"звук"}, "Вера": {"звук"}},
            needed={"звук": 1}, extra={"звук": 1}, dates=dates,
        ))
        self.assertEqual(
            [len(plan.slots[(day, "звук")]) for day in dates], [1, 1]
        )
        self.assertNotEqual(
            plan.slots[(dates[0], "звук")], plan.slots[(dates[1], "звук")]
        )

    def test_month_max_is_hard_even_if_all_dates_marked(self):
        # Анна отметила всё, но максимум 1 — остальное закрывает Вера,
        # а где закрыть некем — место пустое.
        plan = build_plan(plan_input(
            {"Анна": set(SUNDAYS), "Вера": {SUNDAYS[1], SUNDAYS[3]}},
            {"Анна": {"звук"}, "Вера": {"звук"}},
            needed={"звук": 1}, limits={"Анна": (0, 1)},
        ))
        self.assertEqual(plan.load()["Анна"], 1)
        self.assertEqual(plan.load()["Вера"], 2)
        self.assertEqual(len(plan.gaps()), 2)
        # Максимум 0 — человек в этом месяце не ставится.
        plan = build_plan(plan_input(
            {"Анна": set(SUNDAYS)}, {"Анна": {"звук"}},
            needed={"звук": 1}, limits={"Анна": (0, 0)},
        ))
        self.assertEqual(plan.load(), {})

    def test_month_max_does_not_leave_avoidable_gap(self):
        # Анна (максимум 1) свободна 01 и 15, Вера — только 01: Анна
        # уходит на 15, 01 закрывает Вера.
        dates = (SUNDAYS[0], SUNDAYS[2])
        plan = build_plan(plan_input(
            {"Анна": set(dates), "Вера": {SUNDAYS[0]}},
            {"Анна": {"звук"}, "Вера": {"звук"}},
            needed={"звук": 1}, dates=dates, limits={"Анна": (0, 1)},
        ))
        self.assertEqual(plan.gaps(), [])
        self.assertEqual(plan.slots[(SUNDAYS[0], "звук")], ["Вера"])
        self.assertEqual(plan.slots[(SUNDAYS[2], "звук")], ["Анна"])

    def test_month_min_preferred(self):
        # Трое на 3 даты: без лимита — по одному; у Глеба минимум 2 —
        # он получает два служения (не подряд).
        dates = (SUNDAYS[0], SUNDAYS[2], SUNDAYS[4])
        people = {name: set(dates) for name in ("Анна", "Вера", "Глеб")}
        tasks = {name: {"звук"} for name in people}
        plan = build_plan(plan_input(
            people, tasks, needed={"звук": 1}, dates=dates,
            limits={"Глеб": (2, 3)},
        ))
        self.assertEqual(plan.load()["Глеб"], 2)

    def test_respondent_before_non_respondent(self):
        # Ответивший не может 15.11 — там не ответивший; служить подряд
        # никому не приходится, не ответивший ставится и 01.11.
        plan = build_plan(plan_input(
            {
                "Алексей": {SUNDAYS[0], SUNDAYS[1], SUNDAYS[3]},
                "Борис": set(SUNDAYS[:4]),
            },
            {"Алексей": {"звук"}, "Борис": {"звук"}},
            needed={"звук": 1}, dates=SUNDAYS[:4], reluctant={"Борис"},
        ))
        self.assertEqual(
            [plan.slots[(day, "звук")] for day in SUNDAYS[:4]],
            [["Борис"], ["Алексей"], ["Борис"], ["Алексей"]],
        )

    def test_range_fills_minimum_first_then_up_to_maximum(self):
        day = SUNDAYS[0]
        people = {name: {day} for name in ("А", "Б", "В", "Г", "Д")}
        tasks = {
            "А": {"старший"}, "Б": {"в смене"}, "В": {"в смене"},
            "Г": {"в смене"}, "Д": {"в смене"},
        }
        plan = build_plan(plan_input(
            people, tasks, needed={"старший": 1, "в смене": 3},
            extra={"в смене": 1}, dates=(day,),
        ))
        self.assertEqual(len(plan.slots[(day, "в смене")]), 4)
        self.assertEqual(plan.gaps(), [])
        # Людей меньше минимума — недобор только по минимуму.
        plan = build_plan(plan_input(
            {"Б": {day}, "В": {day}}, {"Б": {"в смене"}, "В": {"в смене"}},
            needed={"в смене": 3}, extra={"в смене": 1}, dates=(day,),
        ))
        self.assertEqual(plan.gaps(), [(day, "в смене", 1)])
        # Сначала минимум другой задачи, потом сверх минимума.
        plan = build_plan(plan_input(
            {"А": {day}, "Б": {day}},
            {"А": {"старший", "в смене"}, "Б": {"в смене"}},
            needed={"старший": 1, "в смене": 1}, extra={"в смене": 1},
            dates=(day,),
        ))
        self.assertEqual(plan.slots[(day, "старший")], ["А"])

    def test_dense_schedule_is_fast(self):
        # Пять дат, 30 мест на дату, 30 человек: улучшение оценивает ходы
        # по вкладу затронутых людей, а не пересчётом всего плана.
        people = [f"Человек {index}" for index in range(30)]
        tasks = [f"задача {index}" for index in range(6)]
        data = plan_input(
            {person: set(SUNDAYS) for person in people},
            {
                person: {tasks[index % 6], tasks[(index + 1) % 6]}
                for index, person in enumerate(people)
            },
            needed={task: 5 for task in tasks},
            last_served={
                person: SUNDAYS[0] - timedelta(weeks=1 + index % 3)
                for index, person in enumerate(people)
            },
        )
        started = time.monotonic()
        plan = build_plan(data)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(plan.covered(), 150)


    def test_rotation_when_more_people_than_seats(self):
        # 4 воскресенья, нужен один: кто служил в прошлом месяце позже,
        # тот в этом остаётся без служения.
        dates = SUNDAYS[:4]
        last = {
            "Анна": date(2026, 10, 25), "Вера": date(2026, 10, 18),
            "Галя": date(2026, 10, 11), "Даша": date(2026, 10, 4),
        }
        for people, out in (
            (["Анна", "Вера", "Галя", "Даша", "Ева"], {"Анна"}),
            (["Анна", "Вера", "Галя", "Даша", "Ева", "Женя"], {"Анна", "Вера"}),
        ):
            plan = build_plan(plan_input(
                {person: set(dates) for person in people},
                {person: {"уборка"} for person in people},
                needed={"уборка": 1}, last_served=last, dates=dates,
            ))
            served = set(plan.load())
            self.assertEqual(set(people) - served, out)
            self.assertEqual(max(plan.load().values()), 1)


    def test_service_early_in_month_counts_as_one_week_gap(self):
        # Служил в пн 02.11 (с прошлого месяца) — 08.11 через 6 дней.
        dates = SUNDAYS[1:3]
        plan = build_plan(plan_input(
            {"Анна": set(dates), "Вера": set(dates)},
            {"Анна": {"звук"}, "Вера": {"звук"}},
            needed={"звук": 1}, dates=dates,
            last_served={"Анна": date(2026, 11, 2)},
        ))
        self.assertEqual(plan.slots[(dates[0], "звук")], ["Вера"])

    def test_bundled_tasks_go_to_one_person_and_count_once(self):
        dates = tuple(date(2026, 11, 1) + timedelta(weeks=i) for i in range(5))
        needed = {"звук": 1, "сборка": 1, "спевка": 1}
        tasks = {
            name: frozenset({"звук", "спевка"}) for name in "ABCD"
        } | {"E": frozenset({"сборка"}), "F": frozenset({"сборка"})}
        free = {name: frozenset(dates) for name in tasks}
        bundle = frozenset({"звук", "спевка"})
        plan = build_plan(PlanInput(
            dates, needed, free, tasks,
            bundles={name: bundle for name in "ABCD"},
        ))
        for day in dates:
            self.assertEqual(plan.slots[(day, "звук")], plan.slots[(day, "спевка")])
        load = plan.load()
        # Связка в дату — одно служение; связавшие — по очереди.
        self.assertEqual(sum(load[name] for name in "ABCD"), 5)
        self.assertLessEqual(max(load[name] for name in "ABCD"), 2)
        self.assertEqual(plan.covered_required(), 15)

    def test_single_bundled_person_is_not_forced_every_week(self):
        dates = tuple(date(2026, 11, 1) + timedelta(weeks=i) for i in range(4))
        needed = {"звук": 1, "спевка": 1}
        tasks = {name: frozenset({"звук", "спевка"}) for name in "ABCD"}
        free = {name: frozenset(dates) for name in tasks}
        plan = build_plan(PlanInput(
            dates, needed, free, tasks,
            bundles={"A": frozenset({"звук", "спевка"})},
        ))
        self.assertEqual(plan.covered_required(), 8)
        # Не каждую неделю и не две недели подряд.
        served = sorted(
            day for day in dates
            if "A" in plan.slots[(day, "звук")] + plan.slots[(day, "спевка")]
        )
        self.assertLess(len(served), 4)
        self.assertTrue(all(
            (later - earlier).days > 7 for earlier, later in zip(served, served[1:])
        ))


if __name__ == "__main__":
    unittest.main()
