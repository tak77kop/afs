import unittest
import os
import sys

from tests import SRC_DIR
sys.path.insert(0, os.path.join(SRC_DIR, "afs_therapist"))

from afs_therapist.faces_scoring import FacesScoreCalculator


class TestFacesScoreCalculator(unittest.TestCase):
    """Test FACES IV scoring and percentile conversion with dummy data."""

    def setUp(self):
        self.calculator = FacesScoreCalculator()

    def test_average_ratings_with_dummy_members(self):
        aggregated = {
            "father": {"1": 4.0, "2": 2.0, "3": 1.0},
            "mother": {"1": 2.0, "2": 4.0, "3": 5.0},
        }
        avg = self.calculator.average_ratings(aggregated)

        self.assertEqual(len(avg), 62)
        self.assertAlmostEqual(avg[1], 3.0)
        self.assertAlmostEqual(avg[2], 3.0)
        self.assertAlmostEqual(avg[3], 3.0)
        # Missing items should default to neutral 3.0
        self.assertAlmostEqual(avg[4], 3.0)

    def test_calculate_scores_structure_and_types(self):
        # All items rated as 3.0
        dummy_ratings = {i: 3.0 for i in range(1, 63)}
        result = self.calculator.calculate_scores(dummy_ratings)

        self.assertIn("pcts", result)
        self.assertIn("x", result)
        self.assertIn("y", result)
        self.assertIn("coh_ratio", result)
        self.assertIn("flex_ratio", result)
        self.assertIn("tot_ratio", result)

        self.assertIsInstance(result["x"], float)
        self.assertIsInstance(result["y"], float)

        pcts = result["pcts"]
        for scale in ["Balanced Cohesion", "Balanced Flexibility", "Disengaged", "Enmeshed", "Rigid", "Chaotic", "Communication"]:
            self.assertIn(scale, pcts)
            self.assertGreaterEqual(pcts[scale], 0.0)
            self.assertLessEqual(pcts[scale], 100.0)


if __name__ == "__main__":
    unittest.main()
