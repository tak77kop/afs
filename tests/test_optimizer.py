import unittest
import os
import sys

# Ensure tests import path is configured
from tests import SRC_DIR
sys.path.insert(0, os.path.join(SRC_DIR, "afs_therapist"))

from afs_therapist.gradient_optimizer import GradientOptimizer


class TestGradientOptimizer(unittest.TestCase):
    """Test gradient descent calculation with dummy data."""

    def setUp(self):
        self.optimizer = GradientOptimizer(
            omega_1=1.0,
            omega_2=1.0,
            omega_3=0.5,
            learning_rate_scaling=0.1,
        )

    def test_calculate_gradient_keys_and_ranges(self):
        dummy_pcts = {
            "Balanced Cohesion": 50.0,
            "Balanced Flexibility": 50.0,
            "Disengaged": 30.0,
            "Enmeshed": 40.0,
            "Rigid": 20.0,
            "Chaotic": 35.0,
            "Communication": 50.0,
        }
        x, y = 55.0, 57.5

        result = self.optimizer.calculate_gradient(dummy_pcts, x, y)

        expected_keys = {
            "Balanced Cohesion",
            "Balanced Flexibility",
            "Disengaged",
            "Enmeshed",
            "Rigid",
            "Chaotic",
            "Communication",
        }
        self.assertEqual(set(result.keys()), expected_keys)

        for key, val in result.items():
            self.assertIsInstance(val, float)
            self.assertGreaterEqual(val, 0.0, f"{key} should be non-negative")
            self.assertLessEqual(val, 100.0, f"{key} should not exceed 100.0")

    def test_calculate_gradient_extreme_bounds(self):
        # Test boundary behavior with high imbalance
        dummy_pcts = {
            "Balanced Cohesion": 10.0,
            "Balanced Flexibility": 10.0,
            "Disengaged": 90.0,
            "Enmeshed": 10.0,
            "Rigid": 90.0,
            "Chaotic": 10.0,
            "Communication": 10.0,
        }
        result = self.optimizer.calculate_gradient(dummy_pcts, 0.0, 0.0)
        self.assertIn("Balanced Cohesion", result)
        self.assertTrue(all(0.0 <= v <= 100.0 for v in result.values()))


if __name__ == "__main__":
    unittest.main()
