#!/usr/bin/env python3
"""Prepare first-run Shop secrets for the isolated MTProxy Docker deployment.

Does not modify Docker, Caddy, MTProxy, Pushok ID or WGDashboard. Existing
secrets and configuration are never overwritten. Uses Python stdlib only.
"""
import base64
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_URL = "https://pushok.shop/shop"


def ensure_env(root: Path = ROOT) -> bool:
    path = root / ".env"
    if path.exists() or path.is_symlink():
        return False
    admin_token = secrets.token_urlsafe(48)
    fernet_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    contents = (
        "# Private, generated once for the existing MTProxy/Caddy host.\n"
        f"PUBLIC_URL={PUBLIC_URL}\n"
        f"ADMIN_TOKEN={admin_token}\n"
        f"ENCRYPTION_KEY={fernet_key}\n"
        "BOT_TOKEN=\n"
        "BOT_USERNAME=pushok_app\n"
        'SUPPORT_TEXT="Telegram: @pushok_app"\n'
        "TERMS_TEXT=\n"
        "PAYMENT_MODE=manual\n"
        "BACKGROUND_TASKS=true\n"
        "# Do not enable MTProxy-Shop binding until both servers are configured.\n"
        "FLOW_SHOP_BRIDGE_SECRET=\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as stream:
        stream.write(contents)
    return True


if __name__ == "__main__":
    try:
        created = ensure_env()
    except FileExistsError:
        created = False
    if created:
        print("Created private .env (0600); no values printed. Trial policy remains disabled.")
    else:
        print("Existing .env preserved; review PUBLIC_URL privately before starting Shop.")
    print("This command has not installed or restarted any service.")
