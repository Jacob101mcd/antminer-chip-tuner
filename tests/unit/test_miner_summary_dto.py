from __future__ import annotations

from tuner_app.miner.types import BoardSummary, MinerSummary


def test_from_epic_full_fixture():
    raw = {
        "Status": {"Operating State": "Mining"},
        "Power Supply Stats": {
            "Input Power": 4200.0,
            "Target Voltage": 14500,
            "Output Voltage": 14.5,
        },
        "Fans": {"Fans Speed": 3000},
        "Network": {"Hostname": "miner1"},
        "Type": "Antminer S21",
        "HBs": [
            {
                "Index": 0,
                "Hashrate": [200000.0, 0, 95.5],
                "Core Clock Avg": 500.0,
                "Input Voltage": 14500,
            },
            {
                "Index": 1,
                "Hashrate": [210000.0, 0, 96.0],
                "Core Clock Avg": 543.75,
                "Input Voltage": 14500,
            },
            {
                "Index": 2,
                "Hashrate": [190000.0, 0, 94.5],
                "Core Clock Avg": 479.17,
                "Input Voltage": 14500,
            },
        ],
    }
    summary = MinerSummary.from_epic(raw)
    assert summary.operating_state == "Mining"
    assert abs(summary.hashrate_ths - (200000.0 + 210000.0 + 190000.0) / 1e6) < 1e-9
    assert summary.power_w == 4200.0
    assert summary.target_voltage_mv == 14500.0
    assert summary.output_voltage_mv == 14500.0
    assert summary.fan_speed == 3000
    assert summary.hostname == "miner1"
    assert summary.model == "Antminer S21"
    assert len(summary.boards) == 3
    assert summary.boards[0].index == 0
    assert abs(summary.boards[0].hashrate_ths - 200000.0 / 1e6) < 1e-9
    assert summary.boards[0].freq_mhz == 500.0
    assert summary.boards[0].target_voltage_mv == 14500.0
    assert summary.boards[0].board_health_pct == 95.5
    assert summary.boards[0].chip_freqs_mhz == []
    assert summary.boards[0].chip_temps_c == []
    assert summary.raw is raw


def test_from_epic_empty_dict():
    summary = MinerSummary.from_epic({})
    assert summary.operating_state == ""
    assert summary.hashrate_ths == 0.0
    assert summary.power_w == 0.0
    assert summary.target_voltage_mv is None
    assert summary.output_voltage_mv is None
    assert summary.fan_speed == 0
    assert summary.hostname is None
    assert summary.model is None
    assert summary.boards == []
    assert summary.raw == {}


def test_from_epic_missing_hbs():
    summary = MinerSummary.from_epic({"Status": {"Operating State": "Mining"}})
    assert summary.boards == []


def test_from_epic_three_element_hashrate():
    raw = {
        "Status": {"Operating State": "Mining"},
        "HBs": [
            {"Index": 0, "Hashrate": [200000.0, 0, 95.5], "Core Clock Avg": 500.0},
        ],
    }
    summary = MinerSummary.from_epic(raw)
    board = summary.boards[0]
    assert board.board_health_pct == 95.5
    assert abs(board.hashrate_ths - 200000.0 / 1e6) < 1e-9


def test_from_epic_one_element_hashrate():
    raw = {
        "Status": {"Operating State": "Mining"},
        "HBs": [
            {"Index": 0, "Hashrate": [200000.0]},
        ],
    }
    summary = MinerSummary.from_epic(raw)
    board = summary.boards[0]
    assert board.board_health_pct is None
    assert abs(board.hashrate_ths - 200000.0 / 1e6) < 1e-9


def test_from_epic_output_voltage_conversion():
    raw = {
        "Power Supply Stats": {"Output Voltage": 14.5},
    }
    summary = MinerSummary.from_epic(raw)
    assert summary.output_voltage_mv == 14500.0


def test_from_epic_target_voltage_absent():
    raw = {
        "Power Supply Stats": {"Input Power": 4000},
    }
    summary = MinerSummary.from_epic(raw)
    assert summary.target_voltage_mv is None


