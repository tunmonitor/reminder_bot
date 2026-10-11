"""Парсинг месячного расписания и справочника участников в xlsx."""
from __future__ import annotations

import logging
import re
import zipfile
from io import BytesIO
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.styles.borders import Side

ROLES = ("Ответственный", "Ведущий", "Проповедник")

_MONTH_SHEETS = (
    "ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ",
    "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ",
)
_MONTH_NAMES = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}
_ROLE_ALIASES = {
    "хкц": "Ответственный",
    "ведущий": "Ведущий",
    "ведет": "Ведущий",
    "проповедует": "Проповедник",
    "проповедет": "Проповедник",
}
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _repair_known_stylesheet_issue(path: Path) -> BytesIO | None:
    with zipfile.ZipFile(path) as source:
        try:
            stylesheet = source.read("xl/styles.xml")
        except KeyError:
            return None
        root = ElementTree.fromstring(stylesheet)
        borders = root.find(f"{{{_MAIN_NS}}}borders")
        if borders is None:
            return None

        repaired = False
        valid_styles = Side.style.values
        for border in borders:
            for side in border:
                style = side.get("style")
                if style is None or style in valid_styles:
                    continue
                if style != "solid":
                    raise ValueError(
                        f"Невозможно автоматически исправить стиль границы '{style}'"
                    )
                side.set("style", "thin")
                repaired = True

        if not repaired:
            return None

        repaired_stylesheet = ElementTree.tostring(
            root, encoding="utf-8", xml_declaration=True
        )
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as destination:
            for item in source.infolist():
                data = (
                    repaired_stylesheet
                    if item.filename == "xl/styles.xml"
                    else source.read(item.filename)
                )
                destination.writestr(item, data)
        output.seek(0)
        return output


def _load_workbook(path: Path, debug_logger: logging.Logger):
    try:
        return load_workbook(path, data_only=True, read_only=True)
    except ValueError as exc:
        if "could not read stylesheet" not in str(exc):
            raise
        repaired_workbook = _repair_known_stylesheet_issue(path)
        if repaired_workbook is None:
            raise
        debug_logger.warning(
            "В %s найден некорректный стиль границы 'solid'; "
            "для чтения использован вариант в памяти со стилем 'thin'",
            path,
        )
        return load_workbook(
            repaired_workbook, data_only=True, read_only=True
        )


def _clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _canonical_role(value: str) -> str | None:
    return _ROLE_ALIASES.get(_normalized(value))


def _schedule_sheet_name(target: date) -> str:
    return f"{_MONTH_SHEETS[target.month - 1]}{target.year % 100:02d}"


def _find_sheet(workbook, expected: str, legacy_names: tuple[str, ...] = ()):
    sheets_by_name = {_normalized(name): name for name in workbook.sheetnames}
    for candidate in (expected, *legacy_names):
        sheet_name = sheets_by_name.get(_normalized(candidate))
        if sheet_name is not None:
            return workbook[sheet_name], sheet_name
    return None, None


def _cell_date(value, target_year: int) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value).casefold().replace("ё", "е")
    numeric = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})(?:\b|$)", text)
    if numeric:
        try:
            return date(*(int(part) for part in (numeric[3], numeric[2], numeric[1])))
        except ValueError:
            return None
    named = re.match(r"^(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?(?:\b|$)", text)
    if named:
        month = _MONTH_NAMES.get(named[2])
        if month is None:
            return None
        try:
            return date(int(named[3] or target_year), month, int(named[1]))
        except ValueError:
            return None
    return None


def _parse_task_row(row) -> tuple[str, str] | None:
    task = _clean(row[0]) if row else ""
    if not task:
        return None
    task_name, separator, inline_name = task.partition(";")
    role = _canonical_role(task_name)
    if role is None:
        return None
    name = _clean(row[1]) if len(row) > 1 else ""
    if not name and separator:
        name = inline_name.strip()
    return role, name


