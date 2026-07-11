"""EpicMinerAPI.summary() merges hardware model from /capabilities cache.

ePIC PowerPlay-BMS doesn't expose the hardware model in /summary — it lives
at /capabilities (Model = "AntMiner S21"). The DTO from_epic() leaves
result.model as None for real responses; EpicMinerAPI.summary() then fetches
/capabilities once per client lifetime and merges Model into the DTO.

Coverage:
- summary() populates model from /capabilities on first call
- /capabilities cached: only one API call across multiple summary() calls
- /capabilities returns None: model stays None, no retry storm on subsequent calls
- /capabilities raises MinerOfflineError: caught, summary still returns valid DTO
- /capabilities raises MinerCommandError: caught, summary still returns valid DTO
"""

from __future__ import annotations

from unittest.mock import patch

from tuner_app.miner.epic import EpicMinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError


def _summary_fixture():
    """Representative PowerPlay-BMS response with no inline model field."""
    return {
        "Status": {"Operating State": "Mining"},
        "Hostname": "miner-example",
        "Power Supply Stats": {"Input Power": 3000.0, "Target Voltage": 14000},
        "Fans": {"Fans Speed": 20},
        "HBs": [{"Hashrate": [60000000.0, 98.0, 40.0], "Core Clock Avg": 450.0}],
    }


def _capabilities_fixture():
    """Representative capabilities response: Model lives here, not in summary."""
    return {
        "Model": "AntMiner S21",
        "Model Subtype": "BHB68603",
        "Chip Type": "BM1368",
        "Default Clock": 460,
        "Default Voltage": 14000,
    }


def test_summary_populates_model_from_capabilities():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()),
    ):
        result = miner.summary()
    assert result.hostname == "miner-example"
    assert result.model == "AntMiner S21"


def test_capabilities_fetched_once_then_cached():
    """Subsequent summary() calls don't re-fetch /capabilities — the result
    is cached on the EpicMinerAPI instance because hardware info is static."""
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "capabilities", return_value=_capabilities_fixture()) as mock_caps,
    ):
        for _ in range(5):
            result = miner.summary()
            assert result.model == "AntMiner S21"
        assert mock_caps.call_count == 1


def test_capabilities_returns_none_model_stays_none_no_retry_storm():
    """If /capabilities returns None (HTTP 404 / malformed JSON / older firmware),
    cache the empty sentinel so subsequent summary() calls don't keep retrying."""
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "capabilities", return_value=None) as mock_caps,
    ):
        for _ in range(3):
            result = miner.summary()
            assert result.model is None
        assert mock_caps.call_count == 1


def test_capabilities_offline_error_swallowed():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "capabilities", side_effect=MinerOfflineError("connection refused")),
    ):
        result = miner.summary()
    # summary() still returns a valid DTO; only model is None
    assert result.hostname == "miner-example"
    assert result.operating_state == "Mining"
    assert result.model is None


def test_capabilities_command_error_swallowed():
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(miner, "capabilities", side_effect=MinerCommandError("HTTP 500")),
    ):
        result = miner.summary()
    assert result.hostname == "miner-example"
    assert result.model is None


def test_capabilities_failure_is_cached_no_retry_on_subsequent_calls():
    """A failed /capabilities fetch sets the cache to {} so we don't keep
    hitting a broken endpoint every summary() call."""
    miner = EpicMinerAPI("1.2.3.4")
    with (
        patch.object(miner, "_summary_raw", return_value=_summary_fixture()),
        patch.object(
            miner, "capabilities", side_effect=MinerOfflineError("connection refused")
        ) as mock_caps,
    ):
        for _ in range(3):
            miner.summary()
        assert mock_caps.call_count == 1
