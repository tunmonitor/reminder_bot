"""Shared, safe rendering primitives for Telegram conversation screens."""
from __future__ import annotations

import base64
import html
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MAX_SCREEN_UNITS = 500
MAX_CALLBACK_UNITS = 200
_CODE_MARKER = re.compile(r"\x00CODE:([A-Za-z0-9_-]+)\x00")
_STATUS_PREFIX = re.compile(r"^(?:✅|❌|⚠️|ℹ️|⏳)\s*")
_NAVIGATION_PREFIXES = ("⬅️", "✖️", "✅", "➡️", "↩️", "🏠")
_SINGLE_ACTION_PREFIXES = ("⏰", "📝", "➕")


def code(value: int | str | None) -> str:
    """Mark a value for safe display as an HTML code span."""
    if value is None:
        return "⚪ не задано"
    encoded = base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii")
    return f"\x00CODE:{encoded.rstrip('=')}\x00"


def _escape_rich_text(text: str) -> str:
    pieces = []
    cursor = 0
    for match in _CODE_MARKER.finditer(text):
        pieces.append(html.escape(text[cursor:match.start()]))
        encoded = match.group(1)
        value = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8")
        pieces.append(f"<code>{html.escape(value)}</code>")
        cursor = match.end()
    pieces.append(html.escape(text[cursor:]))
    return "".join(pieces)


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _split_long_line(line: str, limit: int) -> list[str]:
    result = []
    current = []
    current_units = 0
    for character in line:
        size = _units(character)
        if current and current_units + size > limit:
            result.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += size
    if current:
        result.append("".join(current))
    return result or [""]


def render_screen_chunks(text: str, limit: int = MAX_SCREEN_UNITS) -> list[str]:
    """Escape plain dialog text, emphasize its first line, and cap each screen."""
    if limit < 32:
        raise ValueError("Лимит экрана должен быть не меньше 32 символов.")
    source = str(text).strip()
    if not source:
        source = "ℹ️ Продолжайте диалог"

    lines = source.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for line in lines:
        for segment in _split_long_line(line, limit):
            extra = _units(segment) + (1 if current else 0)
            if current and current_units + extra > limit:
                chunks.append("\n".join(current))
                current = []
                current_units = 0
                extra = _units(segment)
            current.append(segment)
            current_units += extra
    if current:
        chunks.append("\n".join(current))

    rendered = []
    for chunk in chunks:
        title, separator, body = chunk.partition("\n")
        title_html = f"<b>{_escape_rich_text(title)}</b>"
        rendered.append(
            title_html
            + (f"\n{_escape_rich_text(body)}" if separator else "")
        )
    return rendered


def callback_text(text: str | None) -> str | None:
    """Normalize Telegram toast text to concise, plain, emoji-free text."""
    if text is None:
        return None
    normalized = _STATUS_PREFIX.sub("", str(text)).strip()
    if not normalized:
        return None
    shortened = []
    units = 0
    for character in normalized:
        size = _units(character)
        if units + size > MAX_CALLBACK_UNITS:
            break
        shortened.append(character)
        units += size
    return "".join(shortened)


def validate_inline_keyboard(rows: list[list[object]]) -> None:
    """Raise when a screen's inline keyboard exceeds Telegram design limits."""
    if any(len(row) > 3 for row in rows):
        raise ValueError("В одном ряду допускается не более 3 кнопок.")
    if sum(map(len, rows)) > 6:
        raise ValueError("На одном экране допускается не более 6 кнопок.")
    for row in rows:
        for button in row:
            callback_data = getattr(button, "callback_data", None)
            if (
                callback_data is not None
                and len(callback_data.encode("utf-8")) > 64
            ):
                raise ValueError("callback_data не может превышать 64 байта.")


def nav_row(
    back: InlineKeyboardButton | None = None,
    save: InlineKeyboardButton | None = None,
    cancel: InlineKeyboardButton | None = None,
) -> list[InlineKeyboardButton]:
    """Build a navigation row from whichever navigation buttons are present."""
    return [button for button in (back, save, cancel) if button is not None]


def layout_buttons(
    buttons: list[InlineKeyboardButton],
    *,
    max_row_width: int = 40,
    max_row_buttons: int = 3,
    short_button: int = 20,
    very_short_button: int = 15,
) -> list[list[InlineKeyboardButton]]:
    """Arrange flat inline buttons into compact rows, keeping navigation last."""
    if (
        max_row_width <= 0
        or max_row_buttons <= 0
        or short_button < 0
        or very_short_button < 0
    ):
        raise ValueError("Параметры компоновки кнопок должны быть положительными.")

    def is_navigation(button: InlineKeyboardButton) -> bool:
        return button.text.startswith(_NAVIGATION_PREFIXES) or button.text.endswith(
            "➡️"
        )

    navigation = [button for button in buttons if is_navigation(button)]
    regular = [button for button in buttons if not is_navigation(button)]
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []

    def flush() -> None:
        if current:
            rows.append(current.copy())
            current.clear()

    for button in regular:
        label = button.text
        if len(label) > short_button or label.startswith(
            _SINGLE_ACTION_PREFIXES
        ):
            flush()
            rows.append([button])
            continue

        proposed = [*current, button]
        proposed_width = sum(len(item.text) for item in proposed) + (
            2 * (len(proposed) - 1)
        )
        max_buttons = (
            max_row_buttons
            if all(len(item.text) <= very_short_button for item in proposed)
            else min(2, max_row_buttons)
        )
        if (
            len(proposed) > max_buttons
            or proposed_width > max_row_width
        ):
            flush()
            current.append(button)
        else:
            current.append(button)
    flush()

    if len(navigation) in {2, 3} and max_row_buttons >= len(navigation):
        if all(len(button.text) <= 24 for button in navigation):
            rows.append(navigation)
        else:
            rows.extend([[button] for button in navigation])
    else:
        rows.extend([[button] for button in navigation])
    return rows


async def send_telegram_screen(
    send_message: Callable[..., Awaitable[Any]],
    chat_id: int,
    text: str,
    limit: int = MAX_SCREEN_UNITS,
    **kwargs,
) -> Any:
    """Send a safe HTML screen, splitting long text at Telegram UI boundaries."""
    kwargs.pop("parse_mode", None)
    markup = kwargs.pop("reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        validate_inline_keyboard(markup.inline_keyboard)
    chunks = render_screen_chunks(text, limit=limit)
    result = None
    for index, chunk in enumerate(chunks):
        result = await send_message(
            chat_id,
            chunk,
            parse_mode="HTML",
            reply_markup=(
                markup if index == len(chunks) - 1 else None
            ),
            **kwargs,
        )
    return result


def render_edit_screen(
    text: str, limit: int = MAX_SCREEN_UNITS
) -> str:
    """Render one editable HTML screen; reject content that needs pagination."""
    chunks = render_screen_chunks(text, limit=limit)
    if len(chunks) != 1:
        raise ValueError("Длинное сообщение нельзя обновить одним экраном.")
    return chunks[0]
