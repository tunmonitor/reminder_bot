"""Telegram command menus filtered by the user's role."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
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
_CHANGE = BotCommand(
    command="change", description="Замена: задача @username [дд.мм]"
)
_ADMIN_COMMANDS = [
    BotCommand(command="service_contact", description="Привязать ответственных"),
    BotCommand(command="service_sync", description="Сверить график и привязки"),
    BotCommand(command="service_chat", description="Групповой чат служения"),
]
_SUPER_ADMIN_COMMANDS = [
    BotCommand(command="service_admin_add", description="Назначить администратора"),
    BotCommand(command="service_admin_remove", description="Снять администратора"),
]
_TEST_NOTIFY = BotCommand(command="test_notify", description="Проверить доставку")

# Временные сбои сети или Telegram: обновление меню стоит повторить позже.
RETRYABLE_ERRORS = (TelegramNetworkError, TelegramServerError, TelegramRetryAfter)

# Роль читается и меню отправляется под одной блокировкой, чтобы обновление
# при запуске не перезаписало меню, уже обновлённое после смены роли.
_user_menu_locks: dict[int, asyncio.Lock] = {}


def commands_for_role(role: str, *, is_main_admin: bool = False) -> list[BotCommand]:
    """Return menu commands available to a role, most frequent first."""
    commands = [_START]
    if role != ROLE_USER:
        commands.extend([_SERVICES, _SERVICE_NOTIFY, _CHANGE, *_ADMIN_COMMANDS])
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


async def set_default_commands(bot: Bot, debug: logging.Logger) -> bool:
    """Show only participant commands to users without a personal menu.

    Returns False when the failure is temporary and worth retrying.
    """
    try:
        await bot.set_my_commands(
            commands_for_role(ROLE_USER), scope=BotCommandScopeDefault()
        )
    except RETRYABLE_ERRORS:
        debug.exception("Не удалось обновить меню команд Telegram")
        return False
    except TelegramAPIError:
        debug.exception("Не удалось обновить меню команд Telegram")
    return True


async def sync_user_commands(
    bot: Bot,
    registry: Any,
    telegram_id: int,
    main_admin_id: int,
    debug: logging.Logger,
) -> bool:
    """Apply the role-specific command menu to one private chat.

    Returns False when the failure is temporary and worth retrying.
    """
    lock = _user_menu_locks.setdefault(telegram_id, asyncio.Lock())
    async with lock:
        return await _sync_user_commands_locked(
            bot, registry, telegram_id, main_admin_id, debug
        )


async def _sync_user_commands_locked(
    bot: Bot,
    registry: Any,
    telegram_id: int,
    main_admin_id: int,
    debug: logging.Logger,
) -> bool:
    try:
        role = user_role(registry, telegram_id)
    except sqlite3.Error:
        debug.exception("Не удалось определить роль для меню команд")
        return True
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
    except TelegramBadRequest as exc:
        if "chat not found" not in str(exc).lower():
            debug.exception("Не удалось обновить меню команд пользователя")
            return True
        # Пользователь ещё не открывал бота: меню появится после его /start.
        debug.info(
            "Меню команд для %s отложено: пользователь ещё не писал боту",
            telegram_id,
        )
    except RETRYABLE_ERRORS:
        debug.exception("Не удалось обновить меню команд пользователя")
        return False
    except TelegramAPIError:
        debug.exception("Не удалось обновить меню команд пользователя")
    return True


async def sync_admin_commands(
    bot: Bot, registry: Any, main_admin_id: int, debug: logging.Logger
) -> bool:
    """Install the default menu and personal menus for all known admins.

    Returns False when some menu hit a temporary failure and should be retried.
    """
    done = await set_default_commands(bot, debug)
    admin_ids = {main_admin_id}
    if registry is not None:
        try:
            admin_ids.update(registry.list_admin_ids())
        except sqlite3.Error:
            debug.exception("Не удалось прочитать администраторов для меню команд")
    for telegram_id in sorted(admin_ids):
        if not await sync_user_commands(
            bot, registry, telegram_id, main_admin_id, debug
        ):
            done = False
    return done
