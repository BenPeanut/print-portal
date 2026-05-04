#!/usr/bin/env python3
"""
Test cart endpoint with proper extension auth token generation
"""
import os
import sys
import json
import requests
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer
from urllib.parse import urlencode
from dotenv import load_dotenv

# Load environment
load_dotenv()

BASE_URL = "http://127.0.0.1:5000"
TEST_USER_ID = "test_user_ext_123"
TEST_USERNAME = "test_ext_user"

def generate_extension_token(user_id, username):
    """Generate a valid extension auth token using Flask's secret key"""
    secret_key = os.getenv('SECRET_KEY')
    if not secret_key:
        print("[ERROR] SECRET_KEY not found in environment")
        return None
    
    serializer = URLSafeTimedSerializer(secret_key, salt='extension-local-auth-v1')
    payload = {
        'uid': str(user_id).strip(),
        'un': str(username).strip(),
        'iat': datetime.utcnow().isoformat() + 'Z',
    }
    try:
        token = serializer.dumps(payload)
        print(f"[OK] Generated auth token: {token[:20]}...")
        return token
    except Exception as e:
        print(f"[ERROR] Failed to generate token: {e}")
        return None

def test_with_token_auth():
    """Test add-to-cart with extension auth token"""
    print("\n" + "="*70)
    print("TESTING ADD-TO-CART WITH EXTENSION AUTH TOKEN")
    print("="*70)
    
    # Generate auth token
    print("\n[1] Generating extension auth token...")
    token = generate_extension_token(TEST_USER_ID, TEST_USERNAME)
    if not token:
        return False
    
    # Prepare order data
    print("\n[2] Preparing order data...")
    order_data = {
        "title": "Awesome 3D Printed Vase",
        "makerworld_link": "https://makerworld.com/en/models/vase-0/files/object-0",
        "price": "18.50",
        "profile": "Standard",
        "filament": "PLA: Translucent Green",
        "ext_auth": token,  # Include token in payload
    }
    
    print(f"   Title: {order_data['title']}")
    print(f"   Link: {order_data['makerworld_link']}")
    print(f"   Price: ${order_data['price']}")
    
    # Make request
    print("\n[3] POSTing to /create_featured_order with auth...")
    headers = {"Content-Type": "application/json"}
    
    try:
        response = requests.post(
            f"{BASE_URL}/create_featured_order",
            json=order_data,
            headers=headers,
            timeout=10
        )
        
        print(f"   Response status: {response.status_code}")
        
        if response.status_code == 200:
            try:
                result = response.json()
                order_id = result.get('order_id')
                print(f"\n[OK] ORDER CREATED! ID: {order_id}")
                
                # Step 4: Verify via /cart/orders
                print("\n[4] Verifying order via backend API...")
                
                # Create a session with the token
                session = requests.Session()
                
                # Try to get cart orders with token
                cart_response = session.get(
                    f"{BASE_URL}/cart/orders?ext_auth={token}",
                    timeout=10
                )
                
                if cart_response.status_code == 200:
                    cart_data = cart_response.json()
                    if cart_data.get('ok'):
                        items = cart_data.get('items', [])
                        print(f"[OK] Found {len(items)} cart items:")
                        for item in items:
                            print(f"     - {item.get('product_name')} (Status: {item.get('status')})")
                        return True
                    else:
                        error = cart_data.get('error', 'Unknown error')
                        print(f"[ERROR] Cart query failed: {error}")
                        return False
                else:
                    print(f"[ERROR] Cart query returned {cart_response.status_code}")
                    print(f"   Response: {cart_response.text[:200]}")
                    return False
                    
            except Exception as e:
                print(f"[ERROR] Failed parsing response: {e}")
                print(f"   Response text: {response.text[:200]}")
                return False
        
        elif response.status_code == 401:
            print("[ERROR] Received 401 Unauthorized")
            print(f"   Response: {response.text}")
            return False
        
        elif response.status_code == 400:
            print("[ERROR] Received 400 Bad Request")
            try:
                error_data = response.json()
                print(f"   Error: {error_data.get('error', 'Unknown')}")
            except:
                print(f"   Response: {response.text}")
            return False
            
        else:
            print(f"[ERROR] Unexpected status: {response.status_code}")
            print(f"   Response: {response.text[:300]}")
            return False
    
    except requests.Timeout:
        print("[ERROR] Request timed out")
        return False
    except Exception as e:
        print(f"[ERROR] Request failed: {e}")
        return False

if __name__ == "__main__":
    print("Starting authenticated cart test...")
    print(f"Target: {BASE_URL}")
    print(f"Test user: {TEST_USER_ID} / {TEST_USERNAME}\n")
    
    success = test_with_token_auth()
    
    print("\n" + "="*70)
    if success:
        print("TEST PASSED: Add-to-cart working with proper auth!")
    else:
        print("TEST FAILED: Check error messages above")
    print("="*70)
    
    sys.exit(0 if success else 1)
