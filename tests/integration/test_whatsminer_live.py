from __future__ import annotations

import json
import os
import time

import pytest

from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.whatsminer import WhatsminerMinerAPI

MINER_IP = os.getenv("WHATSMINER_LIVE_IP", "").strip()
MINER_PASS = os.getenv("WHATSMINER_LIVE_PASSWORD", "")
try:
    MINER_PORT = int(os.getenv("WHATSMINER_LIVE_PORT", "4028"))
except ValueError:
    MINER_PORT = 0

pytestmark = pytest.mark.skipif(
    os.getenv("WHATSMINER_LIVE_TEST") != "1"
    or not MINER_IP
    or not MINER_PASS
    or not (1 <= MINER_PORT <= 65535),
    reason=(
        "live tests require WHATSMINER_LIVE_TEST=1, WHATSMINER_LIVE_IP, "
        "WHATSMINER_LIVE_PASSWORD, and a valid WHATSMINER_LIVE_PORT"
    ),
)

requires_live_write = pytest.mark.skipif(
    os.getenv("WHATSMINER_LIVE_WRITE") != "1",
    reason="mutating live test requires separate WHATSMINER_LIVE_WRITE=1 opt-in",
)

TEAR_DOWN_TIMEOUT_SEC = 60


def _assert_with_dump(condition, msg, request=None, response=None):
    """Assert with debug dump on failure — dumps request/response JSON."""
    if not condition:
        details = []
        if request is not None:
            details.append(f"REQUEST: {json.dumps(request, indent=2, default=str)}")
        if response is not None:
            details.append(f"RESPONSE: {json.dumps(response, indent=2, default=str)}")
        suffix = ("\n" + "\n".join(details)) if details else ""
        raise AssertionError(f"{msg}{suffix}")


def _api():
    """Construct a fresh WhatsminerMinerAPI for the test miner."""
    return WhatsminerMinerAPI(MINER_IP, port=MINER_PORT, password=MINER_PASS)


@pytest.fixture
def live_api():
    """Function-scope fixture returning a fresh WhatsminerMinerAPI."""
    api = _api()
    return api


@pytest.fixture
def tune_state_guard():
    """Function-scope fixture to capture and restore tune state."""
    api = _api()
    api.authenticate()
    try:
        summary = api.summary()
        pre = {
            "power_w": summary.power_w,
            "fan_speed": summary.fan_speed,
        }
        yield
    finally:
        try:
            api.set_power_mode("normal")
        except (MinerCommandError, MinerOfflineError):
            print("WARNING: Failed to restore power mode")
        try:
            api.set_target_freq(0)
        except (MinerCommandError, MinerOfflineError):
            print("WARNING: Failed to restore target freq")
        try:
            api.set_power_limit(int(pre.get("power_w") or 3500))
        except (MinerCommandError, MinerOfflineError):
            print("WARNING: Failed to restore power limit")


def test_a01_authenticate_happy_path_caches_token(live_api):
    assert live_api.authenticate() is True
    assert live_api._token_cache is not None
    t1 = live_api._token_acquired_at
    live_api.authenticate()
    assert live_api._token_acquired_at == t1
    live_api._get_token()
    assert live_api._token_acquired_at == t1


def test_a02_authenticate_wrong_password_returns_false():
    api = WhatsminerMinerAPI(MINER_IP, port=MINER_PORT, password="intentionally-wrong")
    assert api.authenticate() is False


def test_a03_summary_shape(live_api):
    live_api.authenticate()
    s = live_api.summary()
    _assert_with_dump(s.hashrate_ths > 0, "expected nonzero hashrate", response=s.raw)
    _assert_with_dump(s.power_w > 0, "expected nonzero power", response=s.raw)
    _assert_with_dump(
        s.operating_state in {"S", "Mining", "Working", "Active", "", "I"},
        f"unexpected operating_state: {s.operating_state}",
        response=s.raw,
    )
    if s.boards:
        for b in s.boards:
            assert isinstance(b.index, int)
            assert isinstance(b.hashrate_ths, float)


def test_a04_query_methods_match_capability_contract(live_api):
    live_api.authenticate()
    assert live_api.clocks() == []
    assert live_api.temps() == []
    assert live_api.temps_chip() == []
    assert live_api.hashrate() == 0.0
    devs = live_api.devs()
    assert isinstance(devs.get("DEVS"), list)
    assert len(devs["DEVS"]) >= 1


def test_a05_capabilities_voltages_return_dicts(live_api):
    live_api.authenticate()
    assert isinstance(live_api.capabilities(), dict)
    assert isinstance(live_api.voltages(), list)


def test_a06_firmware_type_string(live_api):
    assert live_api.firmware_type() == "whatsminer"


