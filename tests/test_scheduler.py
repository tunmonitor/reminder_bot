import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.scheduler import add_service_reminder_jobs, scheduled_reminder_date


class ServiceReminderSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_deduplicates_and_preserves_unchanged_jobs(self):
        scheduler = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
        callback = AsyncMock()
        services = [
            {
                "id": "assembly",
                "reminders": [
                    {"id": 11, "reminder_days": 3, "reminder_time": "18:00"},
                    {"id": 12, "reminder_days": 1, "reminder_time": "09:15"},
                    {"id": 13, "reminder_days": 3, "reminder_time": "18:00"},
                ],
            },
            {
                "id": "broadcast",
                "reminders": [
                    {"id": 19, "reminder_days": 2, "reminder_time": "12:30"}
                ],
            },
        ]
        logger = MagicMock()

        await add_service_reminder_jobs(
            scheduler, services, callback, ZoneInfo("UTC"), logger
        )
        original = scheduler.get_job("service-reminder-broadcast-19")
        await add_service_reminder_jobs(
            scheduler, services, callback, ZoneInfo("UTC"), logger
        )

        self.assertEqual(
            {job.id for job in scheduler.get_jobs()},
            {
                "service-reminder-assembly-11",
                "service-reminder-assembly-12",
                "service-reminder-broadcast-19",
            },
        )
        self.assertIs(
            scheduler.get_job("service-reminder-broadcast-19"), original
        )
        self.assertEqual(callback.await_count, 0)

    async def test_changed_due_job_runs_before_it_is_replaced(self):
        tz = ZoneInfo("UTC")
        scheduler = AsyncIOScheduler(timezone=tz)
        callback = AsyncMock()
        logger = MagicMock()
        old_rule = {"id": 11, "reminder_days": 3, "reminder_time": "18:00"}
        await add_service_reminder_jobs(
            scheduler,
            [{"id": "assembly", "reminders": [old_rule]}],
            callback,
            tz,
            logger,
        )
        scheduler.start(paused=True)
        old_job = scheduler.get_job("service-reminder-assembly-11")
        scheduled_time = datetime.now(tz) - timedelta(minutes=1)
        old_job.modify(next_run_time=scheduled_time)

        new_rule = {"id": 11, "reminder_days": 2, "reminder_time": "18:00"}
        await add_service_reminder_jobs(
            scheduler,
            [{"id": "assembly", "reminders": [new_rule]}],
            callback,
            tz,
            logger,
        )

        callback.assert_awaited_once_with("assembly", old_rule)
        self.assertEqual(
            scheduler.get_job("service-reminder-assembly-11").args,
            ("assembly", new_rule),
        )

    def test_delayed_cron_execution_recovers_previous_local_date(self):
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 10, 15, 0, 20, tzinfo=tz)

        self.assertEqual(
            scheduled_reminder_date(now, "23:55").isoformat(),
            "2026-10-14",
        )
        self.assertEqual(
            scheduled_reminder_date(now, "00:15").isoformat(),
            "2026-10-15",
        )


if __name__ == "__main__":
    unittest.main()
