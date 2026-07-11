"""Tests for TuningEngine constructor firmware_type branching.

Verifies:
- firmware_type="epic" → engine.api is an EpicMinerAPI instance
- firmware_type="bixbit" → engine.api is a BixbitMinerAPI instance
- firmware_type missing → defaults to "epic" (back-compat)
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tuner_app.miner.bixbit import BixbitMinerAPI
from tuner_app.miner.epic import EpicMinerAPI


class _FakeConfig:
    """Minimal EffectiveConfig-like object for engine construction tests."""

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


class TestEngineInitVendor(unittest.TestCase):
    def _make_engine(self, firmware_type="epic"):
        """Construct a TuningEngine with a minimal fake config, suppressing disk I/O."""
        from tuner_app.tuning_engine.engine import TuningEngine

        cfg = _FakeConfig({"firmware_type": firmware_type})
        ip = "192.0.2.99"

        with (
            patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
            patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
        ):
            engine = TuningEngine(ip, cfg)
        return engine

    def test_epic_firmware_creates_epic_api(self):
        """firmware_type='epic' → engine.api is EpicMinerAPI."""
        engine = self._make_engine("epic")
        self.assertIsInstance(engine.api, EpicMinerAPI)

    def test_bixbit_firmware_creates_bixbit_api(self):
        """firmware_type='bixbit' → engine.api is BixbitMinerAPI."""
        engine = self._make_engine("bixbit")
        self.assertIsInstance(engine.api, BixbitMinerAPI)

    def test_unknown_firmware_raises_value_error(self):
        """Any unknown firmware_type raises ValueError with descriptive message."""
        with self.assertRaises(ValueError) as ctx:
            self._make_engine("luxor")
        self.assertIn("luxor", str(ctx.exception))

    def test_default_is_epic_when_key_missing(self):
        """If firmware_type key is absent, engine defaults to EpicMinerAPI."""
        from tuner_app.tuning_engine.engine import TuningEngine

        # Config with no firmware_type key at all
        cfg = _FakeConfig()
        cfg._data.pop("firmware_type", None)
        ip = "192.0.2.98"

        with (
            patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
            patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
        ):
            engine = TuningEngine(ip, cfg)

        self.assertIsInstance(engine.api, EpicMinerAPI)


if __name__ == "__main__":
    unittest.main(verbosity=2)
