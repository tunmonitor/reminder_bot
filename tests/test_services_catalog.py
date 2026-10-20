import unittest

from bot.services import SERVICES, normalized_name, parse_catalog


def _catalog(**service):
    base = {
        "id": "sound", "name": "Звук", "aliases": ["звук"],
        "columns": [{"headers": ["звук"], "layout": "pairs"}],
        "tasks": ["звук"],
    }
    base.update(service)
    return {"services": [base]}


class ServicesCatalogTest(unittest.TestCase):
    def test_production_catalog_has_agreed_services_and_tasks(self):
        services = {service.id: service for service in SERVICES}

        self.assertEqual(
            list(services),
            [
                "assembly", "broadcast", "kitchen", "sound", "order",
                "office_cleaning", "books", "children",
            ],
        )
        self.assertEqual(
            [(task.name, task.enabled) for task in services["kitchen"].tasks],
            [
                ("старший", True), ("в смене", True), ("пироги", True),
                ("кофе", False),
            ],
        )
        self.assertFalse(
            next(t for t in services["sound"].tasks if t.name == "спевка").enabled
        )
        self.assertEqual(services["books"].tasks, ())

    def test_normalized_name_ignores_case_yo_and_spaces_in_brackets(self):
        self.assertEqual(
            normalized_name(" Монтаж  проповеди ( видео) "),
            normalized_name("монтаж проповеди (видео)"),
        )
        self.assertEqual(normalized_name("Ведёт"), "ведет")

    def test_task_strings_and_objects_are_parsed(self):
        service = parse_catalog(_catalog(tasks=[
            "звук", {"name": "сборка", "aliases": ["сборка оборудования"]},
            {"name": "спевка", "enabled": False},
        ]))[0]

        self.assertEqual(
            [(task.name, task.aliases, task.enabled) for task in service.tasks],
            [
                ("звук", (), True),
                ("сборка", ("сборка оборудования",), True),
                ("спевка", (), False),
            ],
        )

    def test_invalid_catalogs_are_rejected(self):
        invalid = [
            {"services": []},
            _catalog(id="Bad id"),
            _catalog(name=" "),
            _catalog(tasks=["звук", {"name": "Звук"}]),
            _catalog(tasks=["звук", {"name": "сборка", "aliases": ["ЗВУК"]}]),
            _catalog(columns=[{"headers": ["звук"], "layout": "grid"}]),
            _catalog(columns=[
                {"headers": ["спевка"], "layout": "single", "task": "спевка"}
            ]),
            {"services": [
                _catalog()["services"][0],
                {**_catalog()["services"][0], "id": "other", "tasks": []},
            ]},
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_catalog(data)


if __name__ == "__main__":
    unittest.main()
