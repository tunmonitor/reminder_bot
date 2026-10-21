"""Formatting helpers for user-facing assignment summaries."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .ui import user_link


_TASK_EMOJIS = (
    ("аудио", "🎧"),
    ("камер", "📷"),
    ("пульт", "🎛️"),
    ("компьютер", "💻"),
    ("монтаж", "🎬"),
    ("микшер", "🎚️"),
    ("звук", "🎚️"),
    ("слайд", "🖥️"),
    ("презентац", "🖥️"),
    ("трансляц", "📡"),
    ("поряд", "🧹"),
    ("кухн", "🍽️"),
    ("кофе", "☕"),
    ("ведущ", "🎤"),
    ("проповед", "📖"),
    ("хкц", "🧭"),
    ("сборк", "🧰"),
    ("спевк", "🎶"),
    ("пирог", "🥧"),
    ("старш", "⭐"),
    ("смен", "👥"),
    ("книг", "📚"),
    ("уборк", "🧹"),
    ("детск", "🧸"),
)


def task_emoji(task: str) -> str:
    normalized = task.casefold()
    return next(
        (emoji for fragment, emoji in _TASK_EMOJIS if fragment in normalized),
        "📌",
    )


def task_assignment_names(task_info: Mapping | list[str]) -> list[str]:
    if isinstance(task_info, Mapping):
        names = task_info.get("names", [])
        return list(names) if isinstance(names, (list, tuple, set)) else []
    return list(task_info)


def task_is_informational(task_info: Mapping | list[str]) -> bool:
    return (
        bool(task_info.get("is_informational", False))
        if isinstance(task_info, Mapping)
        else False
    )


def person_label(
    task: str,
    name: str,
    contacts: dict[tuple[str, str], dict] | None = None,
    *,
    mark_unlinked: bool = False,
) -> str:
    """Имя ответственного: ссылка на Telegram, если человек привязан."""
    contact = (contacts or {}).get((task, name))
    username = contact.get("username") if contact else None
    label = f"{name} (@{username})" if username else name
    telegram_id = contact.get("telegram_id") if contact else None
    if telegram_id:
        return user_link(telegram_id, label)
    if contact is None and mark_unlinked:
        return f"{name} · не привязан"
    return label


def format_task_responsibles(
    task: str,
    names: list[str],
    contacts: dict[tuple[str, str], dict] | None = None,
) -> str:
    """Поле {task_responsibles}: ответственные одной задачи через запятую."""
    return ", ".join(
        person_label(task, name, contacts, mark_unlinked=True) for name in names
    )


def format_task_assignments(
    assignments: dict[str, dict[str, Any] | list[str]],
    contacts: dict[tuple[str, str], dict] | None = None,
    *,
    mark_unlinked: bool = False,
) -> str:
    if not assignments:
        return "⚪ Ответственные не указаны"

    groups = []
    for task, task_info in assignments.items():
        if task_is_informational(task_info):
            continue
        names = task_assignment_names(task_info)
        lines = [f"{task_emoji(task)} {task}"]
        if not names:
            lines.append("• Ответственные не указаны")
        for name in names:
            lines.append(
                f"• {person_label(task, name, contacts, mark_unlinked=mark_unlinked)}"
            )
        groups.append("\n".join(lines))
    return "\n\n".join(groups) or "⚪ Ответственные не указаны"

def format_informational_notes(
    assignments: dict[str, dict[str, Any] | list[str]],
) -> str:
    notes = []
    for task, task_info in assignments.items():
        if not task_is_informational(task_info):
            continue
        values = task_assignment_names(task_info)
        note = f"📌 {task}"
        if values:
            note += f" — {', '.join(values)}"
        notes.append(note)
    return "\n".join(notes)
