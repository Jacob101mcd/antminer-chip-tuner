import re
from pathlib import Path

MAIN_JS = Path("tuner_app/static/js/main.js").read_text(encoding="utf-8")


def _slice_function_body(name_pattern, source, length=2000):
    m = re.search(name_pattern, source)
    if not m:
        return None
    return source[m.start() : m.start() + length]


def test_bulkAction_warns_when_filter_drops_entries():
    func_body = _slice_function_body(r"function bulkAction", MAIN_JS)
    assert func_body is not None, "bulkAction function not found"
    warn_match = re.search(r"console\.warn\s*\([^)]*\)", func_body)
    assert warn_match is not None, "No console.warn found in bulkAction function"
    warn_call = warn_match.group(0)
    assert "dropped" in warn_call.lower(), "console.warn does not mention 'dropped'"


def test_runBulk_warns_when_response_total_zero():
    func_body = _slice_function_body(r"async function runBulk", MAIN_JS)
    assert func_body is not None, "runBulk function not found"
    warn_match = re.search(r"console\.warn\s*\([^)]*\)", func_body)
    assert warn_match is not None, "No console.warn found in runBulk function"
    warn_call = warn_match.group(0)
    assert any(keyword in warn_call.lower() for keyword in ["total", "0", "succeeded"]), (
        "console.warn does not mention 'total', '0', or 'succeeded'"
    )


def test_console_warn_count_at_least_two_in_main_js():
    warn_count = MAIN_JS.count("console.warn")
    assert warn_count >= 2, f"Expected at least 2 console.warn calls, found {warn_count}"
