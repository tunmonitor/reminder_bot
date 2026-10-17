import stat
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bot.service_registry import ServiceRegistry


class ServiceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "private" / "services.sqlite3"
        self.registry = ServiceRegistry(self.path)
        self.registry.initialize(1001, -10001)

    def test_change_callback_runs_only_for_committed_writes(self):
        changes = []
        registry = ServiceRegistry(
            self.path, on_change=lambda: changes.append("changed")
        )
        registry.initialize(1001, -10001)
        changes.clear()

        registry.list_services()
        self.assertEqual(changes, [])

        registry.set_group_chat(1001, "sound", -10055, enabled=True)
        self.assertEqual(changes, ["changed"])

    def test_bootstrap_seeds_catalog_and_configures_only_assembly(self):
        services = {service["id"]: service for service in self.registry.list_services()}

        self.assertEqual(len(services), 8)
        self.assertTrue(services["assembly"]["enabled"])
        self.assertEqual(services["assembly"]["group_chat_id"], -10001)
        for service_id in (
            "sound", "rehearsal", "slides", "broadcast",
            "order", "kitchen", "coffee",
        ):
            self.assertFalse(services[service_id]["enabled"])
            self.assertIsNone(services[service_id]["group_chat_id"])
        self.assertTrue(self.registry.is_super_admin(1001))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077, 0
        )

    def test_bootstrap_is_idempotent_and_does_not_promote_new_env_admin(self):
        self.registry.set_group_chat(1001, "sound", -10002, enabled=True)

        self.registry.initialize(2002, -19999)

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertTrue(self.registry.is_super_admin(1001))
        self.assertFalse(self.registry.is_super_admin(2002))
        self.assertEqual(services["assembly"]["group_chat_id"], -10001)
        self.assertEqual(services["sound"]["group_chat_id"], -10002)

    def test_initialization_preserves_assembly_chat_and_enabled_state(self):
        self.registry.set_group_chat(1001, "assembly", -10055, enabled=False)

        self.registry.initialize(1001, -19999)

        assembly = self.registry.get_service("assembly")
        self.assertEqual(assembly["group_chat_id"], -10055)
        self.assertFalse(assembly["enabled"])

    def test_disabling_service_preserves_its_group_chat(self):
        self.registry.set_group_chat(1001, "sound", -10055, enabled=True)

        self.registry.set_service_enabled(1001, "sound", False)

        sound = self.registry.get_service("sound")
        self.assertEqual(sound["group_chat_id"], -10055)
        self.assertFalse(sound["enabled"])

    def test_share_request_ids_remain_unique_after_registry_restart(self):
        first_request_id = self.registry.next_share_request_id()
        self.registry.initialize(1001, -19999)

        second_request_id = self.registry.next_share_request_id()

        self.assertGreater(second_request_id, first_request_id)

    def test_no_op_admin_changes_do_not_create_audit_entries(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.remove_service_admin(1001, "sound", 9999)

        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM configuration_audit "
                "WHERE action IN ('service_admin_added', 'service_admin_removed')"
            ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_only_super_admin_can_manage_service_admins_and_contacts(self):
        with self.assertRaises(PermissionError):
            self.registry.add_service_admin(2002, "sound", 3003)

        self.registry.add_service_admin(1001, "sound", 3003)
        self.assertTrue(self.registry.is_service_admin(3003, "sound"))
        self.assertFalse(self.registry.is_service_admin(3003, "slides"))
        self.assertEqual(self.registry.list_service_admins("sound"), [3003])

        self.registry.remove_service_admin(1001, "sound", 3003)
        self.assertFalse(self.registry.is_service_admin(3003, "sound"))

    def test_admins_can_be_shared_across_services_and_have_usernames(self):
        self.registry.add_service_admin(1001, "sound", 3003, "sound_user")
        self.registry.add_service_admin(1001, "sound", 4004, "@other_user")
        self.registry.add_service_admin(1001, "slides", 3003, "sound_user")

        self.assertEqual(
            self.registry.list_service_admin_details("sound"),
            [
                {"telegram_id": 3003, "username": "sound_user"},
                {"telegram_id": 4004, "username": "other_user"},
            ],
        )
        self.assertEqual(
            self.registry.list_service_admin_details("slides"),
            [{"telegram_id": 3003, "username": "sound_user"}],
        )
        self.assertTrue(self.registry.is_service_admin(3003, "sound"))
        self.assertTrue(self.registry.is_service_admin(3003, "slides"))

    def test_existing_admin_username_can_be_refreshed_without_duplicate_audit(self):
        self.registry.add_service_admin(1001, "sound", 3003)

        self.registry.add_service_admin(1001, "sound", 3003, "@new_user")
        self.registry.add_service_admin(1001, "sound", 3003, "@new_user")

        self.assertEqual(
            self.registry.list_service_admin_details("sound"),
            [{"telegram_id": 3003, "username": "new_user"}],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM configuration_audit "
                "WHERE action = 'service_admin_added'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_initialization_adds_username_column_to_existing_admin_table(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                ALTER TABLE service_admins RENAME TO service_admins_with_username;
                CREATE TABLE service_admins (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    created_by INTEGER NOT NULL CHECK (created_by > 0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, telegram_id)
                );
                INSERT INTO service_admins (
                    service_id, telegram_id, created_by, created_at
                )
                SELECT service_id, telegram_id, created_by, created_at
                FROM service_admins_with_username;
                DROP TABLE service_admins_with_username;
                """
            )
            connection.commit()

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.list_service_admin_details("sound"),
            [{"telegram_id": 3003, "username": ""}],
        )

    def test_super_admin_configures_group_and_manual_telegram_mapping(self):
        self.registry.set_group_chat(1001, "sound", -10022, enabled=True)
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "@alex"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 5005, "alex_new"
        )

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertEqual(services["sound"]["group_chat_id"], -10022)
        self.assertTrue(services["sound"]["enabled"])
        self.assertEqual(
            self.registry.get_responsible_contact(
                "sound", "Микшер", "Алексей"
            ),
            {"telegram_id": 5005, "username": "alex_new"},
        )

    def test_notification_template_validation_rejects_invalid_fields_and_syntax(self):
        for group_template, personal_templates in (
            ("{unknown}", {}),
            ("{service_name", {}),
            ("{service_name!r}", {}),
            (None, {"Микшер": "{name.attribute}"}),
            (None, {"Микшер": "{missing}"}),
            (None, {"Микшер": "{name"}),
        ):
            with self.subTest(
                group_template=group_template,
                personal_templates=personal_templates,
            ):
                with self.assertRaises(ValueError):
                    self.registry.set_notification_settings(
                        1001,
                        "sound",
                        reminder_days=1,
                        reminder_time="18:00",
                        group_template=group_template,
                        personal_templates=personal_templates,
                    )

    def test_notification_template_validation_accepts_supported_fields(self):
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=1,
            reminder_time="18:00",
            group_template="{service_name} {sunday_date} {task_assignments}",
            personal_templates={
                "Микшер": (
                    "{name} {task} {service_name} {sunday_date} "
                    "{telegram_id} {username}"
                )
            },
        )

        service = self.registry.get_service("sound")
        self.assertIn("{service_name}", service["group_template"])
        self.assertIn("{telegram_id}", service["personal_templates"]["Микшер"])

    def test_user_notification_preference_defaults_to_disabled_and_is_per_service(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.set_responsible_contact(
            1001, "slides", "Слайды", "Алексей", 4004
        )

        self.assertFalse(
            self.registry.get_user_notifications_enabled(4004, "sound")
        )
        self.assertEqual(
            self.registry.list_users_with_notifications_enabled("sound"), []
        )

        self.registry.set_user_notifications_enabled(4004, "sound", True)

        self.assertTrue(
            self.registry.get_user_notifications_enabled(4004, "sound")
        )
        self.assertFalse(
            self.registry.get_user_notifications_enabled(4004, "slides")
        )
        self.assertEqual(
            self.registry.list_users_with_notifications_enabled("sound"),
            [4004],
        )

        self.registry.set_user_notifications_enabled(4004, "sound", False)
        self.assertEqual(
            self.registry.list_users_with_notifications_enabled("sound"), []
        )

    def test_preference_table_migration_preserves_contacts_and_defaults_false(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE user_notification_prefs")

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.get_responsible_contact(
                "sound", "Микшер", "Алексей"
            )["telegram_id"],
            4004,
        )
        self.assertFalse(
            self.registry.get_user_notifications_enabled(4004, "sound")
        )

    def test_migrates_previous_preference_column_without_losing_enabled_value(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE user_notification_prefs")
            connection.execute(
                """
                CREATE TABLE user_notification_prefs (
                    service_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    user_notifications_enabled INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (service_id, telegram_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO user_notification_prefs VALUES ('sound', 4004, 1)"
            )

        self.registry.initialize(1001, -10001)

        self.assertTrue(
            self.registry.is_user_notifications_enabled(4004, "sound")
        )

    def test_notification_preference_requires_registered_user_and_boolean(self):
        with self.assertRaisesRegex(ValueError, "не привязан"):
            self.registry.set_user_notifications_enabled(4004, "sound", True)
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        with self.assertRaisesRegex(ValueError, "должен быть bool"):
            self.registry.set_user_notifications_enabled(4004, "sound", 1)

    def test_user_notification_state_and_bulk_toggle_keep_services_independent(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.set_responsible_contact(
            1001, "slides", "Слайды", "Алексей", 4004
        )
        sound_text_id = self.registry.upsert_user_text(
            4004, "sound", "Микшер", None
        )
        self.registry.add_schedule(sound_text_id, 2, "09:00")
        self.registry.add_schedule(sound_text_id, 0, "18:00")
        slides_text_id = self.registry.upsert_user_text(
            4004, "slides", "Слайды", None
        )
        self.registry.add_schedule(slides_text_id, 1, "07:00")

        self.assertEqual(
            self.registry.list_user_services(4004), ["slides", "sound"]
        )
        self.assertEqual(
            self.registry.get_user_notifications_state(4004),
            {
                "total_services": 2,
                "enabled_count": 0,
                "disabled_count": 2,
                "per_service": [
                    {
                        "service_id": "slides",
                        "enabled": False,
                        "texts_count": 1,
                        "schedules_count": 1,
                    },
                    {
                        "service_id": "sound",
                        "enabled": False,
                        "texts_count": 1,
                        "schedules_count": 2,
                    },
                ],
            },
        )

        self.registry.set_user_notifications_all(4004, True)
        self.assertEqual(
            self.registry.get_user_notifications_state(4004)["enabled_count"],
            2,
        )
        self.registry.set_user_notifications_all(4004, False)
        self.assertEqual(
            self.registry.get_user_notifications_state(4004)["enabled_count"],
            0,
        )
        self.assertEqual(len(self.registry.list_schedules(sound_text_id)), 2)
        self.assertEqual(len(self.registry.list_schedules(slides_text_id)), 1)

    def test_user_task_list_only_exposes_the_callers_assignments(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Свет", "Борис", 5005
        )

        self.assertEqual(
            self.registry.list_user_tasks(4004, "sound"), ["Микшер"]
        )
        self.assertEqual(self.registry.list_user_tasks(6006, "sound"), [])

    def test_user_text_fallback_override_and_schedule_cascade(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        template = "Default {name} for {task} at {service_name}, {sunday_date}"
        text_id = self.registry.upsert_user_text(
            4004, "sound", "Микшер", None
        )
        self.assertEqual(
            self.registry.resolve_text_for_user(
                4004, "sound", "Микшер", {"Микшер": template}
            ),
            template,
        )
        schedule = self.registry.add_schedule(text_id, 2, "09:00")
        self.registry.add_schedule(text_id, 0, "18:00")
        self.assertEqual(len(self.registry.list_schedules(text_id)), 2)

        custom = "Personal {name}: {task}, {sunday_date}"
        self.registry.upsert_user_text(4004, "sound", "Микшер", custom)
        self.assertEqual(
            self.registry.resolve_text_for_user(
                4004, "sound", "Микшер", {"Микшер": template}
            ),
            custom,
        )
        self.registry.update_schedule(schedule["id"], enabled=False)
        self.assertFalse(
            self.registry.list_schedules(text_id)[0]["enabled"]
        )
        self.registry.delete_user_text(4004, "sound", "Микшер")
        self.assertIsNone(self.registry.get_user_text(4004, "sound", "Микшер"))
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM user_reminder_schedules "
                "WHERE text_id = ?",
                (text_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_ensure_user_text_preserves_custom_body(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        custom = "Personal {name}: {task}"
        text_id = self.registry.upsert_user_text(
            4004, "sound", "Микшер", custom
        )

        self.assertEqual(
            self.registry.ensure_user_text(4004, "sound", "Микшер"),
            text_id,
        )
        self.assertEqual(
            self.registry.get_user_text(4004, "sound", "Микшер")["body"],
            custom,
        )

    def test_reset_user_text_restores_default_body(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.upsert_user_text(
            4004, "sound", "Микшер", "Personal {name}: {task}"
        )

        self.registry.reset_user_text(4004, "sound", "Микшер")

        self.assertIsNone(
            self.registry.get_user_text(4004, "sound", "Микшер")["body"]
        )

    def test_changing_user_schedule_time_clears_only_its_delivery_history(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        text_id = self.registry.ensure_user_text(4004, "sound", "Микшер")
        first = self.registry.add_schedule(text_id, 3, "09:00")
        second = self.registry.add_schedule(text_id, 1, "18:00")
        event_date = "2026-10-18"
        self.registry.mark_user_reminder_delivered(first["id"], event_date)
        self.registry.mark_user_reminder_delivered(second["id"], event_date)

        self.registry.update_schedule(first["id"], days=2)

        self.assertFalse(
            self.registry.is_user_reminder_delivered(first["id"], event_date)
        )
        self.assertTrue(
            self.registry.is_user_reminder_delivered(second["id"], event_date)
        )
        self.registry.mark_user_reminder_delivered(first["id"], event_date)
        self.registry.update_schedule(first["id"], enabled=False)
        self.assertTrue(
            self.registry.is_user_reminder_delivered(first["id"], event_date)
        )

    def test_multiple_reminders_keep_ids_and_update_independently(self):
        current = self.registry.get_service("sound")["reminders"][0]
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=3,
            reminder_time="18:00",
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": current["id"],
                    "reminder_days": 3,
                    "reminder_time": "18:00",
                },
                {"reminder_days": 1, "reminder_time": "09:15"},
            ],
        )

        reminders = self.registry.get_service("sound")["reminders"]
        self.assertEqual(len(reminders), 2)
        self.assertEqual(
            next(item["id"] for item in reminders if item["reminder_days"] == 3),
            current["id"],
        )
        self.assertEqual(
            [(item["reminder_days"], item["reminder_time"]) for item in reminders],
            [(1, "09:15"), (3, "18:00")],
        )

    def test_intentionally_empty_reminder_list_stays_empty_after_restart(self):
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=1,
            reminder_time="18:00",
            group_template=None,
            personal_templates={},
            reminders=[],
        )

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.get_service("sound")["reminders"], []
        )

    def test_duplicate_reminders_keep_independent_delivery_records(self):
        current = self.registry.get_service("sound")["reminders"][0]
        duplicate_rules = [
            {
                "id": current["id"],
                "reminder_days": 2,
                "reminder_time": "17:30",
            },
            {"reminder_days": 2, "reminder_time": "17:30"},
        ]
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.set_notification_settings(
                1001,
                "sound",
                reminder_days=2,
                reminder_time="17:30",
                group_template=None,
                personal_templates={},
                reminders=duplicate_rules,
            )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=2,
            reminder_time="17:30",
            group_template=None,
            personal_templates={},
            reminders=duplicate_rules,
            allow_duplicate_reminders=True,
        )
        reminders = self.registry.get_service("sound")["reminders"]
        event_date = "2026-10-18"
        self.registry.mark_group_reminder_delivered(
            reminders[0]["id"], event_date, "group:-10001"
        )
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                reminders[1]["id"], event_date, "group:-10001"
            )
        )
        self.registry.mark_group_reminder_delivered(
            reminders[1]["id"], event_date, "group:-10001"
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                reminders[1]["id"], event_date, "group:-10001"
            )
        )

    def test_editing_reminder_keeps_delivery_history_for_same_identity(self):
        reminder = self.registry.get_service("sound")["reminders"][0]
        event_date = "2026-10-18"
        recipient = "group:-10001"
        self.registry.mark_group_reminder_delivered(
            reminder["id"], event_date, recipient
        )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=2,
            reminder_time="09:00",
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": reminder["id"],
                    "reminder_days": 2,
                    "reminder_time": "09:00",
                }
            ],
        )

        edited = self.registry.get_service("sound")["reminders"][0]
        self.assertEqual(edited["id"], reminder["id"])
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                edited["id"], event_date, recipient
            )
        )

    def test_replacing_rule_with_new_identity_does_not_carry_delivery_history(self):
        reminder = self.registry.get_service("sound")["reminders"][0]
        event_date = "2026-10-18"
        recipient = "group:-10001"
        self.registry.mark_group_reminder_delivered(
            reminder["id"], event_date, recipient
        )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=reminder["reminder_days"],
            reminder_time=reminder["reminder_time"],
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": None,
                    "reminder_days": reminder["reminder_days"],
                    "reminder_time": reminder["reminder_time"],
                }
            ],
        )

        replacement = self.registry.get_service("sound")["reminders"][0]
        self.assertNotEqual(replacement["id"], reminder["id"])
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                replacement["id"], event_date, recipient
            )
        )

    def test_swapping_two_reminder_schedules_preserves_original_histories(self):
        first = self.registry.get_service("sound")["reminders"][0]
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=first["reminder_days"],
            reminder_time=first["reminder_time"],
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": first["id"],
                    "reminder_days": 1,
                    "reminder_time": "08:00",
                },
                {"reminder_days": 3, "reminder_time": "18:00"},
            ],
        )
        first, second = self.registry.get_service("sound")["reminders"]
        event_date = "2026-10-18"
        self.registry.mark_group_reminder_delivered(
            first["id"], event_date, "group:-10001"
        )
        self.registry.mark_group_reminder_delivered(
            second["id"], event_date, "personal:task:2002"
        )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=first["reminder_days"],
            reminder_time=first["reminder_time"],
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": first["id"],
                    "reminder_days": second["reminder_days"],
                    "reminder_time": second["reminder_time"],
                },
                {
                    "id": second["id"],
                    "reminder_days": first["reminder_days"],
                    "reminder_time": first["reminder_time"],
                },
            ],
        )

        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                first["id"], event_date, "personal:task:2002"
            )
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                first["id"], event_date, "group:-10001"
            )
        )
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                second["id"], event_date, "group:-10001"
            )
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                second["id"], event_date, "personal:task:2002"
            )
        )

    def test_legacy_reminders_and_deliveries_migrate_once(self):
        collision_id = self.registry.list_group_reminders("assembly")[0]["id"]
        with self.registry._connection() as connection:
            connection.execute("DELETE FROM group_reminders")
            connection.execute("DELETE FROM group_reminder_migrations")
            connection.execute("DELETE FROM service_reminders")
            connection.execute(
                "INSERT INTO group_reminders "
                "(id, service_id, body, reminder_days, reminder_time) "
                "VALUES (?, 'assembly', NULL, 0, '10:00')",
                (collision_id,),
            )
            connection.execute(
                "INSERT INTO service_reminders "
                "(id, service_id, reminder_days, reminder_time) "
                "VALUES (?, 'sound', 3, '18:00'), "
                "(102, 'sound', 3, '18:00'), "
                "(103, 'sound', 1, '09:15')",
                (collision_id,),
            )
            connection.execute(
                "INSERT INTO service_reminder_deliveries "
                "(service_id, reminder_id, event_date, recipient_key, "
                "delivered_at) VALUES "
                "('sound', ?, '2026-10-18', 'group:-10001', "
                "'2026-10-15T10:00:00+00:00'), "
                "('sound', 102, '2026-10-18', 'group:-10001', "
                "'2026-10-15T10:01:00+00:00')",
                (collision_id,),
            )

        self.registry.initialize(1001, -10001)
        reminders = self.registry.list_group_reminders("sound")
        self.assertEqual(
            [(item["reminder_days"], item["reminder_time"]) for item in reminders],
            [(1, "09:15"), (3, "18:00")],
        )
        migrated = next(item for item in reminders if item["reminder_days"] == 3)
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                migrated["id"], "2026-10-18", "group:-10001"
            )
        )
        self.assertNotEqual(migrated["id"], collision_id)
        self.assertEqual(
            self.registry.get_group_reminder(collision_id)["service_id"],
            "assembly",
        )
        self.assertTrue(
            self.registry.is_service_reminder_delivered(
                "sound", collision_id, "2026-10-18", "group:-10001"
            )
        )
        self.registry.mark_service_reminder_delivered(
            "sound", collision_id, "2026-10-18", "group:legacy-api"
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                migrated["id"], "2026-10-18", "group:legacy-api"
            )
        )
        with self.registry._connection() as connection:
            marker_count = connection.execute(
                "SELECT COUNT(*) FROM group_reminder_migrations "
                "WHERE name = 'service-reminders-to-group-reminders'"
            ).fetchone()[0]
        self.assertEqual(marker_count, 1)

        self.registry.initialize(1001, -10001)
        self.assertEqual(self.registry.list_group_reminders("sound"), reminders)

    def test_recreated_reminder_does_not_inherit_legacy_deliveries(self):
        event_date = "2026-10-19"
        recipient_key = "group:-10001"
        with self.registry._connection() as connection:
            connection.execute("DELETE FROM group_reminders")
            connection.execute(
                "DELETE FROM group_reminder_migrations "
                "WHERE name = 'service-reminders-to-group-reminders'"
            )
            connection.execute("DELETE FROM service_reminders")
            connection.execute("DELETE FROM service_reminder_deliveries")
            connection.execute(
                "INSERT INTO service_reminders "
                "(service_id, reminder_days, reminder_time) "
                "VALUES ('assembly', 1, '09:00')"
            )
            legacy_id = connection.execute(
                "SELECT id FROM service_reminders WHERE service_id = 'assembly'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO service_reminder_deliveries "
                "(service_id, reminder_id, event_date, recipient_key, "
                "delivered_at) "
                "VALUES ('assembly', ?, ?, ?, '2026-10-18T09:00:00')",
                (legacy_id, event_date, recipient_key),
            )

        self.registry.initialize(1001, -10001)
        migrated = self.registry.list_group_reminders("assembly")
        self.assertEqual(len(migrated), 1)
        migrated_id = migrated[0]["id"]
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                migrated_id, event_date, recipient_key
            )
        )
        with self.registry._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT reminder_id FROM group_reminder_legacy_ids "
                    "WHERE legacy_id = ?",
                    (legacy_id,),
                ).fetchone()["reminder_id"],
                migrated_id,
            )

        self.registry.delete_group_reminder(1001, migrated_id)
        replacement_id = self.registry.add_group_reminder(
            1001, "assembly", 1, "09:00"
        )
        self.assertNotEqual(replacement_id, migrated_id)

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.list_group_reminders("assembly")[0]["id"],
            replacement_id,
        )
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                replacement_id, event_date, recipient_key
            )
        )
        with self.registry._connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM group_reminder_legacy_ids "
                    "WHERE legacy_id = ?",
                    (legacy_id,),
                ).fetchone()
            )

    def test_compatibility_setter_updates_primary_and_preserves_other_reminders(self):
        original = self.registry.list_group_reminders("sound")
        second_id = self.registry.add_group_reminder(
            1001, "sound", 3, "09:15", "Custom {service_name}"
        )
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=2,
            reminder_time="17:30",
            group_template=None,
            personal_templates={},
        )

        current = self.registry.list_group_reminders("sound")
        primary = next(item for item in current if item["id"] == original[0]["id"])
        second = next(item for item in current if item["id"] == second_id)
        self.assertEqual(
            (primary["reminder_days"], primary["reminder_time"]),
            (2, "17:30"),
        )
        self.assertEqual(
            (second["reminder_days"], second["reminder_time"], second["body"]),
            (3, "09:15", "Custom {service_name}"),
        )

    def test_compatibility_settings_update_rolls_back_on_stale_reminder_id(self):
        before = self.registry.get_service("sound")
        reminders_before = self.registry.list_group_reminders("sound")
        with self.assertRaisesRegex(ValueError, "устарело"):
            self.registry.set_notification_settings(
                1001,
                "sound",
                reminder_days=4,
                reminder_time="16:30",
                group_template="New {service_name}",
                personal_templates={"Микшер": "New {name}"},
                reminders=[
                    {
                        "id": 999999,
                        "reminder_days": 4,
                        "reminder_time": "16:30",
                    }
                ],
            )

        after = self.registry.get_service("sound")
        self.assertEqual(after["reminder_days"], before["reminder_days"])
        self.assertEqual(after["reminder_time"], before["reminder_time"])
        self.assertEqual(after["group_template"], before["group_template"])
        self.assertEqual(
            after["personal_templates"], before["personal_templates"]
        )
        self.assertEqual(
            self.registry.list_group_reminders("sound"), reminders_before
        )

    def test_group_reminder_text_and_schedule_are_independent_records(self):
        default_template = "Общее {service_name} · {sunday_date}"
        self.registry.set_service_default_template(
            1001, "sound", default_template
        )
        reminders = self.registry.list_group_reminders("sound")
        first = reminders[0]
        second_id = self.registry.add_group_reminder(
            1001, "sound", 3, "09:15", "Свой текст {task_assignments}"
        )
        second = self.registry.get_group_reminder(second_id)
        service = self.registry.get_service("sound")

        self.assertEqual(
            self.registry.resolve_group_text(first, service),
            default_template,
        )
        self.assertEqual(
            self.registry.resolve_group_text(second, service),
            "Свой текст {task_assignments}",
        )

        self.registry.update_group_reminder(
            1001, first["id"], days=2, time="17:30", enabled=False
        )
        updated = self.registry.get_group_reminder(first["id"])
        self.assertEqual(updated["reminder_days"], 2)
        self.assertEqual(updated["reminder_time"], "17:30")
        self.assertFalse(updated["enabled"])
        self.assertEqual(
            self.registry.resolve_group_text(updated, service),
            default_template,
        )

        self.registry.update_group_reminder(
            1001, first["id"], body="Напоминание {service_name}"
        )
        updated = self.registry.get_group_reminder(first["id"])
        self.assertEqual(
            self.registry.resolve_group_text(updated, service),
            "Напоминание {service_name}",
        )
        self.registry.update_group_reminder(
            1001, first["id"], body=None
        )
        self.registry.set_service_default_template(1001, "sound", None)
        service = self.registry.get_service("sound")
        self.assertEqual(
            self.registry.resolve_group_text(
                self.registry.get_group_reminder(first["id"]), service
            ),
            "Напоминание о служении «{service_name}» в воскресенье "
            "{sunday_date}:\n{task_assignments}",
        )

    def test_legacy_unique_reminder_constraint_is_migrated(self):
        legacy_path = Path(self.temp_dir.name) / "legacy" / "services.sqlite3"
        legacy_path.parent.mkdir(mode=0o700)
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE service_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL,
                    reminder_days INTEGER NOT NULL,
                    reminder_time TEXT NOT NULL,
                    UNIQUE(service_id, reminder_days, reminder_time)
                )
                """
            )
        legacy_path.chmod(0o600)
        legacy_registry = ServiceRegistry(legacy_path)
        legacy_registry.initialize(1001, -10001)
        existing = legacy_registry.get_service("sound")["reminders"][0]
        legacy_registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=1,
            reminder_time="18:00",
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": existing["id"],
                    "reminder_days": 1,
                    "reminder_time": "18:00",
                },
                {"reminder_days": 1, "reminder_time": "18:00"},
            ],
            allow_duplicate_reminders=True,
        )
        self.assertEqual(
            len(legacy_registry.get_service("sound")["reminders"]), 2
        )

    def test_rebinding_contact_replaces_old_name_and_user_rows(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "alex_one"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 5005, "alex_two"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Павел", 5005, "pavel"
        )

        contacts = self.registry.list_responsible_contacts(1001, "sound")
        self.assertEqual(
            [
                (item["name"], item["telegram_id"], item["username"])
                for item in contacts
                if item["task"] == "Микшер"
            ],
            [("Павел", 5005, "pavel")],
        )

    def test_initialization_cleans_existing_duplicate_contact_names(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "alex_old"
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "DROP INDEX idx_responsible_contacts_name"
            )
            connection.execute(
                """
                INSERT INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES ('sound', 'Микшер', 'Алексей', 5005, 'alex_new',
                          1001, '2026-10-13T00:00:00+00:00')
                """
            )
            connection.commit()

        self.registry.initialize(1001, -10001)

        contacts = [
            item
            for item in self.registry.list_responsible_contacts(1001, "sound")
            if item["task"] == "Микшер"
        ]
        self.assertEqual(
            [(item["name"], item["telegram_id"], item["username"]) for item in contacts],
            [("Алексей", 5005, "alex_new")],
        )

    def test_migrates_contact_primary_key_from_name_to_telegram_id(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "alex"
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                ALTER TABLE responsible_contacts
                RENAME TO contacts_new;
                CREATE TABLE responsible_contacts (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    name TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    updated_by INTEGER NOT NULL CHECK (updated_by > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, task, name)
                );
                INSERT INTO responsible_contacts
                SELECT * FROM contacts_new;
                INSERT INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES (
                    'sound', 'Микшер', 'Алексей-дубликат', 4004,
                    'alex_duplicate', 1001, '2026-10-13T00:00:00+00:00'
                );
                DROP TABLE contacts_new;
                CREATE TABLE responsible_contacts_v2 (stale TEXT);
                """
            )

        self.registry.initialize(1001, -10001)

        with closing(sqlite3.connect(self.path)) as connection:
            migrated = connection.execute(
                "SELECT name, telegram_id, username "
                "FROM responsible_contacts WHERE task = 'Микшер'"
            ).fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            leftover = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'responsible_contacts_v2'"
            ).fetchone()
        self.assertEqual(integrity, "ok")
        self.assertIsNone(leftover)
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0][1], 4004)

        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей-дубликат", 5005, "alex2"
        )
        contacts = [
            item for item in self.registry.list_responsible_contacts(1001, "sound")
            if item["task"] == "Микшер"
        ]
        self.assertEqual(
            [(item["name"], item["telegram_id"]) for item in contacts],
            [("Алексей-дубликат", 5005)],
        )

    def test_service_admin_can_edit_only_own_notification_settings(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.add_service_admin(1001, "slides", 4004)
        self.registry.set_notification_settings(
            3003,
            "sound",
            reminder_days=2,
            reminder_time="16:45",
            group_template="Звук: {service_name}",
            personal_templates={"Микшер": "{name}, проверьте звук"},
        )

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertEqual(services["sound"]["reminder_days"], 2)
        self.assertEqual(services["sound"]["reminder_time"], "16:45")
        self.assertEqual(
            services["sound"]["group_template"], "Звук: {service_name}"
        )
        self.assertEqual(
            services["sound"]["personal_templates"],
            {"Микшер": "{name}, проверьте звук"},
        )
        with self.assertRaises(PermissionError):
            self.registry.set_notification_settings(
                3003,
                "slides",
                reminder_days=1,
                reminder_time="17:00",
                group_template="Слайды",
                personal_templates={},
            )
        with self.assertRaises(PermissionError):
            self.registry.set_group_chat(3003, "sound", -10023, enabled=True)

    def test_invalid_ids_and_unknown_services_are_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.add_service_admin(1001, "sound", 0)
        with self.assertRaises(ValueError):
            self.registry.set_group_chat(1001, "unknown", -10003, enabled=True)
        with self.assertRaises(ValueError):
            self.registry.set_group_chat(1001, "sound", 0, enabled=True)
        with self.assertRaises(ValueError):
            self.registry.set_responsible_contact(
                1001, "sound", "Микшер", "Алексей", True
            )
        with self.assertRaises(ValueError):
            self.registry.set_notification_settings(
                1001,
                "sound",
                reminder_days=0,
                reminder_time="25:00",
                group_template="Звук",
                personal_templates={},
            )

    def test_symlink_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.sqlite3"
            target.write_bytes(b"must remain unchanged")
            link = Path(self.temp_dir.name) / "private" / "symlink.sqlite3"
            link.symlink_to(target)
            registry = ServiceRegistry(link)

            with self.assertRaises(PermissionError):
                registry.initialize(1001, -10001)

            self.assertEqual(target.read_bytes(), b"must remain unchanged")


if __name__ == "__main__":
    unittest.main()
