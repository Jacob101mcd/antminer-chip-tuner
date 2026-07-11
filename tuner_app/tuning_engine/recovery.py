"""Miner recovery handling for offline mode and post-recovery settle."""

from __future__ import annotations

import time

from tuner_app.miner.exceptions import MinerNotReady, MinerOfflineError


def enter_offline_mode(engine, reason):
    """Transition into PHASE_OFFLINE. Saves the phase we were in so we can
    restore it on reconnect, stamps the offline-since timestamp, and logs."""
    if engine.phase != engine.PHASE_OFFLINE:
        engine.pre_offline_phase = engine.phase
        engine.pre_offline_phase_detail = engine.phase_detail
    engine.phase = engine.PHASE_OFFLINE
    engine.offline_since_ts = engine.offline_since_ts or time.time()
    engine.offline_failure_count += 1
    engine.phase_detail = f"Miner unreachable — waiting for reconnection ({reason})"
    engine.log(f"Miner went offline: {reason}")
    # Persist offline state so a process restart mid-outage resumes waiting
    # instead of jumping back to the pre-offline phase prematurely.
    try:
        engine._save_checkpoint()
    except Exception as e:
        engine.log(f"Offline-mode checkpoint save failed: {e}")


def wait_for_miner_online(engine):
    """Poll /summary every OFFLINE_POLL_INTERVAL seconds until the miner
    answers. Restores pre-offline phase on reconnect. Returns only when
    self.running becomes False or the miner comes back."""
    poll_interval = max(10, int(engine.config.get("OFFLINE_POLL_INTERVAL", 30)))
    while engine.running:
        # Sleep in small chunks so a Stop click interrupts quickly.
        remaining = poll_interval
        while remaining > 0 and engine.running:
            time.sleep(min(remaining, 5))
            remaining -= 5
        if not engine.running:
            return
        dur = int(time.time() - (engine.offline_since_ts or time.time()))
        engine.phase_detail = f"Miner offline for {dur}s — polling every {poll_interval}s"
        try:
            # summary_lite: liveness probe only, avoids storming the firmware
            # with 10 TCP cmds per poll on LuxOS while it's already offline.
            summary = engine.api.summary_lite()
        except MinerOfflineError:
            continue
        except Exception as e:
            engine.log(f"Reconnect poll failed unexpectedly: {e}")
            continue
        if summary is None:
            # GET didn't raise but returned None (e.g. HTTP 500) — treat as
            # still offline rather than good-enough to resume a tune.
            continue
        # Miner answered. Restore pre-offline phase and return to _run loop.
        dur = int(time.time() - (engine.offline_since_ts or time.time()))
        engine.log(f"Miner back online after {dur}s, resuming {engine.pre_offline_phase}")
        engine.last_successful_contact_ts = time.time()
        engine.offline_since_ts = None
        engine.offline_failure_count = 0
        if engine.pre_offline_phase:
            engine.phase = engine.pre_offline_phase
            engine.phase_detail = engine.pre_offline_phase_detail
        engine.pre_offline_phase = None
        engine.pre_offline_phase_detail = ""
        return


def attempt_miner_recovery(engine, retry_num):
    """Escalating recovery: start_mining first, reboot on later retries."""
    if retry_num <= 2:
        # First attempts: try start_mining (miner may have just stopped)
        engine.log("Recovery: attempting start_mining...")
        try:
            engine.api.start_mining()
            engine.log("Recovery: start_mining command sent")
        except Exception as ex:
            engine.log(f"Recovery: start_mining failed ({ex}), miner may be unreachable")
    else:
        # Later attempts: full reboot (harder reset)
        engine.log("Recovery: attempting reboot...")
        try:
            engine.api.reboot(0)
            engine.log("Recovery: reboot command sent, waiting 120s for firmware restart")
            remaining = 120
            while remaining > 0 and engine.running:
                time.sleep(min(remaining, 10))
                remaining -= 10
        except Exception as ex:
            engine.log(f"Recovery: reboot failed ({ex}), miner may be unreachable")


def wait_for_mining_state(engine, timeout=300):
    """Wait until the miner is in 'Mining' state with non-zero hashrate.
    Raises MinerNotReady on timeout — never silently returns.
    Replaces both _wait_for_ready and _wait_for_settle_basic."""
    start = time.time()
    last_state = "unknown"
    while engine.running and (time.time() - start) < timeout:
        try:
            # summary_lite: only operating_state and is_hashing are read;
            # 10s polls × 30 max iterations would otherwise fire 300 TCP
            # cmds against LuxOS and re-trip the port-storm.
            summary = engine.api.summary_lite()
        except Exception:
            summary = None
        if summary:
            state = summary.operating_state
            last_state = state
            if state == "Mining":  # noqa: SIM102
                # Confirm at least one board is hashing
                if summary.is_hashing:
                    return
                # Mining state but no hashrate yet — keep waiting
            elapsed = int(time.time() - start)
            engine.phase_detail = f"Waiting for miner (state={state}, {elapsed}s/{timeout}s)"
        else:
            elapsed = int(time.time() - start)
            engine.phase_detail = f"Waiting for miner (unreachable, {elapsed}s/{timeout}s)"
            last_state = "unreachable"
        time.sleep(10)
    if not engine.running:
        return  # stopped by user, not a timeout
    raise MinerNotReady(f"Miner not ready after {timeout}s (last state: {last_state})")
