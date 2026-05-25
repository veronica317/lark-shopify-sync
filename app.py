"""
Lark Base <-> Shopify Two-Way Inventory Sync
=============================================
Direction 1: Lark Base -> Shopify
  - Triggered by Lark Base Automation when "Available Stock" changes
  - Finds matching SKU in Shopify and sets inventory level

Direction 2: Shopify -> Lark Base
  - Triggered by Shopify order created webhook
  - Deducts ordered quantities from Lark Base

Direction 3: Shopify Order Cancelled -> Lark Base
  - Restores cancelled quantities back to Lark Base
"""

import os
import json
import hmac
import time
import hashlib
import base64
import logging
import requests
from flask import Flask, request, jsonify, redirect

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
SHOPIFY_STORE_URL      = os.environ.get("SHOPIFY_STORE_URL", "")       # e.g. yourstore.myshopify.com
SHOPIFY_CLIENT_ID      = os.environ.get("SHOPIFY_CLIENT_ID", "")       # Dev Dashboard Client ID
SHOPIFY_CLIENT_SECRET  = os.environ.get("SHOPIFY_CLIENT_SECRET", "")   # Dev Dashboard Secret
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")  # Shopify webhook signing secret
LARK_APP_ID            = os.environ.get("LARK_APP_ID", "")             # From open.larksuite.com
LARK_APP_SECRET        = os.environ.get("LARK_APP_SECRET", "")         # From open.larksuite.com
LARK_BASE_ID           = os.environ.get("LARK_BASE_ID", "")            # From Lark Base URL
LARK_TABLE_ID          = os.environ.get("LARK_TABLE_ID", "")           # From Lark Base URL
WEBHOOK_SECRET         = os.environ.get("WEBHOOK_SECRET", "")          # Your secret for Lark automation
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY TOKEN (short-lived, auto-refreshed)
# ══════════════════════════════════════════════════════════════════════════════

_token_cache = {"token": None, "expires_at": 0}

