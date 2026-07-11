"""Unit tests for tuner_app.metrics.sampler.build_sample (Phase B / B7)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from tuner_app.metrics.sampler import build_sample


def _summary(**fields) -> SimpleNamespace:
    """Build a fake MinerSummary-shaped object with sensible defaults."""
    base = {
        "hashrate_ths": 200.0,
        "power_w": 4200.0,
        "fan_speed": 50,
        "target_voltage_mv": 14630.0,
        "output_voltage_mv": 14600.0,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _board(chip_temps_c: list[float]) -> SimpleNamespace:
    """Build a fake BoardSummary-shaped object with a chip_temps_c list."""
    return SimpleNamespace(chip_temps_c=chip_temps_c)


def _engine(**state) -> SimpleNamespace:
    """Build a fake engine with the attributes build_sample reads."""
    base = {
        "firmware_type": "epic",
        "last_summary": _summary(),
        "last_chip_temps": [_board([72.0, 71.0, 70.0])],
    }
    base.update(state)
    return SimpleNamespace(**base)


class TestBuildSample(TestCase):
    def test_populates_all_canonical_fields(self) -> None:
        sample = build_sample(_engine())
        # Required vendor-neutral fields all present.
        for key in (
            "ts",
            "hashrate_ths",
            "power_w",
            "efficiency_jth",
            "temp_max_c",
            "temp_avg_c",
            "fan_speed",
            "firmware_type",
            "target_voltage_mv",
            "output_voltage_mv",
        ):
            self.assertIn(key, sample, f"missing key: {key}")

    def test_derived_efficiency_jth(self) -> None:
        sample = build_sample(_engine())
        # 4200 W / 200 TH/s = 21 J/TH
        self.assertAlmostEqual(sample["efficiency_jth"], 21.0)

    def test_efficiency_none_when_hashrate_zero(self) -> None:
        sample = build_sample(_engine(last_summary=_summary(hashrate_ths=0.0)))
        self.assertIsNone(sample["efficiency_jth"])

    def test_temp_max_and_avg_filter_zeros(self) -> None:
        # Zero-readings (firmware-reports-failure-as-0) should NOT pull the
        # average down or be considered for the max.
        boards = [_board([72.0, 0.0, 71.0]), _board([0.0, 70.0, 0.0])]
        sample = build_sample(_engine(last_chip_temps=boards))
        self.assertEqual(sample["temp_max_c"], 72.0)
        # Mean of {72, 71, 70}.
        self.assertAlmostEqual(sample["temp_avg_c"], (72.0 + 71.0 + 70.0) / 3)

    def test_no_chip_temps_yields_none(self) -> None:
        sample = build_sample(_engine(last_chip_temps=[]))
        self.assertIsNone(sample["temp_max_c"])
        self.assertIsNone(sample["temp_avg_c"])

    def test_no_summary_returns_minimal_sample(self) -> None:
        # Pre-warmup engine has last_summary=None; sampler must NOT raise.
        sample = build_sample(_engine(last_summary=None))
        self.assertIn("ts", sample)
        self.assertEqual(sample["firmware_type"], "epic")
        # No vendor fields when summary is missing.
        self.assertNotIn("hashrate_ths", sample)

    def test_bixbit_no_target_voltage(self) -> None:
        # Bixbit's MinerSummary.from_bixbit sets target_voltage_mv to None.
        sample = build_sample(
            _engine(
                firmware_type="bixbit",
                last_summary=_summary(target_voltage_mv=None, output_voltage_mv=14500.0),
            )
        )
        self.assertEqual(sample["firmware_type"], "bixbit")
        self.assertIsNone(sample["target_voltage_mv"])
        self.assertEqual(sample["output_voltage_mv"], 14500.0)

    def test_no_chip_temps_attribute_does_not_raise(self) -> None:
        # Even more defensive: if last_chip_temps is somehow None, treat as empty.
        sample = build_sample(_engine(last_chip_temps=None))
        self.assertIsNone(sample["temp_max_c"])
