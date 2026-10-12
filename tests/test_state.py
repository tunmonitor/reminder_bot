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

    def test_completed_week_does_not_match_a_different_group_chat(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = StateCache(Path(tmp_dir) / "state.json")
            cache.replace({
                "sunday_date": "2026-10-11",
                "group_chat_id": -1001,
                "complete": True,
                "participants": {},
            })

            self.assertTrue(cache.matches_week("2026-10-11", -1001))
            self.assertFalse(cache.matches_week("2026-10-11", -1002))

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

    def test_merge_resends_group_message_to_new_chat_but_keeps_old_dm_chat(self):
        previous = _build_state(
            {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                },
            },
            group_chat_id=-1001,
        )
        previous.update(
            sunday_date="2026-10-11",
            general_sent=True,
            general_message_id=50,
            general_text="старое",
            complete=True,
        )
        previous["participants"]["Ответственный"].update(
            delivered=True,
            message_id=51,
            chat_id=12345,
            group_chat_id=-1001,
        )
        current = _build_state(
            {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                },
            },
            group_chat_id=-1002,
        )
        current["sunday_date"] = "2026-10-11"

        merged = _merge_week_state(previous, current, -1002)

        self.assertFalse(merged["general_sent"])
        self.assertIsNone(merged["general_message_id"])
        self.assertEqual(merged["group_chat_id"], -1002)
        self.assertTrue(merged["participants"]["Ответственный"]["delivered"])
        self.assertEqual(
            merged["participants"]["Ответственный"]["group_chat_id"], -1001
        )


if __name__ == "__main__":
    unittest.main()
