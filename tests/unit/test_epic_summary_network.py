"""EpicMinerAPI.summary() lazy-fetches /network for canonical MAC.

Cached on first call (mirrors the _capabilities_cache pattern). Failure stores
{} sentinel so subsequent summary() calls don't keep retrying.
"""

from __future__ import annotations

from unittest.mock import call, patch

from tuner_app.miner.epic import EpicMinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError


def _summary_fixture():
    """Representative /summary shape: Hostname at top level, no model keys."""
    return {
        "Status": {"Operating State": "Mining"},
        "Hostname": "miner-example",
        "Power Supply Stats": {"Input Power": 2800.0, "Target Voltage": 13967},
        "Fans": {"Fans Speed": 20},
        "HBs": [{"Hashrate": [59000000.0, 97.0, 36.0], "Core Clock Avg": 440.0}],
    }


def _capabilities_fixture():
    """Representative /capabilities shape: Model lives here."""
    return {
        "Model": "AntMiner S21",
        "Model Subtype": "BHB68603",
        "Chip Type": "BM1368",
        "Default Clock": 460,
        "Default Voltage": 14000,
    }


def _network_fixture():
    """Representative /network shape: MAC under dhcp.mac_address."""
    return {"dhcp": {"address": "192.0.2.10", "mac_address": "02:00:5E:10:00:01"}}


def test_init_network_cache_starts_none():
    miner = EpicMinerAPI("1.2.3.4")
    assert miner._network_cache is None


def test_summary_fetches_network_on_first_call():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", return_value=_network_fixture()) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        result = miner.summary()
        assert result.mac == "02:00:5e:10:00:01"
        assert mock_net.call_count == 1


def test_summary_caches_network_response_across_calls():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", return_value=_network_fixture()) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        for _ in range(5):
            result = miner.summary()
            assert result.mac == "02:00:5e:10:00:01"
        assert mock_net.call_count == 1


def test_summary_network_returns_none_caches_empty_no_retry():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", return_value=None) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.mac is None
        assert mock_net.call_count == 1


def test_summary_network_offline_error_caches_empty_no_retry():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", side_effect=MinerOfflineError("offline")) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.mac is None
        assert mock_net.call_count == 1


def test_summary_network_command_error_caches_empty_no_retry():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(
            miner, "_network_raw", side_effect=MinerCommandError("cmd failed")
        ) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.mac is None
        assert mock_net.call_count == 1


def test_summary_network_returns_empty_dict_caches_no_retry():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", return_value={}) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.mac is None
        assert mock_net.call_count == 1


def test_summary_network_no_dhcp_block_no_mac():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", return_value={"some_other_key": "value"}) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.mac is None
        assert mock_net.call_count == 1


def test_summary_network_dhcp_block_no_mac_address():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(
            miner, "_network_raw", return_value={"dhcp": {"address": "192.0.2.10"}}
        ) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        result = miner.summary()
        assert result.mac is None
        assert mock_net.call_count == 1


def test_summary_network_fetched_from_path_slash_network():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_get") as mock_get,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):

        def get_side_effect(path):
            if path == "/summary":
                return _summary_fixture()
            if path == "/network":
                return _network_fixture()
            if path == "/capabilities":
                return _capabilities_fixture()
            return None

        mock_get.side_effect = get_side_effect
        miner.summary()
        assert call("/network") in mock_get.call_args_list


def test_summary_capabilities_and_network_caches_independent():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "_network_raw", return_value=_network_fixture()) as mock_net,
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()) as mock_caps,
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.mac == "02:00:5e:10:00:01"
            assert result.model == "AntMiner S21"
        assert mock_net.call_count == 1
        assert mock_caps.call_count == 1
