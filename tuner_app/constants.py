"""
Module-level constants for the tuner application.

This module centralizes all pure constants used throughout the tuner application,
ensuring consistency and maintainability. Constants are grouped logically with
section headers and retain their original comments for clarity.
"""

from __future__ import annotations

import os
import re

from platformdirs import user_data_path

# ─── Hardware safety floor ───
FIRMWARE_FREQ_MIN_MHZ: int = 50

# ─── Sessions / login lockout ───
SESSION_COOKIE_NAME: str = "tuner_session"
SESSION_TTL_SEC: int = 86400  # 24 hours
# Brute-force protection: lock out a client IP after too many failed logins.
LOGIN_LOCKOUT_THRESHOLD: int = 5
LOGIN_LOCKOUT_WINDOW_SEC: int = 300  # 5 minutes

# ─── Fleet-wide override gate (v3 schema) ───
# FLEET_OPS_KEYS is the authoritative per-miner endpoint gate for the v3 nested
# CONFIG schema. Keys in this set live in state.CONFIG["fleet_ops"] and are
# platform-agnostic singletons — they apply to the whole fleet and cannot be
# meaningfully overridden per-miner. The per-miner endpoint (/tuner/config/miner/{ip})
# rejects any key in this set with HTTP 400.
# FLEET_ONLY_KEYS is kept as a backward-compat alias for existing importers.
# Note: POWER_LIMIT_W and BRAIINS_* tuning knobs were removed from this set
# in v3 — they are now per-platform tuning keys (state.CONFIG["defaults"][platform]).
FLEET_OPS_KEYS: frozenset[str] = frozenset(
    [
        # Scanner: IP ranges and passwords apply to the whole fleet;
        # per-miner overrides are nonsensical.
        "SCAN_IP_RANGES",
        "SCAN_IP_BLACKLIST",
        "SCAN_PASSWORDS",
        "SCAN_TIMEOUT_SEC",
        "SCAN_CONCURRENCY",
        "SCAN_INTERVAL_MIN",
        "SCAN_AUTO_REGISTER",
        # MRR credentials + toggle are fleet-wide; MRR_HASHRATE_MODIFIER_PCT and
        # MRR_RIG_ID are per-miner overridable (not in this set). MRR_HASHRATE_UNIT
        # / MRR_STRATUM_USERNAME / MRR_COIN are fleet-wide because a single tuner
        # process only ever runs one algo / one MRR account.
        "MRR_ENABLED",
        "MRR_API_KEY",
        "MRR_API_SECRET",
        "MRR_HASHRATE_UNIT",
        "MRR_STRATUM_USERNAME",
        "MRR_COIN",
        "MRR_PUBLISH_DURING_POLISH",
        # Network: single fleet-wide connection settings.
        "MINER_IPS",
        "SOURCE_IP",
        "API_PORT",
        # Minerstat: fleet-wide snapshot — one source of truth for the whole fleet,
        # not N independent polls. INCOME_MODIFIER_PCT is fleet-wide because electric
        # billing is fleet-wide.
        "MINERSTAT_COIN",
        "MINERSTAT_POLL_DAY",
        "MINERSTAT_API_KEY",
        "INCOME_MODIFIER_PCT",
        # Logging: fleet-wide verbosity gate and dedup window.
        "LOG_STDOUT_LEVEL",
        "LOG_DEDUP_WINDOW_SEC",
        # Metrics retention horizons — fleet-wide (single shared metrics.db).
        "METRICS_RETENTION_RAW_DAYS",
        "METRICS_RETENTION_5MIN_DAYS",
        "METRICS_RETENTION_1HR_DAYS",
        "METRICS_COMPACT_INTERVAL_HOURS",
        # Auth-internal: derived from SCAN_PASSWORDS[0] at load time.
        # Kept in fleet_ops so EffectiveConfig["PASSWORD"] resolves for MinerAPI auth.
        "PASSWORD",
    ]
)

# Backward-compat alias — existing importers of FLEET_ONLY_KEYS keep working
# unmodified. Migrate to FLEET_OPS_KEYS in new code.
FLEET_ONLY_KEYS: frozenset[str] = FLEET_OPS_KEYS

# Canonical platform tuple. Cross-cutting per-platform iteration imports this.
# Audit-grep `test_platform_tuple_consistency` enforces no inline literals
# anywhere else in `tuner_app/`. Order matches the registry (`MINER_API_REGISTRY`).
_PLATFORMS: tuple[str, ...] = ("epic", "bixbit", "luxos", "braiins", "whatsminer")

