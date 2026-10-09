#!/usr/bin/env python3
"""Точка входа Telegram-бота напоминаний.

Аргументы (передаёт reminder_bot_manage.sh):
    --shm-dir <path>   директория оперативных данных (PID, xlsx, state_cache)
    --log-dir <path>   директория логов (interaction.log, debug.log)
"""
import asyncio
import sys

from bot.app import run
from bot.cli import parse_args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Последний шанс зафиксировать падение вне try/except в bot.app
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
