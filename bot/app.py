"""Сборка приложения: логгеры, PID-файл, scheduler, polling и обработчики."""
from __future__ import annotations

import asyncio
import os
import signal
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

if __package__:
    from .cli import CliArgs
    from .config import get_tz, load_credentials, load_schedule, load_templates
    from .excel_parser import parse_people, parse_schedule, resolve_assignments
    from .handlers import register
    from .logger import setup_loggers
    from .notifier import send_assignments
    from .scheduler import build_scheduler
    from .state import StateCache
    from .yandex_disk import download_public
else:  # pragma: no cover - поддержка запуска файла напрямую
    from bot.cli import CliArgs
    from bot.config import get_tz, load_credentials, load_schedule, load_templates
    from bot.excel_parser import parse_people, parse_schedule, resolve_assignments
    from bot.handlers import register
    from bot.logger import setup_loggers
    from bot.notifier import send_assignments
    from bot.scheduler import build_scheduler
    from bot.state import StateCache
    from bot.yandex_disk import download_public


def _next_sunday_in_tz(tz) -> date:
    today = datetime.now(tz).date()
    days_until_sunday = (6 - today.weekday()) % 7
    if days_until_sunday == 0:
        return today + timedelta(days=7)
    return today + timedelta(days=days_until_sunday)


def _pid_path(shm_dir: Path) -> Path:
    return shm_dir / "reminder_bot_bot.pid"


def _current_process_starttime() -> str:
    stat_fields = (
        Path("/proc/self/stat")
        .read_text(encoding="utf-8")
        .rsplit(")", 1)[1]
        .split()
    )
    return stat_fields[19]


def _write_pid(path: Path) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path.parent, directory_flags)
    try:
        directory_stat = os.fstat(directory_fd)
        if directory_stat.st_uid != os.geteuid():
            raise PermissionError(
                f"Каталог PID-файла должен принадлежать текущему пользователю: {path.parent}"
            )
        if stat.S_IMODE(directory_stat.st_mode) & 0o022:
            raise PermissionError(
                f"Каталог PID-файла не должен быть доступен на запись группе или другим: {path.parent}"
            )

        pid_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        pid_fd = os.open(path.name, pid_flags, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(pid_fd, 0o600)
            with os.fdopen(pid_fd, "w", encoding="utf-8") as pid_file:
                pid_fd = -1
                pid_file.write(f"{os.getpid()}:{_current_process_starttime()}")
        finally:
            if pid_fd >= 0:
                os.close(pid_fd)
    finally:
        os.close(directory_fd)


def _build_state(assignments: dict[str, dict]) -> dict:
    return {
        "sunday_date": None,
        "general_message_id": None,
        "participants": {
            role: {
                "name": info["name"],
                "username": info["username"],
                "tg_id": info["tg_id"],
                "confirmed": False,
                "message_id": None,
                "chat_id": None,
            }
            for role, info in assignments.items()
        },
    }


async def _process_upcoming_sunday(
    bot: Bot,
    credentials,
    templates: dict,
    state_cache: StateCache,
    shm_dir: Path,
    interaction_logger,
    debug_logger,
    tz,
) -> None:
    sunday_date = _next_sunday_in_tz(tz)
    sunday_iso = sunday_date.isoformat()
    if state_cache.matches_week(sunday_iso):
        interaction_logger.info("Рассылка для %s уже отправлена", sunday_iso)
        return

    xlsx_path = shm_dir / "reminder_bot_current.xlsx"
    debug_logger.info("Подготовка рассылки на %s", sunday_iso)

    await download_public(credentials.yandex_public_url, xlsx_path, debug_logger)
    schedule = parse_schedule(xlsx_path, sunday_date, debug_logger)
    people = parse_people(xlsx_path, debug_logger)
    assignments = resolve_assignments(schedule, people)

    state = _build_state(assignments)
    state["sunday_date"] = sunday_iso
    state_cache.replace(state)

    try:
        await send_assignments(
            bot=bot,
            group_chat_id=credentials.group_chat_id,
            admin_id=credentials.admin_tg_id,
            assignments=assignments,
            state=state,
            templates=templates,
            sunday_str=sunday_date.strftime("%d.%m.%Y"),
            sunday_iso=sunday_iso,
            interaction_logger=interaction_logger,
            debug_logger=debug_logger,
        )
        state_cache.save()
        interaction_logger.info("Рассылка для %s успешно завершена", sunday_iso)
    except Exception:
        debug_logger.exception("Не удалось отправить рассылку для %s", sunday_iso)
        raise


async def run(args: CliArgs) -> None:
    shm_dir = Path(args.shm_dir)
    log_dir = Path(args.log_dir)
    shm_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    interaction_logger, debug_logger = setup_loggers(log_dir)
    credentials = load_credentials()
    templates = load_templates()
    tz = get_tz(credentials.timezone)
    state_cache = StateCache(shm_dir / "reminder_state_cache.json")

    _write_pid(_pid_path(shm_dir))

    bot = Bot(
        token=credentials.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    context = SimpleNamespace(
        bot=bot,
        group_chat_id=credentials.group_chat_id,
        state_cache=state_cache,
        interaction_logger=interaction_logger,
        debug_logger=debug_logger,
    )
    register(dp, context)

    async def scheduled_job() -> None:
        await _process_upcoming_sunday(
            bot=bot,
            credentials=credentials,
            templates=templates,
            state_cache=state_cache,
            shm_dir=shm_dir,
            interaction_logger=interaction_logger,
            debug_logger=debug_logger,
            tz=tz,
        )

    scheduler = build_scheduler(
        tz=tz,
        jobs=load_schedule(),
        callback=scheduled_job,
        debug_logger=debug_logger,
    )

    shutdown_task: asyncio.Task[None] | None = None

    async def perform_shutdown() -> None:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        pid_file = _pid_path(shm_dir)
        if pid_file.exists():
            pid_file.unlink()

    async def shutdown() -> None:
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(perform_shutdown())
        await asyncio.shield(shutdown_task)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:  # pragma: no cover - Windows/limited platforms
            pass

    try:
        scheduler.start()
        await dp.start_polling(bot)
    finally:
        await shutdown()


__all__ = ["run"]
