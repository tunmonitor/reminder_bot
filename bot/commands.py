"""Telegram command menus filtered by the user's role."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

ROLE_USER = "user"
ROLE_SERVICE_ADMIN = "service_admin"
ROLE_SUPER_ADMIN = "super_admin"

_START = BotCommand(command="start", description="Главное меню")
_MY_NOTIFICATIONS = BotCommand(
    command="my_notifications", description="Мои личные уведомления"
)
_MY_SCHEDULE = BotCommand(command="my_schedule", description="Мои ближайшие служения")
_CANCEL = BotCommand(command="cancel", description="Отменить текущее действие")
_SERVICES = BotCommand(command="services", description="Служения и их настройки")
_SERVICE_NOTIFY = BotCommand(
    command="service_notify", description="Напоминания и шаблоны"
)
_SUPER_ADMIN_COMMANDS = [
    BotCommand(command="service_contact", description="Привязать ответственных"),
    BotCommand(command="service_sync", description="Сверить график и привязки"),
    BotCommand(command="service_chat", description="Групповой чат служения"),
    BotCommand(command="service_admin_add", description="Назначить администратора"),
    BotCommand(command="service_admin_remove", description="Снять администратора"),
]
_TEST_NOTIFY = BotCommand(command="test_notify", description="Проверить доставку")


def commands_for_role(role: str, *, is_main_admin: bool = False) -> list[BotCommand]:
    """Return menu commands available to a role, most frequent first."""
    commands = [_START]
    if role != ROLE_USER:
        commands.extend([_SERVICES, _SERVICE_NOTIFY])
    if role == ROLE_SUPER_ADMIN:
        commands.extend(_SUPER_ADMIN_COMMANDS)
    if is_main_admin:
        commands.append(_TEST_NOTIFY)
    return [*commands, _MY_SCHEDULE, _MY_NOTIFICATIONS, _CANCEL]


def user_role(registry: Any, telegram_id: int) -> str:
    """Resolve the highest administrative role of a Telegram user."""
    if registry is None:
        return ROLE_USER
    if registry.is_super_admin(telegram_id):
        return ROLE_SUPER_ADMIN
    if any(
        registry.is_service_admin(telegram_id, service["id"])
        for service in registry.list_services()
    ):
        return ROLE_SERVICE_ADMIN
    return ROLE_USER


async def set_default_commands(bot: Bot, debug: logging.Logger) -> None:
    """Show only participant commands to users without a personal menu."""
    try:
        await bot.set_my_commands(
            commands_for_role(ROLE_USER), scope=BotCommandScopeDefault()
        )
    except TelegramAPIError:
        debug.exception("Не удалось обновить меню команд Telegram")


async def sync_user_commands(
    bot: Bot,
    registry: Any,
    telegram_id: int,
    main_admin_id: int,
    debug: logging.Logger,
) -> None:
    """Apply the role-specific command menu to one private chat."""
    try:
        role = user_role(registry, telegram_id)
    except sqlite3.Error:
        debug.exception("Не удалось определить роль для меню команд")
        return
    is_main_admin = telegram_id == main_admin_id
    scope = BotCommandScopeChat(chat_id=telegram_id)
    try:
        if role == ROLE_USER and not is_main_admin:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(
                commands_for_role(role, is_main_admin=is_main_admin),
                scope=scope,
            )
    except TelegramAPIError:
        debug.exception("Не удалось обновить меню команд пользователя")


async def sync_admin_commands(
    bot: Bot, registry: Any, main_admin_id: int, debug: logging.Logger
) -> None:
    """Install the default menu and personal menus for all known admins."""
    await set_default_commands(bot, debug)
    admin_ids = {main_admin_id}
    if registry is not None:
        try:
            admin_ids.update(registry.list_admin_ids())
        except sqlite3.Error:
            debug.exception("Не удалось прочитать администраторов для меню команд")
    for telegram_id in sorted(admin_ids):
        await sync_user_commands(bot, registry, telegram_id, main_admin_id, debug)
