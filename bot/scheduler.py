"""Планировщик APScheduler на базе cron-триггеров."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


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
