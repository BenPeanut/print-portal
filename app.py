import os
import requests
import uuid
from urllib.parse import urlparse, unquote
import re
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
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
        return data
    except Exception as e:
        print(f"DB Error: {e}")
        return {"orders": [], "settings": {"filaments": []}}

def save_db(data):
    requests.put(BASE_URL, headers=HEADERS, json=data)

# --- USER ROUTES ---
@app.route('/')
def index():
    db = get_db()
    filaments = db.get('settings', {}).get('filaments', [])
    return render_template('index.html', filaments=filaments)

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
    
    # Capture the name if the user provided one, otherwise "Unnamed Order"
    provided_name = request.form.get('name', '').strip()
    if not provided_name:
        provided_name = "Unnamed Order"

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

    new_order = {
        "id": order_id,
        "name": provided_name,
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
            order['name'] = new_name
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
    current_colors = ", ".join(db.get('settings', {}).get('filaments', []))
    return render_template('dashboard.html', orders=active_orders, current_colors=current_colors)

@app.route('/delete_order/<order_id>', methods=['POST'])
def delete_order(order_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    db['orders'] = [o for o in db['orders'] if o['id'] != order_id]
    save_db(db)
    return redirect(url_for('dashboard'))

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