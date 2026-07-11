"""Central Python logging configuration for the tuner_app process.

Call configure_logging() once at process startup (before apply_defaults) to
route module-level logging.getLogger(__name__) calls through a consistent
format. The per-miner JSONL logger (tuner_app.tuning_engine.logging_.log) is
independent and unaffected by this module.
"""

from __future__ import annotations

import logging


def configure_logging() -> None:
    """Set up root Python logger with a human-readable format.

    Safe to call multiple times — subsequent calls are no-ops because
    basicConfig() is idempotent when handlers are already attached.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(levelname)s %(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