def test_a07_tuning_strategy_string(live_api):
    assert live_api.tuning_strategy() == "power_limit_freq_search"


def test_a08_capability_flags(live_api):
    assert live_api.supports_per_chip_tuning() is False
    assert live_api.has_external_power_limit() is True
    assert live_api.has_capabilities_endpoint() is False
    assert live_api.has_internal_perpetual_tune() is True


def test_a09_hardware_topology(live_api):
    live_api.authenticate()
    topo = live_api.hardware_topology()
    assert isinstance(topo.num_boards, int)
    assert topo.num_boards >= 1
    devs = live_api.devs()
    assert topo.num_boards == len(devs.get("DEVS", []))
    assert topo.chips_per_board == 0
    assert topo.psu_min_mv > 0
    assert topo.psu_max_mv > topo.psu_min_mv


@requires_live_write
def test_a10_set_power_mode_changes_freq_baseline(live_api, tune_state_guard):
    live_api.authenticate()
    try:
        live_api.set_power_mode("low")
        time.sleep(60)
        low_summary = live_api.summary()
        low_baseline = sum(b.freq_mhz for b in low_summary.boards) / len(low_summary.boards)
    except MinerCommandError as e:
        if "Code 132" in str(e):
            pytest.skip("Low power mode not supported")
        raise
    live_api.set_power_mode("normal")
    time.sleep(60)
    normal_summary = live_api.summary()
    normal_baseline = sum(b.freq_mhz for b in normal_summary.boards) / len(normal_summary.boards)
    time.sleep(10)
    live_api.set_target_freq(percent=10)
    time.sleep(60)
    tuned_summary = live_api.summary()
    tuned = sum(b.freq_mhz for b in tuned_summary.boards) / len(tuned_summary.boards)
    delta_current_mode = abs(tuned - normal_baseline * 1.10) / (normal_baseline * 1.10)
    delta_low_mode = abs(tuned - low_baseline * 1.10) / (low_baseline * 1.10)
    _assert_with_dump(
        delta_current_mode < 0.02,
        (
            f"set_target_freq doesn't act on current mode: "
            f"tuned={tuned:.1f}, expected~={normal_baseline * 1.10:.1f}"
        ),
        request={"percent": 10},
        response={"low": low_baseline, "normal": normal_baseline, "tuned": tuned},
    )
    if delta_low_mode < 0.02 and delta_current_mode < 0.02:
        pytest.fail("BOTH interpretations within 2% — modes too similar to disambiguate")


@requires_live_write
def test_a11_set_power_limit_wall_clock_settle(live_api, tune_state_guard):
    live_api.authenticate()
    pre = live_api.summary()
    target_up = int(pre.power_w + 200)
    target_down = int(pre.power_w - 200)
    for direction, target in [("up", target_up), ("down", target_down)]:
        t0 = time.monotonic()
        assert live_api.set_power_limit(target) is True
        while time.monotonic() - t0 < 600:  # 10 min hard cap
            time.sleep(15)
            cur = live_api.summary()
            if abs(cur.power_w - target) <= 100:
                break
        elapsed = time.monotonic() - t0
        _assert_with_dump(
            elapsed < 600,
            f"power_limit {direction} didn't settle in 10min: target={target}, last={cur.power_w}",
            request={"power_limit_w": target},
            response={"power_w": cur.power_w, "elapsed_s": elapsed},
        )
        print(f"a11 {direction}: target={target}W settled in {elapsed:.1f}s, final={cur.power_w}W")


@requires_live_write
def test_a12_adjust_upfreq_speed_succeeds(live_api, tune_state_guard):
    live_api.authenticate()
    assert live_api.adjust_upfreq_speed(3) is True
    print("a12 adjust_upfreq_speed(3) returned True; visual confirm faster settle in operator log")


def test_a13_summary_boards_have_upfreq_complete_and_effective_chips(live_api):
    live_api.authenticate()
    s = live_api.summary()
    _assert_with_dump(len(s.boards) >= 1, "expected at least 1 board from devs", response=s.raw)
    for b in s.boards:
        _assert_with_dump(
            b.upfreq_complete in (0, 1, True, False),
            f"board {b.index}: upfreq_complete={b.upfreq_complete!r} not parseable as 0/1 bool",
            response=s.raw,
        )
        _assert_with_dump(
            isinstance(b.effective_chips, int) and b.effective_chips > 0,
            f"board {b.index}: effective_chips={b.effective_chips!r} (expected positive int)",
            response=s.raw,
        )


