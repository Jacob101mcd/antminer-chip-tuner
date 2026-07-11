"""PHASE_* string constants for the TuningEngine state machine.

Checkpoint files reference these strings; values must remain byte-identical.
"""

from __future__ import annotations

PHASE_IDLE = "idle"
PHASE_DISCOVERY = "phase0_discovery"
PHASE_SET_VOLTAGE = "phase1_set_voltage"
PHASE_BASELINE = "phase2_baseline"
PHASE_VF_EXPLORATION = "phase_v_exploration"
PHASE_PROFILING = "phase3_profiling"
PHASE_POLISH = "phase3b_polish"
PHASE_MEASURE = "phase4_measure"
# Historical value retained for any checkpoints that stamped the
# pre-Phase-V descent phase; the string isn't produced anymore.
PHASE_VOLTAGE_SWEEP = "phase4_voltage_sweep"
PHASE_SAVE = "phase5_save"
PHASE_PERPETUAL = "phase6_perpetual"
PHASE_OFFLINE = "offline"
PHASE_ERROR = "error"
PHASE_STOPPED = "stopped"
PHASE_BRAIINS_DISCOVERY = "phase_braiins_discovery"
PHASE_BRAIINS_WATTAGE_SEARCH = "phase_braiins_wattage_search"
PHASE_BRAIINS_PERPETUAL = "phase_braiins_perpetual"
PHASE_WHATSMINER_DISCOVERY = "phase_whatsminer_discovery"
PHASE_WHATSMINER_PL_FREQ_SEARCH = "phase_whatsminer_pl_freq_search"
PHASE_WHATSMINER_PERPETUAL = "phase_whatsminer_perpetual"
