"""Проверки коммерческой сводки по снимку магазина."""
from app import config as c
from app.db import execute, one
from test_shop import client, register


def order(order_id, user_id, *, kind="purchase", method="manual", status="active",
          created=9600, amount=100, currency="RUB", protocol="xui"):
    execute(
        "INSERT INTO orders(id,user_id,protocol,plan,days,amount,currency,method,status,created,kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, user_id, protocol, "month", 30, amount, currency, method, status, created, kind),
    )


def test_commerce_report_counts_order_cohort_and_trial_conversion(client, monkeypatch):
    from app import commerce_analytics
    monkeypatch.setattr(commerce_analytics.time, "time", lambda: 10000)
    register(client, "buyer@example.com")
    user_id = one("SELECT id FROM users WHERE email='buyer@example.com'")["id"]
    execute("UPDATE users SET created=9600 WHERE id=?", (user_id,))

    order("trial", user_id, kind="trial", method="trial", created=9500, amount=0)
    execute(
        "INSERT INTO trial_claims(user_id,order_id,protocol,created,policy_version,terms_snapshot) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, "trial", "xui", 9500, 1, "Тестовые условия"),
    )
    order("first", user_id, created=9600, amount=100)
    order("renewal", user_id, status="paid", created=9700, amount=200)
    order("pending", user_id, status="pending", created=9800, amount=400)
    order("bonus", user_id, method="referral", created=9850, amount=0)

    before = one("SELECT count(*) AS n FROM orders")["n"]
    report = client.get(
        "/api/admin/commerce-analytics?start=9000&end=10000",
        headers={"Authorization": "Bearer " + c.ADMIN_TOKEN},
    )
    assert report.status_code == 200
    data = report.json()
    assert data["accounts_registered"] == 1
    assert data["orders"] == {
        "created": 3,
        "paid": 2,
        "issued": 1,
        "awaiting_issuance": 1,
        "renewals": 1,
        "paid_buyers": 1,
        "payment_rate_percent": 66.7,
        "issuance_rate_percent": 50.0,
        "by_status": {"active": 1, "paid": 1, "pending": 1},
        "confirmed_amount_by_currency": {"RUB": 300},
    }
    assert data["trials"] == {
        "claimed": 1,
        "issued": 1,
        "converted_to_paid": 1,
        "conversion_rate_percent": 100.0,
    }
    assert data["source"] == "order_creation_cohort_current_state"
    assert one("SELECT count(*) AS n FROM orders")["n"] == before


def test_commerce_report_defaults_to_thirty_days_and_requires_valid_period(client, monkeypatch):
    from app import commerce_analytics
    now = 30 * 86400 + 10000
    monkeypatch.setattr(commerce_analytics.time, "time", lambda: now)
    result = client.get(
        "/api/admin/commerce-analytics",
        headers={"Authorization": "Bearer " + c.ADMIN_TOKEN},
    )
    assert result.status_code == 200
    assert result.json()["period"] == {"start": 10000, "end": now + 1}
    assert client.get(
        "/api/admin/commerce-analytics?start=1",
        headers={"Authorization": "Bearer " + c.ADMIN_TOKEN},
    ).status_code == 422
    assert client.get(
        "/api/admin/commerce-analytics?start=0&end=31708801",
        headers={"Authorization": "Bearer " + c.ADMIN_TOKEN},
    ).status_code == 422
    assert client.get("/api/admin/commerce-analytics").status_code == 401

def test_finance_can_read_analytics_but_support_cannot(client):
    bootstrap = client.post(
        "/api/admin/auth/bootstrap",
        headers={"Authorization": "Bearer " + c.ADMIN_TOKEN},
        json={"email": "owner@example.com", "password": "long-owner-password"},
    )
    assert bootstrap.status_code == 200
    finance = client.post(
        "/api/admin/staff",
        json={"email": "finance@example.com", "password": "finance-password-123", "role": "finance"},
    )
    support = client.post(
        "/api/admin/staff",
        json={"email": "support@example.com", "password": "support-password-123", "role": "support"},
    )
    assert finance.status_code == support.status_code == 201

    client.post("/api/admin/auth/logout")
    assert client.post(
        "/api/admin/auth/login",
        json={"email": "finance@example.com", "password": "finance-password-123"},
    ).status_code == 200
    assert client.get("/api/admin/commerce-analytics").status_code == 200

    client.post("/api/admin/auth/logout")
    assert client.post(
        "/api/admin/auth/login",
        json={"email": "support@example.com", "password": "support-password-123"},
    ).status_code == 200
    assert client.get("/api/admin/commerce-analytics").status_code == 403
