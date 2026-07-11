# tests/integration/test_bulk_legacy_miner_ips.py
from unittest.mock import Mock, patch

import pytest

from tuner_app import state
from tuner_app.config.effective import canonical_miner_key
from tuner_app.constants import _SYNTH_MAC_RE, _normalize_mac
from tuner_app.manager.tuner_manager import TunerManager


@pytest.fixture(autouse=True)
def reset_state():
    original_miner_configs = state.MINER_CONFIGS.copy()
    original_config = state.CONFIG.copy()
    yield
    state.MINER_CONFIGS.clear()
    state.MINER_CONFIGS.update(original_miner_configs)
    state.CONFIG.clear()
    state.CONFIG.update(original_config)


def _make_stub_engine():
    """Stub engine that returns the minimum dict shape get_overview needs."""
    return Mock(
        get_status=lambda: {
            "firmware_type": "epic",
            "phase": "phase_idle",
            "phase_detail": "",
            "tuned_stats": {},
            "tuning_complete": False,
            "engine_busy": False,
        },
        last_summary=None,
        _update_live_data=lambda: None,
        _get_profit_display_context=lambda: (None, None, 0),
    )


def test_get_overview_legacy_miner_ips_emits_synth_mac():
    state.MINER_CONFIGS.clear()
    state.MINER_CONFIGS["192.0.2.122"] = {"firmware_type": "epic"}
    manager = TunerManager(state.CONFIG)
    with patch.object(manager, "get_engine", return_value=_make_stub_engine()):
        overview = manager.get_overview()
        miners = overview["miners"]
        assert len(miners) == 1
        miner = miners[0]
        assert miner["mac"].startswith("syn-")
        assert _SYNTH_MAC_RE.match(miner["mac"])
        assert miner["ip"] == "192.0.2.122"


def test_get_overview_legacy_v3_ip_keyed_config_emits_synth_mac():
    state.MINER_CONFIGS.clear()
    state.MINER_CONFIGS["192.0.2.122"] = {"firmware_type": "epic"}
    manager = TunerManager(state.CONFIG)
    with patch.object(manager, "get_engine", return_value=_make_stub_engine()):
        overview = manager.get_overview()
        miners = overview["miners"]
        assert len(miners) == 1
        miner = miners[0]
        assert miner["mac"].startswith("syn-")
        assert not miner["mac"].startswith("192.168.")


def test_get_overview_v4_real_mac_unchanged():
    state.MINER_CONFIGS.clear()
    state.MINER_CONFIGS["aa:bb:cc:dd:ee:ff"] = {"ip": "192.0.2.122"}
    manager = TunerManager(state.CONFIG)
    with patch.object(manager, "get_engine", return_value=_make_stub_engine()):
        overview = manager.get_overview()
        miners = overview["miners"]
        assert len(miners) == 1
        miner = miners[0]
        assert miner["mac"] == "aa:bb:cc:dd:ee:ff"


def test_canonical_miner_key_handles_synth_encoded_ip():
    state.MINER_CONFIGS.clear()
    result1 = canonical_miner_key("syn-192-0-2-122")
    result2 = canonical_miner_key("192.0.2.122")
    assert result1 == result2


def test_synth_id_is_deterministic_for_same_ip():
    state.MINER_CONFIGS.clear()
    state.MINER_CONFIGS["192.0.2.122"] = {"firmware_type": "epic"}
    manager = TunerManager(state.CONFIG)
    with patch.object(manager, "get_engine", return_value=_make_stub_engine()):
        overview1 = manager.get_overview()
        overview2 = manager.get_overview()
        mac1 = overview1["miners"][0]["mac"]
        mac2 = overview2["miners"][0]["mac"]
        assert mac1 == mac2


def test_normalized_mac_accepts_synth_encoded_ip():
    result = _normalize_mac("syn-192-0-2-122")
    assert result == "syn-192-0-2-122"
