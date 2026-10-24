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
близкие служения (неделя, две) подряд у одного человека + RELUCTANT_WEIGHT
за каждое служение не ответившего (его ставят, только если ответивших не
хватает).

У задачи — минимум людей (`needed`, недобор — ⚠️) и сверх него до
максимума (`extra`). На каждую дату два прохода: сначала обязательные
места всех задач (ответившие дешевле не ответивших), затем места сверх
минимума — только оставшимися ответившими.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

LOAD_WEIGHT = 3
# Не ответивший на опрос свободен во все даты, но ставится строго после
# ответивших: вес больше любых сумм нагрузки и интервалов, поэтому среди
# планов с максимальным покрытием выбирается тот, где не ответивших
# меньше всего, и только затем — ровнее нагрузка и интервалы.
RELUCTANT_WEIGHT = 1_000_000
# Штраф за интервал между служениями человека, в неделях.
GAP_PENALTY = {1: 6, 2: 2}
# Чередование, когда людей больше, чем мест: кто служил в прошлом месяце
# позже, тот в этом ставится позже — штраф за первое служение месяца
# ROTATION_WEEKS − (недель от прошлого служения до начала месяца), не
# меньше 0. Меньше шага нагрузки (2 × LOAD_WEIGHT): ровнее нагрузка
# главнее чередования.
ROTATION_WEEKS = 5
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
    # Задача → сколько ещё людей сверх минимума можно поставить.
    extra: Mapping[str, int] = field(default_factory=dict)
    # Не ответившие на опрос: ставятся после ответивших.
    reluctant: frozenset[str] = frozenset()


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

    def covered_required(self) -> int:
        """Закрытые обязательные места (минимум задачи)."""
        return sum(
            min(len(people), self.needed[task])
            for (_, task), people in self.slots.items()
        )

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


def _rotation_penalty(data: PlanInput, person: str) -> int:
    """Штраф первого служения месяца: кто дольше не служил, тот первый."""
    previous = data.last_served.get(person)
    if previous is None or not data.dates:
        return 0
    return max(0, ROTATION_WEEKS - _weeks(previous, min(data.dates)))


def plan_cost(plan: Plan, data: PlanInput) -> int:
    served: dict[str, list[date]] = {}
    for (event_date, _), people in plan.slots.items():
        for person in people:
            served.setdefault(person, []).append(event_date)
    return sum(
        _person_cost(data, person, dates) for person, dates in served.items()
    )


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


def _seats(data: PlanInput) -> tuple[list[str], list[str]]:
    """Места на дату: обязательные (минимум задачи) и сверх минимума."""
    required = [
        task for task, count in data.needed.items() for _ in range(max(0, count))
    ]
    optional = [
        task for task, count in data.extra.items()
        if task in data.needed for _ in range(max(0, count))
    ]
    return required, optional


def build_plan(data: PlanInput) -> Plan:
    required, optional = _seats(data)
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

    def assign(event_date: date, people: list[str], seats: list[str]) -> set[str]:
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
            + (0 if load.get(person) else _rotation_penalty(data, person))
            + (RELUCTANT_WEIGHT if person in data.reluctant else 0)
            for person in people
        }
        matching = _min_cost_matching(people, seats, allowed, cost)
        for seat, person in sorted(matching.items()):
            plan.slots[(event_date, seats[seat])].append(person)
            load[person] = load.get(person, 0) + 1
            last[person] = event_date
        return set(matching.values())

    for event_date in sorted(set(data.dates)):
        people = sorted(
            person for person in data.free if event_date in data.free[person]
        )
        # Сначала минимум задач — все (ответившие дешевле); затем места
        # сверх минимума — только оставшиеся ответившие.
        busy = assign(event_date, people, required)
        assign(
            event_date,
            [
                person for person in people
                if person not in busy and person not in data.reluctant
            ],
            optional,
        )
    improve_plan(plan, data)
    for people in plan.slots.values():
        people.sort()
    return plan


