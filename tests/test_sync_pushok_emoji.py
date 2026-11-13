"""Контракт Bot API, визуальная сверка и отсутствие побочных действий; ID синтетические."""
import copy
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest

from scripts import sync_pushok_emoji as sync
from app.pushok_button_icons import configured_icons, decorate, without_icons

TOKEN = "123456:" + "fixture_not_a_real_token_" * 2
PNG = b"\x89PNG\r\n\x1a\nfixture"


def pack():
    return {"name": "PushokIcons", "sticker_type": "custom_emoji", "stickers": [
        {"type": "custom_emoji", "set_name": "PushokIcons", "custom_emoji_id": str(10000000000000000 + i),
         "file_id": f"file_{i}", "file_unique_id": f"unique_{i}", "emoji": "🛒",
         "is_animated": False, "is_video": False}
        for i in range(12)]}


class FakeTelegram:
    def __init__(self):
        self.pack = pack()
        self.calls = []

    def call(self, method, data):
        self.calls.append((method, data))
        if method == "getStickerSet":
            return copy.deepcopy(self.pack)
        assert method == "getCustomEmojiStickers"
        return [copy.deepcopy(row) for row in reversed(self.pack["stickers"])
                if row["custom_emoji_id"] in data["custom_emoji_ids"]]

    def preview(self, file_id):
        assert file_id.startswith("file_")
        return "png", PNG


@pytest.fixture
def fetched(tmp_path):
    reader = FakeTelegram()
    folder = tmp_path / "fetch"
    folder.mkdir()
    sync.fetch(reader, folder)
    inventory = sync.read_json(folder / "inventory.json")
    mapping = sync.read_json(folder / "mapping.json")
    for i, action in enumerate(("buy", "account", "support")):
        mapping[action]["custom_emoji_id"] = inventory["stickers"][i]["custom_emoji_id"]
    return reader, folder, inventory, mapping


def test_fetch_gives_gallery_and_no_automatic_mapping(fetched):
    reader, folder, inventory, _ = fetched
    assert len(list(folder.glob("*.png"))) == 12
    assert len(inventory["stickers"]) == 12
    assert all(item["custom_emoji_id"] is None for item in sync.read_json(folder / "mapping.json").values())
    assert all(row["custom_emoji_id"] in (folder / "review.html").read_text() for row in inventory["stickers"])
    assert TOKEN not in "".join(p.read_text() for p in folder.glob("*.json"))
    assert (folder / "inventory.json").stat().st_mode & 0o777 == 0o600
    assert reader.calls == [("getStickerSet", {"name": "PushokIcons"})]


def test_prepare_validates_live_ids_and_config_preserves_menu(fetched, tmp_path, monkeypatch):
    reader, _, inventory, mapping = fetched
    # Перестановка в наборе не должна назначить другой значок кнопке.
    reader.pack["stickers"].reverse()
    sync.prepare(reader, inventory, mapping, tmp_path)
    config = sync.read_json(tmp_path / "button-icons.json")
    manifest = sync.read_json(tmp_path / "bindings.json")
    assert set(config) == {"buy", "account", "support"}
    assert manifest["actions"]["buy"]["icon"] == "cart"
    assert manifest["bot_button_permission_verified"] is False
    monkeypatch.setenv("PUSHOK_BUTTON_EMOJI_JSON", json.dumps(config))
    monkeypatch.setenv("PUSHOK_BUTTON_ICONS_ENABLED", "1")
    buttons = [{"text": "Купить", "web_app": {"url": "https://shop.example/shop"}},
               {"text": "Кабинет", "login_url": {"url": "https://shop.example/login"}},
               {"text": "Помощь", "callback_data": "support"}]
    markup = {"inline_keyboard": [[decorate(b, a) for b, a in zip(buttons, config)]]}
    assert without_icons(markup) == {"inline_keyboard": [buttons]}
    monkeypatch.setenv("PUSHOK_BUTTON_ICONS_ENABLED", "0")
    assert configured_icons() == {}
    assert decorate(buttons[0], "buy") == buttons[0]
    assert {method for method, _ in reader.calls} == {"getStickerSet", "getCustomEmojiStickers"}


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(name="OtherPack"), lambda p: p.update(sticker_type="regular"),
    lambda p: p["stickers"].pop(), lambda p: p["stickers"].append(p["stickers"][0]),
    lambda p: p["stickers"][0].update(custom_emoji_id=12345),
    lambda p: p["stickers"][0].update(custom_emoji_id="bad"),
    lambda p: p["stickers"][1].update(custom_emoji_id=p["stickers"][0]["custom_emoji_id"]),
    lambda p: p["stickers"][1].update(file_unique_id=p["stickers"][0]["file_unique_id"]),
    lambda p: p["stickers"][0].update(set_name="OtherPack"),
    lambda p: p["stickers"][0].update(file_id="../../token"),
    lambda p: p["stickers"][0].update(is_animated=True),
    lambda p: p["stickers"][0].update(emoji=[]),
])
def test_bad_pack_is_rejected(mutation):
    reader = FakeTelegram()
    mutation(reader.pack)
    with pytest.raises(sync.SyncError):
        sync.read_pack(reader)


