"""Безопасные, необязательные иконки Telegram-кнопок Pushok.

Telegram принимает только зарегистрированные custom_emoji_id; SVG/PNG не подходят.
Отсутствующая, повреждённая или неразрешённая конфигурация сохраняет обычное меню.
"""
import json
import os
import re

ALLOWED = frozenset(["buy","account","support","terms","link1","link2"])
_VALID_ID = re.compile(r"^[0-9]{5,32}$")
CONFIG_ENV = "PUSHOK_BUTTON_EMOJI_JSON"
ENABLED_ENV = "PUSHOK_BUTTON_ICONS_ENABLED"


def configured_icons():
    if os.getenv(ENABLED_ENV) != "1":
        return {}
    raw = os.getenv(CONFIG_ENV, "")
    if not raw or len(raw) > 4096:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value for key, value in data.items()
        if key in ALLOWED and isinstance(value, str)
        and _VALID_ID.fullmatch(value) is not None
    }


def decorate(button, action, icons=None):
    """Возвращает новый словарь кнопки, не меняя callback/url/настройки покупки."""
    icon_id = (configured_icons() if icons is None else icons).get(action)
    result = dict(button)
    if isinstance(icon_id, str) and _VALID_ID.fullmatch(icon_id):
        result["icon_custom_emoji_id"] = icon_id
    return result


def without_icons(markup):
    """Копия inline-меню без необязательных значков; None, если удалять нечего."""
    if not isinstance(markup, dict) or not isinstance(markup.get('inline_keyboard'), list):
        return None
    rows = markup['inline_keyboard']
    if any(not isinstance(row, list) or any(not isinstance(button, dict) for button in row) for row in rows):
        return None
    if not any('icon_custom_emoji_id' in button for row in rows for button in row):
        return None
    return {**markup, 'inline_keyboard': [
        [{key: value for key, value in button.items() if key != 'icon_custom_emoji_id'} for button in row]
        for row in rows]}
