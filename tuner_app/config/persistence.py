"""Disk persistence: config save/load (v1/v2→v3→v4 migration), atomic JSON write, auth status."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime

from tuner_app import state
from tuner_app.config.schema import CONFIG_BOUNDS
from tuner_app.constants import (
    _PLATFORMS,
    CONFIG_FILE,
    CROSS_PLATFORM_PER_MINER_KEYS,
    DATA_DIR,
    FLEET_OPS_KEYS,
    _miner_data_path,
    _miner_platform_path,
)
from tuner_app.miner.epic import EpicMinerAPI
from tuner_app.net.mac_resolve import resolve_mac, synthesize_mac_id

# Lazy import — avoids circular dependency at module load time.
# Imported on first use inside load_config_from_disk.
_CONFIG_DEFAULTS_PER_PLATFORM_KEYS = None

# Compiled once — canonical MAC form: "aa:bb:cc:dd:ee:ff"
_MAC_COLON_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")

logger = logging.getLogger(__name__)


class ConfigLoadError(RuntimeError):
    """Raised when an existing config cannot be trusted and startup must stop."""


def _ensure_private_directory(path: str) -> None:
    """Create *path* if needed and enforce owner-only access on POSIX."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except NotImplementedError:
        pass
    except PermissionError:
        # Tests and embedding callers may point CONFIG_FILE directly at a file
        # under a shared system temporary directory. Never chmod that shared
        # parent; the actual application DATA_DIR must still fail closed.
        if os.path.abspath(path) == os.path.abspath(DATA_DIR):
            raise


def _validate_config_document(saved: object) -> None:
    """Reject malformed/unsupported config documents before mutating state."""
    if not isinstance(saved, dict):
        raise ConfigLoadError("config root must be a JSON object")
    version = saved.get("version")
    if version is not None and version not in (1, 2, 3, 4):
        raise ConfigLoadError("config has an unsupported schema version")
    for key in ("defaults", "fleet_ops", "miner_configs", "auth"):
        if key in saved and not isinstance(saved[key], dict):
            raise ConfigLoadError(f"config field {key!r} must be an object")


def _get_per_platform_keys():
    global _CONFIG_DEFAULTS_PER_PLATFORM_KEYS
    if _CONFIG_DEFAULTS_PER_PLATFORM_KEYS is None:
        from tuner_app.config.defaults import CONFIG_DEFAULTS_PER_PLATFORM_KEYS

        _CONFIG_DEFAULTS_PER_PLATFORM_KEYS = CONFIG_DEFAULTS_PER_PLATFORM_KEYS
    return _CONFIG_DEFAULTS_PER_PLATFORM_KEYS


def _atomic_json_write(path, payload, indent=2):
    """Atomically write owner-only JSON and fsync it before replacement."""
    raw_directory = os.path.dirname(path)
    directory = raw_directory or "."
    if raw_directory:
        _ensure_private_directory(directory)
    else:
        os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with suppress(AttributeError, NotImplementedError):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(payload, f, indent=indent)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        with suppress(NotImplementedError):
            os.chmod(path, 0o600)
        # Persist the directory entry as well where the platform supports it.
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(tmp)


def save_config_to_disk():
    """Persist CONFIG + AUTH + MINER_CONFIGS to disk (v4 schema).

    Caller must hold config_lock. Schema:
        {
          version: 4,
          defaults: {epic: {...}, bixbit: {...}, luxos: {...}, braiins: {...}},
          fleet_ops: {...},
          miner_configs: {mac: {...}},
          auth: {...}
        }
    """
    _ensure_private_directory(DATA_DIR)
    payload = {
        "version": 4,
        "defaults": {p: dict(state.CONFIG["defaults"].get(p, {})) for p in _PLATFORMS},
        "fleet_ops": dict(state.CONFIG["fleet_ops"]),
        "miner_configs": {mac: dict(ov) for mac, ov in state.MINER_CONFIGS.items()},
        "auth": dict(state.AUTH),
    }
    _atomic_json_write(CONFIG_FILE, payload)


