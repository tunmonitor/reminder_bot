"""Планировщик APScheduler на базе cron-триггеров."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


def add_service_reminder_jobs(
    scheduler: AsyncIOScheduler,
    services: list[dict],
    callback: Callable[[str, dict], Awaitable[None]],
    tz: ZoneInfo,
    debug_logger: logging.Logger,
) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("service-reminder-"):
            scheduler.remove_job(job.id)
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

            async def run_reminder(
                selected_service_id=service["id"],
                selected_reminder=reminder.copy(),
            ) -> None:
                await callback(selected_service_id, selected_reminder)

            scheduler.add_job(
                run_reminder,
                trigger,
                id=job_id,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            debug_logger.debug(
                "Зарегистрировано уведомление служения %s (%s): %s",
                service["id"],
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
