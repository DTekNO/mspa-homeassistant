# Copilot Instructions — MSpa Home Assistant Integration

This file gives GitHub Copilot (and human contributors) the context needed to
work safely and effectively in this repository.

---

## Project overview

A [HACS](https://hacs.xyz/) custom integration for Home Assistant that connects
MSpa hot tubs to HA via the MSpa cloud REST API.  The integration is Python-only
(no JavaScript, no YAML configuration beyond stock HA patterns).

Key facts:
- **Domain**: `mspa`
- **Current stable**: see `custom_components/mspa/manifest.json` → `version`
- **Supported HA versions**: whatever is current; no legacy HA support needed
- **No local device control** — all communication goes through the MSpa cloud API
- **Single Python package**: everything lives under `custom_components/mspa/`

---

## Repository layout

```
custom_components/mspa/
├── __init__.py         — Entry setup/teardown, service registration, migrations
├── manifest.json       — Integration metadata and version number
├── const.py            — All constants (intervals, region map, service names)
├── mspa_api.py         — HTTP API client (auth, commands, status polling)
├── coordinator.py      — DataUpdateCoordinator: polling, adaptive rate, power-cycle restore
├── config_flow.py      — UI config flow (credentials → device picker)
├── entity.py           — Shared base entity class
├── sensor.py           — All sensor entities (including time-to-target)
├── switch.py           — On/off switch entities (heater, filter, bubble, jet, ozone, uvc)
├── climate.py          — Climate entity (target temperature)
├── number.py           — Number entity (bubble level)
├── capabilities/       — Per-series feature capability maps
├── schemas/            — Voluptuous service-call schemas
├── translations/       — UI string translations
└── services.yaml       — HA service descriptions
notes/
├── API_DISCOVERY_2026-04-27.md  — Confirmed API surface, WebSocket endpoints, roadmap
CHANGELOG.md            — Keep up to date on every change
```

---

## Architecture

### Request flow

```
HA scheduler
    → coordinator._async_update_data()
        → api.get_hot_tub_status()          (REST POST thing_shadow)
        → transform raw payload
        → rate tracking, power-cycle detection, temperature enforcement
        → return transformed_data dict

User action (switch toggle, temperature set, etc.)
    → coordinator.set_feature_state() / set_temperature() / etc.
        → api.send_device_command()
            → acquire api_lock  ← non-reentrant asyncio.Lock
            → _send_device_command_locked()
                → throttle.acquire()         (rate limiter, 0.4 s spacing)
                → HTTP POST /api/device/command
                → (if filter_state=0) _send_device_command_locked(heater_state=0)  ← DIRECT call, not set_heater_state()
                → coordinator.async_request_refresh()
            → release api_lock
        → _enable_rapid_polling(expected_changes, raw_command)
        → async_request_refresh()
        # coordinator _check_adaptive_polling() then polls 1s, retries once after 15s if unconfirmed
```

### Shared auth store (`hass.data["mspa_auth"]`)

All coordinators for the **same MSpa account** (identified by MD5 of
`email:password`) share one dict:

```python
{
    "token":    str | None,   # bearer token
    "lock":     asyncio.Lock, # serialises authenticate() calls
    "api_lock": asyncio.Lock, # serialises write commands
    "throttle": _MSpaThrottle,# proactive rate limiter
}
```

`api_lock` is **not reentrant**.  Never call `send_device_command()` (or any
method that calls it) from inside `_send_device_command_locked()`.  Use
`_send_device_command_locked()` directly instead.

### Adaptive polling

Normal interval: 60 s (`DEFAULT_SCAN_INTERVAL`).
Rapid interval: 1 s (`RAPID_SCAN_INTERVAL`), max 15 s (`RAPID_POLL_TIMEOUT`).

Rapid polling is enabled **only** when there are unconfirmed pending changes
from a user command (`_pending_changes` dict).  Do **not** add new triggers
(e.g. preheat mode, heat_state transitions) — the history of that pattern
causing a polling storm during multi-minute preheat cycles is documented in
the CHANGELOG.

---

## MSpa cloud API

Base URLs (one per region):
- ROW (Europe/rest-of-world): `https://api.iot.the-mspa.com`
- US: `https://api.usiot.the-mspa.com`
- CH: `https://api.mspa.mxchip.com.cn`

All requests use the same HMAC-MD5 signature scheme (see `build_signature()`).
Rate limit: ~3 req/s.  The shared `_MSpaThrottle` enforces 0.4 s minimum
between requests to stay safely under this ceiling.

Key endpoints:
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/enduser/get_token/` | POST | Authenticate, returns bearer token |
| `/api/enduser/devices/` | GET | List devices on account |
| `/api/device/thing_shadow/` | POST | Poll device status |
| `/api/device/command/` | POST | Send desired-state commands |
| `/api/device/detail/` | GET `?device_id=X` | Rich device metadata (not yet used) |

The `desired` field in a command payload is a **JSON-encoded string** (not a
nested object):
```python
"desired": json.dumps({"state": {"desired": desired_dict}})
```

Temperature values are stored **doubled** in the API (e.g. 40 °C → 80).
Always multiply by 2 when sending, divide by 2 when receiving.

### WebSocket (not yet implemented)

Real-time push updates are available via AWS API Gateway WebSocket endpoints
(see `notes/API_DISCOVERY_2026-04-27.md`).  Implementing this would eliminate
polling and is the highest-priority future feature.

---

## Demo mode

Use email `demo@mspa.test` (any password) in the config flow to add up to three
virtual spa devices with no cloud connectivity.  All API calls are short-circuited
inside `mspa_api.py` via the `self.is_demo` guard.  Keep this working when
making changes to the API client or coordinator.

---

## Key patterns and conventions

- **No f-strings in `_LOGGER` calls** — use `%s` style: `_LOGGER.info("foo %s", bar)`
- **Obfuscate email in logs** — never log the raw account email; use the
  `_obfuscate_email()` helper or the pattern already established
- **DIAGNOSTIC prefix** — verbose cloud interaction logs use `"DIAGNOSTIC: "` prefix
  so users can grep for them when filing issues
- **Emoji in coordinator logs** is intentional for visual scan in HA log viewer
  (🔌 = power off, ⚡ = power on, ♻️ = restore, 🌡️ = temp unit, etc.)
- **transformed_data keys** — coordinator translates raw API keys to friendlier
  names (`heater_state` → `heater`, values `0/1` → `"off"/"on"`, etc.).
  All remaining raw keys are passed through verbatim for dynamic diagnostic
  sensors.  The `_STRUCTURED_STATUS_KEYS` frozenset tracks which raw keys are
  explicitly handled so the pass-through doesn't duplicate them.

---

## Common pitfalls

1. **`api_lock` deadlock** — `send_device_command()` acquires `api_lock`.
   Anything called from inside `_send_device_command_locked()` that also calls
   `send_device_command()` will deadlock.  Always call `_send_device_command_locked()`
   directly for nested commands.

2. **Double `async_request_refresh()`** — the API client calls
   `coordinator.async_request_refresh()` at the end of `_send_device_command_locked()`.
   Coordinator command methods (`set_feature_state` etc.) call it again after
   `_enable_rapid_polling()`.  This is intentional (the second call drives the
   rapid-poll cycle); don't remove either call without understanding the sequence.

3. **Temperature unit is doubled** — every `temperature_setting` and
   `water_temperature` value from the API is ×2.  The coordinator divides by 2
   on the way in; `set_temperature_setting()` multiplies by 2 on the way out.

4. **`_pending_changes` key format** — uses *transformed* key names
   (`"filter"`, `"heater"`, etc.) not raw API names (`"filter_state"`).
   `_check_adaptive_polling` compares against `transformed_data`, not the raw
   status payload.

5. **Config vs options** — user-visible options (restore state, track temp unit,
   always enforce unit) live in `config_entry.options`, not `config_entry.data`.
   Don't mix them up.

---

## Development workflow

There is a pytest suite under `tests/` (156 tests, runs in under a second). It is
dependency-light on purpose — `conftest.py` stubs Home Assistant — so it runs on a
development workstation with nothing but pytest installed.

> **Never run the test suite on the Home Assistant host.** It was tried once on the
> RPi 5 and caused significant disruption. Agents running *on* the HA box (e.g. the
> opencode add-on) must not invoke pytest, install test dependencies, or import the
> test modules there. Run the suite on the development machine; validate on the HA
> instance by loading the integration and reading the logs.
>
> The cause was never established — the HA container's own `homeassistant` package
> most likely interferes with the stubs in `conftest.py` — and deliberately was not
> investigated. Do not treat that as a problem to solve and re-enable test runs
> there: the rule is simply don't, wherever the fault lies.

When making changes:
1. Make the smallest correct change; don't refactor unrelated code
2. Update `CHANGELOG.md` under `[Unreleased]`
3. **Never edit the `version` field in `manifest.json`.** Release automation owns
   it — see below.
4. Add or update tests in `tests/` for behaviour changes, and keep the suite green —
   on the development machine, per the warning above
5. Prefer `report_progress` to commit and push each logical unit of work
6. For a beta release, push to a dedicated branch (e.g. `fix/v3-x-y`) and
   tag it `vX.Y.Z-betaN` from the GitHub releases page

### Which code is actually deployed — read this before diagnosing a log

`manifest.json` in a committed tree holds the **last released** version, not the
version of the code beside it. `update-and-release.yaml` sets the field to the
release tag and commits it back to the branch, so `main` permanently claims to be
whatever was released last. A working tree several commits ahead still reports the
old number.

This is a live trap. A log reading `version: 2026.7.1` may be running code eleven
commits newer, and concluding "they are on the release" from that string alone has
already produced one wrong diagnosis.

Two reliable signals instead:

- **`+hot.<sha>` suffix.** Hot deploys stamp the deployed copy as
  `2026.8.1+hot.f23e577`, where the short SHA after `+hot.` is authoritative for
  what is running. Verified safe: AwesomeVersion parses it as SemVer, and because
  build metadata is ignored for precedence it compares *equal* to the release, so
  HACS neither nags nor mis-orders a later version. Do not "fix" the `+` to a `-`;
  a pre-release suffix compares *older* and would make HACS offer a downgrade.
- **Log markers.** Behaviour-specific lines date the code far better than the
  version does. `eff=` / `ema=` / `buckets=` in the Ready at and Heat Schedule
  transition lines, and `phase-uncertain` on skipped rate samples, all postdate
  2026.7.1.

---

## Roadmap (from `notes/API_DISCOVERY_2026-04-27.md`)

Priority order:
1. **WebSocket real-time updates** — replace polling with persistent WSS connection
   (AWS API Gateway endpoints documented in the notes file)
2. **`/api/device/detail/` on init** — exposes product image, warning sensor,
   country, service_region, device_uuid for BLE/MQTT future work
3. **Per-series capability map** — auto-disable entities for features the spa
   doesn't have, based on `product_series` from the device list
4. **Visitor account support** — let HA use a visitor token so the main account
   stays logged in on the phone app
