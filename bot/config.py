"""Загрузка креденшелов, шаблонов и расписания.

Приоритет:
1) файлы с префиксом reminder_bot_* в корне проекта;
2) короткие имена (credentials.env, templates.json, schedule_config.json);
3) переменные окружения процесса.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_NAME = PROJECT_DIR.name  # 'test' или 'prod'


# ----------------------------
# .env-подобный парсер
# ----------------------------
def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


# ----------------------------
# Credentials
# ----------------------------
@dataclass(frozen=True)
class Credentials:
    bot_token: str
    admin_tg_id: int
    group_chat_id: int
    timezone: str
    yandex_public_url: str


def load_credentials() -> Credentials:
    env_path = _first_existing(
        PROJECT_DIR / "reminder_bot_credentials.env",
        PROJECT_DIR / "credentials.env",
    )
    file_data = _parse_env_file(env_path) if env_path else {}

    def get(key: str, default: str | None = None) -> str | None:
        return file_data.get(key) or os.environ.get(key) or default

    token = get("BOT_TOKEN")
    admin = get("ADMIN_TG_ID")
    group = get("GROUP_CHAT_ID")
    yandex = get("YANDEX_PUBLIC_URL")

    missing = [k for k, v in {
        "BOT_TOKEN": token,
        "ADMIN_TG_ID": admin,
        "GROUP_CHAT_ID": group,
        "YANDEX_PUBLIC_URL": yandex,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Не заданы обязательные переменные: {', '.join(missing)}")

    return Credentials(
        bot_token=token,
        admin_tg_id=int(admin),
        group_chat_id=int(group),
        timezone=get("TIMEZONE", "Europe/Moscow"),
        yandex_public_url=yandex,
    )


def service_registry_path() -> Path:
    env_path = _first_existing(
        PROJECT_DIR / "reminder_bot_credentials.env",
        PROJECT_DIR / "credentials.env",
    )
    file_data = _parse_env_file(env_path) if env_path else {}
    configured_path = (
        file_data.get("REMINDER_BOT_DB_PATH")
        or os.environ.get("REMINDER_BOT_DB_PATH")
    )
    if configured_path:
        return Path(configured_path).expanduser()
    return PROJECT_DIR / ".runtime" / "reminder_bot.sqlite3"


# ----------------------------
# Templates
# ----------------------------
def load_templates() -> dict:
    path = _first_existing(
        PROJECT_DIR / "reminder_bot_templates.json",
        PROJECT_DIR / "templates.json",
    )
    if not path:
        raise RuntimeError("templates.json не найден")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "general" not in data or "roles" not in data:
        raise RuntimeError("templates.json: ожидаются ключи 'general' и 'roles'")
    return data


# ----------------------------
# Schedule
# ----------------------------
def load_schedule() -> list[dict]:
    path = _first_existing(
        PROJECT_DIR / "reminder_bot_schedule_config.json",
        PROJECT_DIR / "schedule_config.json",
    )
    if not path:
        raise RuntimeError("schedule_config.json не найден")
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("schedule_config.json: пустой список 'jobs'")
    return jobs


def get_tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)
