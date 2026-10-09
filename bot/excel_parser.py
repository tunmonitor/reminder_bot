"""Парсинг xlsx: листы «График» и «Список»."""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from openpyxl import load_workbook

ROLES = ("Ответственный", "Ведущий", "Проповедник")

_DATE_RE = re.compile(r"^\s*\d{2}\.\d{2}\.\d{4}\b")


# ----------------------------
# Общие утилиты
# ----------------------------
def _clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _strip_role_prefix(cell: str, role: str) -> str | None:
    """'Ответственный; Аркадий' → 'Аркадий'. None, если роль не совпала."""
    if not cell.startswith(role):
        return None
    rest = cell[len(role):]
    rest = rest.lstrip(":;").strip()
    if not rest:
        return None
    return rest


# ----------------------------
# Лист «График»
# ----------------------------
def parse_schedule(path: Path, target: date,
                   debug_logger: logging.Logger) -> dict[str, str]:
    """Возвращает {role: name} на дату target."""
    wb = load_workbook(path, data_only=True, read_only=True)
    debug_logger.debug("Открыт %s, листы=%s", path, wb.sheetnames)
    if "График" not in wb.sheetnames:
        raise RuntimeError("Лист 'График' не найден")

    ws = wb["График"]
    target_str = target.strftime("%d.%m.%Y")
    debug_logger.debug("Ищу дату %s в листе 'График'", target_str)

    rows = list(ws.iter_rows(values_only=True))
    date_row: int | None = None
    for idx, row in enumerate(rows):
        if not row:
            continue
        if target_str in _clean(row[0]):
            date_row = idx
            debug_logger.debug("Дата найдена в строке %s", idx + 1)
            break

    if date_row is None:
        raise RuntimeError(f"Дата {target_str} не найдена на листе 'График'")

    # Собираем роли из блока после даты.
    found: dict[str, str] = {}
    for r in range(date_row + 1, min(date_row + 40, len(rows))):
        row = rows[r]
        if not row:
            continue
        first = _clean(row[0])
        # Наткнулись на следующую дату — блок закончился
        if _DATE_RE.match(first):
            break
        for cell in row:
            cell_s = _clean(cell)
            if not cell_s:
                continue
            for role in ROLES:
                name = _strip_role_prefix(cell_s, role)
                if name and role not in found:
                    found[role] = name
                    debug_logger.debug("Найдено: %s = %s", role, name)

    debug_logger.debug("Итог парсинга 'График': %s", found)
    missing = [r for r in ROLES if r not in found]
    if missing:
        raise RuntimeError(f"Не найдены роли на листе 'График': {missing}")
    return found


# ----------------------------
# Лист «Список»
# ----------------------------
def parse_people(path: Path,
                 debug_logger: logging.Logger) -> dict[str, dict[str, dict]]:
    """Возвращает {role: {name: {'username': str, 'tg_id': int}}}."""
    wb = load_workbook(path, data_only=True, read_only=True)
    if "Список" not in wb.sheetnames:
        raise RuntimeError("Лист 'Список' не найден")
    ws = wb["Список"]

    people: dict[str, dict[str, dict]] = {r: {} for r in ROLES}
    current_role: str | None = None

    for raw_row in ws.iter_rows(values_only=True):
        if not raw_row:
            continue
        cells = [_clean(c) for c in raw_row]
        if not any(cells):
            continue

        first = cells[0]
        if first in ROLES:
            current_role = first
            debug_logger.debug("Лист 'Список': начат блок '%s'", first)
            continue
        if current_role is None:
            continue

        # Склеиваем строку и режем по ';'
        joined = ";".join(c for c in cells if c)
        tokens = [t.strip() for t in joined.split(";") if t.strip()]
        if len(tokens) < 3:
            continue
        name = tokens[0]
        username = tokens[1].lstrip("@").strip()
        tg_id_str = re.sub(r"\D", "", tokens[2])
        if not tg_id_str:
            debug_logger.debug("У '%s' нет TG_ID, пропускаю", name)
            continue
        people[current_role][name] = {
            "username": username,
            "tg_id": int(tg_id_str),
        }

    debug_logger.debug(
        "Лист 'Список' загружен: %s",
        {r: list(v.keys()) for r, v in people.items()},
    )
    return people


# ----------------------------
# Сопоставление
# ----------------------------
def resolve_assignments(schedule: dict[str, str],
                        people: dict[str, dict[str, dict]]
                        ) -> dict[str, dict]:
    """Сопоставляет имена из 'График' с 'Список'. Возвращает полную структуру."""
    result: dict[str, dict] = {}
    for role, name in schedule.items():
        bucket = people.get(role, {})
        if name not in bucket:
            raise RuntimeError(
                f"Имя '{name}' (роль «{role}») не найдено на листе 'Список'"
            )
        info = bucket[name]
        if not info.get("tg_id"):
            raise RuntimeError(
                f"У '{name}' (роль «{role}») отсутствует Telegram_ID"
            )
        result[role] = {
            "name": name,
            "username": info["username"],
            "tg_id": info["tg_id"],
        }
    return result

