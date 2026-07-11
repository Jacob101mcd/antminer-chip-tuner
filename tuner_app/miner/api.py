"""Backward-compatibility shim for MinerAPI import.

This module re-exports MinerAPI from tuner_app.miner.epic to maintain
compatibility with existing code that imports from tuner_app.miner.api.
"""

from tuner_app.miner.epic import EpicMinerAPI as MinerAPI

__all__ = ["MinerAPI"]
