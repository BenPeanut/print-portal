import argparse
import atexit
import csv
import io
import os
import random
import threading
import uuid
import json
from urllib.parse import urlparse, unquote
import re
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timedelta

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
        if not deleted_at:
            continue
        try:
            purge_at = datetime.fromisoformat(deleted_at) + timedelta(days=3)
            order['pending_delete_on'] = purge_at.strftime('%b %d, %Y')
        except Exception:
            # Keep it visible even if timestamp parsing fails.
            order['pending_delete_on'] = None
    return orders


def _purge_expired_soft_deletes(db):
    """Drop orders whose deleted_at timestamp is older than 3 days."""
    cutoff = datetime.utcnow() - timedelta(days=3)
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
    settings = db.setdefault('settings', {'filaments': []})
    filaments, filaments_changed = _normalize_filaments(settings)
    if filaments_changed:
        save_db(db)

    user = next((u for u in db.get('users', []) if u.get('id') == user_id), None)
    control_settings = _load_control_center_settings()
    completed_statuses = {'completed', 'done', 'delivered'}
    inactive_statuses = completed_statuses | {'cancelled', 'declined', 'price denied'}

    user_orders = [o for o in db.get('orders', []) if o.get('owner') == user_id]
    user_orders = sorted(
        user_orders,
        key=lambda o: _order_last_modified(o) or datetime.min,
        reverse=True,
    )
    user_orders = _decorate_orders_with_pending_delete_date(user_orders)

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
    waiting_approval_orders = [
        o for o in user_orders
        if str(o.get('status') or '').strip().lower() == 'waiting for approval'
    ]

    member_since = 'Recently joined'
    if isinstance(user, dict) and user.get('created_at'):
        parsed = _parse_iso_utc(user.get('created_at'))
        if parsed is not None:
            member_since = parsed.strftime('%b %Y')

    return {
        'db': db,
        'user': user,
        'filaments': filaments,
        'featured_items': featured_items,
        'recent_orders': user_orders[:3],
        'all_orders': user_orders,
        'filtered_orders': filtered_orders,
        'latest_order': user_orders[0] if user_orders else None,
        'spotlight_filament': spotlight_filament,
        'print_of_month': featured_items[0] if featured_items else None,
        'shop_open': control_settings.get('shop_open', True),
        'capacity_message': (control_settings.get('announcement_message') or 'We are currently at capacity!').strip(),
        'announcement_message': control_settings.get('announcement_message', ''),
        'active_orders_count': active_orders,
        'total_prints_completed': total_prints_completed,
        'material_credits': material_credits,
        'waiting_approval_orders': waiting_approval_orders,
        'member_since': member_since,
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
    return render_template(
        'user_order_form.html',
        active_tab='order',
        prefill_link=prefill_link,
        **context,
    )


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
    return render_template('user_help.html', active_tab='help', **context)


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
        user = {'id': user_id, 'username': username, 'password_hash': generate_password_hash(password), 'created_at': datetime.utcnow().isoformat()}
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
        "status": "Pending Quote",
        "print_price": "0",
        "material_fee": "0",
        "delivery_time": "TBD",
        "estimated_print_hours": max(0.0, _to_float(request.form.get('estimated_print_hours') or 0)),
    }
    
    db['orders'].append(new_order)
    save_db(db)
    return redirect(url_for('check_order_by_id', order_id=order_id))


@app.route('/order/<order_id>/messages', methods=['GET', 'POST'])
def order_messages(order_id):
    db = get_db()
    order = next((o for o in db['orders'] if o['id'] == order_id), None)
    if not order:
        return jsonify({'error': 'Order not found'}), 404

    if request.method == 'GET':
        return jsonify({'messages': order.get('messages', [])})

    # POST: append a message
    data = request.get_json() or request.form
    text = (data.get('text') or '').strip()
    sender = data.get('sender') or 'user'
    if not text:
        return jsonify({'error': 'Empty message'}), 400

    msg = {
        'sender': sender,
        'text': text,
        'ts': datetime.utcnow().isoformat() + 'Z'
    }
    order.setdefault('messages', []).append(msg)
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
        return render_template('order.html', order=order)
    return "Order not found", 404


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
        if order['id'] == order_id and order['status'] == 'Waiting for Approval':
            order['status'] = 'Approved'
            save_db(db)
            break
    return redirect(url_for('check_order_by_id', order_id=order_id))

@app.route('/deny_price/<order_id>', methods=['POST'])
def deny_price(order_id):
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id and order['status'] == 'Waiting for Approval':
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
    filaments, filaments_changed = _normalize_filaments(settings)
    if filaments_changed or purged_soft_deleted_orders:
        save_db(db)
    active_orders = db['orders']

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
    return render_template(
        'dashboard.html',
        orders=active_orders,
        current_colors=current_colors,
        filaments=filaments,
        user_map=user_map,
        users=payload['users'],
        user_credit_map=user_credit_map,
        featured_prints=featured_prints,
        control_settings=control_settings,
        supabase_connected=_is_database_connected(),
        lifetime_profit=lifetime_profit,
    )


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
    """Soft-delete: marks order with deleted_at; purged after 3 days."""
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id:
            order['deleted_at'] = datetime.utcnow().isoformat()
            break
    save_db(db)
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/decline_order/<order_id>', methods=['POST'])
def decline_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id:
            order['status'] = 'Declined'
            save_db(db)
            break
    return _redirect_back_to_dashboard('#orders-section')


