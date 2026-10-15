"""Сборка приложения: логгеры, PID-файл, scheduler, polling и обработчики."""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

if __package__:
    from .cli import CliArgs
    from .config import (
        PROJECT_DIR,
        get_tz,
        load_credentials,
        load_schedule,
        load_templates,
        load_webapp_config,
        service_registry_path,
    )
    from .excel_parser import (
        ROLES,
        parse_people,
        parse_schedule,
        parse_service_events,
        resolve_assignments,
    )
    from .handlers import register
    from .logger import setup_loggers
    from .notifier import (
        TELEGRAM_TEXT_LIMIT,
        _notify_admin,
        _utf16_length,
        build_general_message,
        build_personal_message,
        build_service_confirm_kb,
        send_assignments,
    )
    from .presentation import format_task_assignments
    from .ui import send_telegram_screen
    from .secure_fs import open_private_directory
    from .scheduler import (
        add_service_reminder_jobs,
        build_scheduler,
        scheduled_reminder_date,
    )
    from .service_registry import ServiceRegistry
    from .state import StateCache
    from .yandex_disk import download_public
    from .backup import (
        BackupManager,
        application_instance_lock,
        environment_name,
    )
else:  # pragma: no cover - поддержка запуска файла напрямую
    from bot.cli import CliArgs
    from bot.config import (
        PROJECT_DIR,
        get_tz,
        load_credentials,
        load_schedule,
        load_templates,
        load_webapp_config,
        service_registry_path,
    )
    from bot.excel_parser import (
        ROLES,
        parse_people,
        parse_schedule,
        parse_service_events,
        resolve_assignments,
    )
    from bot.handlers import register
    from bot.logger import setup_loggers
    from bot.notifier import (
        TELEGRAM_TEXT_LIMIT,
        _notify_admin,
        _utf16_length,
        build_general_message,
        build_personal_message,
        build_service_confirm_kb,
        send_assignments,
    )
    from bot.presentation import format_task_assignments
    from bot.ui import send_telegram_screen
    from bot.secure_fs import open_private_directory
    from bot.scheduler import (
        add_service_reminder_jobs,
        build_scheduler,
        scheduled_reminder_date,
    )
    from bot.service_registry import ServiceRegistry
    from bot.state import StateCache
    from bot.yandex_disk import download_public
    from bot.backup import (
        BackupManager,
        application_instance_lock,
        environment_name,
    )


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


def _open_directory_without_symlinks(path: Path) -> int:
    return open_private_directory(path)


def _write_pid(path: Path) -> None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    pid_fd = -1
    created = False
    try:
        pid_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        pid_fd = os.open(path.name, pid_flags, 0o600, dir_fd=directory_fd)
        created = True
        try:
            os.fchmod(pid_fd, 0o600)
            with os.fdopen(pid_fd, "w", encoding="utf-8") as pid_file:
                pid_fd = -1
                pid_file.write(f"{os.getpid()}:{_current_process_starttime()}")
        except BaseException:
            if created:
                os.unlink(path.name, dir_fd=directory_fd)
            raise
    finally:
        if pid_fd >= 0:
            os.close(pid_fd)
        os.close(directory_fd)


def _remove_pid_if_owned(path: Path) -> None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    try:
        try:
            pid_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return
        try:
            pid_stat = os.fstat(pid_fd)
            if (
                not stat.S_ISREG(pid_stat.st_mode)
                or pid_stat.st_uid != os.geteuid()
                or stat.S_IMODE(pid_stat.st_mode) != 0o600
            ):
                return
            with os.fdopen(pid_fd, "r", encoding="utf-8") as pid_file:
                pid_fd = -1
                if pid_file.read().strip() != (
                    f"{os.getpid()}:{_current_process_starttime()}"
                ):
                    return
        finally:
            if pid_fd >= 0:
                os.close(pid_fd)
        os.unlink(path.name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


async def _configure_webapp_menu(bot: Bot, project_dir: Path, logger) -> None:
    config_candidates = (
        project_dir / "reminder_bot_webapp_config.json",
        project_dir / "webapp_config.json",
    )
    if not any(path.is_file() for path in config_candidates):
        return
    try:
        config = load_webapp_config(project_dir)
    except (OSError, RuntimeError, ValueError):
        logger.exception("Не удалось загрузить конфигурацию Telegram Web App")
        return
    if config.public_url is None:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть приложение",
                web_app=WebAppInfo(url=config.public_url),
            )
        )
    except TelegramAPIError:
        logger.exception("Не удалось настроить кнопку Telegram Web App")


