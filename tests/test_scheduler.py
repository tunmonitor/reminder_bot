import unittest
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.scheduler import add_service_reminder_jobs


class ServiceReminderSchedulerTest(unittest.TestCase):
    def test_registers_and_replaces_a_job_for_each_rule(self):
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

        add_service_reminder_jobs(
            scheduler, services, callback, ZoneInfo("UTC"), logger
        )
        add_service_reminder_jobs(
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


if __name__ == "__main__":
    unittest.main()
