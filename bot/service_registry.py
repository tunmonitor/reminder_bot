"""Persistent configuration for service access and participant contacts."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .secure_fs import open_private_directory
from .services import SERVICES


class ServiceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self, super_admin_id: int, assembly_group_chat_id: int) -> None:
        if super_admin_id <= 0:
            raise ValueError("super_admin_id должен быть положительным Telegram ID")
        if assembly_group_chat_id == 0:
            raise ValueError("ID группового чата не может быть равен нулю")

        directory_fd = open_private_directory(self.path.parent, create=True)
        os.close(directory_fd)
        self._ensure_private_database_file()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    assignment_layout TEXT NOT NULL
                        CHECK (assignment_layout IN ('pairs', 'single')),
                    group_chat_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (enabled IN (0, 1)),
                    reminder_days INTEGER NOT NULL DEFAULT 1
                        CHECK (reminder_days >= 0),
                    reminder_time TEXT NOT NULL DEFAULT '18:00',
                    group_template TEXT,
                    personal_templates_json TEXT
                );

                CREATE TABLE IF NOT EXISTS super_admins (
                    telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_admins (
                    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    created_by INTEGER NOT NULL CHECK (created_by > 0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, telegram_id)
                );

                CREATE TABLE IF NOT EXISTS responsible_contacts (
                    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    name TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    updated_by INTEGER NOT NULL CHECK (updated_by > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, task, name)
                );

                CREATE TABLE IF NOT EXISTS configuration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    action TEXT NOT NULL,
                    service_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            now = _utc_now()
            for service in SERVICES:
                is_assembly = service.id == "assembly"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO services (
                        id, name, aliases_json, assignment_layout,
                        group_chat_id, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        service.id,
                        service.name,
                        json.dumps(service.aliases, ensure_ascii=False),
                        service.assignment_layout,
                        assembly_group_chat_id if is_assembly else None,
                        int(is_assembly),
                    ),
                )
            has_super_admin = connection.execute(
                "SELECT 1 FROM super_admins LIMIT 1"
            ).fetchone()
            if has_super_admin is None:
                connection.execute(
                    "INSERT INTO super_admins (telegram_id, created_at) "
                    "VALUES (?, ?)",
                    (super_admin_id, now),
                )

    def _ensure_private_database_file(self) -> None:
        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            file_stat = self.path.lstat()
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise PermissionError(
                    f"База данных должна быть обычным файлом владельца "
                    f"с правами 600: {self.path}"
                )
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list_services(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, name, aliases_json, assignment_layout, "
                "group_chat_id, enabled, reminder_days, reminder_time, "
                "group_template, personal_templates_json "
                "FROM services ORDER BY id"
            ).fetchall()
        return [
            {
                **dict(row),
                "aliases": json.loads(row["aliases_json"]),
                "enabled": bool(row["enabled"]),
                "personal_templates": (
                    json.loads(row["personal_templates_json"])
                    if row["personal_templates_json"]
                    else {}
                ),
            }
            for row in rows
        ]

    def get_service(self, service_id: str) -> dict | None:
        return next(
            (
                service
                for service in self.list_services()
                if service["id"] == service_id
            ),
            None,
        )

    def is_super_admin(self, telegram_id: int) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM super_admins WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone() is not None

    def is_service_admin(self, telegram_id: int, service_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM service_admins "
                "WHERE telegram_id = ? AND service_id = ?",
                (telegram_id, service_id),
            ).fetchone() is not None

    def add_service_admin(
        self,
        actor_id: int,
        service_id: str,
        telegram_id: int,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "INSERT INTO service_admins "
                "(service_id, telegram_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(service_id, telegram_id) DO NOTHING",
                (service_id, telegram_id, actor_id, _utc_now()),
            )
            if cursor.rowcount:
                self._audit(
                    connection, actor_id, "service_admin_added", service_id,
                    {"telegram_id": telegram_id},
                )

    def remove_service_admin(
        self,
        actor_id: int,
        service_id: str,
        telegram_id: int,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "DELETE FROM service_admins "
                "WHERE service_id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            )
            if cursor.rowcount:
                self._audit(
                    connection, actor_id, "service_admin_removed", service_id,
                    {"telegram_id": telegram_id},
                )

    def set_group_chat(
        self,
        actor_id: int,
        service_id: str,
        group_chat_id: int,
        *,
        enabled: bool,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if group_chat_id == 0:
            raise ValueError("ID группового чата не может быть равен нулю")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            connection.execute(
                "UPDATE services SET group_chat_id = ?, enabled = ? WHERE id = ?",
                (group_chat_id, int(enabled), service_id),
            )
            self._audit(
                connection, actor_id, "service_chat_updated", service_id,
                {"group_chat_id": group_chat_id, "enabled": enabled},
            )

    def set_notification_settings(
        self,
        actor_id: int,
        service_id: str,
        *,
        reminder_days: int,
        reminder_time: str,
        group_template: str,
        personal_templates: dict[str, str],
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if isinstance(reminder_days, bool) or not isinstance(reminder_days, int):
            raise ValueError("reminder_days должен быть целым числом")
        if reminder_days < 0:
            raise ValueError("reminder_days не может быть отрицательным")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reminder_time):
            raise ValueError("reminder_time должен быть в формате HH:MM")
        if not isinstance(group_template, str) or not group_template.strip():
            raise ValueError("Текст группового уведомления обязателен")
        if not isinstance(personal_templates, dict) or any(
            not isinstance(task, str)
            or not isinstance(text, str)
            or not task.strip()
            or not text.strip()
            for task, text in personal_templates.items()
        ):
            raise ValueError("Шаблоны личных сообщений должны быть непустыми")

        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                """
                UPDATE services SET
                    reminder_days = ?,
                    reminder_time = ?,
                    group_template = ?,
                    personal_templates_json = ?
                WHERE id = ?
                """,
                (
                    reminder_days,
                    reminder_time,
                    group_template,
                    json.dumps(
                        personal_templates, ensure_ascii=False, sort_keys=True
                    ),
                    service_id,
                ),
            )
            self._audit(
                connection, actor_id, "service_notifications_updated", service_id,
                {
                    "reminder_days": reminder_days,
                    "reminder_time": reminder_time,
                    "group_template": group_template,
                    "personal_templates": personal_templates,
                },
            )

    def set_responsible_contact(
        self,
        actor_id: int,
        service_id: str,
        task: str,
        name: str,
        telegram_id: int,
        username: str = "",
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        task = task.strip()
        name = name.strip()
        if not task or not name:
            raise ValueError("Название задачи и имя ответственного обязательны")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            connection.execute(
                """
                INSERT INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_id, task, name) DO UPDATE SET
                    telegram_id = excluded.telegram_id,
                    username = excluded.username,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    service_id, task, name, telegram_id,
                    username.strip().lstrip("@"), actor_id, _utc_now(),
                ),
            )
            self._audit(
                connection, actor_id, "responsible_contact_updated", service_id,
                {"task": task, "name": name, "telegram_id": telegram_id},
            )

    def get_responsible_contact(
        self, service_id: str, task: str, name: str
    ) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT telegram_id, username FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND name = ?",
                (service_id, task.strip(), name.strip()),
            ).fetchone()
        return dict(row) if row else None

    def list_responsible_contacts(
        self, actor_id: int, service_id: str
    ) -> list[dict]:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT task, name, telegram_id, username "
                "FROM responsible_contacts WHERE service_id = ? "
                "ORDER BY task, name",
                (service_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_service_admins(self, service_id: str) -> list[int]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT telegram_id FROM service_admins "
                "WHERE service_id = ? ORDER BY telegram_id",
                (service_id,),
            ).fetchall()
        return [row["telegram_id"] for row in rows]

    @staticmethod
    def _require_super_admin(
        connection: sqlite3.Connection, actor_id: int
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM super_admins WHERE telegram_id = ?",
            (actor_id,),
        ).fetchone() is None:
            raise PermissionError("Операция доступна только супер-администратору")

    @staticmethod
    def _require_service(
        connection: sqlite3.Connection, service_id: str
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM services WHERE id = ?", (service_id,)
        ).fetchone() is None:
            raise ValueError(f"Неизвестное служение: {service_id}")

    @staticmethod
    def _require_service_editor(
        connection: sqlite3.Connection, actor_id: int, service_id: str
    ) -> None:
        is_super_admin = connection.execute(
            "SELECT 1 FROM super_admins WHERE telegram_id = ?",
            (actor_id,),
        ).fetchone()
        is_service_admin = connection.execute(
            "SELECT 1 FROM service_admins "
            "WHERE telegram_id = ? AND service_id = ?",
            (actor_id, service_id),
        ).fetchone()
        if is_super_admin is None and is_service_admin is None:
            raise PermissionError(
                "Настройки доступны супер-администратору и администратору служения"
            )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_id: int,
        action: str,
        service_id: str | None,
        details: dict,
    ) -> None:
        connection.execute(
            "INSERT INTO configuration_audit "
            "(actor_id, action, service_id, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                actor_id, action, service_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                _utc_now(),
            ),
        )


def _validate_positive_id(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} должен быть положительным целым числом")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
