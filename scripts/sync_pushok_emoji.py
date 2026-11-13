#!/usr/bin/env python3
"""Чтение PushokIcons и подготовка проверенной карты кнопок, без отправки сообщений.

Токен читается из BOT_TOKEN или скрытого ввода. Команда fetch сохраняет локальную
галерею; prepare повторно сверяет выбранные по изображениям ID с Telegram.
Ни одна команда не включает иконки и не изменяет конфигурацию работающего Shop.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
from http.client import HTTPException
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    from .build_pushok_emoji_manifest import ACTION_ICON_MAP
except ImportError:
    from build_pushok_emoji_manifest import ACTION_ICON_MAP

PACK = "PushokIcons"
EXPECTED_COUNT = 12
SCHEMA = "pushok-telegram-inventory-v1"
ID = re.compile(r"[0-9]{5,32}\Z")
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
TOKEN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,128}\Z")
MAX_JSON = 256 * 1024
MAX_IMAGE = 1024 * 1024


class SyncError(ValueError):
    """Сообщение для оператора, не содержащее ответ Telegram или URL с токеном."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("Telegram вернул перенаправление; запрос остановлен")


class TelegramReader:
    def __init__(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise SyncError("Некорректный BOT_TOKEN; используйте секрет или скрытый ввод")
        self._token = token
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def _read(self, url, limit, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise SyncError("Telegram не подтвердил запрос")
                data = response.read(limit + 1)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 429:
                raise SyncError("Лимит Telegram: повторите команду позднее") from None
            raise SyncError("Telegram отклонил запрос; проверьте токен и доступ к набору") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise SyncError("Не удалось прочитать Telegram; проверьте сеть и повторите команду") from None
        if len(data) > limit:
            raise SyncError("Ответ Telegram превышает допустимый размер")
        return data

    def call(self, method, payload):
        if method not in {"getStickerSet", "getCustomEmojiStickers", "getFile"}:
            raise SyncError("Разрешены только методы чтения набора")
        raw = self._read(f"https://api.telegram.org/bot{self._token}/{method}", MAX_JSON, payload)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SyncError("Некорректный JSON от Telegram") from None
        if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
            raise SyncError("Telegram не подтвердил получение данных")
        return data["result"]

    def preview(self, file_id):
        info = self.call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if (not isinstance(path, str) or len(path) > 256
                or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.(?:png|jpg|webp)", path)):
            raise SyncError("Нет безопасного статичного изображения для сверки")
        data = self._read(f"https://api.telegram.org/file/bot{self._token}/{path}", MAX_IMAGE)
        # Сохраняем только растровые форматы; HTML/SVG не допускаются в галерею.
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = "jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            suffix = "webp"
        else:
            raise SyncError("Telegram вернул неподдерживаемое изображение")
        return suffix, data


def sticker_rows(stickers):
    if not isinstance(stickers, list) or len(stickers) != EXPECTED_COUNT:
        raise SyncError("Ожидались 12 эмодзи PushokIcons; проверьте изменение состава набора")
    rows, ids, unique_ids = [], set(), set()
    for item in stickers:
        if not isinstance(item, dict) or item.get("type") != "custom_emoji" or item.get("set_name") != PACK:
            raise SyncError("Эмодзи не принадлежит набору PushokIcons")
        cid, fid, uid = (item.get(key) for key in ("custom_emoji_id", "file_id", "file_unique_id"))
        emoji = item.get("emoji", "")
        if not isinstance(cid, str) or not ID.fullmatch(cid) or cid in ids:
            raise SyncError("Некорректный или повторяющийся custom_emoji_id")
        if any(not isinstance(v, str) or not FILE_ID.fullmatch(v) for v in (fid, uid)) or uid in unique_ids:
            raise SyncError("Некорректный или повторяющийся идентификатор файла")
        if not isinstance(emoji, str) or len(emoji) > 64:
            raise SyncError("Некорректная подпись эмодзи")
        thumbnail = item.get("thumbnail")
        preview_id = thumbnail.get("file_id") if isinstance(thumbnail, dict) else None
        if preview_id is None and item.get("is_animated") is False and item.get("is_video") is False:
            preview_id = fid
        if not isinstance(preview_id, str) or not FILE_ID.fullmatch(preview_id):
            raise SyncError("У эмодзи нет статичного изображения для проверки")
        rows.append({"custom_emoji_id": cid, "emoji": emoji, "file_id": fid,
                     "file_unique_id": uid, "preview_file_id": preview_id})
        ids.add(cid)
        unique_ids.add(uid)
    return rows


def read_pack(reader):
    pack = reader.call("getStickerSet", {"name": PACK})
    if not isinstance(pack, dict) or pack.get("name") != PACK or pack.get("sticker_type") != "custom_emoji":
        raise SyncError("Telegram вернул другой набор или обычные стикеры")
    return sticker_rows(pack.get("stickers"))


def write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise SyncError("Локальный JSON превышает допустимый размер")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise SyncError("Некорректный локальный JSON") from None


def gallery(rows):
    cards = []
    for row in rows:
        cid = row["custom_emoji_id"]
        cards.append(f'<article><img src="{row["preview"]}" alt="Изображение эмодзи {cid}">'
                     f'<p>{html.escape(row["emoji"])} · <code>{cid}</code></p></article>')
    return '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'">
<title>PushokIcons — сверка значков</title><style>
:root{color-scheme:dark}body{font:16px/1.6 system-ui;background:#0b1823;color:#edf8f7;margin:0;padding:clamp(20px,5vw,64px)}
main{max-width:1100px;margin:auto}h1{font-size:clamp(28px,5vw,42px);margin-bottom:8px}p{overflow-wrap:anywhere}
.tag{color:#62dec9}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:16px;margin:28px 0}
article{border:1px solid #355c66;background:#132a37;border-radius:18px;padding:24px;text-align:center}
img{width:100px;height:100px;object-fit:contain}code{font-size:13px}footer{color:#b4cbcf}</style>
<main><p class="tag">PUSHOK · TELEGRAM</p><h1>Сверка 12 значков</h1>
<p>Выберите по изображению значки покупки, кабинета, поддержки и условий.
Скопируйте ID в mapping.json. Если подходящего значка нет, оставьте null.
Порядок и обычные эмодзи не определяют назначение автоматически.</p><section>''' + "".join(cards) + '''</section>
<footer>Это локальная копия PushokIcons. Настройки магазина не изменены.
Право бота и отображение на Android, iOS и Desktop проверяются отдельно.</footer></main></html>'''


def fetch(reader, directory):
    rows = read_pack(reader)
    for row in rows:
        suffix, data = reader.preview(row["preview_file_id"])
        row["preview"] = f'{row["custom_emoji_id"]}.{suffix}'
        row["preview_sha256"] = hashlib.sha256(data).hexdigest()
        (directory / row["preview"]).write_bytes(data)
    inventory = {"schema": SCHEMA, "set_name": PACK, "fetched_at": datetime.now(timezone.utc).isoformat(),
                 "stickers": rows}
    write_json(directory / "inventory.json", inventory)
    write_json(directory / "mapping.json", {action: {"icon": icon, "custom_emoji_id": None}
                                           for action, icon in ACTION_ICON_MAP.items()})
    (directory / "review.html").write_text(gallery(rows), encoding="utf-8")


def bindings(inventory, mapping, current):
    if not isinstance(inventory, dict) or inventory.get("schema") != SCHEMA or inventory.get("set_name") != PACK:
        raise SyncError("Требуется inventory.json команды fetch")
    saved = inventory.get("stickers")
    if not isinstance(saved, list) or len(saved) != EXPECTED_COUNT or any(not isinstance(r, dict) for r in saved):
        raise SyncError("Повреждён локальный снимок набора")
    old = {r.get("custom_emoji_id"): r for r in saved if isinstance(r.get("custom_emoji_id"), str)}
    live = {r["custom_emoji_id"]: r for r in current}
    if len(old) != EXPECTED_COUNT or set(old) != set(live):
        raise SyncError("Состав набора изменился; повторите fetch и визуальную сверку")
    if any(old[cid].get("file_unique_id") != live[cid]["file_unique_id"] for cid in live):
        raise SyncError("Изображения набора изменились; повторите fetch и визуальную сверку")
    if not isinstance(mapping, dict) or set(mapping) - set(ACTION_ICON_MAP):
        raise SyncError("В карте есть неизвестные действия")
    result = {}
    for action, item in mapping.items():
        if (not isinstance(item, dict) or set(item) != {"icon", "custom_emoji_id"}
                or item.get("icon") != ACTION_ICON_MAP[action]):
            raise SyncError("Некорректная семантика значка для действия")
        cid = item["custom_emoji_id"]
        if cid is None:
            continue
        if not isinstance(cid, str) or cid not in live:
            raise SyncError("Выбранный ID отсутствует в проверенном наборе PushokIcons")
        result[action] = {"icon": item["icon"], "custom_emoji_id": cid,
                          "emoji": live[cid]["emoji"], "file_unique_id": live[cid]["file_unique_id"]}
    if not result:
        raise SyncError("Выберите хотя бы один значок по изображению; остальные оставьте null")
    semantics = {}
    for row in result.values():
        previous = semantics.setdefault(row["custom_emoji_id"], row["icon"])
        if previous != row["icon"]:
            raise SyncError("Один ID назначен разным по смыслу значкам; проверьте карту")
    return result


def prepare(reader, inventory, mapping, directory):
    current = read_pack(reader)
    chosen = bindings(inventory, mapping, current)
    ids = sorted({r["custom_emoji_id"] for r in chosen.values()})
    verified = reader.call("getCustomEmojiStickers", {"custom_emoji_ids": ids})
    if not isinstance(verified, list) or len(verified) != len(ids):
        raise SyncError("Telegram не подтвердил все выбранные ID")
    live = {r["custom_emoji_id"]: r for r in current}
    seen = set()
    for row in verified:
        cid = row.get("custom_emoji_id") if isinstance(row, dict) else None
        if (not isinstance(cid, str) or cid not in ids or cid in seen
                or row.get("type") != "custom_emoji" or row.get("set_name") != PACK
                or row.get("file_unique_id") != live[cid]["file_unique_id"]):
            raise SyncError("Telegram вернул несовпадающие данные выбранного эмодзи")
        seen.add(cid)
    config = {action: row["custom_emoji_id"] for action, row in chosen.items()}
    write_json(directory / "button-icons.json", config)
    write_json(directory / "bindings.json", {
        "schema": "pushok-telegram-bindings-v1", "set_name": PACK,
        "verified_at": datetime.now(timezone.utc).isoformat(), "actions": chosen,
        "bot_button_permission_verified": False, "native_client_acceptance": "pending",
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pull = commands.add_parser("fetch", help="Загрузить 12 изображений и реальные ID")
    pull.add_argument("--output", required=True, type=Path, help="Новый локальный каталог")
    build = commands.add_parser("prepare", help="Проверить выбранные ID и создать JSON для Shop")
    build.add_argument("--inventory", required=True, type=Path)
    build.add_argument("--mapping", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path, help="Новый локальный каталог")
    build.add_argument("--images-reviewed", action="store_true", help="Изображения сверены по смыслу кнопок")
    args = parser.parse_args(argv)
    stage = None
    try:
        if args.command == "prepare" and not args.images_reviewed:
            raise SyncError("Сначала сверьте изображения в review.html; затем укажите --images-reviewed")
        if args.output.exists() or args.output.is_symlink():
            raise SyncError("Каталог результата уже существует; укажите новый каталог")
        token = os.getenv("BOT_TOKEN")
        if not token:
            if not sys.stdin.isatty():
                raise SyncError("BOT_TOKEN отсутствует: запустите в терминале для скрытого ввода")
            token = getpass.getpass("Токен тестового бота (скрытый ввод): ")
        reader = TelegramReader(token)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".pushok-emoji-", dir=args.output.parent))
        if args.command == "fetch":
            fetch(reader, stage)
        else:
            prepare(reader, read_json(args.inventory), read_json(args.mapping), stage)
        # Публикуем результат только после успешного получения и проверки всех данных.
        stage.rename(args.output)
        stage = None
        print("Готово: " + ("откройте review.html и заполните mapping.json" if args.command == "fetch"
                           else "карта button-icons.json проверена через Telegram"))
        print("Иконки не включены. Проверка прав бота и клиентов Telegram остаётся отдельным этапом.")
        return 0
    except (SyncError, OSError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) if isinstance(exc, SyncError) else "Операция прервана или локальные файлы недоступны")
        return 1
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
