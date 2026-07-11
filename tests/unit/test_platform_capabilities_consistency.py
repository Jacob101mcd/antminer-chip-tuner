from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tuner_app.miner.registry import MINER_API_REGISTRY

_PLATFORM_CAPS_PATTERN = re.compile(
    r"const\s+PLATFORM_CAPABILITIES\s*=\s*({.*?^});",
    re.DOTALL | re.MULTILINE,
)


class _FakeConfig:
    def __init__(self, firmware_type):
        self.firmware_type = firmware_type

    def __getitem__(self, key):
        if key == "API_PORT":
            return 4028
        elif key == "PASSWORD":
            return "letmein"
        else:
            raise KeyError(key)

    _GET_DEFAULTS = {
        "LUXOS_MIN_CONN_INTERVAL_SEC": 1.0,
        "LUXOS_OFFLINE_BACKOFF_SEC": 30.0,
        "BRAIINS_USERNAME": "root",
    }

    def get(self, key, default=None):
        return self._GET_DEFAULTS.get(key, default)

    def __contains__(self, key):
        return key in ("API_PORT", "PASSWORD")


class TestPlatformCapabilitiesConsistency(unittest.TestCase):
    def setUp(self):
        js_file = Path(__file__).parent.parent.parent / "tuner_app" / "static" / "js" / "main.js"
        self.js_content = js_file.read_text(encoding="utf-8")

    def test_platform_capabilities_const_exists_in_main_js(self):
        match = _PLATFORM_CAPS_PATTERN.search(self.js_content)
        self.assertIsNotNone(
            match,
            "PLATFORM_CAPABILITIES const not found in tuner_app/static/js/main.js",
        )

    def test_platform_capabilities_covers_all_registry_firmwares(self):
        match = _PLATFORM_CAPS_PATTERN.search(self.js_content)
        self.assertIsNotNone(
            match,
            "PLATFORM_CAPABILITIES const not found in tuner_app/static/js/main.js",
        )

        js_obj_str = match.group(1)
        # Strip comments
        js_obj_str = re.sub(r"//.*$", "", js_obj_str, flags=re.MULTILINE)
        # Replace true/false/null
        js_obj_str = re.sub(r"\btrue\b", "True", js_obj_str)
        js_obj_str = re.sub(r"\bfalse\b", "False", js_obj_str)
        js_obj_str = re.sub(r"\bnull\b", "None", js_obj_str)
        # Quote bare keys
        js_obj_str = re.sub(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\g<key>":', js_obj_str)
        # Remove trailing commas
        js_obj_str = re.sub(r",(\s*[}\]])", r"\1", js_obj_str)
        js_dict = ast.literal_eval(js_obj_str)

        js_keys = set(js_dict.keys())
        registry_keys = set(MINER_API_REGISTRY.keys())
        self.assertEqual(
            js_keys, registry_keys, "Keys mismatch PLATFORM_CAPABILITIES vs MINER_API_REGISTRY"
        )

    def test_each_firmware_flags_match_api_class_methods(self):
        match = _PLATFORM_CAPS_PATTERN.search(self.js_content)
        self.assertIsNotNone(
            match,
            "PLATFORM_CAPABILITIES const not found in tuner_app/static/js/main.js",
        )

        js_obj_str = match.group(1)
        # Strip comments
        js_obj_str = re.sub(r"//.*$", "", js_obj_str, flags=re.MULTILINE)
        # Replace true/false/null
        js_obj_str = re.sub(r"\btrue\b", "True", js_obj_str)
        js_obj_str = re.sub(r"\bfalse\b", "False", js_obj_str)
        js_obj_str = re.sub(r"\bnull\b", "None", js_obj_str)
        # Quote bare keys
        js_obj_str = re.sub(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\g<key>":', js_obj_str)
        # Remove trailing commas
        js_obj_str = re.sub(r",(\s*[}\]])", r"\1", js_obj_str)
        js_dict = ast.literal_eval(js_obj_str)

        for fw in MINER_API_REGISTRY:
            with self.subTest(firmware=fw):
                api = MINER_API_REGISTRY[fw]("127.0.0.1", _FakeConfig(fw))
                strategy = api.tuning_strategy()
                expected = {
                    "supports_per_chip_tuning": api.supports_per_chip_tuning(),
                    "has_external_power_limit": api.has_external_power_limit(),
                    "has_capabilities_endpoint": api.has_capabilities_endpoint(),
                    "has_internal_perpetual_tune": api.has_internal_perpetual_tune(),
                    "voltage_chip_tune_strategy": strategy == "voltage_chip_tune",
                    "power_limit_freq_search_strategy": strategy == "power_limit_freq_search",
                    "wattage_search_strategy": strategy == "wattage_search",
                }
                self.assertEqual(js_dict[fw], expected)
