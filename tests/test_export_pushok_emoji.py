"""Контракт конвертации Pushok Icons в статические эмодзи Telegram."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("emoji_export", ROOT / "scripts" / "export_pushok_emoji.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ExportEmojiTests(unittest.TestCase):
    def test_symbols_can_be_rendered_independently(self):
        sprite = ROOT / "app/static/pushok-icons.svg"
        vectors = module.extract_icons(sprite)
        self.assertGreaterEqual(len(vectors), 8)
        for name, xml in vectors.items():
            self.assertTrue(name.replace("_", "").replace("-", "").isalnum())
            self.assertIn(b'width="100"', xml)
            self.assertIn(b'height="100"', xml)
            self.assertIn(b'<svg', xml)
            self.assertNotIn(b'<symbol', xml)


if __name__ == "__main__":
    unittest.main()
