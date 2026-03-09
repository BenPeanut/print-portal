import argparse
import os
import sqlite3
import uuid
import json
from urllib.parse import urlparse, unquote
import re
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURATION ---
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_testing_key')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')

# SQLite local database file path
DB_PATH = os.path.join(os.path.dirname(__file__), 'data.sqlite3')

# --- DATABASE HELPERS ---

def _execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, params)
    if fetch:
        rows = cur.fetchall()
        conn.close()
        return rows
    conn.commit()
    conn.close()
    return None


def _init_db():
    # Create tables if they don't exist
    _execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    _execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            json TEXT NOT NULL
        )
        """
    )
    _execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            json TEXT NOT NULL
        )
        """
    )
    _execute(
        """
        CREATE TABLE IF NOT EXISTS featured_prints (
            id TEXT PRIMARY KEY,
            json TEXT NOT NULL
        )
        """
    )


def _load_all():
    _init_db()

    settings = {"filaments": []}
    row = _execute("SELECT value FROM settings WHERE name = ?", ("settings",), fetch=True)
    if row:
        try:
            settings = json.loads(row[0][0])
        except Exception:
            pass

    users = []
    for r in _execute("SELECT json FROM users", fetch=True):
        try:
            users.append(json.loads(r[0]))
        except Exception:
            pass

    orders = []
    for r in _execute("SELECT json FROM orders", fetch=True):
        try:
            orders.append(json.loads(r[0]))
        except Exception:
            pass

    featured_prints = []
    for r in _execute("SELECT json FROM featured_prints", fetch=True):
        try:
            featured_prints.append(json.loads(r[0]))
        except Exception:
            pass

    return {
        "settings": settings,
        "users": users,
        "orders": orders,
        "featured_prints": featured_prints,
    }


def get_db():
    return _load_all()


def save_db(data):
    _init_db()

    # Settings
    try:
        _execute("DELETE FROM settings")
        _execute(
            "INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)",
            ("settings", json.dumps(data.get("settings", {"filaments": []})))
        )
    except Exception as e:
        print(f"Failed to save settings: {e}")

    # Users
    try:
        _execute("DELETE FROM users")
        for user in data.get("users", []):
            _execute(
                "INSERT OR REPLACE INTO users (id, json) VALUES (?, ?)",
                (user.get('id'), json.dumps(user))
            )
    except Exception as e:
        print(f"Failed to save users: {e}")

    # Orders
    try:
        _execute("DELETE FROM orders")
        for order in data.get("orders", []):
            _execute(
                "INSERT OR REPLACE INTO orders (id, json) VALUES (?, ?)",
                (order.get('id'), json.dumps(order))
            )
    except Exception as e:
        print(f"Failed to save orders: {e}")

    # Featured prints
    try:
        _execute("DELETE FROM featured_prints")
        for item in data.get("featured_prints", []):
            _execute(
                "INSERT OR REPLACE INTO featured_prints (id, json) VALUES (?, ?)",
                (item.get('id'), json.dumps(item))
            )
    except Exception as e:
        print(f"Failed to save featured prints: {e}")

# --- USER ROUTES ---
@app.route('/')
def index():
    db = get_db()
    filaments = db.get('settings', {}).get('filaments', [])
    # Require user login for using the site
    if not session.get('user_id'):
        return redirect(url_for('user_login'))

    user_id = session.get('user_id')
    # Collect recent orders for this user (most recent first)
    user_orders = [o for o in db.get('orders', []) if o.get('owner') == user_id]
    user_orders = sorted(user_orders, key=lambda o: o.get('id'), reverse=True)
    featured_prints = db.get('featured_prints', [])
    # only show for this user or for all users
    featured_items = [
        f for f in featured_prints
        if f.get('target_user') == 'ALL' or f.get('target_user') == user_id
    ]

    # if no featured items configured, show a small placeholder set
    if not featured_items:
        featured_items = [
            {
                "id": "placeholder-1",
                "image_url": "https://cdn.example.com/placeholder1.jpg",
                "title": "Sleek Gear Box",
                "makerworld_url": "https://makerworld.com/model/1234567-sleek-gear-box",
                "description": "A compact gearbox perfect for prototyping and robotics projects.",
                "price": 15000,
                "suggested_filament": "PLA",
                "target_user": "ALL"
            },
            {
                "id": "placeholder-2",
                "image_url": "https://cdn.example.com/placeholder2.jpg",
                "title": "Modular Phone Stand",
                "makerworld_url": "https://makerworld.com/model/7654321-modular-phone-stand",
                "description": "Adjustable phone stand that folds flat for travel.",
                "price": 12000,
                "suggested_filament": "PETG",
                "target_user": "ALL"
            },
            {
                "id": "placeholder-3",
                "image_url": "https://cdn.example.com/placeholder3.jpg",
                "title": "Articulated Dragon",
                "makerworld_url": "https://makerworld.com/model/2345678-articulated-dragon",
                "description": "A fun, moving model with printed joints for easy assembly.",
                "price": 20000,
                "suggested_filament": "PLA",
                "target_user": "ALL"
            },
        ]

    return render_template('index.html', filaments=filaments, user_orders=user_orders, featured_items=featured_items)


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
    query = request.form.get('q', '').strip().lower()
    db = get_db()
    user_id = session.get('user_id')
    if query:
        results = [
            o for o in db.get('orders', [])
            if o.get('owner') == user_id and (
                query in o.get('id', '').lower()
                or query in (o.get('name','') or '').lower()
                or query in (o.get('nickname','') or '').lower()
                or query in (o.get('product_name','') or '').lower()
            )
        ]
    else:
        results = [o for o in db.get('orders', []) if o.get('owner') == user_id]
    results = sorted(results, key=lambda o: o.get('id'), reverse=True)
    filaments = db.get('settings', {}).get('filaments', [])
    featured_prints = db.get('featured_prints', [])
    featured_items = [
        f for f in featured_prints
        if f.get('target_user') == 'ALL' or f.get('target_user') == user_id
    ]
    if not featured_items:
        featured_items = [
            {
                "id": "placeholder-1",
                "image_url": "https://cdn.example.com/placeholder1.jpg",
                "title": "Sleek Gear Box",
                "makerworld_url": "https://makerworld.com/model/1234567-sleek-gear-box",
                "description": "A compact gearbox perfect for prototyping and robotics projects.",
                "price": 15000,
                "suggested_filament": "PLA",
                "target_user": "ALL"
            },
            {
                "id": "placeholder-2",
                "image_url": "https://cdn.example.com/placeholder2.jpg",
                "title": "Modular Phone Stand",
                "makerworld_url": "https://makerworld.com/model/7654321-modular-phone-stand",
                "description": "Adjustable phone stand that folds flat for travel.",
                "price": 12000,
                "suggested_filament": "PETG",
                "target_user": "ALL"
            },
            {
                "id": "placeholder-3",
                "image_url": "https://cdn.example.com/placeholder3.jpg",
                "title": "Articulated Dragon",
                "makerworld_url": "https://makerworld.com/model/2345678-articulated-dragon",
                "description": "A fun, moving model with printed joints for easy assembly.",
                "price": 20000,
                "suggested_filament": "PLA",
                "target_user": "ALL"
            },
        ]
    return render_template('index.html', filaments=filaments, user_orders=results, search_query=request.form.get('q',''), featured_items=featured_items)

