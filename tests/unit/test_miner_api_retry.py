"""Unit tests for MinerCommandPending retry budget and exception ordering.

Covers:
- _post raises MinerCommandPending after exactly 60 pending retries
- _post returns True if a pending sequence ends with success
- Non-pending error raises plain MinerCommandError
- MinerCommandPending IS a subclass of MinerCommandError
- tuner.py's _run loop catches MinerCommandPending BEFORE MinerCommandError
"""

from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from tuner_app.miner.api import MinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerCommandPending

PATCH_HTTP = "tuner_app.miner.epic.miner_http_request"
PATCH_SLEEP = "tuner_app.miner.epic.time.sleep"


def _make_http_responder(responses):
    """Return a callable popping responses off `responses` in order.
    Tolerates positional and keyword args (matches miner_http_request)."""
    pending = list(responses)

    def responder(*args, **kwargs):
        if not pending:
            raise IndexError("No more responses")
        return pending.pop(0)

    return responder


class TestPendingRetryBudget(unittest.TestCase):
    def test_pending_default_budget_is_60_x_5s(self):
        """60 pending responses raises MinerCommandPending with '60 x 5s' in message."""
        api = MinerAPI("127.0.0.1")
        pending_response = (
            200,
            {},
            b'{"result": false, "error": "Last command is still pending"}',
        )
        responses = [pending_response] * 100
        with (
            patch(PATCH_HTTP, side_effect=_make_http_responder(responses)),
            patch(PATCH_SLEEP, return_value=None),
        ):
            with self.assertRaises(MinerCommandPending) as cm:
                api._post("/tune/voltage", 14000)
            self.assertIn("60 × 5s", str(cm.exception))

    def test_pending_succeeds_within_budget(self):
        """5 pending then 1 success: _post returns True."""
        api = MinerAPI("127.0.0.1")
        pending_response = (
            200,
            {},
            b'{"result": false, "error": "Last command is still pending"}',
        )
        success_response = (200, {}, b'{"result": true, "error": null}')
        responses = [pending_response] * 5 + [success_response]
        with (
            patch(PATCH_HTTP, side_effect=_make_http_responder(responses)),
            patch(PATCH_SLEEP, return_value=None),
        ):
            self.assertTrue(api._post("/tune/voltage", 14000))

    def test_pending_below_budget_does_not_raise(self):
        """59 pending then 1 success: _post returns True (below 60-budget cap)."""
        api = MinerAPI("127.0.0.1")
        pending_response = (
            200,
            {},
            b'{"result": false, "error": "Last command is still pending"}',
        )
        success_response = (200, {}, b'{"result": true, "error": null}')
        responses = [pending_response] * 59 + [success_response]
        with (
            patch(PATCH_HTTP, side_effect=_make_http_responder(responses)),
            patch(PATCH_SLEEP, return_value=None),
        ):
            self.assertTrue(api._post("/tune/voltage", 14000))

    def test_non_pending_error_raises_command_error_not_pending(self):
        """A non-pending miner error raises plain MinerCommandError."""
        api = MinerAPI("127.0.0.1")
        error_response = (200, {}, b'{"result": false, "error": "Some other error"}')
        with (
            patch(PATCH_HTTP, side_effect=_make_http_responder([error_response])),
            patch(PATCH_SLEEP, return_value=None),
        ):
            with self.assertRaises(MinerCommandError) as cm:
                api._post("/tune/voltage", 14000)
            self.assertNotIsInstance(cm.exception, MinerCommandPending)


class TestExceptionHierarchyOrder(unittest.TestCase):
    def test_pending_is_subclass_of_command_error(self):
        """MinerCommandPending IS a subclass of MinerCommandError."""
        self.assertTrue(issubclass(MinerCommandPending, MinerCommandError))

    def test_pending_caught_before_command_error_in_run_loop(self):
        """`except MinerCommandPending` precedes `(MinerNotReady, MinerCommandError)`."""
        # The TuningEngine class shell (including the _run retry loop with these
        # except clauses) moved to tuner_app/tuning_engine/engine.py during
        # Phase 6 finalization. The clauses themselves are unchanged.
        engine_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "tuner_app",
            "tuning_engine",
            "engine.py",
        )
        with open(engine_path) as f:
            engine_source = f.read()
        pending_match = re.search(r"except\s+MinerCommandPending", engine_source)
        error_match = re.search(r"except\s+\(MinerNotReady,\s*MinerCommandError\)", engine_source)
        self.assertIsNotNone(pending_match)
        self.assertIsNotNone(error_match)
        self.assertLess(pending_match.start(), error_match.start())


if __name__ == "__main__":
    unittest.main(verbosity=2)
