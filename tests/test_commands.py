import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
)

from bot.commands import (
    ROLE_SERVICE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
    commands_for_role,
    sync_admin_commands,
    sync_user_commands,
    user_role,
)
from bot.service_registry import ServiceRegistry


def command_names(commands):
    return [item.command for item in commands]


class CommandsForRoleTest(unittest.TestCase):
    def test_user_sees_only_participant_commands(self):
        self.assertEqual(
            command_names(commands_for_role(ROLE_USER)),
            ["start", "my_schedule", "my_notifications", "cancel"],
        )

    def test_main_admin_resolved_as_user_keeps_delivery_check(self):
        self.assertEqual(
            command_names(commands_for_role(ROLE_USER, is_main_admin=True)),
            ["start", "test_notify", "my_schedule", "my_notifications", "cancel"],
        )

    def test_service_admin_has_no_super_admin_commands(self):
        names = command_names(commands_for_role(ROLE_SERVICE_ADMIN))
        self.assertEqual(
            names,
            ["start", "services", "service_notify", "change", "my_schedule", "my_notifications", "cancel"],
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

    def test_main_admin_missing_from_registry_gets_delivery_check(self):
        bot = MagicMock(set_my_commands=AsyncMock(), delete_my_commands=AsyncMock())

        asyncio.run(sync_admin_commands(bot, self.registry, 11111, MagicMock()))

        menus = {
            call.kwargs["scope"].chat_id: command_names(call.args[0])
            for call in bot.set_my_commands.await_args_list[1:]
        }
        self.assertIn("test_notify", menus[11111])
        self.assertNotIn("services", menus[11111])

    def test_chat_not_found_is_logged_without_traceback(self):
        bot = MagicMock(
            set_my_commands=AsyncMock(
                side_effect=[
                    None,
                    TelegramBadRequest(
                        method=MagicMock(), message="Bad Request: chat not found"
                    ),
                    None,
                ]
            ),
            delete_my_commands=AsyncMock(),
        )
        debug = MagicMock()

        asyncio.run(sync_admin_commands(bot, self.registry, 67890, debug))

        debug.exception.assert_not_called()
        debug.info.assert_called_once()

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

    def test_reports_only_temporary_failures_for_retry(self):
        bot = MagicMock(
            set_my_commands=AsyncMock(
                side_effect=[None, TelegramAPIError(method=MagicMock(), message="x"), None]
            ),
            delete_my_commands=AsyncMock(),
        )
        self.assertTrue(
            asyncio.run(sync_admin_commands(bot, self.registry, 67890, MagicMock()))
        )

        bot.set_my_commands = AsyncMock(
            side_effect=[
                TelegramNetworkError(method=MagicMock(), message="DNS"),
                None,
                None,
            ]
        )
        self.assertFalse(
            asyncio.run(sync_admin_commands(bot, self.registry, 67890, MagicMock()))
        )
        self.assertEqual(bot.set_my_commands.await_count, 3)

    def test_menu_updates_for_one_user_run_in_order(self):
        """Роль читается под блокировкой: второе обновление ждёт первое."""
        events = []
        release = asyncio.Event()
        registry = MagicMock()
        registry.is_super_admin = MagicMock(
            side_effect=lambda _tid: events.append("role") or True
        )

        async def set_commands(*_args, **_kwargs):
            events.append("send")
            if len(events) == 2:
                await release.wait()

        bot = MagicMock(set_my_commands=AsyncMock(side_effect=set_commands))

        async def scenario():
            first = asyncio.create_task(
                sync_user_commands(bot, registry, 555, 1, MagicMock())
            )
            await asyncio.sleep(0)
            second = asyncio.create_task(
                sync_user_commands(bot, registry, 555, 1, MagicMock())
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        asyncio.run(scenario())

        self.assertEqual(events, ["role", "send", "role", "send"])


if __name__ == "__main__":
    unittest.main()
