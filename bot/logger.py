"""Два независимых логгера: interaction (INFO) и debug (DEBUG).

interaction.log — только события пользователей (INFO).
debug.log       — технический трек (DEBUG) + дублирование interaction.
На stderr debug-логгер тоже пишет, чтобы sys_nohup.log содержал трейсы.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

INTERACTION_LOG = "reminder_bot_interaction.log"
DEBUG_LOG = "reminder_bot_debug.log"

_FMT = "%(asctime)s | %(levelname)-7s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_loggers(log_dir: Path) -> tuple[logging.Logger, logging.Logger]:
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # --- interaction ---
    interaction = logging.getLogger("reminder_bot.interaction")
    interaction.setLevel(logging.INFO)
    interaction.propagate = False
    interaction.handlers.clear()
    ih = logging.FileHandler(log_dir / INTERACTION_LOG, encoding="utf-8")
    ih.setFormatter(formatter)
    interaction.addHandler(ih)

    # --- debug ---
    debug = logging.getLogger("reminder_bot.debug")
    debug.setLevel(logging.DEBUG)
    debug.propagate = False
    debug.handlers.clear()
    dh = logging.FileHandler(log_dir / DEBUG_LOG, encoding="utf-8")
    dh.setFormatter(formatter)
    debug.addHandler(dh)
    debug.addHandler(ih)                     # INFO-события дублируются в debug
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(formatter)
    debug.addHandler(sh)

    return interaction, debug
