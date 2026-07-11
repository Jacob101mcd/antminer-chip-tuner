"""Phase B / B8 — monitor cycle records a metrics sample after _update_live_data.

Direct unit-level test of the helper ``_record_metrics`` since exercising the
full ``do_monitor_cycle_body`` requires extensive engine fixture setup.  The
helper carries the actual production logic (None-store guard, exception
swallowing, mac+sample wiring); ``do_monitor_cycle_body`` only routes through it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

from tuner_app import state
from tuner_app.tuning_engine.monitor import _record_metrics


class TestRecordMetricsHelper(TestCase):
    def setUp(self) -> None:
        self._saved_store = state.metrics_store

    def tearDown(self) -> None:
        state.metrics_store = self._saved_store

    def _engine(self) -> SimpleNamespace:
        # Build the smallest engine fixture build_sample/_record_metrics needs.
        return SimpleNamespace(
            mac="aa:bb:cc:dd:ee:ff",
            firmware_type="epic",
            last_summary=SimpleNamespace(
                hashrate_ths=200.0,
                power_w=4200.0,
                fan_speed=50,
                target_voltage_mv=14630.0,
                output_voltage_mv=14600.0,
            ),
            last_chip_temps=[SimpleNamespace(chip_temps_c=[72.0, 71.0])],
        )

    def test_record_called_with_mac_and_sample(self) -> None:
        store = MagicMock()
        state.metrics_store = store
        _record_metrics(self._engine())
        self.assertEqual(store.record_sample.call_count, 1)
        args, _kwargs = store.record_sample.call_args
        self.assertEqual(args[0], "aa:bb:cc:dd:ee:ff")
        # Second positional is the sample dict — verify the canonical keys.
        sample = args[1]
        self.assertIn("ts", sample)
        self.assertEqual(sample["hashrate_ths"], 200.0)
        self.assertEqual(sample["firmware_type"], "epic")

    def test_no_store_no_call(self) -> None:
        # When state.metrics_store is None (boot order, tests, etc.) the
        # helper short-circuits without raising.
        state.metrics_store = None
        try:
            _record_metrics(self._engine())
        except Exception as exc:  # pragma: no cover
            self.fail(f"_record_metrics raised when store is None: {exc}")

    def test_store_failure_does_not_propagate(self) -> None:
        # The monitor cycle must not abort on a metrics-write failure.
        store = MagicMock()
        store.record_sample.side_effect = RuntimeError("disk full")
        state.metrics_store = store
        try:
            _record_metrics(self._engine())
        except Exception as exc:  # pragma: no cover
            self.fail(f"_record_metrics propagated exception: {exc}")
