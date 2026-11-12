"""Контрактные проверки оригинальных SVG-иконок Pushok."""
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SPRITE = ROOT / "app/static/pushok-icons.svg"
VIEW = ROOT / "app/static/index.html"
REQUIRED = ["home","price","user","support"]


class TestPushokIcons(unittest.TestCase):
    def test_valid_svg_sprite_and_unique_symbols(self):
        root = ET.parse(SPRITE).getroot()
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        ids = [node.get("id") for node in root.iter() if node.tag.endswith("symbol")]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate icon symbols")
        self.assertTrue(ids)
        for icon in REQUIRED:
            self.assertIn("pi-" + icon, ids)

    def test_screen_references_sprite(self):
        source = VIEW.read_text(encoding="utf-8")
        self.assertIn("pushok-icons.svg", source)
        for icon in REQUIRED[:2]:
            self.assertIn(icon, source)


if __name__ == "__main__":
    unittest.main()
