import unittest

from bot.handlers import (
    GROUP_FIELDS,
    REPORT_FIELDS,
    TEMPLATE_FIELD_HELP,
    template_fields_help,
)
from bot.service_registry import (
    ADMIN_REPORT_FIELDS,
    GENERAL_GROUP_TEMPLATE_FIELDS,
    GROUP_TEMPLATE_FIELDS,
)


class TemplateFieldHelpTest(unittest.TestCase):
    def test_prompt_fields_match_validated_fields(self):
        # Подсказки к вводу не должны расходиться с проверкой шаблонов.
        self.assertEqual(set(GROUP_FIELDS), GENERAL_GROUP_TEMPLATE_FIELDS)
        self.assertEqual(set(REPORT_FIELDS), ADMIN_REPORT_FIELDS)
        for fields in (GROUP_FIELDS, REPORT_FIELDS):
            self.assertEqual(len(fields), len(set(fields)))
        self.assertEqual(
            set(GROUP_FIELDS) | {"task", "task_responsibles"},
            GROUP_TEMPLATE_FIELDS,
        )

    def test_every_field_has_help(self):
        fields = GROUP_TEMPLATE_FIELDS | ADMIN_REPORT_FIELDS
        self.assertLessEqual(fields, set(TEMPLATE_FIELD_HELP))
        for group in (GROUP_FIELDS, REPORT_FIELDS):
            text = template_fields_help(group)
            self.assertEqual(len(text.splitlines()), 1 + len(group))


if __name__ == "__main__":
    unittest.main()
