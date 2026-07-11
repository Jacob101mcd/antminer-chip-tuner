from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from unittest import TestCase

from tuner_app.miner.registry import MINER_API_REGISTRY

PROJECT_ROOT = Path(__file__).parent.parent.parent


class TestConfigVisibilityPerPlatform(TestCase):
    def test_grep_no_vendor_disabled_anywhere(self):
        # Exclude this test file + its compiled bytecode (both contain the
        # literal we're asserting is gone from every other file).
        result = subprocess.run(
            [
                "grep",
                "-r",
                "--exclude=test_config_visibility_per_platform.py",
                "--exclude-dir=__pycache__",
                "vendor-disabled",
                "tuner_app/static/",
                "tests/",
            ],
            capture_output=True,
            cwd=PROJECT_ROOT,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.decode(), "")

    def test_grep_no_not_applicable_tooltip(self):
        result = subprocess.run(
            ["grep", "-r", "Not applicable to this firmware", "tuner_app/static/"],
            capture_output=True,
            cwd=PROJECT_ROOT,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.decode(), "")

    def test_visible_config_set_per_firmware(self):
        main_js_path = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        js_content = main_js_path.read_text()

        # Extract PLATFORM_CAPABILITIES
        _PLATFORM_CAPS_PATTERN = re.compile(
            r"const\s+PLATFORM_CAPABILITIES\s*=\s*({.*?^});",
            re.DOTALL | re.MULTILINE,
        )
        match = _PLATFORM_CAPS_PATTERN.search(js_content)
        self.assertIsNotNone(match)
        js_obj_str = match.group(1)
        js_obj_str = re.sub(r"//.*$", "", js_obj_str, flags=re.MULTILINE)
        js_obj_str = re.sub(r"\btrue\b", "True", js_obj_str)
        js_obj_str = re.sub(r"\bfalse\b", "False", js_obj_str)
        js_obj_str = re.sub(r"\bnull\b", "None", js_obj_str)
        js_obj_str = re.sub(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\g<key>":', js_obj_str)
        js_obj_str = re.sub(r",(\s*[}\]])", r"\1", js_obj_str)
        platform_caps = ast.literal_eval(js_obj_str)

        # Extract CFG_META - LINE-BY-LINE APPROACH FOR requires ONLY
        _CFG_META_PATTERN = re.compile(
            r"const\s+CFG_META\s*=\s*({.*?^});",
            re.DOTALL | re.MULTILINE,
        )
        match = _CFG_META_PATTERN.search(js_content)
        self.assertIsNotNone(match)
        cfg_meta_block = match.group(1)
        cfg_meta_requires = {}
        # Match any line that opens a CFG_META key entry
        _CFG_META_KEY_PATTERN = re.compile(r"^\s*([A-Z_][A-Z_0-9]*)\s*:\s*\{")
        # Match a requires field within a line
        _REQUIRES_PATTERN = re.compile(r"requires:\s*'([a-z_]+)'")
        for line in cfg_meta_block.splitlines():
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("//"):
                continue
            key_match = _CFG_META_KEY_PATTERN.match(line)
            if key_match:
                key = key_match.group(1)
                req_match = _REQUIRES_PATTERN.search(line)
                if req_match:
                    cfg_meta_requires[key] = req_match.group(1)
                else:
                    cfg_meta_requires[key] = None
        # Build full cfg_meta with only 'requires' field
        cfg_meta = {k: {"requires": v} for k, v in cfg_meta_requires.items()}

        # Extract CONFIG_CATEGORIES - LINE-BY-LINE APPROACH
        _CONFIG_CATEGORIES_BLOCK_PATTERN = re.compile(
            r"const\s+CONFIG_CATEGORIES\s*=\s*\[(.*?)^\];",
            re.DOTALL | re.MULTILINE,
        )
        match = _CONFIG_CATEGORIES_BLOCK_PATTERN.search(js_content)
        self.assertIsNotNone(match)
        block = match.group(1)
        config_categories = []
        _NAME_RE = re.compile(r"name:\s*'([^']+)'")
        _KEYS_RE = re.compile(r"keys:\s*\[([^\]]+)\]")
        _KEY_STR_RE = re.compile(r"'([^']+)'")
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            name_m = _NAME_RE.search(line)
            keys_m = _KEYS_RE.search(line)
            if not (name_m and keys_m):
                continue
            keys = _KEY_STR_RE.findall(keys_m.group(1))
            config_categories.append(
                {
                    "name": name_m.group(1),
                    "keys": keys,
                    "hideFromDefaults": "hideFromDefaults:true" in line,
                    "fleetOnly": "fleetOnly:true" in line,
                }
            )

        # Extract FLEET_OPS_KEYS_FRONTEND
        _FLEET_OPS_PATTERN = re.compile(
            r"const\s+FLEET_OPS_KEYS_FRONTEND\s*=\s*new\s+Set\s*\(\s*(\[.*?\])\s*\)",
            re.DOTALL | re.MULTILINE,
        )
        match = _FLEET_OPS_PATTERN.search(js_content)
        self.assertIsNotNone(match)
        fleet_ops_str = match.group(1)
        fleet_ops_str = re.sub(r"//.*$", "", fleet_ops_str, flags=re.MULTILINE)
        fleet_ops_str = re.sub(r"\btrue\b", "True", fleet_ops_str)
        fleet_ops_str = re.sub(r"\bfalse\b", "False", fleet_ops_str)
        fleet_ops_str = re.sub(r"\bnull\b", "None", fleet_ops_str)
        fleet_ops = set(ast.literal_eval(fleet_ops_str))

        # Build visible categories per firmware
        expected_visibility = {
            "whatsminer": {
                "Baseline",
                "Profitability Mode",
                "Thermal Limits",
                "Power",
                "Power Limit / Frequency Search",
                "Resilience & Recovery",
            },
            "bixbit": {
                "Baseline",
                "Voltage Settle",
                "V/F Exploration (dynamic state machine)",
                "Perpetual Tune",
                "Profitability Mode",
                "Thermal Limits",
                "Power",
                "Resilience & Recovery",
            },
            "epic": {
                "Baseline",
                "Voltage Settle",
                "V/F Exploration (dynamic state machine)",
                "Per-Chip Tune (Phase 3 iterative loop)",
                "Phase 3b: Stability Polish",
                "Perpetual Tune",
                "Profitability Mode",
                "Thermal Limits",
                "Resilience & Recovery",
            },
            "luxos": {
                "Baseline",
                "Voltage Settle",
                "V/F Exploration (dynamic state machine)",
                "Per-Chip Tune (Phase 3 iterative loop)",
                "Phase 3b: Stability Polish",
                "Perpetual Tune",
                "Profitability Mode",
                "Thermal Limits",
                "Power",
                "Resilience & Recovery",
            },
            "braiins": {
                "Baseline",
                "Profitability Mode",
                "Thermal Limits",
                "Power",
                "Wattage Search",
                "Resilience & Recovery",
            },
        }

        for firmware in MINER_API_REGISTRY:
            with self.subTest(firmware=firmware):
                # Filter categories - MATCHING buildConfigForm logic exactly
                filtered_categories = [
                    cat
                    for cat in config_categories
                    if not cat.get("hideFromDefaults", False)
                    and not all(k in fleet_ops for k in cat.get("keys", []))
                ]

                # Compute visible keys per category
                visible_categories = set()
                for cat in filtered_categories:
                    cat_name = cat["name"]
                    cat_keys = cat.get("keys", [])
                    visible_keys = []
                    for key in cat_keys:
                        if key in cfg_meta:
                            requires = cfg_meta[key].get("requires")
                            if requires is None or platform_caps[firmware].get(requires):
                                visible_keys.append(key)
                    if visible_keys:
                        visible_categories.add(cat_name)

                self.assertEqual(visible_categories, expected_visibility[firmware])
