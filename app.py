import os
import requests
import uuid
from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)

# --- CONFIGURATION ---
# This looks for environment variables on Render, but uses the text in quotes on your computer.
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_testing_key')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin') 

# PASTE YOUR REAL BIN ID HERE:
BIN_ID = os.environ.get('BIN_ID')
# PASTE YOUR REAL API KEY HERE:
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
    # Pulls filaments for the dropdown/mapping
    filaments = db.get('settings', {}).get('filaments', [])
    return render_template('index.html', filaments=filaments)

@app.route('/submit_order', methods=['POST'])
def submit_order():
    db = get_db()
    order_id = str(uuid.uuid4())[:8]
    
    # 1. Get Profile (Default to 1)
    profile_choice = request.form.get('print_profile', '').strip()
    if not profile_choice:
        profile_choice = "1"

    # 2. Get Color Logic
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
        "name": "Unnamed Order",
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
    
    return render_template('order.html', order=new_order)

@app.route('/check_order', methods=['POST'])
def check_order():
    order_id = request.form.get('order_id', '').strip()
    db = get_db()
    order = next((o for o in db['orders'] if o['id'] == order_id), None)
    if order:
        return render_template('order.html', order=order)
    return "Order not found. Please check your ID and try again.", 404

@app.route('/name_order/<order_id>', methods=['POST'])
def name_order(order_id):
    db = get_db()
    new_name = request.form.get('order_name', '').strip()
    for order in db['orders']:
        if order['id'] == order_id:
            order['name'] = new_name
            save_db(db)
            return render_template('order.html', order=order)
    return "Order not found", 404

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
    active_orders = [o for o in db['orders'] if o.get('status') != 'Delivered']
    current_colors = ", ".join(db.get('settings', {}).get('filaments', []))
    return render_template('dashboard.html', orders=active_orders, current_colors=current_colors)

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
    if not session.get('logged_in'): 
        return redirect(url_for('login'))
    
    # Get the text from the box
    raw_colors = request.form.get('colors_list', '')
    
    # Logic: Split by comma, strip whitespace, and ignore empty entries
    color_list = [c.strip() for c in raw_colors.split(',') if c.strip()]
    
    # Security check: Only save if we actually have colors
    if color_list:
        db = get_db()
        db['settings']['filaments'] = color_list
        save_db(db)
        print(f"Successfully updated colors to: {color_list}")
    else:
        print("Warning: Attempted to save an empty color list. Action blocked.")

    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True)