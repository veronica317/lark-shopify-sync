"""
Lark Base ↔ Shopify Two-Way Inventory Sync
===========================================
Direction 1: Lark Base → Shopify
  - Triggered by Lark Base Automation when "Available Stock" changes
  - Finds matching SKU in Shopify and updates inventory level

Direction 2: Shopify → Lark Base
  - Triggered by Shopify order/fulfillment webhook
  - Finds matching SKU in Lark Base and updates "Available Stock"

Deploy on Render.com (free tier)
"""

import os
import json
import hmac
import hashlib
import base64
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG (set these as Environment Variables on Render) ─────────────────────
SHOPIFY_STORE_URL     = os.environ.get("SHOPIFY_STORE_URL")         # e.g. yourstore.myshopify.com
SHOPIFY_ACCESS_TOKEN  = os.environ.get("SHOPIFY_ACCESS_TOKEN")      # Admin API access token (shpat_) OR leave blank if using Client ID/Secret
SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID")         # Dev Dashboard Client ID
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")     # Dev Dashboard Client Secret
SHOPIFY_WEBHOOK_SECRET= os.environ.get("SHOPIFY_WEBHOOK_SECRET")    # Shopify webhook signing secret
LARK_APP_ID           = os.environ.get("LARK_APP_ID")               # From open.larksuite.com
LARK_APP_SECRET       = os.environ.get("LARK_APP_SECRET")           # From open.larksuite.com
LARK_BASE_ID          = os.environ.get("LARK_BASE_ID")              # From your Lark Base URL
LARK_TABLE_ID         = os.environ.get("LARK_TABLE_ID")             # From your Lark Base URL
WEBHOOK_SECRET        = os.environ.get("WEBHOOK_SECRET", "")        # Your own secret for Lark→Shopify
# ───────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# LARK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_lark_access_token():
    """Get a tenant access token from Lark API."""
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": LARK_APP_ID,
        "app_secret": LARK_APP_SECRET
    })
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Lark auth failed: {data}")
    return data["tenant_access_token"]


def find_lark_record_by_sku(token, sku):
    """
    Search Lark Base for a record matching the given SKU.
    Returns (record_id, current_stock) or (None, None).
    """
    url = (
        f"https://open.larksuite.com/open-apis/bitable/v1/"
        f"apps/{LARK_BASE_ID}/tables/{LARK_TABLE_ID}/records/search"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": "Variant SKU",
                    "operator": "is",
                    "value": [sku]
                }
            ]
        }
    }
    resp = requests.post(url, json=payload, headers=headers)
    data = resp.json()

    items = data.get("data", {}).get("items", [])
    if not items:
        logger.info(f"SKU '{sku}' not found in Lark Base — skipping.")
        return None, None

    record = items[0]
    record_id = record["record_id"]
    current_stock = record.get("fields", {}).get("Available Stock", 0)
    return record_id, current_stock


