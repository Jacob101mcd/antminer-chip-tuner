"""Module containing the TuningEngine class — the per-miner tuning engine.

The class shell is moved here from tuner.py. Method bodies live in topic
modules in this same package (apply, scoring, persistence, etc.); the
class methods are mostly one-line delegations into those topic modules.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections import deque

from tuner_app.miner.registry import MINER_API_REGISTRY
from tuner_app.miner.exceptions import (
    MinerCommandError,
    MinerCommandPending,
    MinerNotReady,
    MinerOfflineError,
    UnsafeVoltageBoundsError,
)
from tuner_app.miner.types import HardwareTopology, MinerSummary
from tuner_app.tuning_engine import (
    apply,
    chip_tune_loop,
    chip_tune_orchestration,
    exploration,
    grid,
    lifecycle,
    logging_,
    measurement,
    monitor,
    mrr_sync,
    perpetual,
    persistence,
    phase_runners,
)
from tuner_app.tuning_engine import phase_vf_exploration as phase_vf_exploration_mod
from tuner_app.tuning_engine import (
    phases,
    recovery,
    reset,
    retune,
    scoring,
    status,
)


class TuningEngine:
    """
    Per-miner tuning engine. Phase V maps the 2D (V, F) efficiency surface
    via an iterative 8-ray walk that re-spawns from any cell that strictly
    beats the current best J/TH, converging when an 8-ray pass from `best`
    finds no improvement. Then an iterative per-chip health loop tunes each
    chip's frequency within `seed_f ± SPREAD/2` at the top-K voltages, until
    no chip moves for CHIP_TUNE_STILLNESS_STREAK consecutive rounds. Phase 4
    measures efficiency, Phase 5 saves, Phase 6 runs the voltage-tracking
    perpetual tune forever.
    """

    PHASE_IDLE = phases.PHASE_IDLE
    PHASE_DISCOVERY = phases.PHASE_DISCOVERY
    PHASE_SET_VOLTAGE = phases.PHASE_SET_VOLTAGE
    PHASE_BASELINE = phases.PHASE_BASELINE
    PHASE_VF_EXPLORATION = phases.PHASE_VF_EXPLORATION
    PHASE_PROFILING = phases.PHASE_PROFILING
    PHASE_POLISH = phases.PHASE_POLISH
    PHASE_MEASURE = phases.PHASE_MEASURE
    # Historical value retained for any checkpoints that stamped the
    # pre-Phase-V descent phase; the string isn't produced anymore.
    PHASE_VOLTAGE_SWEEP = phases.PHASE_VOLTAGE_SWEEP
    PHASE_SAVE = phases.PHASE_SAVE
    PHASE_PERPETUAL = phases.PHASE_PERPETUAL
    PHASE_OFFLINE = phases.PHASE_OFFLINE
    PHASE_ERROR = phases.PHASE_ERROR
    PHASE_STOPPED = phases.PHASE_STOPPED
    PHASE_BRAIINS_DISCOVERY = phases.PHASE_BRAIINS_DISCOVERY
    PHASE_BRAIINS_WATTAGE_SEARCH = phases.PHASE_BRAIINS_WATTAGE_SEARCH
    PHASE_BRAIINS_PERPETUAL = phases.PHASE_BRAIINS_PERPETUAL
    PHASE_WHATSMINER_DISCOVERY = phases.PHASE_WHATSMINER_DISCOVERY
    PHASE_WHATSMINER_PL_FREQ_SEARCH = phases.PHASE_WHATSMINER_PL_FREQ_SEARCH
    PHASE_WHATSMINER_PERPETUAL = phases.PHASE_WHATSMINER_PERPETUAL

    LOG_LINES_MAX_CAP = 100_000
    LOG_ROTATE_TARGET = 80_000
    LOG_ROTATE_CHECK_INTERVAL = 1000

    def __init__(self, mac, config):
        # MAC is the canonical identifier in v4 — used for engines/registries
        # dict keying and for per-platform/log persistence paths. The
        # transitional layer (v3 fallback + IP-based test fixtures) stores
        # whatever the caller passed; per-platform path helpers fall back to
        # legacy _miner_data_path naming when self.mac fails MAC validation.
        self.mac = mac
        self.config = config
        # IP comes from the EffectiveConfig wrapper for v4 entries (config.ip
        # property reads MINER_CONFIGS[mac]["ip"]); for legacy fallback callers
        # that pass an IP directly without an EffectiveConfig, fall back to the
        # identifier itself so existing tests / pre-A9 manager code paths keep
        # working until the manager is rekeyed in A9.
        if hasattr(config, "ip"):
            self.ip = config.ip or mac
        else:
            self.ip = mac
        # firmware_type prefers v4 "current_firmware" then falls back to v3
        # "firmware_type" then defaults to "epic". Stored on the engine so all
        # per-platform persistence paths and downstream branching pivot off a
        # single attribute (rather than re-reading config every call site).
        firmware_type = config.get("current_firmware") or config.get("firmware_type", "epic")
        self.firmware_type = firmware_type
        if firmware_type not in MINER_API_REGISTRY:
            raise ValueError(
                f"Unknown firmware_type {firmware_type!r}; "
                f"expected one of {sorted(MINER_API_REGISTRY)}"
            )
        self.api = MINER_API_REGISTRY[firmware_type](self.ip, config)
        self.phase = self.PHASE_IDLE
        self.phase_detail = ""
        # log_lines holds dict entries: {"ts", "voltage_mv", "phase", "msg"}.
        # Bounded deque — the JSONL file is the authoritative store.
        self.log_lines = deque(maxlen=self.LOG_LINES_MAX_CAP)
        self.log_file_lock = threading.Lock()
        self._log_appends_since_rotate_check = 0
        # Per-engine dedup window state (Run 10c). Prevents stdout/JSONL spam
        # from repeated identical messages (e.g. "Monitor: transient offline").
        # Window duration is LOG_DEDUP_WINDOW_SEC; 0 disables dedup entirely.
        self._log_dedup_msg: str | None = None
        self._log_dedup_level: str | None = None
        self._log_dedup_first_ts: float | None = None
        self._log_dedup_count: int = 0
        # Voltage step currently being swept — tags every log entry so the
        # per-voltage modal can filter. None outside Phases 1–4 of a sweep step.
        self.current_sweep_voltage_mv = None
        # Offline-handling state — updated from the retry loop.
        self.offline_since_ts = None
        self.offline_failure_count = 0
        self.last_successful_contact_ts = None
        self.pre_offline_phase = None
        self.pre_offline_phase_detail = ""
        # Phase-0-specific circuit breaker: counts consecutive _run() exits
        # via MinerOfflineError raised while phase == PHASE_DISCOVERY. Reset
        # to 0 inside phase0_discovery after set_perpetualtune succeeds (the
        # last cmd-issuing step). After PHASE0_CIRCUIT_BREAKER_THRESHOLD
        # consecutive Phase 0 storm-and-die cycles, _run() flips to
        # PHASE_ERROR rather than looping forever. Without this, a miner
        # that consistently refuses connections during Phase 0's command
        # burst (e.g. LuxOS firmware overload) cycles every ~3 minutes
        # indefinitely because wait_for_miner_online resets offline_hits.
        self._phase0_consecutive_offline_hits = 0
        self.thread = None
        self.running = False
        # Latched true by destroy() when this engine instance is being thrown
        # away (Reset Profile / remove miner). The tuning thread can outlive
        # stop()+join(timeout=5) because long sleeps inside sample loops only
        # check self.running at 5–10s intervals — Phase 3's per-round restart
        # cycle can hold the thread for tens of seconds. Without this guard,
        # an orphaned thread that wakes after the file deletion would call
        # _save_checkpoint()/log()/_save_profile() and resurrect the deleted
        # state on disk, which then loads back into the dashboard on the next
        # process restart (or any code path that creates a fresh engine for
        # the IP). All disk-write methods short-circuit when this is true.
        self._destroyed = False
        # Set by the retry loop after _attempt_miner_recovery. _run_inner
        # checks this at the top and runs _reset_to_safe_vf to apply a known
        # baseline V/F before trusting the miner. Cleared once the reset
        # runs so mid-tune phase transitions don't re-trigger it.
        self.last_exit_was_recovery = False
        # Wall-clock timestamp of the most recent point where the miner was
        # confirmed to be in a good state (Phase 0's wait_for_mining_state
        # returned, OR _reset_to_safe_vf succeeded). The outer retry catch
        # uses this — not the iteration's run_duration — to decide whether
        # the previous recovery succeeded. Without this, any slow failure
        # mode (e.g. a 20-min Phase 1 settle timeout) burns past
        # SUCCESSFUL_RUN_SEC and resets the retry counter, so MAX_CONSECUTIVE_RETRIES
        # never escalates to FATAL and the engine loops forever. None means
        # we have no recent good-state evidence — don't reset retries.
        self._iteration_confirmed_good_at: float | None = None
        # Serializes start()/stop()/start_retune(). The HTTP server is
        # threaded, so two rapid /tuner/start clicks can both pass the
        # is_alive() check and each spawn a new worker — overwriting
        # self.thread, leaving a duplicate _run() loop that nothing tracks
        # and both threads mutating engine state concurrently. This lock
        # makes the check-and-spawn atomic.
        self._control_lock = threading.Lock()

        # Tuning state
        self.stock_baseline = {
            "hashrate_ths": 200,
            "power_w": 3500,
            "efficiency_jth": 17.5,
            "voltage_mv": 14000,
        }
        self.start_voltage_mv = 0
        self.min_voltage_mv = 0
        self.psu_min_mv = 0
        self.psu_max_mv = 15182
        # Process-local authorization evidence for direct voltage writes.
        # Deliberately not restored from checkpoints: every process must read
        # and validate live firmware bounds in Phase 0 first.
        self.voltage_topology: HardwareTopology | None = None
        # Placeholder values — overwritten in _phase0_discovery from /capabilities
        # (Max HBs, Chip Count). Any board-dimensional allocation done against
        # these must be reshaped by _resize_board_arrays() once Phase 0 completes,
        # otherwise a miner with a non-default topology will IndexError in Phase 2.
        self.num_boards = 3
        self.chips_per_board = 108
        self.baseline_scores = self._empty_board_arrays()
        # Per-chip Phase 2 captures alongside baseline_scores. Populated by
        # _phase2_baseline alongside the health-score accumulator and surfaced
        # to the dashboard's right-hand "Phase 2 Baseline" heatmap pane.
        self.baseline_chip_temps = self._empty_board_arrays()
        self.baseline_chip_hashrates = self._empty_board_arrays()
        self.baseline_freq_arrays = self._empty_board_arrays()
        self.stable_freq_arrays = self._empty_board_arrays()
        self.proposed_freqs = self._empty_board_arrays()
        # Per-chip "lowest known-unstable freq" memory. None until a chip is
        # first found unstable; once set, monotonically decreases. UP moves
        # in _phase3_profiling are blocked at `target >= chip_max[b][i]`, so
        # a chip that thrashed unstable at e.g. 500 MHz can never climb back
        # to 500 — it stays at 493.75 (or wherever stable is found below the
        # cap). Prevents the per-chip oscillation hazard the iterative loop
        # otherwise has when a chip's true stability boundary sits between
        # two grid points. Reset on every fresh chip-tune (per voltage).
        self.chip_max = None
        # Phase 3 (iterative health tune) progress counters.
        # profiling_round: current round number, monotonically increasing.
        # profiling_completion_pct: monotonic 100 × round / MAX_PROFILING_ROUNDS,
        #   used by the dashboard's progress bar fill (climbs predictably).
        # chips_stable_pct: per-round signal — % of alive chips that did NOT
        #   move this round. Used by the dashboard text. Can go backward when
        #   previously-stable chips start moving again — that's intended.
        # stillness_streak: number of consecutive zero-move rounds; loop exits
        #   when this hits CHIP_TUNE_STILLNESS_STREAK.
        # All four round-trip through checkpoint so a mid-loop crash resumes
        # near where it left off rather than restarting at round 1.
        self.profiling_round = 0
        self.profiling_completion_pct = 0.0
        self.chips_stable_pct = 0.0
        self.chips_converged = 0
        self.chips_alive = 0
        self.stillness_streak = 0
        # True while inside Phase 3's iterative loop — persisted in checkpoint
        # so a crash mid-Phase-3 resumes at the saved round instead of restarting
        # the step from scratch.
        self.phase3_active = False
        # Phase 3b (stability polish) resume state. polish_round = 0 when the
        # phase hasn't started or has finished. While a polish round is in
        # flight the round counter is > 0; resume re-applies stable_freq_arrays
        # and continues from the saved round.
        self.polish_round = 0
        self.polish_active = False
        self.best_efficiency = None
        self.tuning_complete = False

        # ── Braiins firmware state (populated only when firmware_type == "braiins") ──
        # The wattage binary-search algorithm in braiins_phases.py uses these.
        # Each entry in wattage_results: {watt, hashrate_ths, power_w_actual,
        # efficiency_jth, profit_usd_per_day, fan_speed, ts}.
        self.wattage_results = []
        self.wattage_search_low = None
        self.wattage_search_high = None
        self.best_wattage_w = None

        # ── Whatsminer (stock MicroBT) firmware state (populated only when
        # firmware_type == "whatsminer") ──
        # The 2D power_limit × target_freq grid-search algorithm in
        # whatsminer_phases.py uses these. mode_baselines stores the per-mode
        # (low/normal/high) baseline freq + power readings discovered in
        # PHASE_WHATSMINER_DISCOVERY. freq_pct_anchor selects which interpretation
        # of set_target_freq's percent semantics to use (firmware behavior is
        # empirically verified in the discovery phase). Each entry in
        # whatsminer_results: {power_limit_w, target_freq_mhz, hashrate_ths,
        # power_w, efficiency_jth, measured_at, ...}.
        self.whatsminer_baselines = (
            None  # dict[mode -> {target_freq, freq_avg, power_w, supported}]
        )
        self.whatsminer_freq_pct_anchor = None  # "current_mode" | "normal_only" | None
        self.whatsminer_results = []
        self.whatsminer_pre_tune = None  # dict snapshot for restore-on-stop
        self.whatsminer_best_cell = None
        self._wm_current_mode = None
        self._wm_current_percent = None
        self._wm_current_power_limit = None
        self._wm_drift_streak = 0

        # Per-chip-tuned results (one entry per chip-tune target the dynamic
        # state machine has completed). voltage_results is the authoritative
        # log of what's been chip-tuned; the state machine queries it on every
        # iteration to decide whether more chip-tune work is pending.
        self.voltage_results = []
        # 2D (V, F_uniform) efficiency surface samples. The dynamic state
        # machine derives EVERYTHING (which cell to measure next, current
        # top-K, "is coarse done", "is fine done") from this surface plus
        # voltage_results. Each entry: {voltage_mv, freq_mhz, efficiency_jth|
        # None, hashrate_ths|None, power_w|None, fine, measured_at, kind?,
        # coarse_anchor?}. Pre-refactor profiles may carry legacy `stable` /
        # `board_healths_pct` fields — loaded as-is and ignored.
        self.vf_surface = []
        # In-flight chip-tune target — the SOLE additional persistent state
        # required by the dynamic state machine. None when no chip-tune is
        # running. When a chip-tune crashes or the process is killed mid-Phase-3,
        # _restore_saved_state loads this back, _run_inner picks it up and
        # resumes Phase 3 from the saved stable_freq_arrays / round state.
        # Schema: {voltage_mv, freq_mhz, vf_source}. Cleared after the cell's
        # voltage_results entry is appended.
        self.in_flight_chip_tune_target = None
        # Legacy fields — kept as empty defaults so dashboard/get_status code
        # paths that still reference them don't AttributeError. The dynamic
        # state machine does NOT use these for any decision; they're populated
        # for the dashboard's read-only view (top-K visualization, planned
        # grid rendering) but no longer authoritative for resume / sequencing.
        # Future cleanup can remove the dashboard reads and then these fields.
        self.vf_top_k_voltages = []
        self.vf_refinement_index = None
        self.vf_planned_grid = []
        self.vf_skipped = []
        self.vf_fine_anchor = None
        self.vf_coarse_rays_checked = []
        # Operator-queued remeasurements. Each entry: {voltage_mv, freq_mhz,
        # queued_at}. Appended by the POST /tuner/remeasure_cell endpoint from
        # the HTTP thread; drained by the tuning thread one cell per main-loop
        # iteration. The dynamic state machine never drains DURING an atomic
        # chip-tune (the user explicitly chose "remeasure waits, operator stop
        # preempts" semantics), so worst-case wait is one chip-tune duration.
        # Mutations go through _control_lock so add/drain don't race.
        self.remeasure_queue = []
        # The (V, F) point currently being measured by _measure_vf_point, or
        # None. Transient — not persisted in checkpoints (a resume after crash
        # means nothing is in-flight until the next _measure_vf_point call).
        self.current_vf_point = None
        # Active sweep profile — which voltage_results entry Phase 6 references.
        # None = auto-pick the most-efficient winner; integer = operator override.
        self.active_sweep_voltage_mv = None
        # Sweep reference mirrors the active profile (populated by _refresh_sweep_reference).
        # These are the *immutable baseline* Phase 6 tracks hashrate against and reverts to.
        self.sweep_voltage_mv = 0
        self.sweep_hashrate_ths = 0.0
        self.sweep_freq_arrays = self._empty_board_arrays()
        # Signed voltage delta from sweep_voltage_mv applied by Phase 6.
        self.voltage_adjustment_mv = 0
        # Unix ts of last Phase 6 restart; None = never restarted this session.
        self.last_restart_ts = None
        # Epoch seconds when the current voltage step began. Persisted in
        # checkpoint so duration_sec reflects true wall time across restarts
        # instead of only the final iteration. Cleared/overwritten at each
        # step start.
        self.current_step_started_at = None

        # Snapshot of config used at tune start — populated in Phase 0, restored
        # from disk on process restart. Preserved with voltage_results so
        # exports can attribute each tune to the exact config that produced it.
        self.config_snapshot = None

        # Per-board sets of chip indices "parked" as dead this voltage step.
        # Once parked, a chip is skipped in all subsequent Phase 3 rounds for
        # the current step — this is what preserves the high-water mark
        # convergence guarantee for chips that intermittently report
        # score > dead_score (oscillating between the dead and stable
        # branches would otherwise loop forever).
        self.parked_chips = [set(), set(), set()]

        # Restore saved state so dashboard reflects progress on restart
        self._restore_saved_state()

        # Live monitoring data
        self.last_summary = None
        self.last_hashrate = None
        self.last_clocks = None
        self.last_temps = None
        self.last_chip_temps = None
        self.last_update = 0

        # Rate-limit field for `_detect_thermal_emergency` (every long wait/
        # sample loop in Phase 2/V/3/3b/4 + monitor mode polls the detector;
        # 30s spacing is plenty for emergency response without spamming the
        # miner with /temps + /temps/chip reads).
        self._last_thermal_check = 0.0

        # MiningRigRentals sync state. `mrr_last_sync` holds the most recent
        # sync outcome for the dashboard: {intent, rig_id, result, ts,
        # target_status?, advertised_ths?, advertised_unit?, error?, reason}.
        # Round-trips through profile + checkpoint so the dashboard shows the
        # last-known state on restart. `_mrr_phase6_announced` is a once-per-
        # Phase-6-session flag — flipped true on first Phase 6 iteration's
        # sync, cleared on any exit from Phase 6 (stop, retune, error) so the
        # next Phase 6 entry fires a fresh maintaining sync. `_mrr_last_warn_ts`
        # rate-limits the "creds missing" log message to once per hour.
        self.mrr_last_sync = None
        self._mrr_phase6_announced = False
        self._mrr_polish_announced = False
        self._mrr_last_warn_ts = 0.0

    def _stock_file(self):
        return persistence.stock_file(self)

    def _save_stock_baseline(self):
        return persistence.save_stock_baseline(self)

    def _load_stock_baseline(self):
        return persistence.load_stock_baseline(self)

    def _empty_board_arrays(self, n=None):
        """Allocate a fresh list-of-empty-lists sized to the miner's board count.
        Use this instead of `[[], [], []]` literals so the tuner works for
        miners with non-default board counts (L7 has 3, but other models vary).
        Call with explicit `n` before `/capabilities` has run; otherwise uses
        `self.num_boards` (which Phase 0 overwrites from the live miner)."""
        n = n if n is not None else self.num_boards
        return [[] for _ in range(n)]

    def _resize_board_arrays(self):
        """Reshape any board-dimensional array whose outer length doesn't match
        `self.num_boards`. Called at the end of `_phase0_discovery` once the
        live board count is known. Arrays shorter than the true board count
        get padded with empty lists; arrays longer get truncated. Chip-dim
        per-board lists are preserved as-is (Phase 2/3 allocators use
        `self.chips_per_board` when rebuilding). For cross-hardware resume
        (saved 3-board profile loaded on a 4-board miner), upstream restore
        logic should also clear stale tuning state — this helper only ensures
        the outer shape is sane so downstream `range(self.num_boards)` loops
        don't IndexError."""
        n = self.num_boards

        def _pad_or_truncate(arr):
            if not isinstance(arr, list):
                return [[] for _ in range(n)]
            if len(arr) == n:
                return arr
            if len(arr) < n:
                return arr + [[] for _ in range(n - len(arr))]
            return arr[:n]

        self.baseline_scores = _pad_or_truncate(self.baseline_scores)
        self.baseline_chip_temps = _pad_or_truncate(self.baseline_chip_temps)
        self.baseline_chip_hashrates = _pad_or_truncate(self.baseline_chip_hashrates)
        self.baseline_freq_arrays = _pad_or_truncate(self.baseline_freq_arrays)
        self.stable_freq_arrays = _pad_or_truncate(self.stable_freq_arrays)
        self.proposed_freqs = _pad_or_truncate(self.proposed_freqs)
        self.sweep_freq_arrays = _pad_or_truncate(self.sweep_freq_arrays)
        # parked_chips is list-of-sets, not list-of-lists
        if isinstance(self.parked_chips, list) and len(self.parked_chips) != n:
            if len(self.parked_chips) < n:
                self.parked_chips = self.parked_chips + [
                    set() for _ in range(n - len(self.parked_chips))
                ]
            else:
                self.parked_chips = self.parked_chips[:n]

    def _restore_saved_state(self):
        return persistence.restore_saved_state(self)

    def log(self, msg, level="INFO"):
        return logging_.log(self, msg, level)

    def _get_scoring_context(self):
        return scoring.get_scoring_context(self)

    def _get_profit_display_context(self):
        return scoring.get_profit_display_context(self)

    def _score_key(self, ctx=None):
        return scoring.score_key(self, ctx)

    def _load_log_from_disk(self):
        return logging_.load_log_from_disk(self)

    def _rotate_log_if_needed_locked(self, path):
        return logging_._rotate_log_if_needed_locked(self, path)

    def clear_log_entries_for_voltage(self, voltage_mv):
        return logging_.clear_log_entries_for_voltage(self, voltage_mv)

    def start(self):
        return lifecycle.start(self)

    def stop(self):
        return lifecycle.stop(self)

    def destroy(self):
        return lifecycle.destroy(self)

    def start_retune(self, voltage_mv):
        return retune.start_retune(self, voltage_mv)

    def _retune_runner(self, voltage_mv):
        return retune.retune_runner(self, voltage_mv)

    # ── Remeasure queue ──
    #
    # Operators can click a cell in the V/F heatmap and enqueue it for
    # remeasurement (e.g. to retry a no-data cell that hit an API failure
    # the first time around). Adds go through the HTTP thread, drains run
    # on the tuning thread — _control_lock serializes both.
    #
    # Drains happen automatically at the top of _phase_vf_exploration and
    # between ray walks. When the engine is otherwise stopped, the operator
    # can trigger _remeasure_runner explicitly via the "Process queue" UI
    # button.

    @staticmethod
    def _remeasure_key(entry):
        return lifecycle.remeasure_key(entry)

    def enqueue_remeasure(self, voltage_mv, freq_mhz):
        return lifecycle.enqueue_remeasure(self, voltage_mv, freq_mhz)

    def clear_remeasure_queue(self):
        return lifecycle.clear_remeasure_queue(self)

    def _drain_remeasure_queue(self):
        return lifecycle.drain_remeasure_queue(self)

    def start_remeasure_queue(self):
        return lifecycle.start_remeasure_queue(self)

    def _remeasure_runner(self):
        return lifecycle.remeasure_runner(self)

    # ── Main Run Loop ──

    RETRY_BACKOFF_BASE = 60  # seconds
    SUCCESSFUL_RUN_SEC = 300  # _run_inner running > this counts as recovery success
    # Consecutive Phase 6 cycles that hit MinerOfflineError before escalating to
    # _run()'s offline-mode machinery. At the default 10-min cycle, 3 = ~30 min
    # of sustained outage before the dashboard flips to "miner offline".
    PHASE6_OFFLINE_THRESHOLD = 3
    # Consecutive Phase 0 entries that raised MinerOfflineError before the
    # circuit breaker flips the engine to PHASE_ERROR. Targets the LuxOS
    # storm-and-die loop where each Phase 0 attempt fires ~25 TCP cmds and
    # causes port 4028 to start refusing connections; without this guard
    # the engine cycles forever because wait_for_miner_online resets
    # offline_hits after a successful reconnect, then Phase 0 storms again.
    PHASE0_CIRCUIT_BREAKER_THRESHOLD = 5

    def _enter_offline_mode(self, reason):
        return recovery.enter_offline_mode(self, reason)

    def _wait_for_miner_online(self):
        return recovery.wait_for_miner_online(self)

    def _reset_to_safe_vf(self):
        return reset.reset_to_safe_vf(self)

    def _attempt_miner_recovery(self, retry_num):
        return recovery.attempt_miner_recovery(self, retry_num)

    def _run(self):
        retries = 0
        offline_hits = 0
        while self.running:
            try:
                self._run_inner()
                return  # clean exit
            except UnsafeVoltageBoundsError as e:
                # A provenance failure is deterministic and safety-critical;
                # retries/reboots cannot turn a static fallback into live PSU
                # evidence and could trigger a pre-Phase-0 voltage reset.
                self.phase = self.PHASE_ERROR
                self.phase_detail = f"Safety stop: {e}"
                self.log(f"FATAL safety stop: {e}")
                self._mrr_phase6_announced = False
                self._mrr_polish_announced = False
                return
            except MinerOfflineError as e:
                # Miner is unreachable. Filter transient blips by requiring
                # OFFLINE_FAILURE_THRESHOLD consecutive hits before flipping
                # the dashboard to PHASE_OFFLINE. Post-threshold, pause cleanly
                # and wait for reconnect — no retry-budget consumption.
                # Phase-0-specific circuit breaker: if the offline error fired
                # while we were still in Phase 0, increment a separate counter
                # that survives wait_for_miner_online's reset. After
                # PHASE0_CIRCUIT_BREAKER_THRESHOLD consecutive Phase 0
                # storm-and-die cycles, escalate to PHASE_ERROR rather than
                # cycling forever. Reaching set_perpetualtune resets this.
                if self.phase == self.PHASE_DISCOVERY:
                    self._phase0_consecutive_offline_hits += 1
                if self._phase0_consecutive_offline_hits >= self.PHASE0_CIRCUIT_BREAKER_THRESHOLD:
                    self.phase = self.PHASE_ERROR
                    self.phase_detail = (
                        f"Phase 0 cannot complete after "
                        f"{self._phase0_consecutive_offline_hits} consecutive offline failures "
                        f"— miner repeatedly refusing connections. Check miner load "
                        f"or restart firmware, then click Start."
                    )
                    self.log(
                        f"FATAL: Phase 0 circuit breaker tripped after "
                        f"{self._phase0_consecutive_offline_hits} consecutive offline "
                        f"failures ({e})"
                    )
                    self._mrr_phase6_announced = False
                    self._mrr_polish_announced = False
                    self._mrr_sync(
                        "error",
                        reason=f"Phase 0 storm-and-die: {e}",
                    )
                    return
                offline_hits += 1
                threshold = max(1, int(self.config.get("OFFLINE_FAILURE_THRESHOLD", 3)))
                if offline_hits < threshold:
                    self.log(
                        f"Connection failure {offline_hits}/{threshold} — retrying shortly ({e})"
                    )
                    remaining = 10
                    while remaining > 0 and self.running:
                        time.sleep(min(remaining, 5))
                        remaining -= 5
                    continue
                self._enter_offline_mode(str(e))
                self._wait_for_miner_online()
                offline_hits = 0
                continue
            except MinerCommandPending as e:
                # Firmware back-pressure — not a failure. The prior command is
                # still being processed (chip-by-chip ramp on 324 chips can
                # take tens of seconds). Wait a bit and re-enter _run_inner()
                # WITHOUT invoking _attempt_miner_recovery and WITHOUT setting
                # last_exit_was_recovery — we don't want to bounce voltage to
                # BASELINE_VOLTAGE_MV just because the firmware was busy.
                # Don't consume retry budget either; these can cluster when the
                # miner is mid-transition.
                self.log(f"Firmware busy ({e}) — waiting 30s and retrying without recovery")
                offline_hits = 0
                if self.start_voltage_mv > 0:
                    self._save_checkpoint()
                self.phase_detail = "Firmware busy, waiting to retry"
                remaining = 30
                while remaining > 0 and self.running:
                    time.sleep(min(remaining, 5))
                    remaining -= 5
                continue
            except (MinerNotReady, MinerCommandError) as e:
                # If the miner was confirmed in a good state more than
                # SUCCESSFUL_RUN_SEC ago and is failing now, the previous
                # recovery worked — reset counter. The timestamp is set
                # inside _run_inner only after _reset_to_safe_vf or Phase 0
                # actually succeed, so a long-failing iteration (e.g. a
                # 20-min Phase 1 settle that never reaches good state)
                # leaves _iteration_confirmed_good_at None and retries
                # accumulate properly. Without this, run_duration alone
                # (the old check) caused the counter to reset every
                # iteration of an infinite recovery loop.
                confirmed_good = self._iteration_confirmed_good_at
                if (
                    retries > 0
                    and confirmed_good is not None
                    and (time.time() - confirmed_good) > self.SUCCESSFUL_RUN_SEC
                ):
                    elapsed_min = int((time.time() - confirmed_good) / 60)
                    self.log(
                        f"Previous recovery succeeded ({elapsed_min} min of good-state operation), resetting retry counter"
                    )
                    retries = 0
                # Non-offline error means we successfully reached the miner —
                # reset the offline noise filter.
                offline_hits = 0
                retries += 1
                max_retries = self.config.get("MAX_CONSECUTIVE_RETRIES", 5)
                if retries > max_retries:
                    self.phase = self.PHASE_ERROR
                    self.phase_detail = f"Gave up after {retries} consecutive retries: {e}"
                    self.log(f"FATAL: {e} (retry {retries}/{max_retries})")
                    self._mrr_phase6_announced = False
                    self._mrr_polish_announced = False
                    self._mrr_sync("error", reason=f"Exceeded {max_retries} retries: {e}")
                    return
                self.log(f"Recoverable error: {e}")
                self.log(f"Retry {retries}/{max_retries}")
                self.phase_detail = f"Recovering miner (retry {retries}/{max_retries})"
                # Save in-progress checkpoint so _run_inner() can resume
                # instead of restarting from Phase 0
                if self.start_voltage_mv > 0:
                    self.log(
                        f"Saving retry checkpoint (vf_surface={len(self.vf_surface)} points, "
                        f"round={self.profiling_round})"
                    )
                    self._save_checkpoint()
                self._attempt_miner_recovery(retries)
                # Tell _run_inner to gate its next iteration on a
                # hashrate-recovery check and safe-V/F reset before
                # trusting the miner.
                self.last_exit_was_recovery = True
                # Backoff after recovery attempt
                backoff = self.RETRY_BACKOFF_BASE * retries
                self.phase_detail = (
                    f"Waiting {backoff}s after recovery attempt ({retries}/{max_retries})"
                )
                remaining = backoff
                while remaining > 0 and self.running:
                    time.sleep(min(remaining, 10))
                    remaining -= 10
            except Exception as e:
                self.phase = self.PHASE_ERROR
                self.phase_detail = str(e)
                self.log(f"FATAL unrecoverable error: {e}")
                self.log(traceback.format_exc())
                self._mrr_phase6_announced = False
                self._mrr_polish_announced = False
                self._mrr_sync("error", reason=f"Unrecoverable: {e}")
                return

    def _run_inner(self):
        """Dynamic state machine. Each iteration picks the next thing to do
        based on current vf_surface + voltage_results + current settings:

          if baseline incomplete:        do baseline step
          elif find_next_coarse():       measure that coarse cell
          elif find_next_fine():         measure that fine cell
          elif find_next_chip_tune():    chip-tune that cell (atomic Phase 3+3b+4)
          else:                          monitor cycle (one PERPETUAL_VOLTAGE_CHECK_MIN tick)

        Top-K is recomputed every iteration. Settings changes between
        iterations take effect immediately. The atomic chip-tune is preempted
        by operator stop (self.running checks inside Phase 3) but not by
        settings changes mid-cell. Remeasure queue drains one cell per
        iteration BUT does not interrupt an in-flight chip-tune.
        """
        # Post-recovery safe-V/F reset: the retry loop sets
        # last_exit_was_recovery after a start_mining/reboot. Re-apply a
        # known-safe BASELINE_VOLTAGE_MV + BASELINE_FREQ so subsequent
        # measurements don't run on whatever unstable state the firmware
        # was carrying when the recovery fired.
        #
        # We deliberately do NOT gate on hashrate climbing back to some
        # fraction of stock first. An unstable tune cell can leave the
        # miner hashing well below stock immediately post-recovery, which
        # is exactly when we MOST want to apply safe V/F — gating on
        # hashrate first would falsely classify that as a recovery
        # failure, time out after 600s, and loop the outer retry counter
        # forever (the 600s wait exceeded the old duration-based
        # SUCCESSFUL_RUN_SEC reset, so MAX_CONSECUTIVE_RETRIES never
        # escalated to FATAL).
        #
        # If _reset_to_safe_vf itself fails (miner stuck Idling, boards
        # crashed, etc.), it raises MinerNotReady and the outer retry
        # loop escalates normally — _iteration_confirmed_good_at stays
        # None for this iteration, so the timestamp-based reset doesn't
        # incorrectly clear the retry counter.
        if self.last_exit_was_recovery and self.running:
            self._reset_to_safe_vf()
            self.last_exit_was_recovery = False
            self._iteration_confirmed_good_at = time.time()

        # Phase 0 always runs at the top of every retry loop entry: idempotent,
        # detects hardware changes (cross-hardware swap), captures stock
        # baseline on first run, kicks the miner from Stopped -> Mining if it
        # was halted between sessions. Cheap (one /capabilities + one /summary).
        self._phase0_discovery()
        if not self.running:
            return
        # Phase 0 completed: wait_for_mining_state returned, meaning the
        # miner is in Mining/Initializing and responsive to commands. This
        # is the canonical "miner is good" checkpoint for the outer retry
        # loop's timestamp-based counter reset. Failures BEFORE this point
        # leave _iteration_confirmed_good_at None and let retries accumulate
        # toward FATAL; failures AFTER this point are downstream issues
        # (slow Phase 1 settle to an unreachable cell, mid-tune thermal,
        # etc.) and the timestamp lets the retry counter reset normally
        # once a meaningful recovery interval has passed.
        self._iteration_confirmed_good_at = time.time()

        # Vendor split: Braiins miners use a 1D wattage binary-search algorithm
        # (BOS firmware owns the V/F tuner internally). The dedicated loop handles
        # discovery → search → perpetual transitions and never returns until
        # self.running flips False.
        if self.api.tuning_strategy() == "wattage_search":
            from tuner_app.tuning_engine import braiins_phases

            braiins_phases.run_braiins_loop(self)
            return

        # Whatsminer (stock MicroBT) split: 2D power_limit × target_freq grid-search.
        # Firmware quantizes target_freq into mode (low/normal/high) + percent.
        # The dedicated loop handles discovery → coarse → fine → perpetual transitions.
        if self.api.tuning_strategy() == "power_limit_freq_search":
            from tuner_app.tuning_engine import whatsminer_phases

            whatsminer_phases.run_whatsminer_loop(self)
            return

        # Resume in-flight chip-tune if a prior run was killed mid-Phase-3.
        # in_flight_chip_tune_target is the only persistent state needed for
        # crash safety here — when set, we know exactly which cell to resume,
        # and stable_freq_arrays + profiling_round + stillness_streak +
        # polish_round in the checkpoint provide the iterative-loop state to
        # pick up from.
        if self.in_flight_chip_tune_target is not None:
            target = dict(self.in_flight_chip_tune_target)
            self.log(
                f"Resume: picking up in-flight chip-tune at "
                f"{target.get('voltage_mv')} mV / "
                f"{target.get('freq_mhz', 0):.1f} MHz"
            )
            self._do_chip_tune_atomic(target, fresh_start=False)
            # Only clear the resume marker on normal completion. If running
            # flipped to False mid-tune (operator Stop), Phase 3's while loop
            # exited cleanly without finishing the cell — preserve in_flight
            # so the next Start resumes at the saved profiling_round instead
            # of restarting at round 1 with a freshly-seeded stable_freq_arrays.
            if self.running:
                self.in_flight_chip_tune_target = None
            self._save_checkpoint()

        # ── Main dynamic loop ──
        while self.running:
            # Drain at most ONE remeasure cell per iteration. Operator-queued
            # remeasurements take priority over coarse/fine work but cannot
            # monopolize a busy engine — interleaving with monitor cycles
            # ensures a long queue (e.g. 10 cells × 3 min each = 30 min) doesn't
            # fully starve voltage adjustment.
            if self.remeasure_queue and self._drain_one_remeasure():
                self._maybe_clear_tune_complete("Remeasure changed surface")
                continue

            # Stage 1: baseline.
            if not self._is_baseline_done():
                self._do_baseline_step()
                if not self.running:
                    return
                self._save_checkpoint()
                self._maybe_clear_tune_complete("Baseline collected")
                continue

            # Stage 2: coarse rays. Returns the next (V, F) coarse cell to
            # measure based on top-VF_COARSE_TOP_K_RAYS cells (by current
            # scoring) walking 8 rays each. None when every direction from
            # every top-K cell has trend-stopped or hit the grid edge.
            next_coarse = self._find_next_coarse_to_measure()
            if next_coarse is not None:
                v_mv, f_mhz = next_coarse
                self._maybe_clear_tune_complete("New coarse work appeared")
                self.current_step_started_at = time.time()
                result = self._measure_vf_point(v_mv, f_mhz, fine=False)
                if result is not None:
                    self.vf_surface.append(result)
                self._save_checkpoint()
                continue

            # Stage 3: fine grid. Top-VF_FINE_TOP_K coarse anchors get N×N
            # fine grids. Returns (v_mv, f_mhz, anchor_meta) or None.
            next_fine = self._find_next_fine_to_measure()
            if next_fine is not None:
                v_mv, f_mhz, anchor_meta = next_fine
                self._maybe_clear_tune_complete("New fine work appeared")
                self.current_step_started_at = time.time()
                result = self._measure_vf_point(v_mv, f_mhz, fine=True)
                if result is not None:
                    result["kind"] = "fine"
                    result["coarse_anchor"] = dict(anchor_meta)
                    self.vf_surface.append(result)
                self._save_checkpoint()
                continue

            # Stage 4: chip-tune. Top-VF_EXPLORE_TOP_K candidates from inside
            # the top-VF_FINE_TOP_K coarse anchors' fine grids (or top coarse
            # cells when fine is disabled). Returns target descriptor or None.
            next_chip = self._find_next_chip_tune_target()
            if next_chip is not None:
                self._maybe_clear_tune_complete("New chip-tune work appeared")
                self.in_flight_chip_tune_target = dict(next_chip)
                self._save_checkpoint()
                self._do_chip_tune_atomic(next_chip, fresh_start=True)
                # See the resume branch above: keep in_flight set when Stop
                # preempted the cell so the next Start resumes mid-Phase-3.
                if self.running:
                    self.in_flight_chip_tune_target = None
                self._save_checkpoint()
                if not self.running:
                    return
                continue

            # Stage 5: monitor (else branch). All find_next_* returned None →
            # exploration is settled. Mark tune complete (saves profile + fires
            # MRR maintaining once), then run one monitor cycle. Loop iterates
            # back to top after the cycle so settings changes that expose new
            # work cause monitor to exit naturally.
            if not self.tuning_complete:
                self._mark_tune_complete()
            self._do_monitor_cycle()

    # ── Dynamic state-machine helpers ──
    #
    # Eight-ray walk directions. Ridge-first (NE / SW = stability ridge
    # diagonals) so the most-likely-to-improve directions are tried before
    # the off-ridge diagonals (NW = wasted voltage, SE = unstable).

    RAY_DIRECTIONS = (
        ("V+F+", +1, +1),  # NE — stability ridge
        ("V-F-", -1, -1),  # SW — stability ridge
        ("V+", +1, 0),
        ("V-", -1, 0),
        ("F+", 0, +1),
        ("F-", 0, -1),
        ("V+F-", +1, -1),  # NW — off-ridge
        ("V-F+", -1, +1),  # SE — off-ridge
    )

    def _is_baseline_done(self):
        """Baseline is "done" when at least one board has health scores
        recorded. The dynamic loop never re-runs baseline if scores exist;
        operator must Reset Profile to force a re-collect."""
        if not self.baseline_scores:
            return False
        for b in range(self.num_boards):
            if (
                b < len(self.baseline_scores)
                and self.baseline_scores[b]
                and len(self.baseline_scores[b]) > 0
            ):
                return True
        return False

    def _do_baseline_step(self):
        """Phase 1 (set baseline V/F) + Phase 2 (collect health samples).
        Run as one unit — partial baselines aren't useful, and stop_mining
        only happens once at start, so this isn't preemptable mid-collection
        (Phase 2's sampling loop honors self.running for stop)."""
        baseline_voltage = self.config["BASELINE_VOLTAGE_MV"]
        baseline_freq = self.config["BASELINE_FREQ"]
        self.log("Restart before baseline — ensuring clean chip state")
        self._restart_between_probes()
        if not self.running:
            return
        self.log(
            f"Taking baseline at stable conditions: {baseline_voltage} mV / {baseline_freq} MHz"
        )
        self._phase1_set_voltage(baseline_voltage, baseline_freq)
        if not self.running:
            return
        self._phase2_baseline()

    def _drain_one_remeasure(self):
        return lifecycle.drain_one_remeasure(self)

    def _vf_grid_axes(self):
        return grid.vf_grid_axes(self)

    def _vf_surface_by_key(self):
        return grid.vf_surface_by_key(self)

    def _coarse_cells_ranked(self, score_ctx=None):
        return grid.coarse_cells_ranked(self, score_ctx)

    def _next_unmeasured_in_direction(
        self,
        origin_v_idx,
        origin_f_idx,
        dv,
        df,
        v_grid_asc,
        f_grid_asc,
        by_key,
        best_score,
        trend_n,
        score_ctx,
    ):
        return exploration.next_unmeasured_in_direction(
            self,
            origin_v_idx,
            origin_f_idx,
            dv,
            df,
            v_grid_asc,
            f_grid_asc,
            by_key,
            best_score,
            trend_n,
            score_ctx,
        )

    def _top_coarse_ray_origins(self, score_ctx, top_k_rays):
        return grid.top_coarse_ray_origins(self, score_ctx, top_k_rays)

    def _find_next_coarse_to_measure(self):
        return exploration.find_next_coarse_to_measure(self)

    def _fine_axis_offsets(self, anchor, step, lo_bound, hi_bound, n):
        return grid.fine_axis_offsets(self, anchor, step, lo_bound, hi_bound, n)

    def _fine_cell_offsets_for_anchor(
        self, anchor_v, anchor_f, v_step, f_step, fine_count, v_min, v_max, f_min, f_max
    ):
        return grid.fine_cell_offsets_for_anchor(
            self, anchor_v, anchor_f, v_step, f_step, fine_count, v_min, v_max, f_min, f_max
        )

    def _top_fine_anchors(self, score_ctx, fine_top_k):
        return grid.top_fine_anchors(self, score_ctx, fine_top_k)

    def _find_next_fine_to_measure(self):
        return exploration.find_next_fine_to_measure(self)

    def _chip_tune_already_done_for(self, cell):
        return exploration.chip_tune_already_done_for(self, cell)

    def _find_next_chip_tune_target(self):
        return exploration.find_next_chip_tune_target(self)

    def _do_chip_tune_atomic(self, target, fresh_start=True):
        return chip_tune_orchestration.do_chip_tune_atomic(self, target, fresh_start)

    def _do_monitor_cycle(self):
        return monitor.do_monitor_cycle(self)

    def _do_monitor_cycle_body(self):
        return monitor.do_monitor_cycle_body(self)

    def _mark_tune_complete(self):
        """All find_next_* returned None: exploration is settled. Save profile,
        delete the now-stale checkpoint, fire MRR maintaining (handled by
        _do_monitor_cycle's first-entry block). Idempotent — safe to call
        repeatedly; only first call does work."""
        if self.tuning_complete:
            return
        self.tuning_complete = True
        # Pick the winner across voltage_results so active_sweep_voltage_mv
        # gets a sensible default if the operator hasn't pinned anything.
        if self.voltage_results:
            try:
                winner = min(self.voltage_results, key=self._score_key())
                self.best_efficiency = winner.get("efficiency_jth")
                if self.active_sweep_voltage_mv is None:
                    self.active_sweep_voltage_mv = winner.get("voltage_mv")
                self.log(
                    f"=== Tune settled: winner {winner.get('voltage_mv')} mV "
                    f"({winner.get('efficiency_jth', 0):.2f} J/TH, "
                    f"{winner.get('hashrate_ths', 0):.1f} TH/s, "
                    f"{winner.get('power_w', 0):.0f} W). "
                    f"{len(self.voltage_results)} chip-tuned cell(s) total ==="
                )
            except (ValueError, TypeError):
                pass
        else:
            self.log(
                "=== Tune settled: no chip-tunes yet "
                "(top-K not reached or all measurements no-data) ==="
            )
        try:
            self._save_profile()
            self._delete_checkpoint()
        except Exception as ex:
            self.log(f"Profile save failed: {ex}")

    def _maybe_clear_tune_complete(self, reason):
        """Re-entry path: a measurement, settings change, or remeasurement
        revealed work that wasn't there before. Flip tuning_complete back to
        False so the next monitor entry will re-fire profile save + MRR
        maintaining once the new work settles."""
        if self.tuning_complete:
            self.log(f"Tune-complete cleared: {reason}")
            self.tuning_complete = False
            self._mrr_phase6_announced = False  # Re-announce MRR on next monitor entry
            self._mrr_polish_announced = False

    # ── Phase V: 2D (voltage, uniform-frequency) efficiency exploration ──
    #
    # Samples the (V, F) efficiency surface cheaply at uniform frequencies
    # BEFORE any per-chip tuning runs, so per-chip Phase 3 only runs at the
    # top-K voltages identified by Phase V. This replaces the old 1D voltage
    # descent whose "max-stable F at each V" assumption missed the true
    # global minimum on miners where the surface isn't monotone.

    def _apply_uniform_freq(self, freq_mhz):
        return apply.apply_uniform_freq(self, freq_mhz)

    def _measure_vf_point(self, v_mv, f_mhz, fine=False):
        return measurement.measure_vf_point(self, v_mv, f_mhz, fine)

    def _measure_vf_point_inner(self, v_mv, f_mhz, fine, wait, n_samples, sample_interval):
        return measurement.measure_vf_point_inner(
            self, v_mv, f_mhz, fine, wait, n_samples, sample_interval
        )

    def _build_vf_grid_voltages(self):
        return grid.build_vf_grid_voltages(self)

    def _build_vf_grid_freqs(self, f_min=None, f_max=None, count=None):
        return grid.build_vf_grid_freqs(self, f_min, f_max, count)

    def _phase_vf_exploration(self):
        return phase_vf_exploration_mod.phase_vf_exploration(self)

    def _fine_grid_around_cell(self, center_v_mv, center_f_mhz):
        """Run an N×N fine grid around a coarse-grid cell. Used outside
        Phase V's ray walk — e.g. by the profit recompute workflow when a
        coarse-cell winner needs fine-gridding before chip-tuning.

        Builds the coarse V/F grid from current config to derive the fine
        step size, measures each fine cell, stamps kind='fine' and
        coarse_anchor, appends to self.vf_surface. R3: drops the coarse
        center's surface entry so the higher-resolution fine reads replace
        it in every ranking. Sets self.vf_fine_anchor. Saves checkpoint
        after each measurement so a crash mid-grid resumes cleanly.

        No-op if VF_EXPLORE_FINE_COUNT < 2 — fine grid must be enabled in
        config or there's nothing to do. No-op if the grid collapses after
        PSU/firmware clamping (edge cell + snap dedup).

        Does NOT re-enter the coarse walk or mark top-K entries
        `fine_gridded` — this is a single fine-grid pass for out-of-Phase-V
        callers (e.g. profit recompute).
        """
        fine_count = int(self.config.get("VF_EXPLORE_FINE_COUNT", 0))
        if fine_count < 2:
            self.log(f"Fine grid skipped: VF_EXPLORE_FINE_COUNT={fine_count} (must be >= 2)")
            return
        v_grid = self._build_vf_grid_voltages()
        f_grid = self._build_vf_grid_freqs()
        v_grid_asc = sorted(set(int(v) for v in v_grid))
        f_grid_asc = sorted({round(float(f), 3) for f in f_grid})
        if len(v_grid_asc) < 2 or len(f_grid_asc) < 2:
            self.log(
                f"Fine grid skipped: coarse grid too small "
                f"({len(v_grid_asc)} V × {len(f_grid_asc)} F)"
            )
            return
        v_step = (v_grid_asc[-1] - v_grid_asc[0]) / max(1, len(v_grid_asc) - 1)
        f_step = (f_grid_asc[-1] - f_grid_asc[0]) / max(1, len(f_grid_asc) - 1)

        # R1 in-cell subdivision: strictly inside ±step/2 of the center.
        center_v_mv = int(center_v_mv)
        center_f_mhz = float(center_f_mhz)
        v_fine_raw = [
            int(round(center_v_mv + ((i - (fine_count - 1) / 2) / fine_count) * v_step))
            for i in range(fine_count)
        ]
        f_fine_raw = [
            center_f_mhz + ((i - (fine_count - 1) / 2) / fine_count) * f_step
            for i in range(fine_count)
        ]
        # Clamp + snap to firmware grid; dedupe.
        psu_max = self.psu_max_mv or self.config.get("PERPETUAL_VOLTAGE_MAX_DELTA_MV", 15200)
        v_fine = sorted({max(11877, min(psu_max, v)) for v in v_fine_raw}, reverse=True)
        f_fine = sorted(
            {
                max(
                    self.config["VF_EXPLORE_F_MIN"],
                    min(self.config["VF_EXPLORE_F_MAX"], round(f / 3.125) * 3.125),
                )
                for f in f_fine_raw
            },
            reverse=True,
        )
        if len(v_fine) < 2 or len(f_fine) < 2:
            self.log(
                f"Fine grid skipped — after clamp+snap+dedup only "
                f"{len(v_fine)} V × {len(f_fine)} F distinct points"
            )
            return

        self.log("")
        self.log(
            f"=== Fine grid around ({center_v_mv} mV, {center_f_mhz:.1f} MHz): "
            f"{len(v_fine)}×{len(f_fine)} exhaustive points ==="
        )

        # R3: drop the coarse reading at the center — the fine grid is higher
        # resolution and the coarse entry should not compete in any downstream
        # ranking or heatmap display.
        winner_key = (center_v_mv, round(center_f_mhz, 3))
        self.vf_surface = [
            e
            for e in self.vf_surface
            if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) != winner_key
        ]
        self.vf_planned_grid = [
            p
            for p in self.vf_planned_grid
            if (int(p["voltage_mv"]), round(float(p["freq_mhz"]), 3)) != winner_key
        ]
        # Build a fast lookup for already-measured fine cells so resume doesn't
        # re-measure them.
        measured_keys = {
            (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) for e in self.vf_surface
        }

        self.vf_fine_anchor = {
            "voltage_mv": center_v_mv,
            "freq_mhz": round(center_f_mhz, 3),
        }
        anchor_snapshot = dict(self.vf_fine_anchor)

        # Publish fine cells as planned before measurement for dashboard rendering.
        planned_keys = {
            (p["voltage_mv"], round(float(p["freq_mhz"]), 3)) for p in self.vf_planned_grid
        }
        fine_keys_set = {(int(v), round(float(f), 3)) for v in v_fine for f in f_fine}
        for v in v_fine:
            for f in f_fine:
                k = (int(v), round(float(f), 3))
                if k in planned_keys:
                    continue
                self.vf_planned_grid.append(
                    {
                        "voltage_mv": int(v),
                        "freq_mhz": round(float(f), 3),
                        "fine": True,
                        "coarse_anchor": dict(anchor_snapshot),
                    }
                )
                planned_keys.add(k)
        # Un-skip any fine cells that were previously marked off-envelope.
        self.vf_skipped = [
            e
            for e in self.vf_skipped
            if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) not in fine_keys_set
        ]
        self._save_checkpoint()

        # Measure each fine cell (skip already-measured for resume safety).
        for v_mv in v_fine:
            if not self.running:
                return
            for f_mhz in f_fine:
                if not self.running:
                    return
                key = (int(v_mv), round(float(f_mhz), 3))
                if key in measured_keys:
                    continue
                result = self._measure_vf_point(v_mv, f_mhz, fine=True)
                if result is None:
                    return
                result["kind"] = "fine"
                result["coarse_anchor"] = dict(anchor_snapshot)
                self.vf_surface.append(result)
                measured_keys.add(key)
                self._save_checkpoint()
        self.log(f"Fine grid around ({center_v_mv} mV, {center_f_mhz:.1f} MHz) complete")

    def start_fine_then_retune(self, voltage_mv, freq_mhz):
        return retune.start_fine_then_retune(self, voltage_mv, freq_mhz)

    def _fine_then_retune_runner(self, voltage_mv, freq_mhz):
        return retune.fine_then_retune_runner(self, voltage_mv, freq_mhz)

    def _run_phase3_phase4_at_voltage(
        self, voltage_mv, seed_f_mhz, fresh_start=True, vf_source=None
    ):
        return chip_tune_orchestration.run_phase3_phase4_at_voltage(
            self, voltage_mv, seed_f_mhz, fresh_start=fresh_start, vf_source=vf_source
        )

    def _restart_between_probes(self):
        return retune.restart_between_probes(self)

    # ── Phase 0: Discovery ──

    # Bitmain advertised spec — fallback only when the miner isn't mining.
    STOCK_SPEC = {
        "hashrate_ths": 200,
        "power_w": 3500,
        "efficiency_jth": 17.5,
        "voltage_mv": 14000,
        "freq_mhz": 490,
    }

    def _capture_live_stock_baseline(self, initial_summary):
        return lifecycle.capture_live_stock_baseline(self, initial_summary)

    def _phase0_discovery(self):
        return phase_runners.phase0_discovery(self)

    # ── Phase 1: Set Voltage & Baseline Frequency ──

    def _phase1_set_voltage(self, voltage_mv, freq_mhz):
        return apply.phase1_set_voltage(self, voltage_mv, freq_mhz)

    def _get_current_voltage_mv(self):
        """Read current PSU target voltage in mV."""
        summary = self.api.summary()
        if summary:
            return summary.target_voltage_mv or 0
        return 0

    def _wait_for_voltage_settle(self, target_mv):
        return apply.wait_for_voltage_settle(self, target_mv)

    def _wait_for_clock_settle(self, target_means, tolerance_mhz=5.0):
        return apply.wait_for_clock_settle(self, target_means, tolerance_mhz)

    def _wait_for_settle(self, target_freq):
        return apply.wait_for_settle(self, target_freq)

    # ── Phase 2: Baseline Scoring ──

    def _phase2_baseline(self):
        return phase_runners.phase2_baseline(self)

    def _park_dead_chips_from_baseline(self):
        return reset.park_dead_chips_from_baseline(self)

    # ── Phase 3: Iterative Per-Chip Health Tune ──

    def _collect_chip_health_samples(self, num_samples, interval, label):
        return chip_tune_loop.collect_chip_health_samples(self, num_samples, interval, label)

    def _phase3_profiling(self, seed_f_mhz):
        return chip_tune_loop.phase3_profiling(self, seed_f_mhz)

    # ── Phase 3b: Stability Polish (decrement-only) ──

    def _phase3b_polish(self):
        return chip_tune_loop.phase3b_polish(self)

    def _wait_for_mining_state(self, timeout=300):
        return recovery.wait_for_mining_state(self, timeout)

    def _is_miner_hashing(self):
        """Quick check: is the miner in Mining state with non-zero hashrate?
        Returns False for Initializing, Stopped, unreachable, or zero hashrate."""
        try:
            summary = self.api.summary()
        except Exception:
            return False
        if not summary:
            return False
        if summary.operating_state != "Mining":
            return False
        return summary.is_hashing

    # ── Phase 4: Measure Efficiency ──

    def _phase4_measure_efficiency(self):
        return chip_tune_orchestration.phase4_measure_efficiency(self)

    # ── MiningRigRentals sync ──

    def _mrr_set_last_sync(self, intent, rig_id, result, reason="", **extra):
        return mrr_sync.mrr_set_last_sync(self, intent, rig_id, result, reason=reason, **extra)

    def _mrr_sync(self, intent, reason=""):
        return mrr_sync.mrr_sync(self, intent, reason=reason)

    def _mrr_apply_pool_config(self, reason=""):
        return mrr_sync.mrr_apply_pool_config(self, reason=reason)

    # ── Phase 5: Save Profile ──

    def _phase5_save(self):
        return phase_runners.phase5_save(self)

    def _save_profile(self):
        return persistence.save_profile(self)

    def _load_profile(self):
        return persistence.load_profile(self)

    def _save_checkpoint(self):
        return persistence.save_checkpoint(self)

    def _load_checkpoint(self):
        return persistence.load_checkpoint(self)

    def _delete_checkpoint(self):
        filepath = persistence.checkpoint_path(self)
        if os.path.exists(filepath):
            os.remove(filepath)
            self.log("Checkpoint file cleaned up")

    # ── Phase 6: Perpetual Tune (voltage-tracking) ──
    #
    # Designed for mining uptime. Each cycle:
    #   1. Average hashrate against the active sweep profile's measured TH/s
    #   2. Thermal safety sweep — throttle hot chips/boards live (no restart)
    #   3. If hashrate drifts outside the configured deadband, nudge voltage
    #      up (hashrate below target) or down (hashrate above target) by
    #      a configurable step, capped at ±PERPETUAL_VOLTAGE_MAX_DELTA_MV
    #
    # A miner restart only happens when the voltage adjuster hits its positive
    # cap AND PERPETUAL_RESTART_MIN_HOURS have elapsed since the last restart.
    # The restart reverts both voltage and chip frequencies to the active
    # sweep profile, resets voltage_adjustment_mv to 0, and updates
    # last_restart_ts.

    def _refresh_sweep_reference(self):
        return retune.refresh_sweep_reference(self)

    def _phase6_perpetual(self):
        return perpetual.phase6_perpetual(self)

    def _perpetual_sample_hashrate(self, window_min):
        return perpetual.perpetual_sample_hashrate(self, window_min)

    def _perpetual_thermal_sweep(self):
        return monitor.perpetual_thermal_sweep(self)

    def _detect_thermal_emergency(self, min_interval_sec=30):
        return monitor.detect_thermal_emergency(self, min_interval_sec)

    def _drain_firmware_command_queue(self):
        return apply.drain_firmware_command_queue(self)

    def _handle_thermal_in_chip_tune(self, emergency):
        return monitor.handle_thermal_in_chip_tune(self, emergency)

    def _handle_thermal_in_vf_measure(self, emergency, v_mv, f_mhz, fine):
        return monitor.handle_thermal_in_vf_measure(self, emergency, v_mv, f_mhz, fine)

    def _adjust_voltage(self, direction_mv):
        return perpetual.adjust_voltage(self, direction_mv)

    def _do_perpetual_restart(self):
        return perpetual.do_perpetual_restart(self)

    def select_voltage_profile(self, voltage_mv):
        return retune.select_voltage_profile(self, voltage_mv)

    def retune_voltage(self, voltage_mv):
        return retune.retune_voltage(self, voltage_mv)

    # ── Frequency Application ──

    def _apply_stable_freqs(self):
        return apply.apply_stable_freqs(self)

    def _apply_freqs_direct(self, freq_arrays):
        return apply.apply_freqs_direct(self, freq_arrays)

    # ── Temperature Helpers ──

    def _get_board_temps(self):
        temps = self.api.temps()
        if not temps:
            return [0, 0, 0]
        result = []
        for board in temps:
            sensors = [x for x in [board.temp_inlet_c, board.temp_outlet_c] if x is not None]
            if not sensors:
                sensors = [0]
            result.append(max(sensors) if sensors else 0)
        while len(result) < self.num_boards:
            result.append(0)
        return result

    def _get_chip_temps(self):
        data = self.api.temps_chip()
        if not data:
            return None
        result = []
        for board in data:
            result.append(board.chip_temps_c)
        return result

    # ── Live Data for Dashboard ──

    def _update_live_data(self):
        now = time.time()
        if now - self.last_update < 5:
            return
        self.last_update = now
        self.last_summary = self.api.summary()
        if (
            self.mac.startswith("syn-")
            and isinstance(self.last_summary, MinerSummary)
            and self.last_summary.mac
            and self.last_summary.mac != self.mac
        ):
            from tuner_app.manager.bulk import _rekey_miner
            from tuner_app.main import manager

            old_mac = self.mac
            try:
                _rekey_miner(self.mac, self.last_summary.mac, manager=manager)
                self.log(
                    f"[rekey] synth-to-real upgrade: {old_mac} -> {self.mac}",
                    level="INFO",
                )
            except ValueError as ex:
                self.log(
                    f"[rekey] synth-to-real re-key failed: {ex}",
                    level="WARN",
                )
            except Exception as ex:
                self.log(
                    f"[rekey] synth-to-real re-key error: {ex}",
                    level="WARN",
                )
        self.last_hashrate = self.api.hashrate()
        self.last_clocks = self.api.clocks()
        self.last_temps = self.api.temps()
        self.last_chip_temps = self.api.temps_chip()

    def _compute_avg_temps_c(self):
        return status.compute_avg_temps_c(self)

    def _compute_top_tunes(self, limit=3):
        return status.compute_top_tunes(self, limit)

    def _derive_planned_grid_for_dashboard(self):
        return status.derive_planned_grid_for_dashboard(self)

    def _derive_top_k_for_dashboard(self):
        return status.derive_top_k_for_dashboard(self)

    def get_status(self):
        return status.get_status(self)

    def get_live_data(self):
        return status.get_live_data(self)

    def get_export(self, current_config=None):
        return status.get_export(self, current_config)
