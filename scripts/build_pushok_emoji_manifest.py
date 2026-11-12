#!/usr/bin/env python3
"""Создать проверяемую карту emoji-assets для ручной регистрации через @Stickers.

Не хранит Telegram custom_emoji_id и не делает сетевых вызовов.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

ACTION_ICON_MAP = {
  "buy": "cart",
  "account": "user",
  "support": "support",
  "terms": "rules",
  "link1": "link",
  "link2": "link"
}


def build_manifest(directory: Path) -> dict:
    icons = {}
    for file in sorted(directory.glob("*.png")):
        data = file.read_bytes()
        icons[file.stem] = {
            "filename": file.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    if not icons:
        raise ValueError("Нет экспортированных PNG-файлов")
    missing = sorted(set(ACTION_ICON_MAP.values()) - set(icons))
    if missing:
        raise ValueError("Отсутствуют иконки действий: " + ", ".join(missing))
    return {
        "schema": "pushok-telegram-emoji-manifest-v1",
        "telegram_pack_published": False,
        "custom_emoji_ids": {},
        "icons": icons,
        "actions": {
            name: {"icon": icon, "file": icons[icon]["filename"]}
            for name, icon in ACTION_ICON_MAP.items()
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    data = build_manifest(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Карта готова:", len(data["icons"]), "файлов,", len(data["actions"]), "действий")


if __name__ == "__main__":
    main()
