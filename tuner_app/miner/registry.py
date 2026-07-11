"""Vendor registry: maps firmware_type strings to concrete MinerAPI subclasses
and MinerSummary parser callables.

Adding a new vendor = one entry in each registry dict.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tuner_app.miner.base import MinerAPI
from tuner_app.miner.bixbit import BixbitMinerAPI
from tuner_app.miner.braiins import BraiinsMinerAPI
from tuner_app.miner.epic import EpicMinerAPI
from tuner_app.miner.luxos import LuxosMinerAPI
from tuner_app.miner.types import MinerSummary
from tuner_app.miner.whatsminer import WhatsminerMinerAPI


def _make_epic(ip: str, config: Any) -> MinerAPI:
    return EpicMinerAPI(ip, config["API_PORT"], config["PASSWORD"])


def _make_bixbit(ip: str, config: Any) -> MinerAPI:
    return BixbitMinerAPI(ip, config["API_PORT"], config["PASSWORD"])


def _make_luxos(ip: str, config: Any) -> MinerAPI:
    return LuxosMinerAPI(
        ip,
        config["API_PORT"],
        config["PASSWORD"],
        min_conn_interval_sec=config.get("LUXOS_MIN_CONN_INTERVAL_SEC", 1.0),
        offline_backoff_sec=config.get("LUXOS_OFFLINE_BACKOFF_SEC", 30.0),
    )


def _make_braiins(ip: str, config: Any) -> MinerAPI:
    api = BraiinsMinerAPI(ip, config["API_PORT"], config["PASSWORD"])
    api.username = config.get("BRAIINS_USERNAME", "root")
    return api


def _make_whatsminer(ip: str, config: Any) -> MinerAPI:
    return WhatsminerMinerAPI(ip, config["API_PORT"], config["PASSWORD"])


# Registry of firmware_type -> concrete MinerAPI factory callable.
# Each factory takes (ip: str, config) -> MinerAPI.
# Adding a new vendor = one factory function + one line here.
MINER_API_REGISTRY: dict[str, Callable[[str, Any], MinerAPI]] = {
    "epic": _make_epic,
    "bixbit": _make_bixbit,
    "luxos": _make_luxos,
    "braiins": _make_braiins,
    "whatsminer": _make_whatsminer,
}

# Registry of firmware_type -> summary-dict parser.
# Note: from_braiins takes (raw_details, raw_stats, raw_cooling) so the type
# is dict[str, Any] rather than dict[str, Callable[[dict], MinerSummary]].
SUMMARY_PARSER_REGISTRY: dict[str, Any] = {
    "epic": MinerSummary.from_epic,
    "bixbit": MinerSummary.from_bixbit,
    "luxos": MinerSummary.from_luxos,
    "braiins": MinerSummary.from_braiins,
    "whatsminer": MinerSummary.from_whatsminer,
}


def supported_firmware_types() -> list[str]:
    """Sorted list of firmware_type strings the system recognizes.

    Used by config validation and the dashboard firmware-type dropdown.
    """
    return sorted(MINER_API_REGISTRY.keys())
