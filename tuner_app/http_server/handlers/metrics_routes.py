"""GET /tuner/metrics/<mac-dashes> — multi-timeframe statistics read API (Phase B / B11).

Wire shape:

  GET /tuner/metrics/<mac>?range=24h&metrics=hashrate_ths,power_w
  GET /tuner/metrics/<mac>?range=custom&from=<epoch>&to=<epoch>

Query parameters:

  range          one of {1h, 24h, 7d, 30d, custom}.  Defaults to ``24h``.
  metrics        comma-separated metric names.  Defaults to
                 ``hashrate_ths,power_w,efficiency_jth,temp_max_c``.
  from / to      epoch seconds (required when ``range=custom``).
  target_points  optional integer ceiling on the number of buckets per series.
                 Defaults to 300.

Response (always HTTP 200, even for unknown MACs — empty series in that case):

  {
    "mac": "aa:bb:cc:dd:ee:ff",
    "from": <float>, "to": <float>,
    "bucket_sec": <int>,
    "series": {
      "hashrate_ths": {"avg": [[ts, v], ...], "min": [...], "max": [...]},
      "power_w":      {"avg": [...], "min": [...], "max": [...]},
      ...
    }
  }

Auth-gated (not in AUTH_EXEMPT_*); reuses the same MAC-path-segment validator
that powers ``/tuner/live/<mac>``.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

from tuner_app import state

from ._mac_helpers import parse_mac_path_segment

# Range-string → (seconds, target_points) defaults.  ``target_points`` here
# is a sensible cap for the dashboard's chart width; callers may override
# with the ``target_points`` query parameter.
_RANGE_PRESETS: dict[str, tuple[int, int]] = {
    "1h": (3600, 120),
    "24h": (86400, 288),
    "7d": (7 * 86400, 504),
    "30d": (30 * 86400, 720),
}


def metrics(handler, body) -> None:
    """Handle GET /tuner/metrics/<mac>?range=<r>&metrics=...&from=...&to=..."""
    parts = urlsplit(handler.path)
    raw = parts.path.split("/tuner/metrics/", 1)[1]
    mac = parse_mac_path_segment(handler, raw, "/tuner/metrics/")
    if mac is None:
        return

    qs = parse_qs(parts.query)
    range_key = (qs.get("range", ["24h"])[0] or "24h").strip().lower()
    metrics_arg = qs.get("metrics", [""])[0].strip()
    if metrics_arg:
        metrics_list: list[str] | None = [m.strip() for m in metrics_arg.split(",") if m.strip()]
    else:
        metrics_list = None  # store will pick the default set

    target_points: int
    raw_target = qs.get("target_points", ["300"])[0]
    try:
        target_points = max(1, int(raw_target))
    except (TypeError, ValueError):
        target_points = 300

    if range_key == "custom":
        from_arg = qs.get("from", [None])[0]
        to_arg = qs.get("to", [None])[0]
        if not from_arg or not to_arg:
            handler._json_response(
                {"ok": False, "error": "range=custom requires 'from' and 'to' query params"},
                status=400,
            )
            return
        try:
            ts_from = float(from_arg)
            ts_to = float(to_arg)
        except (TypeError, ValueError):
            handler._json_response(
                {"ok": False, "error": "'from' and 'to' must be epoch-second numbers"},
                status=400,
            )
            return
    elif range_key in _RANGE_PRESETS:
        window_sec, default_points = _RANGE_PRESETS[range_key]
        if "target_points" not in qs:
            target_points = default_points
        ts_to = time.time()
        ts_from = ts_to - float(window_sec)
    else:
        handler._json_response(
            {
                "ok": False,
                "error": (
                    f"unknown range {range_key!r}; expected one of "
                    f"{sorted(_RANGE_PRESETS)} or 'custom'"
                ),
            },
            status=400,
        )
        return

    store = state.metrics_store
    if store is None:
        # Boot order should always have wired this up by the time the HTTP
        # server is serving requests, but defense-in-depth keeps the
        # endpoint useful in tests and degraded environments.
        handler._json_response(
            {
                "mac": mac,
                "from": ts_from,
                "to": ts_to,
                "bucket_sec": 0,
                "series": {},
            }
        )
        return

    payload = store.query_range(
        mac,
        ts_from,
        ts_to,
        metrics=metrics_list,
        target_points=target_points,
    )
    handler._json_response(payload)
