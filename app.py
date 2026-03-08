import os
import requests
import uuid
from urllib.parse import urlparse, unquote
import re
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURATION ---
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_testing_key')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin') 

BIN_ID = os.environ.get('BIN_ID')
API_KEY = os.environ.get('API_KEY')

BASE_URL = f"https://api.jsonbin.io/v3/b/{BIN_ID}"
HEADERS = {"X-Master-Key": API_KEY, "Content-Type": "application/json"}

# --- DATABASE HELPERS ---
def get_db():
    try:
        response = requests.get(BASE_URL, headers=HEADERS)
        data = response.json().get('record', {})
        if 'orders' not in data: data['orders'] = []
        if 'settings' not in data: data['settings'] = {"filaments": []}
        if 'users' not in data: data['users'] = []
        if 'featured_prints' not in data: data['featured_prints'] = []
        # legacy support for older key
        if 'featured_items' not in data: data['featured_items'] = []
        return data
    except Exception as e:
        print(f"DB Error: {e}")
        return {"orders": [], "settings": {"filaments": []}, "featured_prints": []}

def save_db(data):
    requests.put(BASE_URL, headers=HEADERS, json=data)

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
    return render_template(
        'dashboard.html',
        orders=active_orders,
        current_colors=current_colors,
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
    filament = (data.get('filament') or '').strip()

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
        'profile': '1',
        'color': filament,
        # use the existing “Waiting for Approval” status so user can confirm in the order page
        'status': 'Waiting for Approval',
        'print_price': str(int(price_val)),
        'material_fee': '0',
        'delivery_time': 'TBD',
        'fixed_price': True,
        'suggested_colors': filament,
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

if __name__ == '__main__':
    app.run(debug=True, port=5000)