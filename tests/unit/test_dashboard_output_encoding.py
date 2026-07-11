"""Static regression checks for miner-controlled dashboard text."""

from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2] / "tuner_app" / "static" / "js" / "main.js"
).read_text(encoding="utf-8")


def test_log_lines_are_html_encoded() -> None:
    assert 'map(l=>`<div class="log-line">${escapeHTML(l)}</div>`)' in SOURCE
    assert 'map(l=>`<div class="log-line">${l}</div>`)' not in SOURCE


def test_phase_detail_is_not_interpolated_raw() -> None:
    assert "${s.phase_detail || ''}" not in SOURCE
    assert "const safePhaseDetail = escapeHTML(s.phase_detail || '');" in SOURCE
