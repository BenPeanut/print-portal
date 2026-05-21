import argparse
import atexit
import csv
import io
import math
import os
import sys
from pathlib import Path
import random
import threading
import uuid
import json
import importlib
from typing import Any
from dotenv import load_dotenv
from urllib.parse import urlparse, unquote
import re
import psycopg2
import requests
from psycopg2.pool import PoolError, ThreadedConnectionPool
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timedelta
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

class _PlaywrightTimeoutFallbackError(Exception):
    pass


PlaywrightTimeoutError: type[Exception] = _PlaywrightTimeoutFallbackError

try:
    _playwright_sync = importlib.import_module('playwright.sync_api')
    sync_playwright = getattr(_playwright_sync, 'sync_playwright', None)
    _playwright_timeout_error = getattr(_playwright_sync, 'TimeoutError', None)
    if isinstance(_playwright_timeout_error, type) and issubclass(_playwright_timeout_error, Exception):
        PlaywrightTimeoutError = _playwright_timeout_error
except Exception:
    sync_playwright = None

try:
    cloudscraper = importlib.import_module('cloudscraper')
except Exception:
    cloudscraper = None

from model_capture_app.capture_blueprint import create_model_capture_blueprint

def _resource_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def _working_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _load_environment_file():
    checked = []
    for candidate in (
        _working_base_dir() / '.env',
        _resource_base_dir() / '.env',
        Path.cwd() / '.env',
    ):
        if candidate in checked:
            continue
        checked.append(candidate)
        if candidate.exists():
            load_dotenv(candidate)
            return candidate
    return None


BASE_DIR = _working_base_dir()
RESOURCE_BASE_DIR = _resource_base_dir()
ENV_PATH = _load_environment_file()

app = Flask(
    __name__,
    template_folder=str(RESOURCE_BASE_DIR / 'templates'),
    static_folder=str(RESOURCE_BASE_DIR / 'static'),
)

app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=30)

# --- CONFIGURATION ---
def _required_env(name):
    value = (os.environ.get(name) or '').strip()
    if not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


app.secret_key = _required_env('SECRET_KEY')
ADMIN_PASSWORD = _required_env('ADMIN_PASSWORD')
EXTENSION_API_KEY = (os.environ.get('EXTENSION_API_KEY') or '').strip()
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
_DESKTOP_CAPTURE_SIGNAL = {
    'id': 0,
    'model_url': '',
    'source': '',
    'triggered_at': '',
}
_DESKTOP_CAPTURE_SIGNAL_LOCK = threading.Lock()
_EXTENSION_AUTH_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def _validate_startup_environment():
    """
    Validate that all required environment variables are properly configured.
    Called at app startup to fail fast with clear diagnostics.
    """
    print('\n' + '=' * 60)
    print('STARTUP VALIDATION: Checking environment configuration')
    print('=' * 60)

    required = ['SECRET_KEY', 'ADMIN_PASSWORD', 'DATABASE_URL']
    missing = []

    for var_name in required:
        value = os.environ.get(var_name, '').strip()
        is_set = bool(value)
        status = 'SET' if is_set else 'MISSING'
        print(f'  {var_name}: {status}')
        if not is_set:
            missing.append(var_name)

    ext_key_set = bool(EXTENSION_API_KEY)
    print(f"  EXTENSION_API_KEY: {'optional, set' if ext_key_set else 'optional, not set (session auth only)'}")

    print(f'  Environment file: {ENV_PATH or "(none found)"}')
    print('=' * 60)

    if missing:
        raise RuntimeError(
            f'Failed to start: missing required environment variables: {", ".join(missing)}\n'
            'Set them in your environment or in a .env file and restart.'
        )


def _get_db_pool():
    global _DB_POOL
    if _DB_POOL is not None:
        return _DB_POOL

    with _DB_POOL_LOCK:
        if _DB_POOL is None:
            db_url = _required_env('DATABASE_URL')
            _DB_POOL = ThreadedConnectionPool(
                minconn=DB_POOL_MIN,
                maxconn=DB_POOL_MAX,
                dsn=db_url,
            )
    return _DB_POOL


def _reset_db_pool():
    global _DB_POOL
    with _DB_POOL_LOCK:
        pool = _DB_POOL
        _DB_POOL = None
    if pool is None:
        return
    try:
        pool.closeall()
    except Exception:
        pass


def _connection_is_usable(conn):
    if conn is None or getattr(conn, 'closed', 1):
        return False
    try:
        cur = conn.cursor()
        try:
            cur.execute('SELECT 1')
            cur.fetchone()
            return True
        finally:
            cur.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


