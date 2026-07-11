from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tuner_app.miner.registry import MINER_API_REGISTRY
from tuner_app.miner.types import HardwareTopology

# Minimal fake config that satisfies all four factory callables.
# Supports dict subscripting (API_PORT, PASSWORD) and .get() (BRAIINS_USERNAME).
_FAKE_CONFIG = {"API_PORT": 4028, "PASSWORD": "letmein", "BRAIINS_USERNAME": "root"}

_REGISTRY_PARAMS = list(MINER_API_REGISTRY.items())
_REGISTRY_IDS = list(MINER_API_REGISTRY.keys())


@pytest.mark.parametrize("firmware_type_str,factory", _REGISTRY_PARAMS, ids=_REGISTRY_IDS)
def test_capability_methods_return_bool(firmware_type_str, factory):
    # instantiate via factory -- capability methods are pure (no network)
    instance = factory("1.2.3.4", _FAKE_CONFIG)
    # Assert all 4 capability methods exist and return bool
    for method_name in (
        "supports_per_chip_tuning",
        "has_external_power_limit",
        "has_capabilities_endpoint",
        "has_internal_perpetual_tune",
    ):
        assert hasattr(instance, method_name), f"{firmware_type_str} API missing {method_name}"
        result = getattr(instance, method_name)()
        assert isinstance(result, bool), (
            f"{firmware_type_str}.{method_name}() returned {type(result).__name__}, expected bool"
        )


@pytest.mark.parametrize("firmware_type_str,factory", _REGISTRY_PARAMS, ids=_REGISTRY_IDS)
def test_hardware_topology_returns_valid_topology(firmware_type_str, factory):
    instance = factory("1.2.3.4", _FAKE_CONFIG)
    if firmware_type_str == "epic":
        # Pre-populate the capabilities cache so hardware_topology() doesn't hit the network
        instance._capabilities_cache = {
            "Psu Info": {"Min Vout": 11877, "Max Vout": 15182},
            "Performance Estimator": {"Chip Count": 108},
            "Max HBs": 3,
        }
        result = instance.hardware_topology()
    elif firmware_type_str == "bixbit":
        # BixbitMinerAPI.hardware_topology() is a constant -- no network
        result = instance.hardware_topology()
    elif firmware_type_str == "luxos":
        # Mock the transport to avoid TCP calls
        instance._transport = MagicMock()
        instance._transport.send_cmd.side_effect = [
            {"LIMITS": [{"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}]},
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
        ]
        result = instance.hardware_topology()
    elif firmware_type_str == "braiins":
        # hardware_topology() now queries _get_json; mock it to avoid network
        instance._get_json = MagicMock(return_value={})
        result = instance.hardware_topology()
    else:
        pytest.skip(f"No mock recipe for firmware_type={firmware_type_str!r}")

    assert isinstance(result, HardwareTopology)
    assert result.num_boards is not None
    assert result.chips_per_board is not None
    assert result.psu_min_mv is not None
    assert result.psu_max_mv is not None
    assert result.num_boards >= 1


_EXPECTED_TUNING_STRATEGY = {
    "epic": "voltage_chip_tune",
    "bixbit": "voltage_chip_tune",
    "luxos": "voltage_chip_tune",
    "braiins": "wattage_search",
    "whatsminer": "power_limit_freq_search",
}


@pytest.mark.parametrize("firmware_type_str,factory", _REGISTRY_PARAMS, ids=_REGISTRY_IDS)
def test_tuning_strategy_per_vendor(firmware_type_str, factory):
    instance = factory("1.2.3.4", _FAKE_CONFIG)
    assert instance.tuning_strategy() == _EXPECTED_TUNING_STRATEGY[firmware_type_str]
