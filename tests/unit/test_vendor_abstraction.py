"""Unit tests for vendor abstraction layer in miner API."""

from unittest.mock import patch

import pytest

from tuner_app.miner.api import MinerAPI as ShimMinerAPI
from tuner_app.miner.base import MinerAPI
from tuner_app.miner.bixbit import BixbitMinerAPI
from tuner_app.miner.braiins import BraiinsMinerAPI
from tuner_app.miner.epic import EpicMinerAPI
from tuner_app.miner.types import BoardSummary, MinerSummary


def test_epic_is_minerapi_subclass():
    assert issubclass(EpicMinerAPI, MinerAPI)


def test_shim_resolves_minerapi_to_epic():
    assert ShimMinerAPI is EpicMinerAPI


def test_abstract_base_cannot_instantiate():
    with pytest.raises(TypeError):
        MinerAPI()
    with pytest.raises(TypeError):
        MinerAPI("1.2.3.4")


def test_firmware_type_returns_epic():
    miner = EpicMinerAPI("1.2.3.4")
    assert miner.firmware_type() == "epic"


def test_epic_summary_returns_minersummary():
    miner = EpicMinerAPI("1.2.3.4")
    mock_summary = {
        "Status": {"Operating State": "Mining"},
        "HBs": [{"Hashrate": [200000.0]}],
        "Power Supply Stats": {},
        "Fans": {},
    }
    # summary() also fetches /capabilities for the model field; stub it so the
    # test doesn't hit the network on the unreachable test IP.
    with (
        patch.object(miner, "_summary_raw", return_value=mock_summary),
        patch.object(miner, "capabilities", return_value={}),
    ):
        result = miner.summary()
        assert isinstance(result, MinerSummary)
        assert result.operating_state == "Mining"


def test_bixbit_summary_returns_minersummary():
    miner = BixbitMinerAPI("1.2.3.4")
    mock_summary = {
        "Status": "Mining",
        "HS RT": 200000000.0,
        "Power Realtime": 3500.0,
        "Fan Speed Out": 0,
    }
    with patch.object(miner, "_summary_raw", return_value=mock_summary):
        result = miner.summary()
        assert isinstance(result, MinerSummary)
        assert result.operating_state == "Mining"
        assert result.hashrate_ths == 200.0


def test_summary_abstract_enforced():
    class _AlmostCompleteAPI(MinerAPI):
        # NOTE: summary() is intentionally ABSENT to trigger TypeError
        def clocks(self):
            pass

        def temps(self):
            pass

        def temps_chip(self):
            pass

        def hashrate(self):
            pass

        def capabilities(self):
            pass

        def voltages(self):
            pass

        def set_voltage(self, mv):
            pass

        def set_clock_all(self, mhz):
            pass

        def set_clock_board(self, board_clocks):
            pass

        def set_clock_chip(self, board_index, chip_freqs):
            pass

        def set_perpetualtune(self, enabled):
            pass

        def set_coin(self, coin, stratum_configs, unique_id=False):
            pass

        def start_mining(self):
            pass

        def stop_mining(self):
            pass

        def reboot(self, delay=0):
            pass

        def authenticate(self):
            pass

        def firmware_type(self) -> str:
            return "test"

        def set_power_limit(self, watts):
            pass

    with pytest.raises(TypeError):
        _AlmostCompleteAPI("1.2.3.4")


def test_epic_and_bixbit_summary_dispatch_correctly():
    epic_miner = EpicMinerAPI("1.2.3.4")
    bixbit_miner = BixbitMinerAPI("1.2.3.4")
    mock_summary = {"Status": "Mining"}
    with (
        patch.object(epic_miner, "_summary_raw", return_value=mock_summary),
        patch.object(epic_miner, "capabilities", return_value={}),
    ):
        epic_result = epic_miner.summary()
        assert epic_result.operating_state == ""
    with patch.object(bixbit_miner, "_summary_raw", return_value=mock_summary):
        bixbit_result = bixbit_miner.summary()
        assert bixbit_result.operating_state == "Mining"


def test_epic_clocks_returns_list_of_boardsummary():
    miner = EpicMinerAPI("1.2.3.4")
    mock_clocks = [
        {"Index": 0, "Data": [500.0, 510.0, 490.0]},
        {"Index": 1, "Data": [520.0]},
    ]
    with patch.object(miner, "_clocks_raw", return_value=mock_clocks):
        result = miner.clocks()
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], BoardSummary)
        assert result[0].index == 0
        assert result[0].chip_freqs_mhz == [500.0, 510.0, 490.0]
        assert result[1].chip_freqs_mhz == [520.0]


