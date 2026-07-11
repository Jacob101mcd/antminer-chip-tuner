"""V/F application + settle helpers.

Voltage/frequency ordering: increasing V+F sets voltage first (more
headroom) then frequency; decreasing V+F sets frequency first (reduce
load) then voltage. The clock-settle gate between set_clock and the
next command is mandatory on the decrease path — without it, the next
command lands on "Last command is still pending".
"""

from __future__ import annotations

import contextlib
import statistics
import time

from tuner_app.miner.exceptions import MinerCommandError, MinerNotReady, MinerOfflineError
from tuner_app.tuning_engine.voltage_safety import require_voltage_mutation_allowed


def apply_uniform_freq(engine, freq_mhz):
    """Apply `freq_mhz` to every alive chip on every board. Dead chips
    (tracked in self.parked_chips) stay at DEAD_CHIP_FREQ. Mirrors the
    result into self.stable_freq_arrays so the dashboard heatmap and any
    subsequent Phase 4 measurement see the applied frequencies. Caller is
    responsible for voltage settling around this call."""
    dead_freq = engine.config["DEAD_CHIP_FREQ"]
    target = float(freq_mhz)
    for b in range(engine.num_boards):
        n = len(engine.baseline_scores[b]) if engine.baseline_scores[b] else engine.chips_per_board
        if n == 0:
            continue
        arr = [dead_freq if i in engine.parked_chips[b] else target for i in range(n)]
        engine.stable_freq_arrays[b] = list(arr)
    apply_freqs_direct(engine, engine.stable_freq_arrays)


def phase1_set_voltage(engine, voltage_mv, freq_mhz):
    require_voltage_mutation_allowed(engine, voltage_mv)
    freq = freq_mhz
    engine.phase = engine.PHASE_SET_VOLTAGE
    engine.min_voltage_mv = voltage_mv
    engine.phase_detail = f"Setting voltage to {voltage_mv} mV and frequency to {freq} MHz"
    engine.log(f"Phase 1: Setting voltage={voltage_mv} mV, freq={freq} MHz")

    # Determine current voltage to decide ordering:
    #   Increasing V+F: voltage first (more headroom), wait, then frequency
    #   Decreasing V+F: frequency first (reduce load), wait, then voltage
    # This prevents chips from entering a crashed state.
    current_voltage = engine._get_current_voltage_mv()
    voltage_increasing = voltage_mv >= current_voltage or current_voltage == 0

    # Ensure miner is ready to accept commands (not mid-adjustment)
    engine._wait_for_mining_state()

    if voltage_increasing:
        engine.log(
            f"Voltage increasing/unchanged ({current_voltage} -> {voltage_mv} mV): "
            f"voltage first, then frequency"
        )
        engine.api.set_voltage(voltage_mv)
        wait_for_voltage_settle(engine, voltage_mv)
        engine._wait_for_mining_state()
        engine.api.set_clock_all(freq)
    else:
        engine.log(
            f"Voltage decreasing ({current_voltage} -> {voltage_mv} mV): "
            f"frequency first, then voltage"
        )
        engine.api.set_clock_all(freq)
        wait_for_clock_settle(engine, [freq] * engine.num_boards)
        engine._wait_for_mining_state()
        engine.api.set_voltage(voltage_mv)

    engine.phase_detail = "Waiting for stabilization"
    engine.log("Waiting for miner to stabilize...")
    wait_for_settle(engine, target_freq=freq)
    engine.log("Miner settled")

    # Apply external power limit after V+F settle for vendors that support
    # it (Bixbit, LuxOS, Braiins). ePIC has no external knob — set_power_limit
    # is a no-op there — so the capability check keeps intent explicit. The
    # firmware_type is already carried in each JSONL entry, so the log line
    # stays vendor-neutral.
    # POWER_LIMIT_W default: 3500W is the S21 baseline.
    if engine.api.has_external_power_limit():
        power_limit_w = engine.config.get("POWER_LIMIT_W", 3500)
        try:
            engine.api.set_power_limit(power_limit_w)
            engine.log(f"set_power_limit({power_limit_w} W) applied")
        except Exception as e:
            engine.log(f"set_power_limit({power_limit_w} W) failed (non-fatal): {e}")


