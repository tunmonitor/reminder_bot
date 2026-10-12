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

    def test_service_admin_can_edit_only_own_notification_settings(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.add_service_admin(1001, "slides", 4004)
        self.registry.set_notification_settings(
            3003,
            "sound",
            reminder_days=2,
            reminder_time="16:45",
            group_template="Звук: {name}",
            personal_templates={"Микшер": "{name}, проверьте звук"},
        )

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertEqual(services["sound"]["reminder_days"], 2)
        self.assertEqual(services["sound"]["reminder_time"], "16:45")
        self.assertEqual(services["sound"]["group_template"], "Звук: {name}")
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
