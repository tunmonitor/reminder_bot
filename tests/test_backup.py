import asyncio
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from bot.backup import BackupManager
from bot.excel_parser import _MONTH_SHEETS


class BackupManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.shm_dir = self.root / "shm"
        self.shm_dir.mkdir(mode=0o700)
        self.database = self.root / "registry.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN")
            connection.execute(
                "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT)"
            )
            connection.execute("INSERT INTO settings VALUES ('mode', 'original')")
            connection.commit()
        self.database.chmod(0o600)
        self.xlsx = self.shm_dir / "reminder_bot_current.xlsx"
        self._write_workbook(self.xlsx)
        self.cache = self.shm_dir / "reminder_state_cache.json"
        self.cache.write_text(
            json.dumps({"sunday_date": "2030-01-06"}),
            encoding="utf-8",
        )
        self.cache.chmod(0o600)
        self.backup_base = self.root / "external" / "reminder_bot" / "backups"
        self.manager = BackupManager(
            "test",
            self.shm_dir,
            ZoneInfo("Europe/Moscow"),
            logging.getLogger("test.backup"),
            backup_base=self.backup_base,
            mount_point=self.root / "external",
            require_mount=False,
            registry_path=self.database,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _write_workbook(path):
        now = datetime.now(ZoneInfo("Europe/Moscow")).date()
        until_sunday = (6 - now.weekday()) % 7
        target = now + timedelta(days=until_sunday or 7)
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = f"{_MONTH_SHEETS[target.month - 1]}{target.year % 100:02d}"
        schedule.append([target])
        schedule.append(["ХКЦ", "Алексей"])
        schedule.append(["Ведущий", "Павел"])
        schedule.append(["Проповедует", "Мария"])
        contacts = workbook.create_sheet("Списки")
        contacts.append(["ХКЦ"])
        contacts.append(["Алексей", "alex", 101])
        contacts.append(["Ведущий"])
        contacts.append(["Павел", "pavel", 102])
        contacts.append(["Проповедует"])
        contacts.append(["Мария", "maria", 103])
        workbook.save(path)
        path.chmod(0o600)

    def test_snapshot_contains_verified_private_restore_set(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["environment"], "test")
        self.assertEqual(
            set(manifest["files"]),
            {
                "database.sqlite3",
                "database.sql",
                "reminder_state_cache.json",
                "schedule.xlsx",
                "schedule.json",
            },
        )
        self.assertEqual((snapshot.stat().st_mode & 0o777), 0o700)
        for filename in manifest["files"]:
            self.assertEqual((snapshot / filename).stat().st_mode & 0o777, 0o600)
        data = json.loads((snapshot / "schedule.json").read_text("utf-8"))
        self.assertEqual(data["assignments"]["Ответственный"], "Алексей")
        self.assertEqual(data["contacts"]["Ответственный"]["Алексей"]["telegram_id"], 101)

    def test_restore_replaces_database_schedule_and_state(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN")
            connection.execute("UPDATE settings SET value='changed' WHERE name='mode'")
            connection.commit()
        self.cache.write_text(json.dumps({"sunday_date": "2030-02-03"}), "utf-8")
        self.cache.chmod(0o600)
        self.xlsx.write_bytes(b"changed workbook")

        self.manager.restore_snapshot(snapshot)

        with closing(sqlite3.connect(self.database)) as connection:
            value = connection.execute(
                "SELECT value FROM settings WHERE name='mode'"
            ).fetchone()[0]
        self.assertEqual(value, "original")
        self.assertEqual(json.loads(self.cache.read_text("utf-8"))["sunday_date"], "2030-01-06")
        self.assertTrue(self.xlsx.read_bytes().startswith(b"PK"))

    def test_rejects_tampered_snapshot_before_restore(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        with (snapshot / "database.sql").open("ab") as stream:
            stream.write(b"-- tampered\n")
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            self.manager.restore_snapshot(snapshot)

    def test_rejects_snapshot_from_another_environment(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        prod_manager = BackupManager(
            "prod",
            self.shm_dir,
            ZoneInfo("Europe/Moscow"),
            logging.getLogger("test.backup"),
            backup_base=self.backup_base,
            mount_point=self.root / "external",
            require_mount=False,
            registry_path=self.database,
        )
        with self.assertRaisesRegex(ValueError, "своего окружения"):
            prod_manager.restore_snapshot(snapshot)

    def test_requires_external_disk_mount_for_production_snapshot(self):
        manager = BackupManager(
            "prod",
            self.shm_dir,
            ZoneInfo("Europe/Moscow"),
            logging.getLogger("test.backup"),
            backup_base=self.backup_base,
            mount_point=self.root / "not-mounted",
            registry_path=self.database,
        )
        with self.assertRaisesRegex(RuntimeError, "не смонтирован"):
            manager._create_snapshot()


class BackupDebounceTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_change_resets_quiet_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackupManager(
                "test",
                Path(tmp) / "shm",
                ZoneInfo("Europe/Moscow"),
                logging.getLogger("test.backup.debounce"),
                backup_base=Path(tmp) / "backups",
                mount_point=Path(tmp),
                require_mount=False,
                debounce_seconds=0.03,
            )
            manager._run_background_backup = AsyncMock()
            manager.mark_dirty()
            await asyncio.sleep(0.015)
            manager.mark_dirty()
            await asyncio.sleep(0.04)
            self.assertEqual(manager._run_background_backup.await_count, 1)


if __name__ == "__main__":
    unittest.main()
