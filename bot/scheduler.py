"""Планировщик APScheduler на базе cron-триггеров."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


def scheduled_reminder_date(now: datetime, reminder_time: str) -> date:
    """Recover yesterday's cron date when execution is delayed past midnight."""
    scheduled_time = time(
        hour=int(reminder_time[:2]), minute=int(reminder_time[3:])
    )
    run_date = now.date()
    if now.timetz().replace(tzinfo=None) < scheduled_time:
        run_date -= timedelta(days=1)
    return run_date


async def add_service_reminder_jobs(
    scheduler: AsyncIOScheduler,
    services: list[dict],
    callback: Callable[[str, dict], Awaitable[None]],
    tz: ZoneInfo,
    debug_logger: logging.Logger,
) -> None:
    now = datetime.now(tz)
    desired: dict[str, tuple[str, dict, CronTrigger]] = {}
    for service in services:
        reminders_by_schedule = {}
        for reminder in service["reminders"]:
            schedule = (
                reminder["reminder_days"],
                reminder["reminder_time"],
            )
            current = reminders_by_schedule.get(schedule)
            if current is None or reminder["id"] < current["id"]:
                reminders_by_schedule[schedule] = reminder
        for reminder in reminders_by_schedule.values():
            reminder_id = reminder["id"]
            trigger = CronTrigger(
                hour=int(reminder["reminder_time"][:2]),
                minute=int(reminder["reminder_time"][3:]),
                timezone=tz,
            )
            job_id = f"service-reminder-{service['id']}-{reminder_id}"
            desired[job_id] = (
                service["id"],
                reminder.copy(),
                trigger,
            )

    existing = {
        job.id: job
        for job in scheduler.get_jobs()
        if job.id.startswith("service-reminder-")
    }
    unchanged = set()
    for job_id, (service_id, reminder, trigger) in desired.items():
        job = existing.get(job_id)
        if (
            job is not None
            and str(job.trigger) == str(trigger)
            and tuple(job.args) == (service_id, reminder)
            and not job.kwargs
        ):
            unchanged.add(job_id)
    for job_id, job in existing.items():
        if job_id in unchanged:
            continue
        next_run_time = getattr(job, "next_run_time", None)
        if next_run_time is not None and next_run_time <= now:
            await callback(job.args[0], job.args[1])
        scheduler.remove_job(job_id)
    for job_id, (service_id, reminder, trigger) in desired.items():
        if job_id in unchanged:
            continue
        scheduler.add_job(
            callback,
            trigger,
            args=(service_id, reminder),
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=24 * 60 * 60,
        )
        debug_logger.debug(
            "Зарегистрировано уведомление служения %s (%s): %s",
            service_id,
            reminder_id,
            trigger,
        )


def build_scheduler(
    tz: ZoneInfo,
    jobs: list[dict],
    callback: Callable[[], Awaitable[None]],
    debug_logger: logging.Logger,
) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=tz)
    for job in jobs:
        try:
            trigger = CronTrigger(
                day_of_week=job.get("day_of_week"),
                hour=int(job.get("hour", 0)),
                minute=int(job.get("minute", 0)),
                timezone=tz,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Некорректная запись расписания {job!r}: {exc}"
            ) from exc

        job_id = job.get("id") or (
            f"job_{job.get('day_of_week')}_{job.get('hour')}_{job.get('minute', 0)}"
        )
        sched.add_job(callback, trigger, id=job_id, replace_existing=True)
        debug_logger.debug("Зарегистрировано задание '%s' → %s", job_id, trigger)
    return sched
