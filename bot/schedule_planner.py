"""Распределение отметившихся по воскресеньям и задачам (этап 5.4).

Правила заказчика:
1. В один день у человека не больше одной задачи служения.
2. Основной задачи нет — человек идёт туда, где он нужнее.
3. Нагрузка в месяце выравнивается.
4. Между служениями человека — как можно больший интервал, с учётом
   прошлого месяца.

Алгоритм:
1. Покрытие: даты независимы, поэтому на каждую дату ищется
   паросочетание «человек → место в задаче» максимального размера —
   задача остаётся пустой, только если закрыть её действительно некем.
2. Справедливость: даты идут по порядку, на каждой берётся
   паросочетание минимальной стоимости среди максимальных; стоимость
   человека — прирост общей стоимости плана от ещё одного служения.
3. Улучшение: замены и обмены людьми, которые не уменьшают покрытие,
   но уменьшают общую стоимость.

Общая стоимость плана: Σ LOAD_WEIGHT × нагрузка² по людям + штрафы за
близкие служения (неделя, две) подряд у одного человека.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

LOAD_WEIGHT = 3
# Штраф за интервал между служениями человека, в неделях.
GAP_PENALTY = {1: 6, 2: 2}
# Предел проходов улучшения: план маленький, улучшения быстро кончаются.
MAX_IMPROVE_ROUNDS = 50


@dataclass(frozen=True)
class PlanInput:
    dates: tuple[date, ...]
    # Задача → сколько людей нужно (только задачи, которые распределяются).
    needed: Mapping[str, int]
    # Человек → даты, когда он свободен.
    free: Mapping[str, frozenset[date]]
    # Человек → его задачи («Списки» ∪ привязки в боте).
    tasks: Mapping[str, frozenset[str]]
    # Человек → последнее служение до месяца (из прошлого месяца).
    last_served: Mapping[str, date] = field(default_factory=dict)


@dataclass
class Plan:
    # (дата, задача) → люди в порядке назначения.
    slots: dict[tuple[date, str], list[str]]
    needed: Mapping[str, int]

    def load(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for people in self.slots.values():
            for person in people:
                result[person] = result.get(person, 0) + 1
        return result

    def covered(self) -> int:
        return sum(len(people) for people in self.slots.values())

    def required(self) -> int:
        return sum(self.needed[task] for _, task in self.slots)

    def gaps(self) -> list[tuple[date, str, int]]:
        """Незакрытые места: (дата, задача, сколько не хватает)."""
        return [
            (event_date, task, self.needed[task] - len(people))
            for (event_date, task), people in sorted(self.slots.items())
            if len(people) < self.needed[task]
        ]


def _weeks(earlier: date, later: date) -> int:
    return (later - earlier).days // 7


def _gap_penalty(earlier: date | None, later: date) -> int:
    if earlier is None:
        return 0
    return GAP_PENALTY.get(_weeks(earlier, later), 0)


def plan_cost(plan: Plan, data: PlanInput) -> int:
    served: dict[str, list[date]] = {}
    for (event_date, _), people in plan.slots.items():
        for person in people:
            served.setdefault(person, []).append(event_date)
    total = 0
    for person, dates in served.items():
        total += LOAD_WEIGHT * len(dates) ** 2
        previous = data.last_served.get(person)
        for event_date in sorted(dates):
            total += _gap_penalty(previous, event_date)
            previous = event_date
    return total


def _can_serve(data: PlanInput, person: str, event_date: date, task: str) -> bool:
    return (
        event_date in data.free.get(person, ())
        and task in data.tasks.get(person, ())
    )


def _min_cost_matching(
    people: list[str],
    seats: list[str],
    allowed: Mapping[str, set[int]],
    cost: Mapping[str, int],
) -> dict[int, str]:
    """Место → человек: максимальное паросочетание минимальной стоимости.

    Поток минимальной стоимости последовательными кратчайшими путями
    (Беллман — Форд): исток → человек (стоимость человека) → место →
    сток, пропускная способность 1. Каждый путь увеличивает поток на 1
    с наименьшим приростом стоимости, поэтому итог — минимальная
    стоимость среди максимальных паросочетаний. Размеры крошечные.
    """
    source, sink = 0, 1 + len(people) + len(seats)
    # Ребро: [куда, остаток, стоимость, индекс обратного ребра].
    graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]

    def add(start: int, end: int, price: int) -> None:
        graph[start].append([end, 1, price, len(graph[end])])
        graph[end].append([start, 0, -price, len(graph[start]) - 1])

    for index, person in enumerate(people, start=1):
        add(source, index, cost[person])
        for seat in sorted(allowed.get(person, ())):
            add(index, 1 + len(people) + seat, 0)
    for seat in range(len(seats)):
        add(1 + len(people) + seat, sink, 0)

    infinity = 1 << 60
    while True:
        distance = [infinity] * (sink + 1)
        came: list[tuple[int, int] | None] = [None] * (sink + 1)
        distance[source] = 0
        for _ in range(sink + 1):
            changed = False
            for node in range(sink + 1):
                if distance[node] == infinity:
                    continue
                for edge_index, (end, left, price, _) in enumerate(graph[node]):
                    if left and distance[node] + price < distance[end]:
                        distance[end] = distance[node] + price
                        came[end] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if distance[sink] == infinity:
            break
        node = sink
        while node != source:
            previous, edge_index = came[node]
            edge = graph[previous][edge_index]
            edge[1] -= 1
            graph[node][edge[3]][1] += 1
            node = previous

    result: dict[int, str] = {}
    for index, person in enumerate(people, start=1):
        for end, left, _, _ in graph[index]:
            seat = end - 1 - len(people)
            if 0 <= seat < len(seats) and left == 0:
                result[seat] = person
    return result


def _seats(data: PlanInput) -> list[str]:
    return [
        task
        for task, count in data.needed.items()
        for _ in range(max(0, count))
    ]


def build_plan(data: PlanInput) -> Plan:
    seats = _seats(data)
    plan = Plan(
        slots={
            (event_date, task): []
            for event_date in data.dates
            for task in data.needed
        },
        needed=data.needed,
    )
    load: dict[str, int] = {}
    last: dict[str, date] = dict(data.last_served)
    for event_date in sorted(set(data.dates)):
        people = sorted(
            person for person in data.free if event_date in data.free[person]
        )
        allowed = {
            person: {
                index for index, task in enumerate(seats)
                if _can_serve(data, person, event_date, task)
            }
            for person in people
        }
        # Прирост общей стоимости от ещё одного служения человека.
        cost = {
            person: LOAD_WEIGHT * (2 * load.get(person, 0) + 1)
            + _gap_penalty(last.get(person), event_date)
            for person in people
        }
        matching = _min_cost_matching(people, seats, allowed, cost)
        for seat, person in sorted(matching.items()):
            plan.slots[(event_date, seats[seat])].append(person)
            load[person] = load.get(person, 0) + 1
            last[person] = event_date
    improve_plan(plan, data)
    for people in plan.slots.values():
        people.sort()
    return plan


def _busy(plan: Plan, event_date: date) -> set[str]:
    return {
        person
        for (slot_date, _), people in plan.slots.items()
        if slot_date == event_date
        for person in people
    }


def _candidate_moves(plan: Plan, data: PlanInput) -> Iterable[Plan]:
    """Планы на шаг от текущего: замена человека в месте на свободного
    и обмен двух людей местами (между датами или задачами)."""
    keys = sorted(plan.slots)
    people = sorted(data.free)
    for key in keys:
        event_date, task = key
        busy = _busy(plan, event_date)
        for index, person in enumerate(plan.slots[key]):
            for other in people:
                if other in busy or not _can_serve(data, other, event_date, task):
                    continue
                moved = _copy(plan)
                moved.slots[key][index] = other
                yield moved
    for first_index, first in enumerate(keys):
        for second in keys[first_index + 1:]:
            for i, a in enumerate(plan.slots[first]):
                for j, b in enumerate(plan.slots[second]):
                    if a == b:
                        continue
                    if not (
                        _can_serve(data, b, first[0], first[1])
                        and _can_serve(data, a, second[0], second[1])
                    ):
                        continue
                    moved = _copy(plan)
                    moved.slots[first][i] = b
                    moved.slots[second][j] = a
                    if _one_task_per_day(moved):
                        yield moved


def _one_task_per_day(plan: Plan) -> bool:
    seen: set[tuple[date, str]] = set()
    for (event_date, _), people in plan.slots.items():
        for person in people:
            if (event_date, person) in seen:
                return False
            seen.add((event_date, person))
    return True


def _copy(plan: Plan) -> Plan:
    return Plan(
        slots={key: list(people) for key, people in plan.slots.items()},
        needed=plan.needed,
    )


def improve_plan(plan: Plan, data: PlanInput) -> None:
    """Локальные улучшения: покрытие не меняется, стоимость падает."""
    cost = plan_cost(plan, data)
    for _ in range(MAX_IMPROVE_ROUNDS):
        best = None
        best_cost = cost
        for candidate in _candidate_moves(plan, data):
            candidate_cost = plan_cost(candidate, data)
            if candidate_cost < best_cost:
                best, best_cost = candidate, candidate_cost
        if best is None:
            return
        plan.slots = best.slots
        cost = best_cost
