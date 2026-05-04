#!/usr/bin/env python3
"""
Automated regression test for extension-auth checkout flow.

Flow per iteration:
1) Create a temporary user account via /user_register
2) Login via /extension-api/user-login to obtain extension_auth_token
3) Save a cart item via /cart/save-item
4) Fetch /cart/orders and verify item exists for that user
5) Checkout via /checkout?response_mode=json using extension token
6) Verify checkout succeeded and source cart item is removed
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
from typing import Dict, List

import requests


BASE_URL = "http://127.0.0.1:5000"

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


def _rand_suffix(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def _assert_ok(resp: requests.Response, expected_status: int, context: str) -> None:
    if resp.status_code != expected_status:
        body = resp.text[:1000]
        raise AssertionError(
            f"{context}: expected HTTP {expected_status}, got {resp.status_code}. Body: {body}"
        )


def _create_temp_user(session: requests.Session, iteration: int) -> Dict[str, str]:
    username = f"tmp_ext_{int(time.time())}_{iteration}_{_rand_suffix(5)}"
    password = f"P@ss_{_rand_suffix(10)}"

    resp = session.post(
        f"{BASE_URL}/user_register",
        data={"username": username, "password": password},
        allow_redirects=True,
        timeout=20,
    )
    _assert_ok(resp, 200, "user_register")
    return {"username": username, "password": password}


def _extension_login(session: requests.Session, username: str, password: str) -> Dict[str, str]:
    resp = session.post(
        f"{BASE_URL}/extension-api/user-login",
        json={"username": username, "password": password},
        timeout=20,
    )
    _assert_ok(resp, 200, "extension-api/user-login")
    data = resp.json()
    if not data.get("ok"):
        raise AssertionError(f"extension-api/user-login returned ok=false: {data}")
    token = str(data.get("extension_auth_token") or "").strip()
    user_id = str(data.get("user_id") or "").strip()
    if not token or not user_id:
        raise AssertionError(f"Missing token/user_id in extension login response: {data}")
    return {"token": token, "user_id": user_id}


def _save_cart_item(session: requests.Session, token: str, iteration: int) -> str:
    payload = {
        "id": f"tmp-cart-item-{iteration}-{_rand_suffix(4)}",
        "displayName": f"Regression Test Model {iteration}",
        "link": f"https://makerworld.com/en/models/{_rand_suffix(10)}",
        "singleFilament": "PLA Gray",
        "colorMode": "single",
        "profile": "Standard",
        "weight": 42,
        "estimatedPrice": 125000,
        "quantity": 1,
    }
    resp = session.post(
        f"{BASE_URL}/cart/save-item?ext_auth={token}",
        json=payload,
        timeout=20,
    )
    _assert_ok(resp, 200, "cart/save-item")
    data = resp.json()
    if not data.get("ok"):
        raise AssertionError(f"cart/save-item returned ok=false: {data}")
    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        raise AssertionError(f"cart/save-item missing order_id: {data}")
    return order_id


def _fetch_cart_orders(session: requests.Session, token: str) -> List[dict]:
    resp = session.get(f"{BASE_URL}/cart/orders?ext_auth={token}", timeout=20)
    _assert_ok(resp, 200, "cart/orders")
    data = resp.json()
    if not data.get("ok"):
        raise AssertionError(f"cart/orders returned ok=false: {data}")
    items = data.get("items")
    if not isinstance(items, list):
        raise AssertionError(f"cart/orders missing items list: {data}")
    return items


def _fetch_order_page(session: requests.Session, order_id: str) -> str:
    resp = session.get(f"{BASE_URL}/order/{order_id}", timeout=20)
    _assert_ok(resp, 200, "order page")
    return resp.text


def _fetch_user_history_page(session: requests.Session) -> str:
    resp = session.get(f"{BASE_URL}/history", timeout=20)
    _assert_ok(resp, 200, "user history")
    return resp.text


def _fetch_admin_dashboard_page(order_id: str) -> str:
    admin_password = str(os.getenv("ADMIN_PASSWORD") or "").strip()
    if not admin_password:
        raise AssertionError("ADMIN_PASSWORD is required in environment for admin dashboard verification")

    with requests.Session() as admin_session:
        login_resp = admin_session.post(
            f"{BASE_URL}/login",
            data={"password": admin_password, "next": "/dashboard"},
            allow_redirects=True,
            timeout=20,
        )
        _assert_ok(login_resp, 200, "admin login")

        dashboard_resp = admin_session.get(f"{BASE_URL}/dashboard", timeout=20)
        _assert_ok(dashboard_resp, 200, "admin dashboard")
        if order_id not in dashboard_resp.text:
            raise AssertionError(f"Admin dashboard did not contain transitioned order id {order_id}")
        return dashboard_resp.text


def _checkout(session: requests.Session, token: str, items: List[dict]) -> Dict[str, object]:
    selected_items = []
    for item in items:
        oid = str(item.get("id") or "").strip()
        if not oid:
            continue
        qty = int(item.get("quantity") or 1)
        unit_price = float(item.get("print_price") or 0) / max(qty, 1)
        selected_items.append(
            {
                "id": oid,
                "orderId": oid,
                "quantity": qty,
                "estimatedPrice": unit_price,
                "displayName": item.get("product_name") or item.get("name") or "Model",
                "profile": item.get("profile") or "",
                "singleFilament": str(item.get("color") or ""),
                "colorMode": "multi" if "|" in str(item.get("color") or "") else "single",
                "weight": float(item.get("print_weight_g") or 0) / max(qty, 1),
                "link": item.get("link") or "",
            }
        )

    if not selected_items:
        raise AssertionError("No selectable items built for checkout")

    resp = session.post(
        f"{BASE_URL}/checkout?response_mode=json&ext_auth={token}",
        json={"items": selected_items},
        timeout=20,
    )
    _assert_ok(resp, 200, "checkout")
    data = resp.json()
    if not data.get("ok"):
        raise AssertionError(f"checkout returned ok=false: {data}")
    return data


def _checkout_with_stale_id(session: requests.Session, token: str, item: dict) -> Dict[str, object]:
    qty = int(item.get("quantity") or 1)
    unit_price = float(item.get("print_price") or 0) / max(qty, 1)
    selected_items = [
        {
            "id": f"stale-ui-id-{_rand_suffix(6)}",
            "orderId": f"stale-order-id-{_rand_suffix(6)}",
            "quantity": qty,
            "estimatedPrice": unit_price,
            "displayName": item.get("product_name") or item.get("name") or "Model",
            "profile": item.get("profile") or "",
            "singleFilament": str(item.get("color") or ""),
            "colorMode": "multi" if "|" in str(item.get("color") or "") else "single",
            "weight": float(item.get("print_weight_g") or 0) / max(qty, 1),
            "link": item.get("link") or "",
        }
    ]

    resp = session.post(
        f"{BASE_URL}/checkout?response_mode=json&ext_auth={token}",
        json={"items": selected_items},
        timeout=20,
    )
    _assert_ok(resp, 200, "checkout(stale-id)")
    data = resp.json()
    if not data.get("ok"):
        raise AssertionError(f"checkout(stale-id) returned ok=false: {data}")
    return data


def run_iteration(iteration: int) -> Dict[str, object]:
    with requests.Session() as session:
        user = _create_temp_user(session, iteration)
        auth = _extension_login(session, user["username"], user["password"])
        order_id = _save_cart_item(session, auth["token"], iteration)

        before = _fetch_cart_orders(session, auth["token"])
        before_ids = {str(o.get("id") or "") for o in before}
        if order_id not in before_ids:
            raise AssertionError(f"Saved order_id {order_id} not present in cart/orders: {sorted(before_ids)}")

        checkout_data = _checkout(session, auth["token"], before)
        checkout_ids = [str(x or "").strip() for x in (checkout_data.get("order_ids") or [])]
        if order_id not in checkout_ids:
            raise AssertionError(
                f"Checkout should return original order id after transition. expected {order_id}, got {checkout_ids}"
            )

        after = _fetch_cart_orders(session, auth["token"])
        after_ids = {str(o.get("id") or "") for o in after}
        if order_id in after_ids:
            raise AssertionError(f"Source order {order_id} still appears in cart after pending transition")

        order_page = _fetch_order_page(session, order_id)
        if "Pending" not in order_page:
            raise AssertionError(f"Order page for {order_id} does not show Pending status")

        history_page = _fetch_user_history_page(session)
        if order_id not in history_page:
            raise AssertionError(f"User history page does not include transitioned order id {order_id}")

        admin_dashboard_page = _fetch_admin_dashboard_page(order_id)
        if "Pending" not in admin_dashboard_page:
            raise AssertionError("Admin dashboard is missing Pending status text")

        # Add a second item and intentionally checkout with a stale orderId to verify fallback matching.
        stale_source_order_id = _save_cart_item(session, auth["token"], iteration + 10000)
        mid = _fetch_cart_orders(session, auth["token"])
        stale_source_item = next((o for o in mid if str(o.get("id") or "") == stale_source_order_id), None)
        if stale_source_item is None:
            raise AssertionError(f"Could not find stale test order {stale_source_order_id} before stale-id checkout")

        stale_checkout_data = _checkout_with_stale_id(session, auth["token"], stale_source_item)
        stale_checkout_ids = [str(x or "").strip() for x in (stale_checkout_data.get("order_ids") or [])]
        if stale_source_order_id not in stale_checkout_ids:
            raise AssertionError(
                f"Stale-id checkout should transition original source order id. expected {stale_source_order_id}, got {stale_checkout_ids}"
            )

        final_after = _fetch_cart_orders(session, auth["token"])
        final_after_ids = {str(o.get("id") or "") for o in final_after}
        if stale_source_order_id in final_after_ids:
            raise AssertionError(f"Stale-id source order {stale_source_order_id} still appears in cart after transition")

        stale_order_page = _fetch_order_page(session, stale_source_order_id)
        if "Pending" not in stale_order_page:
            raise AssertionError(f"Stale-id transitioned order {stale_source_order_id} does not show Pending")

        return {
            "username": user["username"],
            "user_id": auth["user_id"],
            "source_order_id": order_id,
            "checkout_order_ids": checkout_ids,
            "checkout_count": int(checkout_data.get("count") or 0),
            "cart_before": len(before),
            "cart_after": len(after),
            "stale_source_order_id": stale_source_order_id,
            "stale_checkout_order_ids": stale_checkout_ids,
            "cart_final_after": len(final_after),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run extension checkout regression loop")
    parser.add_argument("--loops", type=int, default=3, help="How many full iterations to run")
    args = parser.parse_args()

    try:
        health = requests.get(f"{BASE_URL}/api/health", timeout=10)
    except Exception as exc:
        print(f"[FAIL] Cannot reach backend at {BASE_URL}: {exc}")
        return 1

    if health.status_code != 200:
        print(f"[FAIL] Backend health check failed with HTTP {health.status_code}: {health.text[:300]}")
        return 1

    print(f"[INFO] Backend reachable at {BASE_URL}. Running {args.loops} iteration(s)...")
    for i in range(1, args.loops + 1):
        print(f"\n[RUN {i}] Starting")
        result = run_iteration(i)
        print(
            "[RUN {i}] PASS user={user} user_id={uid} source_order={sid} checkout_ids={cids} "
            "stale_source={ssid} stale_checkout_ids={scids} cart_before={cb} cart_after={ca} cart_final_after={cfa}".format(
                i=i,
                user=result["username"],
                uid=result["user_id"],
                sid=result["source_order_id"],
                cids=result["checkout_order_ids"],
                ssid=result["stale_source_order_id"],
                scids=result["stale_checkout_order_ids"],
                cb=result["cart_before"],
                ca=result["cart_after"],
                cfa=result["cart_final_after"],
            )
        )

    print("\n[PASS] All regression iterations succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