def test_bixbit_clocks_returns_empty_list():
    miner = BixbitMinerAPI("1.2.3.4")
    result = miner.clocks()
    assert result == []


def test_clocks_abstract_enforced():
    class _AlmostCompleteAPI_NoClocksDto(MinerAPI):
        # NOTE: clocks() is intentionally ABSENT to trigger TypeError
        def summary(self):
            pass

        def temps(self):
            pass

        def temps_chip(self):
            pass

        def hashrate(self):
            pass

        def capabilities(self):
            pass

        def voltages(self):
            pass

        def set_voltage(self, mv):
            pass

        def set_clock_all(self, mhz):
            pass

        def set_clock_board(self, board_clocks):
            pass

        def set_clock_chip(self, board_index, chip_freqs):
            pass

        def set_perpetualtune(self, enabled):
            pass

        def set_coin(self, coin, stratum_configs, unique_id=False):
            pass

        def start_mining(self):
            pass

        def stop_mining(self):
            pass

        def reboot(self, delay=0):
            pass

        def authenticate(self):
            pass

        def firmware_type(self) -> str:
            return "test"

        def set_power_limit(self, watts):
            pass

    with pytest.raises(TypeError):
        _AlmostCompleteAPI_NoClocksDto("1.2.3.4")


def test_epic_temps_returns_list_of_boardsummary():
    miner = EpicMinerAPI("1.2.3.4")
    mock_temps = [
        {"Index": 0, "Data": [45.5, 67.2]},
        {"Index": 1, "Data": [48.0, 70.1]},
    ]
    with patch.object(miner, "_temps_raw", return_value=mock_temps):
        result = miner.temps()
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], BoardSummary)
        assert result[0].index == 0
        assert result[0].temp_inlet_c == 45.5
        assert result[0].temp_outlet_c == 67.2
        assert result[1].temp_inlet_c == 48.0


def test_epic_temps_chip_returns_list_of_boardsummary():
    miner = EpicMinerAPI("1.2.3.4")
    mock_temps_chip = [
        {"Index": 0, "Data": [55.0, 56.0, 57.0]},
        {"Index": 1, "Data": [60.0]},
    ]
    with patch.object(miner, "_temps_chip_raw", return_value=mock_temps_chip):
        result = miner.temps_chip()
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], BoardSummary)
        assert result[0].chip_temps_c == [55.0, 56.0, 57.0]
        assert result[1].chip_temps_c == [60.0]


def test_epic_hashrate_returns_list_of_boardsummary():
    miner = EpicMinerAPI("1.2.3.4")
    mock_hashrate = [{"Index": 0, "Data": [[500.0, 98.5], [480.0, 97.0]]}]
    with patch.object(miner, "_hashrate_raw", return_value=mock_hashrate):
        result = miner.hashrate()
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], BoardSummary)
        assert result[0].health_pct == [98.5, 97.0]
        assert result[0].hashrate_per_chip_mhs == [0.5, 0.48]


def test_bixbit_temps_returns_empty_list():
    miner = BixbitMinerAPI("1.2.3.4")
    result = miner.temps()
    assert result == []


def test_bixbit_temps_chip_returns_empty_list():
    miner = BixbitMinerAPI("1.2.3.4")
    result = miner.temps_chip()
    assert result == []


def test_bixbit_hashrate_returns_empty_list():
    miner = BixbitMinerAPI("1.2.3.4")
    result = miner.hashrate()
    assert result == []


def test_temps_abstract_enforced():
    class _AlmostCompleteAPI_NoTempsDto(MinerAPI):
        # NOTE: temps() is intentionally ABSENT to trigger TypeError
        def summary(self):
            pass

        def clocks(self):
            pass

        def temps_chip(self):
            pass

        def hashrate(self):
            pass

        def capabilities(self):
            pass

        def voltages(self):
            pass

        def set_voltage(self, mv):
            pass

        def set_clock_all(self, mhz):
            pass

        def set_clock_board(self, board_clocks):
            pass

        def set_clock_chip(self, board_index, chip_freqs):
            pass

        def set_perpetualtune(self, enabled):
            pass

        def set_coin(self, coin, stratum_configs, unique_id=False):
            pass

        def start_mining(self):
            pass

        def stop_mining(self):
            pass

        def reboot(self, delay=0):
            pass

        def authenticate(self):
            pass

        def firmware_type(self) -> str:
            return "test"

        def set_power_limit(self, watts):
            pass

    with pytest.raises(TypeError):
        _AlmostCompleteAPI_NoTempsDto("1.2.3.4")