@pytest.mark.skip(reason="WIP — requires full engine bootstrap; use as runbook")
@requires_live_write
def test_b01_reduced_e2e_tune(tune_state_guard, live_api):
    import time

    from tuner_app.tuning_engine.engine import TuningEngine

    config = {
        "PASSWORD": MINER_PASS,
        "API_PORT": MINER_PORT,
        "WHATSMINER_PL_COUNT": 3,
        "WHATSMINER_FREQ_COUNT": 3,
        "WHATSMINER_FINE_COUNT": 0,
        "WHATSMINER_FINE_TOP_K": 0,
        "WHATSMINER_STABILIZE_SEC": 30,
        "WHATSMINER_SAMPLE_WINDOW_SEC": 30,
        "WHATSMINER_SAMPLE_INTERVAL_SEC": 10,
        "WHATSMINER_BASELINE_SAMPLES": 3,
        "WHATSMINER_PL_MIN_W": 2000,
        "POWER_LIMIT_W": 3500,
        "WHATSMINER_FREQ_MIN_MHZ": 500,
        "WHATSMINER_FREQ_MAX_MHZ": 700,
        "WHATSMINER_RESTART_WAIT_SEC": 90,
        "WHATSMINER_UPFREQ_TIMEOUT_SEC": 180,
        "WHATSMINER_PERPETUAL_INTERVAL_SEC": 300,
        "WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT": 5.0,
        "WHATSMINER_UPFREQ_SPEED": 5,
        "firmware_type": "whatsminer",
        "current_firmware": "whatsminer",
    }

    engine = TuningEngine(MINER_IP, config)
    engine.start()
    deadline = time.monotonic() + 60 * 60  # 60-minute hard cap
    try:
        while time.monotonic() < deadline:
            if engine.phase == engine.PHASE_WHATSMINER_PERPETUAL:
                break
            if not engine.running:
                break
            time.sleep(10)

        _assert_with_dump(
            engine.phase == engine.PHASE_WHATSMINER_PERPETUAL,
            (
                "engine did not reach PHASE_WHATSMINER_PERPETUAL within 3600s; "
                f"final phase={engine.phase!r}"
            ),
            response={"phase": engine.phase, "vf_surface_count": len(engine.vf_surface)},
        )
        _assert_with_dump(
            len(engine.vf_surface) >= 9,
            f"expected >= 9 cells in vf_surface (3x3 grid), got {len(engine.vf_surface)}",
            response={"vf_surface_count": len(engine.vf_surface)},
        )
        for cell in engine.vf_surface:
            eff = cell.get("efficiency_jth")
            _assert_with_dump(
                eff is not None and eff > 0,
                (
                    f"cell pl={cell.get('power_limit_w')} "
                    f"f={cell.get('target_freq_mhz')} "
                    f"has efficiency_jth={eff!r}"
                ),
                response=cell,
            )
        _assert_with_dump(
            engine.whatsminer_baselines is not None,
            f"whatsminer_baselines was not populated; got {engine.whatsminer_baselines!r}",
            response={"baselines": engine.whatsminer_baselines},
        )
    finally:
        engine.stop()
        engine.destroy()


@pytest.mark.skip(reason="WIP — requires full engine bootstrap; use as runbook")
@requires_live_write
def test_b02_resume_midway(tune_state_guard, live_api):
    import time

    from tuner_app.tuning_engine.engine import TuningEngine

    config = {
        "PASSWORD": MINER_PASS,
        "API_PORT": MINER_PORT,
        "WHATSMINER_PL_COUNT": 3,
        "WHATSMINER_FREQ_COUNT": 3,
        "WHATSMINER_FINE_COUNT": 0,
        "WHATSMINER_FINE_TOP_K": 0,
        "WHATSMINER_STABILIZE_SEC": 30,
        "WHATSMINER_SAMPLE_WINDOW_SEC": 30,
        "WHATSMINER_SAMPLE_INTERVAL_SEC": 10,
        "WHATSMINER_BASELINE_SAMPLES": 3,
        "WHATSMINER_PL_MIN_W": 2000,
        "POWER_LIMIT_W": 3500,
        "WHATSMINER_FREQ_MIN_MHZ": 500,
        "WHATSMINER_FREQ_MAX_MHZ": 700,
        "WHATSMINER_RESTART_WAIT_SEC": 90,
        "WHATSMINER_UPFREQ_TIMEOUT_SEC": 180,
        "WHATSMINER_PERPETUAL_INTERVAL_SEC": 300,
        "WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT": 5.0,
        "WHATSMINER_UPFREQ_SPEED": 5,
        "firmware_type": "whatsminer",
        "current_firmware": "whatsminer",
    }

    engine = TuningEngine(MINER_IP, config)
    engine.start()
    saved_mac = engine.mac
    try:
        deadline = time.monotonic() + 30 * 60
        while time.monotonic() < deadline:
            if len(engine.vf_surface) >= 5:
                break
            time.sleep(5)

        cells_before_stop = list(engine.vf_surface)
        _assert_with_dump(
            len(cells_before_stop) >= 5,
            f"engine did not measure 5 cells within 1800s; got {len(cells_before_stop)}",
            response={"vf_surface_count": len(cells_before_stop)},
        )
    finally:
        engine.stop()
        engine.destroy()

    time.sleep(5)  # let checkpoint flush

    # Resume with fresh engine instance
    engine2 = TuningEngine(saved_mac, config)
    engine2.start()
    try:
        deadline2 = time.monotonic() + 30 * 60
        while time.monotonic() < deadline2:
            if engine2.phase == engine2.PHASE_WHATSMINER_PERPETUAL:
                break
            if not engine2.running:
                break
            time.sleep(10)

        _assert_with_dump(
            len(engine2.vf_surface) >= 9,
            f"expected >= 9 cells after resume, got {len(engine2.vf_surface)}",
            response={"vf_surface_count": len(engine2.vf_surface)},
        )
        before_keys = {(c["power_limit_w"], c["target_freq_mhz"]) for c in cells_before_stop[:5]}
        after_5_cells = [
            c
            for c in engine2.vf_surface
            if (c["power_limit_w"], c["target_freq_mhz"]) in before_keys
        ]
        _assert_with_dump(
            len(after_5_cells) >= 5,
            f"expected the 5 pre-stop cells preserved in resume; got {len(after_5_cells)}",
            response={
                "after_5_count": len(after_5_cells),
                "before_5_count": len(cells_before_stop[:5]),
            },
        )
    finally:
        engine2.stop()
        engine2.destroy()


