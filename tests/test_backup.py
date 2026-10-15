import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from bot.backup import (
    BackupManager,
    MAX_SNAPSHOTS_PER_ENVIRONMENT,
    MIN_RETAINED_SNAPSHOTS,
    STALE_SNAPSHOT_STAGE_AGE,
    application_instance_lock,
    assert_bot_stopped,
)
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

    def test_snapshot_parses_and_hashes_the_staged_workbook(self):
        from datetime import date
        from unittest.mock import patch

        observed_paths = []
        parsed_workbook_bytes = []

        def parse_staged(path):
            observed_paths.append(path)
            parsed_workbook_bytes.append(path.read_bytes())
            self.xlsx.write_bytes(b"live workbook changed during snapshot")
            return date(2030, 1, 6), {"Ответственный": "Алексей"}, {}

        with patch.object(self.manager, "_parse_schedule", side_effect=parse_staged):
            snapshot = asyncio.run(self.manager.create_snapshot("race test"))
        workbook = snapshot / "schedule.xlsx"
        schedule = json.loads((snapshot / "schedule.json").read_text("utf-8"))
        self.assertEqual(len(observed_paths), 1)
        self.assertEqual(observed_paths[0].name, "schedule.xlsx")
        self.assertEqual(parsed_workbook_bytes, [workbook.read_bytes()])
        self.assertEqual(
            schedule["source_sha256"],
            hashlib.sha256(workbook.read_bytes()).hexdigest(),
        )
        self.assertEqual(self.xlsx.read_bytes(), b"live workbook changed during snapshot")

    def test_prunes_only_old_valid_snapshots_after_commit(self):
        snapshot = asyncio.run(self.manager.create_snapshot("retention test"))
        backup_dir = snapshot.parent
        first_created_at = datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC"))

        def set_created_at(path, created_at):
            manifest_path = path / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["created_at"] = created_at.isoformat()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest_path.chmod(0o600)

        set_created_at(snapshot, first_created_at)
        valid_snapshots = [snapshot]
        for index in range(1, MAX_SNAPSHOTS_PER_ENVIRONMENT + 3):
            candidate = backup_dir / f"snapshot-{index:02}"
            shutil.copytree(snapshot, candidate)
            set_created_at(candidate, first_created_at + timedelta(days=index))
            valid_snapshots.append(candidate)

        committed_snapshot = valid_snapshots[-1]
        invalid_snapshot = backup_dir / "invalid-snapshot"
        shutil.copytree(snapshot, invalid_snapshot)
        with (invalid_snapshot / "database.sql").open("ab") as stream:
            stream.write(b"-- tampered\n")

        with self.assertLogs("test.backup", level="WARNING"):
            self.manager._prune_old_snapshots(backup_dir, committed_snapshot)

        remaining_valid = [
            path
            for path in valid_snapshots
            if path.exists()
        ]
        self.assertEqual(len(remaining_valid), MAX_SNAPSHOTS_PER_ENVIRONMENT)
        self.assertGreaterEqual(len(remaining_valid), MIN_RETAINED_SNAPSHOTS)
        self.assertTrue(committed_snapshot.exists())
        self.assertTrue(invalid_snapshot.exists())
        self.assertFalse(valid_snapshots[0].exists())
        self.assertFalse(valid_snapshots[1].exists())
        self.assertFalse(valid_snapshots[2].exists())

    def test_prunes_only_abandoned_staging_directories_older_than_cutoff(self):
        backup_dir = self.backup_base / "test"
        backup_dir.mkdir(parents=True, mode=0o700)
        stale_stage = backup_dir / ".snapshot-abandoned"
        stale_stage.mkdir(mode=0o700)
        recent_stage = backup_dir / ".snapshot-active"
        recent_stage.mkdir(mode=0o700)
        stale_time = datetime.now().timestamp() - (
            STALE_SNAPSHOT_STAGE_AGE.total_seconds() + 60
        )
        os.utime(stale_stage, (stale_time, stale_time))

        self.manager._prune_abandoned_stages(backup_dir)

        self.assertFalse(stale_stage.exists())
        self.assertTrue(recent_stage.exists())

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

    def test_restore_cannot_overlap_a_running_instance(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))

        with application_instance_lock(self.shm_dir):
            with self.assertRaisesRegex(RuntimeError, "уже выполняется"):
                self.manager.restore_snapshot(snapshot)

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

    async def test_close_cancels_debounce_and_waits_for_started_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackupManager(
                "test",
                Path(tmp) / "shm",
                ZoneInfo("Europe/Moscow"),
                logging.getLogger("test.backup.close"),
                backup_base=Path(tmp) / "backups",
                mount_point=Path(tmp),
                require_mount=False,
                debounce_seconds=60,
            )
            manager._run_background_backup = AsyncMock()
            manager.mark_dirty()
            handle = manager._debounce_handle
            task = manager.start_background_backup("active")
            await manager.close()
            self.assertTrue(handle.cancelled())
            manager._run_background_backup.assert_awaited_once_with("active")
            self.assertTrue(task.done())


class RestoreProcessGuardTest(unittest.TestCase):
    def test_rejects_malformed_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm = root / "shm"
            shm.mkdir(mode=0o700)
            pid_file = shm / "reminder_bot_bot.pid"
            pid_file.write_text("not-a-pid", "ascii")
            pid_file.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "неверный формат"):
                assert_bot_stopped(root, shm)

    def test_rejects_unsafe_pid_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm = root / "shm"
            shm.mkdir(mode=0o700)
            pid_file = shm / "reminder_bot_bot.pid"
            pid_file.write_text("999999999:1", "ascii")
            pid_file.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "небезопасен"):
                assert_bot_stopped(root, shm)

    def test_rejects_pid_file_for_live_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm = root / "shm"
            shm.mkdir(mode=0o700)
            stat_fields = (
                Path("/proc/self/stat")
                .read_text("ascii")
                .rsplit(")", 1)[1]
                .split()
            )
            pid_file = shm / "reminder_bot_bot.pid"
            pid_file.write_text(
                f"{os.getpid()}:{stat_fields[19]}",
                "ascii",
            )
            pid_file.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "ещё работает"):
                assert_bot_stopped(root, shm)


if __name__ == "__main__":
    unittest.main()
