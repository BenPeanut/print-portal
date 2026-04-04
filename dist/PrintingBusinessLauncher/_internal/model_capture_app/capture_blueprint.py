import json
import re
import threading
import uuid
from datetime import datetime
from urllib.parse import quote, urlparse

import requests
from flask import Blueprint, Response, jsonify, render_template, request, session, url_for

_ALLOWED_HOSTS = {'makerworld.com'}
_CAPTURE_LOCK = threading.Lock()
_CAPTURE_TOKENS = {}
_CAPTURE_SETTINGS = {}
_LATEST_CAPTURE = {}


def _allowed_host(hostname):
    host = (hostname or '').strip().lower()
    if host.startswith('www.'):
        host = host[4:]
    return any(host == allowed or host.endswith('.' + allowed) for allowed in _ALLOWED_HOSTS)


def _normalize_model_url(raw_url):
    candidate = (raw_url or '').strip()
    if not candidate:
        return ''
    parsed = urlparse(candidate)
    if not parsed.scheme:
        parsed = urlparse('https://' + candidate)
    if parsed.scheme not in {'http', 'https'}:
        return ''
    if not _allowed_host(parsed.netloc):
        return ''
    normalized = parsed._replace(fragment='')
    return normalized.geturl()


def _extract_title_from_url(model_url):
    try:
        path = urlparse(model_url).path or ''
    except Exception:
        return ''

    segments = [seg for seg in path.split('/') if seg]
    for segment in segments:
        cleaned = segment.strip()
        if not cleaned:
            continue
        if re.match(r'^\d+-[A-Za-z0-9\-]+', cleaned):
            parts = cleaned.split('-')
            if len(parts) > 1:
                return ' '.join(parts[1:]).strip().title()
    if segments:
        return segments[-1].replace('-', ' ').strip().title()
    return ''


def _meta_content(html, key, by_property=False):
    attr = 'property' if by_property else 'name'
    pattern = rf'<meta[^>]+{attr}=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']'
    match = re.search(pattern, html, flags=re.IGNORECASE)
    return (match.group(1) if match else '').strip()


