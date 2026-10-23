"""Два независимых логгера: interaction (INFO) и debug (DEBUG).

interaction.log — только события пользователей (INFO).
debug.log       — технический трек (DEBUG) + дублирование interaction.
На stderr debug-логгер тоже пишет, чтобы sys_nohup.log содержал трейсы.
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

INTERACTION_LOG = "reminder_bot_interaction.log"
DEBUG_LOG = "reminder_bot_debug.log"
# Отслеживание навигации (нажатия, экраны, нижняя клавиатура) — ниже DEBUG,
# включается `manage.sh trace on` (аргумент --trace).
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_FMT = "%(asctime)s | %(levelname)-7s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ClosingStreamHandler(logging.StreamHandler):
    def close(self) -> None:
        stream = self.stream
        super().close()
        if stream not in (sys.stdout, sys.stderr):
            stream.close()


class _InteractionInfoFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == logging.INFO


def _file_handler(path: Path, formatter: logging.Formatter) -> logging.Handler:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    file_fd = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError(f"Файл лога должен быть обычным файлом: {path}")
        if file_stat.st_uid != os.geteuid() or file_stat.st_nlink != 1:
            raise PermissionError(
                f"Файл лога должен принадлежать текущему пользователю и не иметь hardlink: {path}"
            )
        os.fchmod(file_fd, 0o600)
        stream = os.fdopen(file_fd, "a", encoding="utf-8")
        file_fd = -1
    finally:
        if file_fd >= 0:
            os.close(file_fd)

    handler = _ClosingStreamHandler(stream)
    handler.setFormatter(formatter)
    return handler


def setup_loggers(
    log_dir: Path, *, trace: bool = False
) -> tuple[logging.Logger, logging.Logger]:
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # --- interaction ---
    interaction = logging.getLogger("reminder_bot.interaction")
    interaction.setLevel(logging.INFO)
    interaction.propagate = False
    old_handlers = set(interaction.handlers)
    debug = logging.getLogger("reminder_bot.debug")
    old_handlers.update(debug.handlers)
    for handler in old_handlers:
        handler.close()
    interaction.handlers.clear()

    ih = _file_handler(log_dir / INTERACTION_LOG, formatter)
    ih.setLevel(logging.INFO)
    ih.addFilter(_InteractionInfoFilter())
    interaction.addHandler(ih)

    # --- debug ---
    debug.setLevel(TRACE if trace else logging.DEBUG)
    debug.propagate = False
    debug.handlers.clear()
    dh = _file_handler(log_dir / DEBUG_LOG, formatter)
    debug.addHandler(dh)
    debug.addHandler(ih)                     # INFO-события дублируются в debug
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(formatter)
    sh.setLevel(logging.DEBUG)               # TRACE — только в debug.log
    debug.addHandler(sh)

    return interaction, debug
