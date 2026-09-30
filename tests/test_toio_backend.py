import unittest
import os
import sys
import tempfile
import json

from tests import SRC_DIR
sys.path.insert(0, os.path.join(SRC_DIR, "afs_toio"))

from afs_toio.toio_backend import clamp, best_addr_name, load_role_toio_map


class DummyDev:
    def __init__(self, address=None, interface_address=None):
        if address:
            self.address = address
        if interface_address:
            class Interface:
                pass
            self.interface = Interface()
            self.interface.address = interface_address


class TestToioBackend(unittest.TestCase):
    """Test toio logic utilities with dummy data."""

    def test_clamp(self):
        self.assertEqual(clamp(15, 0, 10), 10)
        self.assertEqual(clamp(-5, 0, 10), 0)
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(0, 0, 10), 0)
        self.assertEqual(clamp(10, 0, 10), 10)

    def test_best_addr_name(self):
        dev1 = DummyDev(address="AA:BB:CC:DD:EE:FF")
        addr1, name1 = best_addr_name(dev1)
        self.assertEqual(addr1, "AA:BB:CC:DD:EE:FF")

        dev2 = DummyDev(interface_address="11:22:33:44:55:66")
        addr2, name2 = best_addr_name(dev2)
        self.assertEqual(addr2, "11:22:33:44:55:66")

    def test_load_role_toio_map(self):
        dummy_config = {
            "toio_speaker_match": [
                {"role": "Father", "toio_id": "F_TOIO_01"},
                {"role": "Mother", "toio_id": "M_TOIO_02"},
            ]
        }
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(dummy_config, f)
            temp_path = f.name

        try:
            mapping = load_role_toio_map(temp_path)
            self.assertEqual(mapping.get("father"), "F_TOIO_01")
            self.assertEqual(mapping.get("mother"), "M_TOIO_02")
            self.assertIsNone(mapping.get("daughter"))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
