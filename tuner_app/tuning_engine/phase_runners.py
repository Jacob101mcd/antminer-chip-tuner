"""
Legacy Phase 0/2/5 orchestration that's still needed.
"""

from __future__ import annotations

import math
import time

from tuner_app.config.defaults import iter_all_config_keys
from tuner_app.miner.exceptions import MinerCommandError, MinerNotReady, MinerOfflineError
from tuner_app.miner.types import HardwareTopology
from tuner_app.privacy import sanitize


def _validate_topology(topology) -> HardwareTopology:
    """Validate the read-only topology payload before any Phase 0 mutation."""

    if not isinstance(topology, HardwareTopology):
        raise MinerNotReady("Miner returned no valid hardware topology")
    if (
        isinstance(topology.num_boards, bool)
        or not isinstance(topology.num_boards, int)
        or topology.num_boards < 1
    ):
        raise MinerNotReady(f"Invalid hashboard count: {topology.num_boards!r}")
    if (
        isinstance(topology.chips_per_board, bool)
        or not isinstance(topology.chips_per_board, int)
        or topology.chips_per_board < 0
    ):
        raise MinerNotReady(f"Invalid chips-per-board count: {topology.chips_per_board!r}")
    try:
        min_mv = float(topology.psu_min_mv)
        max_mv = float(topology.psu_max_mv)
    except (TypeError, ValueError) as exc:
        raise MinerNotReady("Miner returned non-numeric PSU voltage bounds") from exc
    if not (math.isfinite(min_mv) and math.isfinite(max_mv) and min_mv >= 1000 and max_mv > min_mv):
        raise MinerNotReady(
            f"Invalid PSU voltage bounds: {topology.psu_min_mv!r}-{topology.psu_max_mv!r} mV"
        )
    return topology