def test_from_epic_representative_top_level_hostname():
    """ePIC PowerPlay-BMS v1.17.x returns Hostname at the top level of /summary,
    not nested under Network. Pin against a representative S21 API shape
    (BHB68603, firmware v1.17.1) so we don't regress to the
    Network.Hostname assumption again."""
    raw = {
        "Status": {"Operating State": "Mining"},
        "Hostname": "miner-example",
        "Power Supply Stats": {"Input Power": 2800.0, "Target Voltage": 13967},
        "Fans": {"Fans Speed": 20},
        "HBs": [{"Hashrate": [59000000.0, 97.0, 36.0], "Core Clock Avg": 440.0}],
    }
    summary = MinerSummary.from_epic(raw)
    assert summary.hostname == "miner-example"
    # Real /summary doesn't have Type/Model/MinerType — model stays None until
    # EpicMinerAPI.summary() merges in the cached /capabilities response.
    assert summary.model is None


def test_from_epic_top_level_hostname_takes_precedence_over_network():
    """When both top-level Hostname AND Network.Hostname are present, prefer
    the top-level (matches the live firmware shape)."""
    raw = {
        "Hostname": "top-level",
        "Network": {"Hostname": "network-block"},
    }
    summary = MinerSummary.from_epic(raw)
    assert summary.hostname == "top-level"


def test_from_epic_network_hostname_fallback():
    """Forward-compat: if a firmware variant returns Hostname only under
    Network, the fallback path still extracts it."""
    raw = {"Network": {"Hostname": "miner-via-network"}}
    summary = MinerSummary.from_epic(raw)
    assert summary.hostname == "miner-via-network"


def test_from_epic_model_fallback_chain():
    # Only Type set
    raw1 = {"Type": "Antminer S21"}
    summary1 = MinerSummary.from_epic(raw1)
    assert summary1.model == "Antminer S21"

    # Only Model set (no Type)
    raw2 = {"Model": "M50"}
    summary2 = MinerSummary.from_epic(raw2)
    assert summary2.model == "M50"

    # Only MinerType set (no Type or Model)
    raw3 = {"MinerType": "T21"}
    summary3 = MinerSummary.from_epic(raw3)
    assert summary3.model == "T21"


def test_from_epic_raw_is_verbatim():
    raw = {"a": 1}
    summary = MinerSummary.from_epic(raw)
    assert summary.raw is raw


def test_from_bixbit_full_fixture():
    raw = {
        "STATUS": "S",
        "Status": "Mining",
        "HS RT": 200000000.0,
        "Power Realtime": 4200,
        "Miner Type": "M50S+",
        "PSU Vout": 14.5,
        "Fan Speed Out": 6000,
    }
    summary = MinerSummary.from_bixbit(raw)
    assert summary.operating_state == "Mining"
    assert summary.hashrate_ths == 200.0
    assert summary.power_w == 4200.0
    assert summary.model == "M50S+"
    assert summary.output_voltage_mv == 14500.0
    assert summary.fan_speed == 6000
    assert summary.target_voltage_mv is None
    assert summary.hostname is None
    assert summary.boards == []


def test_from_bixbit_empty_dict():
    summary = MinerSummary.from_bixbit({})
    assert summary.operating_state == ""


def test_from_bixbit_power_fallback():
    raw = {"Status": "Mining", "Power": 4100}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.power_w == 4100.0


