# MSpa Cloud API Discovery — 27 April 2026

Findings from authenticated brute-force probing of `api.iot.the-mspa.com`
using the probe script `probe_api_docs.py`.

---

## Complete API surface (confirmed)

| Endpoint | Method | Status | Used by integration |
|---|---|---|---|
| `/api/enduser/get_token/` | POST | Auth, returns bearer token | ✅ Yes |
| `/api/enduser/devices/` | GET | List devices on account | ✅ Yes |
| `/api/device/thing_shadow/` | POST | Poll device status (thing shadow) | ✅ Yes |
| `/api/device/command/` | POST | Send desired-state commands | ✅ Yes |
| **`/api/device/detail/`** | GET | Rich device metadata (query: `?device_id=XXX`) | ❌ **New** |
| **`/api/enduser/grant_device/`** | POST | Share device with visitor (needs `push_type`, `vercode`) | ❌ **New** |
| **`/api/enduser/visitor/`** | POST | Visitor auth (needs `app_id`, `lan_code`, `push_type`, `visitor_id`) | ❌ **New** |
| `/api/enduser/feedback/` | POST | Submit feedback (needs `email`, `feedback_content`) | ❌ Not useful |

Everything else (100+ paths probed) returned 404. The API is small and tightly scoped.

---

## NEW: `/api/device/detail/` response

Called as `GET /api/device/detail/?device_id=<device_id>` with auth headers.

Returns everything from `/devices/` plus:

```json
{
  "device_uuid": "22097100-0000-0048-fac9-c47968040000",
  "certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
  "cert_time": "2026-04-18 17:38:46",
  "country": "NO",
  "service_region": "eu-central-1",
  "warning": "A0",
  "warning_updated_at": 1776593369,
  "fault": "0",
  "product_category_pk": "2TQKWY",
  "product_model_pk": "GI1BED",
  "product_type_pk": "DZRTLZ",
  "product_tub_pk": "I728CB",
  "product_image": "https://s3.eu-central-1.amazonaws.com/donghuiprodpic/.../image.png",
  "product_sub_image": "https://s3.eu-central-1.amazonaws.com/donghuiprodpic/.../sub.png",
  "detail_image": "https://s3.eu-central-1.amazonaws.com/donghuiprodpic/.../detail.png",
  "is_after_sale": false,
  "disconnect_30": false,
  "message_avoidance": true,
  "message_vibration": false,
  "date_action": "2019-01-01:1",
  "is_cloud_activated": 1
}
```

---

## NEW: WebSocket endpoints (from openHAB binding)

The openHAB MSpa binding (`MSpaConstants.java`) reveals WSS endpoints per region:

| Region | WebSocket URL |
|---|---|
| ROW | `wss://xvvfjuknsi.execute-api.eu-central-1.amazonaws.com/production/` |
| US | `wss://27n7hwtf73.execute-api.us-east-1.amazonaws.com/production` |
| CH | `wss://w7vvlxl4dk.execute-api.eu-west-1.amazonaws.com/press_test/` |

These are AWS API Gateway WebSocket endpoints. The openHAB binding uses them
for real-time push updates from the spa, eliminating the need for polling.

---

## What was NOT found

| Feature | Verdict |
|---|---|
| Device capabilities / feature list | No endpoint. App likely hardcodes per `product_series` |
| Scheduling / tasks / timers | No endpoint. Tasks in the app are probably client-side push via WSS or local |
| Weather data | No endpoint. App almost certainly uses a third-party weather API from the phone |
| Bluetooth / BLE pairing | No endpoint. But `device_uuid` from `/device/detail/` is the BLE UUID |
| Swagger / OpenAPI docs | None. Unauthenticated requests return 403; authenticated return 404 |

---

## How these findings can improve the integration

### 1. Use `/api/device/detail/` on init (low effort, high value)

Call once during `async_init()` alongside the device list. Adds:

- **`device_uuid`** → expose as a diagnostic sensor or device info attribute.
  Could enable future BLE local-control support.
