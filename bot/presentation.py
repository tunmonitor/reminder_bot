"""Formatting helpers for user-facing assignment summaries."""
from __future__ import annotations


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


def format_task_assignments(
    assignments: dict[str, list[str]],
    contacts: dict[tuple[str, str], dict] | None = None,
    *,
    mark_unlinked: bool = False,
) -> str:
    if not assignments:
        return "└ Ответственные не указаны"

    groups = []
    for task, names in assignments.items():
        lines = [f"├ {task} {task_emoji(task)}"]
        if not names:
            lines.append("├ Ответственные не указаны")
        for name in names:
            contact = (contacts or {}).get((task, name))
            username = contact.get("username") if contact else None
            suffix = f" (@{username})" if username else (
                " (Telegram не привязан)"
                if mark_unlinked and contact is None
                else ""
            )
            lines.append(f"├ {name}{suffix}")
        groups.append("\n".join(lines))
    return "\n\n".join(groups)