@app.route('/submit_order', methods=['POST'])
def submit_order():
    db = get_db()
    filaments = db.get('settings', {}).get('filaments', [])
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
            return render_template('index.html', filaments=filaments, error='Only makerworld.com or printables.com links are accepted.')
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
        "name": canonical_name,
        "nickname": nickname,
        "owner": session.get('user_id'),
        "product_name": product_name,
        "admin_note": "",
        "messages": [],
        "link": request.form.get('makerworld_link'),
        "profile": profile_choice,
        "color": color_string,
        "status": "Pending Quote",
        "print_price": "0",
        "material_fee": "0",
        "delivery_time": "TBD"
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
    db = get_db()
    order = next((o for o in db['orders'] if o['id'] == order_id), None)
    if order:
        return render_template('order.html', order=order)
    return "Order not found", 404

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
    locked_statuses = ['Printing', 'Done', 'Delivered']
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
    db = get_db()
    active_orders = db['orders']

    # build map of user ids to usernames for display
    user_map = {u['id']: u['username'] for u in db.get('users', [])}

    # Featured prints management
    featured_prints = db.get('featured_prints', [])

    current_colors = ", ".join(db.get('settings', {}).get('filaments', []))
    filaments = db.get('settings', {}).get('filaments', [])
    return render_template(
        'dashboard.html',
        orders=active_orders,
        current_colors=current_colors,
        filaments=filaments,
        user_map=user_map,
        users=db.get('users', []),
        featured_prints=featured_prints
    )

@app.route('/delete_order/<order_id>', methods=['POST'])
def delete_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    db['orders'] = [o for o in db['orders'] if o['id'] != order_id]
    save_db(db)
    return redirect(url_for('dashboard'))

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
    target_user = request.form.get('target_user', 'ALL')

    if not (title and image_url and makerworld_url and price):
        return redirect(url_for('dashboard'))

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
        'target_user': target_user,
    }

    db.setdefault('featured_prints', []).append(new_item)
    save_db(db)
    return redirect(url_for('dashboard'))

@app.route('/dashboard/featured/delete/<item_id>', methods=['POST'])
def delete_featured_print(item_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    db['featured_prints'] = [f for f in db.get('featured_prints', []) if f.get('id') != item_id]
    save_db(db)
    return redirect(url_for('dashboard'))

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
    profile_choice = (data.get('profile') or data.get('suggested_profile') or '1').strip() or '1'
    suggested_colors = (data.get('suggested_colors') or data.get('filament') or '').strip()

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
    for order in db['orders']:
        if order['id'] == order_id:
            order['status'] = request.form.get('status')
            order['print_price'] = request.form.get('print_price', '0')
            order['material_fee'] = request.form.get('material_fee', '0')
            order['delivery_time'] = request.form.get('delivery_time', 'TBD')
            # Save admin notes if provided
            order['admin_note'] = request.form.get('admin_note', order.get('admin_note', ''))
            break
    save_db(db)
    return redirect(url_for('dashboard'))

@app.route('/update_colors', methods=['POST'])
def update_colors():
    if not session.get('logged_in'): return redirect(url_for('login'))
    raw_colors = request.form.get('colors_list', '')
    color_list = [c.strip() for c in raw_colors.split(',') if c.strip()]
    db = get_db()
    db['settings']['filaments'] = color_list
    save_db(db)
    return redirect(url_for('dashboard'))

def import_jsonbin_dump(path):
    """Import a JSON dump (as-exported from JSONBin) into the local SQLite database."""
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

    save_db(data)
    print(f"Imported JSON data from {path} into local database.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the 3D print orders app.')
    parser.add_argument('--import', dest='import_path', help='Import JSONBin dump (exported JSON) into local DB')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the Flask server on')
    args = parser.parse_args()

    if args.import_path:
        import_jsonbin_dump(args.import_path)
    else:
        app.run(debug=True, port=args.port)