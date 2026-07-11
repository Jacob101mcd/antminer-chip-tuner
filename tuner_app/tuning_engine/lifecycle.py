"""
Instance lifecycle management for the TuningEngine.

This module handles the start, stop, and destroy operations for the tuning
engine instance, along with remeasure queue management and live stock baseline
capture functionality.
"""

from __future__ import annotations

import statistics
import threading
import time
import traceback
from datetime import datetime


def start(engine):
    with engine._control_lock:
        if engine.thread and engine.thread.is_alive():
            return False
        engine.running = True
        engine.thread = threading.Thread(target=engine._run, daemon=True)
        engine.thread.start()
        return True


def stop(engine):
    with engine._control_lock:
        engine.running = False
        engine.phase = engine.PHASE_STOPPED
        engine.phase_detail = "Stopped by user"
        # Leaving Phase 6 — next Phase 6 entry should re-announce.
        engine._mrr_phase6_announced = False
        engine._mrr_polish_announced = False
    # MRR sync outside the lock — the HTTP call can take up to 15s and we
    # don't want to hold _control_lock that long (it'd block start/stop/
    # retune clicks). No-op if MRR is disabled or rig isn't configured.
    engine._mrr_sync("stopped", reason="Operator stop")


def destroy(engine):
    """Permanent-shutdown signal. Used by Reset Profile and remove miner
    when this engine instance is being replaced. Sets running=False so
    the tuning thread exits at its next self.running check, AND latches
    _destroyed=True so any disk write from the orphan thread between now
    and its actual exit is a silent no-op. Without the second flag,
    join(timeout=5) returns before long sleeps inside sample loops finish
    and the thread's next _save_checkpoint() resurrects the file we're
    about to delete (or have already deleted)."""
    with engine._control_lock:
        engine.running = False
        engine._destroyed = True
        engine.phase = engine.PHASE_STOPPED
        engine.phase_detail = "Engine destroyed"
        engine._mrr_phase6_announced = False
        engine._mrr_polish_announced = False


def remeasure_key(entry):
    return (int(entry["voltage_mv"]), round(float(entry["freq_mhz"]), 3))


def enqueue_remeasure(engine, voltage_mv, freq_mhz):
    """Append a (V, F) cell to the remeasure queue. Dedupes against the
    current queue. Returns (added: bool, queue_size: int)."""
    key = (int(voltage_mv), round(float(freq_mhz), 3))
    with engine._control_lock:
        for q in engine.remeasure_queue:
            if engine._remeasure_key(q) == key:
                return False, len(engine.remeasure_queue)
        engine.remeasure_queue.append(
            {
                "voltage_mv": int(voltage_mv),
                "freq_mhz": round(float(freq_mhz), 3),
                "queued_at": datetime.now().isoformat(),
            }
        )
        size = len(engine.remeasure_queue)
    engine.log(
        f"Remeasure queued: ({int(voltage_mv)} mV, {float(freq_mhz):.1f} MHz) — queue size {size}"
    )
    return True, size


def clear_remeasure_queue(engine):
    with engine._control_lock:
        n = len(engine.remeasure_queue)
        engine.remeasure_queue = []
    if n:
        engine.log(f"Remeasure queue cleared ({n} cell{'s' if n != 1 else ''})")


def drain_remeasure_queue(engine):
    """Process queued remeasurements. Overwrites existing vf_surface /
    vf_skipped entries at each queued (V, F). Safe to call mid-Phase-V;
    each cell's measurement + checkpoint is atomic. Returns the count
    of cells successfully processed (including no-data results)."""
    drained = 0
    while engine.running:
        with engine._control_lock:
            if not engine.remeasure_queue:
                break
            item = engine.remeasure_queue.pop(0)
        v_mv = int(item["voltage_mv"])
        f_mhz = round(float(item["freq_mhz"]), 3)
        key = (v_mv, f_mhz)
        # Drop prior entries at this key so the new measurement takes
        # their slot — vf_surface is keyed by (voltage_mv, round(freq_mhz, 3)).
        engine.vf_surface = [e for e in engine.vf_surface if engine._remeasure_key(e) != key]
        engine.vf_skipped = [e for e in engine.vf_skipped if engine._remeasure_key(e) != key]
        engine.log(f"Remeasure: ({v_mv} mV, {f_mhz:.1f} MHz) — running")
        try:
            result = engine._measure_vf_point(v_mv, f_mhz)
        except Exception as ex:
            # Don't let one bad cell abort the whole drain — put the
            # item back so the operator can see which one failed and
            # decide whether to retry or clear.
            engine.log(f"Remeasure failed for ({v_mv} mV, {f_mhz:.1f} MHz): {ex}")
            with engine._control_lock:
                engine.remeasure_queue.insert(0, item)
            raise
        if result is not None:
            engine.vf_surface.append(result)
        engine._save_checkpoint()
        drained += 1
    if drained:
        engine.log(f"Remeasure: processed {drained} cell{'s' if drained != 1 else ''}")
    return drained


