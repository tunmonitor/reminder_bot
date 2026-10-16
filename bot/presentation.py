"""Formatting helpers for user-facing assignment summaries."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_TASK_EMOJIS = (
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
            contact = (contacts or {}).get((task, name))
            username = contact.get("username") if contact else None
            suffix = f" (@{username})" if username else (
                " · не привязан"
                if mark_unlinked and contact is None
                else ""
            )
            lines.append(f"• {name}{suffix}")
        groups.append("\n".join(lines))
    return "\n\n".join(groups) or "⚪ Ответственные не указаны"
