import os
from flask import Flask, render_template

app = Flask(__name__)

# Fetch keys from Environment Variables (set these in Render later)
BIN_ID = os.getenv('BIN_ID')
API_KEY = os.getenv('API_KEY')

@app.route('/')
def index():
    return render_template('index.html', bin_id=BIN_ID, api_key=API_KEY)

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html', bin_id=BIN_ID, api_key=API_KEY)

@app.route('/orders')
def orders():
    return render_template('order.html', bin_id=BIN_ID, api_key=API_KEY)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)