def _person_cost(data: PlanInput, person: str, dates: Iterable[date]) -> int:
    """Вклад человека в стоимость плана: нагрузка² и штрафы интервала."""
    ordered = sorted(dates)
    total = LOAD_WEIGHT * len(ordered) ** 2
    if ordered:
        total += _rotation_penalty(data, person)
    if person in data.reluctant:
        total += RELUCTANT_WEIGHT * len(ordered)
    previous = data.last_served.get(person)
    for event_date in ordered:
        total += _gap_penalty(previous, event_date)
        previous = event_date
    return total


def improve_plan(plan: Plan, data: PlanInput) -> None:
    """Локальные улучшения: покрытие не меняется, стоимость падает.

    Ходы — замена человека в месте на свободного и обмен двух людей
    местами в разные даты (обмен в одну дату стоимость не меняет).
    Ход затрагивает не больше двух людей, поэтому оценивается
    пересчётом только их вклада; план меняется лишь лучшим ходом.
    """
    served: dict[str, set[date]] = {}
    for (event_date, _), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
    people_all = sorted(data.free)

    def cost_of(person: str, dates: set[date]) -> int:
        return _person_cost(data, person, dates)

    for _ in range(MAX_IMPROVE_ROUNDS):
        positions = [
            (key, index, person)
            for key in sorted(plan.slots)
            for index, person in enumerate(plan.slots[key])
        ]
        busy: dict[date, set[str]] = {}
        for (event_date, _), _, person in positions:
            busy.setdefault(event_date, set()).add(person)
        best_delta = 0
        best_move = None
        for key, index, person in positions:
            event_date, task = key
            mine = served[person]
            before_mine = cost_of(person, mine)
            after_mine = cost_of(person, mine - {event_date})
            for other in people_all:
                if other in busy[event_date] or not _can_serve(
                    data, other, event_date, task
                ) or (other in data.reluctant and index >= plan.needed[task]):
                    continue
                theirs = served.get(other, set())
                delta = (
                    after_mine - before_mine
                    + cost_of(other, theirs | {event_date})
                    - cost_of(other, theirs)
                )
                if delta < best_delta:
                    best_delta, best_move = delta, ("replace", key, index, other)
        for first in range(len(positions)):
            key_a, index_a, a = positions[first]
            for second in range(first + 1, len(positions)):
                key_b, index_b, b = positions[second]
                date_a, date_b = key_a[0], key_b[0]
                if date_a == date_b or a == b:
                    continue
                if (
                    b in busy[date_a] or a in busy[date_b]
                    or not _can_serve(data, b, date_a, key_a[1])
                    or not _can_serve(data, a, date_b, key_b[1])
                    or (b in data.reluctant
                        and index_a >= plan.needed[key_a[1]])
                    or (a in data.reluctant
                        and index_b >= plan.needed[key_b[1]])
                ):
                    continue
                dates_a, dates_b = served[a], served[b]
                delta = (
                    cost_of(a, (dates_a - {date_a}) | {date_b})
                    + cost_of(b, (dates_b - {date_b}) | {date_a})
                    - cost_of(a, dates_a) - cost_of(b, dates_b)
                )
                if delta < best_delta:
                    best_delta = delta
                    best_move = ("swap", key_a, index_a, key_b, index_b)
        if best_move is None:
            return
        if best_move[0] == "replace":
            _, key, index, other = best_move
            person = plan.slots[key][index]
            plan.slots[key][index] = other
            served[person].discard(key[0])
            served.setdefault(other, set()).add(key[0])
        else:
            _, key_a, index_a, key_b, index_b = best_move
            a, b = plan.slots[key_a][index_a], plan.slots[key_b][index_b]
            plan.slots[key_a][index_a], plan.slots[key_b][index_b] = b, a
            served[a] = (served[a] - {key_a[0]}) | {key_b[0]}
            served[b] = (served[b] - {key_b[0]}) | {key_a[0]}
