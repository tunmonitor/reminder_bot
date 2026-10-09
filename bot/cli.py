"""Разбор аргументов командной строки."""
from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class CliArgs:
    shm_dir: str
    log_dir: str


def parse_args(argv=None) -> CliArgs:
    parser = argparse.ArgumentParser(
        prog="reminder_bot.py",
        description="Telegram-бот напоминаний по расписанию",
    )
    parser.add_argument(
        "--shm-dir",
        required=True,
        help="Директория оперативных данных в ОЗУ (PID, xlsx, state_cache).",
    )
    parser.add_argument(
        "--log-dir",
        required=True,
        help="Директория логов (reminder_bot_interaction.log, reminder_bot_debug.log).",
    )
    ns = parser.parse_args(argv)
    return CliArgs(shm_dir=ns.shm_dir, log_dir=ns.log_dir)
