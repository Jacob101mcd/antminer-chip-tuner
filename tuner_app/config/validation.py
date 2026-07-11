# ruff: noqa: F541
"""validate_config(updates, platform=None) — bounds-check and cross-field validation."""

from __future__ import annotations

from tuner_app import state
from tuner_app.config.schema import CONFIG_BOUNDS

# Lazy import to avoid circular import at module load time — scanner.ranges
# imports from tuner_app.net which has no config dependency, so it is safe to
# import here at call time. We use a function-level import for clarity.
_parse_ip_ranges = None


def _get_parse_ip_ranges():
    global _parse_ip_ranges
    if _parse_ip_ranges is None:
        from tuner_app.scanner.ranges import parse_ip_ranges

        _parse_ip_ranges = parse_ip_ranges
    return _parse_ip_ranges


def _is_valid_ipv4(s):
    """Return True if `s` is a syntactically valid IPv4 dotted-quad string."""
    if not isinstance(s, str):
        return False
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:  # noqa: SIM110
        if not p.isdigit() or not 0 <= int(p) <= 255:
            return False
    return True


def _lookup_default(key, platform):
    """Resolve a key's current default value using the v3 schema.

    Per-platform keys are looked up in state.CONFIG["defaults"][platform].
    Fleet-ops keys are looked up in state.CONFIG["fleet_ops"].
    Defaults platform to "epic" when platform is None.

    Lookup order (platform bucket first, fleet_ops second) intentionally
    mirrors EffectiveConfig.__getitem__ so both readers agree even if the
    two partitions ever diverged.  The assert in defaults.py ensures they
    are disjoint, so the order only matters for future-proofing.
    """
    p = platform or "epic"
    bucket = state.CONFIG["defaults"].get(p, {})
    if key in bucket:
        return bucket[key]
    return state.CONFIG["fleet_ops"].get(key)


def _key_exists_in_config(key, platform):
    """Return True if the key exists anywhere in the v3 CONFIG schema."""
    if key in state.CONFIG["fleet_ops"]:
        return True
    p = platform or "epic"
    return key in state.CONFIG["defaults"].get(p, {})


def _is_bool_typed(key, platform):
    """Return True if the key's current default value is a bool."""
    val = _lookup_default(key, platform)
    return isinstance(val, bool)