def _apply_renames_and_sanitizers(defaults_data):
    """Apply backward-compat renames and enum sanitization to a flat defaults
    dict in place. Returns the (possibly modified) dict.

    Used for both v1/v2 migration (applied to the flat dict before partition)
    and v3 per-platform buckets (applied independently to each bucket).
    """
    # Backward-compat key rename
    if "BASELINE_STABILIZE_WAIT" in defaults_data and "STABILIZE_WAIT" not in defaults_data:
        defaults_data["STABILIZE_WAIT"] = defaults_data.pop("BASELINE_STABILIZE_WAIT")

    # VF_EXPLORE_FINE_COUNT enum sanitization
    if "VF_EXPLORE_FINE_COUNT" in defaults_data:
        try:
            fc = int(defaults_data["VF_EXPLORE_FINE_COUNT"])
        except (TypeError, ValueError):
            fc = 0
        if fc not in (0, 3, 5, 9, 25, 49):
            defaults_data["VF_EXPLORE_FINE_COUNT"] = (
                0 if fc < 2 else min((3, 5, 9, 25, 49), key=lambda v: abs(v - fc))
            )

    return defaults_data


def _apply_bounds_to_bucket(bucket):
    """Clamp numeric values in `bucket` to CONFIG_BOUNDS in place."""
    for key, (lo, hi) in CONFIG_BOUNDS.items():
        if key in bucket and isinstance(bucket[key], (int, float)):
            bucket[key] = max(lo, min(hi, bucket[key]))


def _looks_like_mac_or_synth(s: str) -> bool:
    """Return True if `s` looks like a canonical MAC or a synth ID.

    Used to distinguish v3 IP-keyed MINER_CONFIGS entries from v4 MAC-keyed
    entries during the migration check.
    """
    if s.startswith("syn-"):
        return True
    return bool(_MAC_COLON_RE.match(s))


def _rename_v3_to_v4_files(ip: str, mac: str, firmware: str, log: logging.Logger) -> None:
    """Best-effort rename of tuning_data files from IP-based to MAC-based names.

    For each file type:
      {ip-dashes}.json            → {mac-dashes}.{firmware}.profile.json
      {ip-dashes}.checkpoint.json → {mac-dashes}.{firmware}.checkpoint.json
      {ip-dashes}.stock.json      → {mac-dashes}.{firmware}.stock.json
      {ip-dashes}.log.jsonl       → {mac-dashes}.log.jsonl  (cross-platform)

    Failures (including target-already-exists) are non-fatal: logged as
    warnings; the migration of in-memory state is unaffected.
    """
    ip_dashes = ip.replace(".", "-")
    renames = [
        (
            os.path.join(DATA_DIR, ip_dashes + ".json"),
            _miner_platform_path(mac, firmware, ".profile.json"),
        ),
        (
            os.path.join(DATA_DIR, ip_dashes + ".checkpoint.json"),
            _miner_platform_path(mac, firmware, ".checkpoint.json"),
        ),
        (
            os.path.join(DATA_DIR, ip_dashes + ".stock.json"),
            _miner_platform_path(mac, firmware, ".stock.json"),
        ),
        (
            os.path.join(DATA_DIR, ip_dashes + ".log.jsonl"),
            _miner_data_path(mac, ".log.jsonl"),
        ),
    ]
    for src, tgt in renames:
        if not os.path.exists(src):
            continue
        if os.path.exists(tgt):
            # Collision — leave source in place, skip silently.
            continue
        try:
            os.rename(src, tgt)
        except OSError as ex:
            log.warning("v3→v4: failed to rename %s → %s: %s", src, tgt, ex)


def _fetch_vendor_mac_for_v3_migration(
    ip: str, firmware_type: str, api_port: int, password: str
) -> str | None:
    """Best-effort vendor-API MAC fetch for v3→v4 migration.

    Returns a canonical MAC string from the vendor API, or None if:
    - firmware_type is not "epic" (other vendors don't support this path)
    - the API call fails for any reason (network, auth, timeout, etc.)
    - summary().mac is None or empty
    """
    try:
        if firmware_type != "epic":
            return None
        api = EpicMinerAPI(ip, port=api_port, password=password)
        summary = api.summary()
        mac = summary.mac
        if isinstance(mac, str) and mac:
            return mac
        return None
    except Exception:
        return None


