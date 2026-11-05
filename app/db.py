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
        CREATE TABLE IF NOT EXISTS ads_policy (
          id INTEGER PRIMARY KEY CHECK(id=1), accepting INTEGER NOT NULL CHECK(accepting IN (0,1)),
          amount INTEGER NOT NULL CHECK(amount>0), hours INTEGER NOT NULL CHECK(hours>0),
          instructions TEXT NOT NULL, version INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ads_requests (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
          channel TEXT NOT NULL, comment TEXT NOT NULL, amount INTEGER NOT NULL, hours INTEGER NOT NULL,
          policy_version INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','canceled')),
          reason TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
          created INTEGER NOT NULL, updated INTEGER NOT NULL, UNIQUE(user_id,request_id));
        CREATE UNIQUE INDEX IF NOT EXISTS ads_open_channel ON ads_requests(user_id,channel) WHERE state IN ('pending','approved');
        CREATE INDEX IF NOT EXISTS ads_request_queue ON ads_requests(state,created,id);
        CREATE TABLE IF NOT EXISTS ads_events (
          id INTEGER PRIMARY KEY, request_id TEXT REFERENCES ads_requests(id),
          actor TEXT NOT NULL, action TEXT NOT NULL, created INTEGER NOT NULL, details TEXT NOT NULL);
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
        CREATE TABLE IF NOT EXISTS servers (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
          protocol TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'disabled', capacity INTEGER NOT NULL DEFAULT 0,
          settings TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL,
          health TEXT NOT NULL DEFAULT 'unknown', checked_at INTEGER, error_code TEXT);
        CREATE TABLE IF NOT EXISTS server_changes (
          id INTEGER PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id), created INTEGER NOT NULL,
          action TEXT NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS subscriptions (
          id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), protocol TEXT NOT NULL,
          expires INTEGER NOT NULL, state TEXT NOT NULL, secret TEXT,
          reminder INTEGER NOT NULL DEFAULT 0, server_id TEXT REFERENCES servers(id), UNIQUE(user_id, protocol));
        CREATE TABLE IF NOT EXISTS coupons (
          id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, percent INTEGER NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL DEFAULT 0,
          max_uses INTEGER NOT NULL DEFAULT 100, plan_id TEXT REFERENCES plans(id),
          protocol TEXT NOT NULL DEFAULT 'all', version INTEGER NOT NULL DEFAULT 1,
          created INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS coupon_uses (
          coupon_id TEXT NOT NULL REFERENCES coupons(id), user_id INTEGER NOT NULL REFERENCES users(id),
          order_id TEXT NOT NULL UNIQUE REFERENCES orders(id), created INTEGER NOT NULL,
          PRIMARY KEY(coupon_id,user_id));
        CREATE TABLE IF NOT EXISTS coupon_changes (
          id INTEGER PRIMARY KEY, coupon_id TEXT NOT NULL REFERENCES coupons(id), created INTEGER NOT NULL,
          before_data TEXT NOT NULL, after_data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS referral_policy (
          id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
          days INTEGER NOT NULL DEFAULT 7, max_invites INTEGER NOT NULL DEFAULT 100,
          min_rub INTEGER NOT NULL DEFAULT 199, min_stars INTEGER NOT NULL DEFAULT 150,
          version INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO referral_policy(id) VALUES (1);
        CREATE TABLE IF NOT EXISTS referral_policy_changes (
          id INTEGER PRIMARY KEY, created INTEGER NOT NULL, before_data TEXT NOT NULL, after_data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS referral_codes (
          user_id INTEGER PRIMARY KEY REFERENCES users(id), code TEXT NOT NULL UNIQUE, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS referral_bindings (
          invitee_id INTEGER PRIMARY KEY REFERENCES users(id), inviter_id INTEGER NOT NULL REFERENCES users(id),
          days INTEGER NOT NULL, min_rub INTEGER NOT NULL, min_stars INTEGER NOT NULL,
          policy_version INTEGER NOT NULL, created INTEGER NOT NULL, CHECK(invitee_id!=inviter_id));
        CREATE INDEX IF NOT EXISTS referral_inviter ON referral_bindings(inviter_id);
        CREATE TABLE IF NOT EXISTS referral_rewards (
          invitee_id INTEGER PRIMARY KEY REFERENCES referral_bindings(invitee_id),
          inviter_id INTEGER NOT NULL REFERENCES users(id), order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
          days INTEGER NOT NULL, created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS referral_rewards_inviter ON referral_rewards(inviter_id);
        CREATE TABLE IF NOT EXISTS referral_redemptions (
          user_id INTEGER NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
          subscription_id TEXT NOT NULL REFERENCES subscriptions(id), order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
          days INTEGER NOT NULL, created INTEGER NOT NULL, PRIMARY KEY(user_id,request_id));
        CREATE TABLE IF NOT EXISTS trial_policy (
          id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
          days INTEGER NOT NULL DEFAULT 1, max_claims INTEGER NOT NULL DEFAULT 100,
          version INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO trial_policy(id) VALUES (1);
        CREATE TABLE IF NOT EXISTS trial_claims (
          user_id INTEGER PRIMARY KEY REFERENCES users(id), order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
          protocol TEXT NOT NULL, created INTEGER NOT NULL, policy_version INTEGER NOT NULL, terms_snapshot TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trial_policy_changes (
          id INTEGER PRIMARY KEY, created INTEGER NOT NULL, before_data TEXT NOT NULL, after_data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscription_links (
          subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id), token_hash TEXT UNIQUE,
          token_secret TEXT, enabled INTEGER NOT NULL DEFAULT 0,
          follow_rotation INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
          created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS primary_rotations (
          id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
          request_id TEXT NOT NULL, old_credential_id TEXT NOT NULL, new_credential_id TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'revoking', created INTEGER NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT,
          UNIQUE(subscription_id,request_id));
        CREATE INDEX IF NOT EXISTS primary_rotations_queue ON primary_rotations(state,next_attempt);
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
          request_id TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          expires INTEGER NOT NULL DEFAULT 0, secret TEXT, created INTEGER NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT,
          UNIQUE(subscription_id,request_id));
        CREATE TABLE IF NOT EXISTS device_rotations (
          id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
          request_id TEXT NOT NULL, old_credential_id TEXT NOT NULL, new_credential_id TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'revoking', created INTEGER NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT,
          UNIQUE(device_id,request_id));
        CREATE INDEX IF NOT EXISTS rotations_queue ON device_rotations(state,next_attempt);
        CREATE INDEX IF NOT EXISTS devices_subscription ON devices(subscription_id,state);
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
        sub_columns = {row['name'] for row in conn.execute('PRAGMA table_info(subscriptions)')}
        if 'server_id' not in sub_columns:
            conn.execute('ALTER TABLE subscriptions ADD COLUMN server_id TEXT REFERENCES servers(id)')
        for column,definition in (('credential_id','TEXT'),('primary_key_state',"TEXT NOT NULL DEFAULT 'active'"),('rotation_error','TEXT')):
            if column not in sub_columns:
                conn.execute('ALTER TABLE subscriptions ADD COLUMN '+column+' '+definition)
        conn.execute('CREATE INDEX IF NOT EXISTS subscriptions_server ON subscriptions(server_id,state,expires)')
        device_columns = {row['name'] for row in conn.execute('PRAGMA table_info(devices)')}
        if 'credential_id' not in device_columns:
            conn.execute('ALTER TABLE devices ADD COLUMN credential_id TEXT')
        for row in conn.execute('SELECT d.id,d.secret,s.secret AS parent_secret FROM devices d JOIN subscriptions s ON s.id=d.subscription_id WHERE d.credential_id IS NULL').fetchall():
            if row['secret'] or row['parent_secret']:
                c.decrypt(row['secret'] or row['parent_secret'])  # Validate the existing key before encrypting migration data.
            conn.execute('UPDATE devices SET credential_id=? WHERE id=?',(c.encrypt(row['id']),row['id']))
        order_columns = {row['name'] for row in conn.execute('PRAGMA table_info(orders)')}
        promo_columns={'base_amount':'INTEGER','discount':'INTEGER NOT NULL DEFAULT 0',
            'promo_id':'TEXT REFERENCES coupons(id)','promo_code':'TEXT','promo_version':'INTEGER','promo_percent':'INTEGER'}
        for column,definition in promo_columns.items():
            if column not in order_columns:conn.execute('ALTER TABLE orders ADD COLUMN '+column+' '+definition)
        conn.execute('UPDATE orders SET base_amount=amount WHERE base_amount IS NULL')
        if 'kind' not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'purchase'")
        if 'access_source' not in sub_columns:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN access_source TEXT NOT NULL DEFAULT 'purchase'")
        # Defaults preserve the four extra profiles promised by previous versions.
        for table in ('plans','orders','subscriptions'):
            existing = {row['name'] for row in conn.execute('PRAGMA table_info('+table+')')}
            if 'device_limit' not in existing:
                conn.execute('ALTER TABLE '+table+' ADD COLUMN device_limit INTEGER NOT NULL DEFAULT 4 CHECK(device_limit BETWEEN 0 AND 20)')
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