def phase0_discovery(engine):
    engine.phase = engine.PHASE_DISCOVERY
    engine.phase_detail = "Connecting to miner"
    engine.log("Phase 0: Discovery & baseline recording")

    # Use MinerNotReady (not plain Exception) so the outer retry loop
    # treats these as recoverable — a miner that's rebooting or briefly
    # off the network is exactly the scenario retries exist for.
    engine.log("Phase 0: reading miner summary")
    summary = engine.api.summary()
    if not summary:
        raise MinerNotReady("Cannot connect to miner (no response from /summary)")

    # Sample live stock baseline BEFORE disabling perpetual tune. This is
    # the only window where the miner is still in whatever state it was
    # before we touched anything.
    engine._capture_live_stock_baseline(summary)

    # Snapshot of the config used for this tune run — preserved with
    # voltage_results / profile so exports include the exact settings
    # that produced each result. Captures the effective view (defaults
    # overlaid with any per-miner overrides) at tune-start.
    engine.config_snapshot = sanitize({k: engine.config[k] for k in iter_all_config_keys()})

    # Capture prior topology for change-detection BEFORE reading the new
    # values — hardware_topology() sets engine.num_boards / chips_per_board.
    prev_num_boards = engine.num_boards
    prev_chips_per_board = engine.chips_per_board

    engine.log("Phase 0: reading hardware topology")
    topology = _validate_topology(engine.api.hardware_topology())
    # This live object is intentionally process-local and never restored from
    # checkpoints. A new process must re-read firmware bounds before it may
    # issue any direct voltage command.
    engine.voltage_topology = topology
    engine.num_boards = topology.num_boards
    engine.chips_per_board = topology.chips_per_board
    engine.psu_min_mv = topology.psu_min_mv
    engine.psu_max_mv = topology.psu_max_mv
    psu_min_mv = topology.psu_min_mv

    # Reshape board-dimensional arrays to the live topology. Handles the
    # cross-hardware resume case (saved 3-board profile loaded on a
    # 4-board miner) by padding/truncating outer lists. If the topology
    # changed, also wipe tuning state that's chip-count-sensitive — stale
    # per-chip freq/baseline arrays from a different miner model would
    # IndexError in the iterative loop when indexed against the new
    # chips_per_board count.
    topology_changed = (
        prev_num_boards != engine.num_boards or prev_chips_per_board != engine.chips_per_board
    )
    engine._resize_board_arrays()
    if (
        topology_changed
        and (prev_num_boards, prev_chips_per_board) != (3, 108)
        and engine.chips_per_board != 0
    ):
        # Only log + invalidate on a *real* topology change — the placeholder
        # 3/108 defaults from __init__ always "differ" on first Phase 0 and
        # that's not a hardware change, it's just initialization.
        # Also skip when chips_per_board==0 (Bixbit sentinel) — Bixbit init
        # is not a real topology change either.
        engine.log(
            f"Hardware topology changed from "
            f"{prev_num_boards}×{prev_chips_per_board} to "
            f"{engine.num_boards}×{engine.chips_per_board} — invalidating "
            f"stale per-chip tuning state"
        )
        engine.baseline_scores = engine._empty_board_arrays()
        engine.baseline_chip_temps = engine._empty_board_arrays()
        engine.baseline_chip_hashrates = engine._empty_board_arrays()
        engine.baseline_freq_arrays = engine._empty_board_arrays()
        engine.stable_freq_arrays = engine._empty_board_arrays()
        engine.proposed_freqs = engine._empty_board_arrays()
        engine.sweep_freq_arrays = engine._empty_board_arrays()
        engine.voltage_results = []
        engine.vf_surface = []
        # Wipe in-flight chip-tune state too — the saved stable_freq_arrays
        # (already wiped above) and target voltage / seed_f are calibrated
        # against the OLD board/chip count and would index out-of-range
        # against the new topology. Resume after this discovery starts the
        # exploration loop fresh.
        engine.in_flight_chip_tune_target = None
        engine.vf_top_k_voltages = []
        engine.vf_planned_grid = []
        engine.vf_skipped = []
        engine.profiling_round = 0
        engine.stillness_streak = 0
        engine.chip_max = None
        engine.phase3_active = False
        engine.parked_chips = [set() for _ in range(engine.num_boards)]
        engine.tuning_complete = False
        engine.best_efficiency = None

    if engine.config["START_VOLTAGE_MV"] > 0:
        engine.start_voltage_mv = engine.config["START_VOLTAGE_MV"]
    else:
        engine.start_voltage_mv = psu_min_mv

    if engine.api.tuning_strategy() == "voltage_chip_tune":
        topology.require_verified_voltage_target(engine.start_voltage_mv)

    engine.min_voltage_mv = engine.start_voltage_mv

    if engine.start_voltage_mv <= 0:
        engine.log(
            "start_voltage_mv resolved to 0 after Phase 0 — using 12000 mV "
            "floor for V/F grid. Set START_VOLTAGE_MV if this could be below your PSU minimum.",
            level="WARN",
        )

    engine.log(
        f"PSU range: {psu_min_mv}-{engine.psu_max_mv} mV, starting at: {engine.start_voltage_mv} mV"
    )
    engine.log(f"Boards: {engine.num_boards}, Chips/board: {engine.chips_per_board}")
    engine.log(
        f"Chip freq spread: {engine.config['CHIP_FREQ_SPREAD_MHZ']} MHz inter-chip cap "
        f"(per-chip search window centered on Phase V winner)"
    )

    # MRR changes the miner's pool configuration, so it must not run until
    # summary and topology reads have succeeded and any direct-voltage
    # strategy has passed the live-bound provenance gate.
    engine._mrr_phase6_announced = False
    engine._mrr_polish_announced = False
    engine.log("Phase 0: applying MRR pool config")
    engine._mrr_apply_pool_config(reason="Phase 0 start")
    engine._mrr_sync("tuning", reason="Phase 0 start")

    engine.phase_detail = "Disabling perpetual tune"
    engine.log("Phase 0: disabling perpetual tune")
    engine.api.set_perpetualtune(False)
    # Phase 0 circuit-breaker reset point. set_perpetualtune is the last
    # expensive (6-TCP) step before we drop into the per-phase state
    # machine; reaching here means the miner survived the Phase 0 burst
    # and we're no longer "storming and dying". The counter increments in
    # _run's MinerOfflineError branch only while phase == PHASE_DISCOVERY.
    engine._phase0_consecutive_offline_hits = 0

    # If the miner is stopped or idling at tune-start (e.g. a fresh tuner
    # process picked up a checkpoint from a prior run that left the miner
    # halted), actively start_mining rather than waiting passively. Without
    # this, Phase V's first measurement would call _phase1_set_voltage →
    # _wait_for_mining_state(300s) → MinerNotReady → retry loop → eventual
    # start_mining, burning ~5 min of false-start time per resume.
    #
    # Reuse the initial summary's operating_state to decide whether to
    # issue start_mining. set_perpetualtune does not flip Mining/Idle, so
    # if the miner was Mining/Initializing at line 33 we can skip both
    # the recheck and start_mining. Saves one full summary() call per
    # Phase 0 — on LuxOS that's 10 fewer TCP cmds during the window where
    # port 4028 is most prone to refusing connections.
    initial_op_state = summary.operating_state if summary else ""
    if initial_op_state not in ("Mining", "Initializing"):
        # Refresh state cheaply — set_perpetualtune may have taken several
        # seconds; the firmware could have transitioned (e.g. Initializing
        # → Mining) in that window. summary_lite is single-cmd on LuxOS.
        refreshed = engine.api.summary_lite()
        op_state = refreshed.operating_state if refreshed else ""
        if op_state not in ("Mining", "Initializing"):
            engine.log(f"Miner state '{op_state}' at tune-start — issuing start_mining")
            try:
                engine.api.start_mining()
            except (MinerCommandError, MinerOfflineError) as e:
                engine.log(f"start_mining failed: {e} — retry loop will escalate")

    engine.log("Phase 0: waiting for mining state")
    engine._wait_for_mining_state(timeout=300)
    engine.log("Disabled perpetual tune")


