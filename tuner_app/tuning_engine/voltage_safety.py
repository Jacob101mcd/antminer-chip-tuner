"""Fail-closed authorization for direct-voltage tuning strategies."""

from __future__ import annotations

from tuner_app.miner.exceptions import UnsafeVoltageBoundsError
from tuner_app.miner.types import HardwareTopology


def require_voltage_mutation_allowed(engine, target_mv: float | None = None) -> None:
    """Authorize a voltage write using this process's live topology read.

    Firmware-owned strategies never issue direct voltage commands and are not
    blocked by placeholder topology ranges. Direct-voltage strategies must
    complete Phase 0 with a ``HardwareTopology`` whose bounds were verified by
    the adapter from the connected firmware.
    """

    if engine.api.tuning_strategy() != "voltage_chip_tune":
        return
    topology = getattr(engine, "voltage_topology", None)
    if not isinstance(topology, HardwareTopology):
        raise UnsafeVoltageBoundsError(
            "refusing voltage mutation: no live, verified PSU topology is loaded"
        )
    topology.require_verified_voltage_target(target_mv)
