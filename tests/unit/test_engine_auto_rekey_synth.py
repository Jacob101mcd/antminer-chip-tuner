# tests/unit/test_engine_auto_rekey_synth.py
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from tuner_app.miner.types import MinerSummary
from tuner_app.tuning_engine.engine import TuningEngine


class _FakeConfig:
    def __init__(self, overrides=None):
        self._data = {
            "API_PORT": 4028,
            "PASSWORD": "letmein",
            "firmware_type": "epic",
        }
        if overrides:
            self._data.update(overrides)

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data


class TestEngineAutoRekeySynth(TestCase):
    def setUp(self):
        # Create a real TuningEngine instance with mocked dependencies
        with (
            patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
            patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
        ):
            self.engine = TuningEngine(
                mac="syn-abc-1",
                config=_FakeConfig(),
            )
        self.engine.api = Mock()
        self.engine.last_update = 0

    @patch("tuner_app.manager.bulk._rekey_miner")
    @patch("tuner_app.main.manager")
    def test_happy_path_synth_to_real_rekey_fires(self, mock_manager, mock_rekey_miner):
        # Setup
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining",
            hashrate_ths=0.0,
            power_w=0.0,
            fan_speed=0,
            mac="aa:bb:cc:dd:ee:01",
        )

        # Act
        self.engine._update_live_data()

        # Assert
        mock_rekey_miner.assert_called_once_with(self.engine.mac, "aa:bb:cc:dd:ee:01", manager=ANY)

    def test_engine_already_has_real_mac_summary_returns_same_mac_no_rekey(self):
        # Setup
        self.engine.mac = "11:22:33:44:55:66"
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining",
            hashrate_ths=0.0,
            power_w=0.0,
            fan_speed=0,
            mac="11:22:33:44:55:66",
        )

        with patch("tuner_app.manager.bulk._rekey_miner") as mock_rekey_miner:
            # Act
            self.engine._update_live_data()

            # Assert
            self.assertEqual(mock_rekey_miner.call_count, 0)

    def test_engine_has_real_mac_summary_returns_different_real_mac_no_rekey(self):
        # Setup
        self.engine.mac = "11:22:33:44:55:66"
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining",
            hashrate_ths=0.0,
            power_w=0.0,
            fan_speed=0,
            mac="aa:bb:cc:dd:ee:01",
        )

        with patch("tuner_app.manager.bulk._rekey_miner") as mock_rekey_miner:
            # Act
            self.engine._update_live_data()

            # Assert
            self.assertEqual(mock_rekey_miner.call_count, 0)

    def test_engine_has_synth_summary_returns_none_mac_no_rekey(self):
        # Setup
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining", hashrate_ths=0.0, power_w=0.0, fan_speed=0, mac=None
        )

        with patch("tuner_app.manager.bulk._rekey_miner") as mock_rekey_miner:
            # Act
            self.engine._update_live_data()

            # Assert
            self.assertEqual(mock_rekey_miner.call_count, 0)

    def test_engine_has_synth_summary_returns_empty_string_mac_no_rekey(self):
        # Setup
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining", hashrate_ths=0.0, power_w=0.0, fan_speed=0, mac=""
        )

        with patch("tuner_app.manager.bulk._rekey_miner") as mock_rekey_miner:
            # Act
            self.engine._update_live_data()

            # Assert
            self.assertEqual(mock_rekey_miner.call_count, 0)

    @patch("tuner_app.manager.bulk._rekey_miner")
    @patch("tuner_app.main.manager")
    def test_rekey_miner_raises_valueerror_engine_continues_no_exception(
        self, mock_manager, mock_rekey_miner
    ):
        # Setup
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining",
            hashrate_ths=0.0,
            power_w=0.0,
            fan_speed=0,
            mac="aa:bb:cc:dd:ee:01",
        )
        mock_rekey_miner.side_effect = ValueError("target MAC already exists")

        # Act & Assert
        try:
            self.engine._update_live_data()
        except Exception:
            self.fail("_update_live_data should not raise when _rekey_miner raises ValueError")

        # Verify other api methods were still called
        self.engine.api.hashrate.assert_called_once()
        self.engine.api.clocks.assert_called_once()
        self.engine.api.temps.assert_called_once()
        self.engine.api.temps_chip.assert_called_once()

        # Verify last_summary was set
        self.assertIsNotNone(self.engine.last_summary)

    @patch("tuner_app.manager.bulk._rekey_miner")
    @patch("tuner_app.main.manager")
    def test_rekey_miner_raises_generic_exception_engine_continues_no_propagation(
        self, mock_manager, mock_rekey_miner
    ):
        # Setup
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining",
            hashrate_ths=0.0,
            power_w=0.0,
            fan_speed=0,
            mac="aa:bb:cc:dd:ee:01",
        )
        mock_rekey_miner.side_effect = Exception("unexpected")

        # Act & Assert
        try:
            self.engine._update_live_data()
        except Exception:
            self.fail(
                "_update_live_data should not raise when _rekey_miner raises generic Exception"
            )

        # Verify other api methods were still called
        self.engine.api.hashrate.assert_called_once()
        self.engine.api.clocks.assert_called_once()
        self.engine.api.temps.assert_called_once()
        self.engine.api.temps_chip.assert_called_once()

        # Verify last_summary was set
        self.assertIsNotNone(self.engine.last_summary)

    @patch("tuner_app.manager.bulk._rekey_miner")
    @patch("tuner_app.main.manager")
    @patch("tuner_app.tuning_engine.engine.time.time")
    def test_5_second_throttle_still_works(self, mock_time, mock_manager, mock_rekey_miner):
        # Setup
        self.engine.mac = "syn-abc-1"
        self.engine.api.summary.return_value = MinerSummary(
            operating_state="Mining",
            hashrate_ths=0.0,
            power_w=0.0,
            fan_speed=0,
            mac="aa:bb:cc:dd:ee:01",
        )
        # First call at t=100; second at t=102 (within 5s window). Use
        # return_value rather than side_effect because self.log() also
        # invokes time.time() internally (logging timestamp), and the
        # patch targets the global time module — exhausting a 2-element
        # side_effect list.
        mock_time.return_value = 100

        # Act - First call
        self.engine._update_live_data()

        # Assert first call succeeded
        self.assertEqual(mock_rekey_miner.call_count, 1)

        # Advance time by 2 seconds — still inside the 5s throttle.
        mock_time.return_value = 102

        # Act - Second call (should be throttled)
        self.engine._update_live_data()

        # Assert second call was throttled (summary not called again)
        self.assertEqual(self.engine.api.summary.call_count, 1)
