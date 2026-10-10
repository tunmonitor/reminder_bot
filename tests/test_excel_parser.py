import logging
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock
from xml.etree import ElementTree

from openpyxl import Workbook
from openpyxl.styles import Border, Font, Side

from bot.excel_parser import parse_people, parse_schedule, resolve_assignments


class ExcelParserTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "schedule.xlsx"
        self.debug_logger = logging.getLogger("test.excel_parser")

    def _make_workbook(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "октябрь26"
        schedule.append(["СОБРАНИЕ"])
        schedule["A1"].border = Border(left=Side(style="thin"))
        schedule.append(["4 октября"])
        schedule.append(["ХКЦ", "Алексей"])
        schedule.append(["ведет", "Павел"])
        schedule.append(["ПРОПОВЕДУЕТ", "Иван"])
        schedule.append(["11 октября"])
        schedule.append(["ХКЦ", "Другой"])

        people = workbook.create_sheet("списки")
        people.append(["Проповедет"])
        people.append(["В.П."])
        people.append(["Иван", "ivan", 123456789])
        people.append(["ведущий"])
        people.append(["Павел", "pavel", 234567891])
        people.append(["Другая задача"])
        people["A6"].font = Font(bold=True)
        people.append(["Не назначать"])
        people.append(["Евгений"])
        people.append(["ХКЦ"])
        people.append(["Сергей"])
        people.append(["Аркадий"])
        people.append(["Алексей", "alebzor", 181601986])
        workbook.save(self.path)

    def test_parses_month_sheet_and_schedule_blocks_case_insensitively(self):
        self._make_workbook()

        assignments = parse_schedule(
            self.path, date(2026, 10, 4), self.debug_logger
        )

        self.assertEqual(
            assignments,
            {
                "Ответственный": "Алексей",
                "Ведущий": "Павел",
                "Проповедник": "Иван",
            },
        )

    def test_empty_duplicate_role_does_not_override_or_reject_assignment(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "ОКТЯБРЬ26"
        schedule.append(["11 октября"])
        schedule.append(["ХКЦ", "Алексей"])
        schedule.append(["ХКЦ", None])
        workbook.save(self.path)

        assignments = parse_schedule(
            self.path, date(2026, 10, 11), self.debug_logger
        )

        self.assertEqual(assignments, {"Ответственный": "Алексей"})

    def test_conflicting_non_empty_duplicate_role_is_rejected(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "ОКТЯБРЬ26"
        schedule.append(["11 октября"])
        schedule.append(["ХКЦ", "Алексей"])
        schedule.append(["ХКЦ", "Павел"])
        workbook.save(self.path)

        with self.assertRaisesRegex(RuntimeError, "указана несколько раз"):
            parse_schedule(self.path, date(2026, 10, 11), self.debug_logger)

    def test_parses_task_sections_anywhere_and_uses_last_filled_person_row(self):
        self._make_workbook()

        people = parse_people(self.path, self.debug_logger)
        assignments = resolve_assignments(
            {
                "Ответственный": "Алексей",
                "Ведущий": "Павел",
                "Проповедник": "Иван",
            },
            people,
        )

        self.assertEqual(assignments["Ответственный"]["tg_id"], 181601986)
        self.assertEqual(assignments["Ответственный"]["username"], "alebzor")
        self.assertEqual(assignments["Проповедник"]["tg_id"], 123456789)
        self.assertNotIn("Не назначать", people["Ведущий"])

    def test_selected_person_without_telegram_id_fails_explicitly(self):
        self._make_workbook()

        logger = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "отсутствует Telegram_ID"):
            resolve_assignments(
                {"Ответственный": "Сергей"},
                parse_people(self.path, self.debug_logger),
                logger,
            )

        logger.error.assert_called_once()
        self.assertIn("Сергей", logger.error.call_args.args)
        self.assertIsNone(logger.error.call_args.args[-1])

    def test_logs_resolved_assignment_details(self):
        logger = MagicMock()
        assignments = resolve_assignments(
            {"Ответственный": "Алексей"},
            {
                "Ответственный": {
                    "Алексей": {
                        "username": "alebzor",
                        "tg_id": 181601986,
                    }
                }
            },
            logger,
        )

        self.assertEqual(assignments["Ответственный"]["tg_id"], 181601986)
        logger.debug.assert_called_once()
        self.assertEqual(
            logger.debug.call_args.args[1:],
            ("Ответственный", "Алексей", "alebzor", 181601986),
        )

    def test_repairs_known_invalid_border_style_in_memory(self):
        self._make_workbook()
        malformed_path = Path(self.temp_dir.name) / "malformed.xlsx"
        main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

        with zipfile.ZipFile(self.path) as source:
            with zipfile.ZipFile(malformed_path, "w") as destination:
                for item in source.infolist():
                    content = source.read(item.filename)
                    if item.filename == "xl/styles.xml":
                        root = ElementTree.fromstring(content)
                        borders = root.find(f"{{{main_ns}}}borders")
                        border_side = next(
                            side
                            for border in borders
                            for side in border
                            if side.get("style") == "thin"
                        )
                        border_side.set("style", "solid")
                        content = ElementTree.tostring(
                            root, encoding="utf-8", xml_declaration=True
                        )
                    elif (
                        item.filename.startswith("xl/worksheets/")
                        and item.filename.endswith(".xml")
                    ):
                        root = ElementTree.fromstring(content)
                        dimension = root.find(f"{{{main_ns}}}dimension")
                        if dimension is not None:
                            dimension.set("ref", "A1")
                            content = ElementTree.tostring(
                                root, encoding="utf-8", xml_declaration=True
                            )
                    destination.writestr(item, content)

        schedule = parse_schedule(
            malformed_path, date(2026, 10, 4), self.debug_logger
        )
        people = parse_people(malformed_path, self.debug_logger)
        assignments = resolve_assignments(schedule, people)

        self.assertEqual(assignments["Ведущий"]["name"], "Павел")
        self.assertEqual(assignments["Ответственный"]["tg_id"], 181601986)
        with zipfile.ZipFile(malformed_path) as archive:
            styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
            schedule_xml = ElementTree.fromstring(
                archive.read("xl/worksheets/sheet1.xml")
            )
        self.assertTrue(
            any(side.get("style") == "solid" for side in styles.iter())
        )
        self.assertEqual(
            schedule_xml.find(f"{{{main_ns}}}dimension").get("ref"),
            "A1",
        )


if __name__ == "__main__":
    unittest.main()