def _existing_pid_process_status(path: Path) -> tuple[str, bool] | None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    try:
        try:
            pid_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            pid_stat = os.fstat(pid_fd)
            if (
                not stat.S_ISREG(pid_stat.st_mode)
                or pid_stat.st_uid != os.geteuid()
                or pid_stat.st_nlink != 1
                or stat.S_IMODE(pid_stat.st_mode) & 0o022
            ):
                return None
            content_bytes = os.read(pid_fd, 129)
            if len(content_bytes) > 128:
                return None
            content = content_bytes.decode("ascii").strip()
        except UnicodeDecodeError:
            return None
        finally:
            os.close(pid_fd)
    finally:
        os.close(directory_fd)

    pid, separator, starttime = content.partition(":")
    if not separator or not pid.isdigit() or not starttime.isdigit():
        return None

    try:
        process_fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()
        )
    except FileNotFoundError:
        return pid, False
    except PermissionError:
        return None
    running = (
        len(process_fields) > 19
        and process_fields[0] != "Z"
        and process_fields[19] == starttime
    )
    return pid, running


def _build_state(
    assignments: dict[str, dict], group_chat_id: int | None = None
) -> dict:
    return {
        "sunday_date": None,
        "group_chat_id": group_chat_id,
        "general_message_id": None,
        "general_sent": False,
        "general_text": None,
        "complete": False,
        "participants": {
            role: {
                "name": info["name"],
                "username": info["username"],
                "tg_id": info["tg_id"],
                "delivered": False,
                "confirmed": False,
                "message_id": None,
                "chat_id": None,
            }
            for role, info in assignments.items()
        },
    }


def _merge_week_state(
    previous: dict | None,
    current: dict,
    group_chat_id: int | None = None,
    legacy_group_chat_id: int | None = None,
) -> dict:
    if not previous or previous.get("sunday_date") != current["sunday_date"]:
        return current

    if group_chat_id is None:
        group_chat_id = current.get("group_chat_id")
    old_group_chat_id = previous.get("group_chat_id")
    if old_group_chat_id is None:
        old_group_chat_id = legacy_group_chat_id
    current["group_chat_id"] = group_chat_id
    same_group_chat = old_group_chat_id == group_chat_id
    if same_group_chat:
        current["general_message_id"] = previous.get("general_message_id")
        current["general_sent"] = previous.get(
            "general_sent", previous.get("general_message_id") is not None
        )
        current["general_text"] = previous.get("general_text")
    old_participants = previous.get("participants", {})
    for role, participant in current["participants"].items():
        old = old_participants.get(role, {})
        if old.get("tg_id") == participant["tg_id"]:
            old_participant_group_chat_id = old.get("group_chat_id")
            if old_participant_group_chat_id is None:
                old_participant_group_chat_id = old_group_chat_id
            same_participant_group = (
                old_participant_group_chat_id == group_chat_id
            )
            participant["delivered"] = old.get(
                "delivered", old.get("message_id") is not None
            )
            participant["message_id"] = old.get("message_id")
            participant["chat_id"] = old.get("chat_id")
            participant["group_chat_id"] = group_chat_id
            participant["confirmed"] = old.get("confirmed", False)
            participant["group_update_sent"] = (
                old.get("group_update_sent", False)
                if same_participant_group
                else False
            )
            if not same_participant_group:
                participant["group_update_persisted"] = False
    return current


async def _prepare_test_notification_previews(
    credentials,
    templates: dict,
    shm_dir: Path,
    tz,
    debug_logger,
) -> dict:
    sunday_date = _next_sunday_in_tz(tz)
    debug_dir = shm_dir / "debug"
    directory_fd = open_private_directory(debug_dir, create=True)
    os.close(directory_fd)

    file_fd, file_name = tempfile.mkstemp(
        prefix=f"schedule-{sunday_date:%Y%m%d}-",
        suffix=".xlsx",
        dir=debug_dir,
    )
    os.close(file_fd)
    xlsx_path = Path(file_name)
    try:
        await download_public(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            attempts=1,
            interval=0,
        )
        schedule = parse_schedule(xlsx_path, sunday_date, debug_logger)
        people = parse_people(xlsx_path, debug_logger)
        issues = [
            f"На {sunday_date:%d.%m.%Y} не найдено назначение для роли «{role}»"
            for role in ROLES
            if role not in schedule
        ]
        assignments = resolve_assignments(
            schedule, people, debug_logger, issues
        )
        sunday_str = sunday_date.strftime("%d.%m.%Y")
        return {
            "sunday_date": sunday_date,
            "group_text": build_general_message(
                assignments, templates, sunday_str
            ),
            "personal_messages": [
                (
                    role,
                    info["name"],
                    build_personal_message(role, info, templates, sunday_str),
                )
                for role, info in assignments.items()
                if info.get("tg_id")
            ],
            "issues": issues,
        }
    finally:
        xlsx_path.unlink(missing_ok=True)


