"""Unit tests for the profitability tuning helpers.

Covers:
  - compute_profit_usd_per_day: math correctness + graceful None handling
  - score_cell: efficiency vs profit mode, returns lower-is-better scalars,
                None for unrankable entries

Run with: `python -m pytest tests/test_profit.py -v`
Or plain: `python tests/test_profit.py`
"""

import unittest

from tuner_app.profit.compute import compute_profit_usd_per_day, score_cell

# Reference coin data used across tests. BTC-ish scenario at realistic
# numbers: $42k price, 6.25 BTC/block, 600s blocks, 520 EH/s network.
BTC_COIN = {
    "price_usd": 42000.0,
    "reward_block": 6.25,
    "network_hashrate": 5.2e20,  # 520 EH/s
    "block_time_s": 600,
}


class TestComputeProfitUsdPerDay(unittest.TestCase):
    """Verify the profit math itself — independent of score_cell wrapping."""

    def test_sanity_range(self):
        """A 200 TH/s miner at 4000 W, $0.10/kWh, current BTC economics
        should land in a plausible $5-$20/day range. We're not pinning the
        exact number (depends on hashprice assumptions) but checking the
        order of magnitude."""
        profit = compute_profit_usd_per_day(200, 4000, BTC_COIN, 0.10)
        self.assertIsNotNone(profit)
        # Revenue = 200 TH/s * (86400/600) * 6.25 * (1e12 / 5.2e20) * $42000
        #        ~= 200 * 144 * 6.25 * 1.923e-9 * 42000
        #        ~= 200 * 144 * 6.25 * 8.077e-5
        #        ~= 14.54 $/day
        # Cost = 4000 W * 24 / 1000 * 0.10 = $9.60/day
        # Profit ~= 14.54 - 9.60 ~= $4.94/day
        self.assertAlmostEqual(
            profit, 4.94, delta=0.5, msg=f"Expected ~$4.94/day, got {profit:.2f}"
        )

    def test_increasing_hashrate_increases_profit(self):
        """Revenue scales linearly with hashrate at constant power."""
        p_low = compute_profit_usd_per_day(100, 4000, BTC_COIN, 0.10)
        p_high = compute_profit_usd_per_day(200, 4000, BTC_COIN, 0.10)
        self.assertGreater(p_high, p_low)

    def test_increasing_power_decreases_profit(self):
        """Cost scales linearly with power at constant hashrate."""
        p_efficient = compute_profit_usd_per_day(200, 3500, BTC_COIN, 0.10)
        p_hungry = compute_profit_usd_per_day(200, 5000, BTC_COIN, 0.10)
        self.assertGreater(p_efficient, p_hungry)

    def test_high_electric_rate_flips_to_loss(self):
        """At high enough $/kWh, every miner loses money."""
        profit = compute_profit_usd_per_day(200, 4000, BTC_COIN, 1.0)
        self.assertLess(profit, 0, msg=f"Expected loss at $1/kWh, got ${profit:.2f}/day")

    def test_none_inputs_return_none(self):
        self.assertIsNone(compute_profit_usd_per_day(None, 4000, BTC_COIN, 0.10))
        self.assertIsNone(compute_profit_usd_per_day(200, None, BTC_COIN, 0.10))
        self.assertIsNone(compute_profit_usd_per_day(200, 4000, None, 0.10))
        self.assertIsNone(compute_profit_usd_per_day(200, 4000, BTC_COIN, None))

    def test_malformed_coin_data_returns_none(self):
        bad_price = dict(BTC_COIN)
        bad_price["price_usd"] = "not a number"
        self.assertIsNone(compute_profit_usd_per_day(200, 4000, bad_price, 0.10))

        missing_field = dict(BTC_COIN)
        del missing_field["reward_block"]
        self.assertIsNone(compute_profit_usd_per_day(200, 4000, missing_field, 0.10))

    def test_zero_network_hashrate_returns_none(self):
        """Division-by-zero guard — a bad minerstat response shouldn't crash."""
        bad = dict(BTC_COIN)
        bad["network_hashrate"] = 0
        self.assertIsNone(compute_profit_usd_per_day(200, 4000, bad, 0.10))

    def test_zero_block_time_returns_none(self):
        bad = dict(BTC_COIN)
        bad["block_time_s"] = 0
        self.assertIsNone(compute_profit_usd_per_day(200, 4000, bad, 0.10))


