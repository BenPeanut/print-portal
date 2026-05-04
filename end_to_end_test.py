#!/usr/bin/env python3
"""
End-to-end test for add-to-cart flow
Tests the complete flow from extension POST to database persistence
"""
import sys
import os
import json
import requests
from datetime import datetime, timedelta
from itsdangerous import URLSafeTimedSerializer

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

BASE_URL = "http://127.0.0.1:5000"
TEST_USER_ID = "test_user_12345"
TEST_USERNAME = "testuser"

def generate_extension_token(user_id, username, secret_key):
    """Generate a valid extension auth token"""
    serializer = URLSafeTimedSerializer(secret_key, salt='extension-local-auth-v1')
    payload = {
        'uid': str(user_id).strip(),
        'un': str(username).strip(),
        'iat': datetime.utcnow().isoformat() + 'Z',
    }
    return serializer.dumps(payload)

def test_add_to_cart():
    """Test the complete add-to-cart flow"""
    print("=" * 70)
    print("END-TO-END ADD-TO-CART FLOW TEST")
    print("=" * 70)
    
    # Step 1: Check Flask is running
    print("\n[1] Checking Flask server status...")
    try:
        response = requests.get(f"{BASE_URL}/", timeout=5)
        if response.status_code == 200:
            print("[OK] Flask server is responding")
        else:
            print(f"[ERROR] Flask returned unexpected status: {response.status_code}")
            return False
    except requests.ConnectionError as e:
        print(f"[ERROR] Cannot connect to Flask: {e}")
        return False
    
    # Step 2: Try to get Flask app to generate a token
    print("\n[2] Attempting direct POST to /create_featured_order with extension auth...")
    
    # For a real test, we would need to:
    # 1. Either get the SECRET_KEY from Flask app
    # 2. Or login first to establish session auth
    
    # Let's try approach 2: Login first
    print("\n[3] Attempting to establish session via login...")
    with requests.Session() as session:
        # Try finding the login endpoint
        login_data = {
            'username': TEST_USERNAME,
            'password': 'test_password',
        }
        
        # Try login
        response = session.post(
            f"{BASE_URL}/login",
            data=login_data,
            timeout=10,
            allow_redirects=True
        )
        
        if response.status_code == 200:
            print("[OK] Login successful (or login page shown)")
        else:
            print(f"[WARN] Login returned: {response.status_code}")
            if response.status_code == 404:
                print("   (Login endpoint might not exist at /login)")
        
        # Try register first
        print("\n[4] Attempting to register test user...")
        register_data = {
            'username': TEST_USERNAME,
            'email': f'{TEST_USERNAME}@test.local',
            'password': 'test_password',
            'confirm_password': 'test_password',
        }
        
        response = session.post(
            f"{BASE_URL}/register",
            data=register_data,
            timeout=10,
            allow_redirects=True
        )
        
        print(f"   Register response: {response.status_code}")
        
        # Now try the cart POST
        print("\n[5] Posting order to /create_featured_order...")
        order_data = {
            "title": "Test 3D Print Model",
            "makerworld_link": "https://makerworld.com/en/models/test-model",
            "price": "25.99",
            "profile": "Standard",
            "filament": "PLA: Red",
        }
        
        headers = {"Content-Type": "application/json"}
        response = session.post(
            f"{BASE_URL}/create_featured_order",
            json=order_data,
            headers=headers,
            timeout=10
        )
        
        print(f"   Response status: {response.status_code}")
        print(f"   Response text: {response.text[:200]}")
        
        if response.status_code == 200:
            try:
                result = response.json()
                order_id = result.get('order_id')
                print(f"\n[OK] SUCCESS! Order created with ID: {order_id}")
                
                # Step 6: Verify order in cart
                print("\n[6] Verifying order is accessible via /cart/orders...")
                response = session.get(
                    f"{BASE_URL}/cart/orders",
                    timeout=10
                )
                
                if response.status_code == 200:
                    cart_data = response.json()
                    items = cart_data.get('items', [])
                    print(f"[OK] Cart has {len(items)} items")
                    for item in items:
                        print(f"  - {item.get('product_name')}: {item.get('status')}")
                    return True
                else:
                    print(f"[ERROR] Cart query returned: {response.status_code}")
                    return False
                    
            except Exception as e:
                print(f"[ERROR] Error parsing response: {e}")
                return False
        elif response.status_code == 401:
            print("   [WARN] Not authorized - session not established")
            return False
        elif response.status_code == 400:
            print(f"   [ERROR] Bad request: {response.json()}")
            return False
        else:
            print(f"   [ERROR] Unexpected status code")
            return False

def check_database_directly():
    """Try to check database directly"""
    print("\n[7] Checking database directly...")
    try:
        import psycopg2
        import json as json_module
        
        # Load environment
        from dotenv import load_dotenv
        load_dotenv()
        
        db_url = os.getenv('DATABASE_URL')
        if not db_url:
            print("   No DATABASE_URL in environment")
            return False
        
        # Parse simple connection string
        # DATABASE_URL format: postgresql://user:password@host:port/dbname
        try:
            conn = psycopg2.connect(db_url)
            cur = conn.cursor()
            
            # Query orders table
            cur.execute("SELECT id, json FROM orders LIMIT 5")
            rows = cur.fetchall()
            
            print(f"   Found {len(rows)} orders in database:")
            for order_id, order_json in rows:
                order = json_module.loads(order_json)
                print(f"     - {order_id}: {order.get('product_name')} ({order.get('status')})")
            
            cur.close()
            conn.close()
            return True
            
        except psycopg2.OperationalError as e:
            print(f"   [WARN] Database connection failed: {e}")
            return False
            
    except ImportError:
        print("   [WARN] psycopg2 not available")
        return False

if __name__ == "__main__":
    print("Starting end-to-end test...")
    print(f"Target: {BASE_URL}\n")
    
    success = test_add_to_cart()
    check_database_directly()
    
    print("\n" + "=" * 70)
    if success:
        print("TEST PASSED: Cart flow working end-to-end")
    else:
        print("TEST: Could not complete (may need session/auth setup)")
    print("=" * 70)
    
    sys.exit(0 if success else 1)
