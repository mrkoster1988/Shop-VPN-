"""Первые этапы заказа сохраняются атомарно и не подменяют старую историю."""
import time
import pytest
from app.db import db, execute, one, all_rows, init_db
from test_shop import client, register
from test_commerce_analytics import order


def buyer(client):
    register(client, "history@example.com")
    return one("SELECT id FROM users WHERE email='history@example.com'")["id"]


def test_transitions_keep_first_time_and_event_period(client):
    from app import commerce_analytics
    uid = buyer(client)
    before = int(time.time())
    order("history", uid, status="pending", created=1)
    assert all_rows("SELECT * FROM order_milestones") == []
    execute("UPDATE orders SET status='paid' WHERE id='history'")
    paid = one("SELECT * FROM order_milestones WHERE event='paid'")
    execute("UPDATE orders SET status='active' WHERE id='history'")
    initial = all_rows("SELECT * FROM order_milestones ORDER BY event")
    execute("UPDATE orders SET status='paid' WHERE id='history'")
    execute("UPDATE orders SET status='active' WHERE id='history'")
    assert all_rows("SELECT * FROM order_milestones ORDER BY event") == initial
    after = int(time.time())
    assert before <= paid["recorded_at"] <= after
    report = commerce_analytics.report(before, after + 1)
    assert report["orders"]["created"] == 0
    assert report["events"]["paid"] == report["events"]["issued"] == 1
    assert commerce_analytics.report(before - 1, before)["events"]["paid"] == 0


def test_rollback_has_no_event_and_free_access_is_not_payment(client):
    uid = buyer(client)
    order("rollback", uid, status="pending")
    with pytest.raises(RuntimeError):
        with db() as conn:
            conn.execute("UPDATE orders SET status='paid' WHERE id='rollback'")
            raise RuntimeError("Отмена транзакции")
    assert one("SELECT status FROM orders WHERE id='rollback'")["status"] == "pending"
    assert all_rows("SELECT * FROM order_milestones") == []
    order("trial-history", uid, kind="trial", method="trial", status="paid", amount=0)
    assert all_rows("SELECT * FROM order_milestones") == []


def test_migration_does_not_invent_history(client):
    uid = buyer(client)
    execute("DROP TRIGGER order_issued_insert")
    order("legacy", uid, status="active")
    started = one("SELECT value FROM meta WHERE key='order_milestones_started_at'")["value"]
    init_db()
    init_db()
    assert all_rows("SELECT * FROM order_milestones") == []
    assert one("SELECT value FROM meta WHERE key='order_milestones_started_at'")["value"] == started
