import unittest
import os
import py_compile

from tests import REPO_ROOT, SRC_DIR


class TestSyntaxCompile(unittest.TestCase):
    """Test that all Python source files in the repository compile cleanly without syntax errors."""

    def test_all_python_files_compile(self):
        py_files = []
        for search_dir in [SRC_DIR, os.path.join(REPO_ROOT, "tests")]:
            for root, dirs, files in os.walk(search_dir):
                for f in files:
                    if f.endswith(".py"):
                        py_files.append(os.path.join(root, f))

        self.assertGreater(len(py_files), 10, "Should find at least 10 Python files")

        failures = []
        for path in py_files:
            try:
                py_compile.compile(path, doraise=True)
            except Exception as e:
                failures.append(f"{path}: {e}")

        self.assertEqual(failures, [], f"Syntax errors detected in {len(failures)} file(s)")


if __name__ == "__main__":
    unittest.main()
