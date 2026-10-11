import tempfile
import unittest
from pathlib import Path

from bot.app import _build_state, _merge_week_state
from bot.state import StateCache


class StateCacheTest(unittest.TestCase):
    def test_week_is_complete_only_after_all_required_deliveries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = StateCache(Path(tmp_dir) / "state.json")
            cache.replace({
                "sunday_date": "2026-10-11",
                "complete": False,
                "participants": {},
            })
            self.assertFalse(cache.matches_week("2026-10-11"))

            cache.state["complete"] = True
            self.assertTrue(cache.matches_week("2026-10-11"))

    def test_merge_preserves_delivered_messages_and_refreshes_changed_id(self):
        previous = _build_state({
            "Ответственный": {
                "name": "Алексей", "username": "alex", "tg_id": 12345,
            },
            "Ведущий": {
                "name": "Павел", "username": "pavel", "tg_id": 23456,
            },
        })
        previous.update(
            sunday_date="2026-10-11",
            general_sent=True,
            general_message_id=50,
            general_text="старое",
        )
        previous["participants"]["Ответственный"].update(
            delivered=True, message_id=51, chat_id=12345, confirmed=True
        )
        current = _build_state({
            "Ответственный": {
                "name": "Алексей", "username": "alex", "tg_id": 12345,
            },
            "Ведущий": {
                "name": "Павел", "username": "pavel", "tg_id": 98765,
            },
        })
        current["sunday_date"] = "2026-10-11"

        merged = _merge_week_state(previous, current)

        self.assertTrue(merged["general_sent"])
        self.assertEqual(merged["general_message_id"], 50)
        self.assertTrue(merged["participants"]["Ответственный"]["delivered"])
        self.assertTrue(merged["participants"]["Ответственный"]["confirmed"])
        self.assertFalse(merged["participants"]["Ведущий"]["delivered"])
        self.assertIsNone(merged["participants"]["Ведущий"]["message_id"])


if __name__ == "__main__":
    unittest.main()
