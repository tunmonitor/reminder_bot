import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramAPIError

from bot.commands import (
    ROLE_SERVICE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
    commands_for_role,
    sync_admin_commands,
    user_role,
)
from bot.service_registry import ServiceRegistry


def command_names(commands):
    return [item.command for item in commands]


class CommandsForRoleTest(unittest.TestCase):
    def test_user_sees_only_participant_commands(self):
        self.assertEqual(
            command_names(commands_for_role(ROLE_USER)),
            ["start", "my_notifications", "cancel"],
        )

    def test_service_admin_has_no_super_admin_commands(self):
        names = command_names(commands_for_role(ROLE_SERVICE_ADMIN))
        self.assertEqual(
            names,
            ["start", "services", "service_notify", "my_notifications", "cancel"],
        )

    def test_main_super_admin_gets_full_menu_with_delivery_check(self):
        names = command_names(
            commands_for_role(ROLE_SUPER_ADMIN, is_main_admin=True)
        )
        self.assertIn("service_admin_add", names)
        self.assertIn("test_notify", names)
        self.assertEqual(names[-2:], ["my_notifications", "cancel"])
        self.assertNotIn(
            "test_notify", command_names(commands_for_role(ROLE_SUPER_ADMIN))
        )


class SyncCommandsTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "registry.sqlite3")
        self.registry.initialize(67890, -100)
        self.registry.add_service_admin(67890, "sound", 24680)

    def test_list_admin_ids_marks_super_admins(self):
        self.assertEqual(self.registry.list_admin_ids(), {24680: False, 67890: True})

    def test_user_role_resolution(self):
        self.assertEqual(user_role(self.registry, 67890), ROLE_SUPER_ADMIN)
        self.assertEqual(user_role(self.registry, 24680), ROLE_SERVICE_ADMIN)
        self.assertEqual(user_role(self.registry, 5555), ROLE_USER)
        self.assertEqual(user_role(None, 67890), ROLE_USER)

    def test_startup_sets_default_and_admin_menus(self):
        bot = MagicMock(set_my_commands=AsyncMock(), delete_my_commands=AsyncMock())

        asyncio.run(sync_admin_commands(bot, self.registry, 67890, MagicMock()))

        calls = bot.set_my_commands.await_args_list
        self.assertEqual(calls[0].kwargs["scope"].type, "default")
        self.assertEqual(command_names(calls[0].args[0]), command_names(commands_for_role(ROLE_USER)))
        menus = {call.kwargs["scope"].chat_id: command_names(call.args[0]) for call in calls[1:]}
        self.assertEqual(set(menus), {24680, 67890})
        self.assertIn("test_notify", menus[67890])
        self.assertNotIn("service_contact", menus[24680])

    def test_telegram_errors_do_not_stop_other_menus(self):
        bot = MagicMock(
            set_my_commands=AsyncMock(
                side_effect=[None, TelegramAPIError(method=MagicMock(), message="x"), None]
            ),
            delete_my_commands=AsyncMock(),
        )
        debug = MagicMock()

        asyncio.run(sync_admin_commands(bot, self.registry, 67890, debug))

        self.assertEqual(bot.set_my_commands.await_count, 3)
        debug.exception.assert_called_once()


if __name__ == "__main__":
    unittest.main()
