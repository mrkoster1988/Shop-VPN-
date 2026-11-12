#!/usr/bin/env python3
"""Подготовка конфигурации custom_emoji_id после публикации набора в @Stickers.

Читает реальные Telegram IDs, полученные владельцем, и сверяет их с
`manifest.json` из успешного GitHub Actions. Не обращается к Telegram и
не включает функцию автоматически.
"""
import argparse
import json
import re
from pathlib import Path

VALID_ID = re.compile(r"[0-9]{5,32}\Z")


def validate(manifest, submitted):
    expected = set(manifest.get("actions", {}))
    if not expected:
        raise ValueError("Пустая карта действий в манифесте")
    if not isinstance(submitted, dict):
        raise ValueError("Требуется JSON-объект action → custom_emoji_id")
    unexpected = set(submitted) - expected
    if unexpected:
        raise ValueError("Неизвестные действия: " + ", ".join(sorted(unexpected)))
    for action, value in submitted.items():
        if not isinstance(value, str) or not VALID_ID.fullmatch(value):
            raise ValueError(f"Некорректный custom_emoji_id для {action}")
    return {key: submitted[key] for key in sorted(submitted)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--ids", required=True, type=Path, help="Файл action → реальный ID из Telegram")
    p.add_argument("--output", required=True, type=Path, help="Готовый JSON для env, НЕ коммитить реальные настройки в main")
    a = p.parse_args()
    manifest = json.loads(a.manifest.read_text(encoding="utf-8"))
    ids = json.loads(a.ids.read_text(encoding="utf-8"))
    mapping = validate(manifest, ids)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    a.output.chmod(0o600)
    print(f"Проверены {len(mapping)} ID. Конфигурация сохранена локально: {a.output}.")
    print("ID должны быть проверены живым Telegram API до PUSHOK_BUTTON_ICONS_ENABLED=1")


if __name__ == "__main__":
    main()
