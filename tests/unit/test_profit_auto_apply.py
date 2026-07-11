"""Unit tests for ``tuner_app.profit.auto_apply.apply_profit_recompute``.

The helper is the single funnel for auto-apply after any minerstat
snapshot refresh — both the manual ``fetch_now`` HTTP handler and the
``MinerstatScheduler._tick`` daemon path call it. These tests cover the
helper in isolation; integration with the HTTP route is exercised in
``tests/integration/test_route_minerstat.py``.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock

from tuner_app import state
from tuner_app.profit.auto_apply import apply_profit_recompute


class _StubManager:
    """Manager double exposing only ``compute_profit_preview`` + ``apply_profit_action``."""

    def __init__(self, preview, apply_results=None, raise_on_apply=None):
        self._preview = preview
        self._apply_results = apply_results or {}
        self._raise_on_apply = raise_on_apply or set()
        self.apply_calls = []

    def compute_profit_preview(self, ips):
        return self._preview

    def apply_profit_action(self, ip, action, voltage_mv, freq_mhz=None):
        self.apply_calls.append((ip, action, voltage_mv, freq_mhz))
        if ip in self._raise_on_apply:
            raise RuntimeError(f"boom-{ip}")
        return self._apply_results.get(ip, (True, "", {}))


class TestApplyProfitRecompute(unittest.TestCase):
    def setUp(self):
        state.CONFIG.setdefault("fleet_ops", {})
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["10.0.0.1", "10.0.0.2"]

    def test_iterates_proposed_actions_and_dispatches_apply_per_miner(self):
        preview = {
            "miners": [
                {
                    "ip": "10.0.0.1",
                    "proposed": {"action": "select_voltage", "voltage_mv": 14000, "freq_mhz": 500},
                },
                {
                    "ip": "10.0.0.2",
                    "proposed": {"action": "retune_voltage", "voltage_mv": 14500},
                },
            ]
        }
        mgr = _StubManager(preview)
        result = apply_profit_recompute(mgr, ips=["10.0.0.1", "10.0.0.2"])
        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["failures"], [])
        self.assertEqual(
            mgr.apply_calls,
            [
                ("10.0.0.1", "select_voltage", 14000, 500),
                ("10.0.0.2", "retune_voltage", 14500, None),
            ],
        )

    def test_skips_miners_with_action_none_and_missing_proposed(self):
        preview = {
            "miners": [
                {"ip": "10.0.0.1", "proposed": {"action": "none", "voltage_mv": 14000}},
                {"ip": "10.0.0.2"},
                {"ip": "10.0.0.3", "proposed": {"action": "select_voltage", "voltage_mv": 14250}},
            ]
        }
        mgr = _StubManager(preview)
        result = apply_profit_recompute(mgr, ips=["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(mgr.apply_calls, [("10.0.0.3", "select_voltage", 14250, None)])

    def test_records_apply_failures_without_short_circuiting(self):
        preview = {
            "miners": [
                {"ip": "10.0.0.1", "proposed": {"action": "select_voltage", "voltage_mv": 14000}},
                {"ip": "10.0.0.2", "proposed": {"action": "select_voltage", "voltage_mv": 14250}},
            ]
        }
        mgr = _StubManager(
            preview,
            apply_results={"10.0.0.1": (False, "engine offline", {})},
            raise_on_apply={"10.0.0.2"},
        )
        result = apply_profit_recompute(mgr, ips=["10.0.0.1", "10.0.0.2"])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(result["failures"]), 2)
        self.assertIn("10.0.0.1: engine offline", result["failures"])
        self.assertIn("10.0.0.2: boom-10.0.0.2", result["failures"])

    def test_returns_failure_summary_when_compute_preview_raises(self):
        mgr = mock.Mock()
        mgr.compute_profit_preview.side_effect = RuntimeError("boom")
        result = apply_profit_recompute(mgr, ips=["10.0.0.1"])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["failures"], ["preview: boom"])

    def test_defaults_ips_to_fleet_miner_ips_when_none(self):
        preview = {"miners": []}
        mgr = mock.Mock()
        mgr.compute_profit_preview.return_value = preview
        apply_profit_recompute(mgr)
        mgr.compute_profit_preview.assert_called_once_with(["10.0.0.1", "10.0.0.2"])

    def test_no_miners_returns_zero_summary_without_calling_manager(self):
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        mgr = mock.Mock()
        result = apply_profit_recompute(mgr)
        self.assertEqual(result, {"applied": 0, "skipped": 0, "failures": []})
        mgr.compute_profit_preview.assert_not_called()


class TestSchedulerUsesHelper(unittest.TestCase):
    """The scheduler's ``_tick`` must funnel through ``apply_profit_recompute``."""

    def setUp(self):
        state.CONFIG.setdefault("fleet_ops", {})
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["10.0.0.1"]
        state.CONFIG["fleet_ops"]["MINERSTAT_POLL_DAY"] = 0  # disable; we drive _tick manually
        state.CONFIG["fleet_ops"]["MINERSTAT_COIN"] = "BTC"
        state.CONFIG["fleet_ops"]["MINERSTAT_API_KEY"] = ""

    def test_tick_uses_shared_helper_after_fetch(self):
        from tuner_app.profit import minerstat as minerstat_mod

        # Configure today's day-of-month so the day-gate accepts.
        today_day = datetime.now(UTC).day
        state.CONFIG["fleet_ops"]["MINERSTAT_POLL_DAY"] = today_day

        mgr = mock.Mock()
        scheduler = minerstat_mod.MinerstatScheduler(mgr)
        with (
            mock.patch.object(minerstat_mod, "get_minerstat_snapshot_copy", return_value={}),
            mock.patch.object(
                minerstat_mod, "fetch_minerstat_coins", return_value={"BTC": {"price": 50000}}
            ),
            mock.patch.object(minerstat_mod, "save_minerstat_snapshot"),
            mock.patch(
                "tuner_app.profit.auto_apply.apply_profit_recompute",
                return_value={"applied": 1, "skipped": 0, "failures": []},
            ) as m_apply,
        ):
            scheduler._tick()

        m_apply.assert_called_once()
        call_kwargs = m_apply.call_args.kwargs
        self.assertEqual(call_kwargs.get("ips"), ["10.0.0.1"])

    def test_tick_skips_helper_when_fetch_fails(self):
        from tuner_app.profit import minerstat as minerstat_mod

        today_day = datetime.now(UTC).day
        state.CONFIG["fleet_ops"]["MINERSTAT_POLL_DAY"] = today_day

        mgr = mock.Mock()
        scheduler = minerstat_mod.MinerstatScheduler(mgr)
        with (
            mock.patch.object(minerstat_mod, "get_minerstat_snapshot_copy", return_value={}),
            mock.patch.object(
                minerstat_mod,
                "fetch_minerstat_coins",
                side_effect=minerstat_mod.MinerstatError("upstream 502"),
            ),
            mock.patch("tuner_app.profit.auto_apply.apply_profit_recompute") as m_apply,
        ):
            scheduler._tick()

        m_apply.assert_not_called()
