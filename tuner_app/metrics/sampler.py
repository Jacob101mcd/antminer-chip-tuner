"""build_sample(engine) — vendor-neutral metrics snapshot from engine state.

The monitor cycle calls this once per pass right after ``engine._update_live_data()``
to capture the most recent summary + per-chip temps in a flat dict that
``MetricsStore.record_sample`` can consume.

All vendor-specific dict-key access is contained here; the store layer never
sees vendor differences.  Vendor-specific fields that don't exist on the
current platform map to ``None``.
"""

from __future__ import annotations

import time


def build_sample(engine) -> dict:
    """Build a single metrics sample dict from ``engine.last_*`` state.

    Returns a dict with these keys (any may be ``None`` when the underlying
    vendor / firmware doesn't surface that field):

      - ``ts``                 epoch seconds, ``time.time()`` at call site
      - ``hashrate_ths``       MinerSummary.hashrate_ths
      - ``power_w``            MinerSummary.power_w
      - ``efficiency_jth``     derived: power_w / hashrate_ths (None if no hashrate)
      - ``temp_max_c``         MAX of non-zero chip temps across boards
      - ``temp_avg_c``         mean of non-zero chip temps across boards
      - ``fan_speed``          MinerSummary.fan_speed (% on ePIC, RPM elsewhere)
      - ``firmware_type``      ``engine.firmware_type``
      - ``target_voltage_mv``  MinerSummary.target_voltage_mv (ePIC only)
      - ``output_voltage_mv``  MinerSummary.output_voltage_mv (when available)

    The function never raises — a missing/None ``last_summary`` returns a
    minimal sample with only ``ts`` and ``firmware_type`` set.  The monitor
    cycle that calls this is wrapped in its own try/except, but defense-in-depth
    keeps the metrics path independent of engine warm-up state.
    """
    sample: dict = {
        "ts": time.time(),
        "firmware_type": getattr(engine, "firmware_type", None),
    }

    summary = getattr(engine, "last_summary", None)
    if summary is None:
        return sample

    sample["hashrate_ths"] = getattr(summary, "hashrate_ths", None)
    sample["power_w"] = getattr(summary, "power_w", None)
    sample["fan_speed"] = getattr(summary, "fan_speed", None)
    sample["target_voltage_mv"] = getattr(summary, "target_voltage_mv", None)
    sample["output_voltage_mv"] = getattr(summary, "output_voltage_mv", None)

    # Derived efficiency — only when both inputs are present and hashrate > 0.
    hr = sample["hashrate_ths"]
    pw = sample["power_w"]
    if isinstance(hr, (int, float)) and isinstance(pw, (int, float)) and hr > 0:
        sample["efficiency_jth"] = pw / hr
    else:
        sample["efficiency_jth"] = None

    # Per-chip temperatures: ``engine.last_chip_temps`` is a list of
    # BoardSummary instances, each with a ``chip_temps_c`` list.  Filter zeros
    # (firmware reports 0 for chips it failed to read) — matches the same
    # filter applied in tuner_app/tuning_engine/status.py:_avg_chip_temps.
    boards = getattr(engine, "last_chip_temps", None) or []
    flat: list[float] = []
    for b in boards:
        chip_temps = getattr(b, "chip_temps_c", None) or []
        for t in chip_temps:
            if isinstance(t, (int, float)) and t > 0:
                flat.append(float(t))
    if flat:
        sample["temp_max_c"] = max(flat)
        sample["temp_avg_c"] = sum(flat) / len(flat)
    else:
        sample["temp_max_c"] = None
        sample["temp_avg_c"] = None

    return sample
