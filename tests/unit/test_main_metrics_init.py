"""Phase B / B9 — main() must wire the persistent metrics store at boot.

Source-level regression guard: a future restructure that drops the
``state.metrics_store = MetricsStore(...)`` assignment from ``main()`` would
silently leave engine monitor cycles unable to record samples.  Mirrors the
TestMainCallsLoadMinerstatSnapshot pattern in test_minerstat_persistence.py.
"""

from __future__ import annotations

import inspect
import unittest

import tuner_app.main as main_mod


class TestMainWiresMetricsStore(unittest.TestCase):
    def test_main_constructs_metrics_store(self) -> None:
        src = inspect.getsource(main_mod.main)
        self.assertIn(
            "MetricsStore(",
            src,
            "tuner_app.main.main() must construct a MetricsStore at boot",
        )
        self.assertIn(
            "state.metrics_store",
            src,
            "tuner_app.main.main() must expose the metrics store on tuner_app.state",
        )

    def test_main_starts_retention_thread(self) -> None:
        src = inspect.getsource(main_mod.main)
        self.assertIn(
            "start_retention_thread()",
            src,
            "tuner_app.main.main() must start the metrics retention thread",
        )

    def test_main_stops_metrics_store_on_keyboard_interrupt(self) -> None:
        src = inspect.getsource(main_mod.main)
        self.assertIn(
            "metrics_store.stop()",
            src,
            "tuner_app.main.main() must stop the metrics store on KeyboardInterrupt",
        )


if __name__ == "__main__":
    unittest.main()
