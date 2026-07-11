"""IP-range network scanner package for the Antminer Chip Tuner.

Discovers ePIC-firmware miners on configured IP ranges, probes for
firmware credentials, and auto-registers found miners with the fleet.
"""

from __future__ import annotations

from .discover import ProbeResult, probe_miner
from .ranges import parse_ip_ranges
from .runner import Scanner

__all__ = ["Scanner", "parse_ip_ranges", "probe_miner", "ProbeResult"]