def _maybe_run_v3_to_v4_migration() -> bool:
    """v3→v4 in-memory + on-disk migration. Idempotent via sentinel file
    AND per-entry v4-shape detection.

    Returns True iff at least one entry was re-keyed (so the caller knows to
    persist via save_config_to_disk).

    Self-heal: even if tuning_data/.migration_v3_to_v4.done exists, scan
    MINER_CONFIGS for any non-v4-shape entry whose key matches an IPv4 dotted-
    quad regex; if any found, run migration anyway. If the sentinel exists AND
    no such entries are present, return False (no work).

    The sentinel file itself is NOT written by this helper — that is the
    caller's (load_config_from_disk's) responsibility, AFTER a successful
    save_config_to_disk.

    For each IP-keyed v3 entry (detected via key NOT looking like a MAC/synth ID
    AND entry NOT having a 'platforms' or 'current_firmware' field):
      - Resolve MAC via _fetch_vendor_mac_for_v3_migration → resolve_mac → synthesize_mac_id.
      - Build v4 entry; pop the IP key; insert under the MAC key.
      - Best-effort rename {ip-dashes}.{ext} → {mac-dashes}.{firmware}.{ext} on disk.
        Failure on any rename is non-fatal — log and continue.
    """
    sentinel_path = os.path.join(DATA_DIR, ".migration_v3_to_v4.done")

    # Self-heal detection: even if sentinel exists, look for v3-shape IPv4-keyed entries
    has_v3_entries = False
    for key, entry in state.MINER_CONFIGS.items():
        if not isinstance(entry, dict):
            continue
        is_v4_shape = "platforms" in entry or "current_firmware" in entry
        if is_v4_shape:
            continue
        # Match canonical IPv4 dotted-quad form (no MAC, no synth ID)
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", key):
            has_v3_entries = True
            break

    if os.path.exists(sentinel_path) and not has_v3_entries:
        return False

    # ── In-memory migration: re-key IP-keyed v3 entries ─────────────────────
    v3_entries: list[tuple[str, dict]] = []
    for key, entry in list(state.MINER_CONFIGS.items()):
        if not isinstance(entry, dict):
            continue
        # Skip already-migrated entries (v4 shape) — defensive guard against
        # double-migration when the sentinel is missing (crash-recovery scenario).
        if "platforms" in entry or "current_firmware" in entry:
            continue
        # Skip entries whose key already looks like a MAC or synth ID — these
        # are v4 entries in flight (shouldn't happen with v4-shape guard above,
        # but defense-in-depth).
        if _looks_like_mac_or_synth(key):
            continue
        v3_entries.append((key, dict(entry)))

    migrated_count = 0
    for ip, v3_entry in v3_entries:
        firmware_type = v3_entry.get("firmware_type", "epic")
        api_port = int(state.CONFIG["fleet_ops"].get("API_PORT", 4028))
        scan_passwords = state.CONFIG["fleet_ops"].get("SCAN_PASSWORDS", []) or []
        password = v3_entry.get("PASSWORD") or (scan_passwords[0] if scan_passwords else "letmein")

        mac = _fetch_vendor_mac_for_v3_migration(ip, firmware_type, api_port, password)
        id_synthesized = False
        if mac is None:
            mac = resolve_mac(ip)
        if mac is None:
            mac = synthesize_mac_id(ip)
            id_synthesized = True

        firmware = v3_entry.pop("firmware_type", "epic")
        v4_entry: dict = {
            "ip": ip,
            "current_firmware": firmware,
            "id_synthesized": id_synthesized,
            "platforms": {firmware: {}},
        }
        for k, val in v3_entry.items():
            if k in CROSS_PLATFORM_PER_MINER_KEYS:
                v4_entry[k] = val
            else:
                v4_entry["platforms"][firmware][k] = val

        state.MINER_CONFIGS.pop(ip, None)
        state.MINER_CONFIGS[mac] = v4_entry

        # File rename — best-effort
        _rename_v3_to_v4_files(ip, mac, firmware, logger)

        migrated_count += 1

    return migrated_count > 0


