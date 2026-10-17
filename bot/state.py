"""Кэш текущей недели (reminder_state_cache.json).

Формат:
{
  "sunday_date": "2026-11-08",
  "group_chat_id": -100123,
  "general_message_id": 123,
  "general_sent": true,
  "participants": {
      "Ответственный": {"name": "...", "username": "...", "tg_id": 1,
                        "delivered": true, "confirmed": false,
                        "message_id": 10, "chat_id": 1,
                        "group_chat_id": -100123},
      "Ведущий":        {...},
      "Проповедник":    {...}
  }
}
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


class StateCache:
    def __init__(
        self, path: Path, on_change: Callable[[], None] | None = None
    ) -> None:
        self.path = path
        self.on_change = on_change
        self.state: dict[str, Any] | None = None
        self._load()

    # --- io ---
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.state = None

    def save(self) -> None:
        if self.state is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".reminder_state_cache-", suffix=".tmp", dir=self.path.parent
        )
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as cache_file:
                fd = -1
                json.dump(self.state, cache_file, ensure_ascii=False, indent=2)
                cache_file.flush()
                os.fsync(cache_file.fileno())
            tmp.replace(self.path)
        finally:
            if fd >= 0:
                os.close(fd)
            tmp.unlink(missing_ok=True)
        if self.on_change is not None:
            self.on_change()

    # --- api ---
    def replace(self, state: dict[str, Any]) -> None:
        """Затирает старую неделю новой."""
        self.state = state
        self.save()

    def clear(self) -> None:
        self.state = None
        if self.path.exists():
            self.path.unlink()
            if self.on_change is not None:
                self.on_change()

    def matches_week(
        self,
        sunday_iso: str,
        group_chat_id: int | None = None,
        legacy_group_chat_id: int | None = None,
    ) -> bool:
        if not self.state or self.state.get("sunday_date") != sunday_iso:
            return False
        saved_chat_id = self.state.get("group_chat_id")
        if saved_chat_id is None:
            saved_chat_id = legacy_group_chat_id
        return (
            self.state.get("complete") is True
            and (
                group_chat_id is None
                or saved_chat_id == group_chat_id
            )
        )
