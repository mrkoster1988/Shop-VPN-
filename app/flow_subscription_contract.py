"""Общий P0-контракт подписки Shop -> Pushok Flow (без сетевых побочных эффектов).

Модуль формирует и проверяет документ, но сам не авторизует устройство,
не читает секреты и не подтверждает отзыв peer на сервере.
"""
import json
from typing import Any

SCHEMA = "pushok-flow-subscription"
VERSION = 1
STATES = frozenset(("active", "expired", "revoked"))
MAX_PROFILES = 20


class SubscriptionContractError(ValueError):
    """Ошибка формата; сообщения не должны содержать конфигурации или ключи."""


def validate_document(document: Any) -> dict:
    """Проверка структуры P0; криптографию и синтаксис WG проверяет Flow."""
    if not isinstance(document, dict) or set(document) != {
        "schema", "version", "revision", "state", "expires_at", "profiles"
    }:
        raise SubscriptionContractError("Invalid document shape")
    if document["schema"] != SCHEMA or type(document["version"]) is not int or document["version"] != VERSION:
        raise SubscriptionContractError("Unsupported subscription schema")
    if type(document["revision"]) is not int or document["revision"] < 1:
        raise SubscriptionContractError("Invalid revision")
    if document["state"] not in STATES:
        raise SubscriptionContractError("Invalid state")
    if type(document["expires_at"]) is not int or document["expires_at"] < 0:
        raise SubscriptionContractError("Invalid expires_at")
    profiles = document["profiles"]
    if not isinstance(profiles, list):
        raise SubscriptionContractError("Invalid profiles")
    if document["state"] == "active":
        if not 1 <= len(profiles) <= MAX_PROFILES:
            raise SubscriptionContractError("Invalid active profile count")
    elif profiles:
        raise SubscriptionContractError("Inactive subscription cannot contain profiles")
    ids = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != {"id", "name", "protocol", "config"}:
            raise SubscriptionContractError("Invalid profile shape")
        if (not isinstance(profile["id"], str) or not 1 <= len(profile["id"]) <= 128
                or profile["id"] in ids):
            raise SubscriptionContractError("Invalid profile id")
        ids.add(profile["id"])
        if not isinstance(profile["name"], str) or not 1 <= len(profile["name"]) <= 128:
            raise SubscriptionContractError("Invalid profile name")
        if profile["protocol"] != "wireguard":
            raise SubscriptionContractError("Unsupported protocol")
        if not isinstance(profile["config"], str) or not 1 <= len(profile["config"]) <= 65536:
            raise SubscriptionContractError("Invalid config")
    return document


def build_document(*, revision: int, state: str, expires_at: int,
                   profiles: list[dict]) -> dict:
    """Собирает отдельный публичный документ из уже авторизованных данных."""
    return validate_document({
        "schema": SCHEMA, "version": VERSION, "revision": revision,
        "state": state, "expires_at": expires_at, "profiles": profiles
    })


def compare_revision(previous: dict, incoming: dict) -> str:
    """Решение для обновления: apply, unchanged, stale или conflict.

    При той же ревизии различающийся документ — конфликт, не обновление.
    Никогда не понижаем revision, в том числе после получения revoked.
    """
    validate_document(previous)
    validate_document(incoming)
    if incoming["revision"] < previous["revision"]:
        return "stale"
    if incoming["revision"] > previous["revision"]:
        return "apply"
    normalized = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "unchanged" if normalized(previous) == normalized(incoming) else "conflict"
