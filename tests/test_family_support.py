import unittest
import os
import sys
from unittest.mock import MagicMock

# Mock fcntl if running on non-POSIX systems like Windows
if "fcntl" not in sys.modules:
    try:
        import fcntl
    except ImportError:
        sys.modules["fcntl"] = MagicMock()

from tests import SRC_DIR
sys.path.insert(0, os.path.join(SRC_DIR, "afs_family"))

from afs_family.family_member_support import normalize_role_name


class TestFamilySupport(unittest.TestCase):
    """Test family member support utilities with dummy data."""

    def test_normalize_role_name_japanese_aliases(self):
        self.assertEqual(normalize_role_name("お父さん"), "father")
        self.assertEqual(normalize_role_name("パパ"), "father")
        self.assertEqual(normalize_role_name("お母さん"), "mother")
        self.assertEqual(normalize_role_name("ママ"), "mother")
        self.assertEqual(normalize_role_name("娘"), "daughter")
        self.assertEqual(normalize_role_name("夏菜"), "daughter")
        self.assertEqual(normalize_role_name("息子"), "son")

    def test_normalize_role_name_english(self):
        self.assertEqual(normalize_role_name("Father"), "father")
        self.assertEqual(normalize_role_name("mother"), "mother")
        self.assertEqual(normalize_role_name("Daughter "), "daughter")
        self.assertEqual(normalize_role_name(""), "")


if __name__ == "__main__":
    unittest.main()
