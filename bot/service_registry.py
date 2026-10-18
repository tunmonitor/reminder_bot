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
    {
        "service_name",
        "sunday_date",
        "task_assignments",
        "informational_notes",
    }
)
PERSONAL_TEMPLATE_FIELDS = frozenset(
    {"name", "task", "service_name", "sunday_date", "telegram_id", "username"}
)
DEFAULT_GROUP_TEMPLATE = (
    "Напоминание о служении «{service_name}» в воскресенье "
    "{sunday_date}:\n{task_assignments}"
)
_UNSET = object()


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

                CREATE TABLE IF NOT EXISTS user_notification_prefs (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    PRIMARY KEY (actor_id, service_id)
                );

                CREATE TABLE IF NOT EXISTS user_reminder_texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    body TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (actor_id, service_id, task)
                );

                CREATE TABLE IF NOT EXISTS user_reminder_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_id INTEGER NOT NULL REFERENCES user_reminder_texts(id)
                        ON DELETE CASCADE,
                    reminder_days INTEGER NOT NULL
                        CHECK (reminder_days BETWEEN 0 AND 6),
                    reminder_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS user_reminder_deliveries (
                    schedule_id INTEGER NOT NULL
                        REFERENCES user_reminder_schedules(id) ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (schedule_id, event_date)
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

                -- Deprecated: retained for migration and rollback compatibility.
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

                CREATE TABLE IF NOT EXISTS group_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    body TEXT,
                    reminder_days INTEGER NOT NULL
                        CHECK (reminder_days >= 0),
                    reminder_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_group_reminders_service
                    ON group_reminders(service_id);

                CREATE TABLE IF NOT EXISTS group_reminder_deliveries (
                    reminder_id INTEGER NOT NULL
                        REFERENCES group_reminders(id) ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (reminder_id, event_date, recipient_key)
                );

                CREATE TABLE IF NOT EXISTS group_reminder_migrations (
                    name TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS group_reminder_legacy_ids (
                    legacy_id INTEGER PRIMARY KEY,
                    reminder_id INTEGER NOT NULL
                        REFERENCES group_reminders(id) ON DELETE CASCADE
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
            preference_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(user_notification_prefs)"
                )
            }
            if "actor_id" not in preference_columns:
                enabled_column = (
                    "enabled"
                    if "enabled" in preference_columns
                    else (
                        "user_notifications_enabled"
                        if "user_notifications_enabled" in preference_columns
                        else "0"
                    )
                )
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    CREATE TABLE user_notification_prefs_v2 (
                        actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                        service_id TEXT NOT NULL REFERENCES services(id)
                            ON DELETE CASCADE,
                        enabled INTEGER NOT NULL DEFAULT 0
                            CHECK (enabled IN (0, 1)),
                        PRIMARY KEY (actor_id, service_id)
                    );
                    INSERT OR IGNORE INTO user_notification_prefs_v2 (
                        actor_id, service_id, enabled
                    )
                    SELECT telegram_id, service_id, {enabled_column}
                    FROM user_notification_prefs;
                    DROP TABLE user_notification_prefs;
                    ALTER TABLE user_notification_prefs_v2
                    RENAME TO user_notification_prefs;
                    """
                )
                preference_columns = {"actor_id", "service_id", "enabled"}
            if "enabled" not in preference_columns:
                connection.execute(
                    "ALTER TABLE user_notification_prefs "
                    "ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (enabled IN (0, 1))"
                )
                if "user_notifications_enabled" in preference_columns:
                    connection.execute(
                        "UPDATE user_notification_prefs "
                        "SET enabled = user_notifications_enabled"
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
                """
                INSERT OR IGNORE INTO user_notification_prefs (
                    actor_id, service_id, enabled
                )
                SELECT DISTINCT telegram_id, service_id, 0
                FROM responsible_contacts
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_urtexts_actor "
                "ON user_reminder_texts(actor_id, service_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_urschedules_text "
                "ON user_reminder_schedules(text_id)"
            )
            connection.execute(
                """
                DELETE FROM user_notification_prefs
                WHERE NOT EXISTS (
                    SELECT 1 FROM responsible_contacts
                    WHERE responsible_contacts.service_id =
                              user_notification_prefs.service_id
                      AND responsible_contacts.telegram_id =
                              user_notification_prefs.actor_id
                )
                """
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
            self._migrate_service_reminders(connection)

    @staticmethod
    def _migrate_service_reminders(
        connection: sqlite3.Connection,
    ) -> None:
        migration_name = "service-reminders-to-group-reminders"
        migrated = connection.execute(
            "SELECT 1 FROM group_reminder_migrations WHERE name = ?",
            (migration_name,),
        ).fetchone()
        if migrated is not None:
            return
        connection.execute("SAVEPOINT group_reminder_migration")
        try:
            rows = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders "
                "ORDER BY service_id, reminder_days, reminder_time, id"
            ).fetchall()
            deliveries = connection.execute(
                "SELECT service_id, reminder_id, event_date, recipient_key, "
                "delivered_at FROM service_reminder_deliveries"
            ).fetchall()
            migrated_ids: dict[int, int] = {}
            new_ids_by_schedule: dict[tuple[str, int, str], int] = {}
            for row in connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM group_reminders ORDER BY id"
            ):
                schedule_key = (
                    row["service_id"],
                    row["reminder_days"],
                    row["reminder_time"],
                )
                new_ids_by_schedule.setdefault(schedule_key, row["id"])
            for row in rows:
                schedule_key = (
                    row["service_id"],
                    row["reminder_days"],
                    row["reminder_time"],
                )
                new_id = new_ids_by_schedule.get(schedule_key)
                if new_id is None:
                    cursor = connection.execute(
                        "INSERT INTO group_reminders "
                        "(service_id, body, reminder_days, reminder_time, "
                        "enabled) VALUES (?, NULL, ?, ?, 1)",
                        schedule_key,
                    )
                    new_id = cursor.lastrowid
                    new_ids_by_schedule[schedule_key] = new_id
                migrated_ids[row["id"]] = new_id
                connection.execute(
                    "INSERT OR IGNORE INTO group_reminder_legacy_ids "
                    "(legacy_id, reminder_id) VALUES (?, ?)",
                    (row["id"], new_id),
                )
            for delivery in deliveries:
                new_id = migrated_ids.get(delivery["reminder_id"])
                if new_id is None:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO group_reminder_deliveries "
                    "(reminder_id, event_date, recipient_key, delivered_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        new_id,
                        delivery["event_date"],
                        delivery["recipient_key"],
                        delivery["delivered_at"],
                    ),
                )
            connection.execute(
                "INSERT OR IGNORE INTO group_reminder_migrations (name) "
                "VALUES (?)",
                (migration_name,),
            )
            connection.execute("RELEASE SAVEPOINT group_reminder_migration")
        except Exception:
            connection.execute(
                "ROLLBACK TO SAVEPOINT group_reminder_migration"
            )
            connection.execute("RELEASE SAVEPOINT group_reminder_migration")
            raise

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
                "SELECT id, service_id, body, reminder_days, reminder_time, "
                "enabled, created_at FROM group_reminders "
                "ORDER BY service_id, reminder_days, reminder_time, id"
            ).fetchall()
        reminders_by_service: dict[str, list[dict]] = {}
        for reminder in reminders:
            reminders_by_service.setdefault(reminder["service_id"], []).append(
                {
                    "id": reminder["id"],
                    "service_id": reminder["service_id"],
                    "body": reminder["body"],
                    "reminder_days": reminder["reminder_days"],
                    "reminder_time": reminder["reminder_time"],
                    "enabled": bool(reminder["enabled"]),
                    "created_at": reminder["created_at"],
                }
            )
        services = []
        for row in rows:
            group_reminders = reminders_by_service.get(row["id"], [])
            services.append({
                **dict(row),
                "aliases": json.loads(row["aliases_json"]),
                "group_reminders": group_reminders,
                # Compatibility alias; both keys use the canonical table.
                "reminders": group_reminders,
                "enabled": bool(row["enabled"]),
                "personal_templates": (
                    json.loads(row["personal_templates_json"])
                    if row["personal_templates_json"]
                    else {}
                ),
            })
        return services

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

    def list_group_reminders(self, service_id: str) -> list[dict]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT id, service_id, body, reminder_days, reminder_time, "
                "enabled, created_at FROM group_reminders "
                "WHERE service_id = ? "
                "ORDER BY reminder_days, reminder_time, id",
                (service_id,),
            ).fetchall()
        return [
            {**dict(row), "enabled": bool(row["enabled"])}
            for row in rows
        ]

    def list_legacy_group_reminders(self, service_id: str) -> list[dict]:
        """Deprecated fallback used only if the migrated table is empty."""
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders WHERE service_id = ? "
                "ORDER BY reminder_days, reminder_time, id",
                (service_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "body": None,
                "enabled": True,
                "created_at": "",
                "legacy": True,
            }
            for row in rows
        ]

    def get_group_reminder(self, reminder_id: int) -> dict | None:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, service_id, body, reminder_days, reminder_time, "
                "enabled, created_at FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
        return (
            {**dict(row), "enabled": bool(row["enabled"])}
            if row is not None
            else None
        )

    def get_legacy_group_reminder(self, reminder_id: int) -> dict | None:
        """Deprecated fallback lookup for legacy scheduler jobs."""
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
        return (
            {
                **dict(row),
                "body": None,
                "enabled": True,
                "created_at": "",
                "legacy": True,
            }
            if row is not None
            else None
        )

    def add_group_reminder(
        self,
        actor_id: int,
        service_id: str,
        days: int,
        time: str,
        body: str | None = None,
        *,
        allow_duplicate: bool = False,
    ) -> int:
        _validate_positive_id(actor_id, "actor_id")
        self._validate_group_reminder_values(days, time, body)
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            self._ensure_group_schedule_unique(
                connection, service_id, days, time, allow_duplicate
            )
            cursor = connection.execute(
                "INSERT INTO group_reminders "
                "(service_id, body, reminder_days, reminder_time) "
                "VALUES (?, ?, ?, ?)",
                (service_id, body.strip() if body else None, days, time),
            )
            reminder_id = cursor.lastrowid
            self._audit(
                connection,
                actor_id,
                "group_reminder_added",
                service_id,
                {
                    "reminder_id": reminder_id,
                    "reminder_days": days,
                    "reminder_time": time,
                    "has_custom_body": body is not None,
                },
            )
        return reminder_id

    def update_group_reminder(
        self,
        actor_id: int,
        reminder_id: int,
        days: int | None = None,
        time: str | None = None,
        body=_UNSET,
        enabled: bool | None = None,
        *,
        allow_duplicate: bool = False,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(
                connection, actor_id, row["service_id"]
            )
            new_days = row["reminder_days"] if days is None else days
            new_time = row["reminder_time"] if time is None else time
            new_body = row["body"] if body is _UNSET else body
            new_enabled = bool(row["enabled"]) if enabled is None else enabled
            self._validate_group_reminder_values(
                new_days, new_time, new_body
            )
            if not isinstance(new_enabled, bool):
                raise ValueError("enabled должен быть логическим значением")
            self._ensure_group_schedule_unique(
                connection,
                row["service_id"],
                new_days,
                new_time,
                allow_duplicate,
                excluding_id=reminder_id,
            )
            connection.execute(
                "UPDATE group_reminders SET body = ?, reminder_days = ?, "
                "reminder_time = ?, enabled = ? WHERE id = ?",
                (
                    new_body.strip() if new_body else None,
                    new_days,
                    new_time,
                    int(new_enabled),
                    reminder_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                "group_reminder_updated",
                row["service_id"],
                {
                    "reminder_id": reminder_id,
                    "reminder_days": new_days,
                    "reminder_time": new_time,
                    "has_custom_body": new_body is not None,
                    "enabled": new_enabled,
                },
            )

    def delete_group_reminder(self, actor_id: int, reminder_id: int) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT service_id FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(
                connection, actor_id, row["service_id"]
            )
            connection.execute(
                "DELETE FROM group_reminders WHERE id = ?", (reminder_id,)
            )
            self._audit(
                connection,
                actor_id,
                "group_reminder_deleted",
                row["service_id"],
                {"reminder_id": reminder_id},
            )

    def resolve_group_text(self, reminder: dict, service: dict) -> str:
        if reminder.get("body") is not None:
            return reminder["body"]
        return service.get("group_template") or DEFAULT_GROUP_TEMPLATE

    def set_service_default_template(
        self, actor_id: int, service_id: str, template: str | None
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if template is not None:
            if not isinstance(template, str) or not template.strip():
                raise ValueError("Текст группового шаблона не может быть пустым")
            validate_notification_template(
                template,
                GROUP_TEMPLATE_FIELDS,
                "Шаблон группового уведомления",
            )
            template = template.strip()
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET group_template = ? WHERE id = ?",
                (template, service_id),
            )
            self._audit(
                connection,
                actor_id,
                "service_default_template_updated",
                service_id,
                {"is_custom": template is not None},
            )

    def set_service_personal_templates(
        self, actor_id: int, service_id: str, templates: dict[str, str]
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        self._validate_personal_templates(templates)
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET personal_templates_json = ? WHERE id = ?",
                (
                    json.dumps(templates, ensure_ascii=False, sort_keys=True),
                    service_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                "service_personal_templates_updated",
                service_id,
                {"templates": templates},
            )

    @staticmethod
    def _validate_group_reminder_values(
        days: int, time: str, body: str | None
    ) -> None:
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 6:
            raise ValueError("Число дней должно быть от 0 до 6.")
        if not isinstance(time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", time
        ):
            raise ValueError("Время должно быть в формате HH:MM.")
        if body is not None:
            if not isinstance(body, str) or not body.strip():
                raise ValueError("Текст группового напоминания не может быть пустым.")
            validate_notification_template(
                body,
                GROUP_TEMPLATE_FIELDS,
                "Текст группового напоминания",
            )

    @staticmethod
    def _ensure_group_schedule_unique(
        connection: sqlite3.Connection,
        service_id: str,
        days: int,
        time: str,
        allow_duplicate: bool,
        *,
        excluding_id: int | None = None,
    ) -> None:
        query = (
            "SELECT 1 FROM group_reminders WHERE service_id = ? "
            "AND reminder_days = ? AND reminder_time = ?"
        )
        parameters: tuple = (service_id, days, time)
        if excluding_id is not None:
            query += " AND id != ?"
            parameters += (excluding_id,)
        if not allow_duplicate and connection.execute(
            query, parameters
        ).fetchone():
            raise ValueError(
                f"Напоминание за {days} дн. в {time} уже задано."
            )

    @staticmethod
    def _validate_personal_templates(
        templates: dict[str, str],
    ) -> None:
        if not isinstance(templates, dict) or any(
            not isinstance(task, str)
            or not isinstance(text, str)
            or not task.strip()
            or not text.strip()
            for task, text in templates.items()
        ):
            raise ValueError("Шаблоны личных сообщений должны быть непустыми")
        for task, template in templates.items():
            validate_notification_template(
                template,
                PERSONAL_TEMPLATE_FIELDS,
                f"Личный шаблон для задачи «{task}»",
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
        """Deprecated compatibility wrapper; use the dedicated setters."""
        _validate_positive_id(actor_id, "actor_id")
        self._validate_group_reminder_values(
            reminder_days, reminder_time, None
        )
        if group_template is not None:
            if not isinstance(group_template, str) or not group_template.strip():
                raise ValueError("Текст группового уведомления не может быть пустым")
            validate_notification_template(
                group_template,
                GROUP_TEMPLATE_FIELDS,
                "Шаблон группового уведомления",
            )
        self._validate_personal_templates(personal_templates)
        normalized_reminders = (
            self._validate_reminders(
                reminders,
                allow_duplicates=allow_duplicate_reminders,
            )
            if reminders is not None
            else None
        )
        normalized_group_template = (
            group_template.strip() if group_template is not None else None
        )
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET reminder_days = ?, reminder_time = ?, "
                "group_template = ?, personal_templates_json = ? WHERE id = ?",
                (
                    reminder_days,
                    reminder_time,
                    normalized_group_template,
                    json.dumps(
                        personal_templates,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    service_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                "service_default_template_updated",
                service_id,
                {"is_custom": normalized_group_template is not None},
            )
            self._audit(
                connection,
                actor_id,
                "service_personal_templates_updated",
                service_id,
                {"templates": personal_templates},
            )
            if normalized_reminders is None:
                primary = connection.execute(
                    "SELECT id FROM group_reminders WHERE service_id = ? "
                    "ORDER BY id LIMIT 1",
                    (service_id,),
                ).fetchone()
                if primary is None:
                    connection.execute(
                        "INSERT INTO group_reminders "
                        "(service_id, body, reminder_days, reminder_time) "
                        "VALUES (?, NULL, ?, ?)",
                        (service_id, reminder_days, reminder_time),
                    )
                else:
                    self._ensure_group_schedule_unique(
                        connection,
                        service_id,
                        reminder_days,
                        reminder_time,
                        allow_duplicate_reminders,
                        excluding_id=primary["id"],
                    )
                    connection.execute(
                        "UPDATE group_reminders SET reminder_days = ?, "
                        "reminder_time = ? WHERE id = ?",
                        (reminder_days, reminder_time, primary["id"]),
                    )
            else:
                self._replace_group_reminders(
                    connection,
                    service_id,
                    normalized_reminders,
                    allow_duplicate=allow_duplicate_reminders,
                )

    def _replace_group_reminders(
        self,
        connection: sqlite3.Connection,
        service_id: str,
        reminders: list[dict],
        *,
        allow_duplicate: bool,
    ) -> None:
        existing_rows = connection.execute(
            "SELECT id, reminder_days, reminder_time FROM group_reminders "
            "WHERE service_id = ?",
            (service_id,),
        ).fetchall()
        existing = {row["id"] for row in existing_rows}
        final_ids = {
            item["id"] for item in reminders if item["id"] is not None
        }
        if final_ids - existing:
            raise ValueError("Одно из уведомлений устарело; обновите настройки.")
        if not allow_duplicate:
            seen = set()
            for item in reminders:
                key = (item["reminder_days"], item["reminder_time"])
                if key in seen:
                    raise ValueError(
                        f"Уведомление за {key[0]} дн. в {key[1]} уже задано."
                    )
                seen.add(key)
        removed = existing - final_ids
        if removed:
            placeholders = ",".join("?" for _ in removed)
            connection.execute(
                "DELETE FROM group_reminders WHERE service_id = ? "
                f"AND id IN ({placeholders})",
                (service_id, *sorted(removed)),
            )
        for reminder in reminders:
            if reminder["id"] is None:
                connection.execute(
                    "INSERT INTO group_reminders "
                    "(service_id, body, reminder_days, reminder_time) "
                    "VALUES (?, NULL, ?, ?)",
                    (
                        service_id,
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ),
                )
                continue
            connection.execute(
                "UPDATE group_reminders SET reminder_days = ?, "
                "reminder_time = ? WHERE id = ?",
                (
                    reminder["reminder_days"],
                    reminder["reminder_time"],
                    reminder["id"],
                ),
            )

    def is_group_reminder_delivered(
        self,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> bool:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM group_reminder_deliveries "
                "WHERE reminder_id = ? AND event_date = ? "
                "AND recipient_key = ?",
                (reminder_id, event_date, recipient_key),
            ).fetchone() is not None

    def mark_group_reminder_delivered(
        self,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> None:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone() is None:
                raise ValueError("Групповое напоминание не найдено.")
            connection.execute(
                "INSERT OR IGNORE INTO group_reminder_deliveries "
                "(reminder_id, event_date, recipient_key, delivered_at) "
                "VALUES (?, ?, ?, ?)",
                (reminder_id, event_date, recipient_key, _utc_now()),
            )

    # Deprecated aliases retained for older integrations.
    def is_service_reminder_delivered(
        self,
        service_id: str,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> bool:
        reminder = self._resolve_group_reminder_id(service_id, reminder_id)
        return bool(
            reminder
            and self.is_group_reminder_delivered(
                reminder["id"], event_date, recipient_key
            )
        )

    def mark_service_reminder_delivered(
        self,
        service_id: str,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> None:
        reminder = self._resolve_group_reminder_id(service_id, reminder_id)
        if reminder is None:
            raise ValueError("Групповое напоминание не найдено.")
        self.mark_group_reminder_delivered(
            reminder["id"], event_date, recipient_key
        )

    def _resolve_group_reminder_id(
        self, service_id: str, reminder_id: int
    ) -> dict | None:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            legacy_mapping = connection.execute(
                "SELECT reminders.id, reminders.service_id, reminders.body, "
                "reminders.reminder_days, reminders.reminder_time, "
                "reminders.enabled, reminders.created_at "
                "FROM group_reminder_legacy_ids AS legacy "
                "JOIN group_reminders AS reminders "
                "ON reminders.id = legacy.reminder_id "
                "WHERE legacy.legacy_id = ? AND reminders.service_id = ?",
                (reminder_id, service_id),
            ).fetchone()
            if legacy_mapping is not None:
                return {
                    **dict(legacy_mapping),
                    "enabled": bool(legacy_mapping["enabled"]),
                }
            current = connection.execute(
                "SELECT id, service_id, body, reminder_days, reminder_time, "
                "enabled, created_at FROM group_reminders "
                "WHERE id = ? AND service_id = ?",
                (reminder_id, service_id),
            ).fetchone()
        return (
            {**dict(current), "enabled": bool(current["enabled"])}
            if current is not None
            else None
        )

    @staticmethod
    def _validate_reminders(
        reminders: list[dict], *, allow_duplicates: bool = False
    ) -> list[dict]:
        if not isinstance(reminders, list):
            raise ValueError("Настройки уведомлений имеют неверный формат.")
        normalized = []
        seen: set[tuple[int, str]] = set()
        seen_ids: set[int] = set()
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
            if reminder_id is not None and reminder_id in seen_ids:
                raise ValueError("Идентификатор уведомления указан повторно.")
            if reminder_id is not None:
                seen_ids.add(reminder_id)
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
        """Deprecated compatibility helper operating on the new table."""
        existing_rows = connection.execute(
            "SELECT id, reminder_days, reminder_time "
            "FROM group_reminders WHERE service_id = ?",
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
        final_reminders = []
        for reminder in reminders:
            if reminder["id"] is None:
                cursor = connection.execute(
                    "INSERT INTO group_reminders "
                    "(service_id, body, reminder_days, reminder_time) "
                    "VALUES (?, NULL, ?, ?)",
                    (
                        service_id,
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ),
                )
                final_reminders.append({**reminder, "id": cursor.lastrowid})
            else:
                final_reminders.append(reminder)

        final_by_id = {
            reminder["id"]: reminder for reminder in final_reminders
        }
        final_ids = set(final_by_id)
        existing_by_id = {row["id"]: row for row in existing_rows}
        changed_ids = set()
        for existing_row in existing_rows:
            reminder_id = existing_row["id"]
            final = final_by_id.get(reminder_id)
            old_schedule = (
                existing_row["reminder_days"],
                existing_row["reminder_time"],
            )
            unchanged = final is not None and (
                final["reminder_days"], final["reminder_time"]
            ) == old_schedule
            if unchanged:
                continue

            changed_ids.add(reminder_id)

        if changed_ids:
            placeholders = ",".join("?" for _ in changed_ids)
            original_deliveries = connection.execute(
                "SELECT reminder_id, event_date, recipient_key, delivered_at "
                "FROM group_reminder_deliveries "
                "WHERE reminder_id IN "
                f"({placeholders})",
                tuple(sorted(changed_ids)),
            ).fetchall()
            deliveries_by_source = {}
            for delivery in original_deliveries:
                deliveries_by_source.setdefault(
                    delivery["reminder_id"], []
                ).append(delivery)

            # Remove all changing histories before copying the original rows,
            # so a two-way schedule swap cannot consume rows copied earlier.
            connection.execute(
                "DELETE FROM group_reminder_deliveries "
                "WHERE reminder_id IN "
                f"({placeholders})",
                tuple(sorted(changed_ids)),
            )
            for reminder_id in changed_ids:
                old_row = existing_by_id[reminder_id]
                old_schedule = (
                    old_row["reminder_days"],
                    old_row["reminder_time"],
                )
                survivors = [
                    reminder
                    for reminder in final_reminders
                    if (
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ) == old_schedule
                ]
                for survivor in survivors:
                    for delivery in deliveries_by_source.get(reminder_id, ()):
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO group_reminder_deliveries (
                                reminder_id, event_date, recipient_key,
                                delivered_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                survivor["id"],
                                delivery["event_date"],
                                delivery["recipient_key"],
                                delivery["delivered_at"],
                            ),
                        )

        for reminder in final_reminders:
            existing_row = existing_by_id.get(reminder["id"])
            if existing_row is None:
                continue
            if (
                existing_row["reminder_days"] != reminder["reminder_days"]
                or existing_row["reminder_time"] != reminder["reminder_time"]
            ):
                connection.execute(
                    "UPDATE group_reminders "
                    "SET reminder_days = ?, reminder_time = ? "
                    "WHERE service_id = ? AND id = ?",
                    (
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                        service_id,
                        reminder["id"],
                    ),
                )
        removed_ids = existing - final_ids
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            connection.execute(
                "DELETE FROM group_reminders WHERE service_id = ? "
                f"AND id IN ({placeholders})",
                (service_id, *sorted(removed_ids)),
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
            connection.execute(
                """
                DELETE FROM user_notification_prefs
                WHERE service_id = ? AND NOT EXISTS (
                    SELECT 1 FROM responsible_contacts
                    WHERE responsible_contacts.service_id = ?
                      AND responsible_contacts.telegram_id =
                              user_notification_prefs.actor_id
                )
                """,
                (service_id, service_id),
            )
            connection.execute(
                """
                DELETE FROM user_reminder_texts
                WHERE service_id = ? AND NOT EXISTS (
                    SELECT 1 FROM responsible_contacts
                    WHERE responsible_contacts.service_id =
                              user_reminder_texts.service_id
                      AND responsible_contacts.task = user_reminder_texts.task
                      AND responsible_contacts.telegram_id =
                              user_reminder_texts.actor_id
                )
                """,
                (service_id,),
            )
            self._audit(
                connection, actor_id, "responsible_contact_updated", service_id,
                {"task": task, "name": name, "telegram_id": telegram_id},
            )

    def get_user_notifications_enabled(
        self, actor_id: int, service_id: str
    ) -> bool:
        return self.is_user_notifications_enabled(actor_id, service_id)

    def is_user_notifications_enabled(
        self, actor_id: int, service_id: str
    ) -> bool:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            row = connection.execute(
                "SELECT enabled "
                "FROM user_notification_prefs "
                "WHERE service_id = ? AND actor_id = ?",
                (service_id, actor_id),
            ).fetchone()
        return bool(row["enabled"]) if row else False

    def set_user_notifications_enabled(
        self, actor_id: int, service_id: str, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть bool")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            registered = connection.execute(
                "SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND telegram_id = ? LIMIT 1",
                (service_id, actor_id),
            ).fetchone()
            if registered is None:
                raise ValueError(
                    "Пользователь не привязан к задачам этого служения"
                )
            current = connection.execute(
                "SELECT enabled "
                "FROM user_notification_prefs "
                "WHERE service_id = ? AND actor_id = ?",
                (service_id, actor_id),
            ).fetchone()
            if current is not None and bool(
                current["enabled"]
            ) == enabled:
                return
            if current is None and not enabled:
                return
            connection.execute(
                """
                INSERT INTO user_notification_prefs (
                    service_id, actor_id, enabled
                ) VALUES (?, ?, ?)
                ON CONFLICT(actor_id, service_id) DO UPDATE SET
                    enabled = excluded.enabled
                """,
                (service_id, actor_id, int(enabled)),
            )
            self._audit(
                connection,
                actor_id,
                "user_notification_preference_updated",
                service_id,
                {"enabled": enabled},
            )

    def set_user_notifications_all(
        self, actor_id: int, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть bool")
        with self._connection() as connection:
            services = connection.execute(
                "SELECT DISTINCT service_id FROM responsible_contacts "
                "WHERE telegram_id = ? ORDER BY service_id",
                (actor_id,),
            ).fetchall()
            for row in services:
                current = connection.execute(
                    "SELECT enabled FROM user_notification_prefs "
                    "WHERE service_id = ? AND actor_id = ?",
                    (row["service_id"], actor_id),
                ).fetchone()
                if current is not None and bool(current["enabled"]) == enabled:
                    continue
                connection.execute(
                    """
                    INSERT INTO user_notification_prefs (
                        service_id, actor_id, enabled
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(actor_id, service_id) DO UPDATE SET
                        enabled = excluded.enabled
                    WHERE enabled != excluded.enabled
                    """,
                    (row["service_id"], actor_id, int(enabled)),
                )
                self._audit(
                    connection,
                    actor_id,
                    "user_notification_preference_updated",
                    row["service_id"],
                    {"enabled": enabled, "bulk": True},
                )

    def list_users_with_notifications_enabled(
        self, service_id: str
    ) -> list[int]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                """
                SELECT DISTINCT preferences.actor_id
                FROM user_notification_prefs AS preferences
                JOIN responsible_contacts AS contacts
                  ON contacts.service_id = preferences.service_id
                 AND contacts.telegram_id = preferences.actor_id
                WHERE preferences.service_id = ?
                  AND preferences.enabled = 1
                ORDER BY preferences.actor_id
                """,
                (service_id,),
            ).fetchall()
        return [row["actor_id"] for row in rows]

    def list_user_services(self, actor_id: int) -> list[str]:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT service_id FROM responsible_contacts "
                "WHERE telegram_id = ? ORDER BY service_id",
                (actor_id,),
            ).fetchall()
        return [row["service_id"] for row in rows]

    def list_user_tasks(self, actor_id: int, service_id: str) -> list[str]:
        """Return only tasks to which this user is currently assigned."""
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT DISTINCT task FROM responsible_contacts "
                "WHERE telegram_id = ? AND service_id = ? ORDER BY task",
                (actor_id, service_id),
            ).fetchall()
        return [row["task"] for row in rows]

    def get_user_notifications_state(self, actor_id: int) -> dict:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT services.id AS service_id,
                       COALESCE(preferences.enabled, 0) AS enabled,
                       COUNT(DISTINCT texts.id) AS texts_count,
                       COUNT(DISTINCT schedules.id) AS schedules_count
                FROM responsible_contacts AS contacts
                JOIN services ON services.id = contacts.service_id
                LEFT JOIN user_notification_prefs AS preferences
                  ON preferences.service_id = contacts.service_id
                 AND preferences.actor_id = contacts.telegram_id
                LEFT JOIN user_reminder_texts AS texts
                  ON texts.actor_id = contacts.telegram_id
                 AND texts.service_id = contacts.service_id
                LEFT JOIN user_reminder_schedules AS schedules
                  ON schedules.text_id = texts.id
                WHERE contacts.telegram_id = ?
                GROUP BY services.id, preferences.enabled
                ORDER BY services.id
                """,
                (actor_id,),
            ).fetchall()
        per_service = [
            {
                "service_id": row["service_id"],
                "enabled": bool(row["enabled"]),
                "texts_count": row["texts_count"],
                "schedules_count": row["schedules_count"],
            }
            for row in rows
        ]
        enabled_count = sum(item["enabled"] for item in per_service)
        return {
            "total_services": len(per_service),
            "enabled_count": enabled_count,
            "disabled_count": len(per_service) - enabled_count,
            "per_service": per_service,
        }

    def list_user_texts(self, actor_id: int, service_id: str) -> list[dict]:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT id, actor_id, service_id, task, body, created_at "
                "FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? ORDER BY task",
                (actor_id, service_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> dict | None:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            row = connection.execute(
                "SELECT id, actor_id, service_id, task, body, created_at "
                "FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task.strip()),
            ).fetchone()
        return dict(row) if row else None

    def upsert_user_text(
        self, actor_id: int, service_id: str, task: str, body: str | None
    ) -> int:
        _validate_positive_id(actor_id, "actor_id")
        task = task.strip()
        if not task:
            raise ValueError("Название задачи обязательно")
        if body is not None:
            if not isinstance(body, str) or not body.strip():
                raise ValueError("Текст уведомления не может быть пустым")
            validate_notification_template(
                body, PERSONAL_TEMPLATE_FIELDS, "Личный шаблон пользователя"
            )
        with self._connection() as connection:
            self._require_service(connection, service_id)
            registered = connection.execute(
                "SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                (service_id, task, actor_id),
            ).fetchone()
            if registered is None:
                raise ValueError(
                    "Пользователь не привязан к этой задаче"
                )
            connection.execute(
                """
                INSERT INTO user_reminder_texts (
                    actor_id, service_id, task, body
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id, service_id, task) DO UPDATE SET
                    body = excluded.body
                """,
                (actor_id, service_id, task, body),
            )
            row = connection.execute(
                "SELECT id FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task),
            ).fetchone()
            text_id = row["id"]
            self._audit(
                connection,
                actor_id,
                "user_reminder_text_updated",
                service_id,
                {"task": task, "body_overridden": body is not None},
            )
        return text_id

    def reset_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        task = task.strip()
        if not task:
            raise ValueError("Название задачи обязательно")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "UPDATE user_reminder_texts SET body = NULL "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task),
            )
            if cursor.rowcount == 0:
                raise ValueError("Личный текст не найден")
            self._audit(
                connection,
                actor_id,
                "user_reminder_text_updated",
                service_id,
                {"task": task, "body_overridden": False},
            )

    def ensure_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> int:
        _validate_positive_id(actor_id, "actor_id")
        task = task.strip()
        if not task:
            raise ValueError("Название задачи обязательно")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            registered = connection.execute(
                "SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                (service_id, task, actor_id),
            ).fetchone()
            if registered is None:
                raise ValueError("Пользователь не привязан к этой задаче")
            connection.execute(
                """
                INSERT INTO user_reminder_texts (
                    actor_id, service_id, task, body
                ) VALUES (?, ?, ?, NULL)
                ON CONFLICT(actor_id, service_id, task) DO NOTHING
                """,
                (actor_id, service_id, task),
            )
            row = connection.execute(
                "SELECT id FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task),
            ).fetchone()
        return row["id"]

    def delete_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            connection.execute(
                "DELETE FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task.strip()),
            )

    def list_schedules(self, text_id: int) -> list[dict]:
        _validate_positive_id(text_id, "text_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, text_id, reminder_days, reminder_time, enabled "
                "FROM user_reminder_schedules WHERE text_id = ? ORDER BY id",
                (text_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_schedule(self, text_id: int, days: int, time: str) -> dict:
        _validate_positive_id(text_id, "text_id")
        self._validate_user_schedule(days, time)
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM user_reminder_texts WHERE id = ?",
                (text_id,),
            ).fetchone()
            if exists is None:
                raise ValueError("Текст личного уведомления не найден")
            cursor = connection.execute(
                "INSERT INTO user_reminder_schedules "
                "(text_id, reminder_days, reminder_time) VALUES (?, ?, ?)",
                (text_id, days, time),
            )
            schedule_id = cursor.lastrowid
            row = connection.execute(
                "SELECT id, text_id, reminder_days, reminder_time, enabled "
                "FROM user_reminder_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return dict(row)

    def update_schedule(
        self,
        schedule_id: int,
        days: int | None = None,
        time: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT reminder_days, reminder_time, enabled "
                "FROM user_reminder_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Правило личного расписания не найдено")
            new_days = row["reminder_days"] if days is None else days
            new_time = row["reminder_time"] if time is None else time
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError("enabled должен быть bool")
            new_enabled = (
                bool(row["enabled"]) if enabled is None else enabled
            )
            self._validate_user_schedule(new_days, new_time)
            if (
                new_days != row["reminder_days"]
                or new_time != row["reminder_time"]
            ):
                connection.execute(
                    "DELETE FROM user_reminder_deliveries "
                    "WHERE schedule_id = ?",
                    (schedule_id,),
                )
            connection.execute(
                "UPDATE user_reminder_schedules "
                "SET reminder_days = ?, reminder_time = ?, enabled = ? "
                "WHERE id = ?",
                (new_days, new_time, int(new_enabled), schedule_id),
            )

    def delete_schedule(self, schedule_id: int) -> None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM user_reminder_schedules WHERE id = ?",
                (schedule_id,),
            )

    @staticmethod
    def _validate_user_schedule(days: int, time: str) -> None:
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 6:
            raise ValueError("Дни до события должны быть от 0 до 6")
        if not isinstance(time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", time
        ):
            raise ValueError("Время должно быть в формате HH:MM")

    def list_enabled_user_reminder_schedules(
        self, actor_id: int | None = None
    ) -> list[dict]:
        if actor_id is not None:
            _validate_positive_id(actor_id, "actor_id")
        query = """
            SELECT schedules.id AS schedule_id, schedules.text_id,
                   schedules.reminder_days, schedules.reminder_time,
                   texts.actor_id, texts.service_id, texts.task, texts.body,
                   services.name AS service_name,
                   services.enabled AS service_enabled,
                   preferences.enabled AS notifications_enabled
            FROM user_reminder_schedules AS schedules
            JOIN user_reminder_texts AS texts ON texts.id = schedules.text_id
            JOIN services ON services.id = texts.service_id
            JOIN user_notification_prefs AS preferences
              ON preferences.service_id = texts.service_id
             AND preferences.actor_id = texts.actor_id
            WHERE schedules.enabled = 1
              AND preferences.enabled = 1
              AND services.enabled = 1
        """
        parameters: tuple = ()
        if actor_id is not None:
            query += " AND texts.actor_id = ?"
            parameters = (actor_id,)
        query += " ORDER BY schedules.id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            result = []
            for row in rows:
                contact = connection.execute(
                    "SELECT name, username FROM responsible_contacts "
                    "WHERE service_id = ? AND task = ? AND telegram_id = ? "
                    "ORDER BY name LIMIT 1",
                    (row["service_id"], row["task"], row["actor_id"]),
                ).fetchone()
                if contact is None:
                    continue
                result.append({
                    **dict(row),
                    "name": contact["name"],
                    "username": contact["username"],
                })
        return result

    def get_user_reminder_schedule(self, schedule_id: int) -> dict | None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT schedules.id AS schedule_id, schedules.text_id,
                       schedules.reminder_days, schedules.reminder_time,
                       texts.actor_id, texts.service_id, texts.task, texts.body,
                       services.name AS service_name,
                       services.enabled AS service_enabled,
                       preferences.enabled AS notifications_enabled,
                       schedules.enabled AS schedule_enabled
                FROM user_reminder_schedules AS schedules
                JOIN user_reminder_texts AS texts ON texts.id = schedules.text_id
                JOIN services ON services.id = texts.service_id
                LEFT JOIN user_notification_prefs AS preferences
                  ON preferences.service_id = texts.service_id
                 AND preferences.actor_id = texts.actor_id
                WHERE schedules.id = ?
                """,
                (schedule_id,),
            ).fetchone()
            if row is None:
                return None
            contact = connection.execute(
                "SELECT name, username FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ? "
                "ORDER BY name LIMIT 1",
                (row["service_id"], row["task"], row["actor_id"]),
            ).fetchone()
        return {
            **dict(row),
            "notifications_enabled": bool(row["notifications_enabled"]),
            "name": contact["name"] if contact else None,
            "username": contact["username"] if contact else "",
        }

    def is_user_reminder_delivered(
        self, schedule_id: int, event_date: str
    ) -> bool:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM user_reminder_deliveries "
                "WHERE schedule_id = ? AND event_date = ?",
                (schedule_id, event_date),
            ).fetchone() is not None

    def mark_user_reminder_delivered(
        self, schedule_id: int, event_date: str
    ) -> None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_reminder_deliveries "
                "(schedule_id, event_date, delivered_at) VALUES (?, ?, ?)",
                (schedule_id, event_date, _utc_now()),
            )

    def resolve_text_for_user(
        self,
        actor_id: int,
        service_id: str,
        task: str,
        personal_templates: dict[str, str] | None = None,
    ) -> str:
        user_text = self.get_user_text(actor_id, service_id, task)
        if user_text is not None and user_text["body"] is not None:
            return user_text["body"]
        if personal_templates and personal_templates.get(task):
            return personal_templates[task]
        return (
            "{name}, напоминаем о задаче «{task}» службы "
            "«{service_name}» на {sunday_date}."
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

    def list_admin_ids(self) -> dict[int, bool]:
        """Return every administrator Telegram ID mapped to super-admin status."""
        with self._connection() as connection:
            service_admins = connection.execute(
                "SELECT DISTINCT telegram_id FROM service_admins"
            ).fetchall()
            super_admins = connection.execute(
                "SELECT telegram_id FROM super_admins"
            ).fetchall()
        admins = {row["telegram_id"]: False for row in service_admins}
        admins.update({row["telegram_id"]: True for row in super_admins})
        return admins

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
