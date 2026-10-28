"""Предложенный график по итогам опроса готовности (этап 5.4–5.5).

По сроку опроса бот распределяет отметившихся (`readiness.summarize`)
с учётом прошлого месяца и сохраняет предложение в `schedule_plans`.
Админ служения правит его («✏️ Изменить»), пересчитывает и утверждает:
график публикуется в чат служения, админу — блок для переноса в Excel.

Слоты графика: дата (ISO) → задача → ключи людей (`readiness_people`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, timedelta
from io import BytesIO

from .excel_parser import ServiceAssignment, ServiceEvent, _schedule_sheet_name
from .group_send import send_to_service_group
from .month_summary import month_end, month_key, month_title
from .presentation import task_emoji
from .readiness import (
    DEFAULT_PLAN_TITLE,
    DEFAULT_PLAN_UPDATE_TITLE,
    Summary,
    month_sundays,
    local_text,
    month_of,
    outside_name,
    phrase,
    render_template,
    summarize,
    task_lines,
)
from .services import (
    ScheduleColumn, normalized_name, split_day_key, task_day_key,
)
from .ui import send_telegram_screen, user_link


# Сколько ждать график прошлого месяца (секунды).
LOADER_TIMEOUT = 30


class PublishedNotSaved(RuntimeError):
    """График ушёл в чат, но отметка об утверждении не сохранилась."""


def revision(plan: dict) -> str:
    """Версия графика для кнопок: правка или пересчёт меняют её, и
    кнопки с прежней версией считаются устаревшими."""
    data = json.dumps(
        [plan["updated_at"], plan["run_id"], plan["slots"],
         plan.get("days", {})],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(data.encode()).hexdigest()[:6]


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _short(day: str) -> str:
    return date.fromisoformat(day).strftime("%d.%m")


def served_dates(slots: dict) -> dict[str, list[str]]:
    """Ключ человека → даты, когда он стоит в графике (связка задач «в
    одну неделю» в дату — одна дата)."""
    result: dict[str, set[str]] = {}
    for day, tasks in slots.items():
        for people in tasks.values():
            for person in people:
                result.setdefault(person, set()).add(day)
    return {person: sorted(days) for person, days in result.items()}


async def last_served(
    registry, service_id: str, year: int, month: int, people: list[dict],
    events_loader, logger: logging.Logger,
) -> tuple[dict[str, str], str | None]:
    """Последнее служение каждого в месяце перед (year, month): по
    утверждённому графику бота, иначе по Excel. Второе — пометка, если
    прошлый месяц учесть не удалось."""
    py, pm = previous_month(year, month)
    previous = registry.get_schedule_plan(service_id, month_key(py, pm))
    if previous is not None and previous["approved"] is not None:
        # Фактический день служения — как был при публикации (разовый или
        # свой день), иначе воскресенье графика.
        served = previous.get("served_offsets") or {}
        snapshot = bool(previous.get("served_snapshot") or served)
        by_key = {person["key"]: person for person in people}
        approved_days = previous.get("approved_days") or {}

        def offset_of(person: str, day: str) -> int:
            if snapshot:
                return served.get(day, {}).get(person) or 0
            # Утверждён до снимка: разовый день на дату, иначе свой день.
            offset = approved_days.get(day, {}).get(person)
            if offset is None:
                offset = (by_key.get(person) or {}).get("day_offset")
            return offset or 0

        result = {}
        for person, days in served_dates(previous["approved"]).items():
            result[person] = max(
                date.fromisoformat(day) + timedelta(days=offset_of(person, day))
                for day in days
            ).isoformat()
        return result, None
    if events_loader is None:
        return {}, "прошлый месяц не учтён — график не загружен"
    first, last = date(py, pm, 1), month_end(py, pm)
    try:
        # Под service_lock: зависшая загрузка не должна держать служение.
        events = await asyncio.wait_for(
            events_loader(since=first, until=last), LOADER_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "График прошлого месяца не загружен для интервалов", exc_info=True
        )
        return {}, "прошлый месяц не учтён — график не загрузился"
    # Имя в Excel → ключ; одинаковые имена у разных людей — неоднозначно.
    by_name: dict[str, list[str]] = {}
    for person in people:
        by_name.setdefault(normalized_name(person["name"]), []).append(
            person["key"]
        )
    result: dict[str, str] = {}
    for event in events:
        if event.service_id != service_id or not first <= event.event_date <= last:
            continue
        for assignment in event.assignments:
            if assignment.is_informational or not assignment.name:
                continue
            keys = by_name.get(normalized_name(assignment.name), [])
            if len(keys) != 1:
                continue
            day = event.event_date.isoformat()
            result[keys[0]] = max(result.get(keys[0], day), day)
    return result, None


def plan_tasks(registry, service_id: str) -> list[dict]:
    """Распределяемые задачи в порядке каталога: минимум и максимум
    (служение без задач — одна задача с его названием)."""
    return [
        {
            "name": task["name"], "needed": task["needed"], "max": task["max"],
            "implicit": task["implicit"], "day": task.get("day"),
        }
        for task in registry.plan_tasks(service_id)
    ]


def implicit_plan(plan: dict) -> bool:
    """График служения без задач: одна задача с названием служения —
    в тексте и правке люди сразу под датой."""
    return len(plan["tasks"]) == 1 and bool(plan["tasks"][0].get("implicit"))


def free_run(service_id: str, year: int, month: int) -> dict:
    """Опрос-заглушка графика без опроса: все воскресенья месяца."""
    return {
        "id": None, "service_id": service_id,
        "month": month_key(year, month), "status": "free",
        "dates": [day.isoformat() for day in month_sundays(year, month)],
    }


def plan_run(plan: dict, run: dict | None) -> dict:
    """Опрос, по которому составлен график (без опроса — заглушка)."""
    if plan["run_id"] is None:
        year, month = map(int, plan["month"].split("-"))
        return {**free_run(plan["service_id"], year, month),
                "dates": list(plan["dates"])}
    return run


def plan_summary(registry, plan: dict, run: dict | None) -> Summary:
    """Состав и ответы для экранов графика; без опроса — все свободны."""
    return summarize(
        registry, plan_run(plan, run), with_plan=False,
        assume_free=plan["run_id"] is None,
    )


async def build_draft(
    registry, run: dict, events_loader, logger: logging.Logger,
) -> dict:
    """Предложенный график по ответам опроса (run["id"] is None — без
    опроса, все свободны); ручные правки сбрасываются."""
    year, month = month_of(run)
    people = registry.readiness_people(run["service_id"])
    served, note = await last_served(
        registry, run["service_id"], year, month, people, events_loader,
        logger,
    )
    summary = summarize(
        registry, run,
        last_served={key: date.fromisoformat(day) for key, day in served.items()},
        assume_free=run["id"] is None,
    )
    tasks = plan_tasks(registry, run["service_id"])
    slots = {
        day: {
            task["name"]: list(
                summary.plan.slots.get((date.fromisoformat(day), task["name"]), [])
            )
            for task in tasks
        }
        for day in run["dates"]
    }
    return registry.save_schedule_draft(
        run["service_id"], month_key(year, month),
        run_id=run["id"], dates=list(run["dates"]), tasks=tasks, slots=slots,
        last_served=served, note=note,
    )


async def ensure_plan(
    registry, run: dict, events_loader, logger: logging.Logger,
) -> dict:
    """График закрытого опроса: сохранённый или (нет, опрос перезапущен)
    — новый предложенный."""
    plan = registry.get_schedule_plan(run["service_id"], month_key(*month_of(run)))
    if plan is not None and plan["run_id"] == run["id"]:
        return sync_implicit_task(registry, plan)
    return await build_draft(registry, run, events_loader, logger)


def sync_implicit_task(registry, plan: dict) -> dict:
    """Служение без задач переименовано в каталоге — задача графика
    (его название) переименовывается вслед, правки сохраняются."""
    current = registry.plan_tasks(plan["service_id"])
    if (
        len(current) == 1 and current[0]["implicit"]
        and len(plan["tasks"]) == 1
        and (
            plan["tasks"][0]["name"] != current[0]["name"]
            or not plan["tasks"][0].get("implicit")
        )
    ):
        return registry.rename_plan_task(
            plan["service_id"], plan["month"], plan["tasks"][0]["name"],
            current[0]["name"],
        )
    return plan


def _names(people: list[dict]) -> dict[str, str]:
    return {person["key"]: person["name"] for person in people}


def covered(plan: dict) -> tuple[int, int]:
    """(закрыто обязательных мест, всего обязательных)."""
    done = total = 0
    for tasks in plan["slots"].values():
        for task in plan["tasks"]:
            people = tasks.get(task["name"], [])
            done += min(len(people), task["needed"])
            total += task["needed"]
    return done, total


def gaps(plan: dict) -> list[str]:
    lines = []
    for day in plan["dates"]:
        for task in plan["tasks"]:
            count = len(plan["slots"].get(day, {}).get(task["name"], []))
            if count < task["needed"]:
                lines.append(
                    f"⚠️ {_short(day)} «{task['name']}»: "
                    + ("никого" if not count else f"{count} из {task['needed']}")
                )
    return lines


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _weekday_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def day_label(day: str, window: tuple[int, int] = (0, 0)) -> str:
    """Дата графика; с окном дней служения — «01.11 (сб 31.10 – пн 02.11)»."""
    sunday = date.fromisoformat(day)
    start, end = window
    if (start, end) == (0, 0):
        return _short(day)
    return (
        f"{_short(day)} ({_weekday_date(sunday + timedelta(days=start))} – "
        f"{_weekday_date(sunday + timedelta(days=end))})"
    )


def day_offset(plan: dict | None, person: dict | None, day: str) -> int | None:
    """День человека на дату: разовый в графике, иначе личный."""
    key = (person or {}).get("key")
    override = (plan or {}).get("days", {}).get(day, {}).get(key)
    if override is not None:
        return override
    return (person or {}).get("day_offset")


def task_days(plan: dict) -> dict[str, int]:
    """Свои дни задач графика (спевка — вс −4)."""
    return {
        task["name"]: task["day"] for task in plan["tasks"]
        if task.get("day") is not None
    }


def slot_offset(plan: dict, person: dict | None, day: str,
                task: str) -> int | None:
    """День человека в задаче на дату: разовый для задачи, разовый в
    графике, иначе день задачи, иначе личный."""
    key = (person or {}).get("key")
    overrides = plan.get("days", {}).get(day, {})
    override = overrides.get(task_day_key(str(key), task))
    if override is None:
        override = overrides.get(key)
    if override is not None:
        return override
    own = task_days(plan).get(task)
    if own is not None:
        return own
    return (person or {}).get("day_offset")


def served_offsets(plan: dict, people: list[dict]) -> dict:
    """Фактический сдвиг дня служения каждого поставленного (разовый, день
    задачи или свой день) — снимок при публикации. У человека в нескольких
    задачах даты — самый поздний день."""
    by_key = {person["key"]: person for person in people}
    latest: dict[tuple[str, str], int] = {}
    for day, tasks in plan["slots"].items():
        for task, keys in tasks.items():
            for key in keys:
                offset = slot_offset(plan, by_key.get(key), day, task) or 0
                latest[day, key] = max(offset, latest.get((day, key), offset))
    result: dict = {}
    for (day, key), offset in latest.items():
        if offset:
            result.setdefault(day, {})[key] = offset
    return result


def week_groups(plan: dict, day: str, window: tuple[int, int]
                ) -> tuple[str, list[tuple[str, int | None, list[dict]]]]:
    """График по неделям (у задач свои дни): «Неделя 26.10–01.11» и дни
    недели по порядку, у каждого — его задачи. Воскресенье — правая граница
    недели; задачи без своего дня — в день (или окно) служения. Элемент:
    подпись дня, сдвиг дня (None — окно служения), задачи."""
    sunday = date.fromisoformat(day)
    own = task_days(plan)
    groups: dict[int | None, list[dict]] = {}
    base = None if window != (0, 0) else 0
    for task in plan["tasks"]:
        groups.setdefault(own.get(task["name"], base), []).append(task)
    offsets = [offset for offset in groups if offset is not None]
    last = max([0, window[1] if None in groups else 0, *offsets])
    title = (
        f"Неделя {sunday - timedelta(days=6):%d.%m}–"
        f"{sunday + timedelta(days=last):%d.%m}"
    )
    result = []
    # Окно служения — после задач со своим днём в тот же день.
    for offset in sorted(
        groups,
        key=lambda value: (window[0], 1) if value is None else (value, 0),
    ):
        if offset is None:
            label = (
                f"{_weekday_date(sunday + timedelta(days=window[0]))} – "
                f"{_weekday_date(sunday + timedelta(days=window[1]))}"
            )
        else:
            label = _weekday_date(sunday + timedelta(days=offset))
        result.append((label, offset, groups[offset]))
    return title, result


def week_events(events: list, own_days: dict[str, int]) -> list[dict]:
    """События графика Excel по воскресеньям: задача со своим днём (спевка
    — вс −4, в файле — отдельной средой) — в блоке своего воскресенья.
    Элемент: дата (воскресенье), назначения и фактический день задач,
    отличный от даты."""
    blocks: dict[date, dict] = {}
    for event in sorted(events, key=lambda item: item.event_date):
        if not event.assignments:
            # Дата без назначений — тоже в обзоре.
            blocks.setdefault(
                event.event_date,
                {"date": event.event_date, "assignments": [], "days": {},
                 "moments": []},
            )
        for assignment in event.assignments:
            offset = own_days.get(assignment.task)
            sunday = event.event_date
            if offset is not None and event.event_date.weekday() != 6:
                candidate = event.event_date - timedelta(days=offset)
                if candidate.weekday() == 6:
                    sunday = candidate
            block = blocks.setdefault(
                sunday,
                {"date": sunday, "assignments": [], "days": {}, "moments": []},
            )
            block["assignments"].append(assignment)
            # Дата каждого назначения — как в файле.
            block["moments"].append(event.event_date)
            if event.event_date != sunday:
                block["days"][assignment.task] = event.event_date
    return [blocks[day] for day in sorted(blocks)]


def week_person_day(plan: dict, person: dict | None, day: str, task: str,
                    group_offset: int | None) -> str:
    """День человека в графике по неделям — в скобках, только если он не
    совпадает с днём строки."""
    offset = slot_offset(plan, person, day, task)
    if offset is None or offset == group_offset:
        return ""
    return (
        " (" + _weekday_date(date.fromisoformat(day) + timedelta(days=offset))
        + ")"
    )


def person_day(person: dict | None, day: str, plan: dict | None = None) -> str:
    """« (чт 29.10)» для человека с личным (или разовым) днём служения —
    в скобках, как окно у даты."""
    offset = day_offset(plan, person, day)
    if offset is None:
        return ""
    return (
        " (" + _weekday_date(date.fromisoformat(day) + timedelta(days=offset))
        + ")"
    )


def date_header(plan: dict, by_key: dict, day: str,
                window: tuple[int, int]) -> tuple[str, bool]:
    """Заголовок даты и скрывать ли дни у людей. У всех поставленных один
    и тот же свой день — он в скобках у даты («29.11 (чт 26.11)»), у имён
    не повторяется; у всех свои, но разные — дата без периода; иначе —
    дата с окном служения."""
    people = [
        key for keys in plan["slots"].get(day, {}).values() for key in keys
    ]
    offsets = {day_offset(plan, by_key.get(key), day) for key in people}
    if people and None not in offsets and len(offsets) == 1:
        (offset,) = offsets
        moment = date.fromisoformat(day) + timedelta(days=offset)
        return f"{_short(day)} ({_weekday_date(moment)})", True
    if people and None not in offsets:
        return _short(day), False
    return day_label(day, window), False


def window_of(registry, service_id: str) -> tuple[int, int]:
    settings = registry.get_readiness_settings(service_id)
    return settings["day_from"], settings["day_to"]


def plan_text(service_label: str, plan: dict, summary: Summary, tz,
              window: tuple[int, int] = (0, 0)) -> str:
    """window — окно дней служения от воскресенья (Уборка: −1, +1)."""
    year, month = map(int, plan["month"].split("-"))
    names = _names(summary.people)
    by_key = {person["key"]: person for person in summary.people}
    silent = {person["key"] for person in summary.not_answered}
    polled = [person for person in summary.people if person["tasks"]]
    used_unpicked = False

    def mark(person: str, day: str) -> str:
        """* — не ответил; ° — ответил, но эту дату не отмечал (решение 16:
        его даты заняли другие — вместо не ответившего; или вручную)."""
        nonlocal used_unpicked
        if person in silent:
            return "*"
        if plan["run_id"] is not None and person in summary.answered and (
            day not in summary.answered[person]
        ):
            used_unpicked = True
            return "°"
        return ""

    done, total = covered(plan)
    if plan["approved"] is None:
        status = "📝 Черновик — ещё не утверждён"
    elif plan["dirty"]:
        status = (
            "✏️ Есть правки после утверждения — «✅ Утвердить» опубликует "
            "обновление"
        )
    else:
        status = f"✅ Утверждён {local_text(plan['approved_at'], tz)}"
    lines = [
        f"📋 График · {service_label} · {month_title(year, month)}",
        status,
        "",
        (
            "📥 Загружен из Excel — напоминания и замены по нему"
            if plan.get("source") == "excel"
            else "🧮 Без опроса: все из состава считаются свободными"
            if plan["run_id"] is None
            else f"Ответили {len(summary.answered)} из {len(polled)}"
        ) + f" · закрыто мест {done} из {total}",
    ]
    used_silent = False
    weekly = bool(task_days(plan))
    for day in plan["dates"]:
        if weekly:
            title, groups = week_groups(plan, day, window)
            lines += ["", f"🗓 {title}"]
            for label, offset, tasks in groups:
                lines.append(f"   {label}")
                for task in tasks:
                    people = plan["slots"].get(day, {}).get(task["name"], [])
                    used_silent |= any(person in silent for person in people)
                    lines += [
                        "  " + line for line in task_lines(
                            task["name"], task["needed"],
                            [
                                names.get(person, person)
                                + mark(person, day)
                                + week_person_day(
                                    plan, by_key.get(person), day,
                                    task["name"], offset,
                                )
                                for person in people
                            ],
                        )
                    ]
            continue
        header, shared_day = date_header(plan, by_key, day, window)
        lines += ["", f"🗓 {header}"]
        for task in plan["tasks"]:
            people = plan["slots"].get(day, {}).get(task["name"], [])
            used_silent |= any(person in silent for person in people)
            lines += task_lines(
                task["name"], task["needed"],
                [
                    names.get(person, person) + mark(person, day)
                    + ("" if shared_day else person_day(
                        by_key.get(person), day, plan
                    ))
                    for person in people
                ],
                bare=implicit_plan(plan),
            )
    # Нагрузка — только админам: сколько воскресений у каждого, по убыванию.
    load = served_dates(plan["slots"])
    ordered = sorted(
        polled, key=lambda person: (-len(load.get(person["key"], [])),
                                    person["name"]),
    )
    lines += ["", "👥 Нагрузка (воскресений в месяце):"] + [
        f"   {len(load.get(person['key'], []))} — {person['name']}"
        + ("" if person["telegram_id"] else " (без Telegram)")
        for person in ordered
    ]
    problems = gaps(plan)
    if problems:
        lines += [""] + problems
    # Задача без людей во все даты, хотя её выполняют люди состава, —
    # обычно её включили после составления черновика.
    able = {task for person in summary.people for task in person["tasks"]}
    idle = [
        task["name"] for task in plan["tasks"]
        if task["name"] in able and not any(
            plan["slots"].get(day, {}).get(task["name"]) for day in plan["dates"]
        )
    ]
    if idle and plan["approved"] is None and not implicit_plan(plan):
        lines.append(
            "ℹ️ Никого во все даты: " + ", ".join(f"«{name}»" for name in idle)
            + ". Если задачу включили после составления — «🔄 Пересчитать»."
        )
    if used_silent:
        lines.append(
            "* не ответил(а) — поставлен(а) по общей логике, после ответивших"
        )
    if used_unpicked:
        lines.append(
            "° не отмечал(а) эту дату в опросе — отмеченные заняли другие "
            "(поставлен(а) вместо не ответившего) или поставлен(а) вручную"
        )
    for answer in summary.outside:
        days = ", ".join(
            date.fromisoformat(day).strftime("%d") for day in answer["dates"]
        ) or "не сможет"
        lines.append(
            f"⚪ Вне состава отметились: {outside_name(answer)} ({days}) — "
            "нет задач"
        )
    if plan.get("note"):
        lines.append(f"ℹ️ Интервалы: {plan['note']}")
    return "\n".join(lines)


def slot_people(plan: dict, day: str, task: str) -> list[str]:
    return list(plan["slots"].get(day, {}).get(task, []))


def busy_on(plan: dict, day: str, task: str,
            bundles: dict | None = None) -> dict[str, str]:
    """Кто уже стоит в этот день на другой задаче: ключ → задача. Задача
    из его связки «в одну неделю» с этой — не занятость."""
    return {
        person: other
        for other, people in plan["slots"].get(day, {}).items()
        if other != task
        for person in people
        if not {task, other} <= (bundles or {}).get(person, frozenset())
    }


def candidates(
    plan: dict, summary: Summary, day: str, task: str,
) -> tuple[list[str], list[str], list[str]]:
    """Кого можно поставить: (свободны и приписаны к задаче, не отвечали,
    остальные из состава). Уже стоящие в этот день — не предлагаются."""
    taken = set(slot_people(plan, day, task)) | set(
        busy_on(plan, day, task, summary.bundles)
    )
    silent = {person["key"] for person in summary.not_answered}
    free, quiet, rest = [], [], []
    for person in summary.people:
        key = person["key"]
        if key in taken or not person["tasks"]:
            continue
        if key in silent:
            (quiet if task in person["tasks"] else rest).append(key)
        elif day in summary.answered.get(key, ()) and task in person["tasks"]:
            free.append(key)
        else:
            rest.append(key)
    return free, quiet, rest


def choice_notes(plan: dict, summary: Summary, key: str, day: str,
                 task: str) -> list[str]:
    """Предупреждения, когда админ ставит человека вручную."""
    person = next(
        (item for item in summary.people if item["key"] == key), None
    )
    notes = []
    silent = {item["key"] for item in summary.not_answered}
    if key in silent:
        notes.append("⚪ не отвечал(а) на опрос")
    elif day not in summary.answered.get(key, ()):
        notes.append(f"⚠️ не отмечал(а) {_short(day)}")
    if person is not None and task not in person["tasks"]:
        notes.append(f"⚠️ не приписан(а) к «{task}»")
    event_date = date.fromisoformat(day)
    near = [
        other for other in served_dates(plan["slots"]).get(key, [])
        if other != day
        and abs((date.fromisoformat(other) - event_date).days) <= 7
    ]
    previous = plan["last_served"].get(key)
    if previous and (event_date - date.fromisoformat(previous)).days <= 7:
        near.append(previous)
    for other in sorted(near):
        verb = "служил(а)" if other < day else "служит"
        notes.append(f"ℹ️ {verb} {_short(other)}")
    return notes


def plan_changes(old: dict, new: dict, names: dict[str, str],
                 nobody: str = "никого") -> list[str]:
    """Отличия нового графика от опубликованного — для «обновлён».
    Единственная задача сменила название (служение без задач переименовано
    в каталоге) — одной строкой, люди сравниваются под новым названием.
    При нескольких задачах смена набора — это выключение и включение, не
    переименование."""
    lines = []
    old_tasks = {task for tasks in old.values() for task in tasks}
    new_tasks = {task for tasks in new.values() for task in tasks}
    if len(old_tasks) == len(new_tasks) == 1 and old_tasks != new_tasks:
        (was,), (now,) = old_tasks, new_tasks
        lines.append(f"• Задача «{was}» теперь называется «{now}»")
        old = {
            day: {(now if task == was else task): people
                  for task, people in tasks.items()}
            for day, tasks in old.items()
        }
    for day in sorted(set(old) | set(new)):
        tasks = list(dict.fromkeys([*new.get(day, {}), *old.get(day, {})]))
        for task in tasks:
            before = old.get(day, {}).get(task, [])
            after = new.get(day, {}).get(task, [])
            if sorted(before) == sorted(after):
                continue

            def shown(people: list[str]) -> str:
                return ", ".join(names.get(p, p) for p in people) or nobody

            lines.append(
                f"• {_short(day)} «{task}»: {shown(before)} → {shown(after)}"
            )
    return lines


def publication_title(service: dict, month: str, update: bool,
                      settings: dict | None = None) -> str:
    """Заголовок графика в чате служения: при первой публикации или при
    обновлении (шаблоны «📝 Тексты»)."""
    year, month_number = map(int, month.split("-"))
    template = (settings or {}).get(
        "plan_update_template" if update else "plan_title_template"
    ) or (DEFAULT_PLAN_UPDATE_TITLE if update else DEFAULT_PLAN_TITLE)
    return render_template(
        template,
        service_name=service["name"],
        month=month_title(year, month_number),
    )


def publication_text(service: dict, plan: dict, people: list[dict],
                     window: tuple[int, int] = (0, 0),
                     settings: dict | None = None) -> str:
    """Утверждённый график для чата служения (с упоминаниями): дата,
    под ней задачи — каждая с новой строки."""
    by_key = {person["key"]: person for person in people}
    update = plan["approved"] is not None

    def mention(key: str, day: str, shared_day: bool = False) -> str:
        person = by_key.get(key)
        if person is None:
            return key
        return user_link(person["telegram_id"], person["name"]) + (
            "" if shared_day else person_day(person, day, plan)
        )

    lines = [
        publication_title(service, plan["month"], update, settings),
    ]
    weekly = bool(task_days(plan))
    for day in plan["dates"]:
        if weekly:
            # У задач свои дни (спевка — ср): неделя, под ней дни и задачи.
            title, groups = week_groups(plan, day, window)
            lines += ["", f"🗓 {title}"]
            for label, offset, tasks in groups:
                lines.append(f"  {label}")
                for task in tasks:
                    people_text = ", ".join(
                        (
                            user_link(by_key[key]["telegram_id"],
                                      by_key[key]["name"])
                            + week_person_day(
                                plan, by_key[key], day, task["name"], offset
                            )
                        ) if key in by_key else key
                        for key in plan["slots"].get(day, {}).get(
                            task["name"], []
                        )
                    ) or phrase(settings, "empty")
                    lines.append(
                        f"    {task_emoji(task['name'])} {task['name']}: "
                        f"{people_text}"
                    )
            continue
        label, shared_day = date_header(plan, by_key, day, window)
        lines += ["", f"🗓 {label}"]
        for task in plan["tasks"]:
            people_keys = plan["slots"].get(day, {}).get(task["name"], [])
            people_text = ", ".join(
                mention(key, day, shared_day) for key in people_keys
            ) or phrase(settings, "empty")
            lines.append(
                f"  {people_text}" if implicit_plan(plan)
                else f"  {task_emoji(task['name'])} {task['name']}: "
                f"{people_text}"
            )
    if update:
        changes = plan_changes(
            plan["approved"], plan["slots"], _names(people),
            phrase(settings, "nobody"),
        )
        names = _names(people)
        for day in plan["dates"]:
            before = plan.get("approved_days", {}).get(day, {})
            after = plan.get("days", {}).get(day, {})
            serving = {
                key for people in plan["slots"].get(day, {}).values()
                for key in people
            }
            for key in sorted(set(before) | set(after)):
                person, task = split_day_key(key)
                if before.get(key) == after.get(key) or person not in serving:
                    continue
                offset = after.get(key)
                changes.append(
                    f"• {_short(day)} {names.get(person, person)}"
                    + (f" ({task})" if task else "") + ": "
                    + (
                        _weekday_date(
                            date.fromisoformat(day) + timedelta(days=offset)
                        ) if offset is not None
                        else phrase(settings, "usual_day")
                    )
                )
        if plan.get("republish"):
            changes.append(phrase(settings, "days_changed"))
        if changes:
            lines += ["", phrase(settings, "changes")] + changes
    return "\n".join(lines)


def _columns(registry, service_id: str) -> list[ScheduleColumn]:
    service = next(
        (item for item in registry.service_catalog() if item.id == service_id),
        None,
    )
    if service is None or not service.columns:
        return [ScheduleColumn(
            headers=(service.name if service else service_id,), layout="pairs",
        )]
    return list(service.columns)


def task_moments(plan: dict, by_key: dict, day: str,
                 task: str) -> list[tuple[date, str]]:
    """Люди задачи на дату и фактический день каждого для Excel: у задачи
    со своим днём — день задачи или разовый день человека, иначе
    воскресенье графика."""
    sunday = date.fromisoformat(day)
    own = task in task_days(plan)
    return [
        (
            sunday + timedelta(
                # Не из состава (имя из Excel) — разовый день по ключу.
                days=slot_offset(
                    plan, by_key.get(key) or {"key": key}, day, task
                ) or 0
            ) if own else sunday,
            key,
        )
        for key in plan["slots"].get(day, {}).get(task, [])
    ]


def excel_plan(registry, service_id: str, year: int, month: int,
               events: list) -> dict:
    """График месяца из событий Excel (решение 15): воскресенья месяца,
    задачи графика, люди — ключами состава (по имени; неизвестные и
    неоднозначные — именем как в файле). Задача со своим днём (среда
    перед воскресеньем) — в его воскресенье; другой день человека —
    разовым днём. → dates, tasks, slots, days, unbound (не привязаны),
    skipped (даты не воскресенья месяца: «ср 11.11 спевка» — у задачи нет
    своего дня)."""
    people = registry.readiness_people(service_id)
    keys: dict[str, list[str]] = {}
    for person in people:
        keys.setdefault(normalized_name(person["name"]), []).append(person["key"])
    by_key = {person["key"]: person for person in people}
    tasks = plan_tasks(registry, service_id)
    names = {task["name"] for task in tasks}
    own_days = {
        task["name"]: task["day"] for task in tasks if task.get("day") is not None
    }
    dates = free_run(service_id, year, month)["dates"]
    slots = {day: {task["name"]: [] for task in tasks} for day in dates}
    days: dict = {}
    unbound: list[str] = []
    skipped: list[str] = []
    for block in week_events(
        [event for event in events if event.service_id == service_id],
        own_days,
    ):
        day = block["date"].isoformat()
        if day not in slots:
            if block["date"].weekday() != 6 and (
                block["date"].year, block["date"].month
            ) == (year, month):
                skipped += [
                    f"{_weekday_date(block['date'])} {item.task}"
                    for item in block["assignments"]
                    if not item.is_informational and item.name
                ]
            continue
        for assignment, moment in zip(block["assignments"], block["moments"]):
            if (
                assignment.is_informational or not assignment.name
                or assignment.task not in names
            ):
                continue
            found = keys.get(normalized_name(assignment.name), [])
            key = found[0] if len(found) == 1 else assignment.name
            people_slot = slots[day][assignment.task]
            if key not in people_slot:
                people_slot.append(key)
            if not (by_key.get(key) or {}).get("telegram_id"):
                if assignment.name not in unbound:
                    unbound.append(assignment.name)
            if assignment.task in own_days:
                # Задача со своим днём не в свой день (в воскресенье) —
                # разовый день человека в этой задаче.
                offset = (moment - block["date"]).days
                if offset != own_days[assignment.task]:
                    days.setdefault(day, {})[
                        task_day_key(key, assignment.task)
                    ] = offset
    return {
        "dates": dates, "tasks": tasks, "slots": slots, "days": days,
        "unbound": unbound, "skipped": list(dict.fromkeys(skipped)),
    }


def plan_events(plan: dict, people: list[dict]) -> list[ServiceEvent]:
    """Утверждённый график бота как события графика (решение 15): по ним
    — напоминания и замены. Задача со своим днём — событием своей даты
    (как в Excel), разовый день человека учитывается."""
    if plan.get("approved") is None:
        return []
    # Задачи и дни — как утверждено (рабочая копия может быть уже
    # переименована или с другим днём задачи). Задача утверждённых
    # назначений не из снимка (снимок миграции из уже переименованной
    # копии) — без своего дня, но не теряется.
    tasks = list(plan.get("approved_tasks") or [])
    known = {task["name"] for task in tasks}
    tasks += [
        {"name": name}
        for name in dict.fromkeys(
            task for day_tasks in plan["approved"].values()
            for task in day_tasks
        )
        if name not in known
    ]
    published = {
        **plan, "slots": plan["approved"], "tasks": tasks,
        "days": plan.get("approved_days") or {},
    }
    names = _names(people)
    by_key = {person["key"]: person for person in people}
    events: dict[date, list[ServiceAssignment]] = {}
    for day in sorted(plan["approved"]):
        events.setdefault(date.fromisoformat(day), [])
        for task in tasks:
            for moment, key in task_moments(published, by_key, day, task["name"]):
                events.setdefault(moment, []).append(
                    ServiceAssignment(task["name"], names.get(key, key))
                )
    return [
        ServiceEvent(plan["service_id"], moment, tuple(items))
        for moment, items in sorted(events.items())
    ]


def excel_rows(registry, plan: dict, people: list[dict]) -> list[list]:
    """Лист как в графике: строка заголовков колонок служения, затем блок
    каждой даты — дата в первой ячейке колонки, ниже назначения. Задача со
    своим днём (спевка) — блоком своей даты (среды), как в файле. У колонки
    со своей задачей («Слайды») — только её люди, остальные задачи — в
    основной колонке; у «задача | имя» следующий человек задачи — без
    повтора задачи."""
    names = _names(people)
    columns = _columns(registry, plan["service_id"])
    own = {
        normalized_name(column.task): index
        for index, column in enumerate(columns) if column.task
    }
    main = next(
        (index for index, column in enumerate(columns) if not column.task), 0
    )
    starts, width = [], 0
    for column in columns:
        starts.append(width)
        width += 2 if column.layout == "pairs" else 1
    header = [""] * width
    for column, start in zip(columns, starts):
        header[start] = column.headers[0]
    rows: list[list] = [header]
    # Задача со своим днём (спевка — ср) — отдельным блоком своей даты, как
    # в файле; блоки по порядку дат. Дата другого месяца (среда перед
    # 01.11 — 28.10) — не на этом листе: она в тексте «Для Excel».
    by_key = {person["key"]: person for person in people}
    year, month = map(int, plan["month"].split("-"))
    blocks: dict[date, list[list[list]]] = {}
    for day in plan["dates"]:
        for task in plan["tasks"]:
            index = own.get(normalized_name(task["name"]), main)
            pairs = columns[index].layout == "pairs"
            # Задача на дату может разойтись по дням (разовый день
            # человека): название задачи — у первого в каждом блоке.
            labelled: set[date] = set()
            for moment, key in task_moments(plan, by_key, day, task["name"]):
                if (moment.year, moment.month) != (year, month):
                    continue
                cells = blocks.setdefault(moment, [[] for _ in columns])
                label = "" if moment in labelled else task["name"]
                labelled.add(moment)
                cells[index].append(
                    [label, names.get(key, key)] if pairs else [names.get(key, key)]
                )
        # Дата графика — блоком, даже если в неё никого.
        blocks.setdefault(date.fromisoformat(day), [[] for _ in columns])
    for moment in sorted(blocks):
        cells = blocks[moment]
        block = [[""] * width for _ in range(1 + max(map(len, cells)))]
        for index, start in enumerate(starts):
            block[0][start] = moment
            for offset, values in enumerate(cells[index], start=1):
                block[offset][start:start + len(values)] = values
        rows += block
    return rows


def has_sheet_columns(registry, service_id: str) -> bool:
    """У служения есть колонки на листе графика (иначе .xlsx не нужен)."""
    return any(
        item.id == service_id and item.columns
        for item in registry.service_catalog()
    )


def excel_text(registry, service: dict, plan: dict, people: list[dict]) -> str:
    year, month = map(int, plan["month"].split("-"))
    names = _names(people)
    lines = [
        f"📋 Для Excel · {service['name']} · лист "
        f"«{_schedule_sheet_name(date(year, month, 1))}»",
        (
            "Файл ниже — те же назначения по колонкам служения, как на "
            "листе графика."
            if has_sheet_columns(registry, service["id"])
            else "У служения нет колонки на листе графика — только текст."
        ),
        "",
    ]
    own_days = task_days(plan)
    by_key = {person["key"]: person for person in people}
    outside = []
    for day in plan["dates"]:
        # Задача со своим днём — в строке своего воскресенья с фактическим
        # днём (на листе — блоком своей даты); разовый день человека — в
        # скобках у имени.
        parts = []
        for task in plan["tasks"]:
            moments = task_moments(plan, by_key, day, task["name"])
            usual = date.fromisoformat(day) + timedelta(
                days=own_days.get(task["name"], 0)
            )
            head = "" if implicit_plan(plan) else (
                f"{task['name']} ({_weekday_date(usual)}) — "
                if task["name"] in own_days else f"{task['name']} — "
            )
            parts.append(head + (", ".join(
                names.get(key, key) + (
                    f" ({_weekday_date(moment)})" if moment != usual else ""
                )
                for moment, key in moments
            ) or "пусто"))
            # Своя дата задачи в другом месяце — на листе того месяца.
            outside += [
                (moment, task["name"], names.get(key, key))
                for moment, key in moments
                if (moment.year, moment.month) != (year, month)
            ]
        lines.append(f"{_short(day)}: " + "; ".join(parts))
    for moment, task, name in outside:
        lines.append(
            f"⚠️ {_weekday_date(moment)} {task} — {name}: это лист "
            f"«{_schedule_sheet_name(moment)}», на листе этого месяца её нет."
        )
    return "\n".join(lines)


def excel_file(registry, service: dict, plan: dict, people: list[dict]) -> bytes:
    from openpyxl import Workbook

    year, month = map(int, plan["month"].split("-"))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _schedule_sheet_name(date(year, month, 1))
    for row in excel_rows(registry, plan, people):
        sheet.append(row)
    for cells in sheet.iter_rows():
        for cell in cells:
            if isinstance(cell.value, date):
                cell.number_format = "DD.MM.YYYY"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def publish(
    bot, registry, service: dict, plan: dict, actor_id: int,
    logger: logging.Logger, text_limit: int,
) -> dict:
    """«✅ Утвердить»: график в чат служения, затем отметка в базе.
    ValueError — публиковать некуда."""
    chat_id = service.get("group_chat_id")
    if not chat_id:
        raise ValueError("Чат служения не привязан — график не опубликован.")
    if not service.get("enabled", True):
        raise ValueError("Служение выключено — график не опубликован.")
    people = registry.readiness_people(service["id"])
    sent: list[int] = []

    async def send_chunk(chat, text, **kwargs):
        message = await send_to_service_group(
            bot.send_message, chat, text, service,
            registry=registry, logger=logger, **kwargs,
        )
        sent.append(message.message_id)
        return message

    try:
        await send_telegram_screen(
            send_chunk, chat_id,
            publication_text(
                service, plan, people, window_of(registry, service["id"]),
                registry.get_readiness_settings(service["id"]),
            ),
            limit=text_limit,
        )
    except BaseException:
        # Длинный график ушёл не целиком — убрать отправленные части, иначе
        # повторное «Утвердить» продублировало бы их.
        for message_id in sent:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:  # noqa: BLE001
                logger.info("Часть графика не удалена", exc_info=True)
        raise
    for attempt in range(2):
        try:
            return registry.approve_schedule_plan(
                actor_id, service["id"], plan["month"], plan["slots"],
                chat_id=chat_id, message_id=sent[0] if sent else None,
                updated_at=plan["updated_at"], days=plan.get("days", {}),
                served_offsets=served_offsets(plan, people),
            )
        except Exception as exc:  # noqa: BLE001
            if attempt:
                # Повторное «Утвердить» опубликовало бы график второй раз.
                raise PublishedNotSaved(str(exc)) from exc
            logger.warning("Утверждение графика не сохранено, повтор",
                           exc_info=True)


def excel_filled(events, service_id: str, year: int, month: int) -> bool:
    """В Excel на этот месяц у служения уже есть назначения."""
    first, last = date(year, month, 1), month_end(year, month)
    return any(
        event.service_id == service_id
        and first <= event.event_date <= last
        and any(
            item.name and not item.is_informational
            for item in event.assignments
        )
        for event in events
    )

