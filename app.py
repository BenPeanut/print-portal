import argparse
import atexit
import csv
import io
import os
from pathlib import Path
import random
import threading
import uuid
import json
from urllib.parse import urlparse, unquote
import re
import psycopg2
from dotenv import load_dotenv
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

app = Flask(__name__)

# --- CONFIGURATION ---
def _required_env(name):
    value = (os.environ.get(name) or '').strip()
    if not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


app.secret_key = _required_env('SECRET_KEY')
ADMIN_PASSWORD = _required_env('ADMIN_PASSWORD')
DB_POOL_MIN = int(os.environ.get('DB_POOL_MIN', '1'))
DB_POOL_MAX = int(os.environ.get('DB_POOL_MAX', '10'))

CONTROL_SETTING_DEFAULTS = {
    'base_fee': 0.0,
    'base_service_fee': 0.0,
    'default_price_per_gram': 0.0,
    'price_per_gram': 0.0,
    'power_cost_per_hour': 0.0,
    'profit_margin': 1.2,
    'shop_open': True,
    'lifetime_total_plastic_used': 0.0,
    'announcement_message': '',
}

PENDING_DELETE_DAYS = 3
PENDING_DELETE_STATUS = 'Awaiting Deletion'
MAX_ADMIN_NOTIFICATIONS = 300
MAX_SUPPORT_REPORTS = 400

_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()
_SCHEMA_READY = False

# --- DATABASE HELPERS ---

def _create_db_pool():
    db_url = _required_env('DATABASE_URL')
    if db_url.startswith('postgres://'):
        db_url = 'postgresql://' + db_url[len('postgres://'):]
    return ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, dsn=db_url)


def _get_pooled_connection():
    global _DB_POOL
    with _DB_POOL_LOCK:
        if _DB_POOL is None:
            _DB_POOL = _create_db_pool()
        pool = _DB_POOL
    return pool.getconn()


def _put_pooled_connection(conn, discard=False):
    if conn is None:
        return
    with _DB_POOL_LOCK:
        pool = _DB_POOL
    if pool is None:
        conn.close()
        return
    try:
        pool.putconn(conn, close=discard or bool(getattr(conn, 'closed', 0)))
    except psycopg2.pool.PoolError:
        # If the global pool was replaced (e.g. during reload), close this
        # orphaned connection rather than crashing the request lifecycle.
        conn.close()


def _close_db_pool():
    global _DB_POOL
    with _DB_POOL_LOCK:
        pool = _DB_POOL
        _DB_POOL = None
    if pool is not None:
        pool.closeall()


atexit.register(_close_db_pool)

def _execute(query, params=(), fetch=False):
    normalized_query = query.replace('?', '%s')
    last_error = None

    # Retry once in case the pool gives us a stale/closed connection.
    for _ in range(2):
        conn = None
        discard_conn = False
        try:
            conn = _get_pooled_connection()
            cur = conn.cursor()
            try:
                cur.execute(normalized_query, params)
                if fetch:
                    return cur.fetchall()
                conn.commit()
                return None
            finally:
                cur.close()
        except Exception as exc:
            last_error = exc
            discard_conn = isinstance(exc, (psycopg2.InterfaceError, psycopg2.OperationalError))
            if conn is not None and not getattr(conn, 'closed', 1):
                try:
                    conn.rollback()
                except Exception:
                    discard_conn = True
            if not discard_conn:
                raise
        finally:
            if conn is not None:
                _put_pooled_connection(conn, discard=discard_conn)

    raise last_error


