import os
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
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created INTEGER NOT NULL, expires INTEGER NOT NULL, consumed INTEGER);
        CREATE INDEX IF NOT EXISTS password_reset_user ON password_reset_tokens(user_id,created);
        CREATE TABLE IF NOT EXISTS support_cases (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          order_id TEXT REFERENCES orders(id), request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
          subject TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','waiting_staff','waiting_customer','closed')),
          version INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL, updated INTEGER NOT NULL,
          UNIQUE(user_id,request_id));
        CREATE INDEX IF NOT EXISTS support_cases_queue ON support_cases(status,updated,id);
        CREATE TABLE IF NOT EXISTS support_messages (
          id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES support_cases(id) ON DELETE CASCADE,
          actor_type TEXT NOT NULL CHECK(actor_type IN ('customer','staff')), actor_id INTEGER,
          body TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS support_messages_case ON support_messages(case_id,id);
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
        DROP INDEX IF EXISTS ads_open_channel;
        CREATE INDEX IF NOT EXISTS ads_channel_lookup ON ads_requests(user_id,channel,state);
        CREATE INDEX IF NOT EXISTS ads_request_queue ON ads_requests(state,created,id);
        CREATE TABLE IF NOT EXISTS ads_events (
          id INTEGER PRIMARY KEY, request_id TEXT REFERENCES ads_requests(id),
          actor TEXT NOT NULL, action TEXT NOT NULL, created INTEGER NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ads_payment_entries (
          id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
          request_id TEXT NOT NULL REFERENCES ads_requests(id),
          kind TEXT NOT NULL CHECK(kind IN ('receipt','refund')),
          amount INTEGER NOT NULL CHECK(amount>0), reference TEXT NOT NULL UNIQUE,
          note TEXT NOT NULL, fingerprint TEXT NOT NULL, created INTEGER NOT NULL,
          UNIQUE(request_id,kind));
        CREATE TABLE IF NOT EXISTS ads_sites (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
          verified INTEGER NOT NULL CHECK(verified IN (0,1)), note TEXT NOT NULL,
          version INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ads_placements (
          request_id TEXT PRIMARY KEY REFERENCES ads_requests(id), site_id TEXT NOT NULL REFERENCES ads_sites(id),
          site_name TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','paused','finished')),
          version INTEGER NOT NULL, allocated_seconds INTEGER NOT NULL CHECK(allocated_seconds>0),
          used_seconds INTEGER NOT NULL CHECK(used_seconds>=0), segment_started INTEGER,
          started INTEGER NOT NULL, finished INTEGER, updated INTEGER NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ads_exclusive_site ON ads_placements(site_id) WHERE state IN ('active','paused');
        CREATE TABLE IF NOT EXISTS ads_placement_actions (
          operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES ads_requests(id),
          action TEXT NOT NULL, fingerprint TEXT NOT NULL, note TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ads_bookings (
          request_id TEXT PRIMARY KEY REFERENCES ads_requests(id), site_id TEXT NOT NULL REFERENCES ads_sites(id),
          site_name TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL CHECK(ends_at>starts_at),
          state TEXT NOT NULL CHECK(state IN ('reserved','consumed','canceled')), version INTEGER NOT NULL,
          created INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ads_booking_slots ON ads_bookings(site_id,state,starts_at,ends_at);
        CREATE TRIGGER IF NOT EXISTS ads_booking_insert BEFORE INSERT ON ads_bookings
          WHEN NEW.state='reserved' AND EXISTS(SELECT 1 FROM ads_bookings b WHERE b.site_id=NEW.site_id AND b.state='reserved' AND b.request_id!=NEW.request_id AND b.starts_at<NEW.ends_at AND b.ends_at>NEW.starts_at)
          BEGIN SELECT RAISE(ABORT,'overlapping_ads_booking'); END;
        CREATE TRIGGER IF NOT EXISTS ads_booking_update BEFORE UPDATE OF site_id,starts_at,ends_at,state ON ads_bookings
          WHEN NEW.state='reserved' AND EXISTS(SELECT 1 FROM ads_bookings b WHERE b.site_id=NEW.site_id AND b.state='reserved' AND b.request_id!=NEW.request_id AND b.starts_at<NEW.ends_at AND b.ends_at>NEW.starts_at)
          BEGIN SELECT RAISE(ABORT,'overlapping_ads_booking'); END;
        CREATE TABLE IF NOT EXISTS ads_booking_actions (
          operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES ads_requests(id),
          action TEXT NOT NULL CHECK(action IN ('reserve','cancel','reschedule')), fingerprint TEXT NOT NULL,
          note TEXT NOT NULL, details TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ads_expenses (
          id TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount>0),
          category TEXT NOT NULL CHECK(category IN ('server','commission','marketing','other')),
          incurred_at INTEGER NOT NULL, reference TEXT NOT NULL, note TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('recorded','void')), version INTEGER NOT NULL,
          created INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ads_expense_reference ON ads_expenses(reference) WHERE state='recorded';
        CREATE INDEX IF NOT EXISTS ads_expense_period ON ads_expenses(incurred_at,id);
        CREATE TABLE IF NOT EXISTS ads_expense_actions (
          operation_id TEXT PRIMARY KEY, expense_id TEXT NOT NULL REFERENCES ads_expenses(id),
          action TEXT NOT NULL CHECK(action IN ('create','void')), fingerprint TEXT NOT NULL,
          note TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ads_expense_void ON ads_expense_actions(expense_id) WHERE action='void';
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
        CREATE TABLE IF NOT EXISTS partner_events (
          event_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, promo_code TEXT NOT NULL,
          payload_hash TEXT NOT NULL, received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS partner_events_lookup ON partner_events(telegram_id,promo_code,expires_at);
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
        CREATE TABLE IF NOT EXISTS staff_users (
          id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('owner','panel_operator','mtproxy_manager','finance','support')),
          enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), created INTEGER NOT NULL,
          updated INTEGER NOT NULL, created_by INTEGER REFERENCES staff_users(id), last_login INTEGER);
        CREATE TABLE IF NOT EXISTS staff_sessions (
          token_hash TEXT PRIMARY KEY, staff_id INTEGER NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
          created INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS staff_sessions_expiry ON staff_sessions(expires);
        CREATE TABLE IF NOT EXISTS admin_audit (
          id INTEGER PRIMARY KEY, created INTEGER NOT NULL, staff_id INTEGER,
          actor_email TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL, status INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS admin_audit_created ON admin_audit(created,id);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, created INTEGER NOT NULL, action TEXT NOT NULL, order_id TEXT);
        CREATE TABLE IF NOT EXISTS store_settings (
          id INTEGER PRIMARY KEY CHECK(id=1), support_text TEXT NOT NULL, terms_text TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS bot_menu_settings (
          id INTEGER PRIMARY KEY CHECK(id=1), content TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL DEFAULT 0);
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
        migrate_booking_actions(conn)
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
        conn.execute('INSERT OR IGNORE INTO store_settings(id,support_text,terms_text,version,updated) VALUES (1,?,?,1,0)',
                     (os.getenv('SUPPORT_TEXT','Напишите администратору магазина.'),os.getenv('TERMS_TEXT','')))
        # Меню создаётся один раз; будущие обновления кода не переписывают выбор владельца.
        from .bot_menu import DEFAULT_BUTTONS
        import json
        conn.execute('INSERT OR IGNORE INTO bot_menu_settings(id,content,version,updated) VALUES (1,?,1,0)',
                     (json.dumps(DEFAULT_BUTTONS,ensure_ascii=False),))
        conn.execute('UPDATE orders SET plan_name=(SELECT name FROM plans WHERE plans.id=orders.plan) WHERE plan_name IS NULL')

        # Записываем первые подтверждения атомарно с изменением заказа.
        # Историю старых заказов не восстанавливаем из текущих статусов.
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS order_milestones (
          order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          event TEXT NOT NULL CHECK(event IN ('paid','issued')),
          recorded_at INTEGER NOT NULL,
          PRIMARY KEY(order_id,event));
        CREATE INDEX IF NOT EXISTS order_milestones_period ON order_milestones(recorded_at,event);
        CREATE TABLE IF NOT EXISTS order_history_state (
          id INTEGER PRIMARY KEY CHECK(id=1), recorded_since INTEGER NOT NULL);
        INSERT OR IGNORE INTO order_history_state(id,recorded_since)
          VALUES (1,CAST(strftime('%s','now') AS INTEGER));
        CREATE TRIGGER IF NOT EXISTS order_paid_insert AFTER INSERT ON orders
        WHEN NEW.status='paid' AND NEW.kind='purchase' AND NEW.method IN ('manual','yookassa','stars')
        BEGIN
          INSERT OR IGNORE INTO order_milestones VALUES (NEW.id,'paid',CAST(strftime('%s','now') AS INTEGER));
        END;
        CREATE TRIGGER IF NOT EXISTS order_paid_update AFTER UPDATE OF status ON orders
        WHEN NEW.status='paid' AND OLD.status!='paid' AND NEW.kind='purchase'
          AND NEW.method IN ('manual','yookassa','stars')
        BEGIN
          INSERT OR IGNORE INTO order_milestones VALUES (NEW.id,'paid',CAST(strftime('%s','now') AS INTEGER));
        END;
        CREATE TRIGGER IF NOT EXISTS order_issued_insert AFTER INSERT ON orders
        WHEN NEW.status='active'
        BEGIN
          INSERT OR IGNORE INTO order_milestones VALUES (NEW.id,'issued',CAST(strftime('%s','now') AS INTEGER));
        END;
        CREATE TRIGGER IF NOT EXISTS order_issued_update AFTER UPDATE OF status ON orders
        WHEN NEW.status='active' AND OLD.status!='active'
        BEGIN
          INSERT OR IGNORE INTO order_milestones VALUES (NEW.id,'issued',CAST(strftime('%s','now') AS INTEGER));
        END;
        """)

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


def migrate_booking_actions(conn):
    """Расширение CHECK без изменения строк, отпечатков или порядка прежнего журнала."""
    definition=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ads_booking_actions'").fetchone()['sql']
    if "'reschedule'" in definition:return
    columns=[r['name'] for r in conn.execute('PRAGMA table_info(ads_booking_actions)')]
    extra=conn.execute("SELECT 1 FROM sqlite_master WHERE tbl_name='ads_booking_actions' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchone()
    if columns!=['operation_id','request_id','action','fingerprint','note','details','created'] or extra:
        raise ValueError('Нестандартная схема журнала брони требует отдельного плана обновления')
    conn.execute('BEGIN IMMEDIATE')
    conn.execute("CREATE TABLE ads_booking_actions_v037 (operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES ads_requests(id),action TEXT NOT NULL CHECK(action IN ('reserve','cancel','reschedule')),fingerprint TEXT NOT NULL,note TEXT NOT NULL,details TEXT NOT NULL,created INTEGER NOT NULL)")
    conn.execute('INSERT INTO ads_booking_actions_v037(rowid,operation_id,request_id,action,fingerprint,note,details,created) SELECT rowid,operation_id,request_id,action,fingerprint,note,details,created FROM ads_booking_actions ORDER BY rowid')
    conn.execute('DROP TABLE ads_booking_actions')
    conn.execute('ALTER TABLE ads_booking_actions_v037 RENAME TO ads_booking_actions')
