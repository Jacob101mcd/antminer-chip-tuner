"""Release gates for branding, operational identifiers, and private artifacts."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".bat",
    ".cff",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rtf",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
SKIP_PARTS = {".git", ".venv", ".uv-cache", "build", "dist", "__pycache__"}

# Confidential release-denylist entries are represented only by a length and a
# one-way digest. The public gate can catch a regression without republishing a
# removed brand or deployment identifier in its own source.
DENIED_BRAND = {
    8: {"6d35a3b0983659a8db51745ab233f66d898e799b63c42647f26b1db88720bb70"},
}
DENIED_DEPLOYMENT_IDENTIFIERS = {
    7: {"b1f134512620e3069fc9aefb864bfd5cdd09486e858ca683c58472762787f63f"},
    8: {"bf303ca1926256c0da73fe852642642a88c14fafad869846a71c7f8018e4a489"},
    9: {"a815281426d6f4c948abe2954a9e689a69bf1ea7ebe3f7aa108ce8d92391e6ce"},
    10: {"5049bd0f35c1f8aea3ece05d040156dfa7491ab90672981147a2d94fc5551b8c"},
    16: {"66f8f64d3229ba9572da360f42a63e357db654dc41e50a64c180593e72e2e2af"},
    17: {
        "03a48f41f2bb7e6f9b1fe0a57319d3d446d04d6ff74128d8b80860c0dc8fe769",
        "f1ed697e713778acbf2100ed64670f87b47a3b43030cd71ab51256bf5fd92833",
    },
    24: {"c97e494eb180743543a6bbc1d50c6c45a2a43e69ad33515dd3bcee720dd8b20f"},
    27: {"568f968405bae87cf41839a368dfec51ed1192730b5077fc97c676d68d7013cb"},
}


def _public_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if SKIP_PARTS.intersection(path.parts):
            continue
        yield path


def _contains_denied_digest(text: str, denylist: dict[int, set[str]]) -> bool:
    normalized = text.casefold()
    for length, digests in denylist.items():
        for start in range(0, len(normalized) - length + 1):
            candidate = normalized[start : start + length]
            if sha256(candidate.encode()).hexdigest() in digests:
                return True
    return False


def test_removed_brand_is_absent() -> None:
    matches = []
    for path in _public_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _contains_denied_digest(text, DENIED_BRAND):
            matches.append(str(path.relative_to(ROOT)))
    assert not matches, f"removed brand remains in: {matches}"


def test_known_live_identifiers_are_absent() -> None:
    matches = []
    for path in _public_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _contains_denied_digest(text, DENIED_DEPLOYMENT_IDENTIFIERS):
            matches.append(str(path.relative_to(ROOT)))
        if path != Path(__file__) and (
            "/users/" in text.casefold() or re.search(r"@[\w.-]+\.local\b", text, re.I)
        ):
            matches.append(str(path.relative_to(ROOT)))
    assert not matches, "known deployment identifiers remain in:\n" + "\n".join(matches)


def test_private_workflow_and_runtime_artifacts_are_absent() -> None:
    forbidden_paths = [
        ROOT / "tuning_data",
        ROOT / "session.cookies",
    ]
    assert not [str(path.relative_to(ROOT)) for path in forbidden_paths if path.exists()]
    assert not list((ROOT / "Independent-research-main").rglob("*-log_old.txt"))


def test_uncleared_vendor_imports_are_absent() -> None:
    forbidden_paths = [
        ROOT / "docs" / "whatsminer" / "api.md",
        ROOT / "docs" / "whatsminer" / "example_script.md",
        ROOT / "docs" / "braiins" / "api.md",
    ]
    assert not [str(path.relative_to(ROOT)) for path in forbidden_paths if path.exists()]


def test_attribution_is_preserved() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Jacob McDaniel" in readme
    assert "UVA" in readme or "University of Virginia" in readme
    assert "Jacob101mcd" in readme
