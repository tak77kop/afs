import unittest
import os
import sys

from tests import SRC_DIR
sys.path.insert(0, os.path.join(SRC_DIR, "afs_evaluator_app"))

from afs_evaluator_app.faces_data import (
    FACES_ITEMS,
    SUBSCALES,
    get_subscale,
    parse_conversation_line,
    parse_conversation_history,
)


class TestFacesData(unittest.TestCase):
    """Test FACES-IV item definitions and parsing with dummy data."""

    def test_item_count_and_subscales(self):
        self.assertEqual(len(FACES_ITEMS), 62)
        self.assertEqual(get_subscale(1), "Balanced Cohesion")
        self.assertEqual(get_subscale(2), "Balanced Flexibility")
        self.assertEqual(get_subscale(3), "Disengaged")
        self.assertEqual(get_subscale(4), "Enmeshed")
        self.assertEqual(get_subscale(5), "Rigid")
        self.assertEqual(get_subscale(6), "Chaotic")
        self.assertEqual(get_subscale(43), "Communication")
        self.assertEqual(get_subscale(53), "Satisfaction")
        self.assertEqual(get_subscale(999), "Unknown")

    def test_parse_conversation_line(self):
        dummy_line = 'S0_T1,daughter,father,conversation,こんにちは,Leda,Leda,Normal,"test rationale",0.5'
        parsed = parse_conversation_line(dummy_line)

        self.assertEqual(parsed["step"], "S0_T1")
        self.assertEqual(parsed["speaker"], "daughter")
        self.assertEqual(parsed["target"], "father")
        self.assertEqual(parsed["type"], "conversation")
        self.assertEqual(parsed["text"], "こんにちは")

    def test_parse_conversation_history_skips_metadata(self):
        dummy_text = """
S0_T1,daughter,father,conversation,こんにちは,Leda,Leda,Normal,"rationale",0.5
[THERAPIST_ANALYSIS] This should be ignored
Determined target scores should also be ignored
S0_T2,father,daughter,conversation,やあ,Fenrir,Fenrir,Normal,"rationale",0.5
S1_T1,mother,daughter,conversation,おはよう,Kore,Kore,Normal,"rationale",0.5
"""
        sessions = parse_conversation_history(dummy_text)
        self.assertIn("S0", sessions)
        self.assertIn("S1", sessions)
        self.assertEqual(len(sessions["S0"]), 2)
        self.assertEqual(len(sessions["S1"]), 1)


if __name__ == "__main__":
    unittest.main()