def validate_config(updates, platform=None):
    """Validate config updates. Returns (cleaned_dict, list_of_errors).

    platform is used for cross-field lookups (resolves current defaults from
    the matching platform bucket). Defaults to "epic" when None (backward
    compat for fleet defaults endpoint until Phase 2 wires the dropdown).
    """
    errors = []
    cleaned = {}
    for key, val in updates.items():
        if key == "firmware_type":
            # Per-miner-only key — not in fleet CONFIG, so must be handled
            # before the unknown-key guard below. Enum validated against the
            # vendor registry so adding a new firmware_type is one-line.
            if not isinstance(val, str):
                errors.append("firmware_type must be a string")
                continue
            v = val.strip().lower()
            from tuner_app.miner.registry import supported_firmware_types

            if v not in supported_firmware_types():
                errors.append(
                    f"firmware_type must be one of {supported_firmware_types()!r} (got '{val}')"
                )
                continue
            cleaned[key] = v
            continue
        if not _key_exists_in_config(key, platform):
            errors.append(f"Unknown config key: {key}")
            continue
        if key == "SOURCE_IP":
            # Allow empty (auto-detect) or a valid IPv4 dotted-quad
            if not isinstance(val, str):
                errors.append(f"SOURCE_IP must be a string")
                continue
            val = val.strip()
            if val and not _is_valid_ipv4(val):
                errors.append(f"SOURCE_IP must be empty or a valid IPv4 address (got '{val}')")
                continue
            cleaned[key] = val
            continue
        if key == "TARGET_MODE":
            # String enum — reject anything outside the known set. Case-fold
            # and strip so the frontend form doesn't need to normalize.
            if not isinstance(val, str):
                errors.append(f"TARGET_MODE must be a string")
                continue
            v = val.strip().lower()
            if v not in ("efficiency", "profitability"):
                errors.append(f"TARGET_MODE must be 'efficiency' or 'profitability' (got '{val}')")
                continue
            cleaned[key] = v
            continue
        if key == "MINERSTAT_COIN":
            # Free-form coin id — upper-case and strip. Minerstat accepts BTC,
            # LTC, ETH, etc. No hard allowlist here because coin coverage is
            # external-data-driven; the fetch will fail later if the coin
            # doesn't exist, which is good enough signal.
            if not isinstance(val, str):
                errors.append(f"MINERSTAT_COIN must be a string")
                continue
            v = val.strip().upper()
            if not v:
                errors.append(f"MINERSTAT_COIN must be non-empty")
                continue
            cleaned[key] = v
            continue
        if key == "MINERSTAT_API_KEY":
            if not isinstance(val, str):
                errors.append(f"MINERSTAT_API_KEY must be a string")
                continue
            cleaned[key] = val.strip()
            continue
        if key == "LOG_STDOUT_LEVEL":
            if not isinstance(val, str):
                errors.append("LOG_STDOUT_LEVEL must be a string")
                continue
            v = val.strip().upper()
            if v not in ("DEBUG", "INFO", "WARN", "ERROR"):
                errors.append(
                    f"LOG_STDOUT_LEVEL must be one of DEBUG, INFO, WARN, ERROR (got '{val}')"
                )
                continue
            cleaned[key] = v
            continue
        if key == "MRR_API_KEY":
            if not isinstance(val, str):
                errors.append(f"MRR_API_KEY must be a string")
                continue
            cleaned[key] = val.strip()
            continue
        if key == "MRR_API_SECRET":
            if not isinstance(val, str):
                errors.append(f"MRR_API_SECRET must be a string")
                continue
            cleaned[key] = val.strip()
            continue
        if key == "MRR_HASHRATE_UNIT":
            if not isinstance(val, str):
                errors.append(f"MRR_HASHRATE_UNIT must be a string")
                continue
            v = val.strip().lower()
            valid_units = ("hash", "kh", "mh", "gh", "th", "ph", "eh")
            if v not in valid_units:
                errors.append(f"MRR_HASHRATE_UNIT must be one of {valid_units} (got '{val}')")
                continue
            cleaned[key] = v
            continue
        if key == "MRR_STRATUM_USERNAME":
            if not isinstance(val, str):
                errors.append(f"MRR_STRATUM_USERNAME must be a string")
                continue
            cleaned[key] = val.strip()
            continue
        if key == "MRR_COIN":
            # ePIC /coin API enum — only BTC or LTC accepted firmware-side.
            if not isinstance(val, str):
                errors.append(f"MRR_COIN must be a string")
                continue
            v = val.strip().upper()
            if v not in ("BTC", "LTC"):
                errors.append(f"MRR_COIN must be 'BTC' or 'LTC' (got '{val}')")
                continue
            cleaned[key] = v
            continue
        if key == "MRR_RIG_ID":
            # 0 = unconfigured, positive int = MRR rig ID. Reject negative and
            # non-integer values.
            try:
                v = int(val)
            except (TypeError, ValueError):
                errors.append(f"MRR_RIG_ID must be an integer (got '{val}')")
                continue
            if v < 0:
                errors.append(f"MRR_RIG_ID must be >= 0 (got {v})")
                continue
            cleaned[key] = v
            continue
        if key == "VF_EXPLORE_FINE_COUNT":
            # Enum: 0 (disabled) plus odd squares so the anchor sits at the
            # exact center for interior anchors. Larger values explode the
            # measurement budget — N=25 means up to 625 cells per top-fine
            # anchor, N=49 is 2401. Values outside the allowed set are
            # rejected before the bounds check.
            try:
                v = int(val)
            except (TypeError, ValueError):
                errors.append(f"VF_EXPLORE_FINE_COUNT must be an integer (got '{val}')")
                continue
            allowed = (0, 3, 5, 9, 25, 49)
            if v not in allowed:
                errors.append(
                    f"VF_EXPLORE_FINE_COUNT must be one of {allowed} "
                    f"(0 = disabled; otherwise odd squares so the anchor cell "
                    f"sits at the center of the NxN fine grid). Got {v}."
                )
                continue
            cleaned[key] = v
            continue
        if key == "SCAN_IP_RANGES":
            # Must be a list of strings; each entry must parse as a valid range.
            if not isinstance(val, list):
                errors.append("SCAN_IP_RANGES must be a list")
                continue
            parse_fn = _get_parse_ip_ranges()
            range_errors = []
            for row_idx, entry in enumerate(val):
                if not isinstance(entry, str):
                    range_errors.append(f"SCAN_IP_RANGES: row {row_idx}: must be a string")
                    continue
                try:
                    parse_fn([entry])
                except ValueError as exc:
                    range_errors.append(f"SCAN_IP_RANGES: {exc}")
            if range_errors:
                errors.extend(range_errors)
                continue
            cleaned[key] = val
            continue
        if key == "SCAN_IP_BLACKLIST":
            # Mirror SCAN_IP_RANGES: list of strings, each parseable as a range.
            if not isinstance(val, list):
                errors.append("SCAN_IP_BLACKLIST must be a list")
                continue
            parse_fn = _get_parse_ip_ranges()
            range_errors = []
            for row_idx, entry in enumerate(val):
                if not isinstance(entry, str):
                    range_errors.append(f"SCAN_IP_BLACKLIST: row {row_idx}: must be a string")
                    continue
                try:
                    parse_fn([entry])
                except ValueError as exc:
                    range_errors.append(f"SCAN_IP_BLACKLIST: {exc}")
            if range_errors:
                errors.extend(range_errors)
                continue
            cleaned[key] = val
            continue
        if key == "SCAN_PASSWORDS":
            # Must be a list of strings.
            if not isinstance(val, list) or not all(isinstance(p, str) for p in val):
                errors.append("SCAN_PASSWORDS must be a list of strings")
                continue
            cleaned[key] = val
            continue
        if key == "SCAN_AUTO_REGISTER":
            # Handled below by the generic boolean coercion — fall through.
            pass
        # Boolean-typed config fields — coerce common frontend shapes into bool.
        if _is_bool_typed(key, platform):
            if isinstance(val, bool):
                cleaned[key] = val
            elif isinstance(val, (int, float)):
                cleaned[key] = bool(val)
            elif isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    cleaned[key] = True
                elif s in ("false", "0", "no", "off", ""):
                    cleaned[key] = False
                else:
                    errors.append(f"{key} must be a boolean (got '{val}')")
            else:
                errors.append(f"{key} must be a boolean")
            continue
        if key in CONFIG_BOUNDS:
            lo, hi = CONFIG_BOUNDS[key]
            if not isinstance(val, (int, float)):
                errors.append(f"{key} must be a number")
                continue
            if val < lo or val > hi:
                errors.append(f"{key} must be between {lo} and {hi} (got {val})")
                continue
        cleaned[key] = val
    # Cross-field validation
    v_step = cleaned.get(
        "PERPETUAL_VOLTAGE_STEP_MV", _lookup_default("PERPETUAL_VOLTAGE_STEP_MV", platform)
    )
    v_max = cleaned.get(
        "PERPETUAL_VOLTAGE_MAX_DELTA_MV",
        _lookup_default("PERPETUAL_VOLTAGE_MAX_DELTA_MV", platform),
    )
    if v_step is not None and v_max is not None and v_step > v_max:
        errors.append(
            f"PERPETUAL_VOLTAGE_STEP_MV ({v_step}) must be <= "
            f"PERPETUAL_VOLTAGE_MAX_DELTA_MV ({v_max})"
        )
    # VF exploration grid sanity: F_MAX must exceed F_MIN with enough spread to
    # produce ≥3 distinct snapped freqs, and the per-chip spread must exceed
    # 2× the iterative step so a chip can take at least one step in either
    # direction within its window.
    vf_fmin = cleaned.get("VF_EXPLORE_F_MIN", _lookup_default("VF_EXPLORE_F_MIN", platform))
    vf_fmax = cleaned.get("VF_EXPLORE_F_MAX", _lookup_default("VF_EXPLORE_F_MAX", platform))
    chip_spread = cleaned.get(
        "CHIP_FREQ_SPREAD_MHZ", _lookup_default("CHIP_FREQ_SPREAD_MHZ", platform)
    )
    chip_step = cleaned.get("CHIP_TUNE_STEP_MHZ", _lookup_default("CHIP_TUNE_STEP_MHZ", platform))
    up_tol = cleaned.get(
        "CHIP_TUNE_UP_TOLERANCE", _lookup_default("CHIP_TUNE_UP_TOLERANCE", platform)
    )
    down_tol = cleaned.get(
        "CHIP_TUNE_DOWN_TOLERANCE", _lookup_default("CHIP_TUNE_DOWN_TOLERANCE", platform)
    )
    vf_vcount = cleaned.get("VF_EXPLORE_V_COUNT", _lookup_default("VF_EXPLORE_V_COUNT", platform))
    vf_topk = cleaned.get("VF_EXPLORE_TOP_K", _lookup_default("VF_EXPLORE_TOP_K", platform))
    if vf_fmin is not None and vf_fmax is not None and vf_fmax <= vf_fmin + 2 * 3.125:
        errors.append(
            f"VF_EXPLORE_F_MAX ({vf_fmax}) must be > VF_EXPLORE_F_MIN + 6.25 "
            f"so the grid has at least 3 distinct snapped freqs"
        )
    if chip_spread is not None and chip_step is not None and chip_spread < 2 * chip_step:
        errors.append(
            f"CHIP_FREQ_SPREAD_MHZ ({chip_spread}) must be >= "
            f"2 x CHIP_TUNE_STEP_MHZ ({2 * chip_step}) so each chip's "
            f"window allows at least one step in either direction"
        )
    if up_tol is not None and down_tol is not None and up_tol > down_tol:
        errors.append(
            f"CHIP_TUNE_UP_TOLERANCE ({up_tol}) must be <= "
            f"CHIP_TUNE_DOWN_TOLERANCE ({down_tol}) — otherwise the "
            f"step-up and step-down branches overlap"
        )
    if vf_topk is not None and vf_vcount is not None and vf_topk > vf_vcount:
        errors.append(f"VF_EXPLORE_TOP_K ({vf_topk}) must be <= VF_EXPLORE_V_COUNT ({vf_vcount})")
    # Whatsminer (stock MicroBT) 2D grid-search bounds: FREQ_MAX > FREQ_MIN so the
    # descending freq axis has at least one step.
    wm_f_min = cleaned.get(
        "WHATSMINER_FREQ_MIN_MHZ", _lookup_default("WHATSMINER_FREQ_MIN_MHZ", platform)
    )
    wm_f_max = cleaned.get(
        "WHATSMINER_FREQ_MAX_MHZ", _lookup_default("WHATSMINER_FREQ_MAX_MHZ", platform)
    )
    if wm_f_min is not None and wm_f_max is not None and wm_f_max <= wm_f_min:
        errors.append(
            f"WHATSMINER_FREQ_MAX_MHZ ({wm_f_max}) must be > WHATSMINER_FREQ_MIN_MHZ ({wm_f_min})"
        )
    # MRR: if being enabled, require non-empty key + secret. Blank-means-keep
    # applies — the effective value is the cleaned update OR the current CONFIG.
    # We only fail if the effective (post-update) state has ENABLED=True without
    # credentials; that way a no-op submit of just `{MRR_ENABLED: true}` still
    # works when creds were already set in a prior submit.
    mrr_enabled_after = cleaned.get(
        "MRR_ENABLED", _lookup_default("MRR_ENABLED", platform) or False
    )
    if mrr_enabled_after:
        mrr_key_after = (
            cleaned.get("MRR_API_KEY", _lookup_default("MRR_API_KEY", platform) or "")
        ).strip()
        mrr_sec_after = (
            cleaned.get("MRR_API_SECRET", _lookup_default("MRR_API_SECRET", platform) or "")
        ).strip()
        mrr_user_after = (
            cleaned.get(
                "MRR_STRATUM_USERNAME", _lookup_default("MRR_STRATUM_USERNAME", platform) or ""
            )
        ).strip()
        if not mrr_key_after:
            errors.append("MRR_ENABLED requires MRR_API_KEY to be set")
        if not mrr_sec_after:
            errors.append("MRR_ENABLED requires MRR_API_SECRET to be set")
        if not mrr_user_after:
            errors.append(
                "MRR_ENABLED requires MRR_STRATUM_USERNAME to be set "
                "(stratum login uses '{username}.{rig_id}')"
            )
    return cleaned, errors
