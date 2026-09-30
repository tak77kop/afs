import os
import sys

# Ensure src packages can be imported without installation
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")

for pkg in os.listdir(SRC_DIR):
    pkg_path = os.path.join(SRC_DIR, pkg)
    if os.path.isdir(pkg_path) and pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)