def test_temps_chip_abstract_enforced():
    class _AlmostCompleteAPI_NoTempsChipDto(MinerAPI):
        # NOTE: temps_chip() is intentionally ABSENT to trigger TypeError
        def summary(self):
            pass

        def clocks(self):
            pass

        def temps(self):
            pass

        def hashrate(self):
            pass

        def capabilities(self):
            pass

        def voltages(self):
            pass

        def set_voltage(self, mv):
            pass

        def set_clock_all(self, mhz):
            pass

        def set_clock_board(self, board_clocks):
            pass

        def set_clock_chip(self, board_index, chip_freqs):
            pass

        def set_perpetualtune(self, enabled):
            pass

        def set_coin(self, coin, stratum_configs, unique_id=False):
            pass

        def start_mining(self):
            pass

        def stop_mining(self):
            pass

        def reboot(self, delay=0):
            pass

        def authenticate(self):
            pass

        def firmware_type(self) -> str:
            return "test"

        def set_power_limit(self, watts):
            pass

    with pytest.raises(TypeError):
        _AlmostCompleteAPI_NoTempsChipDto("1.2.3.4")


def test_hashrate_abstract_enforced():
    class _AlmostCompleteAPI_NoHashrateDto(MinerAPI):
        # NOTE: hashrate() is intentionally ABSENT to trigger TypeError
        def summary(self):
            pass

        def clocks(self):
            pass

        def temps(self):
            pass

        def temps_chip(self):
            pass

        def capabilities(self):
            pass

        def voltages(self):
            pass

        def set_voltage(self, mv):
            pass

        def set_clock_all(self, mhz):
            pass

        def set_clock_board(self, board_clocks):
            pass

        def set_clock_chip(self, board_index, chip_freqs):
            pass

        def set_perpetualtune(self, enabled):
            pass

        def set_coin(self, coin, stratum_configs, unique_id=False):
            pass

        def start_mining(self):
            pass

        def stop_mining(self):
            pass

        def reboot(self, delay=0):
            pass

        def authenticate(self):
            pass

        def firmware_type(self) -> str:
            return "test"

        def set_power_limit(self, watts):
            pass

    with pytest.raises(TypeError):
        _AlmostCompleteAPI_NoHashrateDto("1.2.3.4")


# ---------------------------------------------------------------------------
# Braiins vendor abstraction smoke tests
# ---------------------------------------------------------------------------


def test_braiins_is_minerapi_subclass():
    assert issubclass(BraiinsMinerAPI, MinerAPI)


def test_braiins_firmware_type_returns_braiins():
    miner = BraiinsMinerAPI("1.2.3.4")
    assert miner.firmware_type() == "braiins"


def test_braiins_clocks_returns_empty_list():
    miner = BraiinsMinerAPI("1.2.3.4")
    assert miner.clocks() == []


def test_braiins_temps_returns_empty_list():
    miner = BraiinsMinerAPI("1.2.3.4")
    assert miner.temps() == []


def test_braiins_temps_chip_returns_empty_list():
    miner = BraiinsMinerAPI("1.2.3.4")
    assert miner.temps_chip() == []


def test_braiins_hashrate_returns_empty_list():
    miner = BraiinsMinerAPI("1.2.3.4")
    assert miner.hashrate() == []


def test_braiins_summary_returns_minersummary_type():
    """summary() delegates to from_braiins and returns a MinerSummary with boards==[]."""
    miner = BraiinsMinerAPI("1.2.3.4")
    raw_details = {
        "uid": "u1",
        "platform": "am3",
        "bos_mode": "plus",
        "hostname": "miner-test",
        "mac_address": "aa:bb:cc",
        "system_uptime": "1d",
        "bosminer_uptime_s": 1,
        "system_uptime_s": 1,
        "status": 2,
        "kernel_version": "5.4",
        "control_board_soc_family": "zynq",
    }
    raw_stats = {
        "miner_stats": {
            "real_hashrate": {"last_1m": {"gigahash_per_second": 95_000.0}},
        },
        "power_stats": {"approximated_consumption": {"watt": 3400}},
    }
    raw_cooling = {"fans": [{"rpm": 2800, "position": 0, "target_speed_ratio": 0.6}]}
    with (
        patch.object(miner, "_summary_details_raw", return_value=raw_details),
        patch.object(miner, "_summary_stats_raw", return_value=raw_stats),
        patch.object(miner, "_summary_cooling_raw", return_value=raw_cooling),
    ):
        result = miner.summary()

    assert isinstance(result, MinerSummary)
    assert result.boards == []
    assert result.operating_state == "normal"
    assert abs(result.hashrate_ths - 95.0) < 0.01