def parse_schedule(path: Path, target: date,
                   debug_logger: logging.Logger) -> dict[str, str]:
    """Возвращает назначения трёх ролей для даты target."""
    workbook = _load_workbook(path, debug_logger)
    try:
        expected_sheet = _schedule_sheet_name(target)
        worksheet, sheet_name = _find_sheet(
            workbook, expected_sheet, legacy_names=("График",)
        )
        debug_logger.debug(
            "Открыт %s, листы=%s, лист расписания=%s",
            path, workbook.sheetnames, sheet_name,
        )
        if worksheet is None:
            raise RuntimeError(f"Лист расписания '{expected_sheet}' не найден")

        worksheet.reset_dimensions()
        rows = list(worksheet.iter_rows(values_only=True))
        target_row = next(
            (
                index for index, row in enumerate(rows)
                if row and _cell_date(row[0], target.year) == target
            ),
            None,
        )
        if target_row is None:
            raise RuntimeError(
                f"Дата {target:%d.%m.%Y} не найдена на листе '{sheet_name}'"
            )

        found: dict[str, str] = {}
        for row in rows[target_row + 1:]:
            if row and _cell_date(row[0], target.year) is not None:
                break
            task_row = _parse_task_row(row)
            if task_row is None:
                continue
            role, name = task_row
            if name and role in found and found[role] != name:
                raise RuntimeError(
                    f"Роль «{role}» указана несколько раз на дату {target:%d.%m.%Y}"
                )
            if name:
                found[role] = name
                debug_logger.debug("Найдено: %s = %s", role, name)

        missing = [role for role in ROLES if role not in found]
        if missing:
            debug_logger.error(
                "Не найдены назначения на листе '%s' для %s: %s",
                sheet_name, target.strftime("%d.%m.%Y"), missing,
            )
        debug_logger.debug("Итог парсинга '%s': %s", sheet_name, found)
        return found
    finally:
        workbook.close()


def _telegram_id(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    text = _clean(value)
    return int(text) if text.isdigit() and int(text) > 0 else None


def parse_people(path: Path,
                 debug_logger: logging.Logger) -> dict[str, dict[str, dict]]:
    """Возвращает справочник участников по задаче/роли с листа «Списки»."""
    workbook = _load_workbook(path, debug_logger)
    try:
        worksheet, sheet_name = _find_sheet(
            workbook, "Списки", legacy_names=("Список",)
        )
        if worksheet is None:
            raise RuntimeError("Лист 'Списки' не найден")

        worksheet.reset_dimensions()
        people: dict[str, dict[str, dict]] = {role: {} for role in ROLES}
        current_role: str | None = None
        for raw_row in worksheet.iter_rows():
            cells = list(raw_row)
            first_cell = cells[0] if cells else None
            first = _clean(first_cell.value) if first_cell else ""
            role = _canonical_role(first)
            if role is not None:
                current_role = role
                debug_logger.debug("Лист '%s': начат блок '%s'", sheet_name, first)
                continue
            has_extra_values = any(
                _clean(cell.value) for cell in cells[1:3]
            )
            if (
                first
                and not has_extra_values
                and first_cell is not None
                and getattr(getattr(first_cell, "font", None), "bold", False)
            ):
                current_role = None
                debug_logger.debug(
                    "Лист '%s': пропущен блок неизвестной задачи '%s'",
                    sheet_name, first,
                )
                continue
            if current_role is None or not first:
                continue

            username = _clean(cells[1].value) if len(cells) > 1 else ""
            tg_id = _telegram_id(cells[2].value if len(cells) > 2 else None)
            people[current_role][first] = {
                "username": username.lstrip("@").strip(),
                "tg_id": tg_id,
            }

        debug_logger.debug(
            "Лист '%s' загружен: %s",
            sheet_name, {role: list(names) for role, names in people.items()},
        )
        return people
    finally:
        workbook.close()


def resolve_assignments(schedule: dict[str, str],
                        people: dict[str, dict[str, dict]],
                        debug_logger: logging.Logger | None = None,
                        issues: list[str] | None = None,
                        ) -> dict[str, dict]:
    """Сопоставляет назначения с Telegram-данными по роли."""
    result: dict[str, dict] = {}
    for role, name in schedule.items():
        bucket = people.get(role, {})
        if name not in bucket:
            message = f"Имя '{name}' (роль «{role}») не найдено на листе 'Списки'"
            if debug_logger is not None:
                debug_logger.error(
                    "Не найден участник: роль=%s, имя=%s; в соответствующем "
                    "блоке справочника такая запись отсутствует",
                    role, name,
                )
            if issues is not None:
                issues.append(message)
                continue
            raise RuntimeError(message)
        info = bucket[name]
        if not info.get("tg_id"):
            message = f"У '{name}' (роль «{role}») отсутствует Telegram_ID"
            if debug_logger is not None:
                debug_logger.error(
                    "Найдена запись без Telegram ID: роль=%s, имя=%s, "
                    "username=%s, tg_id=%s",
                    role, name, info.get("username", ""), info.get("tg_id"),
                )
            if issues is not None:
                issues.append(message)
                continue
            raise RuntimeError(message)
        result[role] = {
            "name": name,
            "username": info["username"],
            "tg_id": info["tg_id"],
        }
        if debug_logger is not None:
            debug_logger.debug(
                "Назначение сопоставлено: роль=%s, имя=%s, username=@%s, tg_id=%s",
                role, name, info["username"], info["tg_id"],
            )
    return result