def update_lark_stock(token, record_id, new_quantity):
    """Update the Available Stock field in a Lark Base record."""
    url = (
        f"https://open.larksuite.com/open-apis/bitable/v1/"
        f"apps/{LARK_BASE_ID}/tables/{LARK_TABLE_ID}/records/{record_id}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "fields": {
            "Available Stock": new_quantity
        }
    }
    resp = requests.put(url, json=payload, headers=headers)
    if resp.status_code == 200:
        logger.info(f"✅ Lark Base updated: record {record_id} → {new_quantity} units")
        return True
    else:
        logger.error(f"❌ Lark Base update failed: {resp.status_code} {resp.text}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_shopify_headers():
    """
    Build Shopify API headers.
    Uses X-Shopify-Access-Token with the Client Secret directly.
    This works for Dev Dashboard apps on Shopify.
    """
    return {
        "X-Shopify-Access-Token": SHOPIFY_CLIENT_SECRET,
        "Content-Type": "application/json"
    }


def get_shopify_token():
    """Returns the client secret used as access token for Dev Dashboard apps."""
    return SHOPIFY_CLIENT_SECRET or ""


def get_shopify_variant_by_sku(sku):
    """
    Search Shopify for a product variant matching the given SKU.
    Returns dict with variant_id, inventory_item_id, location_id or None.
    """
    headers = get_shopify_headers()
    graphql_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/graphql.json"
    query = """
    query getVariantBySku($query: String!) {
      productVariants(first: 10, query: $query) {
        edges {
          node {
            id
            sku
            inventoryItem {
              id
              inventoryLevels(first: 1) {
                edges {
                  node {
                    id
                    location { id }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    gql_variables = {"query": f"sku:{sku}"}

    resp = requests.post(graphql_url, json={"query": query, "variables": gql_variables}, headers=headers)
    data = resp.json()
    logger.info(f"[Shopify GraphQL Response]: {json.dumps(data)}")

    edges = data.get("data", {}).get("productVariants", {}).get("edges", [])
    for edge in edges:
        node = edge["node"]
        if node.get("sku") == sku:
            inv_item = node["inventoryItem"]
            inv_levels = inv_item["inventoryLevels"]["edges"]
            if not inv_levels:
                logger.warning(f"SKU {sku} found but has no inventory levels.")
                return None
            return {
                "variant_id":        node["id"].split("/")[-1],
                "inventory_item_id": inv_item["id"].split("/")[-1],
                "location_id":       inv_levels[0]["node"]["location"]["id"].split("/")[-1]
            }

    logger.info(f"SKU '{sku}' not found in Shopify — skipping.")
    return None


def get_shopify_current_quantity(inventory_item_id, location_id):
    """Get the current inventory quantity from Shopify."""
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels.json"
    params = {
        "inventory_item_ids": inventory_item_id,
        "location_ids": location_id
    }
    resp = requests.get(url, headers=get_shopify_headers(), params=params)
    data = resp.json()
    levels = data.get("inventory_levels", [])
    if levels:
        return int(levels[0].get("available") or 0)
    return 0


def update_shopify_inventory(inventory_item_id, location_id, new_quantity):
    """
    Set the inventory level using adjustment.
    Calculates the difference between desired and current quantity.
    """
    # Get current quantity first
    current_qty = get_shopify_current_quantity(inventory_item_id, location_id)
    adjustment = int(new_quantity) - current_qty

    logger.info(f"[Shopify] Current: {current_qty}, Target: {new_quantity}, Adjustment: {adjustment}")

    if adjustment == 0:
        logger.info(f"[Shopify] No change needed, skipping.")
        return True

    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels/adjust.json"
    payload = {
        "location_id":            int(location_id),
        "inventory_item_id":      int(inventory_item_id),
        "available_adjustment":   adjustment
    }
    resp = requests.post(url, json=payload, headers=get_shopify_headers())
    if resp.status_code == 200:
        logger.info(f"✅ Shopify updated: item {inventory_item_id} → {new_quantity} units (adj: {adjustment:+d})")
        return True
    else:
        logger.error(f"❌ Shopify update failed: {resp.status_code} {resp.text}")
        return False


def verify_shopify_webhook(data, hmac_header):
    """Verify that the webhook actually came from Shopify."""
    if not SHOPIFY_WEBHOOK_SECRET:
        return True  # Skip verification if secret not set
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        data,
        hashlib.sha256
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header or "")


def get_shopify_inventory_level(inventory_item_id, location_id):
    """Get current inventory level from Shopify for a given item and location."""
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels.json"
    headers = get_shopify_headers()
    params = {
        "inventory_item_ids": inventory_item_id,
        "location_ids": location_id
    }
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json()
    levels = data.get("inventory_levels", [])
    if levels:
        return levels[0].get("available", 0)
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# DIRECTION 1: Lark Base → Shopify
# Endpoint called by Lark Base Automation
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/sync/lark-to-shopify", methods=["POST"])
def lark_to_shopify():
    """
    Receives webhook from Lark Base Automation when Available Stock changes.

    Expected JSON body (set in Lark Automation → HTTP Request action):
    {
        "secret": "your_webhook_secret",
        "sku": "{{Variant SKU}}",
        "available_stock": "{{Available Stock}}"
    }
    """
    try:
        data = request.get_json(force=True)
        logger.info(f"[Lark→Shopify] Received: {json.dumps(data)}")

        # Validate secret
        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        sku             = str(data.get("sku", "")).strip()
        available_stock = data.get("available_stock")

        if not sku:
            return jsonify({"status": "error", "message": "Missing SKU"}), 400
        if available_stock is None:
            return jsonify({"status": "error", "message": "Missing available_stock"}), 400

        try:
            quantity = int(float(str(available_stock)))
        except ValueError:
            return jsonify({"status": "error", "message": f"Invalid quantity: {available_stock}"}), 400

        logger.info(f"[Lark→Shopify] SKU={sku}, Qty={quantity}")

        variant_info = get_shopify_variant_by_sku(sku)
        if not variant_info:
            return jsonify({"status": "skipped", "message": f"SKU '{sku}' not in Shopify"}), 200

        success = update_shopify_inventory(
            variant_info["inventory_item_id"],
            variant_info["location_id"],
            quantity
        )

        if success:
            return jsonify({"status": "success", "sku": sku, "quantity_set": quantity}), 200
        else:
            return jsonify({"status": "error", "message": "Shopify update failed"}), 500

    except Exception as e:
        logger.error(f"[Lark→Shopify] Error: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# DIRECTION 2: Shopify → Lark Base
# Endpoint called by Shopify order/fulfillment webhook
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/sync/shopify-to-lark", methods=["POST"])
def shopify_to_lark():
    """
    Receives webhook from Shopify when an order is CREATED.
    Deducts ordered quantities from Lark Base Available Stock immediately.

    Register this URL in Shopify Admin:
      Settings → Notifications → Webhooks
      Event: Order created
      URL: https://your-app.onrender.com/sync/shopify-to-lark
    """
    try:
        raw_body    = request.get_data()
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")

        # Verify webhook came from Shopify
        if not verify_shopify_webhook(raw_body, hmac_header):
            logger.warning("[Shopify→Lark] Invalid HMAC — rejected.")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        order = json.loads(raw_body)
        logger.info(f"[Shopify→Lark] Order created — deducting stock")

        # Get Lark access token once for all updates
        token = get_lark_access_token()

        results = []
        line_items = order.get("line_items", [])

        for item in line_items:
            sku      = str(item.get("sku", "")).strip()
            qty_sold = int(item.get("quantity", 0))

            if not sku:
                logger.info(f"[Shopify→Lark] Line item has no SKU, skipping.")
                continue

            logger.info(f"[Shopify→Lark] Processing SKU={sku}, qty_sold={qty_sold}")

            # Find record in Lark Base
            record_id, current_stock = find_lark_record_by_sku(token, sku)
            if not record_id:
                results.append({"sku": sku, "status": "skipped", "reason": "not in Lark Base"})
                continue

            # Deduct sold quantity (never go below 0)
            new_stock = max(0, int(current_stock or 0) - qty_sold)
            logger.info(f"[Shopify→Lark] SKU={sku}: {current_stock} - {qty_sold} = {new_stock}")

            success = update_lark_stock(token, record_id, new_stock)
            results.append({
                "sku":       sku,
                "status":    "success" if success else "error",
                "old_stock": current_stock,
                "new_stock": new_stock
            })

        return jsonify({"status": "success", "results": results}), 200

    except Exception as e:
        logger.error(f"[Shopify→Lark] Error: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# DIRECTION 3: Shopify Order Cancelled → Lark Base (Restore Stock)
# Endpoint called by Shopify order cancelled webhook
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/sync/shopify-order-cancelled", methods=["POST"])
def shopify_order_cancelled():
    """
    Receives webhook from Shopify when an order is CANCELLED.
    Restores the cancelled quantities back to Lark Base Available Stock.

    Register this URL in Shopify Admin:
      Settings → Notifications → Webhooks
      Event: Order cancelled
      URL: https://your-app.onrender.com/sync/shopify-order-cancelled
    """
    try:
        raw_body    = request.get_data()
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")

        # Verify webhook came from Shopify
        if not verify_shopify_webhook(raw_body, hmac_header):
            logger.warning("[Shopify→Lark Cancelled] Invalid HMAC — rejected.")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        order = json.loads(raw_body)
        order_number = order.get("order_number")
        logger.info(f"[Shopify→Lark] Order #{order_number} cancelled — restoring stock")

        # Get Lark access token once for all updates
        token = get_lark_access_token()

        results = []
        line_items = order.get("line_items", [])

        for item in line_items:
            sku          = str(item.get("sku", "")).strip()
            qty_cancelled = int(item.get("quantity", 0))

            if not sku:
                logger.info(f"[Shopify→Lark Cancelled] Line item has no SKU, skipping.")
                continue

            logger.info(f"[Shopify→Lark Cancelled] Restoring SKU={sku}, qty={qty_cancelled}")

            # Find record in Lark Base
            record_id, current_stock = find_lark_record_by_sku(token, sku)
            if not record_id:
                results.append({"sku": sku, "status": "skipped", "reason": "not in Lark Base"})
                continue

            # Add back the cancelled quantity
            new_stock = int(current_stock or 0) + qty_cancelled
            logger.info(f"[Shopify→Lark Cancelled] SKU={sku}: {current_stock} + {qty_cancelled} = {new_stock}")

            success = update_lark_stock(token, record_id, new_stock)
            results.append({
                "sku":       sku,
                "status":    "success" if success else "error",
                "old_stock": current_stock,
                "new_stock": new_stock
            })

        return jsonify({"status": "success", "results": results}), 200

    except Exception as e:
        logger.error(f"[Shopify→Lark Cancelled] Error: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════════
# DEBUG: Test Shopify Connection
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/debug/shopify/<sku>", methods=["GET"])
def debug_shopify(sku):
    """Test endpoint to check Shopify API connection and SKU lookup."""
    try:
        headers = get_shopify_headers()
        graphql_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/graphql.json"
        query = """
        query getVariantBySku($query: String!) {
          productVariants(first: 10, query: $query) {
            edges {
              node {
                id
                sku
                title
              }
            }
          }
        }
        """
        variables = {"query": f"sku:{sku}"}
        resp = requests.post(graphql_url, json={"query": query, "variables": variables}, headers=headers)
        return jsonify({
            "status_code": resp.status_code,
            "shopify_url": SHOPIFY_STORE_URL,
            "sku_searched": sku,
            "response": resp.json()
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/auth", methods=["GET"])
def debug_auth():
    """Debug endpoint to verify auth headers being sent."""
    import base64
    try:
        headers = get_shopify_headers()
        # Show partial credentials for verification (never full secret)
        client_id = SHOPIFY_CLIENT_ID or "NOT SET"
        client_secret = SHOPIFY_CLIENT_SECRET or "NOT SET"
        return jsonify({
            "client_id": client_id,
            "client_secret_length": len(client_secret),
            "client_secret_first4": client_secret[:4] if client_secret else "NOT SET",
            "client_secret_last4": client_secret[-4:] if client_secret else "NOT SET",
            "auth_header_preview": headers.get("Authorization", "")[:30] + "...",
            "shopify_url": SHOPIFY_STORE_URL or "NOT SET"
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
