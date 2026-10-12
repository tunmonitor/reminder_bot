import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import config


class ServiceRegistryPathTest(unittest.TestCase):
    def test_default_registry_path_is_in_private_runtime_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            config, "PROJECT_DIR", Path(tmp_dir)
        ), patch.dict(os.environ, {}, clear=True):
            path = config.service_registry_path()

        self.assertEqual(path, Path(tmp_dir) / ".runtime" / "reminder_bot.sqlite3")

    def test_registry_path_can_be_configured_in_environment(self):
        with patch.dict(
            os.environ, {"REMINDER_BOT_DB_PATH": "/srv/reminder/data.sqlite3"}
        ):
            self.assertEqual(
                config.service_registry_path(),
                Path("/srv/reminder/data.sqlite3"),
            )

    def test_credentials_file_registry_path_takes_environment_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir)
            (project_dir / "credentials.env").write_text(
                "REMINDER_BOT_DB_PATH=./persistent/service.sqlite3\n",
                encoding="utf-8",
            )
            with patch.object(config, "PROJECT_DIR", project_dir), patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertEqual(
                    config.service_registry_path(),
                    Path("./persistent/service.sqlite3"),
                )


if __name__ == "__main__":
    unittest.main()
