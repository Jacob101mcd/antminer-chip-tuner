"""Unit tests for the V/F top-K knob decoupling (Unit 2 of 4).

Three behaviors under test:

1. Cross-field nesting validator removed:
   VF_EXPLORE_TOP_K <= VF_FINE_TOP_K and VF_FINE_TOP_K <=
   VF_COARSE_TOP_K_RAYS are no longer enforced by validate_config.
   The adjacent VF_EXPLORE_TOP_K <= VF_EXPLORE_V_COUNT rule is PRESERVED.

2. Per-knob upper bounds raised in CONFIG_BOUNDS (schema.py):
   VF_EXPLORE_TOP_K:    (1, 5)  -> (1, 50)
   VF_FINE_TOP_K:       (1, 10) -> (1, 50)
   VF_COARSE_TOP_K_RAYS:(1, 10) -> (1, 50)

3. Defensive runtime clamp in find_next_chip_tune_target:
   chip_top_k = min(chip_top_k, fine_top_k) so an oversized
   VF_EXPLORE_TOP_K cannot exceed the fine-anchor pool established
   by VF_FINE_TOP_K; the function must not crash or return
   results outside the top-VF_FINE_TOP_K anchor set.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app.config.validation import validate_config
from tuner_app.tuning_engine.exploration import find_next_chip_tune_target

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fine_cell(voltage_mv, freq_mhz, efficiency_jth, anchor_v, anchor_f):
    """Return a minimal vf_surface fine-mode entry."""
    return {
        "voltage_mv": voltage_mv,
        "freq_mhz": freq_mhz,
        "efficiency_jth": efficiency_jth,
        "hashrate_ths": 200.0,
        "power_w": 4200.0,
        "fine": True,
        "coarse_anchor": {"voltage_mv": anchor_v, "freq_mhz": anchor_f},
    }


def _make_engine_stub(explore_top_k, fine_top_k, fine_count, surface, anchors_returned):
    """Build a minimal mock engine for find_next_chip_tune_target.

    Parameters
    ----------
    explore_top_k : int
        Value returned for config.get("VF_EXPLORE_TOP_K", ...).
    fine_top_k : int
        Value returned for config.get("VF_FINE_TOP_K", ...).
    fine_count : int
        Value returned for config.get("VF_EXPLORE_FINE_COUNT", ...).
    surface : list[dict]
        Contents of engine.vf_surface.
    anchors_returned : list[dict]
        What engine._top_fine_anchors(ctx, k) returns (ignores k).
    """
    engine = MagicMock()

    def config_get(key, default=None):
        mapping = {
            "VF_EXPLORE_TOP_K": explore_top_k,
            "VF_FINE_TOP_K": fine_top_k,
            "VF_EXPLORE_FINE_COUNT": fine_count,
        }
        return mapping.get(key, default)

    engine.config.get.side_effect = config_get
    engine.vf_surface = surface
    engine.voltage_results = []
    # scoring context: efficiency mode, no coin data -> falls back to J/TH
    engine._get_scoring_context.return_value = ("efficiency", 0.10, None, 0.0)
    # _coarse_cells_ranked returns empty (fine mode active)
    engine._coarse_cells_ranked.return_value = []
    # _top_fine_anchors always returns the pre-built anchor list (ignores k arg)
    engine._top_fine_anchors.side_effect = lambda ctx, k: anchors_returned
    return engine


# ---------------------------------------------------------------------------
# Class 1: cross-field validator decoupled
# ---------------------------------------------------------------------------


class TestVFTopKValidatorDecoupled(unittest.TestCase):
    def test_explore_top_k_above_fine_top_k_now_accepted(self):
        """VF_EXPLORE_TOP_K > VF_FINE_TOP_K must NOT produce a validation error after decoupling.

        Uses values within the OLD bounds (EXPLORE_TOP_K=5 <= old upper 5,
        FINE_TOP_K=1 and COARSE_TOP_K_RAYS=1) so the bounds check passes
        regardless of whether the bounds bump landed. The only thing that
        would have fired here is the dropped cross-field nesting rule
        (EXPLORE_TOP_K <= FINE_TOP_K). Assertion: no errors mention
        VF_FINE_TOP_K or VF_COARSE_TOP_K_RAYS.
        """
        _, errors = validate_config(
            {"VF_EXPLORE_TOP_K": 5, "VF_FINE_TOP_K": 1, "VF_COARSE_TOP_K_RAYS": 1}
        )
        fine_errors = [e for e in errors if "VF_FINE_TOP_K" in e or "VF_COARSE_TOP_K_RAYS" in e]
        self.assertEqual(
            fine_errors,
            [],
            msg=(
                "Expected no errors mentioning VF_FINE_TOP_K or VF_COARSE_TOP_K_RAYS; "
                f"cross-field nesting rule should be removed. Got: {errors!r}"
            ),
        )

    def test_fine_top_k_above_coarse_top_k_rays_now_accepted(self):
        """VF_FINE_TOP_K > VF_COARSE_TOP_K_RAYS must NOT error after decoupling.

        Uses values within the OLD bounds (FINE_TOP_K=10 <= old upper 10,
        COARSE_TOP_K_RAYS=1 <= old upper 10) so the bounds check passes
        regardless of whether the bounds bump landed. This exercises ONLY
        the FINE/COARSE cross-field rule, independent of the EXPLORE rule.
        """
        _, errors = validate_config({"VF_FINE_TOP_K": 10, "VF_COARSE_TOP_K_RAYS": 1})
        self.assertEqual(
            errors,
            [],
            msg=(
                "Expected no errors; VF_FINE_TOP_K > VF_COARSE_TOP_K_RAYS rule "
                f"should be removed. Got: {errors!r}"
            ),
        )

    def test_explore_top_k_v_count_validator_preserved(self):
        """VF_EXPLORE_TOP_K > VF_EXPLORE_V_COUNT must still produce an error (unrelated rule).

        Uses values within the OLD AND NEW bounds for EXPLORE_TOP_K
        (EXPLORE_TOP_K=4 <= old upper 5 and new upper 50) and within
        the V_COUNT bounds (V_COUNT=3, lower bound 3). The only thing
        that fires here is the preserved cross-field rule
        EXPLORE_TOP_K <= V_COUNT (4 > 3). This cannot be confused with
        the dropped nesting rule (EXPLORE <= FINE) because neither
        VF_FINE_TOP_K nor VF_COARSE_TOP_K_RAYS is provided.
        """
        _, errors = validate_config({"VF_EXPLORE_TOP_K": 4, "VF_EXPLORE_V_COUNT": 3})
        self.assertTrue(
            any("VF_EXPLORE_V_COUNT" in e for e in errors),
            msg=(
                "Expected error mentioning VF_EXPLORE_V_COUNT; the V_COUNT "
                f"cross-check must be preserved. Got: {errors!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Class 2: per-knob upper bounds raised to 50
# ---------------------------------------------------------------------------


class TestVFTopKBoundsRaised(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # VF_EXPLORE_TOP_K  old upper 5, new upper 50
    # ------------------------------------------------------------------ #

    def test_explore_top_k_at_old_upper_accepted(self):
        """VF_EXPLORE_TOP_K=5 (old upper bound) is accepted after the bounds raise."""
        _, errors = validate_config({"VF_EXPLORE_TOP_K": 5})
        self.assertEqual(errors, [])

    def test_explore_top_k_at_new_upper_accepted(self):
        """VF_EXPLORE_TOP_K=50 with VF_EXPLORE_V_COUNT=50 (both at new upper) is accepted.

        The preserved cross-field rule VF_EXPLORE_TOP_K <= VF_EXPLORE_V_COUNT
        means the new TOP_K bound (1, 50) is only reachable when V_COUNT is
        raised in the same submission. V_COUNT bound was widened to (3, 50)
        as a sister change.
        """
        _, errors = validate_config({"VF_EXPLORE_TOP_K": 50, "VF_EXPLORE_V_COUNT": 50})
        self.assertEqual(
            errors,
            [],
            msg=f"VF_EXPLORE_TOP_K=50 with V_COUNT=50 should be accepted. Got: {errors!r}",
        )

    def test_explore_top_k_above_new_upper_rejected(self):
        """VF_EXPLORE_TOP_K=51 (above new upper bound) produces a bounds error mentioning 50."""
        _, errors = validate_config({"VF_EXPLORE_TOP_K": 51})
        self.assertTrue(
            any("50" in e for e in errors),
            msg=f"Expected bounds error mentioning '50'. Got: {errors!r}",
        )

    # ------------------------------------------------------------------ #
    # VF_FINE_TOP_K  old upper 10, new upper 50
    # ------------------------------------------------------------------ #

    def test_fine_top_k_at_old_upper_accepted(self):
        """VF_FINE_TOP_K=10 (old upper bound) is accepted after the bounds raise."""
        _, errors = validate_config({"VF_FINE_TOP_K": 10})
        self.assertEqual(errors, [])

    def test_fine_top_k_at_new_upper_accepted(self):
        """VF_FINE_TOP_K=50 (new upper bound) is accepted."""
        _, errors = validate_config({"VF_FINE_TOP_K": 50})
        self.assertEqual(
            errors,
            [],
            msg=f"VF_FINE_TOP_K=50 should be accepted at the new upper bound. Got: {errors!r}",
        )

    def test_fine_top_k_above_new_upper_rejected(self):
        """VF_FINE_TOP_K=51 (above new upper bound) produces a bounds error mentioning 50."""
        _, errors = validate_config({"VF_FINE_TOP_K": 51})
        self.assertTrue(
            any("50" in e for e in errors),
            msg=f"Expected bounds error mentioning '50'. Got: {errors!r}",
        )

    # ------------------------------------------------------------------ #
    # VF_COARSE_TOP_K_RAYS  old upper 10, new upper 50
    # ------------------------------------------------------------------ #

    def test_coarse_top_k_rays_at_old_upper_accepted(self):
        """VF_COARSE_TOP_K_RAYS=10 (old upper bound) is accepted after the bounds raise."""
        _, errors = validate_config({"VF_COARSE_TOP_K_RAYS": 10})
        self.assertEqual(errors, [])

    def test_coarse_top_k_rays_at_new_upper_accepted(self):
        """VF_COARSE_TOP_K_RAYS=50 (new upper bound) is accepted."""
        _, errors = validate_config({"VF_COARSE_TOP_K_RAYS": 50})
        self.assertEqual(
            errors,
            [],
            msg=f"VF_COARSE_TOP_K_RAYS=50 should be accepted at new upper. Got: {errors!r}",
        )

    def test_coarse_top_k_rays_above_new_upper_rejected(self):
        """VF_COARSE_TOP_K_RAYS=51 (above new upper bound) produces a bounds error mentioning 50."""
        _, errors = validate_config({"VF_COARSE_TOP_K_RAYS": 51})
        self.assertTrue(
            any("50" in e for e in errors),
            msg=f"Expected bounds error mentioning '50'. Got: {errors!r}",
        )


# ---------------------------------------------------------------------------
# Class 3: runtime clamp in find_next_chip_tune_target
# ---------------------------------------------------------------------------


class TestVFRuntimeClamp(unittest.TestCase):
    def test_find_next_chip_tune_target_no_crash_with_oversized_top_k(self):
        """find_next_chip_tune_target does not crash with TOP_K=50 and one fine cell."""
        # Single valid fine cell; surface is sparse relative to top_k=50.
        surface = [
            _make_fine_cell(
                voltage_mv=14000,
                freq_mhz=490.0,
                efficiency_jth=20.0,
                anchor_v=14000,
                anchor_f=490.0,
            )
        ]
        anchors = [{"voltage_mv": 14000, "freq_mhz": 490.0}]
        engine = _make_engine_stub(
            explore_top_k=50,
            fine_top_k=50,
            fine_count=9,  # >= 2 enables fine-grid path
            surface=surface,
            anchors_returned=anchors,
        )
        try:
            result = find_next_chip_tune_target(engine)
        except (ValueError, IndexError) as exc:
            self.fail(
                f"find_next_chip_tune_target raised {exc!r} with oversized EXPLORE_TOP_K; "
                "expected no crash."
            )
        # Result is either the single cell or None (if already chip-tuned).
        self.assertTrue(
            result is None or isinstance(result, dict),
            msg=f"Expected dict or None; got {result!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
