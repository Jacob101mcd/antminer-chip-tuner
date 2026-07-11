from __future__ import annotations

from tuner_app.miner.types import BoardSummary, MinerSummary


def test_board_summary_backward_compat_no_new_args() -> None:
    board = BoardSummary(index=0, hashrate_ths=1.0, freq_mhz=500.0)
    assert board.upfreq_complete is None
    assert board.effective_chips is None


def test_board_summary_backward_compat_explicit_new_args() -> None:
    board = BoardSummary(
        index=0,
        hashrate_ths=1.0,
        freq_mhz=500.0,
        upfreq_complete=True,
        effective_chips=108,
    )
    assert board.upfreq_complete is True
    assert board.effective_chips == 108


def test_from_whatsminer_happy_path_no_devs() -> None:
    raw_summary = {
        "Status": "Active",
        "MHS av": 200000000,
        "Power": 1500.5,
        "Fan Speed Out": 4500,
        "Miner Type": "WM-210",
        "MAC": "AA:BB:CC:DD:EE:FF",
    }
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.operating_state == "Active"
    assert summary.hashrate_ths == 200.0
    assert summary.power_w == 1500.5
    assert summary.fan_speed == 4500
    assert summary.model == "WM-210"
    assert summary.boards == []
    assert summary.raw == raw_summary


def test_from_whatsminer_fan_speed_fallback() -> None:
    # Test Fan Speed Out preferred
    raw_summary = {"Fan Speed Out": 3000, "Fan Speed In": 2500}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.fan_speed == 3000

    # Test Fan Speed In fallback
    raw_summary = {"Fan Speed In": 2500}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.fan_speed == 2500

    # Test neither key present
    raw_summary = {}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.fan_speed == 0


def test_from_whatsminer_mac_defensive_lookup_chain() -> None:
    # Test "MAC" key
    raw_summary = {"MAC": "AA-BB-CC-DD-EE-FF"}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"

    # Test "MACAddr" key
    raw_summary = {"MACAddr": "11:22:33:44:55:66"}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.mac == "11:22:33:44:55:66"

    # Test "MAC Address" key
    raw_summary = {"MAC Address": "AA:BB:CC:DD:EE:FF"}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"

    # Test empty MAC chain
    raw_summary = {}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.mac is None

    # Test all-zeros placeholder MAC
    raw_summary = {"MAC": "00:00:00:00:00:00"}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.mac is None


def test_from_whatsminer_boards_from_devs() -> None:
    raw_devs = {
        "DEVS": [
            {
                "Slot": 0,
                "MHS av": 100000000,
                "Chip Frequency": 500.0,
                "Temperature": 72.5,
                "Upfreq Complete": 1,
                "Effective Chips": 108,
            },
            {
                "Slot": 1,
                "MHS av": 120000000,
                "Chip Frequency": 520.0,
                "Temperature": 75.0,
                "Upfreq Complete": 0,
                "Effective Chips": 0,
            },
        ]
    }
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert len(summary.boards) == 2
    assert summary.boards[0].index == 0
    assert summary.boards[0].hashrate_ths == 100.0
    assert summary.boards[0].freq_mhz == 500.0
    assert summary.boards[0].temp_outlet_c == 72.5
    assert summary.boards[0].upfreq_complete is True
    assert summary.boards[0].effective_chips == 108
    assert summary.boards[1].index == 1
    assert summary.boards[1].hashrate_ths == 120.0
    assert summary.boards[1].freq_mhz == 520.0
    assert summary.boards[1].temp_outlet_c == 75.0
    assert summary.boards[1].upfreq_complete is False
    assert summary.boards[1].effective_chips is None


def test_from_whatsminer_boards_from_devs_slot_missing() -> None:
    raw_devs = {
        "DEVS": [
            {"MHS av": 100000000, "Chip Frequency": 500.0},  # No Slot
        ]
    }
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert summary.boards[0].index == 0  # Should default to list position


def test_from_whatsminer_boards_empty_devs() -> None:
    raw_devs = {"DEVS": []}
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert summary.boards == []


def test_from_whatsminer_boards_one_dev() -> None:
    raw_devs = {"DEVS": [{"Slot": 0}]}
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert len(summary.boards) == 1


def test_from_whatsminer_boards_three_devs() -> None:
    raw_devs = {"DEVS": [{"Slot": 0}, {"Slot": 1}, {"Slot": 2}]}
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert len(summary.boards) == 3


def test_from_whatsminer_boards_four_devs() -> None:
    raw_devs = {"DEVS": [{"Slot": i} for i in range(4)]}
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert len(summary.boards) == 4


def test_from_whatsminer_boards_temperature_missing() -> None:
    raw_devs = {"DEVS": [{"Slot": 0, "MHS av": 0}]}
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert summary.boards[0].temp_outlet_c is None


def test_from_whatsminer_boards_effective_chips_missing() -> None:
    raw_devs = {"DEVS": [{"Slot": 0, "MHS av": 0}]}
    raw_summary = {"Status": "Active"}
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert summary.boards[0].effective_chips is None


def test_from_whatsminer_zeros_and_missing_values() -> None:
    raw_summary = {}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.operating_state == ""
    assert summary.hashrate_ths == 0.0
    assert summary.power_w == 0.0
    assert summary.fan_speed == 0
    assert summary.model is None
    assert summary.mac is None
    assert summary.boards == []