# ─── Cross-platform per-miner key whitelist (v4 schema) ───
# Whitelist of keys allowed at the TOP LEVEL of MINER_CONFIGS[mac]
# (cross-platform per-miner overrides — values that survive firmware reflash).
# Keys not in this set that appear in a v3 per-miner entry are migrated
# into platforms[current_firmware] during v3→v4 migration.
#
# NOTE: This set may overlap with FLEET_OPS_KEYS (PASSWORD has a fleet
# default via SCAN_PASSWORDS[0] AND can be a per-miner override) and with
# CONFIG_DEFAULTS_PER_PLATFORM_KEYS (MRR_RIG_ID has a fleet default of 0
# AND can be a per-miner override). Fleet defaults coexist with per-miner
# overrides — no disjoint assertion.
CROSS_PLATFORM_PER_MINER_KEYS: frozenset[str] = frozenset(
    {"PASSWORD", "MRR_RIG_ID", "hostname", "current_firmware"}
)

# ─── File paths ───
DATA_DIR_ENV_VAR: str = "ASIC_TUNER_DATA_DIR"


def _resolve_data_dir() -> str:
    """Return the operator override or the platform-native user data directory."""
    override = os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.abspath(os.fspath(user_data_path("antminer-chip-tuner", appauthor=False)))


DATA_DIR: str = _resolve_data_dir()
CONFIG_FILE: str = os.path.join(DATA_DIR, "config.json")
MINERSTAT_FILE: str = os.path.join(DATA_DIR, "minerstat_snapshot.json")
MRR_NONCE_FILE: str = os.path.join(DATA_DIR, "mrr_nonce.json")
# Single shared SQLite metrics database — all miners write into one file
# (concurrency handled by WAL + an application-level lock in MetricsStore).
METRICS_DB_FILE: str = os.path.join(DATA_DIR, "metrics.db")

# ─── MRR stratum ───
# MRR stratum endpoints — three geo-distributed servers with `#xnsub`
# (extranonce.subscribe) so MRR can rotate extranonce without forcing the
# miner to reconnect. The ePIC firmware accepts up to 3 stratum_configs;
# the miner picks the first reachable one and falls back through the list.
# These are the standard MRR entry points for SHA-256 (BTC) rigs as of
# 2026-04. If MRR changes their stratum topology, update here.
MRR_STRATUM_POOLS: tuple[str, ...] = (
    "stratum+tcp://us-east01.miningrigrentals.com:3311#xnsub",
    "stratum+tcp://us-central01.miningrigrentals.com:3311#xnsub",
    "stratum+tcp://us-west01.miningrigrentals.com:3311#xnsub",
)

# MRR ignores the stratum password — it authenticates by login and routes
# shares by the `.{rig_id}` suffix. 'x' is the de-facto convention across
# MRR docs and the rental UI's sample credentials.
MRR_STRATUM_PASSWORD: str = "x"

# ─── Reset scopes ───
RESET_SCOPES: tuple[str, ...] = ("all", "chip", "chip_fine", "chip_fine_coarse")


# ─── MAC normalization helpers ───
# Compiled once at module load — matches exactly 12 lowercase hex chars.
_MAC_BARE_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{12}$")
# Synth-ID format guard. The output of synthesize_mac_id is always
# `syn-<digits-and-dashes>-<8 lowercase hex>`. Restricting the passthrough
# to this character class prevents a future HTTP-path-param caller from
# slipping path-traversal sequences ("../") or shell metacharacters into
# the filesystem layer that consumes _mac_for_filename output downstream.
_SYNTH_MAC_RE: re.Pattern[str] = re.compile(r"^syn-[0-9a-f][0-9a-f\-]*$")

# Path-segment validator for the v4 HTTP routes. Accepts dash-form MAC
# (``aa-bb-cc-dd-ee-ff``), colon-form MAC (URL-encoded as ``aa%3Abb...``;
# the dispatcher decodes before matching), bare 12-hex-char form, or a
# synth ID (``syn-<ip-dashes>-<8-hex>``). Used by handlers that extract
# the identifier from the URL path so a malformed segment is rejected
# with HTTP 400 before any state lookup.
MAC_PATH_RE: re.Pattern[str] = re.compile(
    r"^(?:[0-9a-fA-F]{12}|"
    r"[0-9a-fA-F]{2}(?:[:\-][0-9a-fA-F]{2}){5}|"
    r"syn-[0-9a-fA-F][0-9a-fA-F\-]*)$"
)