def start_remeasure_queue(engine):
    """Launch a dedicated thread that drains the remeasure queue.
    Fails if the engine is already busy — operator must stop the
    current tune first (mirrors start_retune semantics)."""
    with engine._control_lock:
        if engine.thread and engine.thread.is_alive():
            return False, "engine is busy — stop the current tune first"
        if not engine.remeasure_queue:
            return False, "remeasure queue is empty"
        engine.running = True
        engine.thread = threading.Thread(target=engine._remeasure_runner, daemon=True)
        engine.thread.start()
        return True, ""


def remeasure_runner(engine):
    """Standalone drain runner. Does NOT re-enter Phase 6 on completion —
    the operator is responsible for pressing Start again if they want
    perpetual tuning to resume. Leaving the engine stopped keeps the
    miner at whatever V/F the last remeasurement point applied; the
    next Start will re-apply the active profile via Phase 0 → Phase 6."""
    try:
        engine.phase = "remeasure"
        engine.phase_detail = (
            f"Remeasure queue: {len(engine.remeasure_queue)} "
            f"cell{'s' if len(engine.remeasure_queue) != 1 else ''}"
        )
        engine._drain_remeasure_queue()
        if engine.running:
            engine.phase = engine.PHASE_STOPPED
            engine.phase_detail = "Remeasure complete — press Start to resume mining"
    except Exception as ex:
        engine.phase = engine.PHASE_ERROR
        engine.phase_detail = f"Remeasure failed: {ex}"
        engine.log(f"Remeasure failed: {ex}")
        engine.log(traceback.format_exc())
    finally:
        engine.running = False


def drain_one_remeasure(engine):
    """Pop ONE cell from the queue and remeasure it. Returns True if a
    cell was processed (caller continues main loop), False if queue is
    empty. Multi-cell drains happen across many loop iterations, so a
    long queue interleaves with monitor cycles instead of monopolizing.

    Preserves the prior entry's `fine` / `kind` / `coarse_anchor` fields
    across remeasure — without this, a remeasured fine cell (or a fine
    anchor that was converted from coarse) would come back as a plain
    coarse entry, fall out of the fine sub-grid, and lose its place in
    the dashboard's fine-grid layout. _measure_vf_point() defaults
    `fine=False` so we sniff the prior classification first."""
    with engine._control_lock:
        if not engine.remeasure_queue:
            return False
        item = engine.remeasure_queue.pop(0)
    v_mv = int(item["voltage_mv"])
    f_mhz = round(float(item["freq_mhz"]), 3)
    key = (v_mv, f_mhz)
    # Sniff the prior entry's classification BEFORE we drop it.
    prior_fine = False
    prior_anchor = None
    for e in engine.vf_surface:
        if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) == key:
            if e.get("fine"):
                prior_fine = True
                ca = e.get("coarse_anchor")
                if ca:
                    prior_anchor = dict(ca)
            break
    # Drop prior entry at this key so the new measurement takes its slot.
    engine.vf_surface = [
        e
        for e in engine.vf_surface
        if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) != key
    ]
    # Also drop legacy vf_skipped entries at the same key. The new
    # state machine doesn't populate vf_skipped, but pre-refactor
    # checkpoints still load entries into it; without this filter, a
    # remeasured cell would render as both measured and skipped on the
    # dashboard heatmap.
    engine.vf_skipped = [
        e
        for e in engine.vf_skipped
        if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) != key
    ]
    engine.log(f"Remeasure: ({v_mv} mV, {f_mhz:.1f} MHz) — running")
    try:
        result = engine._measure_vf_point(v_mv, f_mhz, fine=prior_fine)
    except Exception as ex:
        engine.log(f"Remeasure failed for ({v_mv} mV, {f_mhz:.1f} MHz): {ex}")
        with engine._control_lock:
            engine.remeasure_queue.insert(0, item)
        # Persist the re-insert before raising so an offline-error path
        # doesn't lose the queued cell on process kill.
        try:  # noqa: SIM105
            engine._save_checkpoint()
        except Exception:
            pass
        raise
    if result is not None:
        if prior_fine:
            result["kind"] = "fine"
            if prior_anchor is not None:
                result["coarse_anchor"] = prior_anchor
        engine.vf_surface.append(result)
    engine._save_checkpoint()
    return True


