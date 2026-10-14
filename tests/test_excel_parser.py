import logging
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from openpyxl import Workbook
from openpyxl.cell.read_only import ReadOnlyCell
from openpyxl.styles import Border, Font, Side

from bot.excel_parser import (
    parse_people,
    parse_schedule,
    parse_service_events,
    resolve_assignments,
)


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

    def test_parses_service_events_from_horizontal_monthly_blocks(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "ОКТЯБРЬ26"
        schedule.append([
            "СОБРАНИЕ", None, "ЗВУК", None, "СПЕВКА", "СЛАЙДЫ",
            "ТРАНСЛЯЦИЯ", None, "ОРГ. ПОРЯДКА", None, "КУХНЯ", None,
            "КОФЕ", None, "ДЕТСКОЕ",
        ])
        schedule.append([
            date(2026, 10, 11), None, date(2026, 10, 12), None,
            date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15),
            None, date(2026, 10, 16), None, date(2026, 10, 17), None,
            date(2026, 10, 18),
        ])
        schedule.append([
            "ХКЦ", "Ответственный А", "Микшер", "Звук А", "Спевка А",
            "Слайды А", "Камера", "Трансляция А", "Организация",
            "Порядок А", "Кухня", "Кухня А", "Кофе", "Кофе А",
            "Посторонний блок",
        ])
        schedule.append([
            "Ведущий", "Ответственный Б", None, "Звук Б", None, None,
            "Компьютер", "Трансляция Б", None, "Порядок Б",
        ])
        schedule.append(["Проповедует", "Ответственный В"])
        schedule.append(["Ответственный; Ответственный inline", None])
        schedule.append([None, None, None, None, None, None, None, None,
                         None, None, None, None, date(2026, 10, 25)])
        schedule.append([
            None, None, None, None, None, None, None, None, None, None,
            None, None, "Кофе", "Кофе В",
        ])
        schedule.append([
            date(2026, 10, 18), None, date(2026, 10, 19), None,
            date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22),
            None, date(2026, 10, 23), None, date(2026, 10, 24), None,
            date(2026, 10, 26),
        ])
        schedule.append([
            "ХКЦ", "Ответственный Г", "Микшер", "Звук В", "Спевка Б",
            "Слайды Б", "Камера", "Трансляция В", "Организация",
            "Порядок В", "Кухня", "Кухня Б", "Кофе", "Кофе Г",
        ])
        workbook.save(self.path)

        parsed = parse_service_events(
            self.path, date(2026, 10, 1), self.debug_logger
        )

        self.assertEqual(
            parsed["assembly"][0].event_date, date(2026, 10, 11)
        )
        self.assertEqual(
            [(item.task, item.name) for item in parsed["assembly"][0].assignments],
            [
                ("Ответственный", "Ответственный А"),
                ("Ведущий", "Ответственный Б"),
                ("Проповедник", "Ответственный В"),
                ("Ответственный", "Ответственный inline"),
            ],
        )
        self.assertEqual(
            [(item.task, item.name) for item in parsed["sound"][0].assignments],
            [("Микшер", "Звук А"), ("Звук", "Звук Б")],
        )
        self.assertEqual(
            [(item.task, item.name) for item in parsed["rehearsal"][0].assignments],
            [("Спевка", "Спевка А")],
        )
        self.assertEqual(
            parsed["slides"][0].assignments[0].name, "Слайды А"
        )
        self.assertEqual(
            [(item.task, item.name) for item in parsed["broadcast"][0].assignments],
            [("Камера", "Трансляция А"), ("Компьютер", "Трансляция Б")],
        )
        self.assertEqual(
            parsed["order"][0].assignments[0].name, "Порядок А"
        )
        self.assertEqual(
            parsed["kitchen"][0].assignments[0].name, "Кухня А"
        )
        self.assertEqual(len(parsed["coffee"]), 3)
        self.assertEqual(
            [item.name for item in parsed["coffee"][0].assignments],
            ["Кофе А"],
        )
        self.assertEqual(
            [item.name for item in parsed["coffee"][1].assignments],
            ["Кофе В"],
        )
        self.assertEqual(
            [item.name for item in parsed["coffee"][2].assignments],
            ["Кофе Г"],
        )
        self.assertNotIn("Посторонний блок", [
            item.name
            for events in parsed.values()
            for event in events
            for item in event.assignments
        ])

    def test_service_events_support_legacy_schedule_sheet_for_assembly(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "График"
        schedule.append(["Дата", "Задача", "Ответственный"])
        schedule.append([date(2026, 10, 11), None, None])
        schedule.append([None, "ХКЦ", "Алексей"])
        schedule.append([None, "Ведущий", "Павел"])
        schedule.append([date(2026, 10, 18), None, None])
        schedule.append([None, "Проповедует", "Иван"])
        workbook.save(self.path)

        parsed = parse_service_events(
            self.path, date(2026, 10, 1), self.debug_logger
        )

        self.assertEqual(
            [
                (
                    event.event_date,
                    [(item.task, item.name) for item in event.assignments],
                )
                for event in parsed["assembly"]
            ],
            [
                (
                    date(2026, 10, 11),
                    [
                        ("Ответственный", "Алексей"),
                        ("Ведущий", "Павел"),
                    ],
                ),
                (
                    date(2026, 10, 18),
                    [("Проповедник", "Иван")],
                ),
            ],
        )
        self.assertTrue(all(not events for key, events in parsed.items() if key != "assembly"))

    def test_service_events_exclude_dates_outside_requested_month_and_year(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "ОКТЯБРЬ26"
        schedule.append(["СОБРАНИЕ", None, "ДРУГОЕ"])
        schedule.append([date(2026, 10, 11), None])
        schedule.append(["ХКЦ; Inline value", "Октябрьский ответственный"])
        schedule.append([date(2026, 11, 1), None])
        schedule.append(["ХКЦ", "Ноябрьский ответственный"])
        schedule.append([date(2027, 10, 2), None])
        schedule.append(["ХКЦ", "Следующего года"])
        workbook.save(self.path)

        parsed = parse_service_events(
            self.path, date(2026, 10, 1), self.debug_logger
        )

        self.assertEqual(len(parsed["assembly"]), 1)
        self.assertEqual(
            parsed["assembly"][0].event_date, date(2026, 10, 11)
        )
        self.assertEqual(
            [item.name for item in parsed["assembly"][0].assignments],
            ["Октябрьский ответственный"],
        )

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

    def test_parses_legacy_sheet_names_and_role_labels(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "График"
        schedule.append(["11 октября"])
        schedule.append(["Ответственный", "Алексей"])
        schedule.append(["Проповедник", "Иван"])
        people = workbook.create_sheet("Список")
        people.append(["Ответственный"])
        people.append(["Алексей", "alex", 181601986])
        people.append(["Проповедник"])
        people.append(["Иван", "ivan", 123456789])
        workbook.save(self.path)

        schedule_data = parse_schedule(
            self.path, date(2026, 10, 11), self.debug_logger
        )
        contacts = parse_people(self.path, self.debug_logger)
        assignments = resolve_assignments(schedule_data, contacts)

        self.assertEqual(schedule_data, {
            "Ответственный": "Алексей",
            "Проповедник": "Иван",
        })
        self.assertEqual(assignments["Ответственный"]["tg_id"], 181601986)
        self.assertEqual(assignments["Проповедник"]["tg_id"], 123456789)

    def test_parses_legacy_schedule_inline_and_non_first_column_roles(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "График"
        schedule.append(["11 октября"])
        schedule.append(["ХКЦ: Алексей"])
        schedule.append([None, "Ведущий", "Павел"])
        schedule.append([None, None, "Проповедник; Иван"])
        workbook.save(self.path)

        assignments = parse_schedule(
            self.path, date(2026, 10, 11), self.debug_logger
        )

        self.assertEqual(assignments, {
            "Ответственный": "Алексей",
            "Ведущий": "Павел",
            "Проповедник": "Иван",
        })

    def test_parses_multiple_legacy_assignments_from_one_row(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "График"
        schedule.append(["11 октября"])
        schedule.append([
            "ХКЦ: Алексей",
            "Ведущий; Павел",
            "Проповедует: Иван",
        ])
        workbook.save(self.path)

        assignments = parse_schedule(
            self.path, date(2026, 10, 11), self.debug_logger
        )

        self.assertEqual(assignments, {
            "Ведущий": "Павел",
            "Ответственный": "Алексей",
            "Проповедник": "Иван",
        })

    def test_legacy_role_cell_is_not_used_as_neighboring_role_name(self):
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = "График"
        schedule.append(["11 октября"])
        schedule.append(["Ответственный", "Ведущий: Павел"])
        workbook.save(self.path)

        assignments = parse_schedule(
            self.path, date(2026, 10, 11), self.debug_logger
        )

        self.assertEqual(assignments, {"Ведущий": "Павел"})

    def test_parses_legacy_semicolon_contacts_and_decorated_ids_only(self):
        workbook = Workbook()
        people = workbook.active
        people.title = "Список"
        people.append(["ХКЦ"])
        people.append(["Алексей; @alex; TG_ID: 123456789"])
        people.append(["Мария", "@maria", "Telegram_ID: 234567891"])
        workbook.save(self.path)

        parsed = parse_people(self.path, self.debug_logger)

        self.assertEqual(
            parsed["Ответственный"]["Алексей"],
            {"username": "alex", "tg_id": 123456789},
        )
        self.assertEqual(
            parsed["Ответственный"]["Мария"],
            {"username": "maria", "tg_id": 234567891},
        )

    def test_tabular_contacts_reject_decorated_telegram_ids(self):
        workbook = Workbook()
        people = workbook.active
        people.title = "Списки"
        people.append(["ХКЦ"])
        people.append(["Алексей", "alex", "TG_ID: 123456789"])
        workbook.save(self.path)

        parsed = parse_people(self.path, self.debug_logger)

        self.assertIsNone(parsed["Ответственный"]["Алексей"]["tg_id"])

    def test_partial_resolution_keeps_username_without_telegram_id(self):
        issues = []
        assignments = resolve_assignments(
            {"Ответственный": "Алексей"},
            {
                "Ответственный": {
                    "Алексей": {"username": "alex", "tg_id": None}
                }
            },
            issues=issues,
        )

        self.assertEqual(
            assignments["Ответственный"],
            {"name": "Алексей", "username": "alex", "tg_id": None},
        )
        self.assertTrue(any("отсутствует Telegram_ID" in issue for issue in issues))

    def test_parses_people_when_read_only_cell_has_no_font(self):
        workbook = Workbook()
        people = workbook.active
        people.title = "Списки"
        people.append(["Неизвестная задача"])
        people["A1"].font = Font(bold=True)
        workbook.save(self.path)

        with patch.object(ReadOnlyCell, "font", new=property(lambda _cell: None)):
            parsed_people = parse_people(self.path, self.debug_logger)

        self.assertEqual(parsed_people, {role: {} for role in (
            "Ответственный", "Ведущий", "Проповедник"
        )})

    def test_partial_duplicate_contact_does_not_clear_telegram_id(self):
        workbook = Workbook()
        people = workbook.active
        people.title = "Списки"
        people.append(["ХКЦ"])
        people.append(["Алексей", "alex", 181601986])
        people.append(["Алексей", "alexander", None])
        workbook.save(self.path)

        parsed_people = parse_people(self.path, self.debug_logger)

        self.assertEqual(
            parsed_people["Ответственный"]["Алексей"],
            {"username": "alexander", "tg_id": 181601986},
        )

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
