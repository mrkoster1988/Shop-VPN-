"""Снимок коммерческой воронки магазина без изменения данных."""
from collections import Counter, defaultdict
import time
from fastapi import HTTPException
from .db import db

MAX_TIME = 32503680000
PAID_METHODS = ("manual", "yookassa", "stars")
PAID_STATUSES = ("paid", "active")


def _rate(numerator, denominator):
    return round(numerator * 100 / denominator, 1) if denominator else None


def report(start=None, end=None):
    now = int(time.time())
    if (start is None) != (end is None):
        raise HTTPException(422, "Укажите обе границы периода.")
    if start is None:
        end = now + 1
        start = max(0, end - 30 * 86400)
    if not 0 <= start < end <= MAX_TIME or end - start > 366 * 86400:
        raise HTTPException(422, "Период должен быть положительным и не превышать 366 дней.")

    with db() as conn:
        conn.execute("BEGIN")
        accounts = conn.execute(
            "SELECT count(*) FROM users WHERE created>=? AND created<?", (start, end)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id,user_id,protocol,kind,method,status,amount,currency,created "
            "FROM orders WHERE created>=? AND created<? AND method IN ('manual','yookassa','stars') "
            "ORDER BY created,id",
            (start, end),
        ).fetchall()
        statuses = Counter(row["status"] for row in rows)
        paid_rows = [row for row in rows if row["status"] in PAID_STATUSES]
        issued_count = sum(row["status"] == "active" for row in paid_rows)
        paid_buyers = {row["user_id"] for row in paid_rows if row["user_id"] is not None}
        paid_amount = defaultdict(int)
        for row in paid_rows:
            paid_amount[row["currency"]] += row["amount"]

        renewals = conn.execute(
            "SELECT count(*) FROM orders o WHERE o.created>=? AND o.created<? "
            "AND o.kind='purchase' AND o.method IN ('manual','yookassa','stars') "
            "AND o.status IN ('paid','active') AND EXISTS ("
            "SELECT 1 FROM orders previous WHERE previous.user_id=o.user_id "
            "AND previous.protocol=o.protocol AND previous.kind='purchase' "
            "AND previous.method IN ('manual','yookassa','stars') "
            "AND previous.status IN ('paid','active') AND previous.created<o.created)",
            (start, end),
        ).fetchone()[0]

        trial_rows = conn.execute(
            "SELECT tc.user_id,tc.created,t.status,"
            "EXISTS (SELECT 1 FROM orders p WHERE p.user_id=tc.user_id "
            "AND p.protocol=tc.protocol AND p.kind='purchase' "
            "AND p.method IN ('manual','yookassa','stars') "
            "AND p.status IN ('paid','active') AND p.created>tc.created) AS converted "
            "FROM trial_claims tc JOIN orders t ON t.id=tc.order_id "
            "WHERE tc.created>=? AND tc.created<? ORDER BY tc.created,tc.order_id",
            (start, end),
        ).fetchall()
        issued_trials = [row for row in trial_rows if row["status"] == "active"]
        converted_trials = sum(bool(row["converted"]) for row in issued_trials)

        return {
            "generated_at": now,
            "source": "order_creation_cohort_current_state",
            "period": {"start": start, "end": end},
            "accounts_registered": accounts,
            "orders": {
                "created": len(rows),
                "paid": len(paid_rows),
                "issued": issued_count,
                "awaiting_issuance": sum(row["status"] == "paid" for row in paid_rows),
                "renewals": renewals,
                "paid_buyers": len(paid_buyers),
                "payment_rate_percent": _rate(len(paid_rows), len(rows)),
                "issuance_rate_percent": _rate(issued_count, len(paid_rows)),
                "by_status": dict(sorted(statuses.items())),
                "confirmed_amount_by_currency": dict(sorted(paid_amount.items())),
            },
            "trials": {
                "claimed": len(trial_rows),
                "issued": len(issued_trials),
                "converted_to_paid": converted_trials,
                "conversion_rate_percent": _rate(converted_trials, len(issued_trials)),
            },
        }
