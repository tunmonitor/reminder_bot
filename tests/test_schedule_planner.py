import time
import unittest
from datetime import date, timedelta

from bot.schedule_planner import PlanInput, build_plan, plan_cost

SUNDAYS = tuple(date(2026, 11, 1) + timedelta(weeks=week) for week in range(5))


def plan_input(free, tasks, needed=None, last_served=None, dates=SUNDAYS):
    return PlanInput(
        dates=dates,
        needed=needed or {"звук": 1, "сборка": 1},
        free={person: frozenset(days) for person, days in free.items()},
        tasks={person: frozenset(names) for person, names in tasks.items()},
        last_served=last_served or {},
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


if __name__ == "__main__":
    unittest.main()
