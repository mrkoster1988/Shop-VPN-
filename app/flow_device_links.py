"""Персональные WireGuard-подписки устройств для Pushok Flow.

Только дополнительные устройства. Основной WG-профиль подключается отдельно:
не смешивать его идентичность и политику ротации с devices.
"""
import hashlib
import json
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

from . import config as c
from .db import db, one
from .security import current_user, digest
from .flow_subscription_contract import build_document, SubscriptionContractError

router = APIRouter()
PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


class FlowLinkUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["enable", "rotate", "disable"]
    version: int = Field(ge=0, strict=True)


def owned_device(conn, did, user):
    row = conn.execute(
        """SELECT d.*, s.user_id, s.protocol, s.state AS subscription_state,
                  s.expires AS subscription_expires, s.access_source
           FROM devices d JOIN subscriptions s ON s.id=d.subscription_id
           WHERE d.id=? AND s.user_id=?""", (did, user["id"])
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Устройство не найдено")
    if row["protocol"] != "wg" or row["access_source"] == "trial":
        raise HTTPException(409, "Ссылка Flow доступна для оплаченного WireGuard")
    return row


def metadata(link):
    if link is None:
        return {"enabled": False, "version": 0, "url": None}
    return {
        "enabled": bool(link["enabled"]),
        "version": link["version"],
        "url": c.PUBLIC_URL + "/flow/sub/" + c.decrypt(link["token_secret"])
               if link["enabled"] else None,
    }


@router.get("/api/devices/{did}/flow-link")
async def get_flow_link(did: str, user=Depends(current_user)):
    with db() as conn:
        owned_device(conn, did, user)
        link = conn.execute("SELECT * FROM flow_device_links WHERE device_id=?", (did,)).fetchone()
        return metadata(link)


@router.put("/api/devices/{did}/flow-link")
async def update_flow_link(did: str, data: FlowLinkUpdate, user=Depends(current_user)):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = owned_device(conn, did, user)
        current = conn.execute("SELECT * FROM flow_device_links WHERE device_id=?", (did,)).fetchone()
        if (current["version"] if current else 0) != data.version:
            raise HTTPException(409, "Настройки ссылки изменились")
        if data.action == "enable":
            if (device["state"] != "active" or device["subscription_state"] != "active"
                    or min(device["expires"], device["subscription_expires"]) <= time.time()
                    or not device["secret"] or device["error"]):
                raise HTTPException(409, "Дождитесь подтверждённой выдачи устройства")
        if data.action == "rotate" and (not current or not current["enabled"]):
            raise HTTPException(409, "Сначала включите ссылку")
        if data.action == "disable" and not current:
            return metadata(None)
        enabled = data.action != "disable"
        new_token = data.action == "rotate" or (enabled and (not current or not current["enabled"]))
        token = secrets.token_urlsafe(32) if new_token else None
        token_hash = digest(token) if new_token else (current["token_hash"] if current else None)
        token_secret = c.encrypt(token) if new_token else (current["token_secret"] if current else None)
        if not enabled:
            token_hash = token_secret = None
        conn.execute(
            """INSERT INTO flow_device_links
                  (device_id,token_hash,token_secret,enabled,version,revision,snapshot_hash,created)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 token_hash=excluded.token_hash,token_secret=excluded.token_secret,
                 enabled=excluded.enabled,version=excluded.version,
                 revision=excluded.revision,snapshot_hash=excluded.snapshot_hash""",
            (did, token_hash, token_secret, int(enabled), data.version+1,
             (current["revision"] + 1 if current else 1), None, int(time.time())),
        )
        updated = conn.execute("SELECT * FROM flow_device_links WHERE device_id=?", (did,)).fetchone()
        return metadata(updated)


def flow_unavailable(status=404):
    return JSONResponse(
        {"detail": "Подписка временно недоступна" if status == 503 else "Подписка недоступна"},
        status_code=status, headers=PRIVATE_HEADERS,
    )


@router.get("/flow/sub/{token}")
async def export_flow_link(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return flow_unavailable()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT l.*,d.name,d.state AS device_state,d.expires AS device_expires,
                      d.secret,d.error,s.protocol,s.state AS subscription_state,
                      s.expires AS subscription_expires,s.access_source
               FROM flow_device_links l
               JOIN devices d ON d.id=l.device_id
               JOIN subscriptions s ON s.id=d.subscription_id
               WHERE l.token_hash=? AND l.enabled=1""", (digest(token),)
        ).fetchone()
        if not row or row["protocol"] != "wg" or row["access_source"] == "trial":
            return flow_unavailable()
        now = int(time.time())
        expires = min(row["device_expires"], row["subscription_expires"])
        if row["device_state"] == "revoked":
            state = "revoked"
        elif row["device_state"] in ("pending", "revoking") or row["error"]:
            return flow_unavailable(503)
        elif row["device_state"] != "active":
            return flow_unavailable(503)
        elif row["subscription_state"] != "active" or expires <= now:
            state = "expired"
        else:
            state = "active"

        profiles = []
        if state == "active":
            if not row["secret"]:
                return flow_unavailable(503)
            try:
                config = c.decrypt(row["secret"])
            except Exception:
                return flow_unavailable(503)
            if (not isinstance(config, str) or not config.startswith("[Interface]")
                    or "[Peer]" not in config or len(config) > 16384):
                return flow_unavailable(503)
            profiles = [{
                "id": row["device_id"], "name": row["name"],
                "protocol": "wireguard", "config": config,
            }]
        payload = {
            "state": state, "expires_at": max(0, int(expires)),
            "profiles": profiles,
        }
        snapshot = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        revision = row["revision"]
        if row["snapshot_hash"] != snapshot:
            if row["snapshot_hash"] is not None:
                revision += 1
            conn.execute(
                "UPDATE flow_device_links SET revision=?,snapshot_hash=? WHERE device_id=?",
                (revision, snapshot, row["device_id"]),
            )
        try:
            document = build_document(revision=revision, **payload)
        except SubscriptionContractError:
            return flow_unavailable(503)
        return JSONResponse(document, headers=PRIVATE_HEADERS)
