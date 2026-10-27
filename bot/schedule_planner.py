"""Распределение отметившихся по воскресеньям и задачам (этап 5.4).

Правила заказчика:
1. В один день у человека не больше одной задачи служения.
2. Основной задачи нет — человек идёт туда, где он нужнее.
3. Нагрузка в месяце выравнивается.
4. Между служениями человека — как можно больший интервал, с учётом
   прошлого месяца. Служение две недели подряд — только если иначе
   место не закрыть: интервал важнее ответа на опрос.

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
хватает). Штраф за неделю подряд (BACK_TO_BACK_WEIGHT) больше
RELUCTANT_WEIGHT: лучше не ответивший, чем служение подряд.

У задачи — минимум людей (`needed`, недобор — ⚠️) и сверх него до
максимума (`extra`). Два прохода по месяцу: сначала обязательные места
всех задач на все даты (ответившие дешевле не ответивших), затем места
сверх минимума — только ответившими, у кого нет служения ни за неделю
до, ни через неделю после (иначе место сверх минимума остаётся пустым).

Задачи «в одну неделю» (решение 12: звук в вс и спевка в ср — один
человек): у кого они связаны, тот ставится на них вместе — одно составное
место, одно служение в неделю. Места связки, которые некем закрыть
вместе, закрываются по отдельности.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date

LOAD_WEIGHT = 3
# Не ответивший на опрос свободен во все даты, но ставится строго после
# ответивших: вес больше любых сумм нагрузки и интервалов, поэтому среди
# планов с максимальным покрытием выбирается тот, где не ответивших
# меньше всего, и только затем — ровнее нагрузка и интервалы.
RELUCTANT_WEIGHT = 1_000_000
# Служение неделю подряд (решение 12): дороже любого числа не ответивших
# в месяце — ставится, только если место иначе остаётся пустым.
BACK_TO_BACK_WEIGHT = 100 * RELUCTANT_WEIGHT
# Штраф за интервал между служениями человека, в неделях.
GAP_PENALTY = {1: BACK_TO_BACK_WEIGHT, 2: 2}
# Чередование, когда людей больше, чем мест: кто служил в прошлом месяце
# позже, тот в этом ставится позже — штраф за первое служение месяца
# ROTATION_WEEKS − (недель от прошлого служения до начала месяца), не
# меньше 0. Меньше шага нагрузки (2 × LOAD_WEIGHT): ровнее нагрузка
# главнее чередования.
ROTATION_WEEKS = 5
# Лимит служений в месяц (решение 12): максимум — жёсткий (сверх него
# человек не ставится, даже если отметил все даты), минимум — штраф за
# каждое недостающее служение: больше шагов нагрузки и интервала в две
# недели, меньше служения подряд и не ответившего.
BELOW_MIN_WEIGHT = 50
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
    # Человек → (минимум, максимум) служений в месяц; максимум None — без
    # ограничения. Нет записи — без лимита.
    limits: Mapping[str, tuple[int, int | None]] = field(default_factory=dict)
    # Человек → задачи, которые он берёт в одну неделю (звук + спевка).
    bundles: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass
class Plan:
    # (дата, задача) → люди в порядке назначения.
    slots: dict[tuple[date, str], list[str]]
    needed: Mapping[str, int]

    def load(self) -> dict[str, int]:
        """Служений (дат) у человека: связка задач в дату — одно."""
        dates: dict[str, set[date]] = {}
        for (event_date, _), people in self.slots.items():
            for person in people:
                dates.setdefault(person, set()).add(event_date)
        return {person: len(days) for person, days in dates.items()}

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
    # Интервал — в неделях графика: даты месяца — воскресенья, прошлое
    # служение — фактический день. День до воскресенья своей недели (свой
    # день человека или задачи, вс −1…−6) даёт то же число недель.
    return (later - earlier).days // 7


def _gap_penalty(earlier: date | None, later: date) -> int:
    if earlier is None:
        return 0
    # Меньше недели (служение в начале месяца с прошлого) — как неделя.
    return GAP_PENALTY.get(max(1, _weeks(earlier, later)), 0)


def _limit(data: PlanInput, person: str) -> tuple[int, int | None]:
    return data.limits.get(person, (0, None))


def _below_max(data: PlanInput, person: str, count: int) -> bool:
    """Ещё одно служение не превысит месячный максимум человека."""
    high = _limit(data, person)[1]
    return high is None or count < high


def _rotation_penalty(data: PlanInput, person: str) -> int:
    """Штраф первого служения месяца: кто дольше не служил, тот первый."""
    previous = data.last_served.get(person)
    if previous is None or not data.dates:
        return 0
    weeks = max(0, _weeks(previous, min(data.dates)))
    return max(0, ROTATION_WEEKS - weeks)


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


# Составное место связки задач во внутреннем плане: «звук\x1fспевка».
BUNDLE_SEPARATOR = "\x1f"


def build_plan(data: PlanInput) -> Plan:
    """План месяца; связки задач «в одну неделю» — составными местами."""
    groups = sorted(
        {
            bundle for bundle in data.bundles.values()
            if len(bundle) > 1 and bundle <= set(data.needed)
        },
        key=lambda bundle: (
            -sum(value == bundle for value in data.bundles.values()),
            sorted(bundle),
        ),
    )
    if not groups:
        return _build_plan(data)
    needed = dict(data.needed)
    tasks = {person: set(items) for person, items in data.tasks.items()}
    virtual: dict[str, list[str]] = {}
    for bundle in groups:
        count = min(needed[task] for task in bundle)
        if count <= 0:
            continue
        members = [task for task in data.needed if task in bundle]
        name = BUNDLE_SEPARATOR.join(members)
        virtual[name] = members
        needed[name] = count
        for task in members:
            needed[task] -= count
        # Составное место может занять и человек без связки — тогда он
        # берёт одну задачу связки, остальные закрываются по отдельности:
        # иначе единственного со связкой ставили бы каждую неделю.
        for person, own in tasks.items():
            if own & bundle:
                own.add(name)
    inner = _build_plan(replace(
        data,
        needed={
            task: count for task, count in needed.items()
            if count > 0 or data.extra.get(task, 0) > 0
        },
        tasks={person: frozenset(items) for person, items in tasks.items()},
        # Места сверх минимума — после обязательных мест связок: иначе
        # лишнее место могло бы вынудить служить две недели подряд.
        extra={},
    ))
    plan = Plan(
        slots={
            (event_date, task): []
            for event_date in data.dates
            for task in data.needed
        },
        needed=data.needed,
    )
    for (event_date, task), people in inner.slots.items():
        if task not in virtual:
            plan.slots[(event_date, task)].extend(people)
            continue
        members = virtual[task]
        for person in people:
            if data.bundles.get(person) == frozenset(members):
                chosen = members
            else:
                # Без связки — одна задача: где на эту дату меньше людей.
                chosen = [min(
                    (
                        member for member in members
                        if member in data.tasks.get(person, ())
                    ),
                    key=lambda member: (
                        len(plan.slots[(event_date, member)]),
                        members.index(member),
                    ),
                )]
            for member in chosen:
                plan.slots[(event_date, member)].append(person)
    _fill_bundle_gaps(plan, data)
    _fill_optional(plan, data)
    for people in plan.slots.values():
        people.sort()
    # Закрыть места важнее связки: если без связок закрывается больше
    # обязательных мест — план без них.
    plain = _build_plan(replace(data, bundles={}))
    if plain.covered_required() > plan.covered_required():
        return plain
    return plan


def _fill_bundle_gaps(plan: Plan, data: PlanInput) -> None:
    """Обязательные места связки, которые некем было закрыть вместе, —
    по отдельности. Сначала тот, кто в эту дату уже на задаче своей связки
    (ещё одна задача связки — не лишнее служение); затем свободный в дату,
    без служения в неё, в пределах максимума: без служения неделей раньше
    или позже (интервал важнее ответа на опрос), ответившие, без связки с
    этой задачей, с меньшей нагрузкой."""
    served: dict[str, set[date]] = {}
    on_date: dict[tuple[str, date], set[str]] = {}
    for (event_date, task), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
            on_date.setdefault((person, event_date), set()).add(task)
    for event_date in sorted(set(data.dates)):
        for task, count in data.needed.items():
            people = plan.slots[(event_date, task)]
            while len(people) < count:
                def extends(person: str) -> bool:
                    current = on_date.get((person, event_date))
                    return bool(current) and current | {task} <= data.bundles.get(
                        person, frozenset()
                    )

                candidates = [
                    person for person in data.free
                    if _can_serve(data, person, event_date, task)
                    and person not in people
                    and (
                        extends(person)
                        or event_date not in served.get(person, set())
                        and _below_max(
                            data, person, len(served.get(person, ()))
                        )
                    )
                ]
                if not candidates:
                    break

                def rank(person: str) -> tuple:
                    days = served.get(person, set())
                    previous = data.last_served.get(person)
                    close = any(
                        abs((other - event_date).days) <= 7 for other in days
                    ) or _gap_penalty(previous, event_date) >= BACK_TO_BACK_WEIGHT
                    # Связавший задачи — по отдельности в последнюю очередь.
                    bound = task in data.bundles.get(person, ())
                    return (
                        not extends(person), close, person in data.reluctant,
                        bound, len(days), person,
                    )

                chosen = min(candidates, key=rank)
                people.append(chosen)
                served.setdefault(chosen, set()).add(event_date)
                on_date.setdefault((chosen, event_date), set()).add(task)


def _fill_optional(plan: Plan, data: PlanInput) -> None:
    """Места сверх минимума при связках — как второй проход основного
    алгоритма: только ответившие, без служения ни за неделю до, ни через
    неделю после, в пределах максимума; сначала с меньшей нагрузкой."""
    served: dict[str, set[date]] = {}
    for (event_date, _), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
    for event_date in sorted(set(data.dates)):
        for task, extra in data.extra.items():
            if task not in data.needed:
                continue
            people = plan.slots[(event_date, task)]
            while len(people) < data.needed[task] + extra:
                candidates = [
                    person for person in data.free
                    if _can_serve(data, person, event_date, task)
                    and person not in data.reluctant
                    and not any(
                        abs((other - event_date).days) <= 7
                        for other in served.get(person, set())
                    )
                    and _gap_penalty(data.last_served.get(person), event_date)
                    < BACK_TO_BACK_WEIGHT
                    and _below_max(data, person, len(served.get(person, ())))
                ]
                if not candidates:
                    break
                chosen = min(
                    candidates,
                    key=lambda person: (len(served.get(person, ())), person),
                )
                people.append(chosen)
                served.setdefault(chosen, set()).add(event_date)


def _build_plan(data: PlanInput) -> Plan:
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

    def assign(
        event_date: date, people: list[str], seats: list[str],
        cost: dict[str, int],
    ) -> None:
        allowed = {
            person: {
                index for index, task in enumerate(seats)
                if _can_serve(data, person, event_date, task)
            }
            for person in people
        }
        matching = _min_cost_matching(people, seats, allowed, cost)
        for seat, person in sorted(matching.items()):
            plan.slots[(event_date, seats[seat])].append(person)
            load[person] = load.get(person, 0) + 1
            last[person] = event_date

    def free_on(event_date: date) -> list[str]:
        return sorted(
            person for person in data.free if event_date in data.free[person]
        )

    # Сначала минимум задач на весь месяц — все (ответившие дешевле).
    for event_date in sorted(set(data.dates)):
        people = [
            person for person in free_on(event_date)
            if _below_max(data, person, load.get(person, 0))
        ]
        # Прирост общей стоимости от ещё одного служения человека (у
        # недобравшего минимум — дешевле на BELOW_MIN_WEIGHT).
        assign(event_date, people, required, {
            person: LOAD_WEIGHT * (2 * load.get(person, 0) + 1)
            + _gap_penalty(last.get(person), event_date)
            + (0 if load.get(person) else _rotation_penalty(data, person))
            + (RELUCTANT_WEIGHT if person in data.reluctant else 0)
            + (0 if load.get(person, 0) < _limit(data, person)[0]
               else BELOW_MIN_WEIGHT)
            for person in people
        })
    repair_gaps(plan, data)
    improve_plan(plan, data)
    # Затем места сверх минимума — только ответившими и без служения
    # подряд ни с прошлой, ни со следующей датой: место сверх минимума
    # можно и не закрывать, а закрытое не должно вынуждать служить подряд.
    served: dict[str, set[date]] = {}
    for (event_date, _), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
    for event_date in sorted(set(data.dates)):
        people = [
            person for person in free_on(event_date)
            if person not in data.reluctant
            and not any(
                abs((other - event_date).days) <= 7
                for other in served.get(person, set())
            )
            and _gap_penalty(data.last_served.get(person), event_date)
            < BACK_TO_BACK_WEIGHT
            and _below_max(data, person, len(served.get(person, ())))
        ]
        # BELOW_MIN_WEIGHT — чтобы стоимость не была отрицательной.
        assign(event_date, people, optional, {
            person: BELOW_MIN_WEIGHT + _person_cost(
                data, person, served.get(person, set()) | {event_date}
            ) - _person_cost(data, person, served.get(person, set()))
            for person in people
        })
        for (day, _), names in plan.slots.items():
            if day == event_date:
                for person in names:
                    served.setdefault(person, set()).add(day)
    improve_plan(plan, data)
    for people in plan.slots.values():
        people.sort()
    return plan


def _person_cost(data: PlanInput, person: str, dates: Iterable[date]) -> int:
    """Вклад человека в стоимость плана: нагрузка² и штрафы интервала."""
    ordered = sorted(dates)
    total = LOAD_WEIGHT * len(ordered) ** 2
    total += BELOW_MIN_WEIGHT * max(0, _limit(data, person)[0] - len(ordered))
    if ordered:
        total += _rotation_penalty(data, person)
    if person in data.reluctant:
        total += RELUCTANT_WEIGHT * len(ordered)
    previous = data.last_served.get(person)
    for event_date in ordered:
        total += _gap_penalty(previous, event_date)
        previous = event_date
    return total


def repair_gaps(plan: Plan, data: PlanInput) -> None:
    """Обязательные места, оставшиеся пустыми из-за месячного максимума,
    закрываются перестановками по всему месяцу: план — поток «исток →
    человек (до его максимума) → (человек, дата) → место (до минимума
    задачи) → сток», и он дополняется увеличивающими путями (Форд —
    Фалкерсон) до максимального. Путь переставляет только людей своей
    цепочки (другая задача в ту же дату, другая дата), остальные остаются
    на местах; поиск в ширину — кратчайшие цепочки."""
    source, sink = ("source",), ("sink",)
    capacity: dict[tuple, int] = {}
    edges: dict[tuple, list[tuple]] = {}

    def add(start: tuple, end: tuple, amount: int) -> None:
        if (start, end) not in capacity:
            edges.setdefault(start, []).append(end)
            edges.setdefault(end, []).append(start)
            capacity.setdefault((end, start), 0)
        capacity[(start, end)] = capacity.get((start, end), 0) + amount

    seats = sorted(plan.slots)
    for person in sorted(data.free):
        high = _limit(data, person)[1]
        add(source, ("person", person), len(seats) if high is None else high)
        for event_date in sorted(data.free[person]):
            add(("person", person), ("day", person, event_date), 1)
            for key in seats:
                if key[0] == event_date and _can_serve(
                    data, person, event_date, key[1]
                ):
                    add(("day", person, event_date), ("seat", key), 1)
    for key in seats:
        add(("seat", key), sink, plan.needed[key[1]])
    # Текущий план — уже пущенный поток.
    for key in seats:
        for person in plan.slots[key]:
            for start, end in (
                (source, ("person", person)),
                (("person", person), ("day", person, key[0])),
                (("day", person, key[0]), ("seat", key)),
                (("seat", key), sink),
            ):
                capacity[(start, end)] -= 1
                capacity[(end, start)] += 1
    while True:
        came: dict[tuple, tuple | None] = {source: None}
        queue = [source]
        for node in queue:
            for end in edges.get(node, ()):
                if end not in came and capacity[(node, end)] > 0:
                    came[end] = node
                    queue.append(end)
        if sink not in came:
            break
        node = sink
        while came[node] is not None:
            start = came[node]
            capacity[(start, node)] -= 1
            capacity[(node, start)] += 1
            node = start
    # Назначение — поток по ребру (человек, дата) → место; уже стоявшие
    # остаются в прежнем порядке, новые — в конце.
    for key in seats:
        assigned = {
            node[1] for node in edges.get(("seat", key), ())
            if node[0] == "day" and capacity[(("seat", key), node)] > 0
        }
        plan.slots[key] = [
            person for person in plan.slots[key] if person in assigned
        ] + sorted(assigned - set(plan.slots[key]))


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
                theirs = served.get(other, set())
                if other in busy[event_date] or not _can_serve(
                    data, other, event_date, task
                ) or (
                    other in data.reluctant and index >= plan.needed[task]
                ) or not _below_max(data, other, len(theirs)):
                    continue
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