- **`warning` + `warning_updated_at`** → dedicated warning sensor separate from
  the thing-shadow `fault` field. The `A0` code means "filter needs changing".
  Could power a filter-change reminder notification.
- **`product_image` / `detail_image`** → use as entity picture in the device
  registry, so the device page shows the actual spa model photo.
- **`certificate`** → store for potential future MQTT direct connection.
- **`country`** / **`service_region`** → expose as device info attributes.
- **`is_after_sale`** → could indicate warranty status.
- **`disconnect_30`** → 30-day disconnect flag, useful for "spa offline" detection.

### 2. WebSocket real-time updates (medium effort, very high value)

Replace polling with a persistent WSS connection:

- **Instant state updates** — no 30-second polling delay when toggling switches
  from the MSpa app or physical panel.
- **Reduced API load** — one persistent connection vs. a request every 30 seconds.
  Eliminates rate-limit risk (code 11000) entirely.
- **Better time-to-target accuracy** — temperature changes reported immediately
  rather than on next poll, giving the EMA tighter samples.
- **Implementation path**: Study the openHAB binding's
  `handler/MSpaPoolHandler.java` to understand the WSS message format.
  HA's `aiohttp` has native WebSocket client support.

### 3. Visitor account support (medium effort, nice-to-have)

The `/api/enduser/visitor/` and `/api/enduser/grant_device/` endpoints enable
the QR-code sharing flow used by the MSpa Link app. Benefits:

- **No token stealing** — the main account stays logged into the phone app
  while HA uses a visitor token. Currently if you auth with the same credentials
  from two places, the last one wins and the other loses its token.
- **Recommended setup simplification** — instead of creating a second MSpa
  account with a guest email, users could share via QR code from their main
  account.
- **Implementation**: The openHAB binding already supports `visitor-account`
  — study their grant flow for the exact payload format.

### 4. Product images in device registry (low effort, nice polish)

Use the `product_image` or `detail_image` URL from `/device/detail/` to set
the device's `configuration_url` or entity picture. The HA device registry
supports a `configuration_url` field that shows as a link; the entity picture
could show the actual spa model photo on dashboard cards.

### 5. AWS IoT MQTT direct connection (high effort, advanced)

The device detail returns a full X.509 certificate for AWS IoT Core. This
could enable:

- **Direct MQTT subscription** to the device's thing shadow topic, bypassing
  the REST API entirely.
- **Sub-second latency** on state changes.
- **OTA firmware update monitoring**.

This is a significant undertaking (AWS IoT auth, MQTT client, topic discovery)
but the certificate is there if we ever want it.

### 6. Hardcoded capability map per product_series (low effort, useful)

Since there's no capabilities endpoint, build a static map:

```python
SERIES_CAPABILITIES = {
    "OSLOUVC": {"heater", "filter", "bubble", "jet", "ozone", "uvc", "lock"},
    "FRAME":   {"heater", "filter", "bubble"},
    "ALPINE":  {"heater", "filter", "bubble", "ozone"},
    # ... extend as users report
}
```

Use `product_series` from the device list to auto-disable entities for
unsupported features, instead of exposing switches that do nothing.

---

## Files produced by probe

| File | Contents |
|---|---|
| `probe_api_docs.py` | Reusable probe script (auth + brute-force path scan) |
| `device_list_full.json` | Full `/api/enduser/devices/` response |
| `get_api_device_detail.json` | `/api/device/detail/` response (GET, no device_id) |
| `post_api_enduser_feedback.json` | `/api/enduser/feedback/` error response |
| `get_api_enduser_feedback.json` | `/api/enduser/feedback/` error response |

---

## Priority roadmap

1. **Now (next release)**: Call `/api/device/detail/` on init, expose product
   images and warning sensor
2. **Soon**: Investigate WSS for real-time updates (study openHAB source)
3. **Later**: Visitor account support, capability map, BLE/MQTT exploration
