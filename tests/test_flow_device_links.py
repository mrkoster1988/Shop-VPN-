"""Контрактные тесты персональных ссылок Flow; WG-ключи исключительно фиктивные."""
import asyncio
import time
from urllib.parse import urlsplit

import pytest
from test_devices import issued, add
from test_shop import register
from app import providers
from app.db import one, execute


CONFIG = "[Interface]\nPrivateKey = NOT_A_REAL_SECRET_TEST_ONLY\nAddress = 10.8.0.2/32\n\n[Peer]\nPublicKey = FAKE_TEST_PEER\nEndpoint = example.invalid:51820\nAllowedIPs = 0.0.0.0/0\n"


@pytest.fixture
def flow_device(issued, monkeypatch):
    client, issue, _ = issued
    async def provision(sub):
        return CONFIG
    monkeypatch.setattr(providers, "provision", provision)
    sub = issue("wg")
    device = add(client, sub["id"], "Мой телефон").json()
    asyncio.run(__import__("app.service", fromlist=["process_devices"]).process_devices())
    return client, sub, device


def change(client, did, action, version=0):
    return client.put("/api/devices/" + did + "/flow-link",
                      json={"action": action, "version": version})


def fetch(client, link):
    return client.get(urlsplit(link["url"]).path)


def test_issued_device_link_is_private_and_json(flow_device):
    client, sub, device = flow_device
    path = "/api/devices/" + device["id"] + "/flow-link"
    assert client.get(path).json() == {"enabled": False, "version": 0, "url": None}
    link = change(client, device["id"], "enable").json()
    assert link["url"].endswith("/flow/sub/" + link["url"].rsplit("/", 1)[-1])
    token = link["url"].rsplit("/", 1)[-1]
    row = one("SELECT * FROM flow_device_links")
    assert token not in str(row)
    assert row["token_hash"] and row["token_secret"] and row["revision"] == 1
    client.post("/api/auth/logout")
    result = fetch(client, link)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["referrer-policy"] == "no-referrer"
    doc = result.json()
    assert doc["schema"] == "pushok-flow-subscription" and doc["version"] == 1
    assert doc["state"] == "active" and doc["revision"] == 1
    assert doc["profiles"][0]["id"] == device["id"]
    assert doc["profiles"][0]["config"] == CONFIG
    assert "token" not in result.text and "server_id" not in result.text


def test_renewal_increments_revision_and_preserves_identity(flow_device):
    client, sub, device = flow_device
    link = change(client, device["id"], "enable").json()
    old = fetch(client, link).json()
    updated_expiry = sub["expires"] + 86400
    execute("UPDATE subscriptions SET expires=? WHERE id=?", (updated_expiry, sub["id"]))
    execute("UPDATE devices SET expires=? WHERE id=?", (updated_expiry, device["id"]))
    fresh = fetch(client, link).json()
    assert fresh["revision"] == old["revision"] + 1
    assert fresh["expires_at"] == updated_expiry
    assert fresh["profiles"][0]["id"] == old["profiles"][0]["id"]
    assert fetch(client, link).json() == fresh


def test_expiry_and_confirmed_revocation_are_distinct(flow_device):
    client, sub, device = flow_device
    link = change(client, device["id"], "enable").json()
    original = fetch(client, link).json()
    execute("UPDATE subscriptions SET expires=1 WHERE id=?", (sub["id"],))
    expired = fetch(client, link).json()
    assert expired["state"] == "expired" and expired["profiles"] == []
    assert expired["revision"] == original["revision"] + 1
    execute("UPDATE devices SET state='revoking' WHERE id=?", (device["id"],))
    assert fetch(client, link).status_code == 503
    execute("UPDATE devices SET state='revoked',secret=NULL WHERE id=?", (device["id"],))
    revoked = fetch(client, link).json()
    assert revoked["state"] == "revoked" and revoked["profiles"] == []
    assert revoked["revision"] == expired["revision"] + 1


def test_rotate_and_disable_invalidate_old_token(flow_device):
    client, sub, device = flow_device
    initial = change(client, device["id"], "enable").json()
    rotated = change(client, device["id"], "rotate", initial["version"]).json()
    assert fetch(client, initial).status_code == 404
    assert fetch(client, rotated).status_code == 200
    assert change(client, device["id"], "rotate", initial["version"]).status_code == 409
    disabled = change(client, device["id"], "disable", rotated["version"]).json()
    assert disabled["url"] is None
    assert fetch(client, rotated).status_code == 404
    assert one("SELECT token_hash,token_secret FROM flow_device_links") == {
        "token_hash": None, "token_secret": None}


def test_no_cross_account_access_or_non_wg_links(flow_device):
    client, sub, device = flow_device
    client.post("/api/auth/logout")
    assert client.get("/api/devices/" + device["id"] + "/flow-link").status_code == 401
    register(client, "second-flow@example.com")
    assert client.get("/api/devices/" + device["id"] + "/flow-link").status_code == 404
    assert change(client, device["id"], "enable").status_code == 404


def test_pending_device_and_invalid_input_fail_closed(flow_device):
    client, sub, device = flow_device
    link = change(client, device["id"], "enable").json()
    assert change(client, device["id"], "disable", link["version"],
                  ).status_code == 200
    assert client.put("/api/devices/" + device["id"] + "/flow-link",
                      json={"action": "enable", "version": True}).status_code == 422
    assert client.put("/api/devices/" + device["id"] + "/flow-link",
                      json={"action": "enable", "version": 2},
                      headers={"Origin": "https://evil.invalid"}).status_code == 403
    execute("UPDATE devices SET state='pending' WHERE id=?", (device["id"],))
    assert change(client, device["id"], "enable", 2).status_code == 409


def test_malformed_token_and_broken_config_do_not_leak(flow_device):
    client, sub, device = flow_device
    link = change(client, device["id"], "enable").json()
    for token in ("bad", "x" * 43):
        result = client.get("/flow/sub/" + token)
        assert result.status_code == 404 and result.headers["cache-control"] == "no-store"
    from app import config as c
    execute("UPDATE devices SET secret=? WHERE id=?", (c.encrypt("private-not-a-wireguard-config"), device["id"]))
    result = fetch(client, link)
    assert result.status_code == 503
    assert "private-not-a-wireguard-config" not in result.text