def wait_for_voltage_settle(engine, target_mv):
    """Wait until PSU output voltage is near target value.
    Used to ensure voltage has stabilized before changing frequency.
    The firmware needs ~3-5s internally (psu.set_voltage waits 3000ms),
    so we enforce a minimum 5s delay before polling to avoid sending
    the next command while the voltage command is still pending."""
    engine.phase_detail = f"Waiting for voltage to settle at {target_mv} mV"
    # Minimum delay: firmware processes voltage changes internally (~3s settle
    # + command overhead). Without this, the output voltage may already read
    # "close enough" from the old value before the change even starts,
    # causing the next command to hit "Last command is still pending".
    time.sleep(5)
    for attempt in range(engine.config["SETTLE_MAX_ATTEMPTS"]):
        if not engine.running:
            return
        summary = engine.api.summary()
        if summary:
            output_mv = summary.output_voltage_mv or 0
            if abs(output_mv - target_mv) < engine.config["SETTLE_VOLTAGE_TOLERANCE_MV"]:
                engine.log(f"Voltage settled at {output_mv:.0f} mV (target {target_mv} mV)")
                return
            engine.phase_detail = (
                f"Voltage settling... ({output_mv:.0f}/{target_mv} mV, attempt {attempt + 1})"
            )
        time.sleep(engine.config["SETTLE_POLL_INTERVAL"])
    engine.log(
        f"Voltage settle timeout after {engine.config['SETTLE_MAX_ATTEMPTS']} attempts "
        f"(target {target_mv} mV)"
    )


def wait_for_clock_settle(engine, target_means, tolerance_mhz=5.0):
    """Wait until each board's average clock is within `tolerance_mhz` of its
    target mean. Required between a clock-write and the *next* command on the
    voltage-DECREASING path: after set_clock_all / set_clock_chip, the ePIC
    firmware ramps chips one-by-one internally (~10–30s for 108 chips).
    Issuing set_voltage while that ramp is mid-flight returns
    "Last command is still pending", which historically burned a full
    recovery cycle (~15 min) per voltage transition.

    target_means: list[float] of per-board expected mean MHz (one per board).
    For a uniform write pass [freq] * self.num_boards.

    Forced 5s pre-delay: firmware needs ~3–5s minimum for the write to
    propagate before /clocks starts reading new values.
    """
    target_desc = (
        f"{target_means[0]:.1f} MHz"
        if target_means and all(abs(t - target_means[0]) < 0.01 for t in target_means)
        else f"per-board means {[round(t, 1) for t in target_means]}"
    )
    engine.phase_detail = f"Waiting for clocks to settle at {target_desc}"
    time.sleep(5)
    for attempt in range(engine.config["SETTLE_MAX_ATTEMPTS"]):
        if not engine.running:
            return
        clocks_data = engine.api.clocks()
        if clocks_data:
            settled = True
            max_dev = 0.0
            for b, board in enumerate(clocks_data):
                if b >= len(target_means):
                    break
                data = board.chip_freqs_mhz
                if not data:
                    settled = False
                    break
                avg = statistics.mean(data)
                dev = abs(avg - target_means[b])
                if dev > max_dev:
                    max_dev = dev
                if dev > tolerance_mhz:
                    settled = False
            if settled:
                engine.log(
                    f"Clocks settled at {target_desc} (max board deviation {max_dev:.1f} MHz)"
                )
                return
            engine.phase_detail = (
                f"Clocks settling... ({max_dev:.1f} MHz off target, attempt {attempt + 1})"
            )
        time.sleep(engine.config["SETTLE_POLL_INTERVAL"])
    engine.log(
        f"Clock settle timeout after {engine.config['SETTLE_MAX_ATTEMPTS']} attempts "
        f"(target {target_desc})"
    )