def _init_db():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    conn = _get_pooled_connection()
    try:
        cur = conn.cursor()
        try:
            # Initialize all required tables in a single roundtrip.
            cur.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'settings'
                          AND column_name = 'name'
                    ) AND NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'settings'
                          AND column_name = 'key'
                    ) THEN
                        ALTER TABLE settings RENAME COLUMN name TO key;
                    END IF;

                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'daily_revenue'
                          AND column_name = 'date'
                    ) AND NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'daily_revenue'
                          AND column_name = 'completion_date'
                    ) THEN
                        ALTER TABLE daily_revenue RENAME COLUMN date TO completion_date;
                    END IF;

                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'daily_revenue'
                          AND column_name = 'revenue'
                    ) AND NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'daily_revenue'
                          AND column_name = 'daily_profit'
                    ) THEN
                        ALTER TABLE daily_revenue RENAME COLUMN revenue TO daily_profit;
                    END IF;
                END $$;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS featured_prints (
                    id TEXT PRIMARY KEY,
                    json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS business_stats (
                    stat_name TEXT PRIMARY KEY,
                    stat_value NUMERIC NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS daily_revenue (
                    completion_date DATE PRIMARY KEY DEFAULT CURRENT_DATE,
                    daily_profit NUMERIC NOT NULL DEFAULT 0
                );
                """
            )
            conn.commit()
            _SCHEMA_READY = True
        finally:
            cur.close()
    except Exception:
        if conn is not None and not getattr(conn, 'closed', 1):
            conn.rollback()
        raise
    finally:
        _put_pooled_connection(conn)


def _load_all():
    _init_db()

    conn = _get_pooled_connection()
    try:
        cur = conn.cursor()
        try:
            settings = {"filaments": []}
            cur.execute("SELECT value FROM settings WHERE key = %s", ("settings",))
            row = cur.fetchone()
            if row:
                try:
                    settings = json.loads(row[0])
                except Exception:
                    pass

            users = []
            cur.execute("SELECT json FROM users")
            for r in cur.fetchall():
                try:
                    users.append(json.loads(r[0]))
                except Exception:
                    pass

            orders = []
            cur.execute("SELECT json FROM orders")
            for r in cur.fetchall():
                try:
                    orders.append(json.loads(r[0]))
                except Exception:
                    pass

            featured_prints = []
            cur.execute("SELECT json FROM featured_prints")
            for r in cur.fetchall():
                try:
                    featured_prints.append(json.loads(r[0]))
                except Exception:
                    pass
        finally:
            cur.close()
    finally:
        _put_pooled_connection(conn)

    return {
        "settings": settings,
        "users": users,
        "orders": orders,
        "featured_prints": featured_prints,
    }


def get_db():
    return _load_all()


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _load_control_center_settings():
    _init_db()
    rows = _execute("SELECT key, value FROM settings", fetch=True) or []
    raw = {k: v for k, v in rows if isinstance(k, str)}
    base_fee = _to_float(
        raw.get('base_fee', raw.get('base_service_fee')),
        CONTROL_SETTING_DEFAULTS['base_fee'],
    )
    price_per_gram = _to_float(
        raw.get('price_per_gram', raw.get('default_price_per_gram')),
        CONTROL_SETTING_DEFAULTS['price_per_gram'],
    )
    return {
        'base_fee': base_fee,
        'base_service_fee': base_fee,
        'default_price_per_gram': price_per_gram,
        'price_per_gram': price_per_gram,
        'power_cost_per_hour': _to_float(raw.get('power_cost_per_hour'), CONTROL_SETTING_DEFAULTS['power_cost_per_hour']),
        'profit_margin': max(0.0, _to_float(raw.get('profit_margin'), CONTROL_SETTING_DEFAULTS['profit_margin'])),
        'shop_open': _to_bool(raw.get('shop_open'), CONTROL_SETTING_DEFAULTS['shop_open']),
        'lifetime_total_plastic_used': _to_float(raw.get('lifetime_total_plastic_used'), CONTROL_SETTING_DEFAULTS['lifetime_total_plastic_used']),
        'announcement_message': str(raw.get('announcement_message', CONTROL_SETTING_DEFAULTS['announcement_message']) or '').strip(),
    }


def _save_control_center_settings(settings_payload):
    payload = dict(CONTROL_SETTING_DEFAULTS)
    payload.update(settings_payload or {})
    for key, value in payload.items():
        _execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, str(value))
        )


def _is_database_connected():
    try:
        _execute("SELECT 1", fetch=True)
        return True
    except Exception:
        return False


def _get_business_stat(stat_name, default=0.0):
    rows = _execute(
        "SELECT stat_value FROM business_stats WHERE stat_name = %s",
        (stat_name,),
        fetch=True,
    ) or []
    if not rows:
        return float(default)
    return _to_float(rows[0][0], default)


def _set_business_stat(stat_name, stat_value):
    _execute(
        """
        INSERT INTO business_stats (stat_name, stat_value)
        VALUES (%s, %s)
        ON CONFLICT (stat_name)
        DO UPDATE SET stat_value = EXCLUDED.stat_value
        """,
        (stat_name, stat_value),
    )


def _increment_business_stat(stat_name, amount):
    _execute(
        """
        INSERT INTO business_stats (stat_name, stat_value)
        VALUES (%s, %s)
        ON CONFLICT (stat_name)
        DO UPDATE SET stat_value = business_stats.stat_value + EXCLUDED.stat_value
        """,
        (stat_name, amount),
    )


def _record_daily_revenue(amount, entry_date=None):
    revenue_date = entry_date or date.today()
    _execute(
        """
        INSERT INTO daily_revenue (completion_date, daily_profit)
        VALUES (%s, %s)
        ON CONFLICT (completion_date)
        DO UPDATE SET daily_profit = daily_revenue.daily_profit + EXCLUDED.daily_profit
        """,
        (revenue_date, amount),
    )


def _order_total_price(order):
    if not isinstance(order, dict):
        return 0.0
    return max(0.0, _to_float(order.get('print_price'), 0)) + max(0.0, _to_float(order.get('material_fee'), 0))


def _sync_missing_completed_order_revenue(db):
    if not isinstance(db, dict):
        return False

    orders = db.get('orders', []) or []
    changed = False
    completed_statuses = {'completed', 'done', 'delivered'}

    for order in orders:
        status = str(order.get('status') or '').strip().lower()
        if status not in completed_statuses:
            continue
        if order.get('revenue_counted'):
            continue

        total_price = int(round(_order_total_price(order)))
        if total_price <= 0:
            order['revenue_counted'] = True
            changed = True
            continue

        completed_at_raw = order.get('completed_at') or order.get('updated_at') or order.get('created_at')
        completion_day = date.today()
        parsed_completed_at = _parse_iso_utc(completed_at_raw)
        if parsed_completed_at is not None:
            completion_day = parsed_completed_at.date()

        _increment_business_stat('lifetime_profit', total_price)
        _record_daily_revenue(total_price, completion_day)
        order['completed_at'] = completion_day.isoformat()
        order['revenue_counted'] = True
        changed = True

    return changed


def _build_chart_data(timeframe='week'):
    normalized = (timeframe or 'week').strip().lower()
    if normalized not in {'week', 'month', 'year'}:
        normalized = 'week'

    if normalized == 'year':
        today = date.today()
        month_start = date(today.year, today.month, 1)
        months = []
        cursor_year = month_start.year
        cursor_month = month_start.month
        for _ in range(12):
            months.append(date(cursor_year, cursor_month, 1))
            cursor_month -= 1
            if cursor_month == 0:
                cursor_month = 12
                cursor_year -= 1
        months.reverse()

        window_start = months[0]

        rows = _execute(
            """
            SELECT DATE_TRUNC('month', completion_date)::date AS month_bucket, SUM(daily_profit) AS total_profit
            FROM daily_revenue
            WHERE completion_date >= %s
            GROUP BY month_bucket
            ORDER BY month_bucket ASC
            """,
            (window_start,),
            fetch=True,
        ) or []
        totals_by_month = {}
        for month_bucket, total_profit in rows:
            bucket_date = month_bucket.date() if hasattr(month_bucket, 'date') else month_bucket
            totals_by_month[bucket_date] = round(_to_float(total_profit, 0.0), 2)

        labels = [bucket.strftime('%b %Y') for bucket in months]
        period_data = [totals_by_month.get(bucket, 0.0) for bucket in months]
        running_total = 0.0
        data = []
        for value in period_data:
            running_total += value
            data.append(round(running_total, 2))
        return {'labels': labels, 'data': data, 'period_data': period_data}

    days = 7 if normalized == 'week' else 30
    start_date = date.today() - timedelta(days=days - 1)
    end_date = date.today()

    rows = _execute(
        """
        SELECT completion_date, daily_profit
        FROM daily_revenue
        WHERE completion_date >= %s AND completion_date <= %s
        ORDER BY completion_date ASC
        """,
        (start_date, end_date),
        fetch=True,
    ) or []

    totals_by_day = {}
    for completion_date, daily_profit in rows:
        bucket_date = completion_date.date() if hasattr(completion_date, 'date') else completion_date
        totals_by_day[bucket_date] = round(_to_float(daily_profit, 0.0), 2)

    labels = []
    period_data = []
    data = []
    running_total = 0.0
    for offset in range(days):
        current_day = start_date + timedelta(days=offset)
        if normalized == 'week':
            labels.append(current_day.strftime('%a'))
        else:
            labels.append(current_day.strftime('%d %b'))
        day_total = round(totals_by_day.get(current_day, 0.0), 2)
        period_data.append(day_total)
        running_total += day_total
        data.append(round(running_total, 2))

    return {'labels': labels, 'data': data, 'period_data': period_data}


def _load_dashboard_payload():
    """Fetch all dashboard-backed data with one top-level call."""
    db = get_db()
    return {
        'db': db,
        'orders': db.get('orders', []),
        'users': db.get('users', []),
        'featured_prints': db.get('featured_prints', []),
        'settings': db.setdefault('settings', {'filaments': []}),
    }


def _redirect_back_to_dashboard(default_hash=''):
    """Safely redirect back to dashboard, preserving current section when provided by form."""
    next_url = (request.form.get('next') or request.args.get('next') or '').strip()
    dashboard_path = url_for('dashboard')

    if next_url:
        parsed = urlparse(next_url)
        # Allow relative dashboard targets only.
        if not parsed.netloc and (parsed.path in ('', dashboard_path)):
            fragment = (parsed.fragment or '').strip()
            if fragment:
                return redirect(f"{dashboard_path}#{fragment}")
            return redirect(dashboard_path)

    if default_hash:
        return redirect(f"{dashboard_path}{default_hash}")
    return redirect(dashboard_path)


def _parse_selected_order_ids():
    selected_ids = []
    seen_ids = set()
    for raw_value in request.form.getlist('selected_order_ids'):
        for piece in str(raw_value or '').split(','):
            order_id = piece.strip()
            if not order_id or order_id in seen_ids:
                continue
            seen_ids.add(order_id)
            selected_ids.append(order_id)
    return selected_ids


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1]
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _order_last_modified(order):
    if not isinstance(order, dict):
        return None
    candidates = [
        order.get('updated_at'),
        order.get('deleted_at'),
        order.get('created_at'),
    ]
    messages = order.get('messages') or []
    if messages and isinstance(messages, list):
        last_msg = messages[-1] if messages[-1:] else None
        if isinstance(last_msg, dict):
            candidates.append(last_msg.get('ts'))

    parsed = [_parse_iso_utc(c) for c in candidates if c]
    return max(parsed) if parsed else None


def _normalize_admin_notifications(settings):
    raw = settings.get('admin_notifications', [])
    if not isinstance(raw, list):
        raw = []

    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        notif_id = str(item.get('id') or '').strip()
        if not notif_id:
            notif_id = str(uuid.uuid4())[:12]

        created_at = str(item.get('created_at') or '').strip() or datetime.utcnow().isoformat() + 'Z'
        normalized.append({
            'id': notif_id,
            'type': str(item.get('type') or 'general').strip() or 'general',
            'title': str(item.get('title') or 'Notification').strip() or 'Notification',
            'message': str(item.get('message') or '').strip(),
            'order_id': str(item.get('order_id') or '').strip() or None,
            'actor_user_id': str(item.get('actor_user_id') or '').strip() or None,
            'created_at': created_at,
            'is_read': bool(item.get('is_read', False)),
        })

    normalized.sort(key=lambda n: _parse_iso_utc(n.get('created_at')) or datetime.min, reverse=True)
    settings['admin_notifications'] = normalized[:MAX_ADMIN_NOTIFICATIONS]
    return settings['admin_notifications']


def _add_admin_notification(db, notif_type, title, message, order_id=None, actor_user_id=None):
    settings = db.setdefault('settings', {'filaments': []})
    notifications = _normalize_admin_notifications(settings)
    notifications.insert(0, {
        'id': str(uuid.uuid4())[:12],
        'type': str(notif_type or 'general').strip() or 'general',
        'title': str(title or 'Notification').strip() or 'Notification',
        'message': str(message or '').strip(),
        'order_id': (str(order_id).strip() if order_id else None),
        'actor_user_id': (str(actor_user_id).strip() if actor_user_id else None),
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'is_read': False,
    })
    settings['admin_notifications'] = notifications[:MAX_ADMIN_NOTIFICATIONS]


def _mark_admin_notification_read(db, notification_id=None, mark_all=False):
    settings = db.setdefault('settings', {'filaments': []})
    notifications = _normalize_admin_notifications(settings)
    changed = False
    if mark_all:
        for notif in notifications:
            if not notif.get('is_read'):
                notif['is_read'] = True
                changed = True
        return changed

    target = str(notification_id or '').strip()
    if not target:
        return False

    for notif in notifications:
        if notif.get('id') == target and not notif.get('is_read'):
            notif['is_read'] = True
            changed = True
            break
    return changed


def _normalize_support_reports(settings):
    raw = settings.get('support_reports', [])
    if not isinstance(raw, list):
        raw = []

    allowed_statuses = {'new', 'in_review', 'planned', 'resolved', 'dismissed'}
    allowed_types = {'bug', 'suggestion', 'other'}
    allowed_severity = {'low', 'medium', 'high', 'critical'}
    normalized = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        report_id = str(item.get('id') or '').strip()[:20] or str(uuid.uuid4())[:12]
        report_type = str(item.get('report_type') or 'bug').strip().lower()
        if report_type not in allowed_types:
            report_type = 'bug'
        status = str(item.get('status') or 'new').strip().lower()
        if status not in allowed_statuses:
            status = 'new'
        severity = str(item.get('severity') or 'medium').strip().lower()
        if severity not in allowed_severity:
            severity = 'medium'

        created_at = str(item.get('created_at') or '').strip() or (datetime.utcnow().isoformat() + 'Z')
        normalized.append({
            'id': report_id,
            'report_type': report_type,
            'status': status,
            'severity': severity,
            'title': str(item.get('title') or '').strip() or 'Untitled report',
            'details': str(item.get('details') or '').strip(),
            'steps': str(item.get('steps') or '').strip(),
            'expected': str(item.get('expected') or '').strip(),
            'actual': str(item.get('actual') or '').strip(),
            'page_url': str(item.get('page_url') or '').strip(),
            'screenshot_url': str(item.get('screenshot_url') or '').strip(),
            'browser': str(item.get('browser') or '').strip(),
            'os': str(item.get('os') or '').strip(),
            'user_id': str(item.get('user_id') or '').strip() or None,
            'username': str(item.get('username') or '').strip() or 'Unknown user',
            'admin_note': str(item.get('admin_note') or '').strip(),
            'created_at': created_at,
            'updated_at': str(item.get('updated_at') or '').strip() or created_at,
        })

    normalized.sort(key=lambda r: _parse_iso_utc(r.get('created_at')) or datetime.min, reverse=True)
    settings['support_reports'] = normalized[:MAX_SUPPORT_REPORTS]
    return settings['support_reports']


def _is_order_pending_deletion(order):
    return bool(isinstance(order, dict) and order.get('deleted_at'))


def _mark_order_pending_deletion(order, requested_by='admin'):
    if not isinstance(order, dict):
        return
    order['deleted_at'] = datetime.utcnow().isoformat()
    order['delete_requested_by'] = str(requested_by or 'admin')
    order['delete_restore_requested_at'] = None
    order['delete_restore_requested_by'] = None
    order['updated_at'] = datetime.utcnow().isoformat()


def _restore_soft_deleted_order(order):
    if not isinstance(order, dict):
        return
    order['deleted_at'] = None
    order['delete_requested_by'] = None
    order['delete_restore_requested_at'] = None
    order['delete_restore_requested_by'] = None
    order['updated_at'] = datetime.utcnow().isoformat()


def save_db(data, full_replace=False):
    _init_db()

    # Settings
    try:
        _execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("settings", json.dumps(data.get("settings", {"filaments": []})))
        )
    except Exception as e:
        print(f"Failed to save settings: {e}")

    # Users
    try:
        _execute("DELETE FROM users")
        for user in data.get("users", []):
            _execute(
                "INSERT INTO users (id, json) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET json = EXCLUDED.json",
                (user.get('id'), json.dumps(user))
            )
    except Exception as e:
        print(f"Failed to save users: {e}")

    # Orders
    try:
        if full_replace:
            _execute("DELETE FROM orders")

        for order in data.get("orders", []):
            order_id = order.get('id')
            if not order_id:
                continue

            incoming = dict(order)
            existing_rows = _execute("SELECT json FROM orders WHERE id = %s", (order_id,), fetch=True)
            existing = None
            if existing_rows:
                try:
                    existing = json.loads(existing_rows[0][0])
                except Exception:
                    existing = None

            # Stamp updates so stale snapshots cannot overwrite newer edits.
            if not incoming.get('updated_at'):
                if existing is None or incoming != existing:
                    incoming['updated_at'] = datetime.utcnow().isoformat()

            chosen = incoming
            if existing is not None:
                incoming_ts = _order_last_modified(incoming)
                existing_ts = _order_last_modified(existing)
                if existing_ts and incoming_ts and existing_ts > incoming_ts:
                    chosen = existing

            _execute(
                "INSERT INTO orders (id, json) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET json = EXCLUDED.json",
                (order_id, json.dumps(chosen))
            )
    except Exception as e:
        print(f"Failed to save orders: {e}")

    # Featured prints
    try:
        _execute("DELETE FROM featured_prints")
        for item in data.get("featured_prints", []):
            _execute(
                "INSERT INTO featured_prints (id, json) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET json = EXCLUDED.json",
                (item.get('id'), json.dumps(item))
            )
    except Exception as e:
        print(f"Failed to save featured prints: {e}")


def _normalize_target_users(raw_targets, fallback='ALL'):
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    if not isinstance(raw_targets, list):
        raw_targets = []

    normalized = []
    for target in raw_targets:
        t = str(target).strip()
        if t and t not in normalized:
            normalized.append(t)

    if not normalized:
        normalized = [fallback]

    if 'ALL' in normalized:
        return ['ALL']
    return normalized


def _default_hex_for_name(name):
    palette = {
        'black': '#222222',
        'white': '#f5f5f5',
        'gray': '#8b8b8b',
        'grey': '#8b8b8b',
        'red': '#d22f27',
        'blue': '#1f6feb',
        'green': '#2ea043',
        'yellow': '#e3b341',
        'orange': '#f0883e',
        'purple': '#8250df',
        'pink': '#d63384',
        'brown': '#8b5a2b',
    }
    lowered = (name or '').lower()
    for key, value in palette.items():
        if key in lowered:
            return value
    return '#2a2f36'


def _normalize_filament_item(raw):
    if isinstance(raw, str):
        label = raw.strip()
        if not label:
            return None
        fid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f'filament:{label.lower()}'))[:8]
        return {
            'id': fid,
            'name': label,
            'brand': 'Generic',
            'material': 'PLA',
            'hex': _default_hex_for_name(label),
            'total_g': 1000,
            'remaining_g': 1000,
            'out_of_stock': False,
        }

    if not isinstance(raw, dict):
        return None

    name = str(raw.get('name') or raw.get('filament_name') or '').strip()
    if not name:
        return None

    total_g = int(raw.get('total_g') or 1000)
    remaining_g = int(raw.get('remaining_g') if raw.get('remaining_g') is not None else total_g)
    total_g = max(1, total_g)
    remaining_g = max(0, min(remaining_g, total_g))

    fid = str(raw.get('id') or '')[:64] or str(uuid.uuid5(uuid.NAMESPACE_DNS, f'filament:{name.lower()}'))[:8]
    return {
        'id': fid,
        'name': name,
        'brand': str(raw.get('brand') or 'Generic').strip() or 'Generic',
        'material': str(raw.get('material') or 'PLA').strip().upper() or 'PLA',
        'hex': str(raw.get('hex') or _default_hex_for_name(name)).strip() or _default_hex_for_name(name),
        'total_g': total_g,
        'remaining_g': remaining_g,
        'out_of_stock': bool(raw.get('out_of_stock', False)),
    }


def _normalize_filaments(settings):
    filaments = settings.setdefault('filaments', [])
    normalized = []
    seen = set()
    for raw in filaments:
        item = _normalize_filament_item(raw)
        if not item:
            continue
        key = item['id']
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)

    changed = filaments != normalized
    settings['filaments'] = normalized
    return normalized, changed


def _decorate_orders_with_pending_delete_date(orders):
    for order in orders:
        deleted_at = order.get('deleted_at')
        order['pending_delete_on'] = None
        base_status = str(order.get('status') or '').strip() or 'Unknown'
        if deleted_at:
            order['status_display'] = PENDING_DELETE_STATUS
            order['status_display_key'] = 'awaiting-deletion'
        else:
            order['status_display'] = base_status
            order['status_display_key'] = base_status.lower().replace(' ', '-')
        if not deleted_at:
            continue
        try:
            purge_at = datetime.fromisoformat(deleted_at) + timedelta(days=PENDING_DELETE_DAYS)
            order['pending_delete_on'] = purge_at.strftime('%b %d, %Y')
        except Exception:
            # Keep it visible even if timestamp parsing fails.
            order['pending_delete_on'] = None
    return orders


def _purge_expired_soft_deletes(db):
    """Drop orders whose deleted_at timestamp is older than the grace period."""
    cutoff = datetime.utcnow() - timedelta(days=PENDING_DELETE_DAYS)
    surviving = []
    expired_ids = []
    for order in db.get('orders', []):
        deleted_at = order.get('deleted_at')
        if deleted_at:
            try:
                if datetime.fromisoformat(deleted_at) > cutoff:
                    surviving.append(order)
                else:
                    expired_ids.append(order.get('id'))
            except Exception:
                surviving.append(order)
        else:
            surviving.append(order)
    db['orders'] = surviving
    for order_id in expired_ids:
        if order_id:
            _execute("DELETE FROM orders WHERE id = %s", (order_id,))


def _featured_item_visible_to_user(item, user_id):
    targets = item.get('target_users')
    if not targets:
        legacy = item.get('target_user')
        targets = [legacy] if legacy else []

    targets = _normalize_target_users(targets, fallback='ALL')
    return 'ALL' in targets or user_id in targets


def _compute_user_material_credits(user_obj, user_orders):
    if isinstance(user_obj, dict) and user_obj.get('material_credits') is not None:
        return max(0, int(round(_to_float(user_obj.get('material_credits'), 0))))

    completed_statuses = {'completed', 'done', 'delivered'}
    completed_grams = sum(
        max(0.0, _to_float(o.get('print_weight_g'), 0))
        for o in (user_orders or [])
        if str(o.get('status') or '').strip().lower() in completed_statuses
    )
    return int(completed_grams // 500.0)


def _estimate_order_eta(order):
    status = str(order.get('status') or '').strip().lower()
    quoted_total = max(0.0, _to_float(order.get('print_price'), 0)) + max(0.0, _to_float(order.get('material_fee'), 0))
    if order.get('deleted_at'):
        return 'Scheduled for deletion'
    if status in {'delivered', 'done', 'completed'}:
        return 'Completed'
    if status in {'cancelled', 'declined', 'price denied'}:
        return 'Stopped'
    if status in {'in cart', 'quoted'}:
        if quoted_total > 0:
            return 'Quoted and ready in cart'
        return 'Quote usually within 24h'
    if status in {'pending', 'confirmed', 'requested', 'awaiting approval', 'waiting for approval'}:
        return 'Requested by you, queued for production'

    est_hours = max(0.0, _to_float(order.get('estimated_print_hours'), 0))
    if status in {'printing', 'approved'}:
        if est_hours <= 0:
            return 'In production, usually 1-2 days'
        remaining_hours = max(1, int(round(est_hours)))
        if remaining_hours <= 24:
            return f'About {remaining_hours}h remaining'
        return f'About {max(1, int(round(remaining_hours / 24.0)))} day(s) remaining'

    return 'Timeline updates after admin review'


def _build_user_notifications(user_obj, user_orders):
    latest_seen_iso = str((user_obj or {}).get('notifications_last_seen_at') or '').strip()
    latest_seen_dt = _parse_iso_utc(latest_seen_iso)
    notifications = []
    added_keys = set()

    for order in user_orders[:40]:
        order_id = str(order.get('id') or '').strip()
        if not order_id:
            continue
        updated_dt = _order_last_modified(order) or datetime.min
        updated_iso = updated_dt.isoformat() if updated_dt != datetime.min else ''
        updated_label = updated_dt.strftime('%b %d, %H:%M') if updated_dt != datetime.min else 'Recently'
        status = str(order.get('status_display') or order.get('status') or 'Unknown').strip()
        status_key = status.lower()

        is_quoted = status_key == 'quoted' or (
            status_key == 'in cart'
            and (max(0.0, _to_float(order.get('print_price'), 0)) + max(0.0, _to_float(order.get('material_fee'), 0)) > 0)
        )

        if is_quoted:
            key = f'quote:{order_id}'
            if key not in added_keys:
                notifications.append({
                    'key': key,
                    'title': 'Quote ready in cart',
                    'message': f'Order #{order_id} has a quote and can be requested from your cart.',
                    'order_id': order_id,
                    'updated_at': updated_iso,
                    'updated_label': updated_label,
                    'is_unread': bool(updated_dt != datetime.min and (latest_seen_dt is None or updated_dt > latest_seen_dt)),
                })
                added_keys.add(key)

        if status_key in {'pending', 'confirmed', 'requested', 'printing', 'completed', 'done', 'delivered'}:
            key = f'status:{order_id}:{status_key}'
            if key not in added_keys:
                notifications.append({
                    'key': key,
                    'title': f'Order {status}',
                    'message': f'Order #{order_id} is now {status}.',
                    'order_id': order_id,
                    'updated_at': updated_iso,
                    'updated_label': updated_label,
                    'is_unread': bool(updated_dt != datetime.min and (latest_seen_dt is None or updated_dt > latest_seen_dt)),
                })
                added_keys.add(key)

        admin_note = str(order.get('admin_note') or '').strip()
        if admin_note:
            key = f'note:{order_id}'
            if key not in added_keys:
                snippet = admin_note if len(admin_note) <= 90 else (admin_note[:87] + '...')
                notifications.append({
                    'key': key,
                    'title': 'Admin note added',
                    'message': f'Order #{order_id}: {snippet}',
                    'order_id': order_id,
                    'updated_at': updated_iso,
                    'updated_label': updated_label,
                    'is_unread': bool(updated_dt != datetime.min and (latest_seen_dt is None or updated_dt > latest_seen_dt)),
                })
                added_keys.add(key)

    notifications.sort(key=lambda n: _parse_iso_utc(n.get('updated_at')) or datetime.min, reverse=True)
    return notifications[:20]

# --- USER ROUTES ---
def _default_featured_items():
    return [
        {
            'id': 'placeholder-1',
            'image_url': 'https://images.unsplash.com/photo-1581093458791-9f3c3900df4b?auto=format&fit=crop&w=1200&q=80',
            'title': 'Precision Gear Organizer',
            'makerworld_url': 'https://makerworld.com',
            'description': 'A practical desktop organizer with tight tolerances and clean edges.',
            'price': 15000,
            'suggested_filament': 'PLA',
            'target_user': 'ALL',
        },
        {
            'id': 'placeholder-2',
            'image_url': 'https://images.unsplash.com/photo-1581092580497-e0d23cbdf1dc?auto=format&fit=crop&w=1200&q=80',
            'title': 'Foldable Device Stand',
            'makerworld_url': 'https://makerworld.com',
            'description': 'A compact stand designed for desk setups and travel use.',
            'price': 12000,
            'suggested_filament': 'PETG',
            'target_user': 'ALL',
        },
    ]


def _build_user_portal_context(user_id, search_query=''):
    db = get_db()
    purged_ids = _purge_expired_soft_deletes(db)
    if purged_ids:
        save_db(db)
    settings = db.setdefault('settings', {'filaments': []})
    filaments, filaments_changed = _normalize_filaments(settings)
    if filaments_changed:
        save_db(db)

    user = next((u for u in db.get('users', []) if u.get('id') == user_id), None)
    control_settings = _load_control_center_settings()
    completed_statuses = {'completed', 'done', 'delivered'}
    inactive_statuses = completed_statuses | {'cancelled', 'declined', 'price denied', 'in cart', 'quoted'}

    owned_orders = [o for o in db.get('orders', []) if o.get('owner') == user_id]
    owned_orders = sorted(
        owned_orders,
        key=lambda o: _order_last_modified(o) or datetime.min,
        reverse=True,
    )
    owned_orders = _decorate_orders_with_pending_delete_date(owned_orders)

    cart_orders = [
        o for o in owned_orders
        if str(o.get('status') or '').strip().lower() in {'in cart', 'quoted', 'pending quote'}
    ]
    user_orders = [
        o for o in owned_orders
        if str(o.get('status') or '').strip().lower() not in {'in cart', 'quoted', 'pending quote'}
    ]

    normalized_query = (search_query or '').strip().lower()
    if normalized_query:
        filtered_orders = [
            o for o in user_orders
            if (
                normalized_query in str(o.get('id', '')).lower()
                or normalized_query in str(o.get('name', '')).lower()
                or normalized_query in str(o.get('nickname', '')).lower()
                or normalized_query in str(o.get('product_name', '')).lower()
                or normalized_query in str(o.get('status', '')).lower()
            )
        ]
    else:
        filtered_orders = user_orders

    featured_items = [
        f for f in db.get('featured_prints', [])
        if _featured_item_visible_to_user(f, user_id)
    ]
    if not featured_items:
        featured_items = _default_featured_items()

    in_stock_filaments = [
        f for f in filaments
        if not _to_bool(f.get('out_of_stock'), False)
        and _to_float(f.get('remaining_g'), 0) > 0
    ]
    spotlight_filament = random.choice(in_stock_filaments) if in_stock_filaments else None

    total_prints_completed = sum(
        1 for o in user_orders
        if str(o.get('status') or '').strip().lower() in completed_statuses
    )
    active_orders = sum(
        1 for o in user_orders
        if str(o.get('status') or '').strip().lower() not in inactive_statuses
    )
    material_credits = _compute_user_material_credits(user, user_orders)
    awaiting_approval_orders = [
        o for o in user_orders
        if str(o.get('status') or '').strip().lower() in {'pending', 'confirmed', 'requested', 'awaiting approval', 'waiting for approval'}
    ]
    eta_by_order = {
        str(o.get('id') or ''): _estimate_order_eta(o)
        for o in user_orders
        if o.get('id')
    }
    user_notifications = _build_user_notifications(user, user_orders)
    unread_user_notification_count = sum(1 for n in user_notifications if n.get('is_unread'))
    user_updates_token = ''
    if user_orders:
        last_dt = _order_last_modified(user_orders[0])
        if last_dt is not None:
            user_updates_token = last_dt.isoformat()

    order_presets = []
    if isinstance(user, dict):
        raw_presets = user.get('order_presets', [])
        if isinstance(raw_presets, list):
            for p in raw_presets:
                if not isinstance(p, dict):
                    continue
                pname = str(p.get('name') or '').strip()
                if not pname:
                    continue
                order_presets.append({
                    'id': str(p.get('id') or '')[:20] or str(uuid.uuid4())[:10],
                    'name': pname,
                    'makerworld_link': str(p.get('makerworld_link') or '').strip(),
                    'model_weight': max(0.0, _to_float(p.get('model_weight'), 0)),
                    'print_profile': str(p.get('print_profile') or '').strip(),
                    'color_mode': 'multi' if str(p.get('color_mode') or '').strip().lower() == 'multi' else 'single',
                    'single_filament': str(p.get('single_filament') or '').strip(),
                    'mappings': p.get('mappings', []) if isinstance(p.get('mappings', []), list) else [],
                    'preferred_delivery_date': str(p.get('preferred_delivery_date') or '').strip(),
                })

    member_since = 'Recently joined'
    if isinstance(user, dict) and user.get('created_at'):
        parsed = _parse_iso_utc(user.get('created_at'))
        if parsed is not None:
            member_since = parsed.strftime('%b %Y')

    cart_clear_ids = session.pop('cart_clear_ids', [])
    if not isinstance(cart_clear_ids, list):
        cart_clear_ids = []
    cart_clear_ids = [str(i).strip() for i in cart_clear_ids if str(i).strip()]

    return {
        'db': db,
        'user': user,
        'filaments': filaments,
        'featured_items': featured_items,
        'recent_orders': user_orders[:3],
        'all_orders': user_orders,
        'filtered_orders': filtered_orders,
        'cart_orders': cart_orders,
        'cart_count': len(cart_orders),
        'latest_order': user_orders[0] if user_orders else None,
        'spotlight_filament': spotlight_filament,
        'print_of_month': featured_items[0] if featured_items else None,
        'shop_open': control_settings.get('shop_open', True),
        'capacity_message': (control_settings.get('announcement_message') or 'We are currently at capacity!').strip(),
        'announcement_message': control_settings.get('announcement_message', ''),
        'active_orders_count': active_orders,
        'total_prints_completed': total_prints_completed,
        'material_credits': material_credits,
        'waiting_approval_orders': awaiting_approval_orders,
        'eta_by_order': eta_by_order,
        'user_notifications': user_notifications,
        'unread_user_notification_count': unread_user_notification_count,
        'user_updates_token': user_updates_token,
        'order_presets': order_presets,
        'member_since': member_since,
        'cart_clear_ids': cart_clear_ids,
    }


@app.route('/')
def index():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(session.get('user_id'))
    return render_template('user_home.html', active_tab='home', **context)


@app.route('/order')
def order_page():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(session.get('user_id'))
    prefill_link = (request.args.get('makerworld_link') or '').strip()
    reorder_id = (request.args.get('reorder') or '').strip()
    prefill_order_data = None
    if reorder_id:
        source = next(
            (
                o for o in context.get('all_orders', [])
                if str(o.get('id') or '') == reorder_id
            ),
            None,
        )
        if source:
            color_raw = str(source.get('color') or '').strip()
            mappings = []
            color_mode = 'single'
            single_filament = color_raw
            if '|' in color_raw and ':' in color_raw:
                color_mode = 'multi'
                single_filament = ''
                for seg in color_raw.split('|'):
                    seg = seg.strip()
                    if not seg:
                        continue
                    part, sep, filament_name = seg.partition(':')
                    mappings.append({
                        'part': part.strip() if sep else '',
                        'filament': (filament_name if sep else seg).strip(),
                    })
            prefill_order_data = {
                'source_order_id': reorder_id,
                'makerworld_link': source.get('link') or '',
                'model_weight': max(0.0, _to_float(source.get('print_weight_g'), 0)),
                'print_profile': source.get('profile') or '',
                'color_mode': color_mode,
                'single_filament': single_filament,
                'mappings': mappings,
                'preferred_delivery_date': source.get('preferred_delivery_date') or '',
            }
            if not prefill_link:
                prefill_link = str(source.get('link') or '').strip()

    return render_template(
        'user_order_form.html',
        active_tab='order',
        prefill_link=prefill_link,
        prefill_order_data=prefill_order_data,
        **context,
    )


@app.route('/cart')
def user_cart():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(session.get('user_id'))
    return render_template('user_cart.html', active_tab='cart', **context)


@app.route('/history')
def user_history():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    q = (request.args.get('q') or '').strip()
    context = _build_user_portal_context(session.get('user_id'), search_query=q)
    return render_template(
        'user_history.html',
        active_tab='history',
        search_query=q,
        **context,
    )


@app.route('/materials')
def user_materials():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(session.get('user_id'))
    return render_template('user_materials.html', active_tab='materials', **context)


@app.route('/help')
def user_help():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(session.get('user_id'))
    report_state = str(request.args.get('report') or '').strip().lower()
    return render_template(
        'user_help.html',
        active_tab='help',
        report_sent=(report_state == 'sent'),
        report_error=(report_state == 'error'),
        **context,
    )


@app.route('/help/report', methods=['POST'])
def submit_support_report():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    report_type = str(request.form.get('report_type') or 'bug').strip().lower()
    if report_type not in {'bug', 'suggestion', 'other'}:
        report_type = 'bug'

    title = str(request.form.get('title') or '').strip()
    details = str(request.form.get('details') or '').strip()
    if not title or not details:
        return redirect(url_for('user_help', report='error'))

    severity = str(request.form.get('severity') or 'medium').strip().lower()
    if severity not in {'low', 'medium', 'high', 'critical'}:
        severity = 'medium'

    page_url = str(request.form.get('page_url') or '').strip()
    if page_url and len(page_url) > 400:
        page_url = page_url[:400]

    screenshot_url = str(request.form.get('screenshot_url') or '').strip()
    if screenshot_url and len(screenshot_url) > 600:
        screenshot_url = screenshot_url[:600]

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    reports = _normalize_support_reports(settings)
    report_id = str(uuid.uuid4())[:12]

    report = {
        'id': report_id,
        'report_type': report_type,
        'status': 'new',
        'severity': severity,
        'title': title,
        'details': details,
        'steps': str(request.form.get('steps') or '').strip(),
        'expected': str(request.form.get('expected') or '').strip(),
        'actual': str(request.form.get('actual') or '').strip(),
        'page_url': page_url,
        'screenshot_url': screenshot_url,
        'browser': str(request.user_agent.browser or '').strip(),
        'os': str(request.user_agent.platform or '').strip(),
        'user_id': str(session.get('user_id') or '').strip() or None,
        'username': str(session.get('username') or 'Unknown user').strip() or 'Unknown user',
        'admin_note': '',
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'updated_at': datetime.utcnow().isoformat() + 'Z',
    }

    reports.insert(0, report)
    settings['support_reports'] = reports[:MAX_SUPPORT_REPORTS]

    issue_label = 'Bug report' if report_type == 'bug' else 'Suggestion'
    _add_admin_notification(
        db,
        notif_type='support_report',
        title=f'New {issue_label.lower()}',
        message=f"{report.get('username')} submitted: {title[:72]}",
        actor_user_id=report.get('user_id'),
    )
    save_db(db)
    return redirect(url_for('user_help', report='sent'))


@app.route('/user_register', methods=['GET', 'POST'])
def user_register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            return render_template('register.html', error='Username and password required')
        db = get_db()
        if any(u for u in db['users'] if u.get('username') == username):
            return render_template('register.html', error='Username already taken')
        user_id = str(uuid.uuid4())[:8]
        user = {
            'id': user_id,
            'username': username,
            'password_hash': generate_password_hash(password),
            'created_at': datetime.utcnow().isoformat(),
        }
        db['users'].append(user)
        save_db(db)
        session['user_id'] = user_id
        session['username'] = username
        return redirect(url_for('index'))
    return render_template('register.html')


@app.route('/user_login', methods=['GET', 'POST'])
def user_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        user = next((u for u in db.get('users', []) if u.get('username') == username), None)
        if user and check_password_hash(user.get('password_hash', ''), password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        return render_template('user_login.html', error='Invalid credentials')
    return render_template('user_login.html')


@app.route('/user_logout')
def user_logout():
    session.pop('user_id', None)
    session.pop('username', None)
    return redirect(url_for('user_login'))


@app.route('/search_orders', methods=['POST'])
def search_orders():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    query = (request.form.get('q') or '').strip()
    return redirect(url_for('user_history', q=query))

@app.route('/submit_order', methods=['POST'])
def submit_order():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    filaments, filaments_changed = _normalize_filaments(settings)
    if filaments_changed:
        save_db(db)

    control_settings = _load_control_center_settings()
    capacity_message = (control_settings.get('announcement_message') or 'We are currently at capacity!').strip()

    if not control_settings.get('shop_open', True):
        context = _build_user_portal_context(session.get('user_id'))
        return render_template(
            'user_order_form.html',
            active_tab='order',
            prefill_link=request.form.get('makerworld_link', ''),
            error=capacity_message,
            **context,
        )
    # Server-side: validate that the provided link is from an allowed domain
    link = request.form.get('makerworld_link', '').strip()
    if link:
        parsed = urlparse(link)
        hostname = parsed.netloc.lower()
        # If user omitted the scheme, urlparse will put the host in the path — try again with a default scheme
        if not hostname:
            parsed = urlparse('http://' + link)
            hostname = parsed.netloc.lower()
        if hostname.startswith('www.'):
            hostname = hostname[4:]
        allowed = ('makerworld.com', 'printables.com')
        if not any(hostname == a or hostname.endswith('.' + a) for a in allowed):
            context = _build_user_portal_context(session.get('user_id'))
            return render_template(
                'user_order_form.html',
                active_tab='order',
                prefill_link=link,
                error='Only makerworld.com or printables.com links are accepted.',
                **context,
            )
    order_id = str(uuid.uuid4())[:8]
    
    # Capture the name if the user provided one (used as nickname).
    raw_name = request.form.get('name', '').strip()
    provided_name = raw_name or "Unnamed Order"

    profile_choice = request.form.get('print_profile', '').strip()
    if not profile_choice:
        profile_choice = "1"

    preferred_delivery_date = (request.form.get('preferred_delivery_date') or '').strip()
    if preferred_delivery_date:
        try:
            chosen_date = date.fromisoformat(preferred_delivery_date)
            if chosen_date < date.today():
                preferred_delivery_date = ''
        except Exception:
            preferred_delivery_date = ''

    mode = request.form.get('color_mode')
    if mode == 'single':
        color_string = request.form.get('single_filament', 'Not Selected')
    else:
        parts = request.form.getlist('model_part[]')
        filaments = request.form.getlist('mapped_filament[]')
        mappings = [f"{p}: {f}" for p, f in zip(parts, filaments) if p.strip()]
        color_string = " | ".join(mappings) if mappings else "Multi-color"

    # Try to extract product name for MakerWorld links
    def extract_product_name(link):
        try:
            p = urlparse(link)
            hostname = p.netloc
            if not hostname:
                p = urlparse('http://' + link)
            path = p.path or ''
            # Look for any path segment that starts with digits + '-' then slug (e.g. "1646935-collapsing-katana-...")
            segments = [seg for seg in path.split('/') if seg]
            candidate = None
            for seg in segments:
                if re.match(r'^\d+-[A-Za-z0-9\-]+', seg):
                    candidate = seg
                    break
            # fallback: common '/models/' pattern
            if not candidate and '/models/' in path:
                candidate = path.split('/models/')[-1].split('/')[0]

            if candidate:
                parts = candidate.split('-')
                if len(parts) >= 2 and parts[0].isdigit():
                    name_slug = '-'.join(parts[1:])
                else:
                    name_slug = candidate
                name = unquote(name_slug).replace('-', ' ').strip()
                name = ' '.join(name.split())
                if name:
                    return name.title()
        except Exception:
            return None
        return None

    product_name = extract_product_name(link)

    # determine canonical name: prefer product name, fall back to provided name or a generic label
    canonical_name = product_name or provided_name or "Unnamed Order"
    # nickname only if user supplied something distinct
    nickname = None
    if raw_name and raw_name != canonical_name:
        nickname = raw_name

    new_order = {
        "id": order_id,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "name": canonical_name,
        "nickname": nickname,
        "owner": session.get('user_id'),
        "product_name": product_name,
        "admin_note": "",
        "messages": [],
        "link": request.form.get('makerworld_link'),
        "print_weight_g": max(0.0, _to_float(request.form.get('model_weight') or 0)),
        "profile": profile_choice,
        "color": color_string,
        "status": "In Cart",
        "print_price": "0",
        "material_fee": "0",
        "delivery_time": "TBD",
        "preferred_delivery_date": preferred_delivery_date,
        "estimated_print_hours": max(0.0, _to_float(request.form.get('estimated_print_hours') or 0)),
    }
    
    db['orders'].append(new_order)
    save_db(db)
    return redirect(url_for('user_cart'))


def _cart_payload_signature(owner_id, raw_link, product_name, color_string, profile, weight, preferred_date):
    return '||'.join([
        str(owner_id or '').strip(),
        str(raw_link or '').strip().lower(),
        str(product_name or '').strip().lower(),
        str(color_string or '').strip().lower(),
        str(profile or '').strip().lower(),
        '{:.3f}'.format(max(0.0, _to_float(weight, 0))),
        str(preferred_date or '').strip(),
    ])


def _parse_cart_quantity(value, default=1):
    try:
        qty = int(float(value))
    except (TypeError, ValueError):
        qty = default
    return max(1, min(99, qty))


@app.route('/checkout', methods=['POST'])
def checkout():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    import json as _json
    cart_json_str = request.form.get('cart_json', '[]')
    try:
        items = _json.loads(cart_json_str)
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []

    if not items:
        return redirect(url_for('user_cart'))

    db = get_db()
    owner_id = session.get('user_id')
    checked_out_item_ids = []
    processed_order_ids = set()
    orders = db.setdefault('orders', [])
    created_checkout_ids = []
    remove_cart_order_ids = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        incoming_order_id = str(item.get('orderId') or item.get('order_id') or '').strip()
        if not incoming_order_id or incoming_order_id in processed_order_ids:
            continue

        existing = next(
            (
                o for o in orders
                if str(o.get('id') or '') == incoming_order_id
                and o.get('owner') == owner_id
            ),
            None,
        )
        if existing is None:
            continue

        status_key = str(existing.get('status') or '').strip().lower()
        if status_key not in {'in cart', 'quoted', 'pending quote'}:
            continue

        quantity = _parse_cart_quantity(item.get('quantity'), default=1)
        quoted_unit_price = max(0.0, _to_float(item.get('estimatedPrice'), 0))
        if quoted_unit_price <= 0:
            existing_total = max(0.0, _to_float(existing.get('print_price'), 0)) + max(0.0, _to_float(existing.get('material_fee'), 0))
            existing_qty = _parse_cart_quantity(existing.get('quantity'), default=1)
            quoted_unit_price = existing_total / existing_qty if existing_qty > 0 else existing_total
        if quoted_unit_price <= 0:
            # Enforce priced-only checkout from cart.
            continue

        selected_profile = str(existing.get('profile') or item.get('profile') or 'Standard').strip() or 'Standard'
        selected_colors = str(existing.get('color') or '').strip()
        if not selected_colors:
            if str(item.get('colorMode') or '').strip().lower() == 'multi':
                mappings = item.get('multiMappings') or []
                selected_colors = ' | '.join(
                    '{}: {}'.format(str(m.get('part') or ''), str(m.get('filament') or '')).strip()
                    for m in mappings if isinstance(m, dict) and (m.get('part') or m.get('filament'))
                )
            else:
                selected_colors = str(item.get('singleFilament') or '').strip()

        total_price = int(round(quoted_unit_price * quantity))
        model_weight = max(0.0, _to_float(item.get('weight'), existing.get('print_weight_g') or 0))
        est_hours_per_unit = max(0.0, _to_float(existing.get('estimated_print_hours'), 0))

        new_order_id = str(uuid.uuid4())[:8]
        checkout_order = {
            'id': new_order_id,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
            'name': existing.get('name') or existing.get('product_name') or str(item.get('displayName') or 'Unnamed Order'),
            'nickname': existing.get('nickname'),
            'owner': owner_id,
            'product_name': existing.get('product_name') or existing.get('name') or str(item.get('displayName') or 'Unnamed Order'),
            'admin_note': existing.get('admin_note', ''),
            'messages': [],
            'link': existing.get('link') or str(item.get('link') or ''),
            'print_weight_g': model_weight * quantity,
            'profile': selected_profile,
            'color': selected_colors,
            'quantity': quantity,
            'status': 'Pending',
            'print_price': str(total_price),
            'material_fee': '0',
            'delivery_time': 'TBD',
            'preferred_delivery_date': existing.get('preferred_delivery_date') or str(item.get('preferredDeliveryDate') or ''),
            'estimated_print_hours': est_hours_per_unit * quantity,
            'source_cart_order_id': incoming_order_id,
            'final_unit_price': int(round(quoted_unit_price)),
            'final_total_price': total_price,
            'selected_print_profile': selected_profile,
            'selected_colors': selected_colors,
            'payment_status': 'Unpaid',
        }
        orders.append(checkout_order)
        created_checkout_ids.append(new_order_id)
        remove_cart_order_ids.add(incoming_order_id)
        processed_order_ids.add(incoming_order_id)

        item_id = str(item.get('id') or '').strip()
        if item_id:
            checked_out_item_ids.append(item_id)

    if not created_checkout_ids:
        return redirect(url_for('user_cart'))

    if remove_cart_order_ids:
        db['orders'] = [
            o for o in db.get('orders', [])
            if str(o.get('id') or '') not in remove_cart_order_ids
        ]
        for old_id in remove_cart_order_ids:
            _execute("DELETE FROM orders WHERE id = %s", (old_id,))

    save_db(db)
    if checked_out_item_ids:
        session['cart_clear_ids'] = checked_out_item_ids
    session['last_checkout_order_ids'] = created_checkout_ids
    return redirect(url_for('checkout_thank_you'))


@app.route('/checkout/thank-you')
def checkout_thank_you():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    order_ids = session.pop('last_checkout_order_ids', [])
    if not isinstance(order_ids, list):
        order_ids = []
    order_ids = [str(oid).strip() for oid in order_ids if str(oid).strip()]
    if not order_ids:
        return redirect(url_for('user_cart'))

    db = get_db()
    owner_id = session.get('user_id')
    orders = [
        o for o in db.get('orders', [])
        if str(o.get('id') or '') in set(order_ids)
        and o.get('owner') == owner_id
    ]
    orders = sorted(orders, key=lambda o: order_ids.index(str(o.get('id') or '')))
    grand_total = int(round(sum(max(0.0, _to_float(o.get('final_total_price', o.get('print_price')), 0)) for o in orders)))

    context = _build_user_portal_context(owner_id)
    return render_template(
        'checkout_thank_you.html',
        active_tab='history',
        checkout_orders=orders,
        checkout_order_ids=order_ids,
        checkout_grand_total=grand_total,
        **context,
    )


@app.route('/cart/save-item', methods=['POST'])
def save_cart_item():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400

    raw_link = str(payload.get('link') or '').strip()
    if not raw_link:
        return jsonify({'ok': False, 'error': 'Model link is required'}), 400

    item_id = str(payload.get('id') or '').strip()
    existing_order_id = str(payload.get('orderId') or '').strip()

    db = get_db()
    owner_id = session.get('user_id')

    # If this client item was already saved, reuse its order id.
    if existing_order_id:
        existing = next(
            (
                o for o in db.get('orders', [])
                if str(o.get('id') or '') == existing_order_id and o.get('owner') == owner_id
            ),
            None,
        )
        if existing is not None:
            return jsonify({'ok': True, 'order_id': existing_order_id})

    if item_id:
        existing_by_item = next(
            (
                o for o in db.get('orders', [])
                if o.get('owner') == owner_id
                and str(o.get('status') or '').strip().lower() == 'in cart'
                and str(o.get('cart_item_id') or '') == item_id
            ),
            None,
        )
        if existing_by_item is not None:
            return jsonify({'ok': True, 'order_id': str(existing_by_item.get('id') or '')})

    color_mode = str(payload.get('colorMode') or 'single')
    if color_mode == 'multi':
        mappings = payload.get('multiMappings') or []
        color_parts = [
            '{}: {}'.format(str(m.get('part') or ''), str(m.get('filament') or ''))
            for m in mappings if isinstance(m, dict) and m.get('part')
        ]
        color_string = ' | '.join(color_parts) if color_parts else 'Multi-color'
    else:
        color_string = str(payload.get('singleFilament') or 'Not Selected')

    product_name = str(payload.get('displayName') or 'Unnamed Order')
    weight = max(0.0, _to_float(payload.get('weight') or 0))
    profile = str(payload.get('profile') or '1').strip() or '1'
    preferred_date = str(payload.get('preferredDeliveryDate') or '').strip()
    if preferred_date:
        try:
            chosen_date = date.fromisoformat(preferred_date)
            if chosen_date < date.today():
                preferred_date = ''
        except Exception:
            preferred_date = ''

    incoming_signature = _cart_payload_signature(owner_id, raw_link, product_name, color_string, profile, weight, preferred_date)
    existing_by_signature = next(
        (
            o for o in db.get('orders', [])
            if o.get('owner') == owner_id
            and str(o.get('status') or '').strip().lower() == 'in cart'
            and _cart_payload_signature(
                owner_id,
                o.get('link'),
                o.get('name') or o.get('product_name'),
                o.get('color'),
                o.get('profile'),
                o.get('print_weight_g'),
                o.get('preferred_delivery_date'),
            ) == incoming_signature
        ),
        None,
    )
    if existing_by_signature is not None:
        return jsonify({'ok': True, 'order_id': str(existing_by_signature.get('id') or '')})

    quantity = _parse_cart_quantity(payload.get('quantity'), default=1)
    total_price = 0
    order_id = str(uuid.uuid4())[:8]

    new_order = {
        'id': order_id,
        'created_at': datetime.utcnow().isoformat(),
        'updated_at': datetime.utcnow().isoformat(),
        'name': product_name,
        'nickname': None,
        'owner': owner_id,
        'product_name': product_name,
        'admin_note': '',
        'messages': [],
        'link': raw_link,
        'print_weight_g': weight * quantity,
        'profile': profile,
        'color': color_string,
        'quantity': quantity,
        'status': 'In Cart',
        'print_price': str(total_price),
        'material_fee': '0',
        'delivery_time': 'TBD',
        'preferred_delivery_date': preferred_date,
        'estimated_print_hours': 0.0,
        'cart_item_id': item_id,
    }
    db['orders'].append(new_order)
    save_db(db)
    return jsonify({'ok': True, 'order_id': order_id})


@app.route('/cart/remove/<order_id>', methods=['POST'])
def remove_cart_item(order_id):
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    db = get_db()
    remaining_orders = []
    removed = False
    for order in db.get('orders', []):
        if (
            order.get('id') == order_id
            and order.get('owner') == session.get('user_id')
            and str(order.get('status') or '').strip().lower() == 'in cart'
        ):
            removed = True
            continue
        remaining_orders.append(order)

    if removed:
        db['orders'] = remaining_orders
        save_db(db)

    return redirect(url_for('user_cart'))


@app.route('/order/<order_id>/messages', methods=['GET', 'POST'])
def order_messages(order_id):
    is_admin = bool(session.get('logged_in'))
    user_id = session.get('user_id')
    if not is_admin and not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    db = get_db()
    order = next((o for o in db['orders'] if o['id'] == order_id), None)
    if not order:
        return jsonify({'error': 'Order not found'}), 404

    if not is_admin and order.get('owner') != user_id:
        return jsonify({'error': 'Unauthorized'}), 403

    if request.method == 'GET':
        return jsonify({'messages': order.get('messages', [])})

    # POST: append a message
    data = request.get_json() or request.form
    text = (data.get('text') or '').strip()
    sender = 'admin' if is_admin else 'user'
    if not text:
        return jsonify({'error': 'Empty message'}), 400

    msg = {
        'sender': sender,
        'text': text,
        'ts': datetime.utcnow().isoformat() + 'Z'
    }
    order.setdefault('messages', []).append(msg)

    if str(sender).strip().lower() == 'user':
        username = session.get('username') or order.get('owner') or 'Unknown user'
        _add_admin_notification(
            db,
            notif_type='user_message',
            title='New user message',
            message=f'{username} sent a new message for order #{order.get("id")}.',
            order_id=order.get('id'),
            actor_user_id=order.get('owner'),
        )

    save_db(db)
    return jsonify({'messages': order.get('messages', [])})

@app.route('/order/<order_id>')
def check_order_by_id(order_id):
    if not session.get('user_id') and not session.get('logged_in'):
        return redirect(url_for('user_login'))

    db = get_db()
    order = next((o for o in db['orders'] if o['id'] == order_id), None)
    if order and session.get('user_id') and not session.get('logged_in'):
        if order.get('owner') != session.get('user_id'):
            return "Order not found", 404
    if order:
        _decorate_orders_with_pending_delete_date([order])
        return render_template('order.html', order=order, order_eta=_estimate_order_eta(order))
    return "Order not found", 404


@app.route('/user/notifications/read_all', methods=['POST'])
def user_notifications_read_all():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    db = get_db()
    user = next((u for u in db.get('users', []) if u.get('id') == session.get('user_id')), None)
    if user is not None:
        user['notifications_last_seen_at'] = datetime.utcnow().isoformat()
        save_db(db)
    next_url = (request.form.get('next') or '').strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for('index'))


@app.route('/api/user/updates')
def user_updates_api():
    if not session.get('user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    context = _build_user_portal_context(session.get('user_id'))
    return jsonify({
        'latest_update_token': context.get('user_updates_token') or '',
        'unread_notifications': int(context.get('unread_user_notification_count') or 0),
    })


@app.route('/order/presets/save', methods=['POST'])
def save_order_preset():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    db = get_db()
    user = next((u for u in db.get('users', []) if u.get('id') == session.get('user_id')), None)
    if user is None:
        return redirect(url_for('order_page'))

    preset_name = (request.form.get('preset_name') or '').strip()
    if not preset_name:
        return redirect(url_for('order_page'))

    color_mode = 'multi' if (request.form.get('color_mode') or '').strip().lower() == 'multi' else 'single'
    single_filament = (request.form.get('single_filament') or '').strip()
    mappings = []
    parts = request.form.getlist('model_part[]')
    mapped_filaments = request.form.getlist('mapped_filament[]')
    for part, filament_name in zip(parts, mapped_filaments):
        p = str(part or '').strip()
        f = str(filament_name or '').strip()
        if not p or not f:
            continue
        mappings.append({'part': p, 'filament': f})

    preset = {
        'id': str(uuid.uuid4())[:10],
        'name': preset_name,
        'makerworld_link': (request.form.get('makerworld_link') or '').strip(),
        'model_weight': max(0.0, _to_float(request.form.get('model_weight') or 0)),
        'print_profile': (request.form.get('print_profile') or '').strip(),
        'color_mode': color_mode,
        'single_filament': single_filament,
        'mappings': mappings,
        'preferred_delivery_date': (request.form.get('preferred_delivery_date') or '').strip(),
    }

    presets = user.setdefault('order_presets', [])
    presets.insert(0, preset)
    user['order_presets'] = presets[:20]
    save_db(db)
    return redirect(url_for('order_page'))


@app.route('/order/presets/delete/<preset_id>', methods=['POST'])
def delete_order_preset(preset_id):
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    db = get_db()
    user = next((u for u in db.get('users', []) if u.get('id') == session.get('user_id')), None)
    if user is None:
        return redirect(url_for('order_page'))

    presets = user.get('order_presets', [])
    if isinstance(presets, list):
        user['order_presets'] = [p for p in presets if str(p.get('id') or '') != str(preset_id)]
        save_db(db)
    return redirect(url_for('order_page'))


@app.route('/order/<order_id>/invoice')
def order_invoice(order_id):
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    db = get_db()
    order = next(
        (
            o for o in db.get('orders', [])
            if o.get('id') == order_id and o.get('owner') == session.get('user_id')
        ),
        None,
    )
    if not order:
        return "Order not found", 404

    print_price = int(round(max(0.0, _to_float(order.get('print_price'), 0))))
    material_fee = int(round(max(0.0, _to_float(order.get('material_fee'), 0))))
    total = print_price + material_fee
    created = _parse_iso_utc(order.get('created_at'))
    created_text = created.strftime('%Y-%m-%d %H:%M UTC') if created else 'N/A'

    invoice_text = "\n".join([
        "CLIENT PRINTING INVOICE",
        "-----------------------",
        f"Invoice Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"Order ID: {order.get('id')}",
        f"Customer: {session.get('username') or session.get('user_id')}",
        f"Created At: {created_text}",
        f"Model: {order.get('name') or order.get('product_name') or 'Unnamed Order'}",
        f"Status: {order.get('status') or 'N/A'}",
        "",
        f"Print Price: Rp{print_price:,}",
        f"Material Fee: Rp{material_fee:,}",
        f"TOTAL: Rp{total:,}",
    ])

    response = Response(invoice_text, mimetype='text/plain; charset=utf-8')
    response.headers['Content-Disposition'] = f"attachment; filename=invoice-{order.get('id')}.txt"
    return response

@app.route('/check_order', methods=['POST'])
def check_order():
    order_id = request.form.get('order_id', '').strip()
    return redirect(url_for('check_order_by_id', order_id=order_id))

@app.route('/approve_price/<order_id>', methods=['POST'])
def approve_price(order_id):
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id and order['status'] == 'Waiting for Approval' and not _is_order_pending_deletion(order):
            order['status'] = 'Approved'
            save_db(db)
            break
    return redirect(url_for('check_order_by_id', order_id=order_id))

@app.route('/deny_price/<order_id>', methods=['POST'])
def deny_price(order_id):
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id and order['status'] == 'Waiting for Approval' and not _is_order_pending_deletion(order):
            order['status'] = 'Price Denied'
            save_db(db)
            break
    return redirect(url_for('check_order_by_id', order_id=order_id))

@app.route('/cancel_order/<order_id>', methods=['POST'])
def cancel_order(order_id):
    db = get_db()
    locked_statuses = ['Printing', 'Completed', 'Done', 'Delivered']
    for order in db['orders']:
        if order['id'] == order_id:
            if _is_order_pending_deletion(order):
                return "This order is awaiting deletion and cannot be changed right now.", 403
            if order['status'] in locked_statuses:
                return "This order is already being processed and cannot be cancelled.", 403
            order['status'] = 'Cancelled'
            save_db(db)
            break
    return redirect(url_for('check_order_by_id', order_id=order_id))

@app.route('/name_order/<order_id>', methods=['POST'])
def name_order(order_id):
    db = get_db()
    new_name = request.form.get('order_name', '').strip()
    for order in db['orders']:
        if order['id'] == order_id:
            # store as nickname; clear if empty
            order['nickname'] = new_name if new_name else None
            save_db(db)
            break
    return redirect(url_for('check_order_by_id', order_id=order_id))

# --- ADMIN ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'): return redirect(url_for('login'))
    payload = _load_dashboard_payload()
    db = payload['db']
    control_settings = _load_control_center_settings()
    before_purge_count = len(db.get('orders', []))
    _purge_expired_soft_deletes(db)
    purged_soft_deleted_orders = len(db.get('orders', [])) != before_purge_count
    settings = payload['settings']
    notifications = _normalize_admin_notifications(settings)
    unread_notification_count = sum(1 for n in notifications if not n.get('is_read'))
    support_reports = _normalize_support_reports(settings)
    open_support_reports_count = sum(1 for r in support_reports if str(r.get('status') or '').lower() in {'new', 'in_review', 'planned'})
    filaments, filaments_changed = _normalize_filaments(settings)
    if filaments_changed or purged_soft_deleted_orders:
        save_db(db)
    status_priority = {
        'in cart': 0,
        'quoted': 1,
        'pending quote': 1,
        'requested': 2,
        'waiting for approval': 2,
        'approved': 2,
        'printing': 3,
        'completed': 4,
        'done': 4,
        'delivered': 4,
        'price denied': 5,
        'cancelled': 5,
        'declined': 5,
    }

    def _dashboard_order_sort_key(order):
        status_key = str(order.get('status') or '').strip().lower()
        priority = status_priority.get(status_key, 4)
        if order.get('deleted_at'):
            priority = max(priority, 6)
        last_modified = _order_last_modified(order) or datetime.min
        return (priority, -last_modified.timestamp())

    active_orders = sorted(db['orders'], key=_dashboard_order_sort_key)

    now_utc = datetime.utcnow()
    fresh_cutoff = now_utc - timedelta(hours=24)
    fresh_order_ids = {
        o.get('id')
        for o in active_orders
        if (
            o.get('id')
            and not o.get('deleted_at')
            and ((_order_last_modified(o) or datetime.min) >= fresh_cutoff)
        )
    }

    # build map of user ids to usernames for display
    user_map = {u['id']: u['username'] for u in payload['users']}

    # Featured prints management
    featured_prints = payload['featured_prints']

    completed_statuses = {'completed', 'done', 'delivered'}
    user_credit_map = {}
    for user in payload['users']:
        uid = user.get('id')
        if not uid:
            continue
        owned_orders = [o for o in active_orders if o.get('owner') == uid]
        if user.get('material_credits') is None:
            completed_grams = sum(
                max(0.0, _to_float(o.get('print_weight_g'), 0))
                for o in owned_orders
                if str(o.get('status') or '').strip().lower() in completed_statuses
            )
            user_credit_map[uid] = int(completed_grams // 500.0)
        else:
            user_credit_map[uid] = max(0, int(round(_to_float(user.get('material_credits'), 0))))

    current_colors = ", ".join([f.get('name', '') for f in filaments])
    lifetime_profit = int(round(_get_business_stat('lifetime_profit', 0)))
    
    # Fetch print profiles from database
    print_profiles = []
    try:
        rows = _execute(
            "SELECT id, name, price_modifier, description, is_active, is_default FROM print_profiles ORDER BY is_default DESC, name",
            fetch=True
        ) or []
        print_profiles = [
            {'id': r[0], 'name': r[1], 'price_modifier': float(r[2] or 0), 'description': r[3], 'is_active': bool(r[4]), 'is_default': bool(r[5])}
            for r in rows
        ]
    except Exception:
        pass
    
    return render_template(
        'dashboard.html',
        orders=active_orders,
        current_colors=current_colors,
        filaments=filaments,
        user_map=user_map,
        users=payload['users'],
        user_credit_map=user_credit_map,
        featured_prints=featured_prints,
        print_profiles=print_profiles,
        fresh_order_ids=fresh_order_ids,
        control_settings=control_settings,
        supabase_connected=_is_database_connected(),
        lifetime_profit=lifetime_profit,
        admin_notifications=notifications,
        unread_notification_count=unread_notification_count,
        support_reports=support_reports,
        open_support_reports_count=open_support_reports_count,
    )


@app.route('/dashboard/support-reports/<report_id>', methods=['POST'])
def update_support_report(report_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    reports = _normalize_support_reports(settings)
    target = str(report_id or '').strip()
    next_status = str(request.form.get('status') or '').strip().lower()
    allowed_statuses = {'new', 'in_review', 'planned', 'resolved', 'dismissed'}
    if next_status not in allowed_statuses:
        next_status = 'new'
    next_note = str(request.form.get('admin_note') or '').strip()

    changed = False
    for report in reports:
        if str(report.get('id') or '') != target:
            continue
        if str(report.get('status') or '').lower() != next_status:
            report['status'] = next_status
            changed = True
        if str(report.get('admin_note') or '') != next_note:
            report['admin_note'] = next_note
            changed = True
        if changed:
            report['updated_at'] = datetime.utcnow().isoformat() + 'Z'
        break

    if changed:
        settings['support_reports'] = reports[:MAX_SUPPORT_REPORTS]
        save_db(db)

    return _redirect_back_to_dashboard('#reports-section')


@app.route('/dashboard/support-reports/<report_id>/delete', methods=['POST'])
def delete_support_report(report_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    reports = _normalize_support_reports(settings)
    target = str(report_id or '').strip()
    remaining_reports = [
        report for report in reports
        if str(report.get('id') or '').strip() != target
    ]

    if len(remaining_reports) != len(reports):
        settings['support_reports'] = remaining_reports[:MAX_SUPPORT_REPORTS]
        save_db(db)

    return _redirect_back_to_dashboard('#reports-section')


@app.route('/dashboard/notifications/read/<notification_id>', methods=['POST'])
def read_dashboard_notification(notification_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    changed = _mark_admin_notification_read(db, notification_id=notification_id)
    if changed:
        save_db(db)
    return _redirect_back_to_dashboard('#home-section')


@app.route('/dashboard/notifications/read_all', methods=['POST'])
def read_all_dashboard_notifications():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    changed = _mark_admin_notification_read(db, mark_all=True)
    if changed:
        save_db(db)
    return _redirect_back_to_dashboard('#home-section')


@app.route('/dashboard/users/<user_id>/credits', methods=['POST'])
def update_user_material_credits(user_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    credits_value = max(0, int(round(_to_float(request.form.get('material_credits'), 0))))
    db = get_db()
    updated = False
    for user in db.get('users', []):
        if user.get('id') == user_id:
            user['material_credits'] = credits_value
            updated = True
            break

    if updated:
        save_db(db)

    return _redirect_back_to_dashboard('#users-section')


@app.route('/admin/analytics')
def admin_analytics():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template(
        'analytics.html',
        lifetime_profit=int(round(_get_business_stat('lifetime_profit', 0))),
    )


@app.route('/api/chart-data')
def chart_data_api():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    timeframe = (request.args.get('timeframe') or 'week').strip().lower()
    return jsonify(_build_chart_data(timeframe))


@app.route('/api/record-entry', methods=['POST'])
def record_entry():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json() or {}
    try:
        amount = float(data.get('amount', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400

    if amount == 0:
        return jsonify({'error': 'Amount cannot be zero'}), 400

    entry_date_str = (data.get('date') or '').strip()
    try:
        entry_date = date.fromisoformat(entry_date_str) if entry_date_str else date.today()
    except ValueError:
        entry_date = date.today()

    rounded = int(round(amount))
    _record_daily_revenue(rounded, entry_date)
    _increment_business_stat('lifetime_profit', rounded)
    new_lifetime = int(round(_get_business_stat('lifetime_profit', 0)))
    return jsonify({'ok': True, 'lifetime_profit': new_lifetime})


@app.route('/api/reset-financials', methods=['POST'])
def reset_financials():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    _execute("DELETE FROM daily_revenue")
    _set_business_stat('lifetime_profit', 0)
    return jsonify({'ok': True})


@app.route('/dashboard/settings/update', methods=['POST'])
def update_control_center_settings():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    base_service_fee = max(0.0, _to_float(request.form.get('base_service_fee'), 0.0))
    price_per_gram = max(0.0, _to_float(request.form.get('price_per_gram', request.form.get('default_price_per_gram')), 0.0))
    power_cost_per_hour = max(0.0, _to_float(request.form.get('power_cost_per_hour'), 0.0))
    profit_margin = max(0.0, _to_float(request.form.get('profit_margin'), CONTROL_SETTING_DEFAULTS['profit_margin']))
    lifetime_total = max(0.0, _to_float(request.form.get('lifetime_total_plastic_used'), 0.0))
    lifetime_profit_override = max(0.0, _to_float(request.form.get('lifetime_profit'), _get_business_stat('lifetime_profit', 0.0)))
    announcement_message = (request.form.get('announcement_message') or '').strip()
    shop_open = request.form.get('shop_open') == 'on'

    _save_control_center_settings({
        'base_fee': int(round(base_service_fee)),
        'base_service_fee': int(round(base_service_fee)),
        'default_price_per_gram': int(round(price_per_gram)),
        'price_per_gram': int(round(price_per_gram)),
        'power_cost_per_hour': int(round(power_cost_per_hour)),
        'profit_margin': round(profit_margin, 4),
        'shop_open': shop_open,
        'lifetime_total_plastic_used': round(lifetime_total, 2),
        'announcement_message': announcement_message,
    })
    _set_business_stat('lifetime_profit', int(round(lifetime_profit_override)))

    return _redirect_back_to_dashboard('#settings-section')

@app.route('/delete_order/<order_id>', methods=['POST'])
def delete_order(order_id):
    """Soft-delete: marks order as awaiting deletion; purged after grace period."""
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id:
            _mark_order_pending_deletion(order, requested_by='admin')
            _add_admin_notification(
                db,
                notif_type='delete_pending',
                title='Order marked for deletion',
                message=f'Order #{order_id} is now awaiting deletion for {PENDING_DELETE_DAYS} days.',
                order_id=order_id,
                actor_user_id=order.get('owner'),
            )
            break
    save_db(db)
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/restore_order/<order_id>', methods=['POST'])
def restore_order(order_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    restored = False
    for order in db.get('orders', []):
        if order.get('id') == order_id and _is_order_pending_deletion(order):
            _restore_soft_deleted_order(order)
            _add_admin_notification(
                db,
                notif_type='delete_restore',
                title='Order restored',
                message=f'Order #{order_id} was restored and is no longer awaiting deletion.',
                order_id=order_id,
                actor_user_id=order.get('owner'),
            )
            restored = True
            break

    if restored:
        save_db(db)
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/hard_delete_order/<order_id>', methods=['POST'])
def hard_delete_order(order_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    before = len(db.get('orders', []))
    db['orders'] = [o for o in db.get('orders', []) if o.get('id') != order_id]
    deleted = len(db.get('orders', [])) != before

    if deleted:
        _execute("DELETE FROM orders WHERE id = %s", (order_id,))
        _add_admin_notification(
            db,
            notif_type='delete_finalized',
            title='Order permanently deleted',
            message=f'Order #{order_id} was permanently removed.',
            order_id=order_id,
        )
        save_db(db)
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/bulk_order_action', methods=['POST'])
def bulk_order_action():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    action = (request.form.get('action') or '').strip().lower()
    selected_ids = _parse_selected_order_ids()
    allowed_actions = {'delete', 'archive', 'decline', 'hard_delete'}
    if action not in allowed_actions or not selected_ids:
        return _redirect_back_to_dashboard('#orders-section')

    db = get_db()
    selected_set = set(selected_ids)
    updated_orders = []
    affected_count = 0

    for order in db.get('orders', []):
        order_id = order.get('id')
        if order_id not in selected_set:
            updated_orders.append(order)
            continue

        if action == 'hard_delete':
            if _is_order_pending_deletion(order):
                _execute("DELETE FROM orders WHERE id = %s", (order_id,))
                affected_count += 1
                continue
            updated_orders.append(order)
            continue

        if _is_order_pending_deletion(order):
            updated_orders.append(order)
            continue

        if action == 'delete':
            _mark_order_pending_deletion(order, requested_by='admin')
        elif action == 'archive':
            order['status'] = 'Delivered'
        elif action == 'decline':
            order['status'] = 'Declined'

        affected_count += 1
        updated_orders.append(order)

    if not affected_count:
        return _redirect_back_to_dashboard('#orders-section')

    db['orders'] = updated_orders
    action_labels = {
        'delete': 'marked for deletion',
        'archive': 'archived',
        'decline': 'declined',
        'hard_delete': 'permanently deleted',
    }
    _add_admin_notification(
        db,
        notif_type='bulk_order_action',
        title='Bulk order update',
        message=f'{affected_count} order(s) were {action_labels[action]}.',
    )
    save_db(db)
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/decline_order/<order_id>', methods=['POST'])
def decline_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id and not _is_order_pending_deletion(order):
            order['status'] = 'Declined'
            save_db(db)
            break
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/archive_order/<order_id>', methods=['POST'])
def archive_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id and not _is_order_pending_deletion(order):
            order['status'] = 'Delivered'
            save_db(db)
            break
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/soft_delete_order/<order_id>', methods=['POST'])
def soft_delete_order(order_id):
    """User-initiated soft delete — hides order and schedules 3-day purge."""
    db = get_db()
    user_id = session.get('user_id')
    is_admin = session.get('logged_in')
    for order in db['orders']:
        if order['id'] == order_id:
            if is_admin or (user_id and order.get('owner') == user_id):
                _mark_order_pending_deletion(order, requested_by='admin' if is_admin else 'user')
                if not is_admin:
                    _add_admin_notification(
                        db,
                        notif_type='delete_pending',
                        title='User requested deletion',
                        message=f'User {session.get("username") or user_id or "unknown"} marked order #{order_id} for deletion.',
                        order_id=order_id,
                        actor_user_id=user_id,
                    )
                save_db(db)
            break
    next_url = (request.form.get('next') or '').strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for('index'))


@app.route('/request_keep_order/<order_id>', methods=['POST'])
def request_keep_order(order_id):
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    db = get_db()
    user_id = session.get('user_id')
    requested = False
    for order in db.get('orders', []):
        if order.get('id') != order_id:
            continue
        if order.get('owner') != user_id:
            return "Order not found", 404
        if not _is_order_pending_deletion(order):
            return redirect(url_for('check_order_by_id', order_id=order_id))

        if not order.get('delete_restore_requested_at'):
            order['delete_restore_requested_at'] = datetime.utcnow().isoformat()
            order['delete_restore_requested_by'] = user_id
            order['updated_at'] = datetime.utcnow().isoformat()
            _add_admin_notification(
                db,
                notif_type='delete_keep_request',
                title='Keep request from user',
                message=f'User {session.get("username") or user_id} requested to keep order #{order_id}.',
                order_id=order_id,
                actor_user_id=user_id,
            )
            requested = True
        break

    if requested:
        save_db(db)
    return redirect(url_for('check_order_by_id', order_id=order_id))

@app.route('/dashboard/featured', methods=['POST'])
def add_featured_print():
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    title = request.form.get('title', '').strip()
    image_url = request.form.get('image_url', '').strip()
    makerworld_url = request.form.get('makerworld_url', '').strip()
    suggested_filament = request.form.get('suggested_filament', '').strip()
    suggested_color_list = [c.strip() for c in request.form.getlist('suggested_color_list[]') if c.strip()]
    suggested_colors = request.form.get('suggested_colors', '').strip()
    if suggested_color_list:
        suggested_colors = ' | '.join(suggested_color_list)
        if not suggested_filament:
            suggested_filament = suggested_color_list[0]
    else:
        suggested_colors = suggested_colors or suggested_filament
        if suggested_colors and not suggested_filament:
            suggested_filament = suggested_colors.split('|', 1)[0].split(':')[-1].strip()
    suggested_profile = request.form.get('suggested_profile', '').strip() or ''
    profile_options_raw = request.form.get('profile_options', '').strip() or ''
    profile_options = [p.strip() for p in profile_options_raw.split(',') if p.strip()]
    profile_pricing_raw = request.form.get('profile_pricing', '').strip() or ''
    profile_pricing = []
    if profile_pricing_raw:
        try:
            parsed_profile_pricing = json.loads(profile_pricing_raw)
            if isinstance(parsed_profile_pricing, list):
                for row in parsed_profile_pricing:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get('name') or '').strip()
                    if not name:
                        continue
                    try:
                        profile_price = float(row.get('price') or row.get('price_modifier') or 0)
                    except (TypeError, ValueError):
                        profile_price = 0.0
                    profile_pricing.append({
                        'name': name,
                        'price': profile_price,
                        'is_default': bool(row.get('is_default')),
                    })
        except Exception:
            profile_pricing = []
    price_value = 0.0
    if profile_pricing:
        if not any(p.get('is_default') for p in profile_pricing):
            profile_pricing[0]['is_default'] = True
        profile_options = [p['name'] for p in profile_pricing]
        suggested_profile = next((p['name'] for p in profile_pricing if p.get('is_default')), profile_options[0] if profile_options else '')
        price_value = next((p['price'] for p in profile_pricing if p.get('is_default')), profile_pricing[0]['price'] if profile_pricing else 0.0)
    category_options_raw = request.form.get('category_options', '').strip() or ''
    category_options = []
    if category_options_raw:
        try:
            category_options = json.loads(category_options_raw)
            if not isinstance(category_options, list):
                category_options = []
        except Exception:
            category_options = []
    parts_configuration_raw = request.form.get('parts_configuration', '').strip() or ''
    parts_configuration = []
    if parts_configuration_raw:
        try:
            parts_configuration = json.loads(parts_configuration_raw)
            if not isinstance(parts_configuration, list):
                parts_configuration = []
        except Exception:
            parts_configuration = []
    target_users = request.form.getlist('target_users')
    if not target_users:
        target_users = [request.form.get('target_user', 'ALL')]
    target_users = _normalize_target_users(target_users, fallback='ALL')
    target_user = 'ALL' if 'ALL' in target_users else target_users[0]

    if not (title and image_url and makerworld_url):
        return _redirect_back_to_dashboard('#suggested-section')

    new_item = {
        'id': str(uuid.uuid4())[:10],
        'title': title,
        'image_url': image_url,
        'makerworld_url': makerworld_url,
        'description': request.form.get('description', '').strip(),
        'price': price_value,
        'suggested_filament': suggested_filament,
        'suggested_colors': suggested_colors,
        'suggested_profile': suggested_profile,
        'profile_options': profile_options,
        'profile_pricing': profile_pricing,
        'category_options': category_options,
        'parts_configuration': parts_configuration,
        'target_user': target_user,
        'target_users': target_users,
    }

    db.setdefault('featured_prints', []).append(new_item)
    save_db(db)
    return _redirect_back_to_dashboard('#suggested-section')


@app.route('/dashboard/featured/edit/<item_id>', methods=['POST'])
def edit_featured_print(item_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    items = db.get('featured_prints', [])
    item = next((f for f in items if f.get('id') == item_id), None)
    if not item:
        return _redirect_back_to_dashboard('#suggested-section')

    title = request.form.get('title', '').strip()
    image_url = request.form.get('image_url', '').strip()
    makerworld_url = request.form.get('makerworld_url', '').strip()
    suggested_filament = request.form.get('suggested_filament', '').strip()
    suggested_color_list = [c.strip() for c in request.form.getlist('suggested_color_list[]') if c.strip()]
    suggested_colors = request.form.get('suggested_colors', '').strip()
    if suggested_color_list:
        suggested_colors = ' | '.join(suggested_color_list)
        if not suggested_filament:
            suggested_filament = suggested_color_list[0]
    else:
        suggested_colors = suggested_colors or suggested_filament
        if suggested_colors and not suggested_filament:
            suggested_filament = suggested_colors.split('|', 1)[0].split(':')[-1].strip()
    suggested_profile = request.form.get('suggested_profile', '').strip() or ''
    profile_options_raw = request.form.get('profile_options', '').strip() or ''
    profile_options = [p.strip() for p in profile_options_raw.split(',') if p.strip()]
    profile_pricing_raw = request.form.get('profile_pricing', '').strip() or ''
    profile_pricing = []
    if profile_pricing_raw:
        try:
            parsed_profile_pricing = json.loads(profile_pricing_raw)
            if isinstance(parsed_profile_pricing, list):
                for row in parsed_profile_pricing:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get('name') or '').strip()
                    if not name:
                        continue
                    try:
                        profile_price = float(row.get('price') or row.get('price_modifier') or 0)
                    except (TypeError, ValueError):
                        profile_price = 0.0
                    profile_pricing.append({
                        'name': name,
                        'price': profile_price,
                        'is_default': bool(row.get('is_default')),
                    })
        except Exception:
            profile_pricing = []
    price_value = float(item.get('price', 0) or 0)
    if profile_pricing:
        if not any(p.get('is_default') for p in profile_pricing):
            profile_pricing[0]['is_default'] = True
        profile_options = [p['name'] for p in profile_pricing]
        suggested_profile = next((p['name'] for p in profile_pricing if p.get('is_default')), profile_options[0] if profile_options else '')
        price_value = next((p['price'] for p in profile_pricing if p.get('is_default')), profile_pricing[0]['price'] if profile_pricing else 0.0)
    category_options_raw = request.form.get('category_options', '').strip() or ''
    category_options = []
    if category_options_raw:
        try:
            category_options = json.loads(category_options_raw)
            if not isinstance(category_options, list):
                category_options = []
        except Exception:
            category_options = []
    parts_configuration_raw = request.form.get('parts_configuration', '').strip() or ''
    parts_configuration = []
    if parts_configuration_raw:
        try:
            parts_configuration = json.loads(parts_configuration_raw)
            if not isinstance(parts_configuration, list):
                parts_configuration = []
        except Exception:
            parts_configuration = []

    target_users = request.form.getlist('target_users')
    if not target_users:
        target_users = [request.form.get('target_user', 'ALL')]
    target_users = _normalize_target_users(target_users, fallback='ALL')
    target_user = 'ALL' if 'ALL' in target_users else target_users[0]

    if not (title and image_url and makerworld_url):
        return _redirect_back_to_dashboard('#suggested-section')

    item['title'] = title
    item['image_url'] = image_url
    item['makerworld_url'] = makerworld_url
    item['description'] = request.form.get('description', '').strip()
    item['price'] = price_value
    item['suggested_filament'] = suggested_filament
    item['suggested_colors'] = suggested_colors
    item['suggested_profile'] = suggested_profile
    item['profile_options'] = profile_options
    item['profile_pricing'] = profile_pricing
    item['category_options'] = category_options
    item['parts_configuration'] = parts_configuration
    item['target_user'] = target_user
    item['target_users'] = target_users

    save_db(db)
    return _redirect_back_to_dashboard('#suggested-section')

@app.route('/dashboard/featured/delete/<item_id>', methods=['POST'])
def delete_featured_print(item_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    db['featured_prints'] = [f for f in db.get('featured_prints', []) if f.get('id') != item_id]
    save_db(db)
    return _redirect_back_to_dashboard('#suggested-section')

@app.route('/api/print-profiles', methods=['GET'])
def get_print_profiles():
    try:
        rows = _execute(
            "SELECT id, name, price_modifier, description, is_active, is_default FROM print_profiles WHERE is_active = TRUE ORDER BY is_default DESC, name",
            fetch=True
        ) or []
        profiles = [
            {'id': r[0], 'name': r[1], 'price_modifier': float(r[2] or 0), 'description': r[3], 'is_active': bool(r[4]), 'is_default': bool(r[5])}
            for r in rows
        ]
        # Guarantee a fallback default so the modal math never breaks
        if not any(p['is_default'] for p in profiles):
            if profiles:
                profiles[0]['is_default'] = True
            else:
                profiles = [{'id': None, 'name': 'Standard', 'price_modifier': 0.0, 'description': '', 'is_active': True, 'is_default': True}]
        return jsonify(profiles)
    except Exception:
        # Hard fallback: DB unreachable
        return jsonify([{'id': None, 'name': 'Standard', 'price_modifier': 0.0, 'description': '', 'is_active': True, 'is_default': True}]), 200

@app.route('/api/print-profiles/default', methods=['GET'])
def get_default_print_profile():
    """Returns only the default profile. Used by order modal auto-selection."""
    try:
        rows = _execute(
            "SELECT id, name, price_modifier, description FROM print_profiles WHERE is_default = TRUE AND is_active = TRUE LIMIT 1",
            fetch=True
        ) or []
        if rows:
            r = rows[0]
            return jsonify({'id': r[0], 'name': r[1], 'price_modifier': float(r[2] or 0), 'description': r[3], 'is_default': True})
        # No default row exists — fall back to first active profile
        rows = _execute(
            "SELECT id, name, price_modifier, description FROM print_profiles WHERE is_active = TRUE ORDER BY name LIMIT 1",
            fetch=True
        ) or []
        if rows:
            r = rows[0]
            return jsonify({'id': r[0], 'name': r[1], 'price_modifier': float(r[2] or 0), 'description': r[3], 'is_default': True})
    except Exception:
        pass
    # Ultimate fallback
    return jsonify({'id': None, 'name': 'Standard', 'price_modifier': 0.0, 'description': '', 'is_default': True})

@app.route('/dashboard/print-profiles', methods=['POST'])
def create_print_profile():
    if not session.get('logged_in'): return redirect(url_for('login'))
    name = (request.form.get('name') or '').strip()
    try:
        price_modifier = float(request.form.get('price_modifier', 0))
    except:
        price_modifier = 0
    description = (request.form.get('description') or '').strip()
    is_default = request.form.get('is_default') == 'true'
    try:
        if is_default:
            # Unset any existing default first (only one allowed)
            _execute("UPDATE print_profiles SET is_default = FALSE WHERE is_default = TRUE")
        _execute(
            "INSERT INTO print_profiles (name, price_modifier, description, is_active, is_default) VALUES (%s, %s, %s, TRUE, %s)",
            (name, price_modifier, description, is_default)
        )
    except Exception:
        pass
    return _redirect_back_to_dashboard('#print-profiles-section')

@app.route('/dashboard/print-profiles/edit/<int:profile_id>', methods=['POST'])
def edit_print_profile(profile_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    name = (request.form.get('name') or '').strip()
    try:
        price_modifier = float(request.form.get('price_modifier', 0))
    except:
        price_modifier = 0
    description = (request.form.get('description') or '').strip()
    is_active = request.form.get('is_active') == 'true'
    is_default = request.form.get('is_default') == 'true'
    try:
        if is_default:
            # Clear the current default on all OTHER profiles first
            _execute("UPDATE print_profiles SET is_default = FALSE WHERE is_default = TRUE AND id != %s", (profile_id,))
        _execute(
            "UPDATE print_profiles SET name = %s, price_modifier = %s, description = %s, is_active = %s, is_default = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (name, price_modifier, description, is_active, is_default, profile_id)
        )
    except Exception:
        pass
    return _redirect_back_to_dashboard('#print-profiles-section')

@app.route('/dashboard/print-profiles/delete/<int:profile_id>', methods=['POST'])
def delete_print_profile(profile_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    try:
        _execute("DELETE FROM print_profiles WHERE id = %s", (profile_id,))
    except Exception:
        pass
    return _redirect_back_to_dashboard('#print-profiles-section')

@app.route('/create_featured_order', methods=['POST'])
def create_featured_order():
    if not session.get('user_id'):
        return jsonify({'error': 'Not authorized'}), 401

    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    makerworld_link = (data.get('makerworld_link') or '').strip()
    try:
        price_val = float(data.get('price', 0))
    except Exception:
        price_val = 0

    # allow featured items to suggest a specific profile and/or multi-color mapping
    profile_choice = (data.get('profile') or data.get('suggested_profile') or '').strip() or 'Standard'
    suggested_colors = (data.get('suggested_colors') or data.get('filament') or '').strip()
    category_choices = data.get('category_choices') or []
    if not isinstance(category_choices, list):
        category_choices = []

    if not title or not makerworld_link or price_val <= 0:
        return jsonify({'error': 'Missing required fields'}), 400

    db = get_db()
    owner_id = session.get('user_id')
    now = datetime.utcnow()
    for existing in db.get('orders', []):
        if existing.get('owner') != owner_id:
            continue
        status_key = str(existing.get('status') or '').strip().lower()
        if status_key not in {'quoted', 'in cart', 'requested', 'pending quote'}:
            continue
        if str(existing.get('link') or '').strip() != makerworld_link:
            continue
        if str(existing.get('name') or existing.get('product_name') or '').strip() != title:
            continue
        if str(existing.get('profile') or '').strip() != profile_choice:
            continue
        if str(existing.get('color') or '').strip() != suggested_colors:
            continue
        existing_price = max(0, int(round(_to_float(existing.get('print_price'), 0))))
        if existing_price != int(round(price_val)):
            continue
        created_at = _parse_iso_utc(existing.get('created_at'))
        if created_at and (now - created_at).total_seconds() <= 120:
            return jsonify({'order_id': str(existing.get('id') or '')})

    order_id = str(uuid.uuid4())[:8]
    new_order = {
        'id': order_id,
        'name': title,
        'nickname': None,
        'owner': session.get('user_id'),
        'product_name': title,
        'admin_note': '',
        'messages': [],
        'link': makerworld_link,
        'profile': profile_choice,
        'color': suggested_colors,
        'category_choices': category_choices,
        'status': 'Quoted',
        'quote_notified_at': datetime.utcnow().isoformat(),
        'print_price': str(int(price_val)),
        'material_fee': '0',
        'delivery_time': 'TBD',
        'fixed_price': True,
        'suggested_colors': suggested_colors,
        'suggested_profile': profile_choice,
        'part_color_choices': category_choices,
    }

    db.setdefault('orders', []).append(new_order)
    username = session.get('username') or owner_id or 'A user'
    _add_admin_notification(
        db,
        notif_type='featured_order',
        title='Featured print ordered',
        message=f'{username} ordered {title}.',
        order_id=order_id,
        actor_user_id=owner_id,
    )
    save_db(db)

    return jsonify({'order_id': order_id})

@app.route('/update_order/<order_id>', methods=['POST'])
def update_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    completed_statuses = {'done', 'delivered', 'completed'}
    for order in db['orders']:
        if order['id'] == order_id:
            old_status = str(order.get('status') or '').strip().lower()
            requested_status = str(request.form.get('status') or '').strip() or 'In Cart'

            price_val = max(0, int(round(_to_float(request.form.get('print_price'), order.get('print_price') or 0))))
            fee_val = max(0, int(round(_to_float(request.form.get('material_fee'), order.get('material_fee') or 0))))
            order['print_price'] = str(price_val)
            order['material_fee'] = str(fee_val)

            requested_key = requested_status.lower()
            if requested_key in {'waiting for approval', 'awaiting approval', 'approved', 'pending quote'}:
                requested_status = 'Quoted'
                requested_key = 'quoted'

            if requested_key == 'in cart' and (price_val + fee_val) > 0:
                requested_status = 'Quoted'
                requested_key = 'quoted'
            elif requested_key == 'quoted' and (price_val + fee_val) <= 0:
                requested_status = 'In Cart'
                requested_key = 'in cart'

            if requested_key == 'quoted' and not order.get('quote_notified_at'):
                order['quote_notified_at'] = datetime.utcnow().isoformat()

            if not _is_order_pending_deletion(order):
                order['status'] = requested_status
            order['delivery_time'] = request.form.get('delivery_time', 'TBD')
            order['print_weight_g'] = max(0.0, _to_float(request.form.get('print_weight_g'), order.get('print_weight_g') or 0))
            order['estimated_print_hours'] = max(0.0, _to_float(request.form.get('estimated_print_hours'), order.get('estimated_print_hours') or 0))
            # Save admin notes if provided
            order['admin_note'] = request.form.get('admin_note', order.get('admin_note', ''))
            order['updated_at'] = datetime.utcnow().isoformat()

            new_status = str(order.get('status') or '').strip().lower()
            is_marking_completed = new_status == 'completed' and old_status != 'completed'
            if (
                new_status in completed_statuses
                and old_status not in completed_statuses
                and not order.get('plastic_counted')
                and _to_float(order.get('print_weight_g'), 0) > 0
            ):
                control_settings = _load_control_center_settings()
                control_settings['lifetime_total_plastic_used'] = round(
                    _to_float(control_settings.get('lifetime_total_plastic_used'), 0)
                    + _to_float(order.get('print_weight_g'), 0),
                    2,
                )
                _save_control_center_settings(control_settings)
                order['plastic_counted'] = True

            break
    save_db(db)
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/dashboard/orders/export.csv')
def export_orders_csv():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    orders = db.get('orders', [])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'id',
        'created_at',
        'updated_at',
        'owner',
        'product_name',
        'status',
        'print_weight_g',
        'estimated_print_hours',
        'print_price',
        'material_fee',
        'delivery_time',
    ])
    for order in orders:
        writer.writerow([
            order.get('id', ''),
            order.get('created_at', ''),
            order.get('updated_at', ''),
            order.get('owner', ''),
            order.get('product_name') or order.get('name', ''),
            order.get('status', ''),
            order.get('print_weight_g', ''),
            order.get('estimated_print_hours', ''),
            order.get('print_price', ''),
            order.get('material_fee', ''),
            order.get('delivery_time', ''),
        ])

    csv_text = buffer.getvalue()
    return app.response_class(
        csv_text,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=orders-export.csv'},
    )


@app.route('/dashboard/orders/clear-completed', methods=['POST'])
def clear_completed_orders():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    db = get_db()
    completed_statuses = {'done', 'delivered', 'completed'}
    db['orders'] = [
        order for order in db.get('orders', [])
        if str(order.get('status') or '').strip().lower() not in completed_statuses
    ]
    save_db(db)
    return _redirect_back_to_dashboard('#settings-section')

@app.route('/update_colors', methods=['POST'])
def update_colors():
    if not session.get('logged_in'): return redirect(url_for('login'))
    raw_colors = request.form.get('colors_list', '')
    color_list = [c.strip() for c in raw_colors.split(',') if c.strip()]
    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    settings['filaments'] = [
        {
            'id': str(uuid.uuid4())[:8],
            'name': c,
            'brand': 'Generic',
            'material': 'PLA',
            'hex': _default_hex_for_name(c),
            'total_g': 1000,
            'remaining_g': 1000,
            'out_of_stock': False,
        }
        for c in color_list
    ]
    save_db(db)
    return _redirect_back_to_dashboard('#filaments-section')

@app.route('/dashboard/filaments/add', methods=['POST'])
def add_filament():
    if not session.get('logged_in'): return redirect(url_for('login'))
    filament_name = request.form.get('filament_name', '').strip()
    if not filament_name:
        return _redirect_back_to_dashboard('#filaments-section')

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    filaments, _ = _normalize_filaments(settings)
    existing = {f.get('name', '').lower(): f for f in filaments}
    if filament_name.lower() not in existing:
        total_g = int(request.form.get('total_g') or 1000)
        total_g = max(1, total_g)
        remaining_g = int(request.form.get('remaining_g') or total_g)
        remaining_g = max(0, min(remaining_g, total_g))
        filaments.append({
            'id': str(uuid.uuid4())[:8],
            'name': filament_name,
            'brand': (request.form.get('brand') or 'Generic').strip() or 'Generic',
            'material': (request.form.get('material') or 'PLA').strip().upper() or 'PLA',
            'hex': (request.form.get('hex') or _default_hex_for_name(filament_name)).strip() or _default_hex_for_name(filament_name),
            'total_g': total_g,
            'remaining_g': remaining_g,
            'out_of_stock': request.form.get('out_of_stock') == 'true',
        })
        settings['filaments'] = filaments
        save_db(db)
    return _redirect_back_to_dashboard('#filaments-section')

@app.route('/dashboard/filaments/delete', methods=['POST'])
def delete_filament():
    if not session.get('logged_in'): return redirect(url_for('login'))
    filament_name = request.form.get('filament_name', '').strip()
    filament_id = request.form.get('filament_id', '').strip()
    if not filament_name:
        if not filament_id:
            return _redirect_back_to_dashboard('#filaments-section')

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    filaments, _ = _normalize_filaments(settings)
    if filament_id:
        settings['filaments'] = [f for f in filaments if f.get('id') != filament_id]
    else:
        settings['filaments'] = [f for f in filaments if f.get('name') != filament_name]
    save_db(db)
    return _redirect_back_to_dashboard('#filaments-section')


@app.route('/dashboard/filaments/log_usage/<filament_id>', methods=['POST'])
def log_filament_usage(filament_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    used_g = int(request.form.get('used_g') or 0)
    if used_g <= 0:
        return _redirect_back_to_dashboard('#filaments-section')

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    filaments, _ = _normalize_filaments(settings)
    for filament in filaments:
        if filament.get('id') == filament_id:
            remaining = int(filament.get('remaining_g') or 0)
            filament['remaining_g'] = max(0, remaining - used_g)
            filament['out_of_stock'] = filament['remaining_g'] <= 0
            break
    settings['filaments'] = filaments
    save_db(db)
    return _redirect_back_to_dashboard('#filaments-section')


@app.route('/dashboard/filaments/edit/<filament_id>', methods=['POST'])
def edit_filament(filament_id):
    if not session.get('logged_in'): return redirect(url_for('login'))

    db = get_db()
    settings = db.setdefault('settings', {'filaments': []})
    filaments, _ = _normalize_filaments(settings)
    for filament in filaments:
        if filament.get('id') != filament_id:
            continue
        name = (request.form.get('name') or filament.get('name') or '').strip()
        brand = (request.form.get('brand') or filament.get('brand') or 'Generic').strip()
        material = (request.form.get('material') or filament.get('material') or 'PLA').strip().upper()
        color_hex = (request.form.get('hex') or filament.get('hex') or _default_hex_for_name(name)).strip()
        total_g = int(request.form.get('total_g') or filament.get('total_g') or 1000)
        total_g = max(1, total_g)
        remaining_g = int(request.form.get('remaining_g') or filament.get('remaining_g') or total_g)
        remaining_g = max(0, min(remaining_g, total_g))
        out_of_stock = request.form.get('out_of_stock') == 'true'

        filament['name'] = name or filament.get('name') or 'Filament'
        filament['brand'] = brand or 'Generic'
        filament['material'] = material or 'PLA'
        filament['hex'] = color_hex or _default_hex_for_name(filament['name'])
        filament['total_g'] = total_g
        filament['remaining_g'] = remaining_g
        filament['out_of_stock'] = out_of_stock or remaining_g <= 0
        break

    settings['filaments'] = filaments
    save_db(db)
    return _redirect_back_to_dashboard('#filaments-section')


def _merge_unique_by_id(preferred_rows, incoming_rows):
    """Merge two lists of dicts by id while preserving preferred rows on conflict."""
    merged = {}
    for row in incoming_rows or []:
        row_id = (row or {}).get('id') if isinstance(row, dict) else None
        if row_id:
            merged[row_id] = row

    for row in preferred_rows or []:
        row_id = (row or {}).get('id') if isinstance(row, dict) else None
        if row_id:
            merged[row_id] = row

    return list(merged.values())


def import_jsonbin_dump(path):
    """Import a JSON dump (as-exported from JSONBin) into the configured Postgres database."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Failed to read JSON file: {e}")
        return

    if isinstance(data, dict) and 'record' in data and isinstance(data['record'], dict):
        data = data['record']

    data.setdefault('settings', {'filaments': []})
    data.setdefault('users', [])
    data.setdefault('orders', [])
    data.setdefault('featured_prints', [])

    save_db(data, full_replace=True)
    print(f"Imported JSON data from {path} into Postgres database.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the 3D print orders app.')
    parser.add_argument('--import', dest='import_path', help='Import JSONBin dump (exported JSON) into Postgres (full replace).')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the Flask server on')
    args = parser.parse_args()

    if args.import_path:
        import_jsonbin_dump(args.import_path)
    else:
        app.run(debug=True, port=args.port)