def get_shopify_token():
    """
    Exchange Client ID + Secret for a short-lived Shopify access token.
    Tokens expire every 24h. Cached and auto-refreshed.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    url = f"https://{SHOPIFY_STORE_URL}/admin/oauth/access_token"
    resp = requests.post(url, json={
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }, headers={"Content-Type": "application/json"})

    data = resp.json()
    token = data.get("access_token")
    expires_in = data.get("expires_in", 86400)

    if not token:
        raise Exception(f"Shopify token fetch failed: {data}")

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in
    logger.info(f"[Shopify] New token fetched, expires in {expires_in}s")
    return token


def shopify_headers():
    return {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }


# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY: Find variant by SKU (also returns current stock)
# ══════════════════════════════════════════════════════════════════════════════

def get_shopify_variant(sku):
    """
    Find a Shopify product variant by SKU using REST API.
    Returns dict with inventory_item_id, location_id, current_qty, or None.
    """
    # Step 1: Search for variant by SKU via REST
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/variants.json"
    params = {"fields": "id,sku,inventory_item_id", "limit": 250}
    
    # Shopify REST doesn't support SKU search directly, use product search
    search_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/products.json"
    search_params = {"fields": "id,variants", "limit": 250}
    
    resp = requests.get(search_url, params=search_params, headers=shopify_headers())
    data = resp.json()
    logger.info(f"[Shopify] Products REST status: {resp.status_code}")

    inventory_item_id = None
    for product in data.get("products", []):
        for variant in product.get("variants", []):
            if variant.get("sku") == sku:
                inventory_item_id = str(variant["inventory_item_id"])
                logger.info(f"[Shopify] Found SKU={sku}, inventory_item_id={inventory_item_id}")
                break
        if inventory_item_id:
            break

    if not inventory_item_id:
        logger.info(f"[Shopify] SKU '{sku}' not found in any product variant")
        return None

    # Step 2: Get inventory levels for this item
    levels_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels.json"
    levels_resp = requests.get(levels_url, params={"inventory_item_ids": inventory_item_id}, headers=shopify_headers())
    levels_data = levels_resp.json()
    logger.info(f"[Shopify] Inventory levels: {json.dumps(levels_data)}")

    levels = levels_data.get("inventory_levels", [])
    if not levels:
        logger.warning(f"[Shopify] No inventory levels for item {inventory_item_id}")
        return None

    level = levels[0]
    result = {
        "inventory_item_id": inventory_item_id,
        "location_id":       str(level["location_id"]),
        "current_qty":       int(level.get("available") or 0)
    }
    logger.info(f"[Shopify] current_qty={result['current_qty']}, location={result['location_id']}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY: Update inventory using adjustment
# ══════════════════════════════════════════════════════════════════════════════

def set_shopify_inventory(inventory_item_id, location_id, target_qty, current_qty):
    """
    Adjust Shopify inventory so it equals target_qty.
    Uses adjust endpoint with delta = target - current.
    """
    adjustment = int(target_qty) - int(current_qty)
    logger.info(f"[Shopify] current={current_qty}, target={target_qty}, adjustment={adjustment:+d}")

    if adjustment == 0:
        logger.info("[Shopify] No adjustment needed")
        return True

    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels/adjust.json"
    payload = {
        "location_id":          int(location_id),
        "inventory_item_id":    int(inventory_item_id),
        "available_adjustment": adjustment
    }
    resp = requests.post(url, json=payload, headers=shopify_headers())
    if resp.status_code == 200:
        logger.info(f"[Shopify] ✅ Inventory set to {target_qty} (adj {adjustment:+d})")
        return True
    else:
        logger.error(f"[Shopify] ❌ Failed: {resp.status_code} {resp.text}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# LARK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_lark_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Lark auth failed: {data}")
    return data["tenant_access_token"]


def find_lark_record(token, sku):
    """Find a Lark Base record by SKU. Returns (record_id, current_stock) or (None, None)."""
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_ID}/tables/{LARK_TABLE_ID}/records/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [{"field_name": "Variant SKU", "operator": "is", "value": [sku]}]
        }
    }
    resp = requests.post(url, json=payload, headers=headers)
    data = resp.json()
    items = data.get("data", {}).get("items", [])
    if not items:
        logger.info(f"[Lark] SKU '{sku}' not found in Lark Base")
        return None, None
    record = items[0]
    stock = record.get("fields", {}).get("Available Stock", 0)
    logger.info(f"[Lark] Found SKU={sku}, stock={stock}")
    return record["record_id"], stock


def update_lark_record(token, record_id, new_qty):
    """Update Available Stock field in a Lark Base record."""
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_ID}/tables/{LARK_TABLE_ID}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.put(url, json={"fields": {"Available Stock": new_qty}}, headers=headers)
    if resp.status_code == 200:
        logger.info(f"[Lark] ✅ Record {record_id} updated to {new_qty}")
        return True
    else:
        logger.error(f"[Lark] ❌ Failed: {resp.status_code} {resp.text}")
        return False


def verify_shopify_hmac(raw_body, hmac_header):
    if not SHOPIFY_WEBHOOK_SECRET:
        return True
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), hmac_header or "")


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE 1: Lark Base -> Shopify
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/sync/lark-to-shopify", methods=["POST"])
def lark_to_shopify():
    try:
        data = request.get_json(force=True)
        logger.info(f"[Lark->Shopify] {json.dumps(data)}")

        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        sku = str(data.get("sku", "")).strip()
        stock = data.get("available_stock")

        if not sku:
            return jsonify({"status": "error", "message": "Missing SKU"}), 400
        if stock is None:
            return jsonify({"status": "error", "message": "Missing available_stock"}), 400

        target_qty = int(float(str(stock)))
        logger.info(f"[Lark->Shopify] SKU={sku}, target_qty={target_qty}")

        variant = get_shopify_variant(sku)
        if not variant:
            return jsonify({"status": "skipped", "message": f"SKU '{sku}' not in Shopify"}), 200

        success = set_shopify_inventory(
            variant["inventory_item_id"],
            variant["location_id"],
            target_qty,
            variant["current_qty"]
        )

        return jsonify({
            "status": "success" if success else "error",
            "sku": sku,
            "target_qty": target_qty,
            "previous_qty": variant["current_qty"]
        }), 200 if success else 500

    except Exception as e:
        logger.error(f"[Lark->Shopify] Error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE 2: Shopify Order Created -> Lark Base (deduct stock)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/sync/shopify-to-lark", methods=["POST"])
def shopify_to_lark():
    try:
        raw_body = request.get_data()
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
        if not verify_shopify_hmac(raw_body, hmac_header):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        order = json.loads(raw_body)
        logger.info(f"[Shopify->Lark] Order #{order.get('order_number')} created")

        token = get_lark_token()
        results = []

        for item in order.get("line_items", []):
            sku = str(item.get("sku", "")).strip()
            qty = int(item.get("quantity", 0))
            if not sku:
                continue

            record_id, current_stock = find_lark_record(token, sku)
            if not record_id:
                results.append({"sku": sku, "status": "skipped"})
                continue

            new_stock = max(0, int(current_stock or 0) - qty)
            success = update_lark_record(token, record_id, new_stock)
            results.append({"sku": sku, "old": current_stock, "new": new_stock, "status": "success" if success else "error"})

        return jsonify({"status": "success", "results": results}), 200

    except Exception as e:
        logger.error(f"[Shopify->Lark] Error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE 3: Shopify Order Cancelled -> Lark Base (restore stock)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/sync/shopify-order-cancelled", methods=["POST"])
def shopify_order_cancelled():
    try:
        raw_body = request.get_data()
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
        if not verify_shopify_hmac(raw_body, hmac_header):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        order = json.loads(raw_body)
        logger.info(f"[Shopify Cancelled] Order #{order.get('order_number')} cancelled")

        token = get_lark_token()
        results = []

        for item in order.get("line_items", []):
            sku = str(item.get("sku", "")).strip()
            qty = int(item.get("quantity", 0))
            if not sku:
                continue

            record_id, current_stock = find_lark_record(token, sku)
            if not record_id:
                results.append({"sku": sku, "status": "skipped"})
                continue

            new_stock = int(current_stock or 0) + qty
            success = update_lark_record(token, record_id, new_stock)
            results.append({"sku": sku, "old": current_stock, "new": new_stock, "status": "success" if success else "error"})

        return jsonify({"status": "success", "results": results}), 200

    except Exception as e:
        logger.error(f"[Shopify Cancelled] Error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# DEBUG ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/debug/shopify/<sku>", methods=["GET"])
def debug_shopify(sku):
    try:
        variant = get_shopify_variant(sku)
        return jsonify({"sku": sku, "variant": variant}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/token", methods=["GET"])
def debug_token():
    try:
        token = get_shopify_token()
        return jsonify({
            "token_prefix": token[:10] + "...",
            "token_length": len(token),
            "expires_at": _token_cache["expires_at"]
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "lark-shopify-two-way-sync"}), 200


# ══════════════════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
