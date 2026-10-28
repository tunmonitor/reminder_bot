import time
import unittest
from datetime import date, timedelta

from bot.schedule_planner import PlanInput, build_plan, open_dates, plan_cost

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

    def test_overlapping_bundles_use_the_available_person(self):
        day = date(2026, 11, 1)
        plan = build_plan(PlanInput(
            (day,), {"a": 1, "b": 1, "c": 1},
            {"X": frozenset(), "Y": frozenset({day})},
            {"X": frozenset({"a", "b"}), "Y": frozenset({"b", "c"})},
            bundles={"X": frozenset({"a", "b"}), "Y": frozenset({"b", "c"})},
        ))
        # Y закрывает обе задачи своей связки.
        self.assertEqual(plan.slots[(day, "b")], ["Y"])
        self.assertEqual(plan.slots[(day, "c")], ["Y"])
        self.assertEqual(plan.covered_required(), 2)

    def test_bundle_gap_prefers_interval_over_answer(self):
        dates = (date(2026, 11, 1), date(2026, 11, 8))
        plan = build_plan(PlanInput(
            dates, {"звук": 1, "спевка": 1},
            {
                "S": frozenset(dates), "A": frozenset(dates),
                "Q": frozenset(dates), "L": frozenset(),
            },
            {
                "S": frozenset({"звук"}), "A": frozenset({"спевка"}),
                "Q": frozenset({"спевка"}),
                "L": frozenset({"звук", "спевка"}),
            },
            last_served={"A": date(2026, 10, 25)},
            reluctant=frozenset({"Q"}),
            bundles={"L": frozenset({"звук", "спевка"})},
        ))
        # A служил 25.10 — на 01.11 лучше не ответивший Q.
        self.assertEqual(plan.slots[(dates[0], "спевка")], ["Q"])

    def test_optional_seat_does_not_force_consecutive_bundle_weeks(self):
        d1, d2, d3 = (date(2026, 11, 1) + timedelta(weeks=i) for i in range(3))
        plan = build_plan(PlanInput(
            (d1, d2, d3), {"a": 1, "b": 1},
            {
                "p0": frozenset({d1, d2, d3}), "p1": frozenset({d1, d3}),
                "p2": frozenset({d2, d3}), "p3": frozenset({d2, d3}),
            },
            {
                "p0": frozenset({"a", "b"}), "p1": frozenset({"a"}),
                "p2": frozenset({"a"}), "p3": frozenset({"b"}),
            },
            extra={"a": 1},
            bundles={"p0": frozenset({"a", "b"})},
        ))
        self.assertEqual(plan.covered_required(), 6)
        served = {}
        for (day, _), people in plan.slots.items():
            for person in people:
                served.setdefault(person, set()).add(day)
        # Место сверх минимума не вынуждает служить две недели подряд.
        self.assertFalse(any(
            day + timedelta(weeks=1) in days
            for days in served.values() for day in days
        ))

    def test_bundle_extras_are_matched_together_and_respect_minimum(self):
        day = date(2026, 11, 1)
        bundle = frozenset({"звук", "спевка"})
        base = dict(
            dates=(day,),
            needed={"звук": 1, "спевка": 1, "a": 1, "b": 1},
            tasks={
                "L": bundle, "Da": frozenset({"a"}), "Db": frozenset({"b"}),
                "P": frozenset({"a", "b"}), "Q": frozenset({"a"}),
            },
            extra={"a": 1, "b": 1},
            bundles={"L": bundle},
            last_served={"P": date(2026, 10, 18), "Q": date(2026, 10, 18)},
        )
        free = {name: frozenset({day}) for name in base["tasks"]}
        plan = build_plan(PlanInput(free=free, **base))
        # P может обе задачи, Q — только «a»: заняты оба места сверх минимума.
        self.assertEqual(
            sorted(plan.slots[(day, "a")] + plan.slots[(day, "b")]),
            ["Da", "Db", "P", "Q"],
        )
        # Недобравший месячный минимум — первым на место сверх минимума.
        tasks = dict(base["tasks"], Q=frozenset({"a", "b"}))
        plan = build_plan(PlanInput(
            free={name: frozenset({day}) for name in ("L", "Da", "Db", "P", "Q")},
            limits={"Q": (1, None)},
            **dict(base, tasks=tasks, extra={"a": 1, "b": 0}),
        ))
        self.assertIn("Q", plan.slots[(day, "a")] + plan.slots[(day, "b")])

    def test_single_day_competition_and_loser_placed_instead_of_silent(self):
        # Решение 16: Марк и Игорь отметили только 22.11, Елисей не ответил.
        dates = SUNDAYS
        both = frozenset({"звук", "спевка"})
        data = PlanInput(
            dates=dates,
            needed={"звук": 1, "спевка": 1, "сборка": 1},
            free={
                "Матвей": frozenset(dates), "Егор": frozenset(dates),
                "Игорь": frozenset({dates[3]}), "Марк": frozenset({dates[3]}),
                "Елисей": frozenset(dates),
                "Василий": frozenset({dates[0], dates[4]}),
            },
            tasks={
                "Матвей": frozenset({"сборка"}), "Егор": frozenset({"сборка"}),
                "Игорь": both, "Марк": both, "Елисей": both, "Василий": both,
            },
            reluctant=frozenset({"Елисей"}),
            bundles={name: both for name in ("Игорь", "Марк", "Елисей", "Василий")},
            # Игорь служил раньше Марка — дата его.
            last_served={
                "Игорь": date(2026, 10, 18), "Марк": date(2026, 10, 25),
                "Елисей": date(2026, 10, 28),
            },
        )
        plan = build_plan(data)
        self.assertEqual(plan.slots[(dates[3], "звук")], ["Игорь"])
        # Марк — вместо не ответившего, Елисей — без служения подряд.
        self.assertEqual(plan.load()["Марк"], 1)
        self.assertEqual(plan.load()["Елисей"], 1)
        mark = next(day for (day, _), people in plan.slots.items()
                    if "Марк" in people)
        self.assertEqual(
            plan.slots[(mark, "звук")] + plan.slots[(mark, "спевка")],
            ["Марк", "Марк"],
        )
        # Раньше ответивший выигрывает при прочих равных.
        tie = build_plan(PlanInput(
            dates=(dates[0],), needed={"звук": 1},
            free={"А": frozenset({dates[0]}), "Б": frozenset({dates[0]})},
            tasks={"А": frozenset({"звук"}), "Б": frozenset({"звук"})},
            answer_order={"Б": 0, "А": 1},
        ))
        self.assertEqual(tie.slots[(dates[0], "звук")], ["Б"])
        # Меньше отмеченных дат — главнее порядка ответа.
        tie = build_plan(PlanInput(
            dates=dates[:2], needed={"звук": 1},
            free={"А": frozenset({dates[0]}), "Б": frozenset(dates[:2])},
            tasks={"А": frozenset({"звук"}), "Б": frozenset({"звук"})},
            answer_order={"Б": 0, "А": 1},
        ))
        self.assertEqual(tie.slots[(dates[0], "звук")], ["А"])

    def test_loser_not_placed_back_to_back_or_when_cant(self):
        dates = SUNDAYS[:2]
        data = PlanInput(
            dates=dates, needed={"звук": 1},
            free={
                "А": frozenset({dates[0]}), "Б": frozenset({dates[0]}),
                "Тихий": frozenset(dates), "Не смогу": frozenset(),
            },
            tasks={name: frozenset({"звук"})
                   for name in ("А", "Б", "Тихий", "Не смогу")},
            reluctant=frozenset({"Тихий"}),
            # Б служил за неделю до 08.11 — туда не ставится.
            last_served={"А": date(2026, 10, 4), "Б": date(2026, 11, 1)},
            answer_order={"А": 0, "Б": 1},
        )
        plan = build_plan(data)
        self.assertEqual(plan.slots[(dates[0], "звук")], ["А"])
        self.assertEqual(plan.slots[(dates[1], "звук")], ["Тихий"])

    def test_open_dates_ignore_silent_and_count_bundles(self):
        both = frozenset({"звук", "спевка"})
        data = PlanInput(
            dates=SUNDAYS[:3], needed={"звук": 1, "спевка": 1},
            free={
                "Связка": frozenset({SUNDAYS[0]}),
                "Звук": frozenset({SUNDAYS[1]}),
                "Тихий": frozenset(SUNDAYS),
            },
            tasks={"Связка": both, "Звук": frozenset({"звук"}), "Тихий": both},
            reluctant=frozenset({"Тихий"}),
            bundles={"Связка": both},
        )
        self.assertEqual(open_dates(data), list(SUNDAYS[1:3]))
        # Связка не даёт одному человеку два места в одной задаче.
        pair = frozenset({"a", "b"})
        data = PlanInput(
            dates=SUNDAYS[:1], needed={"a": 2, "b": 1},
            free={"P": frozenset(SUNDAYS[:1]), "Q": frozenset(SUNDAYS[:1])},
            tasks={"P": pair, "Q": frozenset({"b"})},
            bundles={"P": pair},
        )
        self.assertEqual(open_dates(data), [SUNDAYS[0]])


if __name__ == "__main__":
    unittest.main()