def phase2_baseline(engine):
    engine.phase = engine.PHASE_BASELINE
    stabilize_time = engine.config["STABILIZE_WAIT"]
    engine.log(f"Phase 2: Waiting {stabilize_time}s for chips to stabilize before baseline scoring")

    # Bixbit: firmware manages per-chip tuning internally — no per-chip
    # health data is available via API. Populate empty baseline arrays for
    # all boards so downstream code (park_dead_chips_from_baseline, Phase 3
    # orchestration) sees a consistent shape and short-circuits correctly.
    if not engine.api.supports_per_chip_tuning():
        engine.log("Phase 2: Bixbit vendor — skipping per-chip sampling (no per-chip API)")
        for b in range(engine.num_boards):
            engine.baseline_scores[b] = []
            engine.baseline_chip_temps[b] = []
            engine.baseline_chip_hashrates[b] = []
            engine.baseline_freq_arrays[b] = []
        # Still wait for stabilization so voltage/freq settle before Phase V.
        remaining = stabilize_time
        while remaining > 0 and engine.running:
            engine.phase_detail = f"Chip stabilization ({remaining}s remaining)"
            time.sleep(min(remaining, 10))
            remaining -= 10
            engine._update_live_data()
            em = engine._detect_thermal_emergency()
            if em:
                engine._handle_thermal_in_chip_tune(em)
        engine._park_dead_chips_from_baseline()
        engine.log("Phase 2: Bixbit baseline complete (empty per-chip arrays)")
        return

    # Chips need time to ramp up hashing after a voltage/frequency change.
    # Health scores reflect actual vs expected hash output — sampling too early
    # produces artificially low baselines (e.g. 15-20% instead of 90%+).
    remaining = stabilize_time
    while remaining > 0 and engine.running:
        engine.phase_detail = f"Chip stabilization ({remaining}s remaining)"
        time.sleep(min(remaining, 10))
        remaining -= 10
        engine._update_live_data()
        em = engine._detect_thermal_emergency()
        if em:
            # Drop offending chips and continue baseline. chip_max isn't
            # in scope yet (Phase 3 hasn't started), so the helper just
            # mutates stable_freq_arrays. The throttled chip will come
            # out of Phase 2 with a low baseline score and likely get
            # parked as dead.
            engine._handle_thermal_in_chip_tune(em)

    if not engine.running:
        return

    engine.phase_detail = "Collecting baseline health scores"
    engine.log("Collecting baseline chip health scores")

    num_samples = engine.config["BASELINE_SAMPLES"]
    interval = engine.config["BASELINE_INTERVAL"]
    accum = [None] * engine.num_boards
    # Parallel accumulators for per-chip Phase 2 captures surfaced to the
    # dashboard's right-hand baseline pane. Same divisor (`collected`) and
    # same per-board structure as `accum` (the health accumulator).
    accum_temps = [None] * engine.num_boards
    accum_hashrates = [None] * engine.num_boards
    temp_samples = [
        0
    ] * engine.num_boards  # chip_temps may fail on some samples; track its own counter per board
    collected = 0

    while collected < num_samples and engine.running:
        engine.phase_detail = f"Baseline sample {collected + 1}/{num_samples}"
        em = engine._detect_thermal_emergency()
        if em:
            engine._handle_thermal_in_chip_tune(em)
            # Don't count this sample — the emergency may have skewed it.
            continue
        # Note: unlike Phase 3 / 3b / V (which restart the round/cell on a
        # mid-sample hashing break to redo the stabilize wait), Phase 2
        # tolerates a single disrupted post-recovery sample. The baseline
        # is a relative reference against which Phase 3 differences each
        # chip, so a small uniform bias from one disrupted sample largely
        # subtracts out. Don't "fix" this without a refactor that resets
        # the per-board accumulators (accum / accum_temps / accum_hashrates
        # / temp_samples / collected) so partial samples aren't averaged
        # together with post-restart samples.
        if not engine._is_miner_hashing():
            engine.log("Miner not hashing during baseline, waiting...")
            engine._wait_for_mining_state(timeout=600)
            continue
        hashrate_data = engine.api.hashrate()
        if not hashrate_data:
            time.sleep(interval)
            continue  # retry without counting toward num_samples

        for board in hashrate_data:
            idx = board.index
            if idx >= engine.num_boards:
                continue
            # Per-chip health % and hashrate MH/s from typed DTO fields.
            chip_scores = board.health_pct
            chip_hashrates_mhs = board.hashrate_per_chip_mhs
            if accum[idx] is None:
                accum[idx] = [0.0] * len(chip_scores)
            if accum_hashrates[idx] is None:
                accum_hashrates[idx] = [0.0] * len(chip_hashrates_mhs)
            for i, score in enumerate(chip_scores):
                if i < len(accum[idx]):
                    accum[idx][i] += score
            for i, hr in enumerate(chip_hashrates_mhs):
                if i < len(accum_hashrates[idx]):
                    accum_hashrates[idx][i] += hr

        # Per-chip temp accumulator — separate API call (/temps/chip).
        # Failures are tolerated (rare API hiccups); per-board sample count
        # tracks how many readings actually contributed. Sampled in lockstep
        # with the health/hashrate read above so the averaged temps reflect
        # the same window.
        chip_temps_data = engine.api.temps_chip()
        if chip_temps_data:
            for board in chip_temps_data:
                idx = board.index
                if idx is None or idx >= engine.num_boards:
                    continue
                chip_temps_list = list(board.chip_temps_c)
                if not chip_temps_list:
                    continue
                if accum_temps[idx] is None:
                    accum_temps[idx] = [0.0] * len(chip_temps_list)
                for i, t in enumerate(chip_temps_list):
                    if i < len(accum_temps[idx]) and isinstance(t, (int, float)):
                        accum_temps[idx][i] += t
                temp_samples[idx] += 1

        collected += 1
        if collected < num_samples:
            time.sleep(interval)

    for b in range(engine.num_boards):
        if accum[b] and collected > 0:
            engine.baseline_scores[b] = [s / collected for s in accum[b]]
        else:
            engine.baseline_scores[b] = [100.0] * engine.chips_per_board
        if accum_hashrates[b] and collected > 0:
            engine.baseline_chip_hashrates[b] = [h / collected for h in accum_hashrates[b]]
        else:
            engine.baseline_chip_hashrates[b] = []
        if accum_temps[b] and temp_samples[b] > 0:
            engine.baseline_chip_temps[b] = [t / temp_samples[b] for t in accum_temps[b]]
        else:
            engine.baseline_chip_temps[b] = []

    clocks_data = engine.api.clocks()
    if clocks_data:
        for board in clocks_data:
            idx = board.index
            if idx < engine.num_boards:
                engine.stable_freq_arrays[idx] = list(board.chip_freqs_mhz)
                engine.proposed_freqs[idx] = list(board.chip_freqs_mhz)
                # Immutable snapshot of Phase 2's freq state — separate from
                # stable_freq_arrays which gets mutated by Phase 3's per-chip
                # iterative loop. Used by the right-hand baseline pane's
                # "Phase 2 Freq" tab.
                engine.baseline_freq_arrays[idx] = list(board.chip_freqs_mhz)

    engine.log(f"Baseline collected: {collected} samples over ~{collected * interval}s")
    engine._park_dead_chips_from_baseline()


def phase5_save(engine):
    engine.phase = engine.PHASE_SAVE
    engine.phase_detail = "Saving tuning profile"
    engine.log("Phase 5: Saving tuning profile")
    engine.tuning_complete = True
    engine._save_profile()
    engine._delete_checkpoint()
    engine.log("Profile saved")
