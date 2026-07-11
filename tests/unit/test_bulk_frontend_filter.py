# tests/unit/test_bulk_frontend_filter.py
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"


def test_isValidMacForBulk_helper_defined_exactly_once():
    content = MAIN_JS.read_text()
    matches = re.findall(r"function isValidMacForBulk\(", content)
    assert len(matches) == 1


def test_filter_regex_includes_synth_pattern():
    content = MAIN_JS.read_text()
    assert "^syn-[0-9a-fA-F]" in content


def test_bulkAction_calls_validator():
    content = MAIN_JS.read_text()
    bulk_action_start = content.find("function bulkAction(")
    assert bulk_action_start != -1
    next_function_start = content.find("\nfunction ", bulk_action_start)
    assert next_function_start != -1
    bulk_action_body = content[bulk_action_start:next_function_start]
    assert "isValidMacForBulk" in bulk_action_body


def test_warning_banner_string_present():
    content = MAIN_JS.read_text()
    assert "stale or malformed" in content or "malformed selection" in content


def test_all_invalid_error_message_present():
    content = MAIN_JS.read_text()
    assert "Please reselect by reloading the page" in content