def load_config_from_disk():
    """Load saved config from disk, merging over defaults. Handles v1/v2/v3 → v4 migration."""
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        config_dir = os.path.dirname(CONFIG_FILE)
        if config_dir:
            _ensure_private_directory(config_dir)
        with suppress(NotImplementedError):
            os.chmod(CONFIG_FILE, 0o600)
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        _validate_config_document(saved)

        per_platform_keys = _get_per_platform_keys()

        # ── Detect schema version ──────────────────────────────────────────
        # Detection is shape-first, version-field second.  A hand-edited or
        # partial-write-recovery file may lack the "version" field entirely but
        # still have a valid nested shape — we must not fall through to v1 in
        # that case.
        #
        # v4: version == 4 is present (MAC-keyed miner_configs, same defaults
        #     shape as v3).  Treated identically to v3 for the defaults/fleet_ops
        #     load; miner_configs entries are already v4-shaped.
        # v3: "defaults" is a dict whose top-level keys include any of the
        #     known platform names (see `_PLATFORMS` in tuner_app.constants), OR
        #     version == 3 is present.
        # v2: "defaults" is a flat dict (no platform sub-keys), OR version == 2
        #     is present.  Includes v2 configs written WITHOUT the version field
        #     by older code.
        # v1: top-level flat dict (no "defaults" key at all).
        saved_version = saved.get("version") if isinstance(saved, dict) else None
        defaults_val = saved.get("defaults") if isinstance(saved, dict) else None
        if isinstance(defaults_val, dict):
            is_v3_or_v4 = saved_version in (3, 4) or any(p in defaults_val for p in _PLATFORMS)
        else:
            is_v3_or_v4 = False
        # is_v2: "defaults" exists as a flat dict but no known platform
        # keys are present (or version==2 overrides).  Not v3/v4 by definition.
        is_v2 = isinstance(defaults_val, dict) and not is_v3_or_v4

        miner_configs_data = (saved.get("miner_configs") or {}) if isinstance(saved, dict) else {}
        auth_data = (saved.get("auth") or {}) if isinstance(saved, dict) else {}

        if isinstance(miner_configs_data, dict):
            state.MINER_CONFIGS.clear()
            for key, ov in miner_configs_data.items():
                if isinstance(ov, dict):
                    state.MINER_CONFIGS[key] = dict(ov)
        if isinstance(auth_data, dict):
            state.AUTH.update({k: v for k, v in auth_data.items() if k in state.AUTH})

        if is_v3_or_v4:
            # ── v3/v4 load: assign platform buckets and fleet_ops directly ──
            for p in _PLATFORMS:
                disk_bucket = defaults_val.get(p) or {}
                if not isinstance(disk_bucket, dict):
                    continue
                target = state.CONFIG["defaults"][p]
                _apply_renames_and_sanitizers(disk_bucket)
                _apply_bounds_to_bucket(disk_bucket)
                for key, val in disk_bucket.items():
                    if key in target:
                        target[key] = val

            disk_fleet = saved.get("fleet_ops") or {}
            if isinstance(disk_fleet, dict):
                # PASSWORD→SCAN_PASSWORDS derivation (idempotent)
                if "PASSWORD" in disk_fleet:
                    _pwd = disk_fleet["PASSWORD"]
                    _scan = disk_fleet.setdefault("SCAN_PASSWORDS", [])
                    if _pwd:
                        while _pwd in _scan:
                            _scan.remove(_pwd)
                        _scan.insert(0, _pwd)
                    disk_fleet["PASSWORD"] = _scan[0] if _scan else _pwd
                for key, val in disk_fleet.items():
                    if key in state.CONFIG["fleet_ops"]:
                        state.CONFIG["fleet_ops"][key] = val
                _apply_bounds_to_bucket(state.CONFIG["fleet_ops"])

        else:
            # ── v1/v2 migration: extract flat defaults, apply renames, partition ──
            if is_v2:
                defaults_data = dict(defaults_val or {})
            else:
                # v1 shape: top-level flat dict
                defaults_data = dict(saved) if isinstance(saved, dict) else {}

            # Apply backward-compat renames + enum sanitizers to flat dict
            _apply_renames_and_sanitizers(defaults_data)

            # PASSWORD→SCAN_PASSWORDS migration
            if "PASSWORD" in defaults_data:
                _pwd = defaults_data.pop("PASSWORD")
                _scan = defaults_data.setdefault("SCAN_PASSWORDS", [])
                if _pwd:
                    while _pwd in _scan:
                        _scan.remove(_pwd)
                    _scan.insert(0, _pwd)
                # Keep PASSWORD in defaults_data so it gets partitioned into fleet_ops
                defaults_data["PASSWORD"] = _scan[0] if _scan else _pwd

            # Partition the flat dict into v3 structure
            new_defaults = {p: {} for p in _PLATFORMS}
            new_fleet_ops = {}
            for key, val in defaults_data.items():
                if key in FLEET_OPS_KEYS:
                    new_fleet_ops[key] = val
                elif key in per_platform_keys:
                    for platform in _PLATFORMS:
                        new_defaults[platform][key] = val
                # else: silently drop deprecated/unknown keys

            # Merge into state.CONFIG (only keys present in the defaults)
            for p in _PLATFORMS:
                target = state.CONFIG["defaults"][p]
                _apply_bounds_to_bucket(new_defaults[p])
                _apply_renames_and_sanitizers(new_defaults[p])
                for key, val in new_defaults[p].items():
                    if key in target:
                        target[key] = val

            for key, val in new_fleet_ops.items():
                if key in state.CONFIG["fleet_ops"]:
                    state.CONFIG["fleet_ops"][key] = val
            _apply_bounds_to_bucket(state.CONFIG["fleet_ops"])

        # ── v3 → v4 migration ─────────────────────────────────────────────────
        migrated = _maybe_run_v3_to_v4_migration()

        # ── Per-miner override cleanup ─────────────────────────────────────────
        # After migration, entries may be v4-shaped (MAC-keyed, have 'ip' field +
        # 'current_firmware' + 'platforms') or v3-shaped (IP-keyed, have
        # 'firmware_type', no 'platforms').  The orphan check and stale-key sweep
        # handle both shapes.
        if state.MINER_CONFIGS:
            valid_ips = set(state.CONFIG["fleet_ops"].get("MINER_IPS", []))
            for mac_or_ip in list(state.MINER_CONFIGS.keys()):
                ov = state.MINER_CONFIGS.get(mac_or_ip)
                if not isinstance(ov, dict):
                    state.MINER_CONFIGS.pop(mac_or_ip, None)
                    continue

                is_v4_shape = "platforms" in ov or "current_firmware" in ov

                # ── Orphan check ──────────────────────────────────────────────
                if is_v4_shape:
                    # v4: compare the stored ip field
                    entry_ip = ov.get("ip")
                    if entry_ip not in valid_ips:
                        state.MINER_CONFIGS.pop(mac_or_ip, None)
                        continue
                else:
                    # v3 (legacy / sentinel-skipped or direct-injected by tests):
                    # the dict key IS the IP
                    if mac_or_ip not in valid_ips:
                        state.MINER_CONFIGS.pop(mac_or_ip, None)
                        continue

                # ── Exempt set for stale-key sweep ────────────────────────────
                # v3: "PASSWORD" + "firmware_type" exempt (existing behavior)
                # v4: additionally exempt structural v4 fields
                _per_miner_exempt = {
                    "PASSWORD",
                    "firmware_type",
                    "current_firmware",
                    "id_synthesized",
                    "ip",
                    "platforms",
                }

                if is_v4_shape:
                    # ── v4 stale-key sweep ────────────────────────────────────
                    # Determine which keys are "known" for this sweep
                    known_config_keys = set(state.CONFIG["fleet_ops"].keys()).union(
                        *(set(state.CONFIG["defaults"][p].keys()) for p in _PLATFORMS)
                    )
                    # Top-level stale keys (non-exempt, non-cross-platform, non-known
                    # or fleet-ops-only)
                    stale_top = [
                        k
                        for k in ov
                        if k not in _per_miner_exempt
                        and k not in CROSS_PLATFORM_PER_MINER_KEYS
                        and (k not in known_config_keys or k in FLEET_OPS_KEYS)
                    ]
                    for k in stale_top:
                        ov.pop(k, None)

                    # Per-platform sub-dict stale-key sweep
                    platforms = ov.get("platforms")
                    if isinstance(platforms, dict):
                        for fw_data in platforms.values():
                            if not isinstance(fw_data, dict):
                                continue
                            stale_plat = [
                                k
                                for k in fw_data
                                if k not in known_config_keys or k in FLEET_OPS_KEYS
                            ]
                            for k in stale_plat:
                                fw_data.pop(k, None)

                    # VF_EXPLORE_FINE_COUNT enum sanitize for v4 platforms sub-dicts
                    if isinstance(platforms, dict):
                        for fw_data in platforms.values():
                            if not isinstance(fw_data, dict):
                                continue
                            if "VF_EXPLORE_FINE_COUNT" in fw_data:
                                try:
                                    fc = int(fw_data["VF_EXPLORE_FINE_COUNT"])
                                except (TypeError, ValueError):
                                    fc = 0
                                if fc not in (0, 3, 5, 9, 25, 49):
                                    fw_data["VF_EXPLORE_FINE_COUNT"] = (
                                        0
                                        if fc < 2
                                        else min((3, 5, 9, 25, 49), key=lambda v: abs(v - fc))
                                    )

                    # current_firmware backfill for v4 entries missing it
                    if "current_firmware" not in ov:
                        ov["current_firmware"] = "epic"

                else:
                    # ── v3 (legacy) stale-key sweep ──────────────────────────
                    known_config_keys = set(state.CONFIG["fleet_ops"].keys()).union(
                        *(set(state.CONFIG["defaults"][p].keys()) for p in _PLATFORMS)
                    )
                    stale = [
                        k
                        for k in ov
                        if k not in _per_miner_exempt
                        and (k not in known_config_keys or k in FLEET_OPS_KEYS)
                    ]
                    for k in stale:
                        ov.pop(k, None)
                    # Same enum sanitize as defaults
                    if "VF_EXPLORE_FINE_COUNT" in ov:
                        try:
                            fc = int(ov["VF_EXPLORE_FINE_COUNT"])
                        except (TypeError, ValueError):
                            fc = 0
                        if fc not in (0, 3, 5, 9, 25, 49):
                            ov["VF_EXPLORE_FINE_COUNT"] = (
                                0 if fc < 2 else min((3, 5, 9, 25, 49), key=lambda v: abs(v - fc))
                            )
                    # firmware_type backfill for v3 entries
                    if "firmware_type" not in ov:
                        ov["firmware_type"] = "epic"
                    # Drop fully-empty v3 entries (legacy guard)
                    if not ov:
                        state.MINER_CONFIGS.pop(mac_or_ip, None)

        # ── Save + sentinel write — ONLY when migration actually re-keyed entries ──
        # The sentinel is written ONLY after save_config_to_disk succeeds, so a
        # crash before the persist would NOT incorrectly mark migration "done".
        if migrated:
            sentinel_path = os.path.join(DATA_DIR, ".migration_v3_to_v4.done")
            with state.config_lock:
                try:
                    save_config_to_disk()
                    try:
                        os.makedirs(DATA_DIR, exist_ok=True)
                        with open(sentinel_path, "w") as f:
                            f.write(datetime.now().isoformat())
                    except OSError as ex:
                        logger.warning("v3→v4: failed to write sentinel: %s", ex)
                except Exception as ex:
                    logger.warning(
                        "v3→v4: save_config_to_disk failed; sentinel NOT written: %s", ex
                    )
    except ConfigLoadError:
        raise
    except Exception as exc:
        # Continuing with defaults after a malformed/unreadable existing
        # config would reopen first-run setup and silently discard safety
        # settings.  Stop startup instead, without echoing config contents.
        raise ConfigLoadError("existing config could not be loaded safely") from exc


def auth_is_configured():
    with state.config_lock:
        return bool(state.AUTH.get("password_hash"))