def _normalize_mac(raw: str) -> str:
    """Normalize a MAC-like string to canonical lowercase colon-separated form.

    Accepted input formats (leading/trailing whitespace stripped first):
      - ``"aa:bb:cc:dd:ee:ff"``  colon-separated, any case
      - ``"aa-bb-cc-dd-ee-ff"``  dash-separated, any case
      - ``"aabbccddeeff"``       bare 12 hex chars, any case
      - ``"syn-<...>"``          synthetic ID (starts with ``"syn-"``); returned
        verbatim without validation of the remainder

    Raises:
        TypeError: if *raw* is not a ``str``.
        ValueError: for any other invalid input — empty string, whitespace-only,
            wrong octet count, non-hex characters, mixed separators (``":"`` and
            ``"-"`` both present), or a bare string whose length is not 12.

    Callers needing a uniform exception type can catch ``(ValueError, TypeError)``.
    """
    if not isinstance(raw, str):
        raise TypeError(f"_normalize_mac expects a str, got {type(raw).__name__!r}: {raw!r}")

    s = raw.strip()
    if not s:
        raise ValueError("MAC address must not be empty or whitespace-only")

    # Synth-ID passthrough — format-guarded to prevent path-traversal etc.
    # downstream when the result is used as a filename component.
    if s.startswith("syn-"):
        if not _SYNTH_MAC_RE.match(s):
            raise ValueError(f"Malformed synth MAC ID {raw!r}")
        return s

    has_colon = ":" in s
    has_dash = "-" in s

    # Mixed separators — reject before any further parsing.
    if has_colon and has_dash:
        raise ValueError(
            f"Mixed separators in MAC address {raw!r}: use colons, dashes, or no "
            "separator, but not both"
        )

    if has_colon:
        parts = s.split(":")
        if len(parts) != 6:
            raise ValueError(f"Colon-separated MAC must have 6 octets, got {len(parts)} in {raw!r}")
        for part in parts:
            if len(part) != 2 or not all(c in "0123456789abcdefABCDEF" for c in part):
                raise ValueError(f"Invalid octet {part!r} in MAC address {raw!r}")
        return ":".join(p.lower() for p in parts)

    if has_dash:
        parts = s.split("-")
        if len(parts) != 6:
            raise ValueError(f"Dash-separated MAC must have 6 octets, got {len(parts)} in {raw!r}")
        for part in parts:
            if len(part) != 2 or not all(c in "0123456789abcdefABCDEF" for c in part):
                raise ValueError(f"Invalid octet {part!r} in MAC address {raw!r}")
        return ":".join(p.lower() for p in parts)

    # Bare 12-hex-char form — no separators at all.
    bare = s.lower()
    if not _MAC_BARE_RE.match(bare):
        raise ValueError(f"Bare MAC address must be exactly 12 hex characters, got {raw!r}")
    return ":".join(bare[i : i + 2] for i in range(0, 12, 2))


def _mac_for_filename(mac: str) -> str:
    """Convert a canonical MAC (or synth ID) to filesystem-safe dash form.

    The input is first passed through :func:`_normalize_mac`, which strips
    whitespace, handles all accepted separator variants, and validates the
    value. The canonical colon form is then converted to dashes so the output
    contains no colons (problematic on Windows and awkward in shells).

    Synth IDs (``"syn-..."``) already use dashes throughout and pass through
    unchanged after normalization.

    Raises ``TypeError`` / ``ValueError`` propagated from :func:`_normalize_mac`.
    """
    normalized = _normalize_mac(mac)
    return normalized.replace(":", "-")


# ─── Path helpers ───
def _miner_data_path(identifier: str, suffix: str) -> str:
    """Canonical per-miner persistence path.

    Dots and colons in the identifier become dashes (Windows-friendly).
    Accepts IPv4 (legacy callers), MAC (post-A3 callers), or synth IDs.
    Permissive transform; does not validate — callers are responsible for
    passing a canonical identifier.

    Suffix examples: ``".json"`` (profile, legacy IP-keyed only),
    ``".checkpoint.json"`` (mid-sweep checkpoint, legacy), ``".stock.json"``
    (stock baseline, legacy), ``".log.jsonl"`` (cross-platform JSONL tuning
    log — survives reflash), ``".metrics.db"`` (cross-platform time-series
    store — survives reflash).
    """
    return os.path.join(DATA_DIR, identifier.replace(".", "-").replace(":", "-") + suffix)


def _miner_platform_path(mac: str, firmware: str, suffix: str) -> str:
    """Per-platform persistence path: tuning_data/{mac-dashes}.{firmware}{suffix}.

    Used for tuning artifacts (profile, checkpoint, stock baselines) that
    must be preserved separately per firmware so reflashing a miner from
    e.g. LuxOS to Braiins doesn't lose the prior firmware's tuning profile.
    Operator can switch back and the saved profile reappears.

    MAC is validated and normalized via :func:`_mac_for_filename` (raises
    TypeError/ValueError on bad input). Firmware is concatenated verbatim
    — caller passes one of the supported strings (``"epic"``, ``"bixbit"``,
    ``"luxos"``, ``"braiins"``).
    """
    return os.path.join(DATA_DIR, _mac_for_filename(mac) + "." + firmware + suffix)
