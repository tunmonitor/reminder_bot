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

    def test_duplicate_reminders_require_confirmation_and_deliver_once(self):
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
        self.registry.mark_service_reminder_delivered(
            "sound", reminders[0]["id"], event_date, "group:-10001"
        )
        self.assertTrue(
            self.registry.is_service_reminder_delivered(
                "sound", reminders[1]["id"], event_date, "group:-10001"
            )
        )

    def test_editing_reminder_clears_history_for_old_schedule(self):
        reminder = self.registry.get_service("sound")["reminders"][0]
        event_date = "2026-10-18"
        recipient = "group:-10001"
        self.registry.mark_service_reminder_delivered(
            "sound", reminder["id"], event_date, recipient
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
        self.assertFalse(
            self.registry.is_service_reminder_delivered(
                "sound", edited["id"], event_date, recipient
            )
        )

    def test_replacing_rule_with_same_schedule_carries_delivery_history(self):
        reminder = self.registry.get_service("sound")["reminders"][0]
        event_date = "2026-10-18"
        recipient = "group:-10001"
        self.registry.mark_service_reminder_delivered(
            "sound", reminder["id"], event_date, recipient
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
        self.assertTrue(
            self.registry.is_service_reminder_delivered(
                "sound", replacement["id"], event_date, recipient
            )
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
