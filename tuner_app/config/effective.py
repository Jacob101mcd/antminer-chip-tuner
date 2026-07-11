"""EffectiveConfig: read-only merged view of CONFIG defaults + per-miner overrides.

Transitional adapter (v3 + v4 schema):
---------------------------------------
EffectiveConfig accepts either an IPv4 address or a MAC / synthesized ID.

- IPv4 identifier: triggers a reverse-lookup in state.MINER_CONFIGS for an entry
  whose ``"ip"`` field equals the identifier. If a v4 entry is found, that
  entry's MAC key is used internally. If no v4 entry matches, falls back to
  treating the identifier as a direct v3 key (legacy path).
- MAC / synthesized ID: used as a direct key into state.MINER_CONFIGS (v4 path).

Four-step resolution order:
  1. Cross-platform per-miner override: ``MINER_CONFIGS[key][k]`` when
     ``k in CROSS_PLATFORM_PER_MINER_KEYS`` (top-level v4 entry field).
  2. Per-platform per-miner override:
     - v4 shape: ``MINER_CONFIGS[key]["platforms"][firmware][k]``.
     - v3 legacy shape: flat ``MINER_CONFIGS[key][k]`` for any non-cross-platform key.
  3. Per-platform default: ``CONFIG["defaults"][firmware][k]``.
  4. Fleet-ops singleton: ``CONFIG["fleet_ops"][k]``.
  5. KeyError — key absent everywhere (caller bug).

v4 shape is detected at read-time by presence of ``"platforms"`` or
``"current_firmware"`` in the entry dict.

The ``.ip`` property returns the miner IP regardless of identifier type:
- v4 entry: ``MINER_CONFIGS[key].get("ip", "")``.
- v3 / IP-key fallback: returns the identifier itself.
"""

from __future__ import annotations

import re

from tuner_app import state
from tuner_app.constants import CROSS_PLATFORM_PER_MINER_KEYS

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SYNTH_IPV4_RE = re.compile(r"^syn-(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})$")


def resolve_current_firmware(entry: dict, default: str = "epic") -> str:
    """Read the current firmware string from a v4 (``current_firmware``) entry,
    falling back to v3 shape (``firmware_type``) for unmigrated test fixtures
    and finally to *default* when neither key is present or both are falsy.

    Centralizes the v4-vs-v3 lookup so individual handler files don't repeat
    the chained ``.get`` pattern (which would otherwise need an ``or "epic"``
    expression that the registration-boundary audit grep flags).
    """
    if not isinstance(entry, dict):
        return default
    cur = entry.get("current_firmware")
    if cur:
        return cur
    legacy = entry.get("firmware_type")
    if legacy:
        return legacy
    return default


def canonical_miner_key(identifier: str) -> str:
    """Resolve *identifier* to its canonical ``state.MINER_CONFIGS`` key.

    For an IPv4 string: reverse-lookup ``MINER_CONFIGS`` for an entry whose
    ``ip`` field matches. Returns the matching MAC (v4 path) when found;
    otherwise returns the identifier verbatim (legacy v3 path / not yet
    registered).

    For non-IPv4 strings (already a canonical MAC or synth ID): returns the
    identifier unchanged — no lock needed.

    Used by TunerManager and bulk-helper callers to share IP→MAC resolution
    with EffectiveConfig's transitional adapter so all routing into the
    MAC-keyed engines / cache dicts goes through one code path.
    """
    # Synth-encoded-IPv4: "syn-192-0-2-122" → reverse to "192.0.2.122"
    # then route through the IPv4 reverse-lookup branch. This handles the
    # get_overview wire shape for legacy v3 / MINER_IPS-only entries (Unit 8
    # of the bulk-regression fix).
    synth_match = _SYNTH_IPV4_RE.match(identifier)
    if synth_match:
        identifier = ".".join(synth_match.groups())

    if _IPV4_RE.match(identifier):
        with state.config_lock:
            for mac, entry in state.MINER_CONFIGS.items():
                if isinstance(entry, dict) and entry.get("ip") == identifier:
                    return mac
        return identifier
    return identifier


class EffectiveConfig:
    """Read-only dict-like view that merges CONFIG defaults with per-miner overrides.

    See module docstring for full resolution order and transitional adapter semantics.
    """

    __slots__ = ("_key", "_is_legacy_ip")

    def __init__(self, identifier: str) -> None:
        if _IPV4_RE.match(identifier):
            # Reverse-lookup: scan MINER_CONFIGS for a v4 entry with ip == identifier
            with state.config_lock:
                found_mac = None
                for mac, entry in state.MINER_CONFIGS.items():
                    if isinstance(entry, dict) and entry.get("ip") == identifier:
                        found_mac = mac
                        break
            if found_mac is not None:
                self._key = found_mac
                self._is_legacy_ip = False
            else:
                # Legacy fallback: treat the IP as a direct dict key
                self._key = identifier
                self._is_legacy_ip = True
        else:
            # MAC or synthesized ID — direct key, no lock needed at construction
            self._key = identifier
            self._is_legacy_ip = False

    def __getitem__(self, key: str):
        with state.config_lock:
            ov = state.MINER_CONFIGS.get(self._key, {})
            is_v4 = "platforms" in ov or "current_firmware" in ov

            # Step 1: cross-platform per-miner override (v4 top-level keys only)
            if key in CROSS_PLATFORM_PER_MINER_KEYS and key in ov:
                return ov[key]

            # Step 2: per-platform per-miner override
            if is_v4:
                firmware = ov.get("current_firmware", "epic")
                fw_overrides = ov.get("platforms", {}).get(firmware, {})
                if key in fw_overrides:
                    return fw_overrides[key]
            else:
                # v3 legacy: flat dict is treated as per-miner overrides for any key
                firmware = ov.get("firmware_type", "epic")
                if key in ov and key not in CROSS_PLATFORM_PER_MINER_KEYS:
                    return ov[key]

            # Step 3: per-platform default
            platform_bucket = state.CONFIG["defaults"].get(firmware, {})
            if key in platform_bucket:
                return platform_bucket[key]

            # Step 4: fleet-ops singleton
            if key in state.CONFIG["fleet_ops"]:
                return state.CONFIG["fleet_ops"][key]

            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        with state.config_lock:
            ov = state.MINER_CONFIGS.get(self._key, {})
            is_v4 = "platforms" in ov or "current_firmware" in ov

            # Step 1: cross-platform per-miner override
            if key in CROSS_PLATFORM_PER_MINER_KEYS and key in ov:
                return True

            # Step 2: per-platform per-miner override
            if is_v4:
                firmware = ov.get("current_firmware", "epic")
                fw_overrides = ov.get("platforms", {}).get(firmware, {})
                if key in fw_overrides:
                    return True
            else:
                firmware = ov.get("firmware_type", "epic")
                if key in ov and key not in CROSS_PLATFORM_PER_MINER_KEYS:
                    return True

            # Step 3: per-platform default
            platform_bucket = state.CONFIG["defaults"].get(firmware, {})
            if key in platform_bucket:
                return True

            # Step 4: fleet-ops singleton
            return key in state.CONFIG["fleet_ops"]

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    @property
    def ip(self) -> str:
        """Return the miner IP address regardless of identifier type."""
        with state.config_lock:
            if self._is_legacy_ip:
                return self._key
            return state.MINER_CONFIGS.get(self._key, {}).get("ip", "")