def capture_live_stock_baseline(engine, initial_summary):
    """Sample the miner's current hashrate/power BEFORE disabling perpetual
    tune so "stock vs tuned" compares to reality instead of Bitmain's spec
    sheet. Falls back to spec if the miner isn't mining (e.g. curtailed,
    post-reboot, chainbreak)."""
    # Skip entirely if we already have a recorded baseline (from a prior
    # Phase 0 in this tune or from a saved profile/checkpoint). Retry
    # recoveries re-run Phase 0 — at that moment the miner is already
    # mid-tune (partially-applied frequencies from prior progress), so a
    # "live" sample here misrepresents stock. Only capture on a pristine
    # Phase 0 where we haven't recorded anything yet.
    prior = engine.stock_baseline or {}
    if prior.get("source") in ("live", "spec", "manual"):
        engine.log(
            f"Preserving prior stock baseline ({prior.get('source')}): "
            f"{prior.get('hashrate_ths', 0):.1f} TH/s, "
            f"{prior.get('power_w', 0):.0f}W, "
            f"{prior.get('efficiency_jth', 0):.2f} J/TH"
        )
        return
    state = initial_summary.operating_state
    ths0 = initial_summary.hashrate_ths

    def _spec_fallback(reason):
        # Don't clobber a previously-captured live baseline just because the
        # miner happens to be mid-recovery right now — e.g. Phase 0 re-runs
        # after a retry recovery, the miner is in AdjustingClockVoltage or
        # Idling, sampling would fail → but the original live baseline from
        # the first Phase 0 is still the correct stock reference.
        prior = engine.stock_baseline or {}
        if prior.get("source") in ("live", "manual") and prior.get("hashrate_ths"):
            engine.log(
                f"Preserving existing {prior.get('source')} stock baseline ({reason}): "
                f"{prior['hashrate_ths']:.1f} TH/s, "
                f"{prior.get('power_w', 0):.0f}W, "
                f"{prior.get('efficiency_jth', 0):.2f} J/TH "
                f"(captured {prior.get('captured_at', '?')})"
            )
            return
        # Synthesize uniform per-chip arrays from the spec sheet so the
        # dashboard's right-hand "Stock Baseline" pane has something to
        # render even when we couldn't capture live. Spec doesn't define
        # per-chip temps, so chip_temps stays empty (renders gray).
        n_boards = engine.num_boards
        n_chips = engine.chips_per_board
        spec_hashrate_per_chip_mhs = (
            engine.STOCK_SPEC["hashrate_ths"] * 1000.0 / (n_boards * n_chips)
            if n_boards > 0 and n_chips > 0
            else 0.0
        )
        engine.stock_baseline = dict(
            engine.STOCK_SPEC,
            source="spec",
            reason=reason,
            chip_freqs=[[float(engine.STOCK_SPEC["freq_mhz"])] * n_chips for _ in range(n_boards)],
            chip_health=[[100.0] * n_chips for _ in range(n_boards)],
            chip_hashrates=[[spec_hashrate_per_chip_mhs] * n_chips for _ in range(n_boards)],
            chip_temps=[[] for _ in range(n_boards)],
        )
        engine.log(
            f"Stock baseline (Bitmain spec, {reason}): "
            f"{engine.STOCK_SPEC['hashrate_ths']:.0f} TH/s, "
            f"{engine.STOCK_SPEC['power_w']:.0f}W, {engine.STOCK_SPEC['efficiency_jth']:.1f} J/TH"
        )
        engine._save_stock_baseline()

    if state != "Mining" or ths0 <= 0:
        _spec_fallback(f"state={state or 'unknown'}, hr={ths0:.1f}")
        return

    num_samples = int(engine.config["STOCK_BASELINE_SAMPLES"])
    sample_interval = int(engine.config["STOCK_BASELINE_INTERVAL"])
    total_window_sec = max(0, num_samples - 1) * sample_interval
    engine.phase_detail = "Sampling live stock baseline"
    engine.log(
        f"Capturing live stock baseline (miner is Mining at {ths0:.1f} TH/s; "
        f"sampling {num_samples}x over {total_window_sec}s)"
    )
    samples = []
    # Per-chip accumulators captured alongside the summary samples, averaged
    # at the end. Each is shape [num_boards][chip].
    n_boards = engine.num_boards
    chip_freqs_acc = [None] * n_boards
    chip_health_acc = [None] * n_boards
    chip_hashrates_acc = [None] * n_boards
    chip_temps_acc = [None] * n_boards
    chip_freqs_n = [0] * n_boards
    chip_hashrates_n = [0] * n_boards  # also covers chip_health (same /hashrate response)
    chip_temps_n = [0] * n_boards
    for i in range(num_samples):
        if not engine.running:
            return
        s = initial_summary if i == 0 else engine.api.summary()
        if s:
            p = s.power_w
            h = s.hashrate_ths
            # Target Voltage is mV (firmware setpoint). Output Voltage is V.
            v = s.target_voltage_mv or s.output_voltage_mv or 0
            if h > 0 and p > 0:
                samples.append({"hashrate_ths": h, "power_w": p, "voltage_mv": v})
        # Per-chip captures — best-effort, individual API failures don't
        # break stock capture overall (the summary-driven aggregate stats
        # are the must-have; per-chip arrays degrade to gray cells in the
        # dashboard if they're missing).
        try:
            clocks_data = engine.api.clocks()
            if clocks_data:
                for board in clocks_data:
                    idx = board.index
                    if idx >= n_boards:
                        continue
                    d = list(board.chip_freqs_mhz)
                    if not d:
                        continue
                    if chip_freqs_acc[idx] is None:
                        chip_freqs_acc[idx] = [0.0] * len(d)
                    for j, val in enumerate(d):
                        if j < len(chip_freqs_acc[idx]) and isinstance(val, (int, float)):
                            chip_freqs_acc[idx][j] += val
                    chip_freqs_n[idx] += 1
        except Exception:
            pass
        try:
            hr_data = engine.api.hashrate()
            if hr_data:
                for board in hr_data:
                    idx = board.index
                    if idx is None or idx >= n_boards:
                        continue
                    d = board.health_pct
                    if not d:
                        continue
                    if chip_health_acc[idx] is None:
                        chip_health_acc[idx] = [0.0] * len(d)
                        chip_hashrates_acc[idx] = [0.0] * len(board.hashrate_per_chip_mhs)
                    for j, (chip_hashrate, chip_health) in enumerate(
                        zip(board.hashrate_per_chip_mhs, board.health_pct, strict=False)
                    ):
                        if j >= len(chip_health_acc[idx]):
                            continue
                        chip_hashrates_acc[idx][j] += chip_hashrate
                        chip_health_acc[idx][j] += chip_health
                    chip_hashrates_n[idx] += 1
        except Exception:
            pass
        try:
            temps_data = engine.api.temps_chip()
            if temps_data:
                for board in temps_data:
                    idx = board.index
                    if idx is None or idx >= n_boards:
                        continue
                    d = list(board.chip_temps_c)
                    if not d:
                        continue
                    if chip_temps_acc[idx] is None:
                        chip_temps_acc[idx] = [0.0] * len(d)
                    for j, val in enumerate(d):
                        if j < len(chip_temps_acc[idx]) and isinstance(val, (int, float)):
                            chip_temps_acc[idx][j] += val
                    chip_temps_n[idx] += 1
        except Exception:
            pass
        if i < num_samples - 1:
            time.sleep(sample_interval)

    if not samples:
        _spec_fallback("no valid samples")
        return

    avg_ths = statistics.mean(s["hashrate_ths"] for s in samples)
    avg_power = statistics.mean(s["power_w"] for s in samples)
    avg_v = statistics.mean(s["voltage_mv"] for s in samples)

    def _avg_per_board(acc, n_per_board):
        out = []
        for b in range(n_boards):
            if acc[b] is not None and n_per_board[b] > 0:
                out.append([v / n_per_board[b] for v in acc[b]])
            else:
                out.append([])
        return out

    engine.stock_baseline = {
        "hashrate_ths": avg_ths,
        "power_w": avg_power,
        "efficiency_jth": avg_power / avg_ths,
        "voltage_mv": avg_v,
        "source": "live",
        "samples": len(samples),
        "captured_at": datetime.now().isoformat(),
        "chip_freqs": _avg_per_board(chip_freqs_acc, chip_freqs_n),
        "chip_health": _avg_per_board(chip_health_acc, chip_hashrates_n),
        "chip_hashrates": _avg_per_board(chip_hashrates_acc, chip_hashrates_n),
        "chip_temps": _avg_per_board(chip_temps_acc, chip_temps_n),
    }
    engine.log(
        f"Live stock baseline: {avg_ths:.1f} TH/s, {avg_power:.0f}W, "
        f"{avg_power / avg_ths:.2f} J/TH @ {avg_v:.0f}mV ({len(samples)} samples)"
    )
    engine._save_stock_baseline()
