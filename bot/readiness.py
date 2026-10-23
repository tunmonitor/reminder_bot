"""Опрос готовности (этап 5.2–5.3).

Один Telegram-опрос на служение и месяц: варианты — воскресенья
следующего месяца и «❌ Не смогу». Человек отвечает «когда свободен»,
на задачу его ставит распределение (`schedule_planner`) по его задачам
(«Списки» ∪ привязки в боте).

Расписание по умолчанию: понедельник последней полной недели месяца
(последнее воскресенье − 6 дней) в start_time, срок — через
duration_days в deadline_time; за remind_days до срока — личное
напоминание не ответившим; накануне запуска — превью админам.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .group_send import send_to_service_group
from .month_summary import month_end, month_key, month_title, next_month
from .schedule_planner import Plan, PlanInput, build_plan
from .service_registry import (  # noqa: F401 — шаблоны для экранов
    DEFAULT_READINESS_INTRO,
    DEFAULT_READINESS_REMINDER,
)
from .ui import send_telegram_screen, user_link

CANT_OPTION = "❌ Не смогу в этом месяце"
POLL_QUESTION = "Когда можешь служить в {month}?"
MONTHS_IN = (
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
)


def month_sundays(year: int, month: int) -> list[date]:
    day = date(year, month, 1)
    day += timedelta(days=(6 - day.weekday()) % 7)
    result = []
    while day.month == month:
        result.append(day)
        day += timedelta(weeks=1)
    return result


def target_month(today: date) -> tuple[int, int]:
    """Опрос всегда о следующем месяце."""
    return next_month(today.year, today.month)


def launch_date(year: int, month: int) -> date:
    """Понедельник последней полной недели месяца перед (year, month)."""
    previous = date(year, month, 1) - timedelta(days=1)
    last_sunday = month_sundays(previous.year, previous.month)[-1]
    return last_sunday - timedelta(days=6)


def _at(day: date, clock: str, tz) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=tz)


def schedule(settings: dict, year: int, month: int, tz) -> tuple[datetime, datetime]:
    """(запуск, срок) автоматического опроса о месяце (year, month)."""
    start = launch_date(year, month)
    return (
        _at(start, settings["start_time"], tz),
        _at(
            start + timedelta(days=settings["duration_days"]),
            settings["deadline_time"], tz,
        ),
    )


def manual_deadline(now: datetime, settings: dict) -> datetime:
    """Срок опроса, запущенного вручную: через duration_days, но не позже
    конца текущего месяца — график нужен до начала месяца опроса. Меньше
    часа на ответы — запуск невозможен (ValueError)."""
    last = month_end(now.year, now.month)
    day = min(now.date() + timedelta(days=settings["duration_days"]), last)
    deadline = max(
        _at(day, settings["deadline_time"], now.tzinfo),
        now + timedelta(hours=1),
    )
    limit = datetime.combine(last, time.max, tzinfo=now.tzinfo)
    if deadline > limit:
        raise ValueError(
            "До начала месяца меньше часа — опрос не успеет пройти."
        )
    return deadline


def local_text(moment: datetime | str, tz) -> str:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return moment.astimezone(tz).strftime("%d.%m %H:%M")


def answer_dates(option_ids, dates: list[str]) -> list[str] | None:
    """Варианты опроса → свободные даты; None — голос отозван; [] —
    только «Не смогу». Даты вместе с «Не смогу» — засчитываются даты."""
    chosen = list(option_ids or ())
    if not chosen:
        return None
    return sorted(dates[index] for index in chosen if index < len(dates))


@dataclass
class Summary:
    """Состояние опроса: кто ответил, кто свободен, предварительный план."""

    people: list[dict]
    # Ключ человека (`key` из readiness_people) → свободные даты.
    answered: dict[str, list[str]] = field(default_factory=dict)
    outside: list[dict] = field(default_factory=list)
    not_answered: list[dict] = field(default_factory=list)
    plan: Plan | None = None
    free_count: dict[str, int] = field(default_factory=dict)


def summarize(registry, run: dict, *, with_plan: bool = True) -> Summary:
    people = registry.readiness_people(run["service_id"])
    by_id = {
        person["telegram_id"]: person
        for person in people if person["telegram_id"]
    }
    summary = Summary(people=people)
    for answer in registry.list_readiness_answers(run["id"]):
        person = by_id.get(answer["telegram_id"])
        if person is None or not person["tasks"]:
            # Без задач человека некуда поставить — админ распределяет
            # его по задачам сам, как ответившего вне состава.
            if person is not None:
                answer = {**answer, "full_name": person["name"]}
            summary.outside.append(answer)
            continue
        summary.answered[person["key"]] = answer["dates"]
    summary.not_answered = [
        person for person in people
        if person["tasks"] and person["key"] not in summary.answered
    ]
    summary.free_count = {
        day: sum(day in dates for dates in summary.answered.values())
        for day in run["dates"]
    }
    if with_plan:
        tasks = {person["key"]: person["tasks"] for person in people}
        needed = {
            task["name"]: task["people_needed"]
            for task in registry.list_service_tasks(run["service_id"])
            if task["enabled"]
        }
        summary.plan = build_plan(PlanInput(
            dates=tuple(date.fromisoformat(day) for day in run["dates"]),
            needed=needed,
            free={
                name: frozenset(date.fromisoformat(day) for day in dates)
                for name, dates in summary.answered.items()
            },
            tasks={name: frozenset(tasks[name]) for name in summary.answered},
        ))
    return summary


def _short(day: str) -> str:
    return date.fromisoformat(day).strftime("%d")


def outside_name(answer: dict) -> str:
    return answer["full_name"] or (
        f"@{answer['username']}" if answer["username"] else "Участник чата"
    )


def progress_text(service_label: str, run: dict, summary: Summary, tz) -> str:
    year, month = map(int, run["month"].split("-"))
    status = {
        "open": f"до {local_text(run['deadline'], tz)}",
        "closed": "завершён",
    }.get(run["status"], run["status"])
    polled = [person for person in summary.people if person["tasks"]]
    lines = [
        f"📊 Ход опроса · {service_label} · {month_title(year, month)}",
        f"⏳ {status}",
        "",
        f"Ответили {len(summary.answered)} из {len(polled)}"
        + (f" · вне состава: {len(summary.outside)}" if summary.outside else ""),
        "",
        "Свободны:  " + " · ".join(
            f"{_short(day)} — {summary.free_count.get(day, 0)}"
            for day in run["dates"]
        ),
    ]
    if summary.plan is not None:
        gaps = summary.plan.gaps()
        lines += ["", "Предварительно:"]
        if gaps:
            for event_date, task, missing in gaps:
                needed = summary.plan.needed[task]
                lines.append(
                    f"⚠️ {event_date:%d.%m} «{task}» — "
                    + ("некого поставить" if missing == needed
                       else f"{needed - missing} из {needed}")
                )
        else:
            lines.append("✅ все задачи закрыты")
    for answer in summary.outside:
        days = ", ".join(_short(day) for day in answer["dates"]) or "не сможет"
        lines.append(
            f"⚪ Вне состава: {outside_name(answer)} ({days}) — нет задач, "
            "не распределяется"
        )
    without_telegram = [
        person["name"] for person in summary.not_answered
        if not person["telegram_id"]
    ]
    waiting = [
        person["name"] for person in summary.not_answered
        if person["telegram_id"]
    ]
    if waiting or without_telegram:
        lines += ["", "⏳ Не ответили: " + ", ".join(
            waiting + [f"{name} (без Telegram)" for name in without_telegram]
        )]
    return "\n".join(lines)


class _Values(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, **values: str) -> str:
    """Подстановка полей; строка «👥 » без людей не остаётся пустой."""
    text = template.format_map(_Values(values))
    return "\n".join(
        line for line in text.splitlines() if line.strip() != "👥"
    ).strip()


def intro_text(service: dict, people: list[dict], year: int, month: int,
               deadline: datetime, tz, template: str | None = None) -> str:
    mentions = [
        user_link(person["telegram_id"], person["name"])
        if person["telegram_id"] else person["name"]
        for person in people if person["tasks"]
    ]
    return render_template(
        template or DEFAULT_READINESS_INTRO,
        service_name=service["name"],
        month=month_title(year, month),
        deadline=local_text(deadline, tz),
        people=", ".join(mentions),
    )


def reminder_text(service: dict, run: dict, tz,
                  template: str | None = None) -> str:
    year, month = month_of(run)
    return render_template(
        template or DEFAULT_READINESS_REMINDER,
        service_name=service["name"],
        month=month_title(year, month),
        deadline=local_text(run["deadline"], tz),
    )


async def launch_run(
    bot,
    registry,
    service: dict,
    year: int,
    month: int,
    deadline: datetime,
    tz,
    logger: logging.Logger,
    text_limit: int,
) -> dict:
    """Опрос месяца в чате служения. ValueError — запуск невозможен."""
    chat_id = service.get("group_chat_id")
    if not chat_id:
        raise ValueError("Чат служения не привязан — опрос не запущен.")
    if not service.get("enabled", True):
        raise ValueError("Служение выключено — опрос не запущен.")
    key = month_key(year, month)
    if registry.get_readiness_run(service["id"], key) is not None:
        raise ValueError("Опрос на этот месяц уже есть.")
    dates = [day.isoformat() for day in month_sundays(year, month)]
    people = registry.readiness_people(service["id"])
    settings = registry.get_readiness_settings(service["id"])
    # Вступление может уйти несколькими сообщениями (длинный шаблон,
    # много упоминаний) — запоминаются все, чтобы убрать при сбое опроса.
    intro_ids: list[int] = []

    async def send_chunk(chat, text, **kwargs):
        message = await bot.send_message(chat, text, **kwargs)
        intro_ids.append(message.message_id)
        return message

    intro = await send_to_service_group(
        lambda chat, text, **kwargs: send_telegram_screen(
            send_chunk, chat, text, limit=text_limit, **kwargs
        ),
        chat_id,
        intro_text(
            service, people, year, month, deadline, tz,
            settings.get("intro_template"),
        ),
        service, registry=registry, logger=logger,
    )
    try:
        poll_message = await send_to_service_group(
            bot.send_poll, chat_id,
            POLL_QUESTION.format(month=MONTHS_IN[month - 1]),
            service, registry=registry, logger=logger,
            options=[
                *(date.fromisoformat(day).strftime("%d.%m") for day in dates),
                CANT_OPTION,
            ],
            is_anonymous=False,
            allows_multiple_answers=True,
        )
    except Exception:
        # Без опроса вступление в чате лишнее: повторный запуск прислал бы
        # второе.
        for message_id in intro_ids:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:  # noqa: BLE001
                logger.info("Вступление опроса не удалено", exc_info=True)
        raise
    try:
        return registry.create_readiness_run(
            service["id"], key,
            status="open",
            dates=dates,
            poll_id=poll_message.poll.id,
            chat_id=chat_id,
            message_id=poll_message.message_id,
            intro_message_id=intro_ids[0] if intro_ids else getattr(
                intro, "message_id", None
            ),
            deadline=deadline.astimezone(timezone.utc).isoformat(),
        )
    except Exception:
        # Опрос без записи нельзя подсчитать — останавливаем его.
        logger.exception("Не удалось сохранить опрос готовности")
        try:
            await bot.stop_poll(chat_id, poll_message.message_id)
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось остановить опрос готовности", exc_info=True)
        raise


_POLL_GONE = (
    "poll has already been closed",
    "message to stop not found",
    "message not found",
    "message_id_invalid",
)


async def stop_run_poll(bot, run: dict, logger: logging.Logger) -> bool:
    """True — опрос остановлен (или уже закрыт / удалён в чате)."""
    if not run.get("message_id"):
        return True
    try:
        await bot.stop_poll(run["chat_id"], run["message_id"])
    except Exception as exc:  # noqa: BLE001
        if any(mark in str(exc).lower() for mark in _POLL_GONE):
            return True
        logger.info("Опрос готовности %s не остановлен", run["id"], exc_info=True)
        return False
    return True


def poll_link(run: dict) -> str | None:
    """Ссылка на опрос в супергруппе (t.me/c/…); для обычных групп — нет."""
    chat = str(run.get("chat_id") or "")
    if not chat.startswith("-100") or not run.get("message_id"):
        return None
    return f"https://t.me/c/{chat[4:]}/{run['message_id']}"


async def send_reminders(
    bot, registry, run: dict, service: dict, tz, logger: logging.Logger
) -> int:
    """Лично не ответившим из состава; кто не писал боту — пропускается."""
    summary = summarize(registry, run, with_plan=False)
    link = poll_link(run)
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📡 Открыть опрос", url=link)
    ]]) if link else None
    text = reminder_text(
        service, run, tz,
        registry.get_readiness_settings(service["id"]).get("reminder_template"),
    )
    sent = 0
    reminded = registry.reminded_readiness_ids(run["id"])
    for person in summary.not_answered:
        telegram_id = person["telegram_id"]
        if not telegram_id or telegram_id in reminded:
            continue
        try:
            await bot.send_message(
                telegram_id, text, parse_mode=None, reply_markup=markup,
            )
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            # Не писал боту или заблокировал — повтор не поможет.
            logger.info("Напоминание об опросе не доставлено %s", telegram_id)
        except Exception:  # noqa: BLE001
            # Временный сбой — следующая проверка повторит.
            logger.warning(
                "Напоминание об опросе не отправлено %s", telegram_id,
                exc_info=True,
            )
            continue
        registry.mark_readiness_reminded(run["id"], telegram_id)
    return sent


def deadline_of(run: dict) -> datetime:
    return datetime.fromisoformat(run["deadline"])


def reminder_due(run: dict, settings: dict, now: datetime) -> bool:
    """Пора напомнить; опросу, запущенному уже внутри окна напоминания
    (поздний или ручной запуск), напоминание не шлётся."""
    days = settings["remind_days"]
    if days <= 0:
        return False
    remind_at = deadline_of(run) - timedelta(days=days)
    return datetime.fromisoformat(run["created_at"]) < remind_at <= now


def month_of(run: dict) -> tuple[int, int]:
    year, month = map(int, run["month"].split("-"))
    return year, month


def latest_deadline_day(run: dict) -> date:
    """Последний день для срока: график нужен до начала месяца опроса."""
    return date(*month_of(run), 1) - timedelta(days=1)



# Запуск и закрытие одного служения — по очереди: ежеминутная проверка
# и кнопки админа не должны отправить два опроса или два итога.
_service_locks: dict[str, asyncio.Lock] = {}


def service_lock(service_id: str) -> asyncio.Lock:
    return _service_locks.setdefault(service_id, asyncio.Lock())


def admin_keyboard(service_id: str, *rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        *rows,
        [InlineKeyboardButton(
            text="🗳 Опрос готовности", callback_data=f"rdy:open:{service_id}"
        )],
    ])


async def notify_admins(
    bot, registry, service_id: str, admin_id: int, text: str,
    logger: logging.Logger, text_limit: int, reply_markup=None,
) -> bool:
    """True — уведомление дошло хотя бы до одного админа."""
    from .admin_report import report_recipients

    delivered = False
    for recipient in report_recipients(registry, service_id, admin_id):
        try:
            await send_telegram_screen(
                bot.send_message, recipient, text, limit=text_limit,
                reply_markup=reply_markup,
            )
            delivered = True
        except Exception:  # noqa: BLE001
            logger.info(
                "Уведомление об опросе готовности не доставлено %s", recipient,
                exc_info=True,
            )
    return delivered


async def close_run(
    bot, registry, run: dict, service: dict, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> bool:
    """Закрывает опрос и присылает админам итог. False — уже закрыт."""
    if not registry.close_readiness_run(run["id"]):
        return False
    await finish_run(
        bot, registry, registry.get_readiness_run_by_id(run["id"]), service,
        tz, admin_id, logger, text_limit,
    )
    return True


async def finish_run(
    bot, registry, run: dict, service: dict, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> None:
    """Шаги закрытия — остановка опроса в чате и итог админам; что не
    удалось, повторяет следующая проверка (без повтора удавшегося)."""
    if not run["poll_stopped"] and await stop_run_poll(bot, run, logger):
        registry.mark_readiness_run(run["id"], poll_stopped=True)
    if run["result_sent"]:
        return
    try:
        summary = await asyncio.to_thread(summarize, registry, run)
        text = progress_text(service["name"], run, summary, tz)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось подвести итог опроса готовности")
        text = (
            f"⚠️ {service['name']}: итог посчитать не удалось — откройте "
            "«📊 Итог опроса»."
        )
    if await notify_admins(
        bot, registry, service["id"], admin_id,
        "🗳 Опрос завершён\n\n" + text, logger, text_limit,
        reply_markup=admin_keyboard(service["id"]),
    ):
        registry.mark_readiness_run(run["id"], result_sent=True)


def preview_text(service: dict, people: list[dict], year: int, month: int,
                 start: datetime, tz) -> str:
    polled = [person for person in people if person["tasks"]]
    without = [person["name"] for person in polled if not person["telegram_id"]]
    dates = ", ".join(day.strftime("%d") for day in month_sundays(year, month))
    lines = [
        f"🗳 {local_text(start, tz)} — опрос готовности {service['name']} "
        f"на {month_title(year, month)}",
        f"Даты: {dates}.{month:02d}",
        f"Опрашиваются {len(polled)} чел."
        + (f", без Telegram: {', '.join(without)} — соберите сами"
           if without else ""),
    ]
    if not service.get("group_chat_id"):
        lines.append("⚠️ Чат служения не привязан — опрос не запустится.")
    return "\n".join(lines)


def _missed(registry, service_id: str, key: str, settings: dict,
            start: datetime) -> bool:
    """Автозапуск должен был быть, но опроса нет и попытки не было:
    бот не работал всё окно. Включённый после времени запуска автозапуск
    — не пропуск."""
    enabled_at = settings.get("enabled_at")
    return (
        enabled_at is not None
        and datetime.fromisoformat(enabled_at) <= start
        and not registry.has_readiness_notice(service_id, key, "launch")
    )


async def _tick_service(
    bot, registry, service: dict, now: datetime, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> None:
    settings = registry.get_readiness_settings(service["id"])
    if not settings["enabled"] or not service.get("enabled", True):
        return

    async def tell(text: str) -> None:
        await notify_admins(
            bot, registry, service["id"], admin_id, text, logger, text_limit,
            reply_markup=admin_keyboard(service["id"]),
        )

    async def tell_missed(year: int, month: int, key: str) -> None:
        if registry.claim_readiness_notice(service["id"], key, "missed"):
            await tell(
                f"⚠️ Опрос готовности {service['name']} на "
                f"{month_title(year, month)} не был запущен вовремя — "
                "бот не работал. Запустите вручную или пропустите месяц."
            )

    async with service_lock(service["id"]):
        # Опрос о текущем месяце: если бот пролежал до его начала, его
        # уже не запустить, но админы узнают об этом.
        current = month_key(now.year, now.month)
        start, _ = schedule(settings, now.year, now.month, tz)
        if registry.get_readiness_run(service["id"], current) is None and (
            _missed(registry, service["id"], current, settings, start)
        ):
            await tell_missed(now.year, now.month, current)

        year, month = target_month(now.date())
        key = month_key(year, month)
        start, deadline = schedule(settings, year, month, tz)
        if registry.get_readiness_run(service["id"], key) is not None:
            return
        if start - timedelta(days=1) <= now < start:
            if registry.claim_readiness_notice(service["id"], key, "preview"):
                people = registry.readiness_people(service["id"])
                await tell(preview_text(service, people, year, month, start, tz))
        elif start <= now < deadline:
            # Автозапуск — одна попытка: после сбоя (нет прав на опрос,
            # чат недоступен) бот не повторяет вступление каждую минуту,
            # админ запускает вручную.
            if not registry.claim_readiness_notice(service["id"], key, "launch"):
                return
            try:
                await launch_run(
                    bot, registry, service, year, month, deadline, tz,
                    logger, text_limit,
                )
            except ValueError as exc:
                await tell(
                    f"⚠️ Опрос готовности {service['name']} на "
                    f"{month_title(year, month)}: {exc}"
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Не удалось запустить опрос готовности %s", service["id"]
                )
                await tell(
                    f"❌ Опрос готовности {service['name']} на "
                    f"{month_title(year, month)} отправить не удалось — "
                    "проверьте права бота в чате и запустите вручную."
                )
        elif deadline <= now and _missed(
            registry, service["id"], key, settings, start
        ):
            await tell_missed(year, month, key)


async def _tick_run(
    bot, registry, run: dict, now: datetime, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> None:
    service = registry.get_service(run["service_id"])
    if service is None:
        return
    settings = registry.get_readiness_settings(service["id"])
    async with service_lock(service["id"]):
        current = registry.get_readiness_run_by_id(run["id"])
        if current is None or current["status"] != "open":
            return
        if now >= deadline_of(current):
            await close_run(
                bot, registry, current, service, tz, admin_id, logger,
                text_limit,
            )
        elif reminder_due(current, settings, now):
            # Отметка — на человека после доставки: идемпотентно.
            await send_reminders(bot, registry, current, service, tz, logger)


async def run_readiness_tick(
    bot, registry, now: datetime, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> None:
    """Раз в минуту: превью накануне, автозапуск, напоминание, срок.
    Сбой одного служения не мешает остальным."""
    for service in registry.list_services():
        try:
            await _tick_service(
                bot, registry, service, now, tz, admin_id, logger, text_limit
            )
        except Exception:  # noqa: BLE001
            logger.exception("Сбой опроса готовности %s", service["id"])
    for run in registry.list_open_readiness_runs():
        try:
            await _tick_run(
                bot, registry, run, now, tz, admin_id, logger, text_limit
            )
        except Exception:  # noqa: BLE001
            logger.exception("Сбой опроса готовности %s", run["id"])
    for run in registry.list_unfinished_readiness_runs():
        service = registry.get_service(run["service_id"])
        closed_at = datetime.fromisoformat(run["closed_at"] or run["created_at"])
        if service is None or now - closed_at > timedelta(days=1):
            # Сутки без успеха (админы недоступны, опрос не остановить) —
            # больше не пытаться каждую минуту; итог есть в «📊 Итог опроса».
            registry.mark_readiness_run(
                run["id"], poll_stopped=True, result_sent=True
            )
            continue
        try:
            async with service_lock(service["id"]):
                # Перечитать под блокировкой: «⏹ Завершить» могло только что
                # закрыть опрос и отправить итог — без повтора.
                current = registry.get_readiness_run_by_id(run["id"])
                if current is None or (
                    current["poll_stopped"] and current["result_sent"]
                ):
                    continue
                await finish_run(
                    bot, registry, current, service, tz, admin_id, logger,
                    text_limit,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Сбой закрытия опроса готовности %s", run["id"])
