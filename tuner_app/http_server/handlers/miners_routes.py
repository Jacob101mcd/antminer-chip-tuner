from __future__ import annotations

import json
import logging

from tuner_app import state
from tuner_app.config.effective import resolve_current_firmware
from tuner_app.config.persistence import save_config_to_disk
from tuner_app.config.validation import validate_config
from tuner_app.constants import (
    _PLATFORMS,
    CROSS_PLATFORM_PER_MINER_KEYS,
    FLEET_ONLY_KEYS,
    FLEET_OPS_KEYS,
)
from tuner_app.manager.bulk import _rekey_miner, _remove_miner
from tuner_app.mrr.rental_cache import rental_cache
from tuner_app.net.source_ip import clear_source_ip_cache

from ._mac_helpers import (
    parse_mac_body_field,
    parse_mac_path_segment,
)

logger = logging.getLogger(__name__)


def _register_miner_locked(
    mac: str,
    ip: str,
    password: str | None,
    firmware_type: str,
    id_synthesized: bool = False,
) -> None:
    """Caller MUST hold state.config_lock. Idempotent registration of a discovered
    miner in the v4 schema.

    Writes ``state.MINER_CONFIGS[mac]`` in v4 shape:

      - top-level fields: ``ip``, ``current_firmware``, ``id_synthesized``, and
        ``PASSWORD`` (only when *password* is truthy — falsy passwords leave any
        existing override untouched, matching the pre-A7 helper's behavior).
      - ``platforms`` sub-dict: per-firmware override buckets are initialized
        as empty dicts on first sight; existing buckets are preserved verbatim.

    Also appends *ip* to the ``MINER_IPS`` fleet-ops list (kept for backward
    compatibility with v3 readers; the canonical fleet roster is now
    ``MINER_CONFIGS.keys()``). New IPs are appended; duplicates skipped.

    Idempotent for known MACs: subsequent calls update top-level fields in place
    (``ip``, ``current_firmware``, ``id_synthesized``, optional ``PASSWORD``)
    and leave ``platforms`` sub-dicts untouched. The IP-change-without-teardown
    branch (DHCP move) is handled at the caller via
    ``manager.refresh_engine_ip``; this helper just records the new IP.
    """
    if ip not in state.CONFIG["fleet_ops"]["MINER_IPS"]:
        state.CONFIG["fleet_ops"]["MINER_IPS"].append(ip)
    ov = state.MINER_CONFIGS.setdefault(mac, {})
    ov["ip"] = ip
    ov["current_firmware"] = firmware_type
    ov["id_synthesized"] = bool(id_synthesized)
    if password:
        ov["PASSWORD"] = password
    platforms = ov.setdefault("platforms", {})
    platforms.setdefault(firmware_type, {})


