"""Кэш текущей недели (reminder_state_cache.json).

Формат:
{
  "sunday_date": "2026-11-08",
  "general_message_id": 123,
  "general_sent": true,
  "participants": {
      "Ответственный": {"name": "...", "username": "...", "tg_id": 1,
                        "delivered": true, "confirmed": false,
                        "message_id": 10, "chat_id": 1},
      "Ведущий":        {...},
      "Проповедник":    {...}
  }
}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateCache:
    def __init__(self, path: Path) -> None:
        self.path = path
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
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    # --- api ---
    def replace(self, state: dict[str, Any]) -> None:
        """Затирает старую неделю новой."""
        self.state = state
        self.save()

    def clear(self) -> None:
        self.state = None
        if self.path.exists():
            self.path.unlink()

    def matches_week(self, sunday_iso: str) -> bool:
        return (
            bool(self.state)
            and self.state.get("sunday_date") == sunday_iso
            and self.state.get("complete") is True
        )
