"""Whatsminer (stock MicroBT) 2D power_limit × target_freq grid-search loop.

Mirrors the shape of braiins_phases.py but iterates a 2D grid. Public entry:
run_whatsminer_loop(engine). Internal: _run_whatsminer_pass, _phase_whatsminer_discovery,
_measure_pl_freq_cell, perpetual loop. Called from TuningEngine._run_inner when
api.tuning_strategy() == "power_limit_freq_search".
"""

from __future__ import annotations

import contextlib
import statistics
import time
from datetime import datetime

from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.tuning_engine.apply import (
    wait_for_upfreq_complete,
    wait_for_whatsminer_restart,
    wait_for_whatsminer_stable,
)
from tuner_app.tuning_engine.phases import (
    PHASE_ERROR,
    PHASE_WHATSMINER_DISCOVERY,
    PHASE_WHATSMINER_PERPETUAL,
    PHASE_WHATSMINER_PL_FREQ_SEARCH,
)
from tuner_app.tuning_engine.whatsminer_grid import (
    build_freq_axis,
    build_power_limit_axis,
    freq_to_mode_and_percent,
)


def _phase_whatsminer_discovery(engine) -> None:
    """Discovery: probe each of low/normal/high power modes, sample baseline freq + power.
    Mark unsupported modes (Code:132). Probe percent-anchor semantics (current_mode vs
    normal_only) by setting target_freq=10% and comparing measured freq.
    Sets engine.whatsminer_baselines, engine.whatsminer_freq_pct_anchor.
    """
    engine.phase = PHASE_WHATSMINER_DISCOVERY
    engine.log("Whatsminer: starting discovery", level="INFO")

    # Snapshot pre-tune state for restore-on-stop
    try:
        summary = engine.api.summary()
        engine.whatsminer_pre_tune = {
            "operating_state": getattr(summary, "operating_state", None),
            "hashrate_ths": getattr(summary, "hashrate_ths", None),
            "power_w": getattr(summary, "power_w", None),
        }
    except Exception:
        engine.whatsminer_pre_tune = {}

    baselines: dict[str, dict] = {}
    samples_n = max(1, int(engine.config.get("WHATSMINER_BASELINE_SAMPLES", 5)))

    for mode in ("low", "normal", "high"):
        if not engine.running or engine._destroyed:
            return
        try:
            engine.api.set_power_mode(mode)
            wait_for_whatsminer_restart(engine)
            wait_for_upfreq_complete(engine)
            wait_for_whatsminer_stable(engine)
            # Sample freq + power
            hashrates: list[float] = []
            powers: list[float] = []
            target_freq = 0.0
            for _ in range(samples_n):
                if not engine.running or engine._destroyed:
                    return
                s = engine.api.summary()
                hashrates.append(float(getattr(s, "hashrate_ths", 0.0) or 0.0))
                powers.append(float(getattr(s, "power_w", 0.0) or 0.0))
                # Best-effort target_freq read; assume the firmware exposes it
                tf = getattr(s, "target_freq_mhz", None)
                if tf is None:
                    raw = getattr(s, "raw", {}) or {}
                    tf = raw.get("target_freq") or raw.get("freq_target")
                if tf is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        target_freq = float(tf)
            baselines[mode] = {
                "target_freq": target_freq if target_freq else 500.0,
                "freq_avg": statistics.mean(hashrates) if hashrates else 0.0,
                "power_w": statistics.mean(powers) if powers else 0.0,
                "supported": True,
            }
            engine._wm_current_mode = mode
        except MinerCommandError as exc:
            msg = str(exc)
            # Code:132 indicates mode unsupported on this hardware
            if "132" in msg:
                engine.log(
                    f"Whatsminer: mode {mode!r} unsupported (Code:132)",
                    level="WARN",
                )
                baselines[mode] = {
                    "target_freq": 0.0,
                    "freq_avg": 0.0,
                    "power_w": 0.0,
                    "supported": False,
                }
                continue
            raise
        except MinerOfflineError as exc:
            engine.log(
                f"Whatsminer: discovery offline at mode {mode!r}: {exc}",
                level="WARN",
            )
            baselines[mode] = {
                "target_freq": 0.0,
                "freq_avg": 0.0,
                "power_w": 0.0,
                "supported": False,
            }

    engine.whatsminer_baselines = baselines

    # Anchor probe: pick first supported mode, set target_freq to 10%, sample.
    # If observed freq matches "current mode baseline * 1.10" -> "current_mode".
    # Otherwise -> "normal_only".
    anchor: str | None = None
    supported_modes = [m for m, info in baselines.items() if info.get("supported")]
    if supported_modes:
        probe_mode = supported_modes[0]
        try:
            engine.api.set_power_mode(probe_mode)
            wait_for_whatsminer_restart(engine)
            engine.api.set_target_freq(percent=10)
            wait_for_upfreq_complete(engine)
            wait_for_whatsminer_stable(engine)
            s = engine.api.summary()
            observed = getattr(s, "target_freq_mhz", None)
            if observed is None:
                raw = getattr(s, "raw", {}) or {}
                observed = raw.get("target_freq") or raw.get("freq_target")
            try:
                observed = float(observed) if observed is not None else None
            except (TypeError, ValueError):
                observed = None
            current_baseline = baselines[probe_mode]["target_freq"]
            normal_baseline = baselines.get("normal", {}).get("target_freq", current_baseline)
            if observed and current_baseline:
                expected_current = current_baseline * 1.10
                expected_normal = normal_baseline * 1.10 if normal_baseline else None
                # If observed is closer to expected_current than expected_normal,
                # firmware uses "current_mode". Otherwise "normal_only".
                if expected_normal is None:
                    anchor = "current_mode"
                else:
                    # Degenerate case: if the two expected interpretations agree
                    # within 2%, the probe cannot disambiguate. Fail hard.
                    if (
                        abs(expected_current - expected_normal)
                        / max(abs(expected_current), abs(expected_normal), 1.0)
                    ) < 0.02:
                        engine.phase = PHASE_ERROR
                        engine.log(
                            "Whatsminer: anchor probe inconclusive: "
                            "increase WHATSMINER_FREQ_MIN_MHZ / "
                            "WHATSMINER_FREQ_MAX_MHZ separation between modes",
                            level="ERROR",
                        )
                        return
                    err_current = abs(observed - expected_current)
                    err_normal = abs(observed - expected_normal)
                    anchor = "current_mode" if err_current < err_normal else "normal_only"
            else:
                anchor = "current_mode"
        except Exception as exc:
            engine.log(f"Whatsminer: anchor probe failed: {exc}", level="WARN")
            anchor = "current_mode"

    engine.whatsminer_freq_pct_anchor = anchor or "current_mode"
    engine.log(
        f"Whatsminer: discovery complete; anchor={engine.whatsminer_freq_pct_anchor!r}",
        level="INFO",
    )
    engine._save_checkpoint()