def remove_miner(handler, body) -> None:
    """Handle POST /tuner/miners/remove."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    _remove_miner(handler.manager, mac)
    handler._json_response({"ok": True, "miners": state.CONFIG["fleet_ops"]["MINER_IPS"]})


def set_mac(handler, body) -> None:
    """Handle POST /tuner/miners/set_mac.

    Body shape: ``{"old_mac": "syn-... | aa:bb:...", "new_mac": "aa:bb:..."}``.

    Operator-driven re-key for L3-isolated miners where the original ARP-based
    MAC discovery synthesized a placeholder. The endpoint:

      1. Validates both inputs via ``_normalize_mac``; rejects malformed.
      2. Refuses if ``new_mac == old_mac`` (no-op).
      3. Delegates to ``_rekey_miner`` (in ``tuner_app.manager.bulk``) which
         under ``state.config_lock`` checks for the source MAC, refuses on
         target conflict (HTTP 409), pops the entry, sets
         ``id_synthesized=False``, stores at the new key, persists, and then
         (lock-free) re-keys the engine registry and renames per-platform
         tuning files. Returns ``{"renamed", "engine_rekeyed", "noop"}``.
      4. ``noop=True`` from the helper means ``old_mac`` was not registered
         (after self-rekey is rejected at step 2) — return HTTP 404.
      5. ``ValueError`` from the helper means target conflict — HTTP 409.
    """
    from tuner_app.constants import MAC_PATH_RE, _normalize_mac

    data = json.loads(body) if body else {}
    if not isinstance(data, dict):
        handler._json_response({"ok": False, "error": "body must be an object"}, status=400)
        return
    old_raw = (data.get("old_mac") or "").strip()
    new_raw = (data.get("new_mac") or "").strip()
    if not old_raw or not new_raw:
        handler._json_response({"ok": False, "error": "old_mac and new_mac required"}, status=400)
        return
    if not MAC_PATH_RE.match(old_raw) or not MAC_PATH_RE.match(new_raw):
        handler._json_response(
            {"ok": False, "error": "old_mac/new_mac must be valid MAC or synth ID"},
            status=400,
        )
        return
    try:
        old_mac = _normalize_mac(old_raw)
        new_mac = _normalize_mac(new_raw)
    except (ValueError, TypeError) as ex:
        handler._json_response({"ok": False, "error": f"invalid MAC: {ex}"}, status=400)
        return
    if old_mac == new_mac:
        handler._json_response(
            {"ok": False, "error": "old_mac and new_mac are identical"}, status=400
        )
        return

    try:
        result = _rekey_miner(old_mac, new_mac, manager=handler.manager)
    except ValueError as e:
        handler._json_response({"ok": False, "error": str(e)}, status=409)
        return
    if result["noop"]:
        handler._json_response({"ok": False, "error": f"unknown miner: {old_mac}"}, status=404)
        return
    handler._json_response(
        {"ok": True, "old_mac": old_mac, "new_mac": new_mac, "files_renamed": result["renamed"]}
    )


def _push_mrr_pool_if_needed(handler, cleaned: dict) -> None:
    """Re-push MRR pool config to all engines when relevant fleet-ops keys change.

    Called after saving fleet_ops updates; safe to call with any cleaned dict —
    no-op when none of the triggering keys are present.
    """
    mrr_pool_keys = {"MRR_STRATUM_USERNAME", "MRR_COIN", "MRR_ENABLED"}
    if mrr_pool_keys & set(cleaned.keys()):
        # manager.engines keys are MACs (or synth IDs) post-A9; loop variable
        # named accordingly. The engine itself carries .ip if a logger needs it.
        for mac in list(handler.manager.engines.keys()):
            try:
                eng = handler.manager.engines[mac]
                eng._mrr_apply_pool_config(reason="MRR fleet settings changed")
            except Exception as ex:
                logger.warning("[%s] MRR pool push failed: %s", mac, ex)


def config_defaults(handler, body) -> None:
    """Handle POST /tuner/config/defaults.

    Body shape: {"platform": "epic|bixbit|luxos|braiins", "defaults": {KEY: val, ...}}

    Validates against ``platform``; writes ONLY to state.CONFIG["defaults"][platform].
    Fleet-ops keys are rejected — POST those to /tuner/config/fleet_ops instead.
    Unknown top-level keys beyond "platform" and "defaults" are rejected.
    Missing or null "platform" returns HTTP 400.
    """
    data = json.loads(body) if body else {}
    if not isinstance(data, dict):
        handler._json_response({"updated": False, "errors": ["body must be an object"]}, status=400)
        return

    platform = data.get("platform")
    if platform is None:
        handler._json_response(
            {
                "updated": False,
                "errors": [
                    "'platform' is required — must be one of "
                    f"{list(_PLATFORMS)}. "
                    "POST fleet-ops keys (SCAN_*, MRR_*, MINER_IPS, SOURCE_IP, etc.) "
                    "to /tuner/config/fleet_ops instead."
                ],
            },
            status=400,
        )
        return

    if platform not in _PLATFORMS:
        handler._json_response(
            {
                "updated": False,
                "errors": [f"platform must be one of {list(_PLATFORMS)} (got {platform!r})"],
            },
            status=400,
        )
        return

    defaults_body = data.get("defaults")
    if not isinstance(defaults_body, dict):
        handler._json_response(
            {
                "updated": False,
                "errors": ["'defaults' must be an object when 'platform' is provided"],
            },
            status=400,
        )
        return

    # Reject unknown top-level keys besides "platform" and "defaults".
    extra = [k for k in data if k not in ("platform", "defaults")]
    if extra:
        handler._json_response(
            {"updated": False, "errors": [f"unexpected top-level keys: {extra}"]},
            status=400,
        )
        return

    # Reject fleet-ops keys from the per-platform endpoint — they belong on /fleet_ops.
    fleet_ops_in_body = [k for k in defaults_body if k in FLEET_OPS_KEYS]
    if fleet_ops_in_body:
        handler._json_response(
            {
                "updated": False,
                "errors": [
                    f"{k} is a fleet-ops key — POST to /tuner/config/fleet_ops"
                    for k in fleet_ops_in_body
                ],
            },
            status=400,
        )
        return

    cleaned, errors = validate_config(defaults_body, platform=platform)
    if errors:
        handler._json_response({"updated": False, "errors": errors})
        return

    with state.config_lock:
        for key, val in cleaned.items():
            state.CONFIG["defaults"][platform][key] = val
        save_config_to_disk()
    handler._json_response({"updated": True, "errors": []})


def config_fleet_ops(handler, body) -> None:
    """Handle POST /tuner/config/fleet_ops.

    Body: flat {KEY: val, ...} where every KEY must be in FLEET_OPS_KEYS.
    Writes to state.CONFIG["fleet_ops"]. Triggers cache invalidation
    (source_ip) and MRR pool push for the keys that require it.

    Rejects per-platform keys with HTTP 400 + descriptive error.
    """
    data = json.loads(body) if body else {}
    if not isinstance(data, dict):
        handler._json_response({"updated": False, "errors": ["body must be an object"]}, status=400)
        return

    non_fleet_ops = [k for k in data if k not in FLEET_OPS_KEYS]
    if non_fleet_ops:
        handler._json_response(
            {
                "updated": False,
                "errors": [
                    f"{k} is not a fleet-ops key — POST per-platform keys to /tuner/config/defaults"
                    for k in non_fleet_ops
                ],
            },
            status=400,
        )
        return

    # Validate. Fleet-ops keys don't depend on platform, but validate_config
    # may have cross-field rules that read per-platform defaults; pass "epic"
    # as a safe default so any such lookup resolves cleanly.
    cleaned, errors = validate_config(data, platform="epic")
    if errors:
        handler._json_response({"updated": False, "errors": errors})
        return

    with state.config_lock:
        for key, val in cleaned.items():
            state.CONFIG["fleet_ops"][key] = val
        if "SCAN_PASSWORDS" in cleaned and cleaned["SCAN_PASSWORDS"]:
            state.CONFIG["fleet_ops"]["PASSWORD"] = cleaned["SCAN_PASSWORDS"][0]
        save_config_to_disk()

    if "SOURCE_IP" in cleaned or "API_PORT" in cleaned or "MINER_IPS" in cleaned:
        clear_source_ip_cache()
    _push_mrr_pool_if_needed(handler, cleaned)
    handler._json_response({"updated": True, "errors": []})


def _resolve_target_mac_for_config(target_mac: str) -> tuple[str, str]:
    """Return ``(mac, current_firmware)`` for a per-miner config write.

    *target_mac* is the canonical colon-form MAC validated by the URL parser.
    The function reads ``MINER_CONFIGS[mac]`` to pick up the v4
    ``current_firmware`` field, with a v3-shape ``firmware_type`` fallback for
    test fixtures injected directly without migration. Defaults to ``"epic"``
    when neither key is set.
    """
    with state.config_lock:
        ov = state.MINER_CONFIGS.get(target_mac, {})
        firmware = resolve_current_firmware(ov)
    return target_mac, firmware


def config_miner(handler, body) -> None:
    """Handle POST /tuner/config/miner/{mac}.

    Body shape: ``{<key>: <value>, ...}``. Per-key handling:

      - Cross-platform keys (``PASSWORD``, ``MRR_RIG_ID``, ``hostname``,
        ``current_firmware``) write to the v4 top-level slot.
      - Legacy ``firmware_type`` body field is accepted as an alias for
        ``current_firmware`` so the existing frontend POST shape continues
        to work; on write it's translated to ``current_firmware``.
      - Per-platform tuning keys write to
        ``MINER_CONFIGS[mac]["platforms"][current_firmware][key]``.
      - ``null`` value drops the override (top-level for cross-platform; from
        the platform bucket for per-platform).
      - Fleet-only keys are rejected with HTTP 400.

    A ``current_firmware`` change tears down the existing engine so the next
    ``manager.get_engine`` lazily instantiates a fresh one bound to the new
    vendor's MinerAPI subclass.
    """
    raw_segment = handler.path[len("/tuner/config/miner/") :]
    target_mac = parse_mac_path_segment(handler, raw_segment, "/tuner/config/miner/")
    if target_mac is None:
        return

    data = json.loads(body) if body else {}
    if not isinstance(data, dict):
        handler._json_response({"updated": False, "errors": ["body must be an object"]}, status=400)
        return

    # Reject fleet-only keys on the per-miner endpoint EXCEPT those that
    # are also in CROSS_PLATFORM_PER_MINER_KEYS (PASSWORD legitimately
    # overlaps: it has a fleet default derived from SCAN_PASSWORDS[0] AND
    # supports per-miner override).
    fleet_bad = [
        k
        for k in data.keys()  # noqa: SIM118
        if k in FLEET_ONLY_KEYS and k not in CROSS_PLATFORM_PER_MINER_KEYS
    ]
    if fleet_bad:
        handler._json_response(
            {
                "updated": False,
                "errors": [
                    f"{k} is fleet-wide — edit on the overview page, not per-miner"
                    for k in fleet_bad
                ],
            },
            status=400,
        )
        return

    # Translate legacy firmware_type body field to current_firmware. The
    # operator-facing wire shape supports both for the v4 transition; storage
    # uses current_firmware exclusively per the v4 schema.
    if "firmware_type" in data and "current_firmware" not in data:
        data["current_firmware"] = data.pop("firmware_type")

    deletions = [k for k, v in data.items() if v is None]
    updates = {k: v for k, v in data.items() if v is not None}

    # Per-miner validation: resolve current platform from the v4 entry.
    _mac, miner_platform = _resolve_target_mac_for_config(target_mac)

    # Cross-platform keys (PASSWORD, MRR_RIG_ID, hostname, current_firmware)
    # are not in CONFIG defaults / fleet_ops, so they must skip the
    # ``_key_exists_in_config`` gate inside ``validate_config``. Pull them
    # out of *updates* before validation; merge into *cleaned* afterwards.
    # MRR_RIG_ID still needs bounds-checking (>=0 int); validate_config
    # handles it via the explicit MRR_RIG_ID branch.
    cross_platform_pass_through: dict = {}
    validator_updates = {}
    for k, v in updates.items():
        # current_firmware is renamed to firmware_type on the way IN to the
        # validator (validate_config still uses the legacy key name).
        validator_key = "firmware_type" if k == "current_firmware" else k
        if k == "hostname":
            # hostname has no validator entry — just type-check + length cap.
            if not isinstance(v, str):
                handler._json_response({"updated": False, "errors": ["hostname must be a string"]})
                return
            cross_platform_pass_through[k] = v[:253].strip()
        elif k == "PASSWORD":
            if not isinstance(v, str):
                handler._json_response({"updated": False, "errors": ["PASSWORD must be a string"]})
                return
            cross_platform_pass_through[k] = v
        else:
            validator_updates[validator_key] = v

    cleaned, errors = (
        validate_config(validator_updates, platform=miner_platform)
        if validator_updates
        else ({}, [])
    )
    if errors:
        handler._json_response({"updated": False, "errors": errors})
        return
    if "firmware_type" in cleaned:
        cleaned["current_firmware"] = cleaned.pop("firmware_type")
    cleaned.update(cross_platform_pass_through)

    # Snapshot the prior current_firmware BEFORE the write so we can detect a
    # real change (engine teardown trigger). v3 fixture compatibility:
    # fall back to firmware_type when current_firmware is absent.
    with state.config_lock:
        prior_entry = state.MINER_CONFIGS.get(target_mac, {})
        prior_firmware = resolve_current_firmware(prior_entry)

    with state.config_lock:
        ov = state.MINER_CONFIGS.setdefault(target_mac, {})
        # Detect v4 vs v3 shape so we know where to write per-platform keys.
        is_v4 = "platforms" in ov or "current_firmware" in ov
        # Apply deletions first.
        for k in deletions:
            if k == "firmware_type":
                k = "current_firmware"
            if k in CROSS_PLATFORM_PER_MINER_KEYS:
                ov.pop(k, None)
            elif is_v4:
                platforms_map = ov.get("platforms", {})
                fw_bucket = platforms_map.get(miner_platform)
                if isinstance(fw_bucket, dict):
                    fw_bucket.pop(k, None)
            else:
                ov.pop(k, None)
        # Apply updates.
        for k, v in cleaned.items():
            if k in CROSS_PLATFORM_PER_MINER_KEYS:
                ov[k] = v
            elif is_v4:
                platforms_map = ov.setdefault("platforms", {})
                fw_bucket = platforms_map.setdefault(miner_platform, {})
                fw_bucket[k] = v
            else:
                # v3 legacy fallback (test fixtures only).
                ov[k] = v
        # Prune empty entries (cleared overrides on a non-registered miner).
        if not ov:
            state.MINER_CONFIGS.pop(target_mac, None)
        save_config_to_disk()

    if "SOURCE_IP" in cleaned or "API_PORT" in cleaned:
        clear_source_ip_cache(target_mac)

    # Engine teardown when current_firmware changed — see PR3 / A7 lessons
    # for the lock-ordering and pop-then-destroy rationale.
    new_firmware = cleaned.get("current_firmware")
    if new_firmware is not None and new_firmware != prior_firmware:
        old_engine = handler.manager.pop_engine(target_mac)
        if old_engine is not None:
            try:
                old_engine.destroy()
            except Exception as ex:
                logger.warning(
                    "[%s] engine destroy on current_firmware change failed: %s",
                    target_mac,
                    ex,
                )

    # MRR_RIG_ID change → push pool config + refresh rental cache.
    if "MRR_RIG_ID" in cleaned or "MRR_RIG_ID" in deletions:
        try:
            eng = handler.manager.get_engine(target_mac)
            eng._mrr_apply_pool_config(reason="Rig ID changed via config")
        except Exception as ex:
            logger.warning("[%s] MRR pool push failed: %s", target_mac, ex)
        try:
            if "MRR_RIG_ID" in cleaned:
                new_rig_id = int(cleaned.get("MRR_RIG_ID") or 0)
            else:
                with state.config_lock:
                    new_rig_id = int(
                        state.CONFIG["defaults"]
                        .get(miner_platform, state.CONFIG["defaults"]["epic"])
                        .get("MRR_RIG_ID")
                        or 0
                    )
            rental_cache.refresh_one(target_mac, new_rig_id)
        except Exception as ex:
            logger.warning("[%s] rental_cache refresh failed: %s", target_mac, ex)

    handler._json_response({"updated": True, "errors": []})
