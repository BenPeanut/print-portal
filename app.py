import os
import requests
import uuid
from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)

# --- CONFIGURATION ---
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_testing_key')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin') 

BIN_ID = os.environ.get('BIN_ID', '699701f0ae596e708f3731e1')
API_KEY = os.environ.get('API_KEY', '$2a$10$C8m3WYEfsXkmPchVKfSMB.BVTc6L21MB26E.HkR3NhA/DJQU89/Ni')

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

    new_order = {
        "id": order_id,
        "name": provided_name,
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