def _html_title(html):
    match = re.search(r'<title>(.*?)</title>', html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ''
    value = re.sub(r'\s+', ' ', match.group(1)).strip()
    return value


def _json_ld_name(html):
    for script_match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw_json = script_match.group(1).strip()
        if not raw_json:
            continue
        try:
            parsed = json.loads(raw_json)
        except Exception:
            continue

        stack = parsed if isinstance(parsed, list) else [parsed]
        for row in stack:
            if isinstance(row, dict):
                name = str(row.get('name') or '').strip()
                if name:
                    return name
    return ''


def _weight_guess_g(html):
    lowered = html.lower()
    patterns = [
        r'weight[^\d]{0,20}(\d+(?:\.\d+)?)\s*g',
        r'model weight[^\d]{0,20}(\d+(?:\.\d+)?)\s*g',
        r'(\d+(?:\.\d+)?)\s*grams',
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        try:
            guessed = float(match.group(1))
        except Exception:
            continue
        if guessed >= 0:
            return round(guessed, 2)
    return 0.0


def _scrape_model_page(model_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    response = requests.get(model_url, headers=headers, timeout=12)
    response.raise_for_status()
    html = response.text or ''

    title = (
        _meta_content(html, 'og:title', by_property=True)
        or _json_ld_name(html)
        or _meta_content(html, 'twitter:title', by_property=True)
        or _html_title(html)
        or _extract_title_from_url(model_url)
        or 'MakerWorld Model'
    )

    description = (
        _meta_content(html, 'og:description', by_property=True)
        or _meta_content(html, 'description')
        or ''
    )
    image_url = (
        _meta_content(html, 'og:image', by_property=True)
        or _meta_content(html, 'twitter:image', by_property=True)
        or ''
    )

    return {
        'model_url': model_url,
        'title': title,
        'description': description,
        'image_url': image_url,
        'weight_guess_g': _weight_guess_g(html),
        'captured_at': datetime.utcnow().isoformat() + 'Z',
    }


def _defaults_for_user(user_id, request_host_url):
    existing = _CAPTURE_SETTINGS.get(user_id, {})
    defaults = {
        'api_base': request_host_url.rstrip('/'),
        'default_profile': '1',
        'default_filament': 'Bamboo Green PLA',
        'default_quantity': 1,
        'open_cart_after_confirm': True,
    }
    defaults.update(existing)
    return defaults


def _new_token_for_user(user_id):
    token = uuid.uuid4().hex[:24]
    _CAPTURE_TOKENS[token] = user_id
    return token


def _ensure_user_token(user_id):
    for token, owner in _CAPTURE_TOKENS.items():
        if owner == user_id:
            return token
    return _new_token_for_user(user_id)


def _active_capture_scope():
    if session.get('logged_in'):
        scope = str(session.get('model_capture_admin_scope') or '').strip()
        if not scope:
            scope = 'admin:' + uuid.uuid4().hex[:16]
            session['model_capture_admin_scope'] = scope
        return scope, True

    user_id = str(session.get('user_id') or '').strip()
    if user_id:
        return 'user:' + user_id, False

    return None, None


def _bridge_script(api_base, token):
    config = {
        'apiBase': api_base.rstrip('/'),
        'token': token,
    }
    cfg_json = json.dumps(config)
    return f"""(function() {{
  if (window.__makerCaptureBridgeActive) {{
    window.__makerCaptureBridgePing && window.__makerCaptureBridgePing();
    return;
  }}
  window.__makerCaptureBridgeActive = true;

  var cfg = {cfg_json};
  var hoveredLink = '';

  function showToast(message, ok) {{
    var existing = document.getElementById('mw-capture-toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.id = 'mw-capture-toast';
    toast.textContent = message;
    toast.style.position = 'fixed';
    toast.style.right = '18px';
    toast.style.bottom = '18px';
    toast.style.padding = '11px 14px';
    toast.style.borderRadius = '12px';
    toast.style.fontSize = '13px';
    toast.style.zIndex = '2147483647';
    toast.style.color = '#fff';
    toast.style.background = ok ? '#217a4a' : '#a13030';
    toast.style.boxShadow = '0 8px 30px rgba(0,0,0,.22)';
    document.body.appendChild(toast);
    setTimeout(function() {{ toast.remove(); }}, 2600);
  }}

  function findMakerLink(target) {{
    var current = target;
    while (current && current !== document.body) {{
      if (current.tagName === 'A' && current.href && /makerworld\\.com/i.test(current.href)) {{
        return current.href;
      }}
      current = current.parentElement;
    }}
    return '';
  }}

  document.addEventListener('mouseover', function(e) {{
    var link = findMakerLink(e.target);
    if (link) hoveredLink = link;
  }}, true);

  function shouldIgnoreShortcut(evt) {{
    var tag = (evt.target && evt.target.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || evt.target && evt.target.isContentEditable;
  }}

  async function sendCapture() {{
    if (!hoveredLink) {{
      showToast('Hover a MakerWorld model card, then press Q.', false);
      return;
    }}

    try {{
      var res = await fetch(cfg.apiBase + '/model-capture-app/api/capture', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          token: cfg.token,
          model_url: hoveredLink,
          source_page: location.href,
          triggered_at: new Date().toISOString()
        }})
      }});

      var payload = await res.json();
      if (!res.ok || !payload.ok) {{
        throw new Error(payload.error || ('Capture failed (' + res.status + ')'));
      }}
      showToast('Captured model. Check your companion app to confirm.', true);
    }} catch (err) {{
      showToast('Capture failed: ' + (err && err.message ? err.message : 'Unknown error'), false);
    }}
  }}

  document.addEventListener('keydown', function(evt) {{
    if (shouldIgnoreShortcut(evt)) return;
    if (evt.repeat) return;
    if (evt.key && evt.key.toLowerCase() === 'q') {{
      evt.preventDefault();
      sendCapture();
    }}
  }});

  window.__makerCaptureBridgePing = function() {{
    showToast('Maker capture bridge already active.', true);
  }};

  showToast('Maker capture ready. Hover model then press Q.', true);
}})();
"""


def create_model_capture_blueprint(deps):
    bp = Blueprint(
        'model_capture_app',
        __name__,
        template_folder='templates',
        static_folder='static',
        url_prefix='/model-capture-app',
    )

    def _to_int(value, default_value=1, min_value=1, max_value=99):
        try:
            parsed = int(value)
        except Exception:
            parsed = default_value
        parsed = max(min_value, min(max_value, parsed))
        return parsed

    @bp.route('/')
    def home():
        scope_key, admin_mode = _active_capture_scope()
        if not scope_key:
            return jsonify({'error': 'Please login first at /login (admin) or /user_login (user).'}), 401

        db = deps['get_db']()
        users = db.get('users', []) if isinstance(db, dict) else []
        user_options = [
            {
                'id': str(u.get('id') or '').strip(),
                'username': str(u.get('username') or '').strip() or str(u.get('id') or '').strip(),
            }
            for u in users
            if str(u.get('id') or '').strip()
        ]
        user_options.sort(key=lambda row: row.get('username', '').lower())

        with _CAPTURE_LOCK:
            token = _ensure_user_token(scope_key)
            settings = _defaults_for_user(scope_key, request.host_url)
            if admin_mode and not settings.get('target_user_id') and user_options:
                settings['target_user_id'] = user_options[0]['id']
            _CAPTURE_SETTINGS[scope_key] = settings

        api_base = settings.get('api_base', request.host_url.rstrip('/')).rstrip('/')
        bridge_url = f"{api_base}/model-capture-app/bridge.js?token={quote(token)}&api_base={quote(api_base)}"
        bookmarklet = "javascript:(function(){var s=document.createElement('script');s.src='" + bridge_url + "';s.async=true;document.body.appendChild(s);}());"

        return render_template(
            'model_capture_app/home.html',
            page_title='MakerWorld Companion App',
            capture_token=token,
            capture_settings=settings,
            bookmarklet_code=bookmarklet,
            api_base=api_base,
            latest_capture=_LATEST_CAPTURE.get(scope_key),
            admin_mode=admin_mode,
            user_options=user_options,
        )

    @bp.route('/bridge.js')
    def bridge_js():
        token = (request.args.get('token') or '').strip()
        api_base = (request.args.get('api_base') or request.host_url).strip().rstrip('/')
        if not token:
            return Response("console.warn('Missing token in bridge script URL.');", mimetype='application/javascript')
        script = _bridge_script(api_base, token)
        return Response(script, mimetype='application/javascript')

    @bp.route('/api/capture', methods=['POST', 'OPTIONS'])
    def capture_api():
        if request.method == 'OPTIONS':
            response = Response(status=204)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            return response

        payload = request.get_json(silent=True) or {}
        token = str(payload.get('token') or '').strip()
        model_url = _normalize_model_url(payload.get('model_url') or '')
        if not token:
            response = jsonify({'ok': False, 'error': 'Missing token'})
            response.status_code = 400
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response
        if not model_url:
            response = jsonify({'ok': False, 'error': 'Invalid MakerWorld URL'})
            response.status_code = 400
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response

        with _CAPTURE_LOCK:
            owner_user_id = _CAPTURE_TOKENS.get(token)
        if not owner_user_id:
            response = jsonify({'ok': False, 'error': 'Unknown token. Re-open companion app and refresh bookmarklet.'})
            response.status_code = 403
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response

        try:
            scraped = _scrape_model_page(model_url)
        except Exception as exc:
            response = jsonify({'ok': False, 'error': f'Could not scrape model page: {exc}'})
            response.status_code = 502
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response

        with _CAPTURE_LOCK:
            settings = _defaults_for_user(owner_user_id, request.host_url)
            _CAPTURE_SETTINGS[owner_user_id] = settings
            _LATEST_CAPTURE[owner_user_id] = {
                'source_page': str(payload.get('source_page') or '').strip(),
                'triggered_at': str(payload.get('triggered_at') or '').strip(),
                'model': scraped,
                'suggested_order': {
                    'name': scraped.get('title') or _extract_title_from_url(model_url) or 'MakerWorld Model',
                    'link': model_url,
                    'print_weight_g': float(scraped.get('weight_guess_g') or 0.0),
                    'profile': str(settings.get('default_profile') or '1'),
                    'color': str(settings.get('default_filament') or 'Bamboo Green PLA'),
                    'quantity': _to_int(settings.get('default_quantity'), default_value=1, min_value=1, max_value=20),
                    'status': 'In Cart',
                },
                'captured_at': datetime.utcnow().isoformat() + 'Z',
            }

        response = jsonify({'ok': True, 'model_title': scraped.get('title')})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    @bp.route('/api/latest')
    def latest_api():
        scope_key, _ = _active_capture_scope()
        if not scope_key:
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        with _CAPTURE_LOCK:
            latest = _LATEST_CAPTURE.get(scope_key)
        return jsonify({'ok': True, 'latest': latest})

    @bp.route('/api/settings', methods=['POST'])
    def save_settings_api():
        scope_key, admin_mode = _active_capture_scope()
        if not scope_key:
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        payload = request.get_json(silent=True) or {}
        db = deps['get_db']()
        users = db.get('users', []) if isinstance(db, dict) else []
        valid_user_ids = {str(u.get('id') or '').strip() for u in users if str(u.get('id') or '').strip()}

        with _CAPTURE_LOCK:
            settings = _defaults_for_user(scope_key, request.host_url)
            settings['default_profile'] = str(payload.get('default_profile') or settings.get('default_profile') or '1').strip() or '1'
            settings['default_filament'] = str(payload.get('default_filament') or settings.get('default_filament') or 'Bamboo Green PLA').strip() or 'Bamboo Green PLA'
            settings['default_quantity'] = _to_int(payload.get('default_quantity'), default_value=int(settings.get('default_quantity') or 1), min_value=1, max_value=20)
            settings['open_cart_after_confirm'] = bool(payload.get('open_cart_after_confirm', settings.get('open_cart_after_confirm', True)))
            if admin_mode:
                requested_target = str(payload.get('target_user_id') or settings.get('target_user_id') or '').strip()
                if requested_target in valid_user_ids:
                    settings['target_user_id'] = requested_target
                elif not settings.get('target_user_id') and valid_user_ids:
                    settings['target_user_id'] = sorted(valid_user_ids)[0]
            _CAPTURE_SETTINGS[scope_key] = settings

        return jsonify({'ok': True, 'settings': settings})

    @bp.route('/api/confirm', methods=['POST'])
    def confirm_order_api():
        scope_key, admin_mode = _active_capture_scope()
        if not scope_key:
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

        payload = request.get_json(silent=True) or {}

        with _CAPTURE_LOCK:
            latest = _LATEST_CAPTURE.get(scope_key)
            settings = _defaults_for_user(scope_key, request.host_url)

        if not latest:
            return jsonify({'ok': False, 'error': 'No captured model to confirm yet.'}), 400

        suggestion = dict(latest.get('suggested_order') or {})
        name = str(payload.get('name') or suggestion.get('name') or 'MakerWorld Model').strip() or 'MakerWorld Model'
        model_url = _normalize_model_url(payload.get('link') or suggestion.get('link') or '')
        if not model_url:
            return jsonify({'ok': False, 'error': 'Suggested model URL is invalid.'}), 400

        try:
            weight_g = max(0.0, float(payload.get('print_weight_g', suggestion.get('print_weight_g', 0)) or 0))
        except Exception:
            weight_g = max(0.0, float(suggestion.get('print_weight_g', 0) or 0))

        profile = str(payload.get('profile') or suggestion.get('profile') or settings.get('default_profile') or '1').strip() or '1'
        color = str(payload.get('color') or suggestion.get('color') or settings.get('default_filament') or 'Bamboo Green PLA').strip() or 'Bamboo Green PLA'
        quantity = _to_int(payload.get('quantity', suggestion.get('quantity', settings.get('default_quantity', 1))), default_value=1, min_value=1, max_value=20)

        db = deps['get_db']()
        save_db = deps['save_db']
        users = db.get('users', []) if isinstance(db, dict) else []
        valid_user_ids = {str(u.get('id') or '').strip() for u in users if str(u.get('id') or '').strip()}

        if admin_mode:
            owner_user_id = str(payload.get('owner_user_id') or settings.get('target_user_id') or '').strip()
            if not owner_user_id:
                return jsonify({'ok': False, 'error': 'Choose a target user before confirming.'}), 400
            if owner_user_id not in valid_user_ids:
                return jsonify({'ok': False, 'error': 'Target user does not exist.'}), 400
        else:
            owner_user_id = str(session.get('user_id') or '').strip()
            if not owner_user_id:
                return jsonify({'ok': False, 'error': 'User session missing.'}), 401

        order_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow().isoformat()
        new_order = {
            'id': order_id,
            'created_at': now,
            'updated_at': now,
            'name': name,
            'nickname': None,
            'owner': owner_user_id,
            'product_name': name,
            'admin_note': '',
            'messages': [],
            'link': model_url,
            'print_weight_g': round(weight_g * quantity, 2),
            'profile': profile,
            'color': color,
            'quantity': quantity,
            'status': 'In Cart',
            'print_price': '0',
            'material_fee': '0',
            'delivery_time': 'TBD',
            'preferred_delivery_date': '',
            'estimated_print_hours': 0.0,
        }

        db.setdefault('orders', []).append(new_order)
        save_db(db)

        with _CAPTURE_LOCK:
            if _LATEST_CAPTURE.get(scope_key):
                _LATEST_CAPTURE[scope_key]['last_confirmed_order_id'] = order_id

        open_cart = bool(settings.get('open_cart_after_confirm', True))
        if admin_mode:
            redirect_url = url_for('dashboard') + '#orders-section'
        else:
            redirect_url = url_for('user_cart') if open_cart else url_for('order_page')
        return jsonify({'ok': True, 'order_id': order_id, 'redirect_url': redirect_url})

    return bp