@pytest.mark.skip(reason="depends on b01")
@requires_live_write
def test_c01_perpetual_one_cycle(tune_state_guard, live_api):
    import time

    from tuner_app.tuning_engine.engine import TuningEngine

    config = {
        "PASSWORD": MINER_PASS,
        "API_PORT": MINER_PORT,
        "WHATSMINER_PL_COUNT": 3,
        "WHATSMINER_FREQ_COUNT": 3,
        "WHATSMINER_FINE_COUNT": 0,
        "WHATSMINER_FINE_TOP_K": 0,
        "WHATSMINER_STABILIZE_SEC": 30,
        "WHATSMINER_SAMPLE_WINDOW_SEC": 30,
        "WHATSMINER_SAMPLE_INTERVAL_SEC": 10,
        "WHATSMINER_BASELINE_SAMPLES": 3,
        "WHATSMINER_PL_MIN_W": 2000,
        "POWER_LIMIT_W": 3500,
        "WHATSMINER_FREQ_MIN_MHZ": 500,
        "WHATSMINER_FREQ_MAX_MHZ": 700,
        "WHATSMINER_RESTART_WAIT_SEC": 90,
        "WHATSMINER_UPFREQ_TIMEOUT_SEC": 180,
        "WHATSMINER_PERPETUAL_INTERVAL_SEC": 30,
        "WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT": 5.0,
        "WHATSMINER_UPFREQ_SPEED": 5,
        "firmware_type": "whatsminer",
        "current_firmware": "whatsminer",
    }

    engine = TuningEngine(MINER_IP, config)
    engine.start()
    try:
        deadline = time.monotonic() + 60 * 60
        while time.monotonic() < deadline:
            if engine.phase == engine.PHASE_WHATSMINER_PERPETUAL:
                break
            if not engine.running:
                break
            time.sleep(10)

        _assert_with_dump(
            engine.phase == engine.PHASE_WHATSMINER_PERPETUAL,
            f"engine did not reach perpetual within 3600s; final phase={engine.phase!r}",
            response={"phase": engine.phase},
        )

        initial_results_count = len(engine.whatsminer_results or [])
        time.sleep(120)  # ~2 min: one perpetual cycle (interval=30s + sample window=30s ≈ 60s)
        final_results_count = len(engine.whatsminer_results or [])

        _assert_with_dump(
            final_results_count > initial_results_count,
            (
                "perpetual loop did not append a sample "
                f"(initial={initial_results_count}, final={final_results_count})"
            ),
            response={"initial": initial_results_count, "final": final_results_count},
        )
        if engine.whatsminer_best_cell:
            last_sample = engine.whatsminer_results[-1]
            _assert_with_dump(
                last_sample.get("power_limit_w")
                == engine.whatsminer_best_cell.get("power_limit_w"),
                (
                    f"perpetual sample power_limit_w={last_sample.get('power_limit_w')!r} != "
                    f"best_cell power_limit_w={engine.whatsminer_best_cell.get('power_limit_w')!r}"
                ),
                response={"last_sample": last_sample, "best_cell": engine.whatsminer_best_cell},
            )
    finally:
        engine.stop()
        engine.destroy()