async def _load_service_task_assignments(
    credentials,
    shm_dir: Path,
    tz,
    service_id: str,
    debug_logger,
    only_date: date | None = None,
    *,
    return_event_found: bool = False,
) -> dict[str, list[str]] | tuple[dict[str, list[str]], bool]:
    sunday_date = only_date or _next_sunday_in_tz(tz)
    debug_dir = shm_dir / "debug"
    directory_fd = open_private_directory(debug_dir, create=True)
    os.close(directory_fd)
    file_fd, file_name = tempfile.mkstemp(
        prefix=f"schedule-tasks-{sunday_date:%Y%m%d}-",
        suffix=".xlsx",
        dir=debug_dir,
    )
    os.fchmod(file_fd, 0o600)
    os.close(file_fd)
    xlsx_path = Path(file_name)
    try:
        await download_public(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            attempts=1,
            interval=0,
        )
        events_by_service = parse_service_events(
            xlsx_path, sunday_date, debug_logger
        )
        service_events = events_by_service.get(service_id, [])
        event_found = any(
            event.event_date == sunday_date for event in service_events
        )
        assignments: dict[str, set[str]] = {}
        for event in service_events:
            if only_date is not None and event.event_date != only_date:
                continue
            for assignment in event.assignments:
                assignments.setdefault(assignment.task, set()).add(
                    assignment.name
                )
        result = {
            task: sorted(names)
            for task, names in sorted(assignments.items())
        }
        if return_event_found:
            return result, event_found
        return result
    finally:
        xlsx_path.unlink(missing_ok=True)


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _service_confirmation_key(service_id: str, task: str, telegram_id: int) -> str:
    digest = hashlib.sha256(
        f"{service_id}\0{task}\0{telegram_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"service_{digest}"


async def _process_service_reminder(
    bot: Bot,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    service_id: str,
    reminder: dict,
    interaction_logger,
    debug_logger,
    *,
    run_date: date | None = None,
    state_cache: StateCache | None = None,
) -> None:
    service = registry.get_service(service_id)
    if service is None or not service["enabled"]:
        return
    event_date = (run_date or datetime.now(tz).date()) + timedelta(
        days=reminder["reminder_days"]
    )
    try:
        assignments, event_found = await _load_service_task_assignments(
            credentials,
            shm_dir,
            tz,
            service_id,
            debug_logger,
            only_date=event_date,
            return_event_found=True,
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось подготовить уведомление служения %s на %s",
            service_id,
            event_date,
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Не удалось подготовить уведомление «{service['name']}» "
            f"на {event_date:%d.%m.%Y}: {type(exc).__name__}: {exc}",
            debug_logger,
        )
        return
    if not event_found:
        interaction_logger.info(
            "Уведомление служения %s пропущено: событие на %s не найдено",
            service_id,
            event_date,
        )
        return

    try:
        contacts = registry.list_responsible_contacts(
            credentials.admin_tg_id, service_id
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось загрузить контакты служения %s", service_id
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Не удалось загрузить контакты служения «{service['name']}»: "
            f"{type(exc).__name__}: {exc}",
            debug_logger,
        )
        return
    contacts_by_name = {
        (contact["task"], contact["name"]): contact for contact in contacts
    }
    date_text = event_date.strftime("%d.%m.%Y")
    confirmation_participants = {}
    confirmation_state_ready = service_id != "assembly" or state_cache is None
    if service_id == "assembly" and state_cache is not None:
        event_iso = event_date.isoformat()
        current_state = state_cache.state
        if not current_state or current_state.get("sunday_date") != event_iso:
            current_state = {
                "sunday_date": event_iso,
                "group_chat_id": service["group_chat_id"],
                "general_message_id": None,
                "general_sent": False,
                "general_text": None,
                "complete": False,
                "participants": {},
            }
            state_cache.state = current_state
        participants = current_state.setdefault("participants", {})
        previous_participants = dict(participants)
        for key, participant in tuple(participants.items()):
            if participant.get("service_id") == service_id:
                del participants[key]
        current_state["group_chat_id"] = service["group_chat_id"]
        for task, names in assignments.items():
            for name in names:
                contact = contacts_by_name.get((task, name))
                if contact is None:
                    continue
                telegram_id = contact["telegram_id"]
                key = _service_confirmation_key(
                    service_id, task, telegram_id
                )
                participant = {
                    "name": name,
                    "username": contact["username"],
                    "tg_id": telegram_id,
                    "task": task,
                    "service_id": service_id,
                    "delivered": False,
                    "confirmed": False,
                    "message_id": None,
                    "chat_id": None,
                    "group_chat_id": service["group_chat_id"],
                }
                participant.update(previous_participants.get(key, {}))
                participant.update({
                    "name": name,
                    "username": contact["username"],
                    "tg_id": telegram_id,
                    "task": task,
                    "service_id": service_id,
                    "group_chat_id": service["group_chat_id"],
                })
                participants[key] = participant
                confirmation_participants[(task, name)] = (key, participant)
        try:
            state_cache.save()
            confirmation_state_ready = True
        except OSError:
            debug_logger.exception(
                "Не удалось сохранить состояние подтверждений служения «Собрание»"
            )
            await _notify_admin(
                bot,
                credentials.admin_tg_id,
                "Не удалось сохранить состояние подтверждений «Собрания»; "
                "личные уведомления не отправлены.",
                debug_logger,
            )
            confirmation_state_ready = False

    assignment_text = format_task_assignments(
        assignments, contacts_by_name, mark_unlinked=True
    )
    group_template = service["group_template"] or (
        "Напоминание о служении «{service_name}» в воскресенье "
        "{sunday_date}:\n{task_assignments}"
    )
    try:
        group_text = group_template.format_map(
            _TemplateValues(
                service_name=service["name"],
                sunday_date=date_text,
                task_assignments=assignment_text,
            )
        )
    except (AttributeError, IndexError, KeyError, ValueError) as exc:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Некорректный групповой шаблон служения «{service['name']}»: "
            f"{type(exc).__name__}",
            debug_logger,
        )
        return

    if not assignments:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"В графике найдено служение «{service['name']}» на "
            f"{date_text}, но задачи не назначены.",
            debug_logger,
        )

    group_chat_id = service["group_chat_id"]
    group_recipient = f"group:{group_chat_id}"
    if group_chat_id is None:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Для служения «{service['name']}» не задан групповой чат.",
            debug_logger,
        )
    elif not registry.is_service_reminder_delivered(
        service_id, reminder["id"], event_date.isoformat(), group_recipient
    ):
        if _utf16_length(group_text) > TELEGRAM_TEXT_LIMIT:
            await _notify_admin(
                bot,
                credentials.admin_tg_id,
                f"Групповое уведомление «{service['name']}» на {date_text} "
                "превышает ограничение Telegram и не отправлено.",
                debug_logger,
            )
        else:
            try:
                await send_telegram_screen(
                    bot.send_message, group_chat_id, group_text
                )
            except Exception as exc:
                debug_logger.exception(
                    "Не удалось отправить групповое уведомление %s на %s",
                    service_id,
                    event_date,
                )
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Не удалось отправить групповое уведомление "
                    f"«{service['name']}» на {date_text}: "
                    f"{type(exc).__name__}.",
                    debug_logger,
                )
            else:
                registry.mark_service_reminder_delivered(
                    service_id,
                    reminder["id"],
                    event_date.isoformat(),
                    group_recipient,
                )
                interaction_logger.info(
                    "Групповое уведомление служения %s отправлено на %s",
                    service_id,
                    event_date,
                )

    for task, names in assignments.items():
        for name in names:
            contact = contacts_by_name.get((task, name))
            if contact is None:
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Не привязан Telegram для ответственного «{name}» "
                    f"(задача «{task}», служение «{service['name']}», "
                    f"{date_text}); личное уведомление пропущено.",
                    debug_logger,
                )
                continue
            telegram_id = contact["telegram_id"]
            recipient = f"personal:{task}:{telegram_id}"
            if registry.is_service_reminder_delivered(
                service_id, reminder["id"], event_date.isoformat(), recipient
            ):
                continue
            if service_id == "assembly" and not confirmation_state_ready:
                continue
            template = service["personal_templates"].get(
                task,
                "{name}, вы назначены на задачу «{task}» службы "
                "«{service_name}» на {sunday_date}.",
            )
            try:
                personal_text = template.format_map(
                    _TemplateValues(
                        name=name,
                        task=task,
                        service_name=service["name"],
                        sunday_date=date_text,
                        telegram_id=str(telegram_id),
                        username=contact["username"],
                    )
                )
            except (AttributeError, IndexError, KeyError, ValueError) as exc:
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Некорректный личный шаблон задачи «{task}» "
                    f"служения «{service['name']}»: {type(exc).__name__}.",
                    debug_logger,
                )
                continue
            if not personal_text.startswith("🔔"):
                personal_text = f"🔔 {personal_text}"
            if _utf16_length(personal_text) > TELEGRAM_TEXT_LIMIT:
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Личное уведомление «{service['name']}» для {name} "
                    f"на {date_text} превышает ограничение Telegram.",
                    debug_logger,
                )
                continue
            try:
                send_kwargs = {}
                confirmation = confirmation_participants.get((task, name))
                if confirmation:
                    send_kwargs["reply_markup"] = build_service_confirm_kb(
                        confirmation[0], event_date.isoformat()
                    )
                sent_message = await send_telegram_screen(
                    bot.send_message, telegram_id, personal_text, **send_kwargs
                )
            except Exception as exc:
                debug_logger.exception(
                    "Не удалось отправить личное уведомление %s, задача %s",
                    service_id,
                    task,
                )
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Не удалось отправить личное уведомление «{service['name']}» "
                    f"для {name} на {date_text}: {type(exc).__name__}.",
                    debug_logger,
                )
            else:
                registry.mark_service_reminder_delivered(
                    service_id,
                    reminder["id"],
                    event_date.isoformat(),
                    recipient,
                )
                if confirmation:
                    participant = confirmation[1]
                    participant["delivered"] = True
                    participant["message_id"] = getattr(
                        sent_message, "message_id", None
                    )
                    participant["chat_id"] = getattr(
                        getattr(sent_message, "chat", None), "id", telegram_id
                    )
                    state_cache.save()
                interaction_logger.info(
                    "Личное уведомление служения %s для задачи %s отправлено",
                    service_id,
                    task,
                )


