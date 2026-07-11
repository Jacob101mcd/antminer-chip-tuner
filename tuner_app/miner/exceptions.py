"""
Exception class hierarchy for miner-related errors.
"""

from __future__ import annotations


class MRRError(Exception):
    """Raised on any MRR API failure: HTTP non-2xx, `success: false` body,
    malformed JSON, network timeout. Engine-side MRR calls catch this so the
    tuning thread is never aborted by an MRR outage."""

    pass


class MinerCommandError(Exception):
    """Raised when a miner API POST command fails."""

    pass


class UnsafeVoltageBoundsError(MinerCommandError):
    """Raised before a voltage write when live PSU bounds are unavailable.

    Static/spec fallback values are useful for display and non-voltage tuning
    strategies, but they are not authority to mutate a specific miner's PSU.
    """

    pass


class MinerCommandPending(MinerCommandError):
    """Raised when a miner API POST command keeps returning 'Last command is
    still pending' beyond the `_post` retry budget. Distinct from plain
    MinerCommandError so the outer retry loop can just wait and retry the
    tuning step instead of escalating to _attempt_miner_recovery +
    _reset_to_safe_vf (which bounces voltage to BASELINE_VOLTAGE_MV). A
    pending error means the firmware is still processing the previous
    command — not a failure, just back-pressure."""

    pass


class MinerNotReady(Exception):
    """Raised when the miner is not in a ready/mining state after timeout.
    Recoverable — the miner may come back after chainbreak, power loss, etc."""

    pass


class MinerOfflineError(Exception):
    """Raised when the miner is unreachable over the network (connection refused,
    timeout, DNS failure). Distinct from MinerCommandError — the engine treats
    this as 'pause and wait for the miner to come back' instead of burning
    retry budget toward PHASE_ERROR."""

    pass