def test_from_bixbit_no_hs_rt():
    raw = {"Status": "Mining"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.hashrate_ths == 0.0


def test_from_bixbit_status_dict_defensive():
    raw = {"Status": {"unexpected": "shape"}}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.operating_state == ""


def test_from_bixbit_psu_vout_zero():
    raw = {"Status": "Mining", "PSU Vout": 0}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.output_voltage_mv is None


def test_from_bixbit_raw_is_verbatim():
    raw = {"a": 1}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.raw is raw


def test_is_hashing_epic_positive_hbs():
    raw = {
        "Status": {"Operating State": "Mining"},
        "HBs": [
            {"Index": 0, "Hashrate": [100000.0]},
        ],
    }
    summary = MinerSummary.from_epic(raw)
    assert summary.is_hashing is True


def test_is_hashing_bixbit_positive_hsrt():
    raw = {
        "Status": "Mining",
        "HS RT": 100000000.0,
    }
    summary = MinerSummary.from_bixbit(raw)
    assert summary.is_hashing is True


def test_is_hashing_zero_total_zero_boards():
    summary = MinerSummary.from_epic({})
    assert summary.is_hashing is False


def test_is_hashing_epic_zero_total_one_board_positive():
    board = BoardSummary(index=0, hashrate_ths=0.5, freq_mhz=500.0)
    summary = MinerSummary(
        operating_state="", hashrate_ths=0.0, power_w=0.0, fan_speed=0, boards=[board]
    )
    assert summary.is_hashing is True


# ---------------------------------------------------------------------------
# MinerSummary.mac field tests
# ---------------------------------------------------------------------------

# Cross-vendor: default value
# ---------------------------------------------------------------------------


def test_mac_default_none_on_direct_construction():
    """MinerSummary constructed directly has mac=None by default."""
    summary = MinerSummary(operating_state="", hashrate_ths=0, power_w=0, fan_speed=0)
    assert summary.mac is None


# ePIC-vendor MAC tests
# ---------------------------------------------------------------------------
# (ePIC MAC tests are appended to this file by the Unit 1 Test Writer.)


# Bixbit-vendor MAC tests
# ---------------------------------------------------------------------------


def test_from_bixbit_mac_field_present():
    """from_bixbit extracts MAC from raw['MAC'] when present."""
    raw = {
        "STATUS": "S",
        "Status": "Mining",
        "HS RT": 200000000.0,
        "Power Realtime": 4200,
        "Miner Type": "M50S+",
        "PSU Vout": 14.5,
        "Fan Speed Out": 6000,
        "MAC": "aa:bb:cc:dd:ee:ff",
    }
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_bixbit_mac_field_absent():
    """from_bixbit returns mac=None when no MAC field is present (typical Whatsminer shape)."""
    raw = {
        "STATUS": "S",
        "Status": "Mining",
        "HS RT": 200000000.0,
        "Power Realtime": 4200,
        "Miner Type": "M50S+",
        "PSU Vout": 14.5,
        "Fan Speed Out": 6000,
    }
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac is None


def test_from_bixbit_mac_all_zeros_placeholder():
    """from_bixbit returns mac=None when the MAC value is the all-zeros placeholder."""
    raw = {"Status": "Mining", "MAC": "00:00:00:00:00:00"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac is None


def test_from_bixbit_mac_empty_string():
    """from_bixbit returns mac=None when the MAC field is an empty string."""
    raw = {"Status": "Mining", "MAC": ""}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac is None


def test_from_bixbit_mac_uppercase_normalization():
    """from_bixbit normalizes an uppercase colon-separated MAC to lowercase."""
    raw = {"Status": "Mining", "MAC": "AA:BB:CC:DD:EE:FF"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_bixbit_mac_dash_normalization():
    """from_bixbit normalizes a dash-separated MAC to lowercase colon form."""
    raw = {"Status": "Mining", "MAC": "aa-bb-cc-dd-ee-ff"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


# LuxOS-vendor MAC tests
# ---------------------------------------------------------------------------


def test_from_luxos_mac_from_config():
    """from_luxos extracts MAC from raw_config['CONFIG'][0]['MACAddr']."""
    raw_summary = {"SUMMARY": [{"GHS av": 200.0}], "STATUS": [{"STATUS": "S"}]}
    raw_config = {"CONFIG": [{"MACAddr": "02:00:5e:10:00:02", "Hostname": "miner-example"}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac == "02:00:5e:10:00:02"


def test_from_luxos_mac_raw_config_none():
    """from_luxos returns mac=None when raw_config kwarg is not provided."""
    raw_summary = {"SUMMARY": [{"GHS av": 200.0}], "STATUS": [{"STATUS": "S"}]}
    summary = MinerSummary.from_luxos(raw_summary)
    assert summary.mac is None


def test_from_luxos_mac_config_list_empty():
    """from_luxos returns mac=None when raw_config CONFIG list is empty."""
    raw_summary = {"SUMMARY": [{"GHS av": 200.0}], "STATUS": [{"STATUS": "S"}]}
    raw_config = {"CONFIG": []}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac is None


def test_from_luxos_mac_field_absent_in_config():
    """from_luxos returns mac=None when CONFIG[0] has no MACAddr key."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    raw_config = {"CONFIG": [{"Hostname": "miner-example"}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac is None


def test_from_luxos_mac_all_zeros_placeholder():
    """from_luxos returns mac=None when MACAddr is the all-zeros placeholder."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    raw_config = {"CONFIG": [{"MACAddr": "00:00:00:00:00:00"}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac is None


def test_from_luxos_mac_empty_string():
    """from_luxos returns mac=None when MACAddr is an empty string."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    raw_config = {"CONFIG": [{"MACAddr": ""}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac is None


def test_from_luxos_mac_uppercase_normalization():
    """from_luxos normalizes an uppercase colon-separated MAC to lowercase."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    raw_config = {"CONFIG": [{"MACAddr": "AA:BB:CC:DD:EE:FF"}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_luxos_mac_dash_normalization():
    """from_luxos normalizes a dash-separated MAC to lowercase colon form."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    raw_config = {"CONFIG": [{"MACAddr": "aa-bb-cc-dd-ee-ff"}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


# Braiins-vendor MAC tests
# ---------------------------------------------------------------------------

_BRAIINS_STATS_RAW = {
    "miner_stats": {
        "real_hashrate": {"last_1m": {"gigahash_per_second": 100_000.0}},
    },
    "power_stats": {"approximated_consumption": {"watt": 3500}},
}
_BRAIINS_COOLING_RAW = {
    "fans": [{"rpm": 3600, "position": 0, "target_speed_ratio": 0.7}],
}


def test_from_braiins_mac_from_details():
    """from_braiins extracts MAC from raw_details['mac_address'] in canonical form."""
    raw_details = {
        "uid": "uid1",
        "platform": "am3",
        "bos_mode": "plus",
        "hostname": "miner01",
        "mac_address": "aa:bb:cc:dd:ee:ff",
        "status": 2,
        "miner_identity": {"brand": 1, "miner_model": "Antminer S19", "name": "S19"},
    }
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_braiins_mac_field_absent():
    """from_braiins returns mac=None when mac_address is not in raw_details."""
    raw_details = {
        "uid": "uid1",
        "hostname": "miner01",
        "status": 2,
        "miner_identity": {"miner_model": "Antminer S19"},
    }
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac is None


def test_from_braiins_mac_all_zeros_placeholder():
    """from_braiins returns mac=None when mac_address is the all-zeros placeholder."""
    raw_details = {
        "hostname": "miner01",
        "status": 2,
        "mac_address": "00:00:00:00:00:00",
    }
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac is None


def test_from_braiins_mac_empty_string():
    """from_braiins returns mac=None when mac_address is an empty string."""
    raw_details = {
        "hostname": "miner01",
        "status": 2,
        "mac_address": "",
    }
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac is None


def test_from_braiins_mac_uppercase_normalization():
    """from_braiins normalizes an uppercase colon-separated MAC to lowercase."""
    raw_details = {
        "hostname": "miner01",
        "status": 2,
        "mac_address": "AA:BB:CC:DD:EE:FF",
    }
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_braiins_mac_dash_normalization():
    """from_braiins normalizes a dash-separated MAC to lowercase colon form."""
    raw_details = {
        "hostname": "miner01",
        "status": 2,
        "mac_address": "aa-bb-cc-dd-ee-ff",
    }
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


# ---------------------------------------------------------------------------
# Skeptic-round revision additions (concerns #1–#7)
# ---------------------------------------------------------------------------

# Concern #4 (MAJOR) — Bixbit fallback chain
# ---------------------------------------------------------------------------


def test_from_bixbit_mac_macaddr_fallback_when_mac_absent():
    """from_bixbit falls back to MACAddr when the MAC key is absent."""
    raw = {"Status": "Mining", "MACAddr": "aa:bb:cc:dd:ee:ff"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_bixbit_mac_space_field_fallback():
    """from_bixbit falls back to 'MAC Address' (with space) when both MAC and MACAddr are absent."""
    raw = {"Status": "Mining", "MAC Address": "aa:bb:cc:dd:ee:ff"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


# Concern #5 (MAJOR) — non-string MAC value handling across all vendors
# ---------------------------------------------------------------------------

_NON_STRING_MAC_VALUES = [None, 12345, ["aa:bb:cc:dd:ee:ff"], True, {"nested": "dict"}]


def test_from_bixbit_mac_non_string_values_return_none():
    """from_bixbit returns mac=None and does not raise for any non-string MAC value."""
    for value in _NON_STRING_MAC_VALUES:
        raw = {"MAC": value}
        summary = MinerSummary.from_bixbit(raw)
        assert summary.mac is None, f"Expected None for MAC={value!r}, got {summary.mac!r}"


def test_from_luxos_mac_non_string_values_return_none():
    """from_luxos returns mac=None and does not raise for any non-string MACAddr value."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    for value in _NON_STRING_MAC_VALUES:
        raw_config = {"CONFIG": [{"MACAddr": value}]}
        summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
        assert summary.mac is None, f"Expected None for MACAddr={value!r}, got {summary.mac!r}"


def test_from_braiins_mac_non_string_values_return_none():
    """from_braiins returns mac=None and does not raise for any non-string mac_address value."""
    for value in _NON_STRING_MAC_VALUES:
        raw_details = {"mac_address": value}
        summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
        assert summary.mac is None, f"Expected None for mac_address={value!r}, got {summary.mac!r}"


# Concern #6 (MAJOR) — LuxOS raw_config empty dict
# ---------------------------------------------------------------------------


def test_from_luxos_mac_raw_config_empty_dict():
    """from_luxos returns mac=None when raw_config is an empty dict (no CONFIG key)."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    summary = MinerSummary.from_luxos(raw_summary, raw_config={})
    assert summary.mac is None


# Concern #7 (MAJOR) — Braiins minimal fixture
# ---------------------------------------------------------------------------


def test_from_braiins_mac_minimal_fixture():
    """from_braiins extracts mac_address from a minimal raw_details dict without raising."""
    raw_details = {"mac_address": "aa:bb:cc:dd:ee:ff"}
    summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


# ---------------------------------------------------------------------------
# Skeptic-round-2 revision additions (concerns #1-3)
# ---------------------------------------------------------------------------


def test_from_bixbit_mac_macaddr_takes_precedence_over_mac_address_field():
    """Bixbit MACAddr takes precedence over 'MAC Address' when both are present."""
    raw = {"Status": "Mining", "MACAddr": "aa:bb:cc:11:22:33", "MAC Address": "11:22:33:44:55:66"}
    summary = MinerSummary.from_bixbit(raw)
    assert summary.mac == "aa:bb:cc:11:22:33"


def test_from_bixbit_mac_alt_format_all_zeros_suppressed():
    """from_bixbit suppresses bare-hex and dash-form all-zeros MAC values."""
    for zero_mac in ("000000000000", "00-00-00-00-00-00"):
        raw = {"Status": "Mining", "MAC": zero_mac}
        summary = MinerSummary.from_bixbit(raw)
        assert summary.mac is None, f"Expected None for MAC={zero_mac!r}, got {summary.mac!r}"


def test_from_luxos_mac_alt_format_all_zeros_suppressed():
    """from_luxos suppresses bare-hex and dash-form all-zeros MACAddr values."""
    raw_summary = {"SUMMARY": [{}], "STATUS": [{}]}
    for zero_mac in ("000000000000", "00-00-00-00-00-00"):
        raw_config = {"CONFIG": [{"MACAddr": zero_mac}]}
        summary = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
        assert summary.mac is None, f"Expected None for MACAddr={zero_mac!r}, got {summary.mac!r}"


def test_from_braiins_mac_alt_format_all_zeros_suppressed():
    """from_braiins suppresses bare-hex and dash-form all-zeros mac_address values."""
    for zero_mac in ("000000000000", "00-00-00-00-00-00"):
        raw_details = {"mac_address": zero_mac}
        summary = MinerSummary.from_braiins(raw_details, _BRAIINS_STATS_RAW, _BRAIINS_COOLING_RAW)
        assert summary.mac is None, f"Expected None for {zero_mac!r}, got {summary.mac!r}"


# Unit 1: ePIC MAC tests via /network endpoint
def test_from_epic_mac_from_network_dhcp_canonical_lowercase():
    """Extracts MAC from raw_network.dhcp.mac_address in canonical lowercase form."""
    summary = MinerSummary.from_epic(
        {"some": "data"}, raw_network={"dhcp": {"mac_address": "02:00:5E:10:00:01"}}
    )
    assert summary.mac == "02:00:5e:10:00:01"


def test_from_epic_mac_raw_network_omitted_returns_none():
    """Test that MAC is None when raw_network kwarg is omitted."""
    summary = MinerSummary.from_epic({"some": "data"})
    assert summary.mac is None


def test_from_epic_mac_raw_network_none_returns_none():
    """Test that MAC is None when raw_network is explicitly None."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network=None)
    assert summary.mac is None


def test_from_epic_mac_raw_network_empty_dict_returns_none():
    """Test that MAC is None when raw_network is an empty dict."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network={})
    assert summary.mac is None


def test_from_epic_mac_dhcp_block_missing_returns_none():
    """Test that MAC is None when dhcp key is missing from raw_network."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network={"other": "data"})
    assert summary.mac is None


def test_from_epic_mac_dhcp_no_mac_address_returns_none():
    """Test that MAC is None when dhcp block exists but has no mac_address key."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network={"dhcp": {}})
    assert summary.mac is None


def test_from_epic_mac_all_zeros_placeholder_returns_none():
    """Test that MAC is None when mac_address is all zeros."""
    summary = MinerSummary.from_epic(
        {"some": "data"}, raw_network={"dhcp": {"mac_address": "00:00:00:00:00:00"}}
    )
    assert summary.mac is None


def test_from_epic_mac_empty_string_returns_none():
    """Test that MAC is None when mac_address is an empty string."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network={"dhcp": {"mac_address": ""}})
    assert summary.mac is None


def test_from_epic_mac_non_string_returns_none():
    """Test that MAC is None when mac_address is non-string, and no exception is raised."""
    _NON_STRING_MAC_VALUES = [None, 12345, ["02:00:5E:10:00:01"], True, {"nested": "dict"}]
    for value in _NON_STRING_MAC_VALUES:
        summary = MinerSummary.from_epic(
            {"some": "data"}, raw_network={"dhcp": {"mac_address": value}}
        )
        assert summary.mac is None


def test_from_epic_mac_dash_normalization():
    """Test that MAC is normalized from dash-separated format to canonical colon format."""
    summary = MinerSummary.from_epic(
        {"some": "data"}, raw_network={"dhcp": {"mac_address": "AA-BB-CC-DD-EE-FF"}}
    )
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_epic_mac_bare_hex_normalization():
    """Test that MAC is normalized from bare hex format to canonical colon format."""
    summary = MinerSummary.from_epic(
        {"some": "data"}, raw_network={"dhcp": {"mac_address": "aabbccddeeff"}}
    )
    assert summary.mac == "aa:bb:cc:dd:ee:ff"


def test_from_epic_mac_legacy_network_block_now_ignored():
    """Test that legacy Network.MAC path is ignored and MAC is None."""
    summary = MinerSummary.from_epic({"Network": {"MAC": "aa:bb:cc:dd:ee:ff"}})
    assert summary.mac is None


def test_from_epic_mac_legacy_top_level_mac_now_ignored():
    """Test that legacy top-level MAC path is ignored and MAC is None."""
    summary = MinerSummary.from_epic({"MAC": "aa:bb:cc:dd:ee:ff"})
    assert summary.mac is None


def test_from_epic_mac_dhcp_not_a_dict_returns_none():
    """Test that MAC is None when dhcp is not a dict."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network={"dhcp": "not_a_dict"})
    assert summary.mac is None


def test_from_epic_mac_raw_network_not_a_dict_returns_none():
    """Test that MAC is None when raw_network is not a dict (defense-in-depth)."""
    summary = MinerSummary.from_epic({"some": "data"}, raw_network="not_a_dict")
    assert summary.mac is None
