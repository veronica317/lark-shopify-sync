# Lark Base ↔ Shopify Two-Way Inventory Sync

Real-time **two-way** inventory sync between Lark Base and Shopify.

```
Lark Base stock updated manually
        ↓
Lark Automation → /sync/lark-to-shopify
        ↓
Updates Shopify inventory ✅

Shopify order fulfilled
        ↓
Shopify Webhook → /sync/shopify-to-lark
        ↓
Deducts stock in Lark Base ✅
```

---

## Environment Variables (set in Render Dashboard)

| Key | Value | Required |
|-----|-------|----------|
| `SHOPIFY_STORE_URL` | `yourstore.myshopify.com` | ✅ |
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin API token | ✅ |
| `SHOPIFY_WEBHOOK_SECRET` | From Shopify webhook settings | ✅ |
| `LARK_APP_ID` | From open.larksuite.com | ✅ |
| `LARK_APP_SECRET` | From open.larksuite.com | ✅ |
| `LARK_BASE_ID` | From your Lark Base URL | ✅ |
| `LARK_TABLE_ID` | From your Lark Base URL | ✅ |
| `WEBHOOK_SECRET` | Any password you choose | ✅ |

---

## Part 1: Deploy to Render

1. Upload all 4 files to GitHub repo (`lark-shopify-sync`)
2. Go to render.com → New → Web Service → connect repo
3. Settings:
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
   - Instance Type: Free
4. Add all environment variables above
5. Deploy → get your URL e.g. `https://lark-shopify-sync.onrender.com`
6. Test: visit `/health` → should return `{"status": "ok"}`

---

## Part 2: Lark Base Automation (Lark → Shopify)

1. Open Lark Base → click **Automations**
2. Create Automation:
   - **Trigger**: Record updated → watch field: `Available Stock`
   - **Action**: Send HTTP Request
     - Method: `POST`
     - URL: `https://your-app.onrender.com/sync/lark-to-shopify`
     - Headers: `Content-Type: application/json`
     - Body:
       ```json
       {
         "secret": "your_webhook_secret",
         "sku": "{{Variant SKU}}",
         "available_stock": "{{Available Stock}}"
       }
       ```
3. Save and Enable

---

## Part 3: Shopify Webhook (Shopify → Lark)

1. Go to Shopify Admin → **Settings** → **Notifications**
2. Scroll to bottom → **Webhooks**
3. Click **Create webhook**:
   - Event: **Order fulfilled**
   - Format: JSON
   - URL: `https://your-app.onrender.com/sync/shopify-to-lark`
4. Click Save
5. Copy the **Signing secret** shown → add it to Render as `SHOPIFY_WEBHOOK_SECRET`

---

## How It Works

| Event | What happens |
|-------|-------------|
| You update `Available Stock` in Lark Base | Shopify inventory is set to the new value |
| A Shopify order is fulfilled | Lark Base `Available Stock` is reduced by the ordered quantity |
| SKU in Lark doesn't exist in Shopify | Safely skipped, no error |
| SKU in Shopify order doesn't exist in Lark | Safely skipped, no error |
| Stock would go below 0 | Automatically capped at 0 |

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Lark→Shopify not working | Check `WEBHOOK_SECRET` matches in Render + Lark Automation body |
| Shopify→Lark not working | Check `SHOPIFY_WEBHOOK_SECRET` matches Shopify signing secret |
| SKU not found | Ensure SKU in Lark matches exactly with Shopify variant SKU (case-sensitive) |
| Render logs show 500 error | Check all environment variables are set correctly |
| Lark Base update fails | Ensure `LARK_BASE_ID` and `LARK_TABLE_ID` are correct |
