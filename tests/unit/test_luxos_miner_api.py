from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock

from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.luxos import LuxosMinerAPI
from tuner_app.miner.types import HardwareTopology, MinerSummary


class TestFromLuxosParser(TestCase):
    def test_hashrate_ths_converts_ghs_av(self):
        raw = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]}
        result = MinerSummary.from_luxos(raw)
        self.assertEqual(result.hashrate_ths, 200.0)

    def test_hashrate_ths_zero_when_ghs_av_absent(self):
        raw = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{}]}
        result = MinerSummary.from_luxos(raw)
        self.assertEqual(result.hashrate_ths, 0.0)

    def test_operating_state_mining_when_ghs_av_positive(self):
        raw = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 100_000.0}]}
        result = MinerSummary.from_luxos(raw)
        self.assertEqual(result.operating_state, "Mining")

    def test_operating_state_idle_when_ghs_av_zero(self):
        raw = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0.0}]}
        result = MinerSummary.from_luxos(raw)
        self.assertEqual(result.operating_state, "Idle")

    def test_operating_state_offline_when_status_e(self):
        raw = {"STATUS": [{"STATUS": "E"}], "SUMMARY": [{"GHS av": 100_000.0}]}
        result = MinerSummary.from_luxos(raw)
        self.assertEqual(result.operating_state, "Offline")

    def test_power_w_from_raw_stats(self):
        raw_stats = {"STATUS": [{"STATUS": "S"}], "STATS": [{"foo": "bar"}, {"Power": 3500.5}]}
        result = MinerSummary.from_luxos({}, raw_stats=raw_stats)
        self.assertEqual(result.power_w, 3500.5)

    def test_power_w_zero_when_raw_stats_none(self):
        result = MinerSummary.from_luxos({}, raw_stats=None)
        self.assertEqual(result.power_w, 0.0)

    def test_power_w_zero_when_stats_missing_power_field(self):
        raw_stats = {"STATUS": [{"STATUS": "S"}], "STATS": [{"foo": "bar"}, {"bar": "foo"}]}
        result = MinerSummary.from_luxos({}, raw_stats=raw_stats)
        self.assertEqual(result.power_w, 0.0)

    def test_model_from_raw_version_type(self):
        raw_version = {
            "STATUS": [{"STATUS": "S"}],
            "VERSION": [{"Type": "Antminer S21", "LUXminer": "x"}],
        }
        result = MinerSummary.from_luxos({}, raw_version=raw_version)
        self.assertEqual(result.model, "Antminer S21")

    def test_model_none_when_raw_version_none(self):
        result = MinerSummary.from_luxos({}, raw_version=None)
        self.assertIsNone(result.model)

    def test_hostname_none_by_default(self):
        result = MinerSummary.from_luxos({})
        self.assertIsNone(result.hostname)

    # ── Issue #34: aux fields plumbing (power/hostname/fan_speed) ──────────

    def test_power_from_raw_tunerstatus(self):
        """TUNERSTATUS[0].Power takes priority over STATS Power."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_tunerstatus = {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": [{"Power": 3500.5}]}
        result = MinerSummary.from_luxos(raw_summary, raw_tunerstatus=raw_tunerstatus)
        self.assertEqual(result.power_w, 3500.5)

    def test_power_falls_back_to_raw_stats_when_tunerstatus_missing(self):
        """When raw_tunerstatus is None, falls back to STATS[i].Power (backward compat)."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_stats = {"STATS": [{"foo": "bar"}, {"Power": 4000.0}]}
        result = MinerSummary.from_luxos(raw_summary, raw_stats=raw_stats, raw_tunerstatus=None)
        self.assertEqual(result.power_w, 4000.0)

    def test_power_zero_when_both_aux_dicts_none(self):
        """power_w is 0.0 when both raw_tunerstatus and raw_stats are None."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        result = MinerSummary.from_luxos(raw_summary, raw_tunerstatus=None, raw_stats=None)
        self.assertEqual(result.power_w, 0.0)

    def test_power_zero_when_tunerstatus_has_no_power_field(self):
        """power_w is 0.0 when TUNERSTATUS[0] exists but has no Power field."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_tunerstatus = {"TUNERSTATUS": [{"foo": "bar"}]}
        result = MinerSummary.from_luxos(
            raw_summary, raw_tunerstatus=raw_tunerstatus, raw_stats=None
        )
        self.assertEqual(result.power_w, 0.0)

    def test_hostname_from_raw_config(self):
        """Hostname is read from CONFIG[0].Hostname."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_config = {"STATUS": [{"STATUS": "S"}], "CONFIG": [{"Hostname": "luxminer-12.local"}]}
        result = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
        self.assertEqual(result.hostname, "luxminer-12.local")

    def test_hostname_none_when_config_missing_hostname_field(self):
        """hostname is None when CONFIG[0] exists but has no Hostname key."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_config = {"CONFIG": [{"foo": "bar"}]}
        result = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
        self.assertIsNone(result.hostname)

    def test_hostname_strips_whitespace(self):
        """Leading/trailing whitespace in Hostname is stripped."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_config = {"CONFIG": [{"Hostname": "  luxminer-12  "}]}
        result = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
        self.assertEqual(result.hostname, "luxminer-12")

    def test_hostname_empty_string_becomes_none(self):
        """An empty Hostname string (after stripping) normalises to None."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_config = {"CONFIG": [{"Hostname": ""}]}
        result = MinerSummary.from_luxos(raw_summary, raw_config=raw_config)
        self.assertIsNone(result.hostname)

    def test_fan_speed_from_raw_fans_rpm(self):
        """fan_speed is read from FANS[0].RPM."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_fans = {"STATUS": [{"STATUS": "S"}], "FANS": [{"RPM": 4500}]}
        result = MinerSummary.from_luxos(raw_summary, raw_fans=raw_fans)
        self.assertEqual(result.fan_speed, 4500)

    def test_fan_speed_from_raw_fans_speed_fallback(self):
        """fan_speed falls back to FANS[0].Speed when RPM key is absent."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_fans = {"FANS": [{"Speed": 3800}]}
        result = MinerSummary.from_luxos(raw_summary, raw_fans=raw_fans)
        self.assertEqual(result.fan_speed, 3800)

    def test_fan_speed_zero_when_raw_fans_none(self):
        """fan_speed is 0 when raw_fans is None."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        result = MinerSummary.from_luxos(raw_summary, raw_fans=None)
        self.assertEqual(result.fan_speed, 0)

    def test_fan_speed_zero_when_fans_array_empty(self):
        """fan_speed is 0 when the FANS array is empty."""
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]}
        raw_fans = {"FANS": []}
        result = MinerSummary.from_luxos(raw_summary, raw_fans=raw_fans)
        self.assertEqual(result.fan_speed, 0)


class TestReadOnlyCommands(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_summary_calls_all_aux_commands_defensively(self):
        # Issue #34: summary() now fetches 8 cmds: summary, version, stats,
        # tunerstatus, fans, config, power, voltageget.
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21", "LUXminer": "x"}]},
            {"STATUS": [{"STATUS": "S"}], "STATS": [{"foo": "bar"}, {"Power": 3500.0}]},
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": []},  # no Power -> falls back to STATS
            {"STATUS": [{"STATUS": "S"}], "FANS": []},  # no fan data
            {"STATUS": [{"STATUS": "S"}], "CONFIG": []},  # no hostname
            {"STATUS": [{"STATUS": "S"}], "POWER": []},  # no Watts -> falls back to STATS
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertIsInstance(result, MinerSummary)
        self.assertEqual(result.hashrate_ths, 200.0)
        self.assertEqual(result.power_w, 3500.0)  # STATS fallback
        self.assertEqual(result.model, "Antminer S21")
        self.assertEqual(result.output_voltage_mv, 14500.0)
        self.api._transport.send_cmd.assert_any_call("summary")
        self.api._transport.send_cmd.assert_any_call("version")
        self.api._transport.send_cmd.assert_any_call("stats")
        self.api._transport.send_cmd.assert_any_call("voltageget", "0")

    def test_summary_degrades_when_version_cmd_fails(self):
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            MinerCommandError("version failed"),
            {"STATUS": [{"STATUS": "S"}], "STATS": [{"foo": "bar"}, {"Power": 3500.0}]},
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": []},
            {"STATUS": [{"STATUS": "S"}], "FANS": []},
            {"STATUS": [{"STATUS": "S"}], "CONFIG": []},
            {"STATUS": [{"STATUS": "S"}], "POWER": []},
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertIsNone(result.model)
        self.assertEqual(result.hashrate_ths, 200.0)
        self.assertEqual(result.power_w, 3500.0)
        self.assertEqual(result.output_voltage_mv, 14500.0)

    def test_summary_degrades_when_stats_cmd_fails(self):
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21", "LUXminer": "x"}]},
            MinerCommandError("stats failed"),
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": []},
            {"STATUS": [{"STATUS": "S"}], "FANS": []},
            {"STATUS": [{"STATUS": "S"}], "CONFIG": []},
            {"STATUS": [{"STATUS": "S"}], "POWER": []},
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertEqual(result.hashrate_ths, 200.0)
        self.assertEqual(result.model, "Antminer S21")
        self.assertEqual(result.output_voltage_mv, 14500.0)
        self.assertEqual(result.power_w, 0.0)

    def test_clocks_calls_frequencyget_per_board(self):
        self.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=3, chips_per_board=108, psu_min_mv=11877, psu_max_mv=15182
            )
        )
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "FREQS": [{"Freqs": ["490.0", "500.0", "480.0"]}]},
            {"STATUS": [{"STATUS": "S"}], "FREQS": [{"Freqs": ["490.0", "500.0", "480.0"]}]},
            {"STATUS": [{"STATUS": "S"}], "FREQS": [{"Freqs": ["490.0", "500.0", "480.0"]}]},
        ]
        result = self.api.clocks()
        self.assertEqual(len(result), 3)
        for i, board in enumerate(result):
            self.assertEqual(board.chip_freqs_mhz, [490.0, 500.0, 480.0])
            self.api._transport.send_cmd.assert_any_call("frequencyget", str(i))

    def test_temps_populates_inlet_outlet(self):
        """LuxOS API 3.7 ``temps`` carries per-board ``ID`` + four position
        readings. Per the response METADATA, ``Right`` columns are ``Intake``
        (cold inlet) and ``Left`` columns are ``Exhaust`` (hot outlet).
        Confirmed on a supervised test unit: TopLeft/BottomLeft ≈ 49°C,
        TopRight/BottomRight ≈ 37°C — exhaust hotter than intake, as
        expected. ``temp_inlet_c`` / ``temp_outlet_c`` take the max across
        top/bottom positions so a single warm reading isn't masked.
        """
        self.api._transport.send_cmd.return_value = {
            "STATUS": [{"STATUS": "S"}],
            "TEMPS": [
                {
                    "ID": 0,
                    "TopLeft": 49.0,
                    "TopRight": 37.0,
                    "BottomLeft": 48.0,
                    "BottomRight": 36.0,
                }
            ],
        }
        result = self.api.temps()
        self.assertEqual(len(result), 1)
        board = result[0]
        self.assertEqual(board.index, 0)
        # Intake (Right) — max of top/bottom.
        self.assertEqual(board.temp_inlet_c, 37.0)
        # Exhaust (Left) — max of top/bottom.
        self.assertEqual(board.temp_outlet_c, 49.0)

    def test_voltages_returns_raw_dict(self):
        mock_return = {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]}
        self.api._transport.send_cmd.return_value = mock_return
        result = self.api.voltages()
        self.assertEqual(result, mock_return)

    def test_capabilities_returns_epic_shape(self):
        self.api._transport.send_cmd.side_effect = [
            {"LIMITS": [{"VoltageMin": 11.877, "VoltageMax": 15.182}]},
            {"DEVDETAILS": [{}, {}, {}]},
            {"FREQS": [{"Count": 108}]},
        ]
        result = self.api.capabilities()
        self.assertAlmostEqual(result["Psu Info"]["Min Vout"], 11877.0)
        self.assertEqual(result["Max HBs"], 3)
        self.assertEqual(result["Performance Estimator"]["Chip Count"], 108)

    # ── Issue #34: aux command orchestration in summary() ─────────────────

    def test_summary_calls_all_six_aux_commands_defensively(self):
        """summary() fetches summary/version/stats/tunerstatus/fans/config/power/voltageget."""
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21"}]},
            {"STATUS": [{"STATUS": "S"}], "STATS": [{}, {}]},
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": [{"Power": 3500.5}]},
            {"STATUS": [{"STATUS": "S"}], "FANS": [{"RPM": 4500}]},
            {"STATUS": [{"STATUS": "S"}], "CONFIG": [{"Hostname": "luxminer-12"}]},
            {"STATUS": [{"STATUS": "S"}], "POWER": []},  # no Watts -> tunerstatus wins
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertEqual(result.hashrate_ths, 200.0)
        self.assertEqual(result.power_w, 3500.5)
        self.assertEqual(result.model, "Antminer S21")
        self.assertEqual(result.fan_speed, 4500)
        self.assertEqual(result.hostname, "luxminer-12")
        self.assertEqual(result.output_voltage_mv, 14500.0)
        self.api._transport.send_cmd.assert_any_call("tunerstatus")
        self.api._transport.send_cmd.assert_any_call("fans")
        self.api._transport.send_cmd.assert_any_call("config")

    def test_summary_degrades_when_tunerstatus_cmd_fails(self):
        """tunerstatus raising MinerCommandError falls back to STATS Power."""
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21"}]},
            {"STATUS": [{"STATUS": "S"}], "STATS": [{}, {"Power": 4200.0}]},
            MinerCommandError("tunerstatus failed"),
            {"STATUS": [{"STATUS": "S"}], "FANS": [{"RPM": 4500}]},
            {"STATUS": [{"STATUS": "S"}], "CONFIG": [{"Hostname": "h"}]},
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertEqual(result.power_w, 4200.0)
        self.assertEqual(result.fan_speed, 4500)
        self.assertEqual(result.hostname, "h")

    def test_summary_degrades_when_fans_cmd_fails(self):
        """fans raising MinerCommandError leaves fan_speed=0; other fields populated."""
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21"}]},
            {"STATUS": [{"STATUS": "S"}], "STATS": [{}, {}]},
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": [{"Power": 3500.5}]},
            MinerCommandError("fans failed"),
            {"STATUS": [{"STATUS": "S"}], "CONFIG": [{"Hostname": "luxminer-12"}]},
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertEqual(result.fan_speed, 0)
        self.assertEqual(result.power_w, 3500.5)
        self.assertEqual(result.hostname, "luxminer-12")
        self.assertEqual(result.hashrate_ths, 200.0)

    def test_summary_degrades_when_config_cmd_fails(self):
        """config raising MinerCommandError leaves hostname=None; other fields populated."""
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 200_000.0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21"}]},
            {"STATUS": [{"STATUS": "S"}], "STATS": [{}, {}]},
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": [{"Power": 3500.5}]},
            {"STATUS": [{"STATUS": "S"}], "FANS": [{"RPM": 4500}]},
            MinerCommandError("config failed"),
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertIsNone(result.hostname)
        self.assertEqual(result.power_w, 3500.5)
        self.assertEqual(result.fan_speed, 4500)
        self.assertEqual(result.hashrate_ths, 200.0)


class TestVoltageConversion(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_set_voltage_converts_mv_to_volts(self):
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}
        )
        self.api.set_voltage(14000)
        self.api._transport.send_cmd.assert_called_once_with(
            "voltageset", "0", "14.0", "0.05", requires_session=True
        )

    def test_set_voltage_snaps_to_step_grid(self):
        # 14025 mV / 1000 = 14.025 V; snap(14.025, 0.05):
        # round(14.025/0.05)=round(280.5)=280 (banker's rounding), 280*0.05=14.0
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}
        )
        self.api.set_voltage(14025)
        self.api._transport.send_cmd.assert_called_once_with(
            "voltageset", "0", "14.0", "0.05", requires_session=True
        )

    def test_set_voltage_out_of_range_raises(self):
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}
        )
        with self.assertRaises(MinerCommandError):
            self.api.set_voltage(20000)

    def test_summary_output_voltage_mv_converts_v_to_mv(self):
        self.api._transport.send_cmd.side_effect = [
            {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 0}]},
            {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "Antminer S21", "LUXminer": "x"}]},
            {"STATUS": [{"STATUS": "S"}], "STATS": [{"Power": 0}]},
            {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": []},
            {"STATUS": [{"STATUS": "S"}], "FANS": []},
            {"STATUS": [{"STATUS": "S"}], "CONFIG": []},
            {"STATUS": [{"STATUS": "S"}], "POWER": []},
            {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
        ]
        result = self.api.summary()
        self.assertEqual(result.output_voltage_mv, 14500.0)

    def test_validate_voltage_error_message_contains_mv(self):
        with self.assertRaises(MinerCommandError) as cm:
            self.api._validate_voltage_v(20.0, {"VoltageMin": 11.877, "VoltageMax": 15.182})
        self.assertIn("20000", str(cm.exception))


class TestClockSetters(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_set_clock_all_sends_three_board_frequencyset(self):
        self.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=3, chips_per_board=108, psu_min_mv=11877, psu_max_mv=15182
            )
        )
        self.api.set_clock_all(500)
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "0", "500", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "1", "500", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "2", "500", requires_session=True
        )
        self.assertEqual(self.api._transport.send_cmd.call_count, 3)

    def test_set_clock_chip_full_board_uniform_coalesces_to_one_call(self):
        # 3 chips all at 480, target 500 for all — full-board, uniform → coalesce
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Freqs": ["480", "480", "480"]}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        chip_freqs = [(0, 500), (1, 500), (2, 500)]
        self.api.set_clock_chip(0, chip_freqs)
        # Only 2 total calls: 1 frequencyget + 1 coalesced frequencyset
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "0", "500", requires_session=True
        )

    def test_set_clock_chip_partial_board_sends_per_chip(self):
        # 4 chips, only updating chips 1 and 3 → NOT full-board → per-chip
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Freqs": ["480", "480", "480", "480"]}]},
            {"STATUS": [{"STATUS": "S"}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        chip_freqs = [(1, 500), (3, 500)]
        self.api.set_clock_chip(0, chip_freqs)
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "0", "500", "1", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "0", "500", "3", requires_session=True
        )

    def test_set_clock_chip_diff_and_skip_matching_freqs(self):
        # current=[500,480,500], targets=[(0,500),(1,490),(2,500)]
        # chip0 matches, chip2 matches → only chip1 (490!=480) is sent
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Freqs": ["500", "480", "500"]}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        chip_freqs = [(0, 500), (1, 490), (2, 500)]
        self.api.set_clock_chip(0, chip_freqs)
        # 2 total calls: 1 frequencyget + 1 frequencyset for chip 1 only
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)
        self.api._transport.send_cmd.assert_any_call(
            "frequencyset", "0", "490", "1", requires_session=True
        )


class TestMiningStateAndControl(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_set_perpetualtune_false_sends_both_commands(self):
        self.api.set_perpetualtune(False)
        self.api._transport.send_cmd.assert_any_call(
            "atmset", "enabled=false", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call(
            "autotunerset", "enabled=false", requires_session=True
        )
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)

    def test_set_perpetualtune_true_sends_both_commands(self):
        self.api.set_perpetualtune(True)
        self.api._transport.send_cmd.assert_any_call(
            "atmset", "enabled=true", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call(
            "autotunerset", "enabled=true", requires_session=True
        )
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)

    def test_start_and_stop_mining_sends_curtail(self):
        self.api.start_mining()
        self.api._transport.send_cmd.assert_called_once_with(
            "curtail", "wakeup", requires_session=True
        )
        self.api._transport.send_cmd.reset_mock()
        self.api.stop_mining()
        self.api._transport.send_cmd.assert_called_once_with(
            "curtail", "sleep", requires_session=True
        )

    def test_reboot_re_raises_command_error_as_offline(self):
        self.api._transport.send_cmd.side_effect = MinerCommandError("session refresh exhausted")
        with self.assertRaises(MinerOfflineError) as context:
            self.api.reboot()
        self.assertIn("rebootdevice", str(context.exception))

    def test_set_power_limit_sends_powertargetset(self):
        # LUXminer 2026.4.3 rejects bare positional watts ("3500") with
        # "Invalid key/value format"; the wire format must be "power=<watts>".
        # Confirmed on a supervised test unit.
        self.api.set_power_limit(3500)
        self.api._transport.send_cmd.assert_called_once_with(
            "powertargetset", "power=3500", requires_session=True
        )


class TestSetCoin(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_new_pool_calls_addpool_then_switchpool(self):
        stratum_configs = [{"pool": "stratum+tcp://X:3333", "login": "user1", "password": "x"}]
        self.api._transport.send_cmd.side_effect = [
            {"POOLS": []},
            {"STATUS": [{"STATUS": "S"}]},
            {"POOLS": [{"URL": "stratum+tcp://X:3333", "POOL": 0}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        result = self.api.set_coin("BTC", stratum_configs)
        self.assertTrue(result)
        self.api._transport.send_cmd.assert_any_call(
            "addpool", "stratum+tcp://X:3333", "user1", "x", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call("switchpool", "0", requires_session=True)

    def test_duplicate_pool_skips_addpool(self):
        stratum_configs = [{"pool": "stratum+tcp://X:3333", "login": "user1", "password": "x"}]
        self.api._transport.send_cmd.side_effect = [
            {"POOLS": [{"URL": "stratum+tcp://X:3333", "POOL": 0}]},
            {"POOLS": [{"URL": "stratum+tcp://X:3333", "POOL": 0}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        result = self.api.set_coin("BTC", stratum_configs)
        self.assertTrue(result)
        call_cmds = [c.args[0] for c in self.api._transport.send_cmd.call_args_list]
        self.assertNotIn("addpool", call_cmds)
        self.api._transport.send_cmd.assert_any_call("switchpool", "0", requires_session=True)


class TestCapabilityAndTopology(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_capability_flags_all_true(self):
        api = LuxosMinerAPI("1.2.3.4")
        self.assertEqual(api.firmware_type(), "luxos")
        self.assertTrue(api.supports_per_chip_tuning())
        self.assertTrue(api.has_external_power_limit())
        self.assertTrue(api.has_capabilities_endpoint())
        self.assertTrue(api.has_internal_perpetual_tune())

    def test_firmware_type_returns_luxos(self):
        self.assertEqual(self.api.firmware_type(), "luxos")

    def test_hardware_topology_reads_limits_and_converts_to_mv(self):
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}
        )
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
        ]
        result = self.api.hardware_topology()
        self.assertIsInstance(result, HardwareTopology)
        self.assertEqual(result.num_boards, 3)
        self.assertEqual(result.chips_per_board, 108)
        self.assertEqual(result.psu_min_mv, 11877)
        self.assertEqual(result.psu_max_mv, 15182)
        self.assertTrue(result.psu_bounds_verified)
        self.assertEqual(result.psu_bounds_source, "firmware:limits")

    def test_hardware_topology_clamps_above_spec_psu_max_for_s21(self):
        """LUXminer 2026.4.3 reports VoltageMax=15.48 V on S21 hardware,
        which exceeds the S21 PSU Type 193 spec of 15.182 V. The model-aware
        clamp in hardware_topology() pulls psu_max_mv back to 15182 so Phase V
        exploration cannot push voltage above hardware spec.
        """
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 11.86, "VoltageMax": 15.48, "VoltageStepMin": 0.05}
        )
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
            {"VERSION": [{"Type": "Antminer S21", "LUXminer": "2026.4.3"}]},
        ]
        result = self.api.hardware_topology()
        self.assertEqual(result.psu_max_mv, 15182)
        self.assertEqual(result.psu_min_mv, 11877)

    def test_hardware_topology_no_clamp_for_unknown_model(self):
        """Non-S21 hardware falls through to the unclamped LuxOS-reported range.
        A future Antminer model would report a different ``Type`` string and
        the clamp branch would not match — ``S21`` substring match is the gate.
        """
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 11.86, "VoltageMax": 15.48, "VoltageStepMin": 0.05}
        )
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Count": 200}]},
            {"DEVDETAILS": [{}, {}, {}, {}]},
            {"VERSION": [{"Type": "Antminer X99", "LUXminer": "2027.1.0"}]},
        ]
        result = self.api.hardware_topology()
        # Unclamped: full LuxOS range preserved.
        self.assertEqual(result.psu_min_mv, 11860)
        self.assertEqual(result.psu_max_mv, 15480)

    def test_hardware_topology_no_clamp_when_already_within_s21_spec(self):
        """If LuxOS already reports a within-spec range, the clamp is a no-op
        and emits no warning log. Validates that the clamp branch only
        narrows the range, never widens it.
        """
        self.api._get_limits_cached = MagicMock(
            return_value={"VoltageMin": 12.0, "VoltageMax": 15.0, "VoltageStepMin": 0.05}
        )
        self.api._transport.send_cmd.side_effect = [
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
            {"VERSION": [{"Type": "Antminer S21", "LUXminer": "2026.4.3"}]},
        ]
        result = self.api.hardware_topology()
        # LuxOS-reported range is within spec — no clamping.
        self.assertEqual(result.psu_min_mv, 12000)
        self.assertEqual(result.psu_max_mv, 15000)


# NEW TESTS FOR ISSUE #34 - power_w and output_voltage_mv


class TestFromLuxosPowerPriorityNew(TestCase):
    def test_power_from_raw_power_cmd_wins_over_tunerstatus(self):
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 100_000.0}]}
        result = MinerSummary.from_luxos(
            raw_summary,
            raw_power={"POWER": [{"PSU": True, "Watts": 1234}]},
            raw_tunerstatus={"TUNERSTATUS": [{"Power": 5678}]},
        )
        self.assertEqual(result.power_w, 1234.0)

    def test_power_zero_when_raw_power_lacks_watts_field(self):
        raw_summary = {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 100_000.0}]}
        result = MinerSummary.from_luxos(
            raw_summary,
            raw_power={"POWER": [{"PSU": True}]},
            raw_tunerstatus=None,
            raw_stats=None,
        )
        self.assertEqual(result.power_w, 0.0)


class TestSummaryLite(TestCase):
    """summary_lite() must fire ONLY the 'summary' TCP cmd — every other
    aux dict is left at default. This is the load-shedding path used by
    Phase 0's post-perpetualtune recheck and recovery polling so a busy
    LuxOS firmware (port 4028 stormy on 2026.4.3) is not poked with 10
    sequential cmds per liveness probe.
    """

    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_summary_lite_fires_only_summary_cmd(self):
        self.api._transport.send_cmd.return_value = {
            "STATUS": [{"STATUS": "S"}],
            "SUMMARY": [{"GHS av": 200_000.0}],
        }
        self.api.summary_lite()
        self.assertEqual(self.api._transport.send_cmd.call_count, 1)
        self.api._transport.send_cmd.assert_called_once_with("summary")

    def test_summary_lite_returns_minersummary_with_state_and_hashrate(self):
        self.api._transport.send_cmd.return_value = {
            "STATUS": [{"STATUS": "S"}],
            "SUMMARY": [{"GHS av": 200_000.0}],
        }
        result = self.api.summary_lite()
        self.assertIsInstance(result, MinerSummary)
        self.assertEqual(result.operating_state, "Mining")
        self.assertEqual(result.hashrate_ths, 200.0)

    def test_summary_lite_aux_fields_default_when_only_summary_cmd_used(self):
        self.api._transport.send_cmd.return_value = {
            "STATUS": [{"STATUS": "S"}],
            "SUMMARY": [{"GHS av": 0.0}],
        }
        result = self.api.summary_lite()
        self.assertEqual(result.power_w, 0.0)
        self.assertIsNone(result.model)
        self.assertEqual(result.fan_speed, 0)
        self.assertIsNone(result.hostname)
        self.assertIsNone(result.output_voltage_mv)


class TestSetCoinIdempotent(TestCase):
    """set_coin must be a no-op when all stratums are already present and
    the first stratum is the Active pool. Otherwise it falls through to
    addpool and switchpool. The single-pools-read fast path saves 4 TCP
    cmds per Phase 0 entry.
    """

    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_fast_path_when_all_present_and_first_active(self):
        stratum_configs = [
            {"pool": "stratum+tcp://A:3333", "login": "u", "password": "x"},
            {"pool": "stratum+tcp://B:3333", "login": "u", "password": "x"},
        ]
        self.api._transport.send_cmd.return_value = {
            "POOLS": [
                {"URL": "stratum+tcp://A:3333", "POOL": 0, "Status": "Active"},
                {"URL": "stratum+tcp://B:3333", "POOL": 1, "Status": "Alive"},
            ]
        }
        result = self.api.set_coin("BTC", stratum_configs)
        self.assertTrue(result)
        self.assertEqual(self.api._transport.send_cmd.call_count, 1)
        self.api._transport.send_cmd.assert_called_once_with("pools")

    def test_no_fast_path_when_first_pool_not_active(self):
        stratum_configs = [
            {"pool": "stratum+tcp://A:3333", "login": "u", "password": "x"},
        ]
        self.api._transport.send_cmd.side_effect = [
            {"POOLS": [{"URL": "stratum+tcp://A:3333", "POOL": 0, "Status": "Alive"}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        self.api.set_coin("BTC", stratum_configs)
        # 1 pools read + 1 switchpool — no addpool, no second pools read.
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)
        call_cmds = [c.args[0] for c in self.api._transport.send_cmd.call_args_list]
        self.assertEqual(call_cmds, ["pools", "switchpool"])

    def test_missing_pool_triggers_addpool(self):
        stratum_configs = [
            {"pool": "stratum+tcp://A:3333", "login": "u", "password": "x"},
        ]
        self.api._transport.send_cmd.side_effect = [
            {"POOLS": []},  # no existing pools
            {"STATUS": [{"STATUS": "S"}]},  # addpool ack
            {"POOLS": [{"URL": "stratum+tcp://A:3333", "POOL": 0, "Status": "Active"}]},
            {"STATUS": [{"STATUS": "S"}]},  # switchpool ack
        ]
        self.api.set_coin("BTC", stratum_configs)
        call_cmds = [c.args[0] for c in self.api._transport.send_cmd.call_args_list]
        self.assertEqual(call_cmds, ["pools", "addpool", "pools", "switchpool"])

    def test_empty_stratum_configs_returns_immediately(self):
        result = self.api.set_coin("BTC", [])
        self.assertTrue(result)
        self.api._transport.send_cmd.assert_not_called()

    def test_switchpool_failure_retries_with_fresh_pools(self):
        stratum_configs = [
            {"pool": "stratum+tcp://A:3333", "login": "u", "password": "x"},
        ]
        self.api._transport.send_cmd.side_effect = [
            {"POOLS": [{"URL": "stratum+tcp://A:3333", "POOL": 0, "Status": "Alive"}]},
            MinerCommandError("switchpool: stale POOL id"),
            {"POOLS": [{"URL": "stratum+tcp://A:3333", "POOL": 1, "Status": "Alive"}]},
            {"STATUS": [{"STATUS": "S"}]},
        ]
        self.api.set_coin("BTC", stratum_configs)
        call_cmds = [c.args[0] for c in self.api._transport.send_cmd.call_args_list]
        # First switchpool fails, we re-read pools, then a second switchpool succeeds.
        self.assertEqual(call_cmds, ["pools", "switchpool", "pools", "switchpool"])


class TestSetPerpetualtuneCache(TestCase):
    """set_perpetualtune caches the last successfully-applied state and
    skips both atmset+autotunerset (6 TCP cycles) when called with the
    same value. Errors invalidate the cache so a partial-state failure
    forces re-issuance on the next call.
    """

    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def test_first_call_fires_both_cmds(self):
        self.api.set_perpetualtune(False)
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)
        self.assertEqual(self.api._perpetualtune_cached, False)

    def test_repeat_call_with_same_value_fires_zero_cmds(self):
        self.api.set_perpetualtune(False)
        self.api._transport.send_cmd.reset_mock()
        result = self.api.set_perpetualtune(False)
        self.assertTrue(result)
        self.api._transport.send_cmd.assert_not_called()

    def test_call_with_different_value_fires_both_cmds(self):
        self.api.set_perpetualtune(False)
        self.api._transport.send_cmd.reset_mock()
        self.api.set_perpetualtune(True)
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)
        self.api._transport.send_cmd.assert_any_call(
            "atmset", "enabled=true", requires_session=True
        )
        self.api._transport.send_cmd.assert_any_call(
            "autotunerset", "enabled=true", requires_session=True
        )
        self.assertEqual(self.api._perpetualtune_cached, True)

    def test_command_error_invalidates_cache_and_propagates(self):
        # Pre-cache a successful (False) state.
        self.api.set_perpetualtune(False)
        self.api._transport.send_cmd.reset_mock()
        # The next True call hits an error mid-pair: cache must reset to None
        # so the *following* call (regardless of value) re-issues both cmds.
        self.api._transport.send_cmd.side_effect = MinerCommandError("atmset failed")
        with self.assertRaises(MinerCommandError):
            self.api.set_perpetualtune(True)
        self.assertIsNone(self.api._perpetualtune_cached)
        # Now setting back to False must re-fire — cached=None defeats the
        # short-circuit even though prior known-good state was False.
        self.api._transport.send_cmd.side_effect = None
        self.api._transport.send_cmd.reset_mock()
        self.api.set_perpetualtune(False)
        self.assertEqual(self.api._transport.send_cmd.call_count, 2)


class TestSummaryVoltagePerBoardFallback(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api._transport.send_cmd = MagicMock()

    def _make_dispatch(self, voltageget_responses):
        def dispatch(cmd, *args, **kwargs):
            if cmd == "summary":
                return {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 100_000.0}]}
            elif cmd == "version":
                return {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "TestMiner"}]}
            elif cmd == "stats":
                return {"STATUS": [{"STATUS": "S"}], "STATS": []}
            elif cmd == "tunerstatus":
                return {"STATUS": [{"STATUS": "S"}], "TUNERSTATUS": []}
            elif cmd == "fans":
                return {"STATUS": [{"STATUS": "S"}], "FANS": []}
            elif cmd == "config":
                return {"STATUS": [{"STATUS": "S"}], "CONFIG": []}
            elif cmd == "voltageget":
                board = args[0] if args else "0"
                return voltageget_responses.get(board, {"VOLTAGE": [{"Voltage": 0.0}]})
            elif cmd == "power":
                return {"STATUS": [{"STATUS": "S"}], "POWER": [{"PSU": True, "Watts": 900}]}
            return {"STATUS": [{"STATUS": "S"}]}

        return dispatch

    def test_summary_voltage_falls_back_to_board_1_when_board_0_zero(self):
        voltage_responses = {
            "0": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
            "1": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 14.5}]},
            "2": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
        }
        self.api._transport.send_cmd.side_effect = self._make_dispatch(voltage_responses)
        result = self.api.summary()
        self.assertEqual(result.output_voltage_mv, 14500.0)

    def test_summary_voltage_none_when_all_boards_zero(self):
        voltage_responses = {
            "0": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
            "1": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
            "2": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
        }
        self.api._transport.send_cmd.side_effect = self._make_dispatch(voltage_responses)
        result = self.api.summary()
        self.assertIsNone(result.output_voltage_mv)

    def test_summary_calls_power_cmd_in_summary(self):
        voltage_responses = {
            "0": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
            "1": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
            "2": {"STATUS": [{"STATUS": "S"}], "VOLTAGE": [{"Voltage": 0.0}]},
        }
        self.api._transport.send_cmd.side_effect = self._make_dispatch(voltage_responses)
        self.api.summary()
        self.api._transport.send_cmd.assert_any_call("power")


class TestFetchChipHealth(TestCase):
    """Regression coverage for the LUXminer 2026.4.3 healthchipget shape.

    A supervised protocol probe confirmed ``healthchipget(board_id)`` (single positional
    param) returns ALL chips on that board in one CHIPS array. Pre-fix,
    ``_fetch_chip_health`` (1) read non-existent ``chip_info["Score"]`` and
    ``chip_info["Hash"]`` — crashed the engine with ``KeyError`` on Phase 2
    baseline scoring; (2) iterated chips one-at-a-time (board_id, chip_id)
    instead of using the bulk per-board read — ~324 TCP cmds × 1.0 s rate gate
    = ~5.4 minutes per call, vs ~3 seconds for the bulk approach.
    """

    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()
        self.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=1, chips_per_board=2, psu_min_mv=11877, psu_max_mv=15182
            )
        )

    def _board_response(self, chips):
        """Build a healthchipget response with the given list of chip dicts."""
        return {"STATUS": [{"STATUS": "S"}], "CHIPS": chips}

    def _chip(self, board=0, chip=0, ghs_5m=337.0, healthy="Yes", chip_temp=57.4):
        return {
            "Board": board,
            "Chip": chip,
            "GHS 5m": ghs_5m,
            "Healthy": healthy,
            "ChipTemp": chip_temp,
        }

    def test_uses_bulk_per_board_call(self):
        # 1 healthchipget per board (parameter is the board_id only) — NOT
        # a separate call per (board_id, chip_id) pair. Verifies the 100x
        # perf fix that's now visible after the KeyError no longer hides it.
        self.api._transport.send_cmd.side_effect = [
            self._board_response([self._chip(0, 0), self._chip(0, 1)])
        ]
        self.api._fetch_chip_health()
        self.assertEqual(self.api._transport.send_cmd.call_count, 1)
        self.api._transport.send_cmd.assert_called_with("healthchipget", "0")

    def test_health_pct_uses_ghs_5m_in_mhs(self):
        self.api._transport.send_cmd.side_effect = [
            self._board_response([self._chip(0, 0, ghs_5m=337.5), self._chip(0, 1, ghs_5m=340.0)])
        ]
        boards = self.api._fetch_chip_health()
        # GHS 5m × 1000 = MH/s (matches ePIC's hashrate_per_chip_mhs units).
        self.assertEqual(boards[0].health_pct, [337500.0, 340000.0])
        self.assertEqual(boards[0].hashrate_per_chip_mhs, [337500.0, 340000.0])

    def test_healthy_no_zeros_health_pct(self):
        self.api._transport.send_cmd.side_effect = [
            self._board_response(
                [
                    self._chip(0, 0, ghs_5m=0.0, healthy="No"),
                    self._chip(0, 1, ghs_5m=337.0, healthy="Yes"),
                ]
            )
        ]
        boards = self.api._fetch_chip_health()
        # Healthy="No" → health_pct=0 so park_dead_chips_from_baseline excludes it.
        self.assertEqual(boards[0].health_pct[0], 0.0)
        self.assertEqual(boards[0].hashrate_per_chip_mhs[0], 0.0)
        self.assertEqual(boards[0].health_pct[1], 337000.0)

    def test_chip_temps_populated_from_ChipTemp(self):
        self.api._transport.send_cmd.side_effect = [
            self._board_response(
                [self._chip(0, 0, chip_temp=57.4), self._chip(0, 1, chip_temp=58.1)]
            )
        ]
        boards = self.api._fetch_chip_health()
        self.assertEqual(boards[0].chip_temps_c, [57.4, 58.1])

    def test_missing_fields_default_to_zero_no_keyerror(self):
        # Defensive .get() reads must keep the rest of the batch intact when a
        # single chip entry is missing fields.
        self.api._transport.send_cmd.side_effect = [
            self._board_response(
                [
                    {"Board": 0, "Chip": 0},  # no GHS 5m / Healthy / ChipTemp
                    self._chip(0, 1, ghs_5m=337.0),
                ]
            )
        ]
        boards = self.api._fetch_chip_health()
        self.assertEqual(boards[0].health_pct, [0.0, 337000.0])
        self.assertEqual(boards[0].chip_temps_c[0], 0.0)
        self.assertEqual(boards[0].chip_temps_c[1], 57.4)
