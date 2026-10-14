"""Manual snapshot and restore commands for the managed bot instance."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .backup import BackupManager, assert_bot_stopped, environment_name
from .config import PROJECT_DIR, load_timezone


def _parser():
    parser = argparse.ArgumentParser(prog="bot.backup_cli")
    parser.add_argument("action", choices=("create", "restore"))
    parser.add_argument("--shm-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logger = logging.getLogger("reminder_bot.backup")
    manager = BackupManager(
        environment_name(PROJECT_DIR),
        args.shm_dir,
        load_timezone(),
        logger,
    )
    if args.action == "create":
        snapshot = asyncio.run(manager.create_snapshot("ручная команда"))
        print(snapshot)
        return 0
    if args.snapshot is None:
        raise ValueError("Для восстановления укажите --snapshot")
    assert_bot_stopped(PROJECT_DIR, args.shm_dir)
    manager.restore_snapshot(args.snapshot)
    print(f"Данные восстановлены из {args.snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
