import asyncio
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot.app import _prepare_service_test_previews, get_group_reminder_preview
from bot.service_registry import ServiceRegistry


class ServicePreviewTest(unittest.TestCase):
    def _group_reminder_preview(self, registry, temp_dir, assignments):
        reminder = registry.list_group_reminders("sound")[0]
        credentials = SimpleNamespace(
            admin_tg_id=67890,
            yandex_public_url="https://example.invalid/schedule",
        )

        async def run():
            with patch(
                "bot.app._next_sunday_in_tz",
                return_value=date(2026, 10, 25),
            ), patch(
                "bot.app._load_service_task_assignments",
                new=AsyncMock(return_value=(assignments, True)),
            ):
                return await get_group_reminder_preview(
                    reminder["id"],
                    credentials,
                    Path(temp_dir),
                    ZoneInfo("UTC"),
                    registry,
                    logging.getLogger("test-group-reminder-preview"),
                )

        return asyncio.run(run()), reminder

    def test_group_reminder_preview_uses_default_template_and_informational_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_service_default_template(
                67890,
                "sound",
                "{service_name} · {sunday_date}\n{task_assignments}\n"
                "{informational_notes}",
            )
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 12345, "alex"
            )
            result, reminder = self._group_reminder_preview(
                registry,
                temp_dir,
                {
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    },
                    "2-я часть": {
                        "names": ["нет"],
                        "is_informational": True,
                    },
                },
            )

            self.assertTrue(result["has_data"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["date"], "25.10.2026")
            self.assertEqual(result["time"], reminder["reminder_time"])
            self.assertIn("Звук · 25.10.2026", result["text"])
            self.assertIn("Алексей (@alex)", result["text"])
            self.assertIn("📌 2-я часть — нет", result["text"])

    def test_group_reminder_preview_falls_back_to_builtin_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            result, _ = self._group_reminder_preview(
                registry,
                temp_dir,
                {"Микшер": ["Алексей"]},
            )

            self.assertTrue(result["has_data"])
            self.assertIn(
                "Напоминание о служении «Звук» в воскресенье 25.10.2026:",
                result["text"],
            )
            self.assertIn("Микшер", result["text"])

    def test_group_reminder_preview_uses_reminder_body_when_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder_id = registry.add_group_reminder(
                67890,
                "sound",
                2,
                "11:00",
                "Свое сообщение {service_name}: {informational_notes}",
            )
            reminder = registry.get_group_reminder(reminder_id)
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app._next_sunday_in_tz",
                    return_value=date(2026, 10, 25),
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=({}, True)),
                ):
                    return await get_group_reminder_preview(
                        reminder_id,
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-custom-reminder-preview"),
                    )

            result = asyncio.run(run())

            self.assertTrue(result["has_data"])
            self.assertEqual(result["time"], "11:00")
            self.assertEqual(result["text"], "Свое сообщение Звук: ")

    def test_group_reminder_preview_leaves_informational_notes_empty_when_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_service_default_template(
                67890, "sound", "Текст {informational_notes}"
            )

            result, _ = self._group_reminder_preview(
                registry,
                temp_dir,
                {
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    }
                },
            )

            self.assertTrue(result["has_data"])
            self.assertEqual(result["text"], "Текст ")
            self.assertNotIn("📌", result["text"])

    def test_group_reminder_preview_reports_missing_event_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder = registry.list_group_reminders("sound")[0]
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app._next_sunday_in_tz",
                    return_value=date(2026, 10, 25),
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=({}, False)),
                ):
                    return await get_group_reminder_preview(
                        reminder["id"],
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-missing-reminder-data"),
                    )

            result = asyncio.run(run())

            self.assertFalse(result["has_data"])
            self.assertEqual(result["error"], "Нет данных из графика на 25.10.2026.")
            self.assertEqual(result["text"], "")

    def test_group_reminder_preview_reports_schedule_download_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder = registry.list_group_reminders("sound")[0]
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app._next_sunday_in_tz",
                    return_value=date(2026, 10, 25),
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(side_effect=RuntimeError("download failed")),
                ):
                    return await get_group_reminder_preview(
                        reminder["id"],
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-download-failure-preview"),
                    )

            result = asyncio.run(run())

            self.assertFalse(result["has_data"])
            self.assertEqual(
                result["error"],
                "Не удалось загрузить график. Попробуйте позже.",
            )

    def test_group_reminder_preview_reports_unsupported_template_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder_id = registry.list_group_reminders("sound")[0]["id"]
            with closing(sqlite3.connect(registry.path)) as connection:
                connection.execute(
                    "UPDATE group_reminders SET body = ? WHERE id = ?",
                    ("Значение: {foo}", reminder_id),
                )
                connection.commit()
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app._next_sunday_in_tz",
                    return_value=date(2026, 10, 25),
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=({}, True)),
                ):
                    return await get_group_reminder_preview(
                        reminder_id,
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-template-error-preview"),
                    )

            result = asyncio.run(run())

            self.assertTrue(result["has_data"] is False)
            self.assertEqual(
                result["error"],
                "Ошибка в шаблоне: поле {foo} не поддерживается.",
            )

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
                self.assertEqual(
                    load_assignments.await_args.kwargs["only_date"],
                    result["sunday_date"],
                )
                return result

            preview = asyncio.run(run())

            self.assertIn("Звук", preview["group_text"])
            self.assertIn("🎚️ Микшер\n• Алексей (@alex)", preview["group_text"])
            self.assertNotIn("12345", preview["group_text"])
            self.assertIn("• Мария · не привязан", preview["group_text"])
            self.assertIn("{task_assignments}", preview["example_group_template"])
            self.assertIn("Алексей (@alex)", preview["example_group_text"])
            self.assertNotIn("12345", preview["example_group_text"])
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

    def test_preview_excludes_informational_tasks_from_group_and_personal_texts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_responsible_contact(
                67890, "sound", "Праздник", "Дежурный", 12345
            )
            assignments = {
                "Микшер": {
                    "names": ["Алексей"],
                    "is_informational": False,
                },
                "Праздник": {
                    "names": ["Дежурный"],
                    "is_informational": True,
                },
            }

            async def run():
                with patch(
                    "bot.app._next_sunday_in_tz",
                    return_value=date(2026, 10, 25),
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=assignments),
                ):
                    return await _prepare_service_test_previews(
                        "sound",
                        SimpleNamespace(admin_tg_id=67890),
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-info-preview"),
                    )

            preview = asyncio.run(run())

            self.assertIn("Микшер", preview["group_text"])
            self.assertNotIn("Праздник", preview["group_text"])
            self.assertEqual(
                [item["task"] for item in preview["personal_messages"]],
                ["Микшер"],
            )

    def test_task_loader_can_filter_assignments_to_one_sunday(self):
        from bot.app import _load_service_task_assignments

        target_sunday = date(2026, 10, 25)
        next_sunday = date(2026, 11, 1)
        parsed_events = {
            "sound": [
                SimpleNamespace(
                    event_date=target_sunday,
                    assignments=[
                        SimpleNamespace(
                            task="Звук",
                            name="Алексей",
                            is_informational=False,
                        ),
                        SimpleNamespace(
                            task="Праздник",
                            name="",
                            is_informational=True,
                        ),
                    ],
                ),
                SimpleNamespace(
                    event_date=next_sunday,
                    assignments=[
                        SimpleNamespace(
                            task="Звук", name="Мария",
                            is_informational=False,
                        ),
                        SimpleNamespace(
                            task="Звук", name="",
                            is_informational=True,
                        ),
                        SimpleNamespace(
                            task="Слайды", name="Иван",
                            is_informational=False,
                        ),
                    ],
                ),
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = SimpleNamespace(yandex_public_url="public-url")
            logger = logging.getLogger("test-service-date-filter")

            async def run():
                with (
                    patch(
                        "bot.app._next_sunday_in_tz",
                        side_effect=[target_sunday, next_sunday],
                    ),
                    patch("bot.app.download_public", new=AsyncMock()),
                    patch(
                        "bot.app.parse_service_events",
                        return_value=parsed_events,
                    ) as parse_events,
                ):
                    month_assignments = await _load_service_task_assignments(
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        "sound",
                        logger,
                    )
                    sunday_assignments = await _load_service_task_assignments(
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        "sound",
                        logger,
                        only_date=target_sunday,
                    )
                self.assertEqual(parse_events.call_count, 2)
                self.assertEqual(
                    [call.args[1] for call in parse_events.call_args_list],
                    [target_sunday, target_sunday],
                )
                return month_assignments, sunday_assignments

            month_assignments, sunday_assignments = asyncio.run(run())

        self.assertEqual(
            month_assignments,
            {
                "Звук": {
                    "names": ["Алексей", "Мария"],
                    "is_informational": False,
                },
                "Праздник": {"names": [], "is_informational": True},
                "Слайды": {"names": ["Иван"], "is_informational": False},
            },
        )
        self.assertEqual(
            sunday_assignments,
            {
                "Звук": {
                    "names": ["Алексей"],
                    "is_informational": False,
                },
                "Праздник": {"names": [], "is_informational": True},
            },
        )

    def test_malformed_template_is_reported_as_clear_value_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            with closing(sqlite3.connect(registry.path)) as connection:
                connection.execute(
                    "UPDATE services SET group_template = ? WHERE id = ?",
                    ("{service_name.attribute}", "sound"),
                )
                connection.commit()
            with patch(
                "bot.app._load_service_task_assignments",
                new=AsyncMock(return_value={"Звук": ["Алексей"]}),
            ):
                with self.assertRaisesRegex(
                    ValueError, "Некорректный групповой шаблон"
                ):
                    asyncio.run(
                        _prepare_service_test_previews(
                            "sound",
                            SimpleNamespace(admin_tg_id=67890),
                            Path(temp_dir),
                            ZoneInfo("UTC"),
                            registry,
                            logging.getLogger("test-invalid-template"),
                        )
                    )

    def test_preview_renders_templates_under_guided_setup_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 12345, "alex"
            )
            assignments = {"Микшер": ["Алексей"]}

            async def run():
                with patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=assignments),
                ):
                    return await _prepare_service_test_previews(
                        "sound",
                        SimpleNamespace(admin_tg_id=67890),
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-service-template-overrides"),
                        group_template="Новый {service_name}: {task_assignments}",
                        personal_templates={
                            "Микшер": "{name} ({telegram_id}, @{username})"
                        },
                    )

            previews = asyncio.run(run())
            self.assertIn("Новый Звук", previews["group_text"])
            self.assertIn("Алексей (@alex)", previews["group_text"])
            self.assertNotIn("12345", previews["group_text"])
            self.assertEqual(
                previews["personal_messages"][0]["text"],
                "Алексей (12345, @alex)",
            )


if __name__ == "__main__":
    unittest.main()