def wait_for_per_chip_freqs_settle(engine, target_arrays, tolerance_mhz=5.0):
    """Wait until each ALIVE chip's reported frequency is within
    `tolerance_mhz` of its commanded value in target_arrays. Required
    after per-chip set_clock_chip writes — firmware ramps chips
    asynchronously (10-30 s per chip), so the per-board AVG check in
    wait_for_clock_settle is too coarse. Without this gate the
    STABILIZE_WAIT countdown starts before chips have reached target,
    polluting LuxOS GHS_5m moving-average health readings.

    target_arrays: list[list[float]] — engine.stable_freq_arrays shape:
    one inner list per board, containing per-chip target MHz.

    tolerance_mhz: per-chip absolute deviation allowed. Default 5.0 MHz
    is consistent with wait_for_clock_settle's per-board tolerance.
    BM1368 firmware grid is 3.125 MHz — operators may pass a stricter
    tolerance if the consistency-vs-precision tradeoff calls for it.

    Skips chips listed in engine.parked_chips[b] (dead chips parked at
    DEAD_CHIP_FREQ — they may not report exactly that value). Tolerates
    engine.parked_chips being None or [] (defensive — Phase 1 paths can
    reach apply_freqs_direct before Phase 2 populates parked_chips).

    Capability-gated: returns immediately if
    engine.api.supports_per_chip_tuning() is False (Bixbit / Braiins
    don't support per-chip writes — the gate is a no-op for them).

    Forced 5 s pre-delay (consistent with wait_for_voltage_settle and
    wait_for_clock_settle): firmware needs ~3-5 s minimum for the write
    to propagate before /clocks starts reading new values.

    Fail-soft: on timeout (after SETTLE_MAX_ATTEMPTS polls), logs a
    warning and returns. Does NOT raise. Matches the contract of
    wait_for_voltage_settle and wait_for_clock_settle.
    """
    if not engine.api.supports_per_chip_tuning():
        return

    engine.phase_detail = "Waiting for per-chip clocks to settle"
    time.sleep(5)

    # Defensive: handle parked_chips being None or empty list
    parked = engine.parked_chips if engine.parked_chips else []

    for attempt in range(engine.config["SETTLE_MAX_ATTEMPTS"]):
        if not engine.running:
            return

        clocks_data = engine.api.clocks()
        if not clocks_data:
            time.sleep(engine.config["SETTLE_POLL_INTERVAL"])
            continue

        settled = True
        off_count = 0
        max_dev = 0.0

        for b in range(min(len(clocks_data), len(target_arrays))):
            actual = clocks_data[b].chip_freqs_mhz
            if not actual:
                settled = False
                break

            for i in range(min(len(actual), len(target_arrays[b]))):
                # Skip parked chips
                if parked and b < len(parked) and i in parked[b]:
                    continue

                dev = abs(actual[i] - target_arrays[b][i])
                if dev > max_dev:
                    max_dev = dev
                if dev > tolerance_mhz:
                    settled = False
                    off_count += 1

        if settled:
            engine.log(f"Per-chip clocks settled (attempt {attempt + 1})")
            return

        engine.phase_detail = (
            f"Per-chip clocks settling... ({off_count} chips off target, attempt {attempt + 1})"
        )
        time.sleep(engine.config["SETTLE_POLL_INTERVAL"])

    engine.log(
        f"Per-chip clock settle timeout after {engine.config['SETTLE_MAX_ATTEMPTS']} attempts"
    )