def _measure_pl_freq_cell(
    engine,
    power_limit_w: int,
    target_freq_mhz: float,
    fine: bool = False,
    coarse_anchor: dict | None = None,
) -> dict | None:
    """Apply (power_limit_w, target_freq_mhz) cell, settle, sample, return cell dict.
    Caches engine._wm_current_mode/percent/power_limit so transitions are skipped
    when unchanged. Appends the cell to engine.vf_surface.
    """
    if not engine.running or engine._destroyed:
        return None

    baselines = engine.whatsminer_baselines or {}
    anchor = engine.whatsminer_freq_pct_anchor or "current_mode"

    # Compute mode + percent for target freq (best-effort)
    target_mode: str | None = None
    target_percent: float | None = None
    try:
        target_mode, target_percent = freq_to_mode_and_percent(target_freq_mhz, baselines, anchor)
    except Exception:
        target_mode, target_percent = None, None

    # Apply mode if changed
    if target_mode and engine._wm_current_mode != target_mode:
        try:
            engine.api.set_power_mode(target_mode)
            wait_for_whatsminer_restart(engine)
            wait_for_upfreq_complete(engine)
            wait_for_whatsminer_stable(engine)
            engine._wm_current_mode = target_mode
        except (MinerCommandError, MinerOfflineError) as exc:
            engine.log(
                f"Whatsminer: set_power_mode({target_mode}) failed: {exc}",
                level="WARN",
            )

    # Apply percent if changed (>0.01 tolerance)
    if target_percent is not None and (
        engine._wm_current_percent is None
        or abs(engine._wm_current_percent - target_percent) > 0.01
    ):
        try:
            engine.api.set_target_freq(percent=target_percent)
            wait_for_upfreq_complete(engine)
            wait_for_whatsminer_stable(engine)
            engine._wm_current_percent = target_percent
        except (MinerCommandError, MinerOfflineError) as exc:
            engine.log(
                f"Whatsminer: set_target_freq({target_percent}) failed: {exc}",
                level="WARN",
            )

    # Apply power limit if changed
    if engine._wm_current_power_limit != power_limit_w:
        try:
            engine.api.set_power_limit(power_limit_w)
            wait_for_upfreq_complete(engine)
            wait_for_whatsminer_stable(engine)
            engine._wm_current_power_limit = power_limit_w
        except (MinerCommandError, MinerOfflineError) as exc:
            engine.log(
                f"Whatsminer: set_power_limit({power_limit_w}) failed: {exc}",
                level="WARN",
            )

    # Sample
    sample_window = max(1, int(engine.config.get("WHATSMINER_SAMPLE_WINDOW_SEC", 60)))
    sample_interval = max(1, int(engine.config.get("WHATSMINER_SAMPLE_INTERVAL_SEC", 10)))
    n_samples = max(1, sample_window // sample_interval)

    hashrates: list[float] = []
    powers: list[float] = []
    for _ in range(n_samples):
        if not engine.running or engine._destroyed:
            break
        try:
            s = engine.api.summary()
            h = float(getattr(s, "hashrate_ths", 0.0) or 0.0)
            p = float(getattr(s, "power_w", 0.0) or 0.0)
            hashrates.append(h)
            powers.append(p)
        except Exception:
            pass
        if sample_interval > 0:
            slept = 0
            while slept < sample_interval:
                if not engine.running or engine._destroyed:
                    break
                time.sleep(1)
                slept += 1

    mean_h = statistics.mean(hashrates) if hashrates else 0.0
    mean_p = statistics.mean(powers) if powers else 0.0
    eff = mean_p / mean_h if mean_h > 0 else None

    cell = {
        "power_limit_w": int(power_limit_w),
        "target_freq_mhz": float(target_freq_mhz),
        "voltage_mv": None,
        "freq_mhz": float(target_freq_mhz),
        "hashrate_ths": mean_h,
        "power_w": mean_p,
        "efficiency_jth": eff,
        "axis_x_kind": "power_limit_w",
        "fine": bool(fine),
        "coarse_anchor": coarse_anchor,
        "kind": "fine" if fine else "coarse",
        "measured_at": datetime.now().isoformat(),
    }
    engine.vf_surface.append(cell)
    engine.whatsminer_results.append(cell)
    with contextlib.suppress(Exception):
        engine._save_checkpoint()
    return cell


def _run_whatsminer_pass(engine) -> None:
    """One pass through discovery (if needed) -> coarse grid -> optional fine.
    Iterates the (power_limit, target_freq) coarse grid in mode-major order
    (outer = mode hint, middle = freq, inner = power_limit) to minimize
    mode-restart count.
    """
    if not engine.running or engine._destroyed:
        return

    # Discovery if no baselines yet
    if not engine.whatsminer_baselines:
        _phase_whatsminer_discovery(engine)
        if not engine.running or engine._destroyed:
            return

    # Coarse grid
    engine.phase = PHASE_WHATSMINER_PL_FREQ_SEARCH
    pl_axis = build_power_limit_axis(engine)
    f_axis = build_freq_axis(engine)

    # Mode-major iteration: outer mode hint, middle freq, inner pl
    # Mode hint comes from freq via freq_to_mode_and_percent — use it to
    # group iterations so set_power_mode happens once per freq band.
    baselines = engine.whatsminer_baselines or {}
    anchor = engine.whatsminer_freq_pct_anchor or "current_mode"

    # Group freqs by their predicted mode
    freqs_by_mode: dict[str, list[float]] = {}
    for f in f_axis:
        try:
            m, _pct = freq_to_mode_and_percent(f, baselines, anchor)
        except Exception:
            m = "normal"
        freqs_by_mode.setdefault(m, []).append(f)

    for mode in ("low", "normal", "high"):
        freqs = freqs_by_mode.get(mode, [])
        if not freqs:
            continue
        for f in freqs:
            for pl in pl_axis:
                if not engine.running or engine._destroyed:
                    return
                _measure_pl_freq_cell(engine, pl, f, fine=False)

    # Optional fine pass — top-K coarse cells get a finer grid around them.
    fine_count = int(engine.config.get("WHATSMINER_FINE_COUNT", 0) or 0)
    fine_top_k = int(engine.config.get("WHATSMINER_FINE_TOP_K", 0) or 0)
    if fine_count > 0 and fine_top_k > 0 and engine.vf_surface:
        # Rank coarse cells by efficiency (lower J/TH = better)
        coarse_cells = [
            c
            for c in engine.vf_surface
            if not c.get("fine") and c.get("efficiency_jth") is not None
        ]
        coarse_cells.sort(key=lambda c: c.get("efficiency_jth") or float("inf"))
        for anchor_cell in coarse_cells[:fine_top_k]:
            anchor_pl = anchor_cell["power_limit_w"]
            anchor_f = anchor_cell["target_freq_mhz"]
            # Generate a fine_count x fine_count grid around the anchor
            pl_step = max(
                1,
                (engine.config["POWER_LIMIT_W"] - engine.config["WHATSMINER_PL_MIN_W"])
                // max(1, engine.config["WHATSMINER_PL_COUNT"]),
            )
            f_step = max(
                1,
                (
                    engine.config["WHATSMINER_FREQ_MAX_MHZ"]
                    - engine.config["WHATSMINER_FREQ_MIN_MHZ"]
                )
                // max(1, engine.config["WHATSMINER_FREQ_COUNT"]),
            )
            half = fine_count // 2
            for i in range(-half, half + 1):
                for j in range(-half, half + 1):
                    if not engine.running or engine._destroyed:
                        return
                    fine_pl = max(
                        engine.config["WHATSMINER_PL_MIN_W"],
                        min(engine.config["POWER_LIMIT_W"], anchor_pl + i * (pl_step // 2)),
                    )
                    fine_f = max(
                        engine.config["WHATSMINER_FREQ_MIN_MHZ"],
                        min(engine.config["WHATSMINER_FREQ_MAX_MHZ"], anchor_f + j * (f_step // 2)),
                    )
                    _measure_pl_freq_cell(
                        engine,
                        int(fine_pl),
                        float(int(round(fine_f))),
                        fine=True,
                        coarse_anchor={
                            "power_limit_w": anchor_pl,
                            "target_freq_mhz": anchor_f,
                        },
                    )

    # Pick best cell
    if engine.vf_surface:
        best = min(
            (c for c in engine.vf_surface if c.get("efficiency_jth") is not None),
            key=lambda c: c.get("efficiency_jth") or float("inf"),
            default=None,
        )
        engine.whatsminer_best_cell = best

    with contextlib.suppress(Exception):
        engine._save_profile()

    # Perpetual loop: re-sample at best cell periodically; detect drift
    perpetual_interval = int(engine.config.get("WHATSMINER_PERPETUAL_INTERVAL_SEC", 300))
    drift_threshold = float(engine.config.get("WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT", 5.0))

    while engine.running and not engine._destroyed:
        engine.phase = PHASE_WHATSMINER_PERPETUAL
        # Sleep in 1s slices honoring engine.running
        for _ in range(perpetual_interval):
            if not engine.running or engine._destroyed:
                return
            time.sleep(1) if perpetual_interval > 0 else None
            if perpetual_interval == 0:
                break
        if not engine.running or engine._destroyed:
            return
        # Sample at best cell
        best = engine.whatsminer_best_cell
        if not best:
            return
        sample = _measure_pl_freq_cell(
            engine,
            int(best["power_limit_w"]),
            float(best["target_freq_mhz"]),
            fine=False,
        )
        if sample is None:
            return
        # Check drift against best
        prev_eff = best.get("efficiency_jth")
        cur_eff = sample.get("efficiency_jth")
        if prev_eff and cur_eff:
            drift_pct = abs(cur_eff - prev_eff) / prev_eff * 100.0
            if drift_pct > drift_threshold:
                engine._wm_drift_streak += 1
            else:
                engine._wm_drift_streak = 0
        # If drift_streak >= 2, re-enter pass (but break out for now to allow caller to re-enter)
        if engine._wm_drift_streak >= 2:
            engine._wm_drift_streak = 0
            return
        # Tests with WHATSMINER_PERPETUAL_INTERVAL_SEC == 0 (the default in the
        # _make_engine fixture) need a clean exit so the test doesn't hang.
        if perpetual_interval == 0:
            return


def run_whatsminer_loop(engine) -> None:
    """Outer loop: repeatedly run a pass until engine.running flips False."""
    while engine.running and not engine._destroyed:
        _run_whatsminer_pass(engine)
