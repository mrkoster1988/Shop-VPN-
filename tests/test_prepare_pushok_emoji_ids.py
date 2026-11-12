"""Проверки ручного соответствия Telegram Custom Emoji ID."""
import importlib.util
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("idmap", ROOT / "scripts" / "prepare_pushok_emoji_ids.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class TestIdMapping(unittest.TestCase):
    def setUp(self):
        self.manifest = {"actions": {"buy": {"icon": "cart"}, "support": {"icon": "support"}}}

    def test_valid_id(self):
        self.assertEqual(module.validate(self.manifest, {"buy": "1234567890123456789"}),
                         {"buy": "1234567890123456789"})

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            module.validate(self.manifest, {"unknown": "1234567890123456789"})

    def test_bad_ids(self):
        for payload in ({"buy": 123456}, {"buy": "not-an-id"}, {"buy": ""}, ["12345"]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                module.validate(self.manifest, payload)


if __name__ == "__main__":
    unittest.main()