def wait_for_settle(engine, target_freq):
    """Wait until voltage and frequency are at target values AND chips are hashing.
    Times out after SETTLE_MAX_ATTEMPTS polls to avoid hanging forever.

    target_freq: the freq we told the miner to go to — Phase V / Phase 2 /
    retune all know this value at callsite. Required arg; there is no
    sensible fallback now that Phase V drives all V+F transitions."""
    expected_freq = target_freq
    max_attempts = engine.config["SETTLE_MAX_ATTEMPTS"]
    attempts = 0
    while engine.running and attempts < max_attempts:
        attempts += 1
        summary = engine.api.summary()
        if not summary:
            time.sleep(engine.config["SETTLE_POLL_INTERVAL"])
            continue

        # Check operating state — must be Mining, not Initializing/Adjusting
        state = summary.operating_state
        state_ok = state == "Mining"

        current_v = summary.output_voltage_mv or 0
        v_tolerance = engine.config["SETTLE_VOLTAGE_TOLERANCE_MV"]
        voltage_ok = abs(current_v - engine.min_voltage_mv) < v_tolerance

        clocks_data = engine.api.clocks()
        freq_ok = True
        if clocks_data:
            for board in clocks_data:
                avg = statistics.mean(board.chip_freqs_mhz or [expected_freq])
                # Chips can report a few MHz off from the set value briefly
                # after a set_clock_all — give it a small tolerance rather
                # than requiring exact equality.
                if abs(avg - expected_freq) > 5:
                    freq_ok = False
                    break

        # Check that boards are actually hashing (not just at target V/F)
        hashing_ok = summary.is_hashing

        if state_ok and voltage_ok and freq_ok and hashing_ok:
            return

        engine.phase_detail = (
            f"Settling... ({attempts}/{max_attempts}, state={state}, "
            f"V={'ok' if voltage_ok else 'adj'}, "
            f"F={'ok' if freq_ok else 'adj'}, "
            f"hash={'ok' if hashing_ok else 'waiting'})"
        )
        time.sleep(engine.config["SETTLE_POLL_INTERVAL"])

    if attempts >= max_attempts:
        # Raise as MinerNotReady so the outer retry loop attempts recovery
        # (restart mining / reboot) rather than going permanently FATAL.
        raise MinerNotReady(
            f"Miner failed to settle after {max_attempts} attempts "
            f"({max_attempts * engine.config['SETTLE_POLL_INTERVAL']}s)"
        )


def drain_firmware_command_queue(engine):
    """Wait for the firmware to settle after stop_mining so the next clock
    write isn't rejected with 'Last command is still pending'. The clock
    ramp the miner was running winds down within seconds once mining
    stops; polling /clocks for stability is a cheap proxy for that."""
    try:
        # Best-effort: poll a few times until clocks stop changing. We
        # don't care WHAT the values are — only that consecutive reads
        # match within tolerance, indicating the ramp finished.
        prev = None
        for _ in range(6):
            if not engine.running:
                return
            time.sleep(2)
            try:
                clocks = engine.api.clocks()
            except Exception:
                continue
            snapshot = []
            for entry in clocks:
                data = entry.chip_freqs_mhz
                if data:
                    snapshot.append(round(sum(data) / len(data), 1))
            if prev is not None and snapshot == prev:
                return
            prev = snapshot
    except Exception:
        return  # never propagate — handler outer must keep running


def apply_stable_freqs(engine):
    """Apply stable frequency arrays and voltage with correct ordering.
    Voltage/frequency change ordering prevents chip crashes:
      - Voltage increasing: set voltage first (more headroom), wait, then apply frequencies
      - Voltage decreasing: apply frequencies first (reduce load), wait, then set voltage
    The ePIC firmware handles thermal-safe chip-by-chip transitions internally,
    so we send the target freq arrays in a single write per board — no ramping."""
    target_voltage = engine.min_voltage_mv
    require_voltage_mutation_allowed(engine, target_voltage)
    current_voltage = engine._get_current_voltage_mv()
    voltage_increasing = target_voltage >= current_voltage or current_voltage == 0
    voltage_changed = abs(target_voltage - current_voltage) > 50 if current_voltage > 0 else True

    # Ensure miner is ready before sending commands
    engine._wait_for_mining_state()

    if voltage_increasing:
        # Voltage up/unchanged: set voltage first, wait, then apply frequencies
        if voltage_changed:
            engine.log(
                f"Voltage increasing ({current_voltage} -> {target_voltage} mV): "
                f"setting voltage first, then frequencies"
            )
            engine.api.set_voltage(target_voltage)
            wait_for_voltage_settle(engine, target_voltage)
            engine._wait_for_mining_state()
        apply_freqs_direct(engine, engine.stable_freq_arrays)
    else:
        # Voltage down: apply frequencies first, wait, then set voltage
        engine.log(
            f"Voltage decreasing ({current_voltage} -> {target_voltage} mV): "
            f"setting frequencies first, then voltage"
        )
        apply_freqs_direct(engine, engine.stable_freq_arrays)
        board_means = [statistics.mean(arr) if arr else 0.0 for arr in engine.stable_freq_arrays]
        wait_for_clock_settle(engine, board_means)
        engine._wait_for_mining_state()
        engine.api.set_voltage(target_voltage)
        wait_for_voltage_settle(engine, target_voltage)


