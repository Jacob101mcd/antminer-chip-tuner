"""Standalone MRR helpers (rig-rental detection) and re-exported stratum constants."""

from __future__ import annotations

from tuner_app.constants import MRR_STRATUM_PASSWORD, MRR_STRATUM_POOLS

__all__ = ["MRR_STRATUM_PASSWORD", "MRR_STRATUM_POOLS", "is_rig_rented"]


def is_rig_rented(rig: object) -> bool:
    """Return True if the rig dict returned by MRRClient.get_rig() indicates
    an active rental. MRR reports this in multiple shapes depending on the
    API version — a top-level `available_status` of `"rented"`, a `status`
    sub-dict with `rented: true`, or a boolean `rented` field. Handle all
    three defensively."""
    if not isinstance(rig, dict):
        return False
    status_obj = rig.get("status")
    if isinstance(status_obj, dict):
        if status_obj.get("rented") is True:
            return True
        if str(status_obj.get("status") or "").lower() == "rented":
            return True
    elif isinstance(status_obj, str):
        if status_obj.lower() == "rented":
            return True
    if rig.get("rented") is True:
        return True
    avail = str(rig.get("available_status") or "").lower()
    if avail == "rented":  # noqa: SIM103
        return True
    return False
