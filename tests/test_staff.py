"""Проверки адресов электронной почты учётных записей сотрудников."""
import pytest
from fastapi import HTTPException
from app import staff
from app.db import one
from test_shop import client


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("owner@example.com", "owner@example.com"),
        ("  Owner+shop@Example.COM  ", "owner+shop@example.com"),
        ("first.last@sub.example.org", "first.last@sub.example.org"),
    ],
)
def test_clean_email_accepts_normal_addresses(email, expected):
    assert staff.clean_email(email) == expected


@pytest.mark.parametrize(
    "email",
    [
        "owner name@example.com",
        "owner@example com",
        "owner@@example.com",
        "owner@example",
        "@example.com",
        "owner@.com",
    ],
)
def test_clean_email_rejects_malformed_addresses(email):
    with pytest.raises(HTTPException) as error:
        staff.clean_email(email)
    assert error.value.status_code == 422


def test_bootstrap_rejects_malformed_email_without_creating_owner(client):
    response = client.post(
        "/api/admin/auth/bootstrap",
        headers={"Authorization": "Bearer " + "a" * 48},
        json={"email": "owner name@example.com", "password": "long-owner-password"},
    )
    assert response.status_code == 422
    assert one("SELECT id FROM staff_users") is None


def test_bootstrap_accepts_normal_email(client):
    response = client.post(
        "/api/admin/auth/bootstrap",
        headers={"Authorization": "Bearer " + "a" * 48},
        json={"email": "owner@example.com", "password": "long-owner-password"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "owner"
    assert one("SELECT email FROM staff_users")["email"] == "owner@example.com"
