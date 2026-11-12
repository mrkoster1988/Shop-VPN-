"""Регрессии безопасных опциональных значков Telegram на кнопках Pushok."""
import os
import unittest
from unittest.mock import patch
from app.pushok_button_icons import configured_icons, decorate


class TestButtonIcons(unittest.TestCase):
    def test_disabled_keeps_buttons_unchanged(self):
        with patch.dict(os.environ, {"PUSHOK_BUTTON_ICONS_ENABLED": "0", "PUSHOK_BUTTON_EMOJI_JSON": '{"buy":"123456789012"}'}):
            self.assertEqual(configured_icons(), {})
            original = {"text": "Кнопка", "callback_data": "start"}
            self.assertEqual(decorate(original, "buy"), original)

    def test_registered_numeric_id_is_added(self):
        with patch.dict(os.environ, {"PUSHOK_BUTTON_ICONS_ENABLED": "1", "PUSHOK_BUTTON_EMOJI_JSON": '{"buy":"1234567890123456789"}'}):
            original = {"text": "Кнопка", "callback_data": "start"}
            actual = decorate(original, "buy")
            self.assertEqual(actual["icon_custom_emoji_id"], "1234567890123456789")
            self.assertEqual(actual["callback_data"], "start")
            self.assertNotIn("icon_custom_emoji_id", original)

    def test_bad_config_never_breaks_menu(self):
        for raw in ("oops", '[]', '{"buy":1}', '{"buy":"not-an-id"}', '{"unknown":"123456789"}'):
            with self.subTest(raw=raw), patch.dict(os.environ, {"PUSHOK_BUTTON_ICONS_ENABLED": "1", "PUSHOK_BUTTON_EMOJI_JSON": raw}):
                self.assertEqual(configured_icons(), {})
                self.assertEqual(decorate({"text": "Ок"}, "buy"), {"text": "Ок"})

    def test_shop_purchase_button_keeps_original_action(self):
        with patch.dict(os.environ, {
            "PUSHOK_BUTTON_ICONS_ENABLED": "1",
            "PUSHOK_BUTTON_EMOJI_JSON": '{"buy":"123456789012","support":"123456789013"}',
        }):
            buy = {"text": "Купить", "web_app": {"url": "https://example.org/shop"}}
            result = decorate(buy, "buy")
            self.assertEqual(result["web_app"], buy["web_app"])
            self.assertEqual(result["icon_custom_emoji_id"], "123456789012")
            self.assertEqual(decorate({"text": "Кабинет", "url": "https://example.org"}, "account"),
                             {"text": "Кабинет", "url": "https://example.org"})


if __name__ == "__main__":
    unittest.main()