async def _prepare_service_test_previews(
    service_id: str,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    debug_logger,
    group_template: str | None = None,
    personal_templates: dict[str, str] | None = None,
) -> dict:
    sunday_date = _next_sunday_in_tz(tz)
    service = registry.get_service(service_id)
    if service is None:
        raise ValueError(f"Служение '{service_id}' не найдено")
    assignments = await _load_service_task_assignments(
        credentials,
        shm_dir,
        tz,
        service_id,
        debug_logger,
        only_date=sunday_date,
    )
    contacts = registry.list_responsible_contacts(
        credentials.admin_tg_id, service_id
    )
    contacts_by_name = {
        (contact["task"], contact["name"]): contact for contact in contacts
    }
    rendered_assignment_names: dict[str, list[str]] = {}
    personal_messages = []
    issues = []
    sunday_str = sunday_date.strftime("%d.%m.%Y")
    templates = (
        service["personal_templates"]
        if personal_templates is None
        else personal_templates
    )
    for task, names in assignments.items():
        for name in names:
            contact = contacts_by_name.get((task, name))
            if contact is None:
                rendered_assignment_names.setdefault(task, []).append(name)
                issues.append(
                    f"Не привязан Telegram-пользователь: {task} — {name}"
                )
                telegram_id = ""
                username = ""
            else:
                username = contact["username"]
                telegram_id = str(contact["telegram_id"])
                rendered_assignment_names.setdefault(task, []).append(name)
            template = templates.get(
                task,
                "{name}, вы назначены на задачу «{task}» службы "
                "«{service_name}» на {sunday_date}.",
            )
            try:
                personal_text = template.format_map(
                    _TemplateValues(
                        name=name,
                        task=task,
                        service_name=service["name"],
                        sunday_date=sunday_str,
                        telegram_id=telegram_id,
                        username=username,
                    )
                )
            except (AttributeError, IndexError, KeyError, ValueError) as exc:
                raise ValueError(
                    f"Некорректный личный шаблон для задачи «{task}» "
                    f"служения «{service['name']}»"
                ) from exc
            personal_messages.append(
                {
                    "task": task,
                    "name": name,
                    "telegram_id": telegram_id,
                    "text": personal_text,
                }
            )
    if not assignments:
        issues.append("В актуальном графике для службы нет назначенных задач.")
    assignment_text = format_task_assignments(
        rendered_assignment_names,
        contacts_by_name,
        mark_unlinked=True,
    )
    effective_group_template = (
        service["group_template"]
        if group_template is None
        else group_template
    ) or (
        "{service_name} — {sunday_date}\n{task_assignments}"
    )
    try:
        group_text = effective_group_template.format_map(
            _TemplateValues(
                service_name=service["name"],
                sunday_date=sunday_str,
                task_assignments=assignment_text,
            )
        )
    except (AttributeError, IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            f"Некорректный групповой шаблон служения «{service['name']}»"
        ) from exc
    example_group_template = (
        "Напоминание о служении «{service_name}» в воскресенье "
        "{sunday_date}:\n{task_assignments}"
    )
    example_group_text = example_group_template.format_map(
        _TemplateValues(
            service_name=service["name"],
            sunday_date=sunday_str,
            task_assignments=assignment_text,
        )
    )
    return {
        "sunday_date": sunday_date,
        "group_text": group_text,
        "example_group_template": example_group_template,
        "example_group_text": example_group_text,
        "personal_messages": personal_messages,
        "issues": issues,
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
    service_registry=None,
    backup_manager: BackupManager | None = None,
) -> None:
    delivery_group_chat_id = credentials.group_chat_id
    if service_registry is not None:
        assembly = service_registry.get_service("assembly")
        if assembly is None:
            raise RuntimeError("В реестре не настроено служение «Собрание»")
        if not assembly["enabled"]:
            interaction_logger.info(
                "Рассылка «Собрания» пропущена: служение отключено в реестре"
            )
            return
        delivery_group_chat_id = assembly["group_chat_id"]
        if delivery_group_chat_id is None:
            await _notify_admin(
                bot,
                credentials.admin_tg_id,
                "Рассылка «Собрания» не выполнена: не задан ID группового чата.",
                debug_logger,
            )
            return

    sunday_date = _next_sunday_in_tz(tz)
    sunday_iso = sunday_date.isoformat()
    if state_cache.matches_week(
        sunday_iso,
        delivery_group_chat_id,
        legacy_group_chat_id=credentials.group_chat_id,
    ):
        interaction_logger.info("Рассылка для %s уже отправлена", sunday_iso)
        return

    xlsx_path = shm_dir / "reminder_bot_current.xlsx"
    debug_logger.info("Подготовка рассылки на %s", sunday_iso)

    try:
        await download_public(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            on_change=backup_manager.mark_dirty if backup_manager else None,
        )
        schedule = parse_schedule(xlsx_path, sunday_date, debug_logger)
        people = parse_people(xlsx_path, debug_logger)
        assignment_issues = [
            f"На {sunday_date:%d.%m.%Y} не найдено назначение для роли «{role}»"
            for role in ROLES
            if role not in schedule
        ]
        assignments = resolve_assignments(
            schedule, people, debug_logger, assignment_issues
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось подготовить расписание на %s", sunday_iso
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            (
                f"Не удалось загрузить или разобрать расписание "
                f"на {sunday_date:%d.%m.%Y}: {type(exc).__name__}: {exc}"
            ),
            debug_logger,
        )
        raise

    if assignment_issues:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            (
                f"Неполные данные расписания на {sunday_date:%d.%m.%Y}. "
                "Рассылка продолжится для найденных участников. "
                + "; ".join(assignment_issues)
            ),
            debug_logger,
        )

    state = _build_state(assignments, delivery_group_chat_id)
    state["sunday_date"] = sunday_iso
    state = _merge_week_state(
        state_cache.state,
        state,
        delivery_group_chat_id,
        legacy_group_chat_id=credentials.group_chat_id,
    )
    state_cache.replace(state)

    try:
        general_delivery_complete = await send_assignments(
            bot=bot,
            group_chat_id=delivery_group_chat_id,
            admin_id=credentials.admin_tg_id,
            assignments=assignments,
            state=state,
            templates=templates,
            sunday_str=sunday_date.strftime("%d.%m.%Y"),
            sunday_iso=sunday_iso,
            interaction_logger=interaction_logger,
            debug_logger=debug_logger,
            save_state=state_cache.save,
        )
        state["complete"] = (
            not assignment_issues
            and general_delivery_complete
            and state.get("general_sent") is True
            and all(
                participant.get("delivered") is True
                for participant in state["participants"].values()
                if participant.get("tg_id")
            )
            and all(
                participant.get("tg_id")
                for participant in state["participants"].values()
            )
        )
        state_cache.save()
        if state["complete"]:
            interaction_logger.info("Рассылка для %s успешно завершена", sunday_iso)
        else:
            interaction_logger.info(
                "Рассылка для %s частично выполнена; незавершённые отправки "
                "будут повторены при следующем запуске",
                sunday_iso,
            )
    except Exception:
        debug_logger.exception("Не удалось отправить рассылку для %s", sunday_iso)
        raise


