import os
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import debug_schedule


class DebugScheduleTest(unittest.IsolatedAsyncioTestCase):
    def test_default_output_directory_is_on_ram_disk(self):
        self.assertEqual(
            debug_schedule.DEFAULT_OUTPUT_DIR,
            Path("/dev/shm/reminder_bot/debug"),
        )

    async def test_download_is_retained_when_parsing_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "debug"

            async def fake_download(_url, path, _logger, **_kwargs):
                path.write_bytes(b"debug workbook")
                return path

            with (
                patch.object(
                    debug_schedule,
                    "load_credentials",
                    return_value=SimpleNamespace(
                        yandex_public_url="https://example.invalid/public",
                        timezone="UTC",
                    ),
                ),
                patch.object(debug_schedule, "download_public", side_effect=fake_download),
                patch.object(
                    debug_schedule,
                    "parse_schedule",
                    side_effect=RuntimeError("test parse failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "test parse failure"):
                    await debug_schedule.run_check(date(2026, 10, 11), output_dir)

            downloaded_files = list(output_dir.glob("schedule-20261011-*.xlsx"))
            self.assertEqual(len(downloaded_files), 1)
            self.assertEqual(downloaded_files[0].read_bytes(), b"debug workbook")
            self.assertEqual(
                stat.S_IMODE(downloaded_files[0].stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(output_dir.stat().st_mode) & 0o077,
                0,
            )

    async def test_rejects_output_directory_accessible_to_others(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "public"
            output_dir.mkdir(mode=0o755)
            os.chmod(output_dir, 0o755)

            with self.assertRaisesRegex(PermissionError, "доступен группе"):
                debug_schedule._secure_output_dir(output_dir)

    async def test_rejects_symlinked_output_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            target.mkdir(mode=0o700)
            output_dir = Path(temp_dir) / "linked"
            output_dir.symlink_to(target, target_is_directory=True)

            with self.assertRaises(OSError):
                debug_schedule._secure_output_dir(output_dir)

            self.assertEqual(list(target.iterdir()), [])

    async def test_rejects_symlinked_output_directory_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir(mode=0o700)
            linked_parent = root / "linked"
            linked_parent.symlink_to(target, target_is_directory=True)

            with self.assertRaises(OSError):
                debug_schedule._secure_output_dir(linked_parent / "debug")

            self.assertFalse((target / "debug").exists())


if __name__ == "__main__":
    unittest.main()