class TestScoreCellEfficiencyMode(unittest.TestCase):
    """Efficiency mode: score returns efficiency_jth directly. Lower = better."""

    def test_returns_efficiency_jth(self):
        entry = {"efficiency_jth": 20.5, "hashrate_ths": 200, "power_w": 4100}
        self.assertEqual(score_cell(entry, "efficiency", 0.0, None), 20.5)

    def test_ranks_monotonically(self):
        """Lower J/TH scores lower — directly usable with min()."""
        good = {"efficiency_jth": 18.0, "hashrate_ths": 220, "power_w": 3960}
        bad = {"efficiency_jth": 22.0, "hashrate_ths": 180, "power_w": 3960}
        self.assertLess(
            score_cell(good, "efficiency", 0.0, None),
            score_cell(bad, "efficiency", 0.0, None),
        )

    def test_missing_jth_returns_none(self):
        entry = {"hashrate_ths": 200, "power_w": 4100}  # no efficiency_jth
        self.assertIsNone(score_cell(entry, "efficiency", 0.0, None))

    def test_none_entry_returns_none(self):
        self.assertIsNone(score_cell(None, "efficiency", 0.0, None))


class TestScoreCellProfitMode(unittest.TestCase):
    """Profit mode: score returns -profit_usd_per_day (negated so min() picks
    the most-profitable entry)."""

    def test_returns_negated_profit(self):
        entry = {"efficiency_jth": 20.5, "hashrate_ths": 200, "power_w": 4000}
        s = score_cell(entry, "profitability", 0.10, BTC_COIN)
        expected_profit = compute_profit_usd_per_day(200, 4000, BTC_COIN, 0.10)
        self.assertAlmostEqual(s, -expected_profit, places=6)

    def test_more_profitable_scores_lower(self):
        """The min()-selection pattern must pick the more profitable cell."""
        # Same efficiency (same power/hashrate ratio) but higher absolute
        # hashrate ⇒ more profit, so min() should pick it.
        small = {"efficiency_jth": 20.0, "hashrate_ths": 180, "power_w": 3600}
        big = {"efficiency_jth": 20.0, "hashrate_ths": 220, "power_w": 4400}
        self.assertLess(
            score_cell(big, "profitability", 0.10, BTC_COIN),
            score_cell(small, "profitability", 0.10, BTC_COIN),
            msg="bigger hashrate at same efficiency should win in profit mode",
        )

    def test_efficiency_winner_beats_profit_winner_when_possible(self):
        """Sanity check: if two cells have same hashrate, the lower-power
        one wins in BOTH modes (always more efficient AND more profitable)."""
        low_power = {"efficiency_jth": 18.0, "hashrate_ths": 200, "power_w": 3600}
        high_power = {"efficiency_jth": 22.0, "hashrate_ths": 200, "power_w": 4400}
        self.assertLess(
            score_cell(low_power, "profitability", 0.10, BTC_COIN),
            score_cell(high_power, "profitability", 0.10, BTC_COIN),
        )
        self.assertLess(
            score_cell(low_power, "efficiency", 0.0, None),
            score_cell(high_power, "efficiency", 0.0, None),
        )

    def test_no_coin_data_returns_none(self):
        """Profit mode with no minerstat snapshot ⇒ cell is unrankable."""
        entry = {"efficiency_jth": 20.5, "hashrate_ths": 200, "power_w": 4000}
        self.assertIsNone(score_cell(entry, "profitability", 0.10, None))

    def test_missing_hashrate_returns_none(self):
        """Cells without measurement data can't be profit-ranked."""
        entry = {"efficiency_jth": 20.5, "power_w": 4000}
        self.assertIsNone(score_cell(entry, "profitability", 0.10, BTC_COIN))

    def test_negated_profit_preserves_sign(self):
        """A losing cell (negative profit) should score POSITIVE under
        negation, so it sorts WORSE than any profitable cell."""
        winning = {"efficiency_jth": 18.0, "hashrate_ths": 220, "power_w": 3960}
        losing = {"efficiency_jth": 50.0, "hashrate_ths": 80, "power_w": 4000}
        # Very high $/kWh makes both losing-ish, but the more-profitable
        # cell still has a smaller score.
        s_win = score_cell(winning, "profitability", 0.50, BTC_COIN)
        s_lose = score_cell(losing, "profitability", 0.50, BTC_COIN)
        self.assertLess(s_win, s_lose)


class TestScoreCellEdgeCases(unittest.TestCase):
    def test_unknown_mode_falls_back_to_efficiency(self):
        """Defensive: an accidental bad mode string should not crash."""
        entry = {"efficiency_jth": 20.5, "hashrate_ths": 200, "power_w": 4000}
        # Any non-"profitability" mode treats as efficiency.
        self.assertEqual(score_cell(entry, "weird", 0.0, None), 20.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
