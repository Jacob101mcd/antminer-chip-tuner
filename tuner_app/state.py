"""
Shared module-level state for the tuner application.

Inside `tuner_app/`, never `from tuner_app.state import CONFIG` etc. — do
`from tuner_app import state` then `state.CONFIG[...]`. tuner.py is the legacy
bridge and is exempt from that rule.

Thread locks are used to protect shared mutable state:
- config_lock protects CONFIG, AUTH, MINER_CONFIGS (all persist to the same file)
- minerstat_lock protects MINERSTAT_SNAPSHOT
- _sessions_lock protects _sessions
- _login_attempts_lock protects _login_attempts
"""

from __future__ import annotations

import threading

# ─── Configuration ─────────────────────────────────────────────────
# v3 nested schema: per-platform tuning defaults + fleet-ops singletons.
# apply_defaults() populates these on startup; load_config_from_disk()
# overlays saved values and migrates v1/v2 on-disk configs to v3 shape.
CONFIG: dict = {
    "defaults": {
        "epic": {},
        "bixbit": {},
        "luxos": {},
        "braiins": {},
        "whatsminer": {},
    },
    "fleet_ops": {},
}

# ─── Authentication state ──────────────────────────────────────────────────
#
# Single-password auth with session cookies. scrypt hash (stdlib) stored in
# config.json under the `auth` key. No default password — first-run triggers a
# setup form in the UI. Sessions live in-memory only; restarting the server
# logs everyone out.
AUTH = {"password_hash": None, "created_at": None}

# ─── Per-miner config overrides ──────────────────────────────────────────────
#
# `CONFIG["defaults"][platform]` holds per-platform tuning defaults;
# `CONFIG["fleet_ops"]` holds platform-agnostic singleton keys.
# `MINER_CONFIGS[mac]` (v4 schema) holds per-miner state — top-level fields
# (ip, current_firmware, id_synthesized, PASSWORD, MRR_RIG_ID, hostname) plus
# nested `platforms[firmware]` per-platform tuning overrides.
# Engines read through EffectiveConfig(mac), which resolves:
#   cross-platform per-miner → per-platform per-miner → per-platform default →
#   fleet_ops → KeyError.
MINER_CONFIGS = {}

# ─── Minerstat snapshot ───────────────────────────────────────────────────
#
# Fleet-wide cache of minerstat.com coin data. Populated by the "Fetch now"
# button on the dashboard or by the MinerstatScheduler on its configured
# monthly day. Persisted to `tuning_data/minerstat_snapshot.json` so it
# survives tuner restarts.
MINERSTAT_SNAPSHOT = {}

# ─── Thread locks ─────────────────────────────────────────────────────
# Thread lock for CONFIG / AUTH / MINER_CONFIGS access (all persist to the same file)
config_lock = threading.Lock()

# Lock for MINERSTAT_SNAPSHOT access
minerstat_lock = threading.Lock()

# Lock for _sessions access
_sessions_lock = threading.Lock()

# Lock for _login_attempts access
_login_attempts_lock = threading.Lock()

# ─── Session and login tracking ─────────────────────────────────────────────
_sessions = {}  # token -> expiry_ts
_session_gc_counter = 0
_login_attempts = {}  # client_ip -> (fail_count, first_fail_ts)

# ─── Metrics store (Phase B) ─────────────────────────────────────────────────
# tuner_app.main wires this up after apply_defaults() / load_config_from_disk()
# and clears it on KeyboardInterrupt. Engine monitor cycle reads it via
# `state.metrics_store` so a None means "metrics disabled" — the cycle
# never aborts on metrics-write failure.
metrics_store = None  # type: ignore[var-annotated]