@app.route('/archive_order/<order_id>', methods=['POST'])
def archive_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    for order in db['orders']:
        if order['id'] == order_id:
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
                order['deleted_at'] = datetime.utcnow().isoformat()
                save_db(db)
            break
    return redirect(url_for('index'))

@app.route('/dashboard/featured', methods=['POST'])
def add_featured_print():
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    title = request.form.get('title', '').strip()
    image_url = request.form.get('image_url', '').strip()
    makerworld_url = request.form.get('makerworld_url', '').strip()
    price = request.form.get('price', '').strip()
    suggested_filament = request.form.get('suggested_filament', '').strip()
    suggested_colors = request.form.get('suggested_colors', '').strip() or suggested_filament
    suggested_profile = request.form.get('suggested_profile', '').strip() or ''
    profile_options_raw = request.form.get('profile_options', '').strip() or ''
    profile_options = [p.strip() for p in profile_options_raw.split(',') if p.strip()]
    category_options_raw = request.form.get('category_options', '').strip() or ''
    category_options = []
    if category_options_raw:
        try:
            category_options = json.loads(category_options_raw)
            if not isinstance(category_options, list):
                category_options = []
        except Exception:
            category_options = []
    target_users = request.form.getlist('target_users')
    if not target_users:
        target_users = [request.form.get('target_user', 'ALL')]
    target_users = _normalize_target_users(target_users, fallback='ALL')
    target_user = 'ALL' if 'ALL' in target_users else target_users[0]

    if not (title and image_url and makerworld_url and price):
        return _redirect_back_to_dashboard('#suggested-section')

    new_item = {
        'id': str(uuid.uuid4())[:10],
        'title': title,
        'image_url': image_url,
        'makerworld_url': makerworld_url,
        'description': request.form.get('description', '').strip(),
        'price': float(price),
        'suggested_filament': suggested_filament,
        'suggested_colors': suggested_colors,
        'suggested_profile': suggested_profile,
        'profile_options': profile_options,
        'category_options': category_options,
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
    price = request.form.get('price', '').strip()
    suggested_filament = request.form.get('suggested_filament', '').strip()
    suggested_colors = request.form.get('suggested_colors', '').strip() or suggested_filament
    suggested_profile = request.form.get('suggested_profile', '').strip() or ''
    profile_options_raw = request.form.get('profile_options', '').strip() or ''
    profile_options = [p.strip() for p in profile_options_raw.split(',') if p.strip()]
    category_options_raw = request.form.get('category_options', '').strip() or ''
    category_options = []
    if category_options_raw:
        try:
            category_options = json.loads(category_options_raw)
            if not isinstance(category_options, list):
                category_options = []
        except Exception:
            category_options = []

    target_users = request.form.getlist('target_users')
    if not target_users:
        target_users = [request.form.get('target_user', 'ALL')]
    target_users = _normalize_target_users(target_users, fallback='ALL')
    target_user = 'ALL' if 'ALL' in target_users else target_users[0]

    if not (title and image_url and makerworld_url and price):
        return _redirect_back_to_dashboard('#suggested-section')

    item['title'] = title
    item['image_url'] = image_url
    item['makerworld_url'] = makerworld_url
    item['description'] = request.form.get('description', '').strip()
    item['price'] = float(price)
    item['suggested_filament'] = suggested_filament
    item['suggested_colors'] = suggested_colors
    item['suggested_profile'] = suggested_profile
    item['profile_options'] = profile_options
    item['category_options'] = category_options
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
    profile_choice = (data.get('profile') or data.get('suggested_profile') or '').strip()
    suggested_colors = (data.get('suggested_colors') or data.get('filament') or '').strip()
    category_choices = data.get('category_choices') or []
    if not isinstance(category_choices, list):
        category_choices = []

    if not title or not makerworld_link or price_val <= 0:
        return jsonify({'error': 'Missing required fields'}), 400

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
        # use the existing “Waiting for Approval” status so user can confirm in the order page
        'status': 'Waiting for Approval',
        'print_price': str(int(price_val)),
        'material_fee': '0',
        'delivery_time': 'TBD',
        'fixed_price': True,
        'suggested_colors': suggested_colors,
        'suggested_profile': profile_choice,
    }

    db = get_db()
    db.setdefault('orders', []).append(new_order)
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
            order['status'] = request.form.get('status')
            order['print_price'] = request.form.get('print_price', '0')
            order['material_fee'] = request.form.get('material_fee', '0')
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