async def _run_locked(args: CliArgs) -> None:
    shm_dir = Path(args.shm_dir)
    log_dir = Path(args.log_dir)
    for directory in (shm_dir, log_dir):
        directory_fd = open_private_directory(directory, create=True)
        os.close(directory_fd)

    interaction_logger, debug_logger = setup_loggers(log_dir)
    credentials = load_credentials()
    templates = load_templates()
    tz = get_tz(credentials.timezone)
    try:
        backup_manager = BackupManager(
            environment_name(PROJECT_DIR),
            shm_dir,
            tz,
            debug_logger,
            registry_path=service_registry_path(),
        )
    except RuntimeError as exc:
        debug_logger.warning("Резервирование отключено: %s", exc)
        backup_manager = None
    change_callback = backup_manager.mark_dirty if backup_manager else None
    state_cache = StateCache(
        shm_dir / "reminder_state_cache.json", on_change=change_callback
    )
    service_registry = ServiceRegistry(
        service_registry_path(), on_change=change_callback
    )
    registry_context_group_chat_id = credentials.group_chat_id
    try:
        service_registry.initialize(
            credentials.admin_tg_id, credentials.group_chat_id
        )
        assembly = service_registry.get_service("assembly")
        if assembly and assembly["group_chat_id"] is not None:
            registry_context_group_chat_id = assembly["group_chat_id"]
    except Exception:
        debug_logger.exception(
            "Не удалось инициализировать реестр служений; "
            "продолжаем запуск существующих напоминаний без него"
        )
        service_registry = None
    jobs = load_schedule() if service_registry is None else []
    pid_file = _pid_path(shm_dir)
    bot = None
    scheduler = None
    nightly_backup_task: asyncio.Task | None = None
    pid_created = False
    active_jobs: set[asyncio.Task[None]] = set()
    service_reminder_locks: dict[tuple[str, date], asyncio.Lock] = {}

    try:
        bot = Bot(
            token=credentials.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        if callable(getattr(bot, "set_my_commands", None)):
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="services", description="Список служений"),
                        BotCommand(
                            command="service_settings",
                            description="Настройки служения",
                        ),
                        BotCommand(
                            command="service_admin_add",
                            description="Назначить администратора служения",
                        ),
                        BotCommand(
                            command="service_admin_remove",
                            description="Снять администратора служения",
                        ),
                        BotCommand(
                            command="service_chat",
                            description="Настроить групповой чат",
                        ),
                        BotCommand(
                            command="service_contact",
                            description="Связать ответственного с Telegram",
                        ),
                        BotCommand(
                            command="service_sync",
                            description="Обновить задачи и ответственных из графика",
                        ),
                        BotCommand(
                            command="service_notify",
                            description="Настроить тексты и время уведомлений",
                        ),
                        BotCommand(
                            command="cancel",
                            description="Отменить ввод параметров",
                        ),
                        BotCommand(
                            command="start",
                            description="Начать работу с ботом",
                        ),
                        BotCommand(
                            command="test_notify",
                            description="Проверить доставку уведомлений",
                        ),
                    ]
                )
            except TelegramAPIError:
                debug_logger.exception(
                    "Не удалось обновить меню команд Telegram"
                )
        dp = Dispatcher(storage=MemoryStorage())
        context = SimpleNamespace(
            bot=bot,
            group_chat_id=registry_context_group_chat_id,
            admin_id=credentials.admin_tg_id,
            state_cache=state_cache,
            interaction_logger=interaction_logger,
            debug_logger=debug_logger,
            confirmation_locks={},
            service_registry=service_registry,
        )

        async def prepare_test_previews() -> dict:
            return await _prepare_test_notification_previews(
                credentials, templates, shm_dir, tz, debug_logger
            )

        async def load_service_task_assignments(
            service_id: str,
        ) -> dict[str, list[str]]:
            return await _load_service_task_assignments(
                credentials, shm_dir, tz, service_id, debug_logger
            )

        async def prepare_service_test_previews(
            service_id: str,
            group_template: str | None = None,
            personal_templates: dict[str, str] | None = None,
        ) -> dict:
            if service_registry is None:
                raise ValueError("Реестр настроек служений недоступен")
            return await _prepare_service_test_previews(
                service_id,
                credentials,
                shm_dir,
                tz,
                service_registry,
                debug_logger,
                group_template,
                personal_templates,
            )

        context.prepare_test_previews = prepare_test_previews
        context.load_service_task_assignments = load_service_task_assignments
        context.prepare_service_test_previews = prepare_service_test_previews
        register(dp, context)

        if backup_manager is not None:
            async def notify_backup_failure(message: str) -> None:
                await _notify_admin(
                    bot, credentials.admin_tg_id, message, debug_logger
                )

            backup_manager.set_failure_callback(notify_backup_failure)

            async def create_startup_backup() -> None:
                try:
                    current_schedule = shm_dir / "reminder_bot_current.xlsx"
                    if not current_schedule.is_file():
                        await download_public(
                            credentials.yandex_public_url,
                            current_schedule,
                            debug_logger,
                            attempts=1,
                            interval=0,
                            on_change=backup_manager.mark_dirty,
                        )
                    await backup_manager.create_snapshot("запуск экземпляра")
                except Exception as exc:
                    debug_logger.exception(
                        "Не удалось создать резервную копию при запуске"
                    )
                    await notify_backup_failure(
                        "Не удалось создать резервную копию при запуске: "
                        f"{type(exc).__name__}: {exc}"
                    )

        else:
            create_startup_backup = None

        async def scheduled_job() -> None:
            task = asyncio.current_task()
            if task is not None:
                active_jobs.add(task)
            try:
                await _process_upcoming_sunday(
                    bot=bot,
                    credentials=credentials,
                    templates=templates,
                    state_cache=state_cache,
                    shm_dir=shm_dir,
                    interaction_logger=interaction_logger,
                    debug_logger=debug_logger,
                    tz=tz,
                    service_registry=service_registry,
                    backup_manager=backup_manager,
                )
            finally:
                if task is not None:
                    active_jobs.discard(task)

        scheduler = build_scheduler(
            tz=tz,
            jobs=jobs,
            callback=scheduled_job,
            debug_logger=debug_logger,
        )
        if service_registry is not None:
            async def scheduled_service_reminder(
                service_id: str, reminder: dict
            ) -> None:
                run_date = scheduled_reminder_date(
                    datetime.now(tz), reminder["reminder_time"]
                )
                event_date = run_date + timedelta(
                    days=reminder["reminder_days"]
                )
                lock = service_reminder_locks.setdefault(
                    (service_id, event_date), asyncio.Lock()
                )
                task = asyncio.current_task()
                if task is not None:
                    active_jobs.add(task)
                try:
                    async with lock:
                        await _process_service_reminder(
                            bot,
                            credentials,
                            shm_dir,
                            tz,
                            service_registry,
                            service_id,
                            reminder,
                            interaction_logger,
                            debug_logger,
                            run_date=run_date,
                            state_cache=state_cache,
                        )
                except Exception:
                    debug_logger.exception(
                        "Ошибка обработки напоминания служения %s",
                        service_id,
                    )
                    await _notify_admin(
                        bot,
                        credentials.admin_tg_id,
                        f"Ошибка обработки напоминания служения "
                        f"«{service_id}». Подробности записаны в журнал.",
                        debug_logger,
                    )
                finally:
                    if task is not None:
                        active_jobs.discard(task)

            async def refresh_service_reminders() -> None:
                if service_registry is None or scheduler is None:
                    return
                await add_service_reminder_jobs(
                    scheduler,
                    service_registry.list_services(),
                    scheduled_service_reminder,
                    tz,
                    debug_logger,
                )

            context.refresh_service_reminder_jobs = refresh_service_reminders

        try:
            _write_pid(pid_file)
            pid_created = True
        except FileExistsError as exc:
            existing_process = _existing_pid_process_status(pid_file)
            if existing_process is None:
                status = "Содержимое PID-файла не удалось безопасно проверить."
            elif existing_process[1]:
                status = f"Процесс с PID {existing_process[0]} уже запущен."
            else:
                status = (
                    f"PID-файл устарел (PID {existing_process[0]} "
                    "не соответствует живому процессу)."
                )
            message = (
                f"PID-файл уже существует: {pid_file}. {status} "
                "Удалите его вручную, если он устарел."
            )
            debug_logger.error(message)
            raise FileExistsError(message) from exc

        if service_registry is not None:
            await refresh_service_reminders()
        scheduler.start()
        await _configure_webapp_menu(bot, PROJECT_DIR, debug_logger)
        if backup_manager is not None:
            if create_startup_backup is not None:
                backup_manager.start_task(
                    create_startup_backup(),
                    name="reminder-bot-startup-backup",
                )
            nightly_backup_task = asyncio.create_task(
                backup_manager.run_nightly(),
                name="reminder-bot-nightly-backup",
            )
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        if nightly_backup_task is not None:
            nightly_backup_task.cancel()
            await asyncio.gather(nightly_backup_task, return_exceptions=True)
        try:
            if scheduler is not None and scheduler.running:
                scheduler.pause()
                await asyncio.sleep(0)
                if active_jobs:
                    _done, pending = await asyncio.wait(
                        tuple(active_jobs), timeout=30,
                    )
                    if pending:
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                scheduler.shutdown(wait=False)
        finally:
            try:
                if backup_manager is not None:
                    await backup_manager.close()
            finally:
                try:
                    if bot is not None:
                        await bot.session.close()
                finally:
                    if pid_created:
                        _remove_pid_if_owned(pid_file)


async def run(args: CliArgs) -> None:
    with application_instance_lock(Path(args.shm_dir)):
        await _run_locked(args)


__all__ = ["run"]
