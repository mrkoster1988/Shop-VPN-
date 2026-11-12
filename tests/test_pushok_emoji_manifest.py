"""Проверка соответствия пользовательских действий и подготовленных иконок."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts" / "build_pushok_emoji_manifest.py"
SPEC = importlib.util.spec_from_file_location("pushok_manifest", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class EmojiManifestTests(unittest.TestCase):
    def test_manifest_and_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for icon in set(module.ACTION_ICON_MAP.values()):
                (directory / f"{icon}.png").write_bytes(b"test-" + icon.encode())
            actual = module.build_manifest(directory)
            self.assertFalse(actual["telegram_pack_published"])
            self.assertEqual(actual["custom_emoji_ids"], {})
            self.assertEqual(set(actual["actions"]), set(module.ACTION_ICON_MAP))
            for item in actual["icons"].values():
                self.assertEqual(len(item["sha256"]), 64)

    def test_missing_asset_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                module.build_manifest(Path(tmp))


if __name__ == "__main__":
    unittest.main()