def apply_freqs_direct(engine, freq_arrays):
    """Apply per-chip frequency arrays in a single write per board.

    All chips including dead ones are sent — the validation bounds
    enforce DEAD_CHIP_FREQ >= 50 MHz (the ePIC firmware's undocumented
    minimum), so every value in the payload is firmware-safe.
    """
    for b in range(engine.num_boards):
        if not freq_arrays[b]:
            continue
        chip_freqs = [(chip_idx, freq) for chip_idx, freq in enumerate(freq_arrays[b])]
        engine.api.set_clock_chip(b, chip_freqs)
    wait_for_per_chip_freqs_settle(engine, freq_arrays)


# ── Whatsminer (stock MicroBT) settle helpers ────────────────────────────────
# Used by tuner_app.tuning_engine.whatsminer_phases for the 2D power_limit ×
# target_freq grid-search. Mirror the existing settle-helper conventions
# (1-second sleep slices honoring engine.running / engine._destroyed; never
# raise — return False on timeout so the caller measures the cell as-is).


def wait_for_upfreq_complete(engine, timeout_sec: int | None = None) -> bool:
    """Poll engine.api.devs() until every DEVS entry's 'Upfreq Complete'
    field == 1. Returns True on completion, False on timeout (no exception
    raised). Honors engine.running and engine._destroyed for early-exit
    (returns False).

    Uses the cgminer-standard `devs` cmd. The `edevs` (extended-devs) variant
    is not present on H616-platform M-series firmwares; `devs` returns the
    same per-board shape on every Whatsminer btminer build observed in the
    field.

    timeout_sec: defaults to engine.config["WHATSMINER_UPFREQ_TIMEOUT_SEC"]
    when None.
    """
    if timeout_sec is None:
        timeout_sec = engine.config.get("WHATSMINER_UPFREQ_TIMEOUT_SEC", 180)
    deadline = time.time() + timeout_sec

    while time.time() < deadline:
        if not engine.running or engine._destroyed:
            return False
        try:
            devs_response = engine.api.devs()
            devs = devs_response.get("DEVS") if isinstance(devs_response, dict) else devs_response
            if devs and all(dev.get("Upfreq Complete") == 1 for dev in devs):
                return True
        except Exception as e:  # noqa: BLE001
            with contextlib.suppress(Exception):
                engine.log(f"wait_for_upfreq_complete: devs() failed: {e}", level="WARN")
        # 1-second sleep slice for interruptibility
        if not engine.running or engine._destroyed:
            return False
        time.sleep(1)
    return False


def wait_for_whatsminer_stable(engine) -> None:
    """Wait for upfreq completion, then sleep WHATSMINER_STABILIZE_SEC in 1s
    slices honoring engine.running. Best-effort — does not raise on upfreq
    timeout (the caller measures the cell as-is).
    """
    wait_for_upfreq_complete(engine)
    stabilize_s = int(engine.config.get("WHATSMINER_STABILIZE_SEC", 60))
    for _ in range(stabilize_s):
        if not engine.running or engine._destroyed:
            return
        time.sleep(1)


def wait_for_whatsminer_restart(engine) -> bool:
    """Wait for the miner to come back after a power_mode change. Polls
    engine.api.summary() in 1s slices up to WHATSMINER_RESTART_WAIT_SEC.
    Tolerates ConnectionRefusedError, TimeoutError, MinerOfflineError briefly
    (expected during the restart window). Returns True when summary()
    succeeds AND summary.operating_state in {"Mining", "Idle"}; False on
    timeout. Honors engine.running and engine._destroyed.
    """
    timeout = int(engine.config.get("WHATSMINER_RESTART_WAIT_SEC", 90))
    deadline = time.time() + timeout

    while time.time() < deadline:
        if not engine.running or engine._destroyed:
            return False
        try:
            summary = engine.api.summary()
            if summary is not None and getattr(summary, "operating_state", None) in {
                "Mining",
                "Idle",
            }:
                return True
        except (
            ConnectionRefusedError,
            TimeoutError,
            MinerOfflineError,
            MinerCommandError,
            OSError,
        ):
            pass
        time.sleep(1)
    return False
