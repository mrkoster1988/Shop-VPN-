import sqlite3
from contextlib import contextmanager
from . import config as c

@contextmanager
def db():
    conn = sqlite3.connect(c.DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT,
          telegram_id INTEGER UNIQUE, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires INTEGER);
        CREATE TABLE IF NOT EXISTS tickets (
          token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires INTEGER);
        CREATE TABLE IF NOT EXISTS plans (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, days INTEGER NOT NULL CHECK(days > 0),
          price INTEGER NOT NULL CHECK(price > 0), stars INTEGER NOT NULL CHECK(stars > 0),
          enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
          version INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL, updated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS plan_changes (
          id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id), created INTEGER NOT NULL,
          before_data TEXT NOT NULL, after_data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), protocol TEXT NOT NULL,
          plan TEXT NOT NULL, days INTEGER NOT NULL, amount INTEGER NOT NULL,
          currency TEXT NOT NULL DEFAULT 'RUB', method TEXT NOT NULL DEFAULT 'manual', checkout_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL,
          payment_id TEXT UNIQUE, payment_url TEXT, receipt_email TEXT, target_expires INTEGER,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
          error TEXT, notified INTEGER NOT NULL DEFAULT 0,
          plan_version INTEGER NOT NULL DEFAULT 1, plan_name TEXT);
        CREATE TABLE IF NOT EXISTS subscriptions (
          id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), protocol TEXT NOT NULL,
          expires INTEGER NOT NULL, state TEXT NOT NULL, secret TEXT,
          reminder INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id, protocol));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, created INTEGER NOT NULL, action TEXT NOT NULL, order_id TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
        ''')
        # Additive migration for installations created before editable tariffs.
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(orders)')}
        if 'plan_version' not in columns:
            conn.execute('ALTER TABLE orders ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 1')
        if 'plan_name' not in columns:
            conn.execute('ALTER TABLE orders ADD COLUMN plan_name TEXT')
        for position, plan in enumerate(c.PLANS):
            conn.execute('INSERT OR IGNORE INTO plans(id,name,days,price,stars,position) VALUES (?,?,?,?,?,?)',
                         (plan['id'], plan['name'], plan['days'], plan['price'], plan['stars'], position))
        conn.execute('UPDATE orders SET plan_name=(SELECT name FROM plans WHERE plans.id=orders.plan) WHERE plan_name IS NULL')

def one(sql, args=()):
    with db() as conn:
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None

def all_rows(sql, args=()):
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]

def execute(sql, args=()):
    with db() as conn:
        return conn.execute(sql, args).lastrowid
