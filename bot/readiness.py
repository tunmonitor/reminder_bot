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
import time as time_module
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
)

from .admin_report import fit_one_screen
from .group_send import send_to_service_group
from .month_summary import month_end, month_key, month_title, next_month
from .presentation import task_emoji
from .schedule_planner import Plan, PlanInput, build_plan
from .service_registry import (  # noqa: F401 — шаблоны для экранов
    DEFAULT_READINESS_CLOSED,
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
        catalog = [
            task for task in registry.list_service_tasks(run["service_id"])
            if task["enabled"]
        ]
        all_dates = frozenset(date.fromisoformat(day) for day in run["dates"])
        free = {
            name: frozenset(date.fromisoformat(day) for day in dates)
            for name, dates in summary.answered.items()
        }
        # Не ответившие (и люди без Telegram) — по общей логике: свободны
        # во все даты, но ставятся после ответивших.
        silent = {person["key"] for person in summary.not_answered}
        free.update({name: all_dates for name in silent})
        summary.plan = build_plan(PlanInput(
            dates=tuple(sorted(all_dates)),
            needed={task["name"]: task["people_needed"] for task in catalog},
            extra={
                task["name"]: task["people_max"] - task["people_needed"]
                for task in catalog
            },
            free=free,
            tasks={name: frozenset(tasks[name]) for name in free},
            reluctant=frozenset(silent),
        ))
    return summary


def _short(day: str) -> str:
    return date.fromisoformat(day).strftime("%d")


def outside_name(answer: dict) -> str:
    return answer["full_name"] or (
        f"@{answer['username']}" if answer["username"] else "Участник чата"
    )


def progress_text(service_label: str, run: dict, summary: Summary, tz) -> str:
    year, month = month_of(run)
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
    names = {person["key"]: person["name"] for person in summary.people}
    if summary.answered:
        lines += ["", "✅ Ответили: " + "; ".join(
            f"{names.get(key, key)} ("
            + (", ".join(_short(day) for day in dates) or "не сможет")
            + ")"
            for key, dates in summary.answered.items()
        )]
    if summary.plan is not None:
        silent = {person["key"] for person in summary.not_answered}
        lines += ["", "Предварительно:"]
        used_silent = False
        for day in run["dates"]:
            event_date = date.fromisoformat(day)
            lines.append(f"🗓 {event_date:%d.%m}")
            for task, needed in summary.plan.needed.items():
                people = summary.plan.slots.get((event_date, task), [])
                used_silent |= any(person in silent for person in people)
                shown = ", ".join(
                    names.get(person, person) + ("*" if person in silent else "")
                    for person in people
                )
                if not people:
                    shown = "⚠️ некого поставить"
                elif len(people) < needed:
                    shown += f" · ⚠️ {len(people)} из {needed}"
                lines.append(f"   {task_emoji(task)} {task} — {shown}")
        if used_silent:
            lines.append(
                "* не ответил(а) — поставлен(а) по общей логике, после "
                "ответивших"
            )
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


def closed_text(service: dict, run: dict, template: str | None = None) -> str:
    year, month = month_of(run)
    return render_template(
        template or DEFAULT_READINESS_CLOSED,
        service_name=service["name"],
        month=month_title(year, month),
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
    # Запись — до отправки: месяц занят (перезапуск, прерванный между
    # отправкой и записью, не вернёт старый опрос поверх нового).
    run = registry.create_readiness_run(
        service["id"], key,
        status="open",
        dates=dates,
        deadline=deadline.astimezone(timezone.utc).isoformat(),
    )
    # Вступление может уйти несколькими сообщениями (длинный шаблон,
    # много упоминаний) — запоминаются все, чтобы убрать при сбое опроса.
    # Переход в общую тему (тема удалена или закрыта) — на каждую часть
    # отдельно, иначе отправленные части повторились бы.
    intro_ids: list[int] = []

    async def send_chunk(chat, text, **kwargs):
        message = await send_to_service_group(
            bot.send_message, chat, text, service,
            registry=registry, logger=logger, **kwargs,
        )
        intro_ids.append(message.message_id)
        return message

    poll_message = None
    sending_poll = False
    try:
        intro = await send_telegram_screen(
            send_chunk, chat_id,
            intro_text(
                service, people, year, month, deadline, tz,
                settings.get("intro_template"),
            ),
            limit=text_limit,
        )
        sending_poll = True
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
        attached = registry.attach_readiness_poll(
            run["id"],
            poll_id=poll_message.poll.id,
            chat_id=chat_id,
            message_id=poll_message.message_id,
            intro_message_id=intro_ids[0] if intro_ids else getattr(
                intro, "message_id", None
            ),
        )
        await replay_early_answers(registry, poll_message.poll.id, logger)
        return attached
    except BaseException as exc:
        # BaseException — и при отмене задачи запись не остаётся без опроса.
        if poll_message is not None:
            # Опрос в чате есть, но без записи его не подсчитать.
            logger.exception("Не удалось сохранить опрос готовности")
            await _abandon_poll(bot, registry, run, poll_message, chat_id, logger)
        elif sending_poll and isinstance(exc, (TelegramNetworkError,
                                               asyncio.TimeoutError)):
            # Ответ Telegram потерян: опрос мог появиться в чате. Месяц
            # остаётся занятым (без автоповтора), админ проверяет чат.
            registry.close_readiness_run(run["id"])
            registry.mark_readiness_run(
                run["id"], poll_stopped=True, result_sent=True,
                closed_announced=True,
            )
            raise ValueError(
                "Связь с Telegram прервалась при отправке опроса — проверьте "
                "чат служения: опрос мог появиться. Если его нет — «▶️ "
                "Запустить заново»."
            ) from exc
        else:
            # Без опроса вступление в чате лишнее: повторный запуск
            # прислал бы второе.
            for message_id in intro_ids:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:  # noqa: BLE001
                    logger.info("Вступление опроса не удалено", exc_info=True)
            registry.delete_readiness_run(run["id"])
        raise


async def _abandon_poll(bot, registry, run: dict, poll_message, chat_id: int,
                        logger: logging.Logger) -> None:
    """Опрос отправлен, но не привязан к записи. Остановлен — запись не
    нужна; не остановлен — запись остаётся закрытой с ID опроса, и
    ежеминутная проверка остановит его позже."""
    try:
        await bot.stop_poll(chat_id, poll_message.message_id)
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось остановить опрос готовности", exc_info=True)
        try:
            registry.attach_readiness_poll(
                run["id"], poll_id=poll_message.poll.id, chat_id=chat_id,
                message_id=poll_message.message_id, intro_message_id=None,
            )
            registry.close_readiness_run(run["id"])
            registry.mark_readiness_run(
                run["id"], result_sent=True, closed_announced=True,
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("Опрос готовности %s не сохранён", run["id"])
    registry.delete_readiness_run(run["id"])


# Голоса, пришедшие между отправкой опроса и записью его ID (доли секунды):
# придерживаются и учитываются сразу после записи.
_EARLY_ANSWERS: dict[str, list[tuple[float, tuple]]] = {}
_EARLY_TTL = 120


def hold_early_answer(poll_id: str, *answer) -> None:
    now = time_module.monotonic()
    for key in list(_EARLY_ANSWERS):
        _EARLY_ANSWERS[key] = [
            item for item in _EARLY_ANSWERS[key] if now - item[0] < _EARLY_TTL
        ]
        if not _EARLY_ANSWERS[key]:
            del _EARLY_ANSWERS[key]
    if sum(len(items) for items in _EARLY_ANSWERS.values()) < 500:
        _EARLY_ANSWERS.setdefault(poll_id, []).append((now, answer))


async def replay_early_answers(registry, poll_id: str,
                               logger: logging.Logger) -> None:
    for _, (telegram_id, username, full_name, option_ids) in _EARLY_ANSWERS.pop(
        poll_id, []
    ):
        run = registry.get_readiness_run_by_poll(poll_id)
        if run is None:
            return
        try:
            registry.record_readiness_answer(
                poll_id, telegram_id, username, full_name,
                answer_dates(option_ids, run["dates"]),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось учесть ранний ответ в опросе")


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
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            # Не писал боту, заблокировал или Telegram отклонил запрос —
            # повтор каждую минуту не поможет.
            logger.info(
                "Напоминание об опросе не доставлено %s: %s", telegram_id, exc
            )
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
    # «YYYY-MM#id» — опрос, отложенный перезапуском.
    year, month = map(int, run["month"].split("#")[0].split("-"))
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


_UNREACHABLE = ("chat not found", "user is deactivated", "bot was blocked")


def unreachable(exc: Exception) -> bool:
    """Получателю не доставить в принципе: повтор не поможет."""
    return isinstance(exc, TelegramForbiddenError) or (
        isinstance(exc, TelegramBadRequest)
        and any(mark in str(exc).lower() for mark in _UNREACHABLE)
    )


async def notify_admins(
    bot, registry, service_id: str, admin_id: int, text: str,
    logger: logging.Logger, text_limit: int, reply_markup=None,
    *, delivery_key: str | None = None, month: str | None = None,
) -> bool:
    """True — повторять не нужно: каждому админу уведомление доставлено или
    не может быть доставлено в принципе (не писал боту, заблокировал).
    False — был временный сбой, следующая проверка повторит."""
    from .admin_report import report_recipients

    done = True
    for recipient in report_recipients(registry, service_id, admin_id):
        # С delivery_key доставка отмечается на получателя: повтор идёт
        # только тем, у кого был временный сбой, без дублей остальным.
        kind = f"{delivery_key}:{recipient}" if delivery_key else None
        if kind and registry.has_readiness_notice(service_id, month, kind):
            continue
        try:
            await send_telegram_screen(
                bot.send_message, recipient, text, limit=text_limit,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001
            if not unreachable(exc):
                done = False
                logger.warning(
                    "Уведомление об опросе готовности не отправлено %s",
                    recipient, exc_info=True,
                )
                continue
            # Не писал боту или заблокировал — без трассировки и повторов.
            logger.info(
                "Уведомление об опросе готовности не доставлено %s: %s",
                recipient, exc,
            )
        if kind:
            registry.claim_readiness_notice(service_id, month, kind)
    return done


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


async def announce_closed(
    bot, registry, run: dict, service: dict, logger: logging.Logger
) -> bool:
    """Сообщение в чат служения, что опрос завершён (ответом на опрос).
    True — отправлено или отправлять некуда; False — временный сбой."""
    if not run.get("chat_id") or not run.get("message_id"):
        return True
    try:
        await send_to_service_group(
            bot.send_message, run["chat_id"],
            closed_text(
                service, run,
                registry.get_readiness_settings(service["id"]).get(
                    "closed_template"
                ),
            ),
            service, registry=registry, logger=logger, parse_mode=None,
            reply_parameters=ReplyParameters(
                message_id=run["message_id"], allow_sending_without_reply=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        if unreachable(exc):
            logger.info("Сообщение о завершении опроса не доставлено: %s", exc)
            return True
        logger.warning(
            "Сообщение о завершении опроса не отправлено", exc_info=True
        )
        return False
    return True


async def finish_run(
    bot, registry, run: dict, service: dict, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> None:
    """Шаги закрытия — остановка опроса в чате и итог админам; что не
    удалось, повторяет следующая проверка (без повтора удавшегося)."""
    if not run["poll_stopped"] and await stop_run_poll(bot, run, logger):
        registry.mark_readiness_run(run["id"], poll_stopped=True)
        run = {**run, "poll_stopped": 1}
    # Сообщение в чат — после остановки опроса; не дошло — повтор.
    if run["poll_stopped"] and not run.get("closed_announced") and (
        await announce_closed(bot, registry, run, service, logger)
    ):
        registry.mark_readiness_run(run["id"], closed_announced=True)
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
    # Одно сообщение: доставка отмечается на получателя целиком (длинный
    # итог сокращается, полностью — «📊 Итог опроса»).
    text = fit_one_screen("🗳 Опрос завершён\n\n" + text, text_limit)
    if await notify_admins(
        bot, registry, service["id"], admin_id, text, logger, text_limit,
        reply_markup=admin_keyboard(service["id"]),
        delivery_key=f"result-{run['id']}", month=run["month"],
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

    async def tell(text: str) -> bool:
        return await notify_admins(
            bot, registry, service["id"], admin_id, text, logger, text_limit,
            reply_markup=admin_keyboard(service["id"]),
        )

    async def tell_missed(year: int, month: int, key: str) -> None:
        if registry.claim_readiness_notice(service["id"], key, "missed"):
            # Не дошло из-за временного сбоя — следующая проверка повторит.
            if not await tell(
                f"⚠️ Опрос готовности {service['name']} на "
                f"{month_title(year, month)} не был запущен вовремя — "
                "бот не работал. Запустите вручную или пропустите месяц."
            ):
                registry.release_readiness_notice(service["id"], key, "missed")

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
                if not await tell(
                    preview_text(service, people, year, month, start, tz)
                ):
                    registry.release_readiness_notice(
                        service["id"], key, "preview"
                    )
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
        if not current["poll_id"]:
            # Запись без опроса в чате: запуск прервался между записью и
            # отправкой. Через 5 минут — убрать, месяц снова свободен.
            created = datetime.fromisoformat(current["created_at"])
            if now - created > timedelta(minutes=5):
                registry.delete_readiness_run(current["id"])
                logger.warning(
                    "Удалена запись опроса %s без опроса в чате", current["id"]
                )
            return
        if now >= deadline_of(current):
            await close_run(
                bot, registry, current, service, tz, admin_id, logger,
                text_limit,
            )
        elif reminder_due(current, settings, now):
            # Отметка — на человека после доставки: идемпотентно.
            await send_reminders(bot, registry, current, service, tz, logger)


def settle_parked_run(registry, parked: dict, logger: logging.Logger) -> None:
    """Перезапуск прервался. Вызывать под service_lock: снимок отложенного
    опроса перечитывается — перезапуск мог уже вернуть его на место."""
    parked = registry.get_readiness_run_by_id(parked["id"])
    if parked is None or "#" not in parked["month"]:
        return
    key = parked["month"].split("#")[0]
    current = registry.get_readiness_run(parked["service_id"], key)
    if current is not None and current["status"] == "open" and not (
        current["poll_id"]
    ):
        # Запись нового опроса без опроса в чате (бот упал до отправки) —
        # нового опроса нет.
        registry.delete_readiness_run(current["id"])
        current = None
    if current is not None and current["id"] != parked["id"] and (
        current["status"] != "skipped"
    ):
        # Новый опрос есть — старый с ответами больше не нужен.
        registry.delete_readiness_run(parked["id"])
        logger.info("Удалён опрос %s, отложенный перезапуском", parked["id"])
        return
    if current is not None and current["status"] == "skipped":
        # Пропуск месяца не должен стереть ответы отложенного опроса.
        registry.delete_readiness_run(current["id"])
    registry.move_readiness_run(parked["id"], key)
    registry.close_readiness_run(parked["id"])
    # Опрос в чате уже остановлен; итог и сообщение о завершении — обычным
    # порядком (если ещё не были).
    registry.mark_readiness_run(parked["id"], poll_stopped=True)
    logger.info("Возвращён опрос %s после прерванного перезапуска", parked["id"])


async def run_readiness_tick(
    bot, registry, now: datetime, tz, admin_id: int,
    logger: logging.Logger, text_limit: int,
) -> None:
    """Раз в минуту: превью накануне, автозапуск, напоминание, срок.
    Сбой одного служения не мешает остальным."""
    for parked in registry.list_parked_readiness_runs():
        try:
            async with service_lock(parked["service_id"]):
                settle_parked_run(registry, parked, logger)
        except Exception:  # noqa: BLE001
            logger.exception("Сбой разбора отложенного опроса %s", parked["id"])
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
                run["id"], poll_stopped=True, result_sent=True,
                closed_announced=True,
            )
            continue
        try:
            async with service_lock(service["id"]):
                # Перечитать под блокировкой: «⏹ Завершить» могло только что
                # закрыть опрос и отправить итог — без повтора.
                current = registry.get_readiness_run_by_id(run["id"])
                if current is None or (
                    current["poll_stopped"] and current["result_sent"]
                    and current["closed_announced"]
                ):
                    continue
                await finish_run(
                    bot, registry, current, service, tz, admin_id, logger,
                    text_limit,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Сбой закрытия опроса готовности %s", run["id"])
