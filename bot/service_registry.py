"""Persistent configuration for service access and participant contacts."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Callable

from .secure_fs import open_private_directory
from .services import SERVICES


GROUP_TEMPLATE_FIELDS = frozenset(
    {"service_name", "sunday_date", "task_assignments"}
)
PERSONAL_TEMPLATE_FIELDS = frozenset(
    {"name", "task", "service_name", "sunday_date", "telegram_id", "username"}
)


def validate_notification_template(
    template: str,
    allowed_fields: frozenset[str],
    label: str,
) -> None:
    allowed = ", ".join(sorted(f"{{{field}}}" for field in allowed_fields))
    try:
        for _, field_name, format_spec, conversion in Formatter().parse(template):
            if field_name is None:
                continue
            if field_name not in allowed_fields:
                raise ValueError(
                    f"{label}: поле {{{field_name}}} не поддерживается. "
                    f"Доступны: {allowed}."
                )
            if format_spec or conversion:
                raise ValueError(
                    f"{label}: для шаблонов разрешены только простые "
                    f"поля без форматирования. Доступны: {allowed}."
                )
    except ValueError as exc:
        if str(exc).startswith(f"{label}:"):
            raise
        raise ValueError(f"{label}: некорректный синтаксис шаблона.") from exc


class ServiceRegistry:
    def __init__(
        self, path: Path, on_change: Callable[[], None] | None = None
    ) -> None:
        self.path = path
        self.on_change = on_change

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
                    username TEXT NOT NULL DEFAULT '',
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
                    PRIMARY KEY (service_id, task, telegram_id)
                );

                CREATE TABLE IF NOT EXISTS configuration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    action TEXT NOT NULL,
                    service_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_counters (
                    name TEXT PRIMARY KEY,
                    next_value INTEGER NOT NULL
                        CHECK (next_value >= 0 AND next_value <= 2147483647)
                );

                CREATE TABLE IF NOT EXISTS service_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    reminder_days INTEGER NOT NULL CHECK (reminder_days >= 0),
                    reminder_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_reminder_deliveries (
                    service_id TEXT NOT NULL,
                    reminder_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (
                        service_id, reminder_id, event_date, recipient_key
                    )
                );

                CREATE TABLE IF NOT EXISTS service_reminder_migrations (
                    name TEXT PRIMARY KEY
                );
                """
            )
            reminder_indexes = connection.execute(
                "PRAGMA index_list(service_reminders)"
            ).fetchall()
            if any(row["unique"] for row in reminder_indexes):
                connection.executescript(
                    """
                    CREATE TABLE service_reminders_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        service_id TEXT NOT NULL REFERENCES services(id)
                            ON DELETE CASCADE,
                        reminder_days INTEGER NOT NULL
                            CHECK (reminder_days >= 0),
                        reminder_time TEXT NOT NULL
                    );
                    INSERT INTO service_reminders_v2 (
                        id, service_id, reminder_days, reminder_time
                    )
                    SELECT id, service_id, reminder_days, reminder_time
                    FROM service_reminders;
                    DROP TABLE service_reminders;
                    ALTER TABLE service_reminders_v2
                        RENAME TO service_reminders;
                    """
                )
            admin_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(service_admins)"
                )
            }
            if "username" not in admin_columns:
                connection.execute(
                    "ALTER TABLE service_admins "
                    "ADD COLUMN username TEXT NOT NULL DEFAULT ''"
                )
            contact_primary_key = [
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(responsible_contacts)"
                )
                if row["pk"]
            ]
            if contact_primary_key != ["service_id", "task", "telegram_id"]:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    DROP TABLE IF EXISTS responsible_contacts_v2;
                    CREATE TABLE responsible_contacts_v2 (
                        service_id TEXT NOT NULL
                            REFERENCES services(id) ON DELETE CASCADE,
                        task TEXT NOT NULL,
                        name TEXT NOT NULL,
                        telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                        username TEXT NOT NULL DEFAULT '',
                        updated_by INTEGER NOT NULL CHECK (updated_by > 0),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (service_id, task, telegram_id)
                    );
                    INSERT INTO responsible_contacts_v2 (
                        service_id, task, name, telegram_id, username,
                        updated_by, updated_at
                    )
                    SELECT service_id, task, name, telegram_id, username,
                           updated_by, updated_at
                    FROM responsible_contacts
                    WHERE rowid IN (
                        SELECT MAX(rowid)
                        FROM responsible_contacts
                        GROUP BY service_id, task, telegram_id
                    )
                    AND rowid IN (
                        SELECT MAX(rowid)
                        FROM responsible_contacts
                        GROUP BY service_id, task, name
                    );
                    DROP TABLE responsible_contacts;
                    ALTER TABLE responsible_contacts_v2
                    RENAME TO responsible_contacts;
                    COMMIT;
                    """
                )
            connection.execute(
                """
                DELETE FROM responsible_contacts
                WHERE rowid NOT IN (
                    SELECT MAX(rowid)
                    FROM responsible_contacts
                    GROUP BY service_id, task, name
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_responsible_contacts_name "
                "ON responsible_contacts(service_id, task, name)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_counters (name, next_value) "
                "VALUES ('telegram_share_request_id', ?)",
                (secrets.randbelow(2**30) + 2**30,),
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
            migrated_reminders = connection.execute(
                "SELECT 1 FROM service_reminder_migrations "
                "WHERE name = 'legacy-reminders'"
            ).fetchone()
            if migrated_reminders is None:
                connection.execute(
                    """
                    INSERT INTO service_reminders (
                        service_id, reminder_days, reminder_time
                    )
                    SELECT services.id, services.reminder_days,
                           services.reminder_time
                    FROM services
                    WHERE NOT EXISTS (
                        SELECT 1 FROM service_reminders
                        WHERE service_reminders.service_id = services.id
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO service_reminder_migrations (name) "
                    "VALUES ('legacy-reminders')"
                )

    def next_share_request_id(self) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_value FROM runtime_counters "
                "WHERE name = 'telegram_share_request_id'"
            ).fetchone()
            if row is None or row["next_value"] == 0:
                raise ValueError(
                    "Исчерпан счётчик запросов выбора Telegram."
                )
            request_id = row["next_value"]
            if request_id == 2**31 - 1:
                connection.execute(
                    "UPDATE runtime_counters SET next_value = 0 "
                    "WHERE name = 'telegram_share_request_id'"
                )
            else:
                connection.execute(
                    "UPDATE runtime_counters SET next_value = ? "
                    "WHERE name = 'telegram_share_request_id'",
                    (request_id + 1,),
                )
        return request_id

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
        changed = False
        try:
            with connection:
                yield connection
            changed = connection.total_changes > 0
        finally:
            connection.close()
        if changed and self.on_change is not None:
            self.on_change()

    def list_services(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, name, aliases_json, assignment_layout, "
                "group_chat_id, enabled, reminder_days, reminder_time, "
                "group_template, personal_templates_json "
                "FROM services ORDER BY id"
            ).fetchall()
            reminders = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders ORDER BY service_id, reminder_days, "
                "reminder_time, id"
            ).fetchall()
        reminders_by_service: dict[str, list[dict]] = {}
        for reminder in reminders:
            reminders_by_service.setdefault(reminder["service_id"], []).append(
                {
                    "id": reminder["id"],
                    "reminder_days": reminder["reminder_days"],
                    "reminder_time": reminder["reminder_time"],
                }
            )
        return [
            {
                **dict(row),
                "aliases": json.loads(row["aliases_json"]),
                "reminders": reminders_by_service.get(row["id"], []),
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
        username: str = "",
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        normalized_username = username.strip()
        if normalized_username.startswith("@"):
            normalized_username = normalized_username[1:]
        if normalized_username and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{4,31}", normalized_username
        ):
            raise ValueError("Некорректный Telegram username.")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            existing = connection.execute(
                "SELECT 1 FROM service_admins "
                "WHERE service_id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO service_admins "
                "(service_id, telegram_id, username, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(service_id, telegram_id) DO UPDATE SET "
                "username = CASE WHEN excluded.username != '' "
                "THEN excluded.username ELSE service_admins.username END",
                (
                    service_id, telegram_id, normalized_username,
                    actor_id, _utc_now(),
                ),
            )
            if existing is None:
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

    def set_service_enabled(
        self, actor_id: int, service_id: str, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть логическим значением")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            connection.execute(
                "UPDATE services SET enabled = ? WHERE id = ?",
                (int(enabled), service_id),
            )
            self._audit(
                connection,
                actor_id,
                "service_enabled_updated",
                service_id,
                {"enabled": enabled},
            )

    def set_notification_settings(
        self,
        actor_id: int,
        service_id: str,
        *,
        reminder_days: int,
        reminder_time: str,
        group_template: str | None,
        personal_templates: dict[str, str],
        reminders: list[dict] | None = None,
        allow_duplicate_reminders: bool = False,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if isinstance(reminder_days, bool) or not isinstance(reminder_days, int):
            raise ValueError("reminder_days должен быть целым числом")
        if reminder_days < 0:
            raise ValueError("reminder_days не может быть отрицательным")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reminder_time):
            raise ValueError("reminder_time должен быть в формате HH:MM")
        if group_template is not None and (
            not isinstance(group_template, str) or not group_template.strip()
        ):
            raise ValueError("Текст группового уведомления не может быть пустым")
        if group_template is not None:
            validate_notification_template(
                group_template,
                GROUP_TEMPLATE_FIELDS,
                "Шаблон группового уведомления",
            )
        if not isinstance(personal_templates, dict) or any(
            not isinstance(task, str)
            or not isinstance(text, str)
            or not task.strip()
            or not text.strip()
            for task, text in personal_templates.items()
        ):
            raise ValueError("Шаблоны личных сообщений должны быть непустыми")
        for task, template in personal_templates.items():
            validate_notification_template(
                template,
                PERSONAL_TEMPLATE_FIELDS,
                f"Личный шаблон для задачи «{task}»",
            )
        normalized_reminders = (
            self._validate_reminders(
                reminders,
                allow_duplicates=allow_duplicate_reminders,
            )
            if reminders is not None
            else None
        )

        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            if normalized_reminders is None:
                existing_reminders = connection.execute(
                    "SELECT id, reminder_days, reminder_time "
                    "FROM service_reminders WHERE service_id = ? "
                    "ORDER BY id",
                    (service_id,),
                ).fetchall()
                default_reminders = [
                    {
                        "id": row["id"],
                        "reminder_days": row["reminder_days"],
                        "reminder_time": row["reminder_time"],
                    }
                    for row in existing_reminders
                ]
                if default_reminders:
                    default_reminders[0] = {
                        "id": default_reminders[0]["id"],
                        "reminder_days": reminder_days,
                        "reminder_time": reminder_time,
                    }
                else:
                    default_reminders = [{
                        "reminder_days": reminder_days,
                        "reminder_time": reminder_time,
                    }]
                normalized_reminders = self._validate_reminders(
                    default_reminders,
                    allow_duplicates=allow_duplicate_reminders,
                )
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
            self._replace_service_reminders(
                connection, service_id, normalized_reminders
            )
            self._audit(
                connection, actor_id, "service_notifications_updated", service_id,
                {
                    "reminder_days": reminder_days,
                    "reminder_time": reminder_time,
                    "reminders": normalized_reminders,
                    "group_template": group_template,
                    "personal_templates": personal_templates,
                },
            )

    def is_service_reminder_delivered(
        self,
        service_id: str,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> bool:
        with self._connection() as connection:
            reminder_ids = self._equivalent_reminder_ids(
                connection, service_id, reminder_id
            )
            if not reminder_ids:
                reminder_ids = [reminder_id]
            placeholders = ",".join("?" for _ in reminder_ids)
            return connection.execute(
                f"""
                SELECT 1 FROM service_reminder_deliveries
                WHERE service_id = ? AND reminder_id IN ({placeholders})
                  AND event_date = ? AND recipient_key = ?
                """,
                (service_id, *reminder_ids, event_date, recipient_key),
            ).fetchone() is not None

    def mark_service_reminder_delivered(
        self,
        service_id: str,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> None:
        with self._connection() as connection:
            reminder_ids = self._equivalent_reminder_ids(
                connection, service_id, reminder_id
            ) or [reminder_id]
            connection.executemany(
                """
                INSERT OR IGNORE INTO service_reminder_deliveries (
                    service_id, reminder_id, event_date, recipient_key,
                    delivered_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        service_id,
                        duplicate_id,
                        event_date,
                        recipient_key,
                        _utc_now(),
                    )
                    for duplicate_id in reminder_ids
                ),
            )

    @staticmethod
    def _equivalent_reminder_ids(
        connection: sqlite3.Connection, service_id: str, reminder_id: int
    ) -> list[int]:
        reminder = connection.execute(
            "SELECT reminder_days, reminder_time FROM service_reminders "
            "WHERE service_id = ? AND id = ?",
            (service_id, reminder_id),
        ).fetchone()
        if reminder is None:
            return []
        return [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM service_reminders WHERE service_id = ? "
                "AND reminder_days = ? AND reminder_time = ?",
                (service_id, reminder["reminder_days"], reminder["reminder_time"]),
            )
        ]

    @staticmethod
    def _validate_reminders(
        reminders: list[dict], *, allow_duplicates: bool = False
    ) -> list[dict]:
        if not isinstance(reminders, list):
            raise ValueError("Настройки уведомлений имеют неверный формат.")
        normalized = []
        seen: set[tuple[int, str]] = set()
        for reminder in reminders:
            if not isinstance(reminder, dict):
                raise ValueError("Настройка уведомления имеет неверный формат.")
            days = reminder.get("reminder_days")
            reminder_time = reminder.get("reminder_time")
            if isinstance(days, bool) or not isinstance(days, int) or days < 0:
                raise ValueError("Число дней должно быть целым числом от 0 до 6.")
            if days > 6:
                raise ValueError("Число дней должно быть целым числом от 0 до 6.")
            if not isinstance(reminder_time, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", reminder_time
            ):
                raise ValueError("Время уведомления должно быть в формате HH:MM.")
            key = (days, reminder_time)
            if key in seen and not allow_duplicates:
                raise ValueError(
                    f"Уведомление за {days} дн. в {reminder_time} уже задано."
                )
            seen.add(key)
            reminder_id = reminder.get("id")
            if reminder_id is not None and (
                isinstance(reminder_id, bool)
                or not isinstance(reminder_id, int)
                or reminder_id <= 0
            ):
                raise ValueError("Идентификатор уведомления некорректен.")
            normalized.append(
                {
                    "id": reminder_id,
                    "reminder_days": days,
                    "reminder_time": reminder_time,
                }
            )
        return normalized

    @staticmethod
    def _replace_service_reminders(
        connection: sqlite3.Connection,
        service_id: str,
        reminders: list[dict],
    ) -> None:
        existing_rows = connection.execute(
            "SELECT id, reminder_days, reminder_time "
            "FROM service_reminders WHERE service_id = ?",
            (service_id,),
        ).fetchall()
        existing = {row["id"] for row in existing_rows}
        requested_ids = {
            reminder["id"]
            for reminder in reminders
            if reminder["id"] is not None
        }
        if requested_ids - existing:
            raise ValueError("Одно из уведомлений устарело; обновите настройки.")
        removed_ids = existing - requested_ids
        if removed_ids:
            requested_by_id = {
                reminder["id"]: reminder for reminder in reminders
                if reminder["id"] is not None
            }
            for row in existing_rows:
                if row["id"] not in removed_ids:
                    continue
                survivors = [
                    reminder for reminder in requested_by_id.values()
                    if reminder["reminder_days"] == row["reminder_days"]
                    and reminder["reminder_time"] == row["reminder_time"]
                ]
                for survivor in survivors:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO service_reminder_deliveries (
                            service_id, reminder_id, event_date,
                            recipient_key, delivered_at
                        )
                        SELECT service_id, ?, event_date, recipient_key,
                               delivered_at
                        FROM service_reminder_deliveries
                        WHERE service_id = ? AND reminder_id = ?
                        """,
                        (survivor["id"], service_id, row["id"]),
                    )
            placeholders = ",".join("?" for _ in removed_ids)
            connection.execute(
                "DELETE FROM service_reminder_deliveries "
                "WHERE service_id = ? AND reminder_id IN "
                f"({placeholders})",
                (service_id, *sorted(removed_ids)),
            )
            connection.execute(
                "DELETE FROM service_reminders WHERE service_id = ? "
                f"AND id IN ({placeholders})",
                (service_id, *sorted(removed_ids)),
            )
        for reminder in reminders:
            if reminder["id"] is None:
                connection.execute(
                    "INSERT INTO service_reminders "
                    "(service_id, reminder_days, reminder_time) VALUES (?, ?, ?)",
                    (
                        service_id,
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ),
                )
            else:
                connection.execute(
                    "UPDATE service_reminders "
                    "SET reminder_days = ?, reminder_time = ? "
                    "WHERE service_id = ? AND id = ?",
                    (
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                        service_id,
                        reminder["id"],
                    ),
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
                "DELETE FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? "
                "AND (name = ? OR telegram_id = ?)",
                (service_id, task, name, telegram_id),
            )
            connection.execute(
                """
                INSERT INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
                "WHERE service_id = ? AND task = ? AND name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
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

    def list_service_admin_details(self, service_id: str) -> list[dict]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT telegram_id, username FROM service_admins "
                "WHERE service_id = ? ORDER BY telegram_id",
                (service_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
