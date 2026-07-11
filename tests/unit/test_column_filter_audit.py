# tests/unit/test_column_filter_audit.py
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
DASHBOARD_HTML = PROJECT_ROOT / "tuner_app" / "static" / "dashboard.html"


def test_fleet_columns_const_exists_exactly_once():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"FLEET_COLUMNS\s*=\s*\[", content)) == 1


def test_fleet_columns_has_all_thirteen_keys():
    content = MAIN_JS.read_text(encoding="utf-8")
    match = re.search(r"FLEET_COLUMNS\s*=\s*(\[[\s\S]*?\]);", content)
    assert match is not None
    slice_content = match.group(1)
    keys = re.findall(r"key:\s*[\"']([^\"']+)[\"']", slice_content)
    expected_keys = [
        "mac",
        "mrr",
        "hostname",
        "model",
        "state",
        "phase",
        "hashrate",
        "power",
        "efficiency",
        "profit",
        "voltage",
        "board_t",
        "chip_t",
    ]
    assert keys == expected_keys


def test_fleet_columns_labels_present():
    content = MAIN_JS.read_text(encoding="utf-8")
    match = re.search(r"FLEET_COLUMNS\s*=\s*(\[[\s\S]*?\]);", content)
    assert match is not None
    slice_content = match.group(1)
    labels = re.findall(r"label:\s*[\"']([^\"']+)[\"']", slice_content)
    expected_labels = [
        "MAC",
        "MRR",
        "Hostname",
        "Model",
        "State",
        "Phase",
        "Hashrate",
        "Power",
        "J/TH",
        "$/day",
        "Voltage",
        "Board T",
        "Chip T",
    ]
    assert labels == expected_labels


def test_storage_key_versioned():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert "tuner.fleetTable.columnPrefs.v1" in content


def test_load_column_prefs_defined():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function loadColumnPrefs\(", content)) == 1


def test_save_column_prefs_defined():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function saveColumnPrefs\(", content)) == 1


def test_normalize_column_prefs_defined():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function normalizeColumnPrefs\(", content)) == 1


def test_state_pill_helper_defined():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function statePillFor\(", content)) == 1


def test_phase_pill_helper_defined():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function phasePillFor\(", content)) == 1


def test_profit_helper_defined():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function formatProfit\(", content)) == 1


def test_render_table_header_function_exists():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function renderTableHeader\(", content)) == 1


def test_render_table_header_called_in_boot():
    content = MAIN_JS.read_text(encoding="utf-8")
    # The function containing the loadModelFilter() CALL SITE (startApp)
    # should also call renderTableHeader() near it so the empty <thead>
    # gets populated before the first render. The semicolon distinguishes
    # the call site (`loadModelFilter();`) from the function declaration
    # (`function loadModelFilter(){`).
    idx = content.find("loadModelFilter();")
    assert idx >= 0
    window = content[idx : idx + 600]
    assert "renderTableHeader()" in window


def test_get_active_columns_helper_exists():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function getActiveColumns\(", content)) == 1


def test_dashboard_thead_collapsed():
    dashboard_html = DASHBOARD_HTML.read_text(encoding="utf-8")
    # Locate the miner-table specifically (other tables exist in modals).
    table_match = re.search(
        r'<table id="miner-table">[\s\S]*?</table>',
        dashboard_html,
    )
    assert table_match is not None
    table_html = table_match.group(0)
    thead_match = re.search(
        r"<thead[^>]*>\s*<tr[^>]*>([\s\S]*?)</tr>\s*</thead>",
        table_html,
    )
    assert thead_match is not None
    inner_content = thead_match.group(1)
    # Should be empty or whitespace only — dynamically populated by
    # renderTableHeader() at boot.
    assert inner_content.strip() == ""


def test_render_table_dynamic_colspan():
    content = MAIN_JS.read_text(encoding="utf-8")
    start = content.find("function renderTable(")
    assert start >= 0
    end = content.find("\nfunction ", start + 1)
    assert end > start
    func_body = content[start:end]
    assert 'colspan="15"' not in func_body
    assert 'colspan="${' in func_body


def test_dashboard_no_hardcoded_colspan_15():
    dashboard_html = DASHBOARD_HTML.read_text(encoding="utf-8")
    # The pre-Unit-3 dashboard.html had `colspan="15"` in two places (the
    # static thead implicit count + the Loading… row). Both must be gone
    # post-Unit-3 — the thead is now dynamically populated, and the Loading
    # row uses a smaller fallback colspan that doesn't lock in the count.
    assert 'colspan="15"' not in dashboard_html


def test_open_column_filter_modal_function_exists():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function openColumnFilterModal\(", content)) == 1


def test_apply_column_preset_function_exists():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function applyColumnPreset\(", content)) == 1


def test_submit_column_prefs_function_exists():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert len(re.findall(r"function submitColumnPrefs\(", content)) == 1


def test_column_presets_const_with_four_presets():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert "COLUMN_PRESETS" in content
    assert "default:" in content
    assert "compact:" in content
    assert "thermals:" in content
    assert "profitability:" in content


def test_actions_object_has_column_filter_entries():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert "openColumnFilterModal:" in content
    assert "applyColumnPreset:" in content


def test_dashboard_has_columns_trigger_button():
    content = DASHBOARD_HTML.read_text(encoding="utf-8")
    assert 'data-action="openColumnFilterModal"' in content
    assert content.count('data-action="openColumnFilterModal"') == 1


def test_four_preset_arg_names_present():
    content = MAIN_JS.read_text(encoding="utf-8")
    assert 'data-arg-name="default"' in content
    assert 'data-arg-name="compact"' in content
    assert 'data-arg-name="thermals"' in content
    assert 'data-arg-name="profitability"' in content


def test_fleet_columns_mac_entry_default_hidden():
    content = MAIN_JS.read_text(encoding="utf-8")
    match = re.search(r"FLEET_COLUMNS\s*=\s*(\[[\s\S]*?\]);", content)
    assert match is not None
    slice_content = match.group(1)
    # The mac entry must have defaultVisible: false (whitespace-flexible).
    assert re.search(r"key:\s*['\"]mac['\"][^}]*defaultVisible:\s*false", slice_content) is not None


def test_fleet_columns_only_mac_has_default_hidden():
    content = MAIN_JS.read_text(encoding="utf-8")
    match = re.search(r"FLEET_COLUMNS\s*=\s*(\[[\s\S]*?\]);", content)
    assert match is not None
    slice_content = match.group(1)
    # Exactly one entry has defaultVisible: false (the mac entry).
    matches = re.findall(r"defaultVisible:\s*false", slice_content)
    assert len(matches) == 1


def test_normalize_column_prefs_uses_default_visible_semantic():
    content = MAIN_JS.read_text(encoding="utf-8")
    # Extract the normalizeColumnPrefs function body.
    fn_match = re.search(r"function normalizeColumnPrefs\([\s\S]*?\n\}", content)
    assert fn_match is not None
    fn_body = fn_match.group(0)
    # The missingColumns mapping line must use col.defaultVisible !== false.
    assert re.search(r"col\.defaultVisible\s*!==\s*false", fn_body) is not None


def test_column_presets_do_not_contain_mac():
    content = MAIN_JS.read_text(encoding="utf-8")
    match = re.search(r"COLUMN_PRESETS\s*=\s*\{([\s\S]*?)\};", content)
    assert match is not None
    presets_body = match.group(1)
    # Neither 'mac' nor "mac" may appear inside the preset arrays.
    assert "'mac'" not in presets_body
    assert '"mac"' not in presets_body