@pytest.mark.parametrize("change", ["removed", "replaced", "foreign", "number", "action", "semantic", "duplicate", "empty"])
def test_prepare_rejects_stale_or_unreviewable_mapping(fetched, tmp_path, change):
    reader, _, inventory, mapping = fetched
    if change == "removed":
        reader.pack["stickers"][0]["custom_emoji_id"] = "999999999"
    elif change == "replaced":
        reader.pack["stickers"][0]["file_unique_id"] = "replaced_image"
    elif change in ("foreign", "number"):
        mapping["buy"]["custom_emoji_id"] = "999999999" if change == "foreign" else 12345
    elif change == "action":
        mapping["unknown"] = mapping["buy"]
    elif change == "semantic":
        mapping["buy"]["icon"] = "support"
    elif change == "duplicate":
        mapping["buy"]["custom_emoji_id"] = mapping["support"]["custom_emoji_id"]
    else:
        for row in mapping.values():
            row["custom_emoji_id"] = None
    with pytest.raises(sync.SyncError):
        sync.prepare(reader, inventory, mapping, tmp_path)
    assert not (tmp_path / "button-icons.json").exists()


@pytest.mark.parametrize("value", [[], None, [{"custom_emoji_id": "99999"}],
                                  [{"custom_emoji_id": [1]}] * 3])
def test_selected_ids_must_roundtrip_through_api(fetched, tmp_path, value):
    reader, _, inventory, mapping = fetched
    original = reader.call
    reader.call = lambda method, data: value if method == "getCustomEmojiStickers" else original(method, data)
    with pytest.raises(sync.SyncError):
        sync.prepare(reader, inventory, mapping, tmp_path)


def test_gallery_escapes_untrusted_emoji():
    rows = [{"custom_emoji_id": "12345", "preview": "12345.png", "emoji": '<script>alert("x")</script>'}]
    result = sync.gallery(rows)
    assert "<script>" not in result and "&lt;script&gt;" in result
    assert "default-src 'none'" in result


@pytest.mark.parametrize("failure", [HTTPError("https://example/" + TOKEN, 400, TOKEN, {}, io.BytesIO()),
                                     HTTPError("https://example/" + TOKEN, 429, TOKEN, {}, io.BytesIO()),
                                     URLError(TOKEN), TimeoutError(TOKEN)])
def test_network_errors_never_expose_token(failure):
    reader = sync.TelegramReader(TOKEN)
    reader._opener = Mock()
    reader._opener.open.side_effect = failure
    with pytest.raises(sync.SyncError) as exc:
        reader.call("getStickerSet", {"name": "PushokIcons"})
    assert TOKEN not in str(exc.value)
    assert "http" not in str(exc.value)


@pytest.mark.parametrize("raw", [b"bad", b"[]", b'{"ok":false}', b'{"ok":true}', b"x" * (sync.MAX_JSON + 1)])
def test_invalid_or_large_responses_fail(raw):
    reader = sync.TelegramReader(TOKEN)
    response = io.BytesIO(raw)
    response.status = 200
    reader._opener = Mock()
    reader._opener.open.return_value = response
    with pytest.raises(sync.SyncError):
        reader.call("getStickerSet", {"name": "PushokIcons"})


def test_transport_fixed_host_timeout_no_proxy_redirects_or_send_methods():
    reader = sync.TelegramReader(TOKEN)
    assert not any(hasattr(h, "proxies") and h.proxies for h in reader._opener.handlers)
    with pytest.raises(sync.SyncError):
        sync.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")
    with pytest.raises(sync.SyncError):
        reader.call("sendMessage", {})
    response = io.BytesIO(b'{"ok":true,"result":{}}')
    response.status = 200
    reader._opener = Mock()
    reader._opener.open.return_value = response
    reader.call("getStickerSet", {"name": "PushokIcons"})
    args, kwargs = reader._opener.open.call_args
    assert args[0].full_url == f"https://api.telegram.org/bot{TOKEN}/getStickerSet"
    assert json.loads(args[0].data) == {"name": "PushokIcons"}
    assert kwargs == {"timeout": 15}


@pytest.mark.parametrize("path", ["../secret.png", "https://evil.example/a.png", "emoji/../../a.png", "emoji/a.svg", "a.png?token=1"])
def test_file_paths_cannot_escape_telegram(path):
    reader = sync.TelegramReader(TOKEN)
    reader.call = lambda *args: {"file_path": path}
    reader._read = Mock()
    with pytest.raises(sync.SyncError):
        reader.preview("file_1")
    reader._read.assert_not_called()


def test_preview_checks_real_image_header():
    reader = sync.TelegramReader(TOKEN)
    reader.call = lambda *args: {"file_path": "stickers/file.png"}
    reader._read = lambda *args: b"<html>not an image</html>"
    with pytest.raises(sync.SyncError):
        reader.preview("file_1")
    reader._read = lambda *args: PNG
    assert reader.preview("file_1") == ("png", PNG)


def test_cli_needs_visual_review_and_never_overwrites(tmp_path, monkeypatch):
    token = Mock(side_effect=AssertionError("token must not be requested"))
    monkeypatch.setattr(sync.getpass, "getpass", token)
    args = ["prepare", "--inventory", "absent", "--mapping", "absent", "--output", str(tmp_path / "new")]
    assert sync.main(args) == 1
    assert sync.main(["fetch", "--output", str(tmp_path)]) == 1
    token.assert_not_called()


def test_cli_success_then_failed_fetch_has_no_partial_results(tmp_path, monkeypatch):
    reader = FakeTelegram()
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setattr(sync, "TelegramReader", lambda token: reader)
    output = tmp_path / "result"
    assert sync.main(["fetch", "--output", str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o700
    reader.preview = Mock(side_effect=sync.SyncError("fixture failure"))
    assert sync.main(["fetch", "--output", str(tmp_path / "failed")]) == 1
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob(".pushok-emoji-*"))


def test_no_token_without_terminal_fails_without_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setattr(sync.sys.stdin, "isatty", lambda: False)
    prompt = Mock(side_effect=AssertionError("no echo fallback"))
    monkeypatch.setattr(sync.getpass, "getpass", prompt)
    assert sync.main(["fetch", "--output", str(tmp_path / "out")]) == 1
    prompt.assert_not_called()
