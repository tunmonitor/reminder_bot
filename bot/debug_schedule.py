"""Скачивание и проверка расписания без запуска Telegram-бота."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_credentials, load_templates
from .excel_parser import (
    parse_people,
    parse_schedule,
    parse_service_events,
    resolve_assignments,
)
from .logger import setup_loggers
from .notifier import build_general_message, build_personal_message
from .secure_fs import open_private_directory
from .yandex_disk import download_public

DEFAULT_OUTPUT_DIR = Path("/dev/shm/reminder_bot/debug")


def _secure_output_dir(path: Path) -> None:
    directory_fd = open_private_directory(path, create=True)
    os.close(directory_fd)


def _target_date(value: str | None, timezone: str) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД") from exc

    today = datetime.now(ZoneInfo(timezone)).date()
    days_until_sunday = (6 - today.weekday()) % 7
    return date.fromordinal(today.toordinal() + (days_until_sunday or 7))


async def run_check(target: date | None, output_dir: Path) -> Path:
    credentials = load_credentials()
    selected_date = target or _target_date(None, credentials.timezone)
    _secure_output_dir(output_dir)

    path = output_dir / f"schedule-{selected_date:%Y%m%d}-{uuid.uuid4().hex}.xlsx"
    _, debug_logger = setup_loggers(output_dir)

    await download_public(
        credentials.yandex_public_url, path, debug_logger, attempts=1, interval=0
    )
    os.chmod(path, 0o600)
    print(f"Скачанный файл сохранён: {path}")
    print(f"Размер файла: {path.stat().st_size} байт")

    schedule = parse_schedule(path, selected_date, debug_logger)
    people = parse_people(path, debug_logger)
    issues = [
        f"Не найдено назначение для роли «{role}»"
        for role in ("Ответственный", "Ведущий", "Проповедник")
        if role not in schedule
    ]
    assignments = resolve_assignments(schedule, people, debug_logger, issues)
    service_events = parse_service_events(
        path, selected_date, debug_logger
    )
    task_kinds = sorted(
        {
            (
                service_id,
                assignment.task,
                assignment.is_informational,
            )
            for service_id, events in service_events.items()
            for event in events
            for assignment in event.assignments
        }
    )
    print("Задачи служений в файле:")
    for service_id, task, is_informational in task_kinds:
        label = "информационная" if is_informational else "обычная"
        print(f"  {service_id}: {task} — {label}")
    templates = load_templates()
    sunday_str = selected_date.strftime("%d.%m.%Y")

    debug_logger.info(
        "=== Имитация рассылки на %s; отправка в Telegram отключена ===",
        sunday_str,
    )
    for issue in issues:
        debug_logger.warning("Предупреждение, которое получит администратор: %s", issue)
    general_text = build_general_message(assignments, templates, sunday_str)
    debug_logger.info(
        "Подготовлено сообщение в групповой чат (без отправки):\n%s",
        general_text,
    )
    for role, info in assignments.items():
        if not info.get("tg_id"):
            continue
        personal_text = build_personal_message(
            role, info, templates, sunday_str
        )
        debug_logger.info(
            "Подготовлено личное сообщение (без отправки): "
            "роль=%s, tg_id=%s, username=@%s\n%s",
            role, info["tg_id"], info["username"], personal_text,
        )
    debug_logger.info("=== Имитация рассылки завершена ===")

    print(f"Парсинг расписания на {sunday_str}: OK")
    print(
        f"Для имитации подготовлено: группа + "
        f"{sum(bool(info.get('tg_id')) for info in assignments.values())} личных "
        f"сообщений; предупреждений: {len(issues)}"
    )
    if issues:
        print("Проверка завершена с неполными данными; подробности записаны в debug-лог.")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.debug_schedule",
        description=(
            "Скачать расписание в постоянный приватный каталог и проверить "
            "парсинг без отправки сообщений в Telegram."
        ),
    )
    parser.add_argument(
        "--date",
        help="Целевая дата в формате ГГГГ-ММ-ДД (по умолчанию — ближайшее воскресенье).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Каталог отладки (по умолчанию: {DEFAULT_OUTPUT_DIR}).",
    )
    args = parser.parse_args(argv)

    try:
        target = date.fromisoformat(args.date) if args.date else None
        asyncio.run(run_check(target, args.output_dir))
    except Exception as exc:
        print(
            f"Проверка не пройдена: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