def test_from_whatsminer_dict_shaped_status() -> None:
    raw_summary = {"Status": {"key": "value"}}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.operating_state == ""


def test_from_whatsminer_cgminer_nested_shape_parses_correctly():
    raw_summary = {
        "STATUS": [{"STATUS": "S", "Code": 11, "Msg": "Summary"}],
        "SUMMARY": [
            {"MHS av": 70200000, "Power": 3200, "Fan Speed Out": 5400, "Miner Type": "M50S+"}
        ],
    }
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.hashrate_ths == 70.2
    assert summary.power_w == 3200
    assert summary.fan_speed == 5400
    assert summary.model == "M50S+"
    assert summary.raw == raw_summary


def test_from_whatsminer_cgminer_empty_summary_array_safe_defaults():
    raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": []}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.hashrate_ths == 0.0
    assert summary.power_w == 0.0
    assert summary.fan_speed == 0
    assert summary.model is None


def test_from_whatsminer_cgminer_empty_status_array_still_parses_summary():
    raw_summary = {
        "STATUS": [],
        "SUMMARY": [{"MHS av": 50000000, "Power": 2500, "Miner Type": "M30S++"}],
    }
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.hashrate_ths == 50.0
    assert summary.power_w == 2500
    assert summary.model == "M30S++"


def test_from_whatsminer_flat_shape_still_works():
    raw_summary = {"MHS av": 100000000, "Power": 4000, "Fan Speed Out": 5000, "Miner Type": "FlatM"}
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.hashrate_ths == 100.0
    assert summary.power_w == 4000
    assert summary.fan_speed == 5000
    assert summary.model == "FlatM"


def test_from_whatsminer_msg_wrapped_shape_h616_firmware():
    """H616-platform M-series firmware (e.g. M66S++_VM30 fw 20251209.16.Rel3,
    api_ver 2.2.2) returns a btminer-wrapped response with all summary fields
    nested under a top-level `Msg` object — there is no SUMMARY array and the
    top-level keys are STATUS/When/Code/Msg/Description. The parser must
    unwrap Msg to read MHS av / Power / Miner Type. This shape was discovered
    via direct `nc` probe against the user's miner; the live-verified payload
    is shown verbatim below.
    """
    raw_summary = {
        "STATUS": "S",
        "When": 1778768933,
        "Code": 131,
        "Msg": {
            "Elapsed": 499.94,
            "MHS av": 264538480,
            "MHS 1m": 257260432,
            "Power": 3949.0,
            "Fan Speed Out": 7200,
            "Hash Stable": "false",
            "Miner Type": "M66S++_VM30",
            "Power Mode": "User",
            "Power Limit": 4000,
        },
        "Description": "",
    }
    summary = MinerSummary.from_whatsminer(raw_summary, None)
    assert summary.hashrate_ths == 264.53848
    assert summary.power_w == 3949.0
    assert summary.fan_speed == 7200
    assert summary.model == "M66S++_VM30"
    # raw stays the full wrapped response, not the unwrapped Msg subset.
    assert summary.raw == raw_summary


def test_from_whatsminer_msg_wrapped_shape_with_devs_boards():
    """Combination: H616 Msg-wrapped summary + cgminer-shape devs response
    (the actual paired calls that WhatsminerMinerAPI.summary() makes
    against an H616 miner). Verifies that the two branches don't interfere:
    summary unwraps Msg correctly AND boards are populated from raw_devs.
    """
    raw_summary = {
        "STATUS": "S",
        "Code": 131,
        "Msg": {"MHS av": 100000000, "Power": 3000.0, "Miner Type": "M66S++"},
    }
    raw_devs = {
        "STATUS": [{"STATUS": "S", "Msg": "4 ASC(s)"}],
        "DEVS": [
            {"Slot": 0, "MHS av": 25000000, "Chip Frequency": 360, "Upfreq Complete": 1},
            {"Slot": 1, "MHS av": 25000000, "Chip Frequency": 365, "Upfreq Complete": 1},
            {"Slot": 2, "MHS av": 25000000, "Chip Frequency": 358, "Upfreq Complete": 1},
            {"Slot": 3, "MHS av": 25000000, "Chip Frequency": 362, "Upfreq Complete": 1},
        ],
        "id": 1,
    }
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert summary.hashrate_ths == 100.0
    assert summary.power_w == 3000.0
    assert summary.model == "M66S++"
    assert len(summary.boards) == 4
    assert all(b.upfreq_complete for b in summary.boards)


def test_from_whatsminer_cgminer_shape_with_devs_boards():
    raw_summary = {
        "STATUS": [{"STATUS": "S", "Code": 11, "Msg": "Summary"}],
        "SUMMARY": [
            {"MHS av": 70200000, "Power": 3200, "Fan Speed Out": 5400, "Miner Type": "M50S+"}
        ],
    }
    raw_devs = {
        "DEVS": [
            {
                "Slot": 0,
                "MHS av": 30000000,
                "Chip Frequency": 600.0,
                "Upfreq Complete": 1,
                "Effective Chips": 102,
            }
        ]
    }
    summary = MinerSummary.from_whatsminer(raw_summary, raw_devs)
    assert len(summary.boards) == 1
    assert summary.boards[0].hashrate_ths == 30.0
    assert summary.boards[0].freq_mhz == 600.0
    assert summary.boards[0].upfreq_complete is True
    assert summary.boards[0].effective_chips == 102
