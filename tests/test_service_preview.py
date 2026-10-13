import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot.app import _prepare_service_test_previews
from bot.service_registry import ServiceRegistry


class ServicePreviewTest(unittest.TestCase):
    def test_preview_uses_selected_service_assignments_and_contacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=1,
                reminder_time="18:00",
                group_template=(
                    "{service_name} {sunday_date}\n{task_assignments}"
                ),
                personal_templates={
                    "Микшер": "{name}: {task} для {service_name} "
                    "на {sunday_date} ({telegram_id}, {username})"
                },
            )
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 12345, "alex"
            )
            assignments = {
                "Микшер": ["Алексей", "Мария"],
                "Презентация": ["Иван"],
            }

            async def run():
                with patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=assignments),
                ) as load_assignments:
                    result = await _prepare_service_test_previews(
                        "sound",
                        SimpleNamespace(admin_tg_id=67890),
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-service-preview"),
                    )
                load_assignments.assert_awaited_once()
                self.assertEqual(
                    load_assignments.await_args.args[3], "sound"
                )
                return result

            preview = asyncio.run(run())

            self.assertIn("Звук", preview["group_text"])
            self.assertIn("Микшер: Алексей", preview["group_text"])
            self.assertIn("ID 12345, @alex", preview["group_text"])
            self.assertIn("Мария (Telegram не привязан)", preview["group_text"])
            self.assertEqual(len(preview["personal_messages"]), 3)
            alex_message = preview["personal_messages"][0]
            self.assertEqual(alex_message["telegram_id"], "12345")
            self.assertIn("Алексей: Микшер для Звук", alex_message["text"])
            self.assertTrue(
                any("Микшер — Мария" in issue for issue in preview["issues"])
            )
            self.assertTrue(
                any("Презентация — Иван" in issue for issue in preview["issues"])
            )


if __name__ == "__main__":
    unittest.main()