def _get_pooled_connection():
    last_error = None
    for attempt in range(2):
        pool = _get_db_pool()
        conn = None
        try:
            conn = pool.getconn()
        except PoolError as exc:
            last_error = exc
            if attempt == 0:
                _reset_db_pool()
                continue
            raise RuntimeError(f'Unable to get DB connection from pool: {exc}') from exc

        if _connection_is_usable(conn):
            return conn

        try:
            pool.putconn(conn, close=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
        conn = None
        _reset_db_pool()

    if last_error is not None:
        raise RuntimeError(f'Unable to get DB connection from pool: {last_error}') from last_error
    raise RuntimeError('Unable to get a usable DB connection from pool')


def _put_pooled_connection(conn, discard=False):
    if conn is None:
        return
    pool = _DB_POOL
    if pool is None:
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        pool.putconn(conn, close=bool(discard))
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _execute(query, params=None, fetch=False):
    last_error = None
    for _ in range(2):
        conn = None
        discard_conn = False
        try:
            conn = _get_pooled_connection()
            cur = conn.cursor()
            try:
                cur.execute(str(query).replace('?', '%s'), params or ())
                rows = cur.fetchall() if fetch else None
                conn.commit()
                return rows
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

    if last_error is not None:
        raise last_error
    raise RuntimeError('Database execution failed without exception details')


def _init_db():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    schema_sql = """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
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
        CREATE TABLE IF NOT EXISTS print_profiles (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            price_modifier NUMERIC NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """

    last_error = None
    for _ in range(2):
        conn = None
        discard_conn = False
        try:
            conn = _get_pooled_connection()
            cur = conn.cursor()
            try:
                cur.execute(schema_sql)
                cur.execute("SELECT COUNT(*) FROM print_profiles")
                count = int(cur.fetchone()[0] or 0)
                if count == 0:
                    cur.execute(
                        """
                        INSERT INTO print_profiles (name, price_modifier, description, is_active, is_default)
                        VALUES (%s, %s, %s, TRUE, TRUE)
                        """,
                        ('Standard', 0, ''),
                    )
                conn.commit()
                _SCHEMA_READY = True
                return
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

    if last_error is not None:
        raise last_error


# --- OPTIMIZED QUERY FUNCTIONS (ROUTE-SPECIFIC) ---

def _get_settings():
    """Fetch only settings (lightweight)."""
    _init_db()
    settings = {"filaments": []}
    rows = _execute("SELECT value FROM settings WHERE key = %s", ("settings",), fetch=True)
    if rows:
        try:
            settings = json.loads(rows[0][0])
        except Exception:
            pass
    return settings

def _get_all_users(limit=None, offset=0):
    """Fetch users with optional pagination."""
    _init_db()
    query = "SELECT json FROM users ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = _execute(query, fetch=True) if not limit else _execute(query, fetch=True)
    users = []
    if rows:
        for r in rows:
            try:
                users.append(json.loads(r[0]))
            except Exception:
                pass
    return users

def _get_user_by_id(user_id):
    """Fetch a single user by ID."""
    _init_db()
    rows = _execute("SELECT json FROM users WHERE id = %s", (user_id,), fetch=True)
    if rows:
        try:
            return json.loads(rows[0][0])
        except Exception:
            pass
    return None

def _get_all_orders(limit=None, offset=0):
    """Fetch orders with optional pagination."""
    _init_db()
    query = "SELECT json FROM orders ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = _execute(query, fetch=True)
    orders = []
    if rows:
        for r in rows:
            try:
                orders.append(json.loads(r[0]))
            except Exception:
                pass
    return orders

def _get_user_orders(user_id, limit=None, offset=0):
    """Fetch orders for a specific user with pagination."""
    _init_db()
    query = "SELECT json FROM orders WHERE (json::jsonb ->> 'owner') = %s ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = _execute(query, (user_id,), fetch=True)
    orders = []
    if rows:
        for r in rows:
            try:
                orders.append(json.loads(r[0]))
            except Exception:
                pass
    return orders

def _get_orders_count(user_id=None):
    """Get count of orders, optionally filtered by user."""
    _init_db()
    if user_id:
        rows = _execute("SELECT COUNT(*) FROM orders WHERE (json::jsonb ->> 'owner') = %s", (user_id,), fetch=True)
    else:
        rows = _execute("SELECT COUNT(*) FROM orders", fetch=True)
    return rows[0][0] if rows else 0

def _get_all_featured_prints(limit=None, offset=0):
    """Fetch featured prints with optional pagination."""
    _init_db()
    query = "SELECT json FROM featured_prints ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = _execute(query, fetch=True)
    featured_prints = []
    if rows:
        for r in rows:
            try:
                featured_prints.append(json.loads(r[0]))
            except Exception:
                pass
    return featured_prints

def _get_featured_prints_count():
    """Get count of featured prints."""
    _init_db()
    rows = _execute("SELECT COUNT(*) FROM featured_prints", fetch=True)
    return rows[0][0] if rows else 0

def _load_all():
    """
    Load all tables (backward compatibility).
    DEPRECATED: Use specific query functions instead for better egress control.
    """
    _init_db()
    return {
        "settings": _get_settings(),
        "users": _get_all_users(),
        "orders": _get_all_orders(),
        "featured_prints": _get_all_featured_prints(),
    }


def get_db():
    """
    Return full database object (backward compatibility).
    WARNING: This loads all tables. Use specific query functions for high-traffic routes.
    """
    global _SCHEMA_READY
    last_error = None
    for _ in range(3):
        try:
            return _load_all()
        except Exception as exc:
            last_error = exc
            is_transient = isinstance(exc, (psycopg2.InterfaceError, psycopg2.OperationalError, PoolError))
            if not is_transient and 'Unable to get DB connection from pool' not in str(exc):
                raise
            _SCHEMA_READY = False
            _reset_db_pool()

    if last_error is not None:
        raise last_error
    raise RuntimeError('Failed to load database payload')


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _to_int(value, default=0, min_value=None, max_value=None):
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    if min_value is not None:
        parsed = max(int(min_value), parsed)
    if max_value is not None:
        parsed = min(int(max_value), parsed)
    return parsed


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _safe_next_path(raw_path, fallback_endpoint):
    fallback_path = url_for(fallback_endpoint)
    candidate = str(raw_path or '').strip()
    if not candidate:
        return fallback_path

    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback_path
    if not candidate.startswith('/'):
        return fallback_path
    return candidate


def _is_allowed_model_link(raw_link):
    link = str(raw_link or '').strip()
    if not link:
        return False
    parsed = urlparse(link)
    host = parsed.netloc.lower()
    if not host:
        parsed = urlparse('http://' + link)
        host = parsed.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    allowed = ('makerworld.com', 'printables.com')
    return any(host == a or host.endswith('.' + a) for a in allowed)


def _extension_api_authorized(payload=None):
    """
    Verify extension API key from either query parameter or Authorization header.
    If EXTENSION_API_KEY is unset, key-based auth is disabled and callers should use session auth.
    
    Args:
        payload: Request payload dict (can be query params or JSON body)
    
    Returns:
        True if the provided key matches EXTENSION_API_KEY, False otherwise.
    
    Security: Migrate from query params to Authorization header to avoid
    exposing keys in browser history and proxy logs.
    """
    if not EXTENSION_API_KEY:
        return False

    # Check Authorization header first (preferred method)
    auth_header = request.headers.get('Authorization', '').strip()
    if auth_header.startswith('Bearer '):
        provided = auth_header[7:]  # Remove "Bearer " prefix
        if provided and provided == EXTENSION_API_KEY:
            return True
    
    # Fall back to query parameter for backward compatibility
    provided = str((payload or {}).get('api_key') or '').strip()
    if provided:
        return provided == EXTENSION_API_KEY
    
    return False


def _extension_session_authorized():
    return bool(str(session.get('user_id') or '').strip())


def _extension_token_serializer():
    secret_key = app.secret_key
    if not secret_key:
        raise RuntimeError('SECRET_KEY is not configured')
    return URLSafeTimedSerializer(secret_key, salt='extension-local-auth-v1')


def _issue_extension_auth_token(user_id, username):
    payload = {
        'uid': str(user_id or '').strip(),
        'un': str(username or '').strip(),
        'iat': datetime.utcnow().isoformat() + 'Z',
    }
    return _extension_token_serializer().dumps(payload)


def _read_extension_auth_token(raw_token):
    token = str(raw_token or '').strip()
    if not token:
        return None
    try:
        data = _extension_token_serializer().loads(
            token,
            max_age=_EXTENSION_AUTH_TOKEN_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        return None
    user_id = str((data or {}).get('uid') or '').strip()
    username = str((data or {}).get('un') or '').strip()
    if not user_id:
        return None
    return {'user_id': user_id, 'username': username, 'token': token}


def _extract_extension_auth_token(payload=None):
    auth_header = str(request.headers.get('Authorization') or '').strip()
    if auth_header.startswith('Extension '):
        return auth_header[len('Extension '):].strip()

    token_header = str(request.headers.get('X-Extension-Auth') or '').strip()
    if token_header:
        return token_header

    token_query = str(request.args.get('ext_auth') or '').strip()
    if token_query:
        return token_query

    return str((payload or {}).get('ext_auth') or '').strip()


def _extension_request_user(payload=None):
    token_data = _read_extension_auth_token(_extract_extension_auth_token(payload))
    if token_data:
        token_data['source'] = 'token'
        return token_data

    session_user_id = str(session.get('user_id') or '').strip()
    if session_user_id:
        return {
            'user_id': session_user_id,
            'username': str(session.get('username') or '').strip(),
            'token': '',
            'source': 'session',
        }

    return None


def _sync_extension_session(auth_user):
    if not auth_user:
        return False
    user_id = str(auth_user.get('user_id') or '').strip()
    username = str(auth_user.get('username') or '').strip()
    if not user_id:
        return False
    changed = False
    if str(session.get('user_id') or '').strip() != user_id:
        session['user_id'] = user_id
        changed = True
    if username and str(session.get('username') or '').strip() != username:
        session['username'] = username
        changed = True
    return changed


@app.before_request
def _bind_extension_token_session():
    # Keep extension popup/overlay requests bound to the extension-authenticated user.
    # This makes website session state follow the active extension account automatically.
    token_user = _read_extension_auth_token(_extract_extension_auth_token())
    if token_user:
        _sync_extension_session(token_user)


# --- EGRESS DIAGNOSTICS & STORAGE HELPERS ---
_EGRESS_STATS = {
    'requests': {},  # {route: {'count': int, 'total_bytes': int, 'avg_bytes': int}}
}

def _track_egress(route_key, response_bytes):
    """Track response size for egress monitoring."""
    if route_key not in _EGRESS_STATS['requests']:
        _EGRESS_STATS['requests'][route_key] = {'count': 0, 'total_bytes': 0}
    stats = _EGRESS_STATS['requests'][route_key]
    stats['count'] += 1
    stats['total_bytes'] += response_bytes
    stats['avg_bytes'] = stats['total_bytes'] // max(1, stats['count'])

def _get_egress_stats():
    """Return current egress statistics (helpful for monitoring)."""
    return {
        'requests': _EGRESS_STATS['requests'],
        'total_tracked_requests': sum(s['count'] for s in _EGRESS_STATS['requests'].values()),
        'total_tracked_bytes': sum(s['total_bytes'] for s in _EGRESS_STATS['requests'].values()),
    }

@app.after_request
def _measure_egress(response):
    """Track response size for high-traffic routes."""
    try:
        # Only measure JSON/HTML responses (not static assets)
        content_type = response.headers.get('Content-Type', '').lower()
        if any(ct in content_type for ct in ['application/json', 'text/html']):
            route_key = request.endpoint or 'unknown'
            response_bytes = len(response.get_data()) if hasattr(response, 'get_data') else 0
            _track_egress(route_key, response_bytes)
    except Exception:
        pass
    return response

class SupabaseStorageHelper:
    """Helper for managing Supabase Storage uploads with optimized cache headers."""
    
    @staticmethod
    def get_cache_headers(is_immutable=True, max_age_seconds=31536000):
        """
        Return optimized cache headers for Supabase Smart CDN.
        
        Args:
            is_immutable: If True, adds immutable directive (for versioned assets)
            max_age_seconds: Cache duration in seconds (default: 1 year)
        
        Returns:
            dict with Cache-Control and other relevant headers
        """
        cache_control = f'public, max-age={max_age_seconds}'
        if is_immutable:
            cache_control += ', immutable'
        
        return {
            'Cache-Control': cache_control,
            'X-Content-Type-Options': 'nosniff',
        }
    
    @staticmethod
    def get_content_type(filename):
        """Guess content type from filename."""
        ext = (str(filename) or '').lower().split('.')[-1]
        types = {
            'zip': 'application/zip',
            'pdf': 'application/pdf',
            'webp': 'image/webp',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'svg': 'image/svg+xml',
            'stl': 'model/stl',
            '3mf': 'model/3mf',
        }
        return types.get(ext, 'application/octet-stream')


def _response_with_cache(response_data, cache_control_header='public, max-age=3600'):
    """
    Helper to attach cache headers to a Flask response.
    For use with json-like endpoints that can be cached.
    """
    from flask import make_response
    resp = make_response(response_data if isinstance(response_data, str) else jsonify(response_data))
    resp.headers['Cache-Control'] = cache_control_header
    return resp


def _extension_request_authorized(payload=None):
    desktop_client = request.headers.get('X-Desktop-Client') == '1'
    if desktop_client:
        return True
    if _extension_request_user(payload):
        return True
    return _extension_api_authorized(payload)


def _extract_first_hours(text):
    for pattern in (
        re.compile(r'(\d+(?:\.\d+)?)\s*h(?:ours?)?(?:\s*(\d+)\s*m(?:in(?:ute)?s?)?)?', re.IGNORECASE),
        re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)?', re.IGNORECASE),
    ):
        match = pattern.search(text or '')
        if not match:
            continue
        if len(match.groups()) >= 2 and match.group(2) is not None:
            hours = float(match.group(1) or 0)
            minutes = float(match.group(2) or 0)
            return round(hours + (minutes / 60.0), 2)
        value = float(match.group(1) or 0)
        if 'm' in match.group(0).lower() and 'h' not in match.group(0).lower():
            return round(value / 60.0, 2)
        return round(value, 2)
    return 0.0


def _extract_grams_candidates(text):
    candidates = []
    for match in re.finditer(r'(\d+(?:\.\d+)?)\s*g\b', text or '', re.IGNORECASE):
        try:
            value = float(match.group(1))
        except Exception:
            continue
        # Ignore absurd/obviously unrelated values.
        if 1.0 <= value <= 5000.0:
            candidates.append(value)
    return candidates


def _derive_weight_from_text_blocks(text_blocks):
    if not text_blocks:
        return None, None

    # 1) Prefer designer profile style blocks.
    designer_blocks = [
        block for block in text_blocks
        if re.search(r"designer(?:'s)?\s+profile|\bdesigner\b", block, re.IGNORECASE)
    ]
    for block in designer_blocks:
        grams = _extract_grams_candidates(block)
        if grams:
            return max(grams), 'designer_profile'

    # 2) General print profile blocks.
    profile_blocks = [
        block for block in text_blocks
        if re.search(r'print\s*profile|plate|\bh\b', block, re.IGNORECASE)
    ]
    for block in profile_blocks:
        grams = _extract_grams_candidates(block)
        if grams:
            return max(grams), 'print_profile'

    # 3) BOM / description fallback.
    bom_blocks = [
        block for block in text_blocks
        if re.search(r'bill\s+of\s+materials|\bbom\b|filament\s*used|grams', block, re.IGNORECASE)
    ]
    for block in bom_blocks:
        grams = _extract_grams_candidates(block)
        if grams:
            return max(grams), 'bom_or_description'

    return None, None


def _extract_title_from_html(html_text, fallback='MakerWorld Model'):
    match = re.search(r'<title>(.*?)</title>', html_text or '', re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    cleaned = re.sub(r'\s+', ' ', str(match.group(1) or '')).strip()
    cleaned = re.sub(r'\s*[-|]\s*MakerWorld\s*$', '', cleaned, flags=re.IGNORECASE)

    # Keep the main model title segment and drop profile/descriptive suffixes.
    segments = [s.strip() for s in re.split(r'\s*[|\-]\s*', cleaned) if s and s.strip()]
    if segments:
        cleaned = segments[0]

    cleaned = re.sub(
        r'\s*[\[(][^\])]*(?:layer\s*height|infill|infill\s*density|nozzle|line\s*width|wall\s*count|supports?)\b[^\])]*[\])]',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r'\s*(?:[,;/]|\s+-\s+)\s*(?:\d+(?:\.\d+)?\s*mm\b|\d{1,3}\s*%\s*infill\b|layer\s*height\b[^,;/\-]*)\s*$',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -|,;/')
    return cleaned or fallback


def _extract_balanced_json_chunk(text, start_index, open_char, close_char, max_scan=250000):
    if start_index < 0 or start_index >= len(text or ''):
        return ''
    depth = 0
    in_string = False
    escaped = False
    limit = min(len(text), start_index + max_scan)
    for index in range(start_index, limit):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start_index:index + 1]
    return ''


def _extract_makerworld_instances(html_text):
    instances = []
    for match in re.finditer(r'"instances"\s*:\s*\[', html_text or '', re.IGNORECASE):
        array_text = _extract_balanced_json_chunk(html_text, match.end() - 1, '[', ']')
        if not array_text:
            continue
        try:
            parsed = json.loads(array_text)
        except Exception:
            continue
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    instances.append(item)
    return instances


def _extract_model_image_urls(html_text, instances):
    urls = []
    allowed_model_keys = set()

    def _normalize_image_url(raw):
        u = str(raw or '').strip()
        if not re.match(r'^https?://', u, re.IGNORECASE):
            return ''
        # Keep a stable variant to avoid duplicate x-oss-process forms.
        return u.split('?')[0]

    def _add(url):
        u = str(url or '').strip()
        if not u:
            return
        if not re.match(r'^https?://', u, re.IGNORECASE):
            return
        stable = _normalize_image_url(u)
        if not stable:
            return
        if stable not in urls:
            urls.append(stable)

    def _register_model_key(url):
        u = str(url or '').strip()
        m = re.search(r'/makerworld/model/([^/]+)/', u, re.IGNORECASE)
        if m:
            allowed_model_keys.add(m.group(1))

    for inst in (instances or []):
        if not isinstance(inst, dict):
            continue
        _register_model_key(inst.get('cover'))

        pictures = inst.get('pictures')
        if isinstance(pictures, list):
            for pic in pictures:
                if isinstance(pic, dict):
                    pic_url = pic.get('url') or pic.get('imageUrl') or pic.get('cover')
                    _register_model_key(pic_url)
                else:
                    _register_model_key(pic)

        model2d = inst.get('model2DInfo')
        if isinstance(model2d, dict):
            model2d_url = model2d.get('cover') or model2d.get('imageUrl')
            _register_model_key(model2d_url)

    html = str(html_text or '')
    og_matches = re.findall(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    for m in og_matches:
        _add(m)
        _register_model_key(m)

    tw_matches = re.findall(
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    for m in tw_matches:
        _add(m)
        _register_model_key(m)

    # Gallery/design images shown in the main model page image strip.
    # Only include design images that belong to this model key, to avoid related-model images.
    design_matches = re.finditer(
        r'(https://makerworld\.bblmw\.com/makerworld/model/([^/]+)/design/[^"\'\s)]+\.(?:png|jpg|jpeg|webp)(?:\?[^"\'\s)]*)?)',
        html,
        flags=re.IGNORECASE,
    )
    for m in design_matches:
        full_url = m.group(1)
        model_key = m.group(2)
        if allowed_model_keys and model_key not in allowed_model_keys:
            continue
        _add(full_url)

    return urls[:16]


def _extract_instance_image_urls(instance):
    urls = []

    def _add(url):
        u = str(url or '').strip()
        if not u or not re.match(r'^https?://', u, re.IGNORECASE):
            return
        if u not in urls:
            urls.append(u)

    if not isinstance(instance, dict):
        return urls

    _add(instance.get('cover'))

    pictures = instance.get('pictures')
    if isinstance(pictures, list):
        for pic in pictures:
            if isinstance(pic, dict):
                _add(pic.get('url') or pic.get('imageUrl') or pic.get('cover'))
            else:
                _add(pic)

    return urls


def _makerworld_instance_colors(instance):
    """Extract per-color usage data from MakerWorld instance JSON.

    MakerWorld payloads vary by model/version, so this scans nested dict/list nodes
    and accepts multiple key aliases for color/name/used grams.
    """
    if not isinstance(instance, dict):
        return []

    bucket = []

    def _as_hex(v):
        h = str(v or '').strip()
        if not h:
            return ''
        if not h.startswith('#'):
            h = '#' + h
        if re.fullmatch(r'#[0-9a-fA-F]{6}', h):
            return h.lower()
        return ''

    def _as_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    def _extract_node(node):
        if isinstance(node, dict):
            color_hex = _as_hex(
                node.get('color')
                or node.get('colorHex')
                or node.get('hex')
                or node.get('color_code')
            )
            name = str(
                node.get('colorName')
                or node.get('filamentName')
                or node.get('name')
                or node.get('label')
                or ''
            ).strip()
            used_g = _as_float(
                node.get('usedG')
                or node.get('used_g')
                or node.get('materialWeight')
                or node.get('weight_g')
                or 0
            )

            if color_hex or name:
                bucket.append({
                    'name': name,
                    'hex': color_hex or '#888888',
                    'used_g': round(max(0.0, used_g), 2),
                })

            for v in node.values():
                if isinstance(v, (dict, list)):
                    _extract_node(v)

        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    _extract_node(item)

    # Prefer explicit filament arrays first (most accurate, avoids duplicate nested hits).
    for key in ('instanceFilaments', 'filaments'):
        filaments = instance.get(key)
        if isinstance(filaments, list) and filaments:
            _extract_node(filaments)
            break

    # Fallback only if explicit arrays were not available.
    if not bucket:
        _extract_node(instance)

    # Aggregate duplicates by (name, hex) and keep meaningful rows.
    merged = {}
    for row in bucket:
        key = (str(row.get('name') or '').strip().lower(), str(row.get('hex') or '#888888').lower())
        current = merged.get(key)
        if current is None:
            merged[key] = {
                'name': str(row.get('name') or '').strip(),
                'hex': str(row.get('hex') or '#888888').strip() or '#888888',
                'used_g': round(float(row.get('used_g') or 0.0), 2),
            }
        else:
            current['used_g'] = round(float(current.get('used_g') or 0.0) + float(row.get('used_g') or 0.0), 2)

    cleaned = [
        r for r in merged.values()
        if (r.get('name') or r.get('hex')) and float(r.get('used_g') or 0.0) >= 0
    ]
    cleaned.sort(key=lambda x: float(x.get('used_g') or 0.0), reverse=True)
    return cleaned


def _makerworld_instance_weight(instance):
    filaments = instance.get('filaments') if isinstance(instance, dict) else None
    if isinstance(filaments, list):
        total = 0.0
        found = False
        for filament in filaments:
            if not isinstance(filament, dict):
                continue
            used_g = filament.get('usedG')
            if used_g is None:
                continue
            try:
                value = float(used_g)
            except Exception:
                continue
            if value >= 0:
                total += value
                found = True
        if found and 1.0 <= total <= 5000.0:
            return round(total, 2)

    try:
        fallback_weight = float(instance.get('weight') or 0)
    except Exception:
        fallback_weight = 0.0
    if 1.0 <= fallback_weight <= 5000.0:
        return round(fallback_weight, 2)
    return None


def _makerworld_instance_hours(instance):
    try:
        prediction = float(instance.get('prediction') or 0)
    except Exception:
        prediction = 0.0

    if prediction <= 0:
        return None

    if prediction >= 600:
        return round(prediction / 3600.0, 2)
    if prediction >= 10:
        return round(prediction / 60.0, 2)
    return round(prediction, 2)


def _pick_makerworld_instance(instances, target_profile_id=None):
    if not instances:
        return None

    normalized_target = str(target_profile_id or '').strip()
    if normalized_target:
        for instance in instances:
            if str(instance.get('id') or '').strip() == normalized_target:
                return instance
        for instance in instances:
            if str(instance.get('profileId') or '').strip() == normalized_target:
                return instance

    for instance in instances:
        if instance.get('authorsChoice'):
            return instance

    ordered = []
    for instance in instances:
        if _makerworld_instance_weight(instance) is not None:
            ordered.append(instance)
    if ordered:
        return ordered[0]
    return instances[0]


def _makerworld_instance_name(instance, index):
    if not isinstance(instance, dict):
        return f'Profile {index}'

    for key in ('name', 'profileName', 'title', 'displayName'):
        value = str(instance.get(key) or '').strip()
        if value:
            return value

    return f'Profile {index}'


def _resolve_pricing_inputs(overrides=None):
    cfg = _load_control_center_settings()
    source = {
        'base_fee': float(cfg.get('base_service_fee') or 0.0),
        'price_per_gram': float(cfg.get('price_per_gram') or 0.0),
        'power_cost_per_hour': float(cfg.get('power_cost_per_hour') or 0.0),
        'profit_margin': float(cfg.get('profit_margin') or 1.2),
    }
    source['profit_margin'] = source['profit_margin'] if source['profit_margin'] > 0 else 1.2

    incoming = overrides or {}

    def _coerce_float(value, fallback):
        try:
            if value is None or str(value).strip() == '':
                return float(fallback)
            return float(value)
        except Exception:
            return float(fallback)

    source['base_fee'] = max(0.0, _coerce_float(incoming.get('base_fee'), source['base_fee']))
    source['price_per_gram'] = max(0.0, _coerce_float(incoming.get('price_per_gram'), source['price_per_gram']))
    source['power_cost_per_hour'] = max(0.0, _coerce_float(incoming.get('power_cost_per_hour'), source['power_cost_per_hour']))
    source['profit_margin'] = _coerce_float(incoming.get('profit_margin'), source['profit_margin'])
    if source['profit_margin'] <= 0:
        source['profit_margin'] = 1.0
    return source


def _calc_profile_price(weight_g, hours, pricing_inputs):
    def _round_price_to_nearest_5000(amount):
        value = max(0.0, float(amount or 0.0))
        return float(int((value + 2500.0) // 5000.0) * 5000)

    base_fee = float(pricing_inputs.get('base_fee') or 0.0)
    ppg = float(pricing_inputs.get('price_per_gram') or 0.0)
    power = float(pricing_inputs.get('power_cost_per_hour') or 0.0)
    margin = float(pricing_inputs.get('profit_margin') or 1.0)
    subtotal = base_fee + (max(0.0, float(weight_g or 0.0)) * ppg) + (max(0.0, float(hours or 0.0)) * power)
    return _round_price_to_nearest_5000(subtotal * (margin if margin > 0 else 1.0))


def _extract_model_profile_metrics(model_url, pricing_overrides=None, calc_result=None):
    pricing_inputs = _resolve_pricing_inputs(pricing_overrides)
    if calc_result is not None:
        html_text = str(calc_result.get('html') or '')
        title = str(calc_result.get('title') or 'MakerWorld Model')
        if not html_text:
            fallback_weight = float(calc_result.get('weight') or 50.0)
            fallback_hours = float(calc_result.get('hours') or 0.0)
            return {
                'title': title,
                'profiles': [{
                    'id': '',
                    'name': '',
                    'weight_g': round(fallback_weight, 2),
                    'estimated_print_hours': round(fallback_hours, 2),
                    'price': _calc_profile_price(fallback_weight, fallback_hours, pricing_inputs),
                    'is_default': True,
                    'weight_needs_review': bool(calc_result.get('needs_review')),
                    'source': str(calc_result.get('source') or 'fallback_default'),
                    'colors': [],
                    'image_urls': [],
                }],
            }
    else:
        try:
            quick = _quick_parse_model_page(model_url)
            html_text = str(quick.get('html') or '')
            title = str(quick.get('title') or 'MakerWorld Model')
        except Exception:
            calc_result = _calculate_model_metrics(model_url)
            fallback_weight = float(calc_result.get('weight') or 50.0)
            fallback_hours = float(calc_result.get('hours') or 0.0)
            return {
                'title': str(calc_result.get('title') or 'MakerWorld Model'),
                'profiles': [{
                    'id': '',
                    'name': '',
                    'weight_g': round(fallback_weight, 2),
                    'estimated_print_hours': round(fallback_hours, 2),
                    'price': _calc_profile_price(fallback_weight, fallback_hours, pricing_inputs),
                    'is_default': True,
                    'weight_needs_review': bool(calc_result.get('needs_review')),
                    'source': str(calc_result.get('source') or 'fallback_default'),
                    'colors': [],
                    'image_urls': [],
                }],
            }

    profile_id_m = re.search(r'profileId[-_](\d+)', model_url, re.IGNORECASE)
    target_profile_id = profile_id_m.group(1) if profile_id_m else ''

    instances = _extract_makerworld_instances(html_text)
    profiles = []
    seen = set()
    for index, instance in enumerate(instances, start=1):
        profile_id = str(instance.get('profileId') or instance.get('id') or '').strip()
        name = _makerworld_instance_name(instance, index)

        weight_g = _makerworld_instance_weight(instance)
        hours = _makerworld_instance_hours(instance)

        needs_review = False
        if weight_g is None or float(weight_g) <= 0:
            weight_g = 50.0
            needs_review = True
        if hours is None or float(hours) < 0:
            hours = 0.0

        price_value = _calc_profile_price(weight_g, hours, pricing_inputs)
        is_default_match = bool(
            (target_profile_id and target_profile_id in {str(instance.get('id') or ''), str(instance.get('profileId') or '')})
            or instance.get('authorsChoice')
        )

        dedupe_key = (profile_id or name.lower(), round(float(weight_g), 2), round(float(hours), 2))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        profiles.append({
            'id': profile_id,
            'name': name,
            'weight_g': round(float(weight_g), 2),
            'estimated_print_hours': round(float(hours), 2),
            'price': price_value,
            'is_default': is_default_match,
            'weight_needs_review': needs_review,
            'source': 'instance_json',
            'colors': _makerworld_instance_colors(instance),
            'image_urls': _extract_instance_image_urls(instance),
        })

    if not profiles:
        if calc_result is None:
            calc_result = _calculate_model_metrics(model_url)
        fallback_weight = float(calc_result.get('weight') or 50.0)
        fallback_hours = float(calc_result.get('hours') or 0.0)
        return {
            'title': title,
            'profiles': [{
                'id': '',
                'name': '',
                'weight_g': round(fallback_weight, 2),
                'estimated_print_hours': round(fallback_hours, 2),
                'price': _calc_profile_price(fallback_weight, fallback_hours, pricing_inputs),
                'is_default': True,
                'weight_needs_review': bool(calc_result.get('needs_review')),
                'source': str((calc_result or {}).get('source') or 'fallback_default'),
                'colors': [],
                'image_urls': [],
            }],
        }

    if not any(bool(p.get('is_default')) for p in profiles):
        profiles[0]['is_default'] = True

    return {
        'title': title,
        'profiles': profiles,
    }


def _quick_parse_model_page(model_url):
    # Fast parser: cloudscraper/requests + MakerWorld embedded JSON.
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        )
    }

    if cloudscraper is not None:
        client = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
        response = client.get(model_url, headers=headers, timeout=30)
    else:
        response = requests.get(model_url, headers=headers, timeout=30)

    if int(response.status_code or 0) >= 400:
        raise RuntimeError(f'HTTP {response.status_code}')

    html_text = str(response.text or '')
    title = _extract_title_from_html(html_text)

    # Extract the profileId from the URL hash (e.g. #profileId-2672331).
    # MakerWorld uses this for either an instance id or a profile id depending on page state.
    profile_id_m = re.search(r'profileId[-_](\d+)', model_url, re.IGNORECASE)
    target_profile_id = profile_id_m.group(1) if profile_id_m else None

    weight = None
    hours = None
    source = None

    instances = _extract_makerworld_instances(html_text)
    chosen_instance = _pick_makerworld_instance(instances, target_profile_id)
    if chosen_instance is not None:
        weight = _makerworld_instance_weight(chosen_instance)
        hours = _makerworld_instance_hours(chosen_instance)
        if weight is not None:
            source = 'instance_json'

    # Last-resort classic patterns (avoids the erroneous top-level "weight" key)
    if weight is None:
        for pattern in (
            re.compile(r'"weight_g"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
            re.compile(r'"filamentWeight"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
            re.compile(r'(\d+(?:\.\d+)?)\s*g\b', re.IGNORECASE),
        ):
            m = pattern.search(html_text)
            if not m:
                continue
            try:
                parsed = float(m.group(1))
            except Exception:
                continue
            if 1.0 <= parsed <= 5000.0:
                weight = parsed
                break

    if hours is None:
        for pattern in (
            re.compile(r'"(?:estimatedPrintTime|printTime|print_time|costTime)"\s*:\s*"?(\d+(?:\.\d+)?)"?', re.IGNORECASE),
            re.compile(r'"prediction"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
        ):
            match = pattern.search(html_text)
            if not match:
                continue
            try:
                raw_value = float(match.group(1))
            except Exception:
                continue
            if raw_value <= 0:
                continue
            if raw_value >= 600:
                hours = round(raw_value / 3600.0, 2)
            elif raw_value >= 10:
                hours = round(raw_value / 60.0, 2)
            else:
                hours = round(raw_value, 2)
            source = source or 'instance_prediction'
            break

    return {'html': html_text, 'title': title, 'weight': weight, 'hours': hours, 'source': source}


def _calculate_model_metrics(model_url, manual_weight=None, manual_hours=None):
    control_settings = _load_control_center_settings()
    price_per_kg = float(control_settings.get('price_per_gram') or 0.0) * 1000.0
    markup = float(control_settings.get('profit_margin') or 1.0)
    if markup <= 0:
        markup = 1.0

    result = {
        'success': False,
        'title': 'MakerWorld Model',
        'html': '',
        'weight': 0.0,
        'hours': 0.0,
        'raw_price': 0.0,
        'formatted_price': 'Rp0',
        'error': '',
        'source': 'unknown',
        'needs_review': False,
    }

    # 1) Prefer manual weight if provided.
    try:
        if manual_weight is not None:
            parsed_manual = float(manual_weight)
            if parsed_manual > 0:
                result['weight'] = round(parsed_manual, 2)
                result['source'] = 'manual'
    except Exception:
        pass

    html_text = ''
    if result['weight'] <= 0 and model_url:
        try:
            quick = _quick_parse_model_page(model_url)
            html_text = quick.get('html') or ''
            result['html'] = html_text
            result['title'] = quick.get('title') or result['title']
            parsed_weight = quick.get('weight')
            if parsed_weight is not None and float(parsed_weight) > 0:
                result['weight'] = round(float(parsed_weight), 2)
                result['source'] = str(quick.get('source') or 'regex_html')
            parsed_hours = quick.get('hours')
            if parsed_hours is not None and float(parsed_hours) > 0:
                result['hours'] = round(float(parsed_hours), 2)
        except Exception as exc:
            result['error'] = f'Quick parse failed: {exc}'

    # 2) If still missing, try robust Playwright parser.
    if result['weight'] <= 0 and model_url:
        pw_metrics = _scrape_makerworld_metrics(model_url)
        pw_weight = float(pw_metrics.get('weight_g') or 0.0)
        if pw_weight > 0:
            result['weight'] = round(pw_weight, 2)
            result['source'] = str(pw_metrics.get('weight_source') or 'playwright')
            result['needs_review'] = bool(pw_metrics.get('weight_needs_review'))
        if float(pw_metrics.get('estimated_print_hours') or 0.0) > 0:
            result['hours'] = round(float(pw_metrics.get('estimated_print_hours') or 0.0), 2)
        if not result['error'] and pw_metrics.get('scrape_error'):
            result['error'] = str(pw_metrics.get('scrape_error'))

    # 3) Optional manual hours override.
    try:
        if manual_hours is not None:
            h = float(manual_hours)
            if h >= 0:
                result['hours'] = round(h, 2)
    except Exception:
        pass

    # 4) Final fallback default per requested behavior.
    if result['weight'] <= 0:
        result['weight'] = 50.0
        result['source'] = 'fallback_default'
        result['needs_review'] = True
        if not result['error']:
            result['error'] = 'Weight not found in profile/BOM/description; defaulted to 50g.'

    # Price formula: cost = (weight_in_grams / 1000) * price_per_kg * markup
    base_cost = (float(result['weight']) / 1000.0) * price_per_kg
    total = int((max(0.0, base_cost * markup) + 2500.0) // 5000.0) * 5000
    result['raw_price'] = float(total)
    result['formatted_price'] = f"Rp{int(total):,}".replace(',', '.')
    result['success'] = result['weight'] > 0

    # If we got page HTML but no hours yet, do lightweight hours parse.
    if result['hours'] <= 0 and html_text:
        result['hours'] = _extract_first_hours(html_text)

    return result


def _scrape_makerworld_metrics(model_url):
    # Required fallback behavior when we cannot scrape robustly.
    fallback = {
        'weight_g': 50.0,
        'estimated_print_hours': 0.0,
        'weight_source': 'fallback_default',
        'weight_needs_review': True,
        'scrape_error': '',
    }

    if sync_playwright is None:
        fallback['scrape_error'] = 'playwright_not_installed'
        return fallback

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled'],
            )
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1440, 'height': 900},
                locale='en-US',
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page.goto(model_url, wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_selector('text=/Print\\s*Profile/i', timeout=15000)
            except PlaywrightTimeoutError:
                # Continue: some pages lazy-render or localize text differently.
                pass

            page.wait_for_timeout(1500)
            page_text = page.locator('body').inner_text(timeout=10000)
            block_texts = page.evaluate(
                                r"""
                () => {
                  const nodes = Array.from(document.querySelectorAll('section, article, div, li'));
                  return nodes
                    .map(n => (n && n.innerText ? n.innerText.trim() : ''))
                    .filter(t => t.length >= 8 && t.length <= 360)
                    .filter(t => /\d+(?:\.\d+)?\s*g\b/i.test(t) || /print\s*profile|designer|bill\s+of\s+materials|\bbom\b|filament\s*used/i.test(t));
                }
                """
            )
            context.close()
            browser.close()
    except Exception as exc:
        fallback['scrape_error'] = str(exc)
        return fallback

    text_blocks = []
    if isinstance(block_texts, list):
        text_blocks.extend([str(t or '') for t in block_texts if str(t or '').strip()])
    if page_text:
        text_blocks.extend([line.strip() for line in str(page_text).splitlines() if line.strip()])

    weight_g, source = _derive_weight_from_text_blocks(text_blocks)
    hours = _extract_first_hours('\n'.join(text_blocks))

    if weight_g is None:
        fallback['estimated_print_hours'] = hours
        fallback['weight_source'] = 'fallback_default'
        fallback['weight_needs_review'] = True
        return fallback

    return {
        'weight_g': round(float(weight_g), 2),
        'estimated_print_hours': round(float(hours), 2),
        'weight_source': source or 'print_profile',
        'weight_needs_review': False,
        'scrape_error': '',
    }


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

    parsed = []
    for candidate in candidates:
        if not candidate:
            continue
        parsed_value = _parse_iso_utc(candidate)
        if parsed_value is not None:
            parsed.append(parsed_value)
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


def _execute_in_transaction(queries_and_params):
    """Run multiple (query, params) pairs atomically in a single transaction."""
    last_error = None
    for _ in range(2):
        conn = None
        discard_conn = False
        try:
            conn = _get_pooled_connection()
            cur = conn.cursor()
            try:
                for query, params in queries_and_params:
                    cur.execute(query.replace('?', '%s'), params)
                conn.commit()
                return
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
    if last_error is None:
        raise RuntimeError('Transactional database operation failed without an exception')
    raise last_error


def save_db(data, full_replace=False, raise_on_error=False):
    _init_db()
    errors = []

    def _record_save_error(section, exc):
        message = f"Failed to save {section}: {exc}"
        try:
            app.logger.exception(message)
        except Exception:
            print(message)
        errors.append((section, exc))

    # Settings
    try:
        _execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("settings", json.dumps(data.get("settings", {"filaments": []})))
        )
    except Exception as e:
        _record_save_error("settings", e)

    # Users — DELETE + all INSERTs run in one atomic transaction so a
    # mid-loop failure cannot leave the table empty.
    try:
        user_queries: list[tuple[str, tuple[Any, ...]]] = [("DELETE FROM users", ())]
        for user in data.get("users", []):
            user_queries.append((
                "INSERT INTO users (id, json) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET json = EXCLUDED.json",
                (user.get('id'), json.dumps(user))
            ))
        _execute_in_transaction(user_queries)
    except Exception as e:
        _record_save_error("users", e)

    # Orders
    try:
        if full_replace:
            _execute("DELETE FROM orders")
    except Exception as e:
        _record_save_error("orders", e)

    for order in data.get("orders", []):
        try:
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
            _record_save_error("orders", e)

    # Featured prints
    try:
        _execute("DELETE FROM featured_prints")
        for item in data.get("featured_prints", []):
            _execute(
                "INSERT INTO featured_prints (id, json) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET json = EXCLUDED.json",
                (item.get('id'), json.dumps(item))
            )
    except Exception as e:
        _record_save_error("featured prints", e)

    if errors and raise_on_error:
        failed_sections = ', '.join(section for section, _ in errors)
        raise RuntimeError(f"Database persistence failed for: {failed_sections}") from errors[0][1]

    return errors


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


def _normalize_profile_pricing(profile_pricing_raw):
    rows = profile_pricing_raw
    if isinstance(profile_pricing_raw, str):
        text = profile_pricing_raw.strip()
        if not text:
            rows = []
        else:
            try:
                rows = json.loads(text)
            except Exception:
                rows = []

    if not isinstance(rows, list):
        return []

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        name = str(row.get('name') or '').strip()
        if not name:
            continue

        try:
            profile_price = float(row.get('price') or row.get('price_modifier') or 0)
        except (TypeError, ValueError):
            profile_price = 0.0

        try:
            weight_g = float(row.get('weight_g') or 0)
        except (TypeError, ValueError):
            weight_g = 0.0

        try:
            estimated_print_hours = float(row.get('estimated_print_hours') or 0)
        except (TypeError, ValueError):
            estimated_print_hours = 0.0

        raw_colors = row.get('colors')
        colors = []
        if isinstance(raw_colors, list):
            for c in raw_colors:
                if not isinstance(c, dict):
                    continue
                try:
                    used_g = float(c.get('used_g') or 0)
                except (TypeError, ValueError):
                    used_g = 0.0
                colors.append({
                    'name': str(c.get('name') or '').strip(),
                    'hex': str(c.get('hex') or '#888888').strip(),
                    'used_g': round(max(0.0, used_g), 2),
                })

        normalized.append({
            'id': str(row.get('id') or '').strip(),
            'name': name,
            'price': profile_price,
            'is_default': bool(row.get('is_default')),
            'weight_g': max(0.0, weight_g),
            'estimated_print_hours': max(0.0, estimated_print_hours),
            'colors': colors,
        })

    if normalized and (not any(p.get('is_default') for p in normalized)):
        normalized[0]['is_default'] = True

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

    total_g_raw = raw.get('total_g')
    try:
        total_g = int(total_g_raw) if total_g_raw is not None else 1000
    except Exception:
        total_g = 1000
    remaining_g_raw = raw.get('remaining_g')
    try:
        remaining_g = int(remaining_g_raw) if remaining_g_raw is not None else total_g
    except Exception:
        remaining_g = total_g
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


def _featured_target_matches_user(target, user_id, username=''):
    candidate = str(target or '').strip()
    if not candidate:
        return False

    if candidate == 'ALL':
        return True

    normalized_user_id = str(user_id or '').strip()
    normalized_username = str(username or '').strip().lower()
    candidate_lower = candidate.lower()

    if normalized_user_id and candidate == normalized_user_id:
        return True

    if normalized_username and candidate_lower == normalized_username:
        return True

    return False


def _featured_item_visible_to_user(item, user_id, username=''):
    targets = item.get('target_users')
    if not targets:
        legacy = item.get('target_user')
        targets = [legacy] if legacy else []

    targets = _normalize_target_users(targets, fallback='ALL')
    return any(_featured_target_matches_user(target, user_id, username) for target in targets)


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


def _is_cart_visible_order(order):
    if not isinstance(order, dict):
        return False
    if order.get('deleted_at'):
        return False
    if order.get('cart_checkout_archived_at'):
        return False
    return str(order.get('status') or '').strip().lower() in {'in cart', 'quoted', 'pending quote'}

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


def _build_user_portal_context(user_id, search_query='', history_page=1, history_page_size=16, featured_page=1, featured_page_size=6):
    # Optimized: Use specific query functions instead of loading entire database
    settings = _get_settings()
    user = _get_user_by_id(user_id)
    
    # Purge soft-deleted orders for this user before building context
    all_orders = _get_all_orders()
    purged_ids = _purge_expired_soft_deletes({'orders': all_orders, 'settings': settings})
    if purged_ids:
        db = {'orders': all_orders, 'settings': settings}
        save_db(db)
    
    filaments, filaments_changed = _normalize_filaments(settings)
    if filaments_changed:
        save_db({'settings': settings})
    
    control_settings = _load_control_center_settings()
    completed_statuses = {'completed', 'done', 'delivered'}
    inactive_statuses = completed_statuses | {'cancelled', 'declined', 'price denied', 'in cart', 'quoted'}

    # Get user's orders from the full list (this is still needed for soft-delete logic)
    owned_orders = [o for o in all_orders if o.get('owner') == user_id]
    owned_orders = sorted(
        owned_orders,
        key=lambda o: _order_last_modified(o) or datetime.min,
        reverse=True,
    )
    owned_orders = _decorate_orders_with_pending_delete_date(owned_orders)

    cart_orders = [o for o in owned_orders if _is_cart_visible_order(o)]
    user_orders = [
        o for o in owned_orders
        if not _is_cart_visible_order(o)
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

    history_page_size = _to_int(history_page_size, default=16, min_value=6, max_value=40)
    history_total = len(filtered_orders)
    history_total_pages = max(1, int(math.ceil(history_total / float(history_page_size))))
    history_page = _to_int(history_page, default=1, min_value=1, max_value=history_total_pages)
    history_start = (history_page - 1) * history_page_size
    filtered_orders_page = filtered_orders[history_start:history_start + history_page_size]

    all_featured = [
        f for f in _get_all_featured_prints()
        if _featured_item_visible_to_user(f, user_id, str((user or {}).get('username') or '').strip())
    ]
    # Personal suggestions = items targeted specifically at THIS user (not ALL-user items)
    personal_suggestions = [
        f for f in all_featured
        if 'ALL' not in _normalize_target_users(
            f.get('target_users') or ([f.get('target_user')] if f.get('target_user') else []),
            fallback='ALL'
        )
    ]
    # General featured = ALL-targeted items only
    all_targeted_items = [
        f for f in all_featured
        if 'ALL' in _normalize_target_users(
            f.get('target_users') or ([f.get('target_user')] if f.get('target_user') else []),
            fallback='ALL'
        )
    ]
    # Browse catalog = all items visible to the user (ALL-targeted + personally targeted)
    browse_items = list(all_featured)

    # Home hero slideshow only includes suggestions explicitly opted in.
    slideshow_items = [
        f for f in all_featured
        if _to_bool(f.get('show_in_slideshow'), default=False)
    ]

    hero_items = []
    seen_hero_keys = set()
    for item in slideshow_items:
        if not isinstance(item, dict):
            continue
        key = str(item.get('id') or item.get('makerworld_url') or item.get('title') or '').strip()
        if not key or key in seen_hero_keys:
            continue
        seen_hero_keys.add(key)
        hero_items.append(item)

    featured_items = list(all_featured)

    if not featured_items:
        featured_items = _default_featured_items()

    featured_page_size = _to_int(featured_page_size, default=6, min_value=3, max_value=12)
    featured_total = len(featured_items)
    featured_total_pages = max(1, int(math.ceil(featured_total / float(featured_page_size))))
    featured_page = _to_int(featured_page, default=1, min_value=1, max_value=featured_total_pages)
    featured_start = (featured_page - 1) * featured_page_size
    featured_items_page = featured_items[featured_start:featured_start + featured_page_size]

    browse_total = len(browse_items)

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

    cart_clear_ids = session.get('cart_clear_ids', [])
    if not isinstance(cart_clear_ids, list):
        cart_clear_ids = []
    cart_clear_ids = [str(i).strip() for i in cart_clear_ids if str(i).strip()]

    return {
        'user': user,
        'filaments': filaments,
        'featured_items': featured_items_page,
        'featured_items_total': featured_total,
        'featured_page': featured_page,
        'featured_total_pages': featured_total_pages,
        'slideshow_items': hero_items,
        'browse_items': browse_items,
        'browse_items_total': browse_total,
        'personal_suggestions': personal_suggestions,
        'recent_orders': user_orders[:3],
        'all_orders': user_orders,
        'filtered_orders': filtered_orders_page,
        'history_orders_total': history_total,
        'history_page': history_page,
        'history_total_pages': history_total_pages,
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
    featured_page = _to_int(request.args.get('featured_page'), default=1, min_value=1)
    context = _build_user_portal_context(
        session.get('user_id'),
        featured_page=featured_page,
    )
    return render_template('user_home.html', active_tab='home', **context)


@app.route('/desktop-capture')
@app.route('/desktop-capture/')
@app.route('/desktop_capture')
def desktop_capture_page():
    return render_template('desktop_capture.html')


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


@app.route('/cart/embed')
def user_cart_embed():
    auth_user = _extension_request_user()
    if not auth_user:
        return redirect(url_for('user_login'))
    _sync_extension_session(auth_user)
    user_id = str(auth_user.get('user_id') or '').strip()
    if not user_id:
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(user_id)
    return render_template(
        'user_cart_embed.html',
        active_tab='cart',
        extension_auth_token=str(auth_user.get('token') or ''),
        **context,
    )


@app.route('/history')
def user_history():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    q = (request.args.get('q') or '').strip()
    history_page = _to_int(request.args.get('page'), default=1, min_value=1)
    context = _build_user_portal_context(
        session.get('user_id'),
        search_query=q,
        history_page=history_page,
    )
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


@app.route('/extension-install')
def extension_install():
    if not session.get('user_id'):
        return redirect(url_for('user_login'))
    context = _build_user_portal_context(session.get('user_id'))
    shot_specs = {
        'chrome_extensions': 'chrome_extensions',
        'load_unpacked': 'load_unpacked',
        'select_folder': 'select_folder',
        'popup_login': 'popup_login',
    }
    extensions = ('.png', '.jpg', '.jpeg', '.webp')
    shots = {}
    for key, stem in shot_specs.items():
        detected = None
        for ext in extensions:
            candidate = RESOURCE_BASE_DIR / 'static' / 'extension_setup' / f'{stem}{ext}'
            if candidate.exists():
                detected = f'/static/extension_setup/{stem}{ext}'
                break
        shots[key] = detected
    return render_template('extension_install.html', active_tab='help', extension_setup_shots=shots, **context)


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
    next_path = _safe_next_path(request.values.get('next'), 'index')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        user = next((u for u in db.get('users', []) if u.get('username') == username), None)
        if user and check_password_hash(user.get('password_hash', ''), password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(next_path)
        return render_template('user_login.html', error='Invalid credentials', next_path=next_path)
    return render_template('user_login.html', next_path=next_path)


@app.route('/user_logout')
def user_logout():
    session.pop('user_id', None)
    session.pop('username', None)
    return redirect(url_for('user_login'))


@app.route('/extension-api/user-auth-status', methods=['GET'])
def extension_user_auth_status():
    auth_user = _extension_request_user()
    _sync_extension_session(auth_user)
    user_id = str((auth_user or {}).get('user_id') or '').strip()
    username = str((auth_user or {}).get('username') or '').strip()
    token = _issue_extension_auth_token(user_id, username) if user_id else ''
    return jsonify({
        'ok': True,
        'logged_in': bool(user_id),
        'user_id': user_id,
        'username': username,
        'extension_auth_token': token,
    })


@app.route('/extension-api/user-login', methods=['POST'])
def extension_user_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get('username') or '').strip()
    password = str(payload.get('password') or '')
    if not username or not password:
        return jsonify({'ok': False, 'error': 'Username and password are required.'}), 400

    db = get_db()
    user = next((u for u in db.get('users', []) if str(u.get('username') or '').strip() == username), None)
    if not user or not check_password_hash(user.get('password_hash', ''), password):
        return jsonify({'ok': False, 'error': 'Invalid credentials.'}), 401

    session['user_id'] = user.get('id')
    session['username'] = user.get('username')
    token = _issue_extension_auth_token(user.get('id'), user.get('username'))
    return jsonify({
        'ok': True,
        'user_id': str(user.get('id') or ''),
        'username': str(user.get('username') or ''),
        'extension_auth_token': token,
    })


@app.route('/extension-api/user-logout', methods=['POST'])
def extension_user_logout():
    session.pop('user_id', None)
    session.pop('username', None)
    return jsonify({'ok': True})


def _build_extension_hover_order_item(model_url):
    calc_result = _calculate_model_metrics(model_url)
    profile_metrics = _extract_model_profile_metrics(model_url, calc_result=calc_result)
    profiles = profile_metrics.get('profiles') or []
    title = str(profile_metrics.get('title') or calc_result.get('title') or 'MakerWorld Model').strip() or 'MakerWorld Model'

    default_profile = next((p for p in profiles if p.get('is_default')), profiles[0] if profiles else {})

    db = get_db()
    settings = db.get('settings', {})
    filaments, _ = _normalize_filaments(settings)
    filament_catalog = []
    for row in filaments:
        if not isinstance(row, dict):
            continue
        name = str(row.get('name') or '').strip()
        if not name:
            continue
        filament_catalog.append({
            'name': name,
            'hex': str(row.get('hex') or row.get('color_hex') or '#8b8b8b').strip() or '#8b8b8b',
            'remaining_g': float(_to_float(row.get('remaining_g', row.get('total_g', 0)), 0)),
            'out_of_stock': bool(row.get('out_of_stock')),
        })

    html_text = str(calc_result.get('html') or '')
    instances = _extract_makerworld_instances(html_text)
    global_images = _extract_model_image_urls(html_text, instances)

    normalized_profiles = []
    profile_customizations = []
    default_parts = []
    default_suggested = []
    for idx, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            continue
        profile_name = str(profile.get('name') or f'Profile {idx + 1}').strip() or f'Profile {idx + 1}'
        profile_id = str(profile.get('id') or '').strip()
        weight_g = float(_to_float(profile.get('weight_g'), 0))
        hours = float(_to_float(profile.get('estimated_print_hours'), 0))
        price_val = float(_to_float(profile.get('price'), 0))
        colors = profile.get('colors') if isinstance(profile.get('colors'), list) else []
        image_urls = profile.get('image_urls') if isinstance(profile.get('image_urls'), list) else []
        profile_image = str((image_urls[0] if image_urls else (global_images[0] if global_images else '')) or '').strip()

        part_rows = []
        insufficient_by_part = {}
        sufficient_by_part = {}

        if not colors:
            fallback_name = str((default_profile or {}).get('name') or 'Main').strip() or 'Main'
            fallback_used = max(0.0, weight_g)
            colors = [{'name': fallback_name, 'hex': '#8b8b8b', 'used_g': fallback_used}]

        for part_idx, color in enumerate(colors):
            color_name = str((color or {}).get('name') or f'Color {part_idx + 1}').strip() or f'Color {part_idx + 1}'
            color_hex = str((color or {}).get('hex') or '#8b8b8b').strip() or '#8b8b8b'
            used_g = float(_to_float((color or {}).get('used_g'), 0))
            if used_g <= 0 and weight_g > 0:
                used_g = max(0.0, weight_g / max(1, len(colors)))
            part_name = f'{color_name} Part'
            part_key = f'part_{part_idx}'
            part_rows.append({
                'part': part_name,
                'suggested_filament': color_name,
                'suggested_hex': color_hex,
                'used_g': round(max(0.0, used_g), 2),
            })

            insufficient = []
            sufficient = []
            for filament in filament_catalog:
                fname = str(filament.get('name') or '').strip()
                if not fname:
                    continue
                remaining = float(_to_float(filament.get('remaining_g'), 0))
                out_of_stock = bool(filament.get('out_of_stock'))
                if out_of_stock or (used_g > 0 and remaining < used_g):
                    insufficient.append(fname)
                else:
                    sufficient.append(fname)
            insufficient_by_part[part_key] = insufficient
            sufficient_by_part[part_key] = sufficient

        normalized_profiles.append({
            'id': profile_id,
            'name': profile_name,
            'weight_g': round(max(0.0, weight_g), 2),
            'estimated_print_hours': round(max(0.0, hours), 2),
            'price': round(max(0.0, price_val), 2),
            'is_default': bool(profile.get('is_default')),
            'colors': colors,
        })

        profile_customizations.append({
            'profile_id': profile_id,
            'profile_name': profile_name,
            'image_url': profile_image,
            'parts_configuration': part_rows,
            'insufficient_filaments': [],
            'insufficient_filaments_by_part': insufficient_by_part,
            'sufficient_filaments_by_part': sufficient_by_part,
        })

        if bool(profile.get('is_default')) or (not default_parts and idx == 0):
            default_parts = part_rows
            default_suggested = [
                f"{str(p.get('part') or '').strip()}: {str(p.get('suggested_filament') or '').strip()}"
                for p in part_rows
                if str(p.get('part') or '').strip() and str(p.get('suggested_filament') or '').strip()
            ]

    if normalized_profiles and not any(bool(p.get('is_default')) for p in normalized_profiles):
        normalized_profiles[0]['is_default'] = True

    default_profile_obj = next((p for p in normalized_profiles if p.get('is_default')), normalized_profiles[0] if normalized_profiles else {})
    suggested_filament = ''
    if default_parts:
        suggested_filament = str(default_parts[0].get('suggested_filament') or '').strip()

    return {
        'id': 'ext-' + str(uuid.uuid4())[:8],
        'title': title,
        'description': 'Configure profile and part colors, then add to cart.',
        'image_url': str((global_images[0] if global_images else '') or '').strip(),
        'makerworld_url': model_url,
        'price': round(max(0.0, _to_float(default_profile_obj.get('price'), 0)), 2),
        'model_weight': round(max(0.0, _to_float(default_profile_obj.get('weight_g'), _to_float(calc_result.get('weight'), 0))), 2),
        'profile_options': [str(p.get('name') or '').strip() for p in normalized_profiles if str(p.get('name') or '').strip()],
        'profile_pricing': normalized_profiles,
        'suggested_profile': str(default_profile_obj.get('name') or '').strip(),
        'parts_configuration': default_parts,
        'profile_customizations': profile_customizations,
        'insufficient_filaments': [],
        'suggested_filament': suggested_filament,
        'suggested_colors': ' | '.join(default_suggested),
        'estimated_print_hours': round(max(0.0, _to_float(default_profile_obj.get('estimated_print_hours'), _to_float(calc_result.get('hours'), 0))), 2),
    }


@app.route('/extension-api/hover-order-item', methods=['GET'])
def extension_hover_order_item_api():
    auth_user = _extension_request_user({'ext_auth': request.args.get('ext_auth')})
    if not auth_user:
        return jsonify({'ok': False, 'error': 'Login required.', 'error_code': 'AUTH_REQUIRED'}), 401
    _sync_extension_session(auth_user)

    model_url = str(request.args.get('model_url') or '').strip()
    if not _is_allowed_model_link(model_url):
        return jsonify({'ok': False, 'error': 'Only makerworld.com or printables.com links are allowed.'}), 400

    try:
        item = _build_extension_hover_order_item(model_url)
        return jsonify({'ok': True, 'item': item})
    except Exception as exc:
        app.logger.exception('extension_hover_order_item_api failed')
        return jsonify({'ok': False, 'error': str(exc) or 'Unable to build item data.'}), 500


@app.route('/extension/order-overlay', methods=['GET'])
def extension_order_overlay_page():
    auth_user = _extension_request_user({'ext_auth': request.args.get('ext_auth')})
    if not auth_user:
        return jsonify({'ok': False, 'error': 'Login required.', 'error_code': 'AUTH_REQUIRED'}), 401
    _sync_extension_session(auth_user)

    model_url = str(request.args.get('model_url') or '').strip()
    if not _is_allowed_model_link(model_url):
        return redirect(url_for('user_home'))

    context = _build_user_portal_context(auth_user.get('user_id'))
    return render_template(
        'extension_order_overlay.html',
        active_tab='home',
        model_url=model_url,
        extension_auth_token=str(auth_user.get('token') or ''),
        confetti_asset_name='extension_confetti.gif',
        **context,
    )


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


def _save_cart_item_for_owner(db, owner_id, payload):
    if not isinstance(payload, dict):
        return {'ok': False, 'error': 'Invalid payload'}, 400

    raw_link = str(payload.get('link') or '').strip()
    if not raw_link:
        return {'ok': False, 'error': 'Model link is required'}, 400

    if not _is_allowed_model_link(raw_link):
        return {'ok': False, 'error': 'Only makerworld.com or printables.com links are allowed.'}, 400
    app.logger.info(f"[SAVE_ITEM] owner_id={owner_id!r}, link={raw_link!r}, payload_keys={list(payload.keys())}")
    item_id = str(payload.get('id') or '').strip()
    existing_order_id = str(payload.get('orderId') or payload.get('order_id') or '').strip()

    if existing_order_id:
        existing = next(
            (
                o for o in db.get('orders', [])
                if str(o.get('id') or '') == existing_order_id and o.get('owner') == owner_id
            ),
            None,
        )
        if existing is not None:
            return {'ok': True, 'order_id': existing_order_id}, 200

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
            return {'ok': True, 'order_id': str(existing_by_item.get('id') or '')}, 200

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

    product_name = str(payload.get('displayName') or payload.get('name') or 'Unnamed Order').strip() or 'Unnamed Order'
    profile = str(payload.get('profile') or '').strip()
    weight = max(0.0, _to_float(payload.get('weight'), 0))
    preferred_date = str(payload.get('preferredDeliveryDate') or '').strip()
    quantity = _parse_cart_quantity(payload.get('quantity'), default=1)
    est_unit_price = max(0.0, _to_float(payload.get('estimatedPrice'), 0))
    total_price = int(round(est_unit_price * quantity))
    signature = _cart_payload_signature(owner_id, raw_link, product_name, color_string, profile, weight, preferred_date)

    existing_match = next(
        (
            o for o in db.get('orders', [])
            if o.get('owner') == owner_id
            and _is_cart_visible_order(o)
            and str(o.get('cart_signature') or '').strip() == signature
        ),
        None,
    )
    if existing_match is not None:
        order_id = str(existing_match.get('id') or '').strip()
        if item_id:
            existing_match['cart_item_id'] = item_id
        save_db(db)
        return {'ok': True, 'order_id': order_id}, 200

    order_id = uuid.uuid4().hex[:8]
    order = {
        'id': order_id,
        'created_at': datetime.utcnow().isoformat(),
        'updated_at': datetime.utcnow().isoformat(),
        'owner': owner_id,
        'name': product_name,
        'product_name': product_name,
        'link': raw_link,
        'profile': profile,
        'color': color_string,
        'print_weight_g': weight * quantity,
        'quantity': quantity,
        'status': 'In Cart',
        'print_price': str(total_price),
        'material_fee': '0',
        'delivery_time': 'TBD',
        'preferred_delivery_date': preferred_date,
        'estimated_print_hours': max(0.0, _to_float(payload.get('estimated_print_hours'), 0)),
        'messages': [],
        'admin_note': '',
        'cart_item_id': item_id,
        'cart_signature': signature,
    }
    db.setdefault('orders', []).append(order)
    save_db(db)
    return {'ok': True, 'order_id': order_id}, 200


def _checkout_error(message, status_code, wants_json=False):
    if wants_json:
        return jsonify({'ok': False, 'error': str(message or 'Checkout failed')}), int(status_code)
    return redirect(url_for('user_cart'))


@app.route('/checkout', methods=['POST'])
def checkout():
    wants_json = str(request.args.get('response_mode') or '').strip().lower() == 'json'

    payload_for_auth = request.get_json(silent=True) if request.is_json else None
    auth_user = _extension_request_user(payload_for_auth or {})
    if not auth_user:
        if wants_json:
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        return redirect(url_for('user_login'))

    _sync_extension_session(auth_user)
    owner_id = str(auth_user.get('user_id') or '').strip()
    if not owner_id:
        return _checkout_error('Unauthorized', 401, wants_json=wants_json)

    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, list):
            items = body
        else:
            items = body.get('items') or []
    else:
        raw = str(request.form.get('cart_json') or '').strip()
        try:
            items = json.loads(raw) if raw else []
        except Exception:
            items = []

    if not isinstance(items, list) or not items:
        return _checkout_error('No items selected for checkout.', 400, wants_json=wants_json)

    db = get_db()
    created_checkout_ids = []
    created_checkout_orders = []
    checked_out_item_ids = []

    for item in items:
        if not isinstance(item, dict):
            continue

        incoming_order_id = str(item.get('orderId') or item.get('order_id') or item.get('id') or '').strip()
        existing = None
        if incoming_order_id:
            existing = next(
                (
                    o for o in db.get('orders', [])
                    if str(o.get('id') or '').strip() == incoming_order_id
                    and str(o.get('owner') or '').strip() == owner_id
                    and _is_cart_visible_order(o)
                ),
                None,
            )

        if existing is None:
            fallback_link = str(item.get('link') or '').strip()
            fallback_name = str(item.get('displayName') or item.get('name') or '').strip().lower()
            fallback_profile = str(item.get('profile') or '').strip().lower()
            fallback_match = next(
                (
                    o for o in db.get('orders', [])
                    if str(o.get('owner') or '').strip() == owner_id
                    and _is_cart_visible_order(o)
                    and (
                        (fallback_link and str(o.get('link') or '').strip() == fallback_link)
                        or (
                            fallback_name
                            and str(o.get('product_name') or o.get('name') or '').strip().lower() == fallback_name
                            and (
                                not fallback_profile
                                or str(o.get('profile') or '').strip().lower() == fallback_profile
                            )
                        )
                    )
                ),
                None,
            )
            if fallback_match is None:
                continue
            existing = fallback_match
            incoming_order_id = str(existing.get('id') or '').strip()

        status_key = str(existing.get('status') or '').strip().lower()
        if status_key not in {'in cart', 'quoted', 'pending quote', 'pending'}:
            continue

        quantity = _parse_cart_quantity(item.get('quantity'), default=1)
        quoted_unit_price = max(0.0, _to_float(item.get('estimatedPrice'), 0))
        if quoted_unit_price <= 0:
            existing_total = max(0.0, _to_float(existing.get('print_price'), 0)) + max(0.0, _to_float(existing.get('material_fee'), 0))
            existing_qty = _parse_cart_quantity(existing.get('quantity'), default=1)
            quoted_unit_price = existing_total / existing_qty if existing_qty > 0 else existing_total
        if quoted_unit_price <= 0:
            continue

        selected_profile = existing.get('profile') if existing.get('profile') is not None else item.get('profile')
        selected_profile = '' if selected_profile is None else str(selected_profile)
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

        existing['updated_at'] = datetime.utcnow().isoformat()
        existing['name'] = existing.get('name') or existing.get('product_name') or str(item.get('displayName') or 'Unnamed Order')
        existing['owner'] = owner_id
        existing['product_name'] = existing.get('product_name') or existing.get('name') or str(item.get('displayName') or 'Unnamed Order')
        existing['link'] = existing.get('link') or str(item.get('link') or '')
        existing['print_weight_g'] = model_weight * quantity
        existing['profile'] = selected_profile
        existing['color'] = selected_colors
        existing['quantity'] = quantity
        existing['status'] = 'Pending'
        existing['cart_checkout_archived_at'] = datetime.utcnow().isoformat()
        existing['fixed_price'] = True
        existing['suggested_profile'] = selected_profile
        existing['suggested_colors'] = selected_colors
        existing['print_price'] = str(total_price)
        existing['material_fee'] = '0'
        existing['delivery_time'] = existing.get('delivery_time') or 'TBD'
        existing['preferred_delivery_date'] = existing.get('preferred_delivery_date') or str(item.get('preferredDeliveryDate') or '')
        existing['estimated_print_hours'] = est_hours_per_unit * quantity
        existing['final_unit_price'] = int(round(quoted_unit_price))
        existing['final_total_price'] = total_price
        existing['selected_print_profile'] = selected_profile
        existing['selected_colors'] = selected_colors
        existing['payment_status'] = existing.get('payment_status') or 'Unpaid'
        if not existing.get('quote_notified_at'):
            existing['quote_notified_at'] = datetime.utcnow().isoformat()
        if not existing.get('checkout_confirmed_at'):
            existing['checkout_confirmed_at'] = datetime.utcnow().isoformat()

        created_checkout_ids.append(incoming_order_id)
        created_checkout_orders.append(existing)

        checked_item_id = str(item.get('id') or '').strip()
        if checked_item_id:
            checked_out_item_ids.append(checked_item_id)

    app.logger.info(f"[CHECKOUT] created_checkout_ids={created_checkout_ids}, skipped={len(items) - len(created_checkout_ids)}")

    if not created_checkout_ids:
        return _checkout_error('No eligible cart items found for checkout.', 400, wants_json=wants_json)

    try:
        save_db(db, raise_on_error=True)
    except Exception as exc:
        app.logger.exception(f"[CHECKOUT] ERROR: Failed to persist checkout orders {created_checkout_ids}: {exc}")
        return _checkout_error('Failed to save checkout to database.', 500, wants_json=wants_json)

    if checked_out_item_ids:
        session['cart_clear_ids'] = checked_out_item_ids
    session['last_checkout_order_ids'] = created_checkout_ids

    if wants_json:
        grand_total = int(round(sum(
            max(0.0, _to_float(o.get('final_total_price', o.get('print_price')), 0))
            for o in created_checkout_orders
        )))
        unit_count = int(sum(max(1, _parse_cart_quantity(o.get('quantity'), default=1)) for o in created_checkout_orders))
        return jsonify({
            'ok': True,
            'message': 'Checkout complete.',
            'order_ids': created_checkout_ids,
            'order_count': len(created_checkout_orders),
            'unit_count': unit_count,
            'grand_total': grand_total,
            'count': len(created_checkout_orders),
        }), 200

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
    payload = request.get_json(silent=True) or {}
    app.logger.info(f"[CART_SAVE] Received save-item request, payload keys: {list(payload.keys())}")
    
    auth_user = _extension_request_user(payload)
    if not auth_user:
        app.logger.warning(f"[CART_SAVE] Unauthorized: no auth_user")
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    _sync_extension_session(auth_user)
    owner_id = str(auth_user.get('user_id') or '').strip()
    if not owner_id:
        app.logger.warning(f"[CART_SAVE] Unauthorized: no owner_id from auth_user={auth_user}")
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    app.logger.info(f"[CART_SAVE] Saving item for owner={owner_id!r}, item_link={payload.get('link')!r}, item_id={payload.get('id')!r}")
    db = get_db()
    response_body, status_code = _save_cart_item_for_owner(db, owner_id, payload)
    app.logger.info(f"[CART_SAVE] Response: status={status_code}, ok={response_body.get('ok')}, order_id={response_body.get('order_id')}")
    return jsonify(response_body), status_code


@app.route('/cart/remove/<order_id>', methods=['POST'])
def remove_cart_item(order_id):
    auth_user = _extension_request_user()
    if not auth_user:
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    user_id = str(auth_user.get('user_id') or '').strip()
    target_id = str(order_id or '').strip()
    if not target_id:
        return jsonify({'ok': False, 'error': 'Missing order id'}), 400

    db = get_db()
    remaining_orders = []
    removed = False
    for order in db.get('orders', []):
        if (
            str(order.get('id') or '').strip() == target_id
            and str(order.get('owner') or '').strip() == user_id
            and _is_cart_visible_order(order)
        ):
            app.logger.info(f"[CART_DELETE] Removing order {target_id} for user {user_id} from cart")
            removed = True
            continue
        remaining_orders.append(order)

    if removed:
        db['orders'] = remaining_orders
        app.logger.info(f"[CART_DELETE] Deleting order {target_id} from database")
        _execute("DELETE FROM orders WHERE id = %s", (target_id,))
        save_db(db)
        app.logger.info(f"[CART_DELETE] Successfully deleted order {target_id}")
    else:
        app.logger.warning(f"[CART_DELETE] Order {target_id} not found for user {user_id}")

    return jsonify({'ok': True, 'removed': removed, 'order_id': target_id}), 200


@app.route('/cart/orders', methods=['GET'])
def get_cart_orders():
    auth_user = _extension_request_user()
    if not auth_user:
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    _sync_extension_session(auth_user)
    user_id = str(auth_user.get('user_id') or '').strip()

    db = get_db()
    cart_orders = [
        o for o in db.get('orders', [])
        if str(o.get('owner') or '').strip() == user_id
        and _is_cart_visible_order(o)
    ]
    
    # OPTIMIZATION: Return only essential fields for list view
    # Details endpoint can be added later if needed for full order expansion
    minimal_items = [
        {
            'orderId': str(o.get('id') or ''),
            'displayName': str(o.get('product_name') or o.get('name') or ''),
            'link': str(o.get('link') or ''),
            'status': str(o.get('status') or ''),
            'print_price': float(o.get('print_price') or 0),
            'quantity': int(o.get('quantity') or 1),
        }
        for o in cart_orders
    ]
    
    return jsonify({'ok': True, 'items': minimal_items}), 200


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


@app.route('/api/health', methods=['GET'])
def api_health():
    try:
        # Tiny query keeps the pooled DB connection active.
        _execute("SELECT 1", fetch=True)
        return jsonify({'status': 'alive', 'db': 'connected'})
    except Exception:
        return jsonify({'status': 'alive', 'db': 'disconnected'}), 503


def _makerworld_http_get(url, timeout=25):
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }
    if cloudscraper is not None:
        client = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
        response = client.get(url, headers=headers, timeout=timeout)
    else:
        response = requests.get(url, headers=headers, timeout=timeout)
    status_code = int(response.status_code or 0)
    html_text = str(response.text or '')

    # Some hosts block plain HTTP clients from data-center IPs. If available,
    # try a real browser context to render JS-delivered markup.
    if (status_code >= 400 or len(html_text) < 4000) and sync_playwright is not None:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        user_agent=headers['User-Agent'],
                        locale='en-US',
                        viewport={'width': 1366, 'height': 900},
                    )
                    page = context.new_page()
                    response_pw = page.goto(url, wait_until='domcontentloaded', timeout=int(timeout * 1000))
                    try:
                        page.wait_for_load_state('networkidle', timeout=5000)
                    except Exception:
                        pass
                    html_pw = str(page.content() or '')
                    status_pw = int(response_pw.status) if response_pw is not None else status_code
                    if html_pw:
                        status_code = status_pw
                        html_text = html_pw
                finally:
                    browser.close()
        except Exception:
            # Keep the original response if browser rendering is unavailable.
            pass

    return status_code, html_text


def _makerworld_slug_to_title(raw_slug):
    slug = unquote(str(raw_slug or '')).strip('-_ ')
    if not slug:
        return 'MakerWorld Model'
    return re.sub(r'\s+', ' ', slug.replace('-', ' ').replace('_', ' ')).strip().title() or 'MakerWorld Model'


def _extract_makerworld_live_rows(html_text, query=''):
    rows = []
    seen_model_ids = set()
    normalized_query = str(query or '').strip().lower()

    href_pattern = re.compile(
        r'href=["\'](?P<href>(?:https?://(?:www\.)?makerworld\.com)?/[a-z]{2}/models/\d+[^"\']*)["\']',
        re.IGNORECASE,
    )
    raw_url_pattern = re.compile(
        r'(?P<href>(?:https?://(?:www\.)?makerworld\.com)?/[a-z]{2}/models/\d+(?:-[^"\'\s<]*)?)',
        re.IGNORECASE,
    )
    image_pattern = re.compile(
        r'(https://makerworld\.bblmw\.com/[^"\'\s]+\.(?:png|jpg|jpeg|webp|gif)(?:\?[^"\'\s]*)?)',
        re.IGNORECASE,
    )

    matches = []
    raw_html = str(html_text or '')
    decoded_html = raw_html.replace('\\/', '/').replace('&amp;', '&')
    matches.extend((m.start(), str(m.group('href') or '')) for m in href_pattern.finditer(decoded_html))
    matches.extend((m.start(), str(m.group('href') or '')) for m in raw_url_pattern.finditer(decoded_html))

    for start_idx, raw_href in matches:
        href = str(raw_href or '').replace('&amp;', '&').strip()
        if not href:
            continue
        if href.startswith('/'):
            full_url = f'https://makerworld.com{href}'
        else:
            full_url = href

        parsed = urlparse(full_url)
        path = str(parsed.path or '')
        model_match = re.search(r'/models/(\d+)(?:-([^/?#]+))?', path, re.IGNORECASE)
        if not model_match:
            continue
        model_id = str(model_match.group(1) or '').strip()
        if not model_id or model_id in seen_model_ids:
            continue

        slug = str(model_match.group(2) or '').strip()
        title = _makerworld_slug_to_title(slug)
        if normalized_query and normalized_query not in title.lower() and normalized_query not in full_url.lower():
            continue

        around = decoded_html[start_idx:min(len(decoded_html), start_idx + 1600)]
        image_match = image_pattern.search(around)
        image_url = str(image_match.group(1) if image_match else '').strip()

        seen_model_ids.add(model_id)
        rows.append({
            'id': f'mw-live-{model_id}',
            'title': title,
            'description': '',
            'image_url': image_url,
            'makerworld_url': full_url,
            'price': 0.0,
            'model_weight': 0.0,
            'profile_options': [],
            'profile_pricing': [],
            'suggested_profile': '',
            'parts_configuration': [],
            'profile_customizations': [],
            'insufficient_filaments': [],
        })

        if len(rows) >= 400:
            break

    return rows


@app.route('/api/user/updates')
def user_updates_api():
    if not session.get('user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session.get('user_id')
    user_rows = _execute("SELECT json FROM users WHERE id = %s", (user_id,), fetch=True) or []
    user = None
    if user_rows:
        try:
            user = json.loads(user_rows[0][0])
        except Exception:
            user = None

    order_rows = _execute(
        "SELECT json FROM orders WHERE (json::jsonb ->> 'owner') = %s",
        (user_id,),
        fetch=True,
    ) or []
    user_orders = []
    for row in order_rows:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                user_orders.append(parsed)
        except Exception:
            continue

    user_orders = sorted(
        user_orders,
        key=lambda o: _order_last_modified(o) or datetime.min,
        reverse=True,
    )
    user_notifications = _build_user_notifications(user, user_orders)
    unread_user_notification_count = sum(1 for n in user_notifications if n.get('is_unread'))
    user_updates_token = ''
    if user_orders:
        last_dt = _order_last_modified(user_orders[0])
        if last_dt is not None:
            user_updates_token = last_dt.isoformat()

    return jsonify({
        'latest_update_token': user_updates_token,
        'unread_notifications': int(unread_user_notification_count or 0),
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
    next_path = _safe_next_path(request.values.get('next'), 'dashboard')
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(next_path)
    return render_template('login.html', next_path=next_path)

# --- EGRESS DIAGNOSTICS ENDPOINT ---
@app.route('/admin/egress-stats')
def admin_egress_stats():
    """Admin-only endpoint to view egress diagnostics and high-traffic routes."""
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    
    stats = _get_egress_stats()
    sorted_routes = sorted(
        stats['requests'].items(),
        key=lambda x: x[1]['total_bytes'],
        reverse=True
    )
    
    return jsonify({
        'ok': True,
        'total_requests': stats['total_tracked_requests'],
        'total_bytes': stats['total_tracked_bytes'],
        'top_talkers': [
            {
                'route': route,
                'count': data['count'],
                'total_bytes': data['total_bytes'],
                'avg_bytes': data['avg_bytes'],
                'percent_of_total': round(100.0 * data['total_bytes'] / max(1, stats['total_tracked_bytes']), 2),
            }
            for route, data in sorted_routes[:20]
        ],
        'measurement_note': 'Tracking JSON/HTML responses only (not static assets)',
    })

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
        'pending': 2,
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


def _remove_user_from_featured_items(featured_items, user_id):
    cleaned = []
    target_id = str(user_id or '').strip()
    for item in featured_items or []:
        current = dict(item or {})
        raw_targets = current.get('target_users')
        if not isinstance(raw_targets, list) or not raw_targets:
            raw_targets = [current.get('target_user', 'ALL')]

        normalized_targets = _normalize_target_users(raw_targets, fallback='ALL')
        if 'ALL' in normalized_targets:
            cleaned.append(current)
            continue

        next_targets = [t for t in normalized_targets if str(t or '').strip() != target_id]
        if not next_targets:
            continue

        current['target_users'] = next_targets
        current['target_user'] = next_targets[0] if len(next_targets) == 1 else 'MULTI'
        cleaned.append(current)

    return cleaned


@app.route('/dashboard/users/<user_id>/delete', methods=['POST'])
def delete_dashboard_user(user_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    target_id = str(user_id or '').strip()
    if not target_id:
        return _redirect_back_to_dashboard('#users-section')

    _execute("DELETE FROM users WHERE id = %s", (target_id,))
    _execute("DELETE FROM orders WHERE (json::jsonb ->> 'owner') = %s", (target_id,))
    return _redirect_back_to_dashboard('#users-section')


@app.route('/dashboard/users/cleanup-temporary', methods=['POST'])
def cleanup_temporary_dashboard_users():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    rows = _execute("SELECT id, json FROM users", fetch=True) or []
    temp_ids = []
    for row in rows:
        uid = str(row[0] or '').strip()
        if not uid:
            continue
        try:
            payload = json.loads(row[1]) if row[1] else {}
        except Exception:
            payload = {}
        username = str((payload or {}).get('username') or '').strip().lower()
        if username.startswith('tmp_ext_'):
            temp_ids.append(uid)

    if not temp_ids:
        return _redirect_back_to_dashboard('#users-section')

    _execute("DELETE FROM users WHERE id = ANY(%s)", (temp_ids,))
    _execute("DELETE FROM orders WHERE (json::jsonb ->> 'owner') = ANY(%s)", (temp_ids,))
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


@app.route('/extension-api/desktop-capture/push', methods=['POST'])
def extension_push_desktop_capture_link():
    payload = request.get_json(silent=True) or {}
    if not _extension_request_authorized(payload):
        return jsonify({'ok': False, 'error': 'Unauthorized extension API key.'}), 401

    model_url = str(payload.get('model_url') or payload.get('makerworld_link') or '').strip()
    if not _is_allowed_model_link(model_url):
        return jsonify({'ok': False, 'error': 'Only makerworld.com or printables.com links are allowed.'}), 400

    source = str(payload.get('source') or '').strip()
    triggered_at = str(payload.get('triggered_at') or '').strip()

    with _DESKTOP_CAPTURE_SIGNAL_LOCK:
        _DESKTOP_CAPTURE_SIGNAL['id'] = int(_DESKTOP_CAPTURE_SIGNAL.get('id') or 0) + 1
        _DESKTOP_CAPTURE_SIGNAL['model_url'] = model_url
        _DESKTOP_CAPTURE_SIGNAL['source'] = source
        _DESKTOP_CAPTURE_SIGNAL['triggered_at'] = triggered_at
        signal_id = _DESKTOP_CAPTURE_SIGNAL['id']

    return jsonify({'ok': True, 'signal_id': signal_id, 'model_url': model_url})


@app.route('/extension-api/desktop-capture/poll', methods=['GET'])
def extension_poll_desktop_capture_link():
    api_key = request.args.get('api_key') or request.headers.get('X-Api-Key', '')
    if not _extension_request_authorized({'api_key': api_key}):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    try:
        last_id = int(request.args.get('last_id') or 0)
    except (TypeError, ValueError):
        last_id = 0

    with _DESKTOP_CAPTURE_SIGNAL_LOCK:
        current = dict(_DESKTOP_CAPTURE_SIGNAL)

    current_id = int(current.get('id') or 0)
    if current_id <= last_id:
        return jsonify({'ok': True, 'has_update': False, 'signal_id': current_id})

    return jsonify({
        'ok': True,
        'has_update': True,
        'signal_id': current_id,
        'model_url': str(current.get('model_url') or ''),
        'source': str(current.get('source') or ''),
        'triggered_at': str(current.get('triggered_at') or ''),
    })


@app.route('/extension-api/confirm-capture', methods=['POST'])
def extension_confirm_capture():
    payload = request.get_json(silent=True) or {}
    if not _extension_request_authorized(payload):
        return jsonify({'ok': False, 'error': 'Unauthorized extension API key.'}), 401

    target_user_id = str(payload.get('target_user_id') or '').strip()
    target_username = str(payload.get('target_username') or '').strip()
    target_users_raw = payload.get('target_users')

    db = get_db()
    users = db.get('users', [])

    # Multi-user path: target_users array sent by desktop app
    if isinstance(target_users_raw, list) and target_users_raw:
        normalized = _normalize_target_users(target_users_raw, fallback='ALL')
        if 'ALL' in normalized:
            owner_user_id = 'ALL'
            resolved_target_users = ['ALL']
        else:
            resolved_target_users = []
            for uname in normalized:
                matched = next(
                    (u for u in users if str(u.get('username') or '').strip().lower() == str(uname).lower()),
                    None,
                )
                if matched:
                    resolved_target_users.append(str(matched.get('id') or '').strip())
            if not resolved_target_users:
                return jsonify({'ok': False, 'error': 'No valid target users found.'}), 400
            owner_user_id = resolved_target_users[0]
    else:
        # Legacy single-user path
        # Resolve user ID — only use target_user_id if it's non-empty AND actually exists
        owner_user_id = ''
        if target_user_id and any(str(u.get('id') or '').strip() == target_user_id for u in users):
            owner_user_id = target_user_id
        if not owner_user_id and target_username:
            matched_user = next(
                (u for u in users if str(u.get('username') or '').strip().lower() == target_username.lower()),
                None,
            )
            if matched_user:
                owner_user_id = str(matched_user.get('id') or '').strip()

        if not owner_user_id:
            return jsonify({'ok': False, 'error': 'Target user not found. Provide a valid target_username or target_user_id.'}), 400
        resolved_target_users = [owner_user_id]

    model_link = str(payload.get('makerworld_url') or payload.get('link') or '').strip()
    if not _is_allowed_model_link(model_link):
        return jsonify({'ok': False, 'error': 'Only makerworld.com or printables.com links are allowed.'}), 400

    title = str(payload.get('title') or payload.get('name') or '').strip() or 'MakerWorld Model'
    image_url = str(payload.get('image_url') or '').strip()
    description = str(payload.get('description') or '').strip()

    suggested_filament = str(payload.get('suggested_filament') or payload.get('color') or '').strip()
    suggested_colors = str(payload.get('suggested_colors') or suggested_filament or '').strip()
    suggested_profile = str(payload.get('suggested_profile') or '').strip()
    show_in_slideshow = _to_bool(payload.get('show_in_slideshow'), default=False)

    raw_insuf = payload.get('insufficient_filaments')
    insufficient_filaments = [str(n).strip() for n in raw_insuf if str(n).strip()] if isinstance(raw_insuf, list) else []

    raw_category_options = payload.get('category_options')
    category_options = raw_category_options if isinstance(raw_category_options, list) else []
    category_options = [
        {
            'part': str(item.get('part') or '').strip(),
            'suggested_filament': str(item.get('suggested_filament') or '').strip(),
        }
        for item in category_options
        if isinstance(item, dict)
    ]

    raw_parts_configuration = payload.get('parts_configuration')
    parts_configuration = raw_parts_configuration if isinstance(raw_parts_configuration, list) else []
    parts_configuration = [
        {
            'part': str(item.get('part') or '').strip(),
            'suggested_filament': str(item.get('suggested_filament') or '').strip(),
        }
        for item in parts_configuration
        if isinstance(item, dict)
    ]

    raw_profile_customizations = payload.get('profile_customizations')
    profile_customizations = raw_profile_customizations if isinstance(raw_profile_customizations, list) else []
    normalized_profile_customizations = []
    for item in profile_customizations:
        if not isinstance(item, dict):
            continue
        sufficient_filaments_by_part = item.get('sufficient_filaments_by_part')
        if not isinstance(sufficient_filaments_by_part, dict):
            sufficient_filaments_by_part = {}
        insufficient_filaments_by_part = item.get('insufficient_filaments_by_part')
        if not isinstance(insufficient_filaments_by_part, dict):
            insufficient_filaments_by_part = {}
        insufficient_filaments = item.get('insufficient_filaments')
        if not isinstance(insufficient_filaments, list):
            insufficient_filaments = []
        parts_configuration = item.get('parts_configuration')
        if not isinstance(parts_configuration, list):
            parts_configuration = []

        normalized_profile_customizations.append({
            'profile_id': str(item.get('profile_id') or '').strip(),
            'profile_name': str(item.get('profile_name') or '').strip(),
            'is_default': bool(item.get('is_default')),
            'image_url': str(item.get('image_url') or '').strip(),
            'suggested_filament': str(item.get('suggested_filament') or '').strip(),
            'suggested_colors': str(item.get('suggested_colors') or '').strip(),
            'sufficient_filaments_by_part': {
                str(k).strip(): [
                    str(n).strip()
                    for n in v
                    if str(n).strip()
                ]
                for k, v in sufficient_filaments_by_part.items()
                if str(k).strip() and isinstance(v, list)
            },
            'insufficient_filaments_by_part': {
                str(k).strip(): [
                    str(n).strip()
                    for n in v
                    if str(n).strip()
                ]
                for k, v in insufficient_filaments_by_part.items()
                if str(k).strip() and isinstance(v, list)
            },
            'insufficient_filaments': [
                str(n).strip()
                for n in insufficient_filaments
                if str(n).strip()
            ],
            'parts_configuration': [
                {
                    'part': str(p.get('part') or '').strip(),
                    'suggested_filament': str(p.get('suggested_filament') or '').strip(),
                }
                for p in parts_configuration
                if isinstance(p, dict)
            ],
        })
    profile_customizations = normalized_profile_customizations

    profile_pricing = _normalize_profile_pricing(payload.get('profile_pricing'))
    if profile_pricing:
        suggested_profile = next((p['name'] for p in profile_pricing if p.get('is_default')), suggested_profile)

    profile_options = [p['name'] for p in profile_pricing]

    try:
        price_value = float(payload.get('price') or 0)
    except (TypeError, ValueError):
        price_value = 0.0

    manual_weight = payload.get('print_weight_g', payload.get('printWeightG'))
    manual_hours = payload.get('estimated_print_hours', payload.get('printHours'))
    calc_result = _calculate_model_metrics(model_link, manual_weight=manual_weight, manual_hours=manual_hours)
    base_weight = float(calc_result.get('weight') or 50.0)
    estimated_print_hours = float(calc_result.get('hours') or 0.0)
    weight_source = str(calc_result.get('source') or 'fallback_default')
    weight_needs_review = bool(calc_result.get('needs_review'))

    # If extension did not send a computed price, compute it server-side from current settings.
    if price_value <= 0:
        price_value = float(calc_result.get('raw_price') or 0.0)

    featured_id = str(uuid.uuid4())[:10]
    new_item = {
        'id': featured_id,
        'title': title,
        'image_url': image_url,
        'makerworld_url': model_link,
        'description': description,
        'price': price_value,
        'suggested_filament': suggested_filament,
        'suggested_colors': suggested_colors,
        'suggested_profile': suggested_profile,
        'profile_options': profile_options,
        'profile_pricing': profile_pricing,
        'category_options': category_options,
        'parts_configuration': parts_configuration,
        'profile_customizations': profile_customizations,
        'show_in_slideshow': show_in_slideshow,
        'target_user': owner_user_id,
        'target_users': resolved_target_users,
        'base_weight': base_weight,
        'estimated_print_hours': estimated_print_hours,
        'weight_source': weight_source,
        'weight_needs_review': weight_needs_review,
        'weight_scrape_error': str(calc_result.get('error') or ''),
        'source': 'browser_extension',
        'insufficient_filaments': insufficient_filaments,
    }

    db.setdefault('featured_prints', []).append(new_item)
    save_db(db)
    return jsonify({
        'ok': True,
        'featured_id': featured_id,
        'base_weight': base_weight,
        'estimated_print_hours': estimated_print_hours,
        'weight_source': weight_source,
        'weight_needs_review': weight_needs_review,
        'weight_scrape_error': str(calc_result.get('error') or ''),
        'auto_price_formatted': str(calc_result.get('formatted_price') or ''),
    })


@app.route('/extension-api/scrape-model-metrics', methods=['GET'])
def extension_scrape_model_metrics():
    api_key = request.args.get('api_key') or request.headers.get('X-Api-Key', '')
    if not _extension_request_authorized({'api_key': api_key}):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    model_url = str(request.args.get('model_url') or '').strip()
    if not _is_allowed_model_link(model_url):
        return jsonify({'ok': False, 'error': 'Only makerworld.com or printables.com links are allowed.'}), 400

    manual_weight = request.args.get('manual_weight')
    calc_result = _calculate_model_metrics(model_url, manual_weight=manual_weight)

    profile_metrics = {'title': str(calc_result.get('title') or 'MakerWorld Model'), 'profiles': []}
    profile_extract_error = ''
    try:
        profile_metrics = _extract_model_profile_metrics(
            model_url,
            pricing_overrides={
                'base_fee': request.args.get('base_fee'),
                'price_per_gram': request.args.get('price_per_gram'),
                'power_cost_per_hour': request.args.get('power_cost_per_hour'),
                'profit_margin': request.args.get('profit_margin'),
            },
            calc_result=calc_result,
        )
    except Exception as exc:
        profile_extract_error = str(exc)

    profiles = profile_metrics.get('profiles') or []
    image_urls = _extract_model_image_urls(str(calc_result.get('html') or ''), _extract_makerworld_instances(str(calc_result.get('html') or '')))
    if not image_urls:
        try:
            quick = _quick_parse_model_page(model_url)
            quick_html = str(quick.get('html') or '')
            quick_instances = _extract_makerworld_instances(quick_html)
            image_urls = _extract_model_image_urls(quick_html, quick_instances)
        except Exception:
            image_urls = []
    default_profile = next((p for p in profiles if p.get('is_default')), profiles[0] if profiles else None)

    weight_g = float(default_profile.get('weight_g') or calc_result.get('weight') or 0.0) if default_profile else float(calc_result.get('weight') or 0.0)
    estimated_print_hours = float(default_profile.get('estimated_print_hours') or calc_result.get('hours') or 0.0) if default_profile else float(calc_result.get('hours') or 0.0)
    raw_price = float(default_profile.get('price') or calc_result.get('raw_price') or 0.0) if default_profile else float(calc_result.get('raw_price') or 0.0)

    return jsonify({
        'ok': bool(calc_result.get('success')),
        'weight_g': weight_g,
        'estimated_print_hours': estimated_print_hours,
        'title': str(profile_metrics.get('title') or calc_result.get('title') or 'MakerWorld Model'),
        'weight_source': str(calc_result.get('source') or 'unknown'),
        'weight_needs_review': bool(calc_result.get('needs_review')),
        'raw_price': raw_price,
        'formatted_price': f"Rp{int(round(raw_price)):,}".replace(',', '.'),
        'error': str(calc_result.get('error') or profile_extract_error or ''),
        'profiles': profiles,
        'image_urls': image_urls,
        'image_url': image_urls[0] if image_urls else '',
    })


@app.route('/extension-api/debug-model-text', methods=['GET'])
def extension_debug_model_text():
    api_key = request.args.get('api_key') or request.headers.get('X-Api-Key', '')
    if not _extension_request_authorized({'api_key': api_key}):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    model_url = str(request.args.get('model_url') or '').strip()
    if not _is_allowed_model_link(model_url):
        return jsonify({'ok': False, 'error': 'Only makerworld.com or printables.com links are allowed.'}), 400

    try:
        quick = _quick_parse_model_page(model_url)
        html_text = str(quick.get('html') or '')

        weight_key_hits = []
        for m in re.finditer(r'"weight"\s*:\s*([^,}\]]+)', html_text, re.IGNORECASE):
            s = max(0, m.start() - 120)
            e = min(len(html_text), m.end() + 140)
            weight_key_hits.append(html_text[s:e].replace('\n', ' '))
            if len(weight_key_hits) >= 15:
                break

        usedg_hits = []
        for m in re.finditer(r'"usedG"\s*:\s*"?(\d+(?:\.\d+)?)"?', html_text, re.IGNORECASE):
            usedg_hits.append(float(m.group(1)))
            if len(usedg_hits) >= 50:
                break

        grams_hits = []
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*g\b', html_text, re.IGNORECASE):
            grams_hits.append(float(m.group(1)))
            if len(grams_hits) >= 80:
                break

        calc = _calculate_model_metrics(model_url)

        return jsonify({
            'ok': True,
            'title': quick.get('title') or 'MakerWorld Model',
            'regex_weight_value': quick.get('weight'),
            'usedG_sample': usedg_hits[:25],
            'grams_sample': grams_hits[:25],
            'weight_key_context': weight_key_hits,
            'calc_result': {
                'success': bool(calc.get('success')),
                'source': str(calc.get('source') or ''),
                'weight': float(calc.get('weight') or 0.0),
                'hours': float(calc.get('hours') or 0.0),
                'formatted_price': str(calc.get('formatted_price') or ''),
                'error': str(calc.get('error') or ''),
                'needs_review': bool(calc.get('needs_review')),
            },
        })
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/extension-api/pricing-config', methods=['GET'])
def extension_pricing_config():
    api_key = request.args.get('api_key') or request.headers.get('X-Api-Key', '')
    if not _extension_request_authorized({'api_key': api_key}):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    settings = _load_control_center_settings()
    response_data = {
        'ok': True,
        'base_service_fee': settings['base_service_fee'],
        'price_per_gram': settings['price_per_gram'],
        'power_cost_per_hour': settings['power_cost_per_hour'],
        'profit_margin': settings['profit_margin'],
    }
    # Cache pricing config for 1 hour (changes rarely)
    resp = make_response(jsonify(response_data))
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

# --- PAGINATED FEATURED ITEMS API (CACHEABLE) ---
@app.route('/api/featured-items', methods=['GET'])
def api_featured_items():
    """
    Paginated featured items endpoint with cache headers.
    Query params: page (1-indexed), page_size (3-12, default 6)
    """
    user_id = session.get('user_id')
    username = ''
    
    if user_id:
        user = _get_user_by_id(user_id)
        if user:
            username = str(user.get('username') or '').strip()
    
    page = _to_int(request.args.get('page'), default=1, min_value=1)
    page_size = _to_int(request.args.get('page_size'), default=6, min_value=3, max_value=12)
    
    # Fetch featured items that are visible to this user
    all_featured = [
        f for f in _get_all_featured_prints()
        if _featured_item_visible_to_user(f, user_id or '', username)
    ]
    
    total = len(all_featured)
    total_pages = max(1, int(math.ceil(total / float(page_size))))
    page = min(page, total_pages)
    
    start = (page - 1) * page_size
    items_page = all_featured[start:start + page_size]
    
    response_data = {
        'ok': True,
        'items': items_page,
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': total_pages,
    }
    
    # Cache this response for 1 hour since featured items change rarely
    resp = make_response(jsonify(response_data))
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@app.route('/extension-api/app-data', methods=['GET'])
def extension_app_data():
    api_key = request.args.get('api_key') or request.headers.get('X-Api-Key', '')
    if not _extension_request_authorized({'api_key': api_key}):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    # Optimized: Only load users and settings, not full db
    users = [
        {'id': str(u.get('id') or ''), 'username': str(u.get('username') or '')}
        for u in _get_all_users()
        if u.get('id') and u.get('username')
    ]
    settings = _get_settings()
    filaments, _ = _normalize_filaments(settings)
    filament_data = [
        {
            'name': f['name'],
            'color_hex': f.get('color_hex') or f.get('hex') or '#888888',
            'hex': f.get('hex') or f.get('color_hex') or '#888888',
            'material': f.get('material', ''),
            'remaining_g': int(f.get('remaining_g', f.get('total_g', 1000))),
            'out_of_stock': bool(f.get('out_of_stock', False)),
        }
        for f in filaments
    ]
    try:
        rows = _execute(
            "SELECT id, name, price_modifier, is_default FROM print_profiles WHERE is_active = TRUE ORDER BY is_default DESC, name",
            fetch=True
        ) or []
        profiles = [
            {'id': r[0], 'name': r[1], 'price_modifier': float(r[2] or 0), 'is_default': bool(r[3])}
            for r in rows
        ]
    except Exception:
        profiles = []
    if not any(p['is_default'] for p in profiles) and profiles:
        profiles[0]['is_default'] = True
    return jsonify({'ok': True, 'users': users, 'filaments': filament_data, 'profiles': profiles})


@app.route('/dashboard/settings/update', methods=['POST'])
def update_control_center_settings():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    base_service_fee = max(0.0, _to_float(request.form.get('base_service_fee'), 0.0))
    price_per_gram = max(0.0, _to_float(request.form.get('price_per_gram', request.form.get('default_price_per_gram')), 0.0))
    power_cost_per_hour = max(0.0, _to_float(request.form.get('power_cost_per_hour'), 0.0))
    profit_margin = max(0.0, _to_float(request.form.get('profit_margin'), CONTROL_SETTING_DEFAULTS['profit_margin']))
    lifetime_total = max(0.0, _to_float(request.form.get('lifetime_total_plastic_used'), 0.0))
    lifetime_profit_override = _to_float(request.form.get('lifetime_profit'), _get_business_stat('lifetime_profit', 0.0))
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
    profile_pricing = _normalize_profile_pricing(profile_pricing_raw)
    price_value = 0.0
    if profile_pricing:
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
    show_in_slideshow = _to_bool(request.form.get('show_in_slideshow'), default=True)

    if not (title and image_url and makerworld_url):
        return _redirect_back_to_dashboard('#home-section')

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
        'show_in_slideshow': show_in_slideshow,
        'target_user': target_user,
        'target_users': target_users,
        'source': 'dashboard',
    }

    db.setdefault('featured_prints', []).append(new_item)
    save_db(db)
    return _redirect_back_to_dashboard('#home-section')


@app.route('/dashboard/featured/edit/<item_id>', methods=['POST'])
def edit_featured_print(item_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    items = db.get('featured_prints', [])
    item = next((f for f in items if f.get('id') == item_id), None)
    if not item:
        return _redirect_back_to_dashboard('#home-section')

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
    profile_pricing = _normalize_profile_pricing(profile_pricing_raw)
    price_value = float(item.get('price', 0) or 0)
    if profile_pricing:
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
    show_in_slideshow = _to_bool(request.form.get('show_in_slideshow'), default=_to_bool(item.get('show_in_slideshow'), default=False))

    if not (title and image_url and makerworld_url):
        return _redirect_back_to_dashboard('#home-section')

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
    item['show_in_slideshow'] = show_in_slideshow
    item['target_user'] = target_user
    item['target_users'] = target_users

    save_db(db)
    return _redirect_back_to_dashboard('#home-section')

@app.route('/dashboard/featured/delete/<item_id>', methods=['POST'])
def delete_featured_print(item_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    db['featured_prints'] = [f for f in db.get('featured_prints', []) if f.get('id') != item_id]
    save_db(db)
    return _redirect_back_to_dashboard('#home-section')

@app.route('/dashboard/featured/toggle-slideshow/<item_id>', methods=['POST'])
def toggle_featured_slideshow(item_id):
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    db = get_db()
    item = next((f for f in db.get('featured_prints', []) if f.get('id') == item_id), None)
    if not item:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    item['show_in_slideshow'] = not bool(item.get('show_in_slideshow'))
    save_db(db)
    return jsonify({'ok': True, 'show_in_slideshow': item['show_in_slideshow']})

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
    app.logger.info("[CART] POST /create_featured_order - START")
    data = request.get_json() or {}
    app.logger.info(f"[CART] Request data: {data}")
    auth_user = _extension_request_user(data)
    app.logger.info(f"[CART] Auth user: {auth_user}")
    if not auth_user:
        app.logger.warning("[CART] ERROR: Not authorized")
        return jsonify({'error': 'Not authorized'}), 401
    _sync_extension_session(auth_user)
    owner_id = str(auth_user.get('user_id') or '').strip()
    owner_username = str(auth_user.get('username') or '').strip()
    title = (data.get('title') or '').strip()
    makerworld_link = (data.get('makerworld_link') or '').strip()
    try:
        price_val = float(data.get('price', 0))
    except Exception:
        price_val = 0
    app.logger.info(f"[CART] Title: {title}, Link: {makerworld_link}, Price: {price_val}")

    # allow featured items to suggest a specific profile and/or multi-color mapping
    incoming_profile = data.get('profile')
    profile_choice = incoming_profile if incoming_profile is not None else data.get('suggested_profile')
    profile_choice = '' if profile_choice is None else str(profile_choice)
    suggested_colors = (data.get('suggested_colors') or data.get('filament') or '').strip()
    category_choices = data.get('category_choices') or []
    if not isinstance(category_choices, list):
        category_choices = []

    if not title or not makerworld_link:
        app.logger.warning("[CART] ERROR: Missing required fields")
        return jsonify({'error': 'Missing required fields'}), 400

    app.logger.info("[CART] Getting database...")
    db = get_db()
    app.logger.info(f"[CART] Got database, checking for duplicates...")
    now = datetime.utcnow()
    for existing in db.get('orders', []):
        if existing.get('owner') != owner_id:
            continue
        status_key = str(existing.get('status') or '').strip().lower()
        if status_key not in {'quoted', 'in cart', 'pending quote', 'pending'}:
            continue
        if str(existing.get('link') or '').strip() != makerworld_link:
            continue
        if str(existing.get('name') or existing.get('product_name') or '').strip() != title:
            continue
        if str(existing.get('profile') or '') != profile_choice:
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
        'owner': owner_id,
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
    username = owner_username or owner_id or 'A user'
    app.logger.info(f"[CART] Creating order {order_id}, adding admin notification...")
    _add_admin_notification(
        db,
        notif_type='featured_order',
        title='Featured print ordered',
        message=f'{username} ordered {title}.',
        order_id=order_id,
        actor_user_id=owner_id,
    )
    app.logger.info(f"[CART] Saving database with order {order_id}...")
    try:
        save_db(db, raise_on_error=True)
    except Exception as exc:
        app.logger.exception(f"[CART] ERROR: Failed to persist order {order_id}: {exc}")
        return jsonify({'error': 'Failed to save order to database.'}), 500
    app.logger.info(f"[CART] SUCCESS: Order {order_id} saved")
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


app.register_blueprint(
    create_model_capture_blueprint(
        {
            'get_db': get_db,
            'save_db': save_db,
            'to_float': _to_float,
        }
    )
)


if __name__ == '__main__':
    # Validate required environment configuration before starting
    _validate_startup_environment()
    
    parser = argparse.ArgumentParser(description='Run the 3D print orders app.')
    parser.add_argument('--import', dest='import_path', help='Import JSONBin dump (exported JSON) into Postgres (full replace).')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the Flask server on')
    args = parser.parse_args()

    if args.import_path:
        import_jsonbin_dump(args.import_path)
    else:
        app.run(debug=True, port=args.port, use_reloader=False)