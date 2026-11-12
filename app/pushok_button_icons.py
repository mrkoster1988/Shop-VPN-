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
