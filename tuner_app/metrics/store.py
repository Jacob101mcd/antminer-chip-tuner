"""SQLite-backed MetricsStore — three-tier time-series persistence for the tuner.

Storage layout (single file ``tuning_data/metrics.db``):

  ┌──────────────┐  retention sweep   ┌──────────────────┐  retention sweep   ┌──────────────────┐
  │ samples      │ ─── compact() ───> │ samples_5min     │ ─── compact() ───> │ samples_1hr      │
  │  (raw, ~60s) │                    │ (5-min buckets)  │                    │ (1-hr buckets)   │
  └──────────────┘                    └──────────────────┘                    └──────────────────┘
       N days                              M days                                forever (or N days)

Three retention horizons govern the sweep:

  ``METRICS_RETENTION_RAW_DAYS``    raw rows older than this are downsampled into
                                    ``samples_5min`` and deleted.  Default 90.
  ``METRICS_RETENTION_5MIN_DAYS``   5-min rows older than this are downsampled
                                    into ``samples_1hr`` and deleted.  Default 365.
  ``METRICS_RETENTION_1HR_DAYS``    1-hr rows older than this are deleted.
                                    Default 0 (= keep forever).

Each downsampled bucket carries AVG / MIN / MAX columns per metric so a long-range
query still reflects brief peaks and troughs that a pure-AVG aggregate would smooth
out.  Thread-safety is handled at the application layer: a single
``threading.Lock`` serializes writes; reads are concurrent thanks to WAL.

The store opens a fresh ``sqlite3.Connection`` per call.  This is a minor
overhead but keeps the threading model trivial — SQLite connection objects are
not safe to share across threads by default, and the per-call cost is dwarfed by
the actual I/O on the WAL pages.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# Bucket sizes in seconds for the two downsampled tables.  Constants here so the
# SQL ``GROUP BY (ts / N)`` and the test assertions agree.
BUCKET_5MIN_SEC: int = 300
BUCKET_1HR_SEC: int = 3600

# Metrics columns that get the AVG / MIN / MAX treatment in the downsampled
# tables.  Order matters for the ``_DOWNSAMPLED_COLUMNS`` SQL fragment.
_METRIC_COLS: tuple[str, ...] = (
    "hashrate_ths",
    "power_w",
    "efficiency_jth",
    "temp_max_c",
    "temp_avg_c",
    "fan_speed",
)

_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS samples (
  mac               TEXT NOT NULL,
  ts                REAL NOT NULL,
  hashrate_ths      REAL,
  power_w           REAL,
  efficiency_jth    REAL,
  temp_max_c        REAL,
  temp_avg_c        REAL,
  fan_speed         INTEGER,
  firmware_type     TEXT,
  target_voltage_mv REAL,
  output_voltage_mv REAL,
  PRIMARY KEY (mac, ts)
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS samples_5min (
  mac                  TEXT NOT NULL,
  ts                   REAL NOT NULL,
  hashrate_ths_avg     REAL, hashrate_ths_min     REAL, hashrate_ths_max     REAL,
  power_w_avg          REAL, power_w_min          REAL, power_w_max          REAL,
  efficiency_jth_avg   REAL, efficiency_jth_min   REAL, efficiency_jth_max   REAL,
  temp_max_c_avg       REAL, temp_max_c_min       REAL, temp_max_c_max       REAL,
  temp_avg_c_avg       REAL, temp_avg_c_min       REAL, temp_avg_c_max       REAL,
  fan_speed_avg        REAL, fan_speed_min        INTEGER, fan_speed_max     INTEGER,
  firmware_type        TEXT,
  PRIMARY KEY (mac, ts)
);
CREATE INDEX IF NOT EXISTS idx_5min_ts ON samples_5min(ts);

CREATE TABLE IF NOT EXISTS samples_1hr (
  mac                  TEXT NOT NULL,
  ts                   REAL NOT NULL,
  hashrate_ths_avg     REAL, hashrate_ths_min     REAL, hashrate_ths_max     REAL,
  power_w_avg          REAL, power_w_min          REAL, power_w_max          REAL,
  efficiency_jth_avg   REAL, efficiency_jth_min   REAL, efficiency_jth_max   REAL,
  temp_max_c_avg       REAL, temp_max_c_min       REAL, temp_max_c_max       REAL,
  temp_avg_c_avg       REAL, temp_avg_c_min       REAL, temp_avg_c_max       REAL,
  fan_speed_avg        REAL, fan_speed_min        INTEGER, fan_speed_max     INTEGER,
  firmware_type        TEXT,
  PRIMARY KEY (mac, ts)
);
CREATE INDEX IF NOT EXISTS idx_1hr_ts ON samples_1hr(ts);
"""


class MetricsStore:
    """SQLite-backed time-series store with per-MAC raw + downsampled tables.

    Public methods are thread-safe.  Writes (``record_sample``, ``compact``)
    serialize on ``self._lock``; reads (``query_range``) take the same lock
    only briefly to open a connection — the actual SELECT runs concurrently
    with writes thanks to WAL.

    Lifecycle:

      store = MetricsStore("tuning_data/metrics.db")
      store.start_retention_thread()   # daemon, non-blocking
      ...
      store.record_sample(mac, sample_dict)        # called from monitor cycle
      payload = store.query_range(mac, ts_from, ts_to, metrics, target_points)
      ...
      store.stop()                                 # signals daemon to exit

    The SCHEMA SQL is idempotent (``CREATE TABLE IF NOT EXISTS``) so re-opening
    an existing DB is safe.  A schema upgrade would add new ``ALTER TABLE``
    statements here gated on a ``schema_version`` user-pragma; current schema
    is v1 so no upgrade path is needed yet.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._init_db()

    # ── connection helper ───────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with WAL + NORMAL synchronous.

        ``isolation_level=None`` puts the connection in autocommit mode so
        each ``execute`` is its own transaction — simpler with our application-
        level lock.  WAL allows concurrent reads while a writer is active;
        ``synchronous=NORMAL`` skips the per-commit fsync on the WAL file
        (durability after crash drops from "full" to "best-effort recent
        commits intact").  For metrics this is the right tradeoff — losing the
        last 1-2 samples on power loss is tolerable; the alternative
        (synchronous=FULL) would fsync every monitor cycle.
        """
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        """Create tables + indexes idempotently.  Called from ``__init__``."""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA_SQL)
            finally:
                conn.close()

    # ── B2: write path ──────────────────────────────────────────────────────
    def record_sample(self, mac: str, sample: dict) -> None:
        """INSERT OR REPLACE one raw sample for ``mac``.

        ``sample`` is the dict produced by ``tuner_app.metrics.sampler.build_sample``
        (B7).  Missing keys default to NULL so vendor-specific columns
        (``target_voltage_mv``) round-trip through SQLite as None.

        The (mac, ts) primary key lets the monitor cycle re-record at the same
        epoch second without raising — the second insert wins.  In practice the
        monitor runs at a much coarser cadence than 1 Hz so duplicate ts values
        are rare, but the REPLACE semantics make the writer idempotent.
        """
        ts = float(sample["ts"])
        row = (
            mac,
            ts,
            _coerce_float(sample.get("hashrate_ths")),
            _coerce_float(sample.get("power_w")),
            _coerce_float(sample.get("efficiency_jth")),
            _coerce_float(sample.get("temp_max_c")),
            _coerce_float(sample.get("temp_avg_c")),
            _coerce_int(sample.get("fan_speed")),
            sample.get("firmware_type"),
            _coerce_float(sample.get("target_voltage_mv")),
            _coerce_float(sample.get("output_voltage_mv")),
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO samples ("
                    "  mac, ts,"
                    "  hashrate_ths, power_w, efficiency_jth,"
                    "  temp_max_c, temp_avg_c, fan_speed,"
                    "  firmware_type, target_voltage_mv, output_voltage_mv"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            finally:
                conn.close()

    # ── B4 / B5: read path ──────────────────────────────────────────────────
    def query_range(
        self,
        mac: str,
        ts_from: float,
        ts_to: float,
        metrics: list[str] | tuple[str, ...] | None = None,
        target_points: int = 300,
    ) -> dict:
        """Return AVG/MIN/MAX series for ``mac`` over [ts_from, ts_to].

        The source table is selected by the range width:
          - <= 24 h  → ``samples`` (raw)
          - <= 30 d  → ``samples_5min``
          - >  30 d  → ``samples_1hr``

        Once the source table is chosen, the result is further downsampled into
        at most ``target_points`` buckets via ``GROUP BY (ts / bucket_sec)``.
        For raw input the AVG/MIN/MAX are computed by SQLite over the bucket;
        for already-downsampled input the per-bucket aggregates are
        AVG of input AVGs / MIN of input MINs / MAX of input MAXes.

        For ranges that fit the source granularity exactly (no further
        downsampling), AVG/MIN/MAX collapse to the same single value per
        bucket — the API still returns all three for shape consistency so the
        frontend never needs a special case.

        Unknown ``mac`` → empty series (every metric maps to empty lists).
        """
        if metrics is None:
            metrics = ("hashrate_ths", "power_w", "efficiency_jth", "temp_max_c")
        else:
            metrics = tuple(m for m in metrics if m in _METRIC_COLS)
        ts_from = float(ts_from)
        ts_to = float(ts_to)
        if ts_to <= ts_from or not metrics:
            return {
                "mac": mac,
                "from": ts_from,
                "to": ts_to,
                "bucket_sec": 0,
                "series": {m: {"avg": [], "min": [], "max": []} for m in metrics},
            }

        range_sec = ts_to - ts_from
        # Source table picker — comments at the top of the file explain the cutoffs.
        if range_sec <= 86400.0:
            source = "samples"
            source_bucket_sec = 0  # raw, no inherent bucket
        elif range_sec <= 30 * 86400.0:
            source = "samples_5min"
            source_bucket_sec = BUCKET_5MIN_SEC
        else:
            source = "samples_1hr"
            source_bucket_sec = BUCKET_1HR_SEC

        # Output bucket: at most target_points buckets across the range, never
        # smaller than the source granularity.
        bucket_sec = max(int(math.ceil(range_sec / max(target_points, 1))), source_bucket_sec or 1)

        if source == "samples":
            return self._query_raw(mac, ts_from, ts_to, metrics, bucket_sec)
        return self._query_downsampled(mac, ts_from, ts_to, metrics, bucket_sec, source)

    def _query_raw(
        self,
        mac: str,
        ts_from: float,
        ts_to: float,
        metrics: tuple[str, ...],
        bucket_sec: int,
    ) -> dict:
        """Read from ``samples`` and aggregate by bucket using AVG/MIN/MAX."""
        # Build per-metric SELECT fragments.  ``fan_speed`` is INTEGER but its
        # AVG should still be a float (it is an average); MIN/MAX preserve the
        # integer nature, but SQLite returns the column type unchanged so they
        # round-trip as ints without explicit casts.
        select_cols = ["CAST(ts / ? AS INTEGER) AS bucket"]
        for m in metrics:
            select_cols.append(f"AVG({m}) AS {m}_avg")
            select_cols.append(f"MIN({m}) AS {m}_min")
            select_cols.append(f"MAX({m}) AS {m}_max")
        sql = (
            f"SELECT {', '.join(select_cols)} "
            f"FROM samples "
            f"WHERE mac = ? AND ts >= ? AND ts < ? "
            f"GROUP BY bucket "
            f"ORDER BY bucket"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (bucket_sec, mac, ts_from, ts_to)).fetchall()
            finally:
                conn.close()

        series: dict[str, dict[str, list]] = {m: {"avg": [], "min": [], "max": []} for m in metrics}
        for row in rows:
            bucket = row[0]
            ts_bucket = float(bucket * bucket_sec)
            # Each metric occupies 3 columns (avg, min, max) in the same order.
            for i, m in enumerate(metrics):
                avg_v = row[1 + i * 3]
                min_v = row[2 + i * 3]
                max_v = row[3 + i * 3]
                if avg_v is not None:
                    series[m]["avg"].append([ts_bucket, avg_v])
                if min_v is not None:
                    series[m]["min"].append([ts_bucket, min_v])
                if max_v is not None:
                    series[m]["max"].append([ts_bucket, max_v])
        return {
            "mac": mac,
            "from": ts_from,
            "to": ts_to,
            "bucket_sec": bucket_sec,
            "series": series,
        }

    def _query_downsampled(
        self,
        mac: str,
        ts_from: float,
        ts_to: float,
        metrics: tuple[str, ...],
        bucket_sec: int,
        source: str,
    ) -> dict:
        """Read from ``samples_5min`` / ``samples_1hr`` and aggregate by bucket.

        The input rows already carry per-bucket AVG/MIN/MAX columns.  For an
        output bucket spanning M input buckets, the output stat is:
          - AVG = AVG of input AVGs (slightly off if input buckets are not
                  uniform but close enough at our scales)
          - MIN = MIN of input MINs
          - MAX = MAX of input MAXes
        """
        select_cols = ["CAST(ts / ? AS INTEGER) AS bucket"]
        for m in metrics:
            select_cols.append(f"AVG({m}_avg) AS {m}_avg")
            select_cols.append(f"MIN({m}_min) AS {m}_min")
            select_cols.append(f"MAX({m}_max) AS {m}_max")
        sql = (
            f"SELECT {', '.join(select_cols)} "
            f"FROM {source} "
            f"WHERE mac = ? AND ts >= ? AND ts < ? "
            f"GROUP BY bucket "
            f"ORDER BY bucket"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (bucket_sec, mac, ts_from, ts_to)).fetchall()
            finally:
                conn.close()

        series: dict[str, dict[str, list]] = {m: {"avg": [], "min": [], "max": []} for m in metrics}
        for row in rows:
            bucket = row[0]
            ts_bucket = float(bucket * bucket_sec)
            for i, m in enumerate(metrics):
                avg_v = row[1 + i * 3]
                min_v = row[2 + i * 3]
                max_v = row[3 + i * 3]
                if avg_v is not None:
                    series[m]["avg"].append([ts_bucket, avg_v])
                if min_v is not None:
                    series[m]["min"].append([ts_bucket, min_v])
                if max_v is not None:
                    series[m]["max"].append([ts_bucket, max_v])
        return {
            "mac": mac,
            "from": ts_from,
            "to": ts_to,
            "bucket_sec": bucket_sec,
            "series": series,
        }

    # ── B6: retention compaction ────────────────────────────────────────────
    def compact(
        self,
        now: float | None = None,
        retention_raw_days: int = 90,
        retention_5min_days: int = 365,
        retention_1hr_days: int = 0,
    ) -> dict:
        """Roll old data into coarser tables and prune past 1-hr horizon.

        Returns a small dict with row counts moved/deleted at each tier — handy
        for tests and for a future ``/tuner/metrics/compact`` admin endpoint.

        Steps (each gated on the matching retention threshold):

          1. samples → samples_5min
             Rows older than ``now - retention_raw_days * 86400`` are bucketed
             into 5-min slots, AVG/MIN/MAX computed, INSERT OR REPLACE into
             samples_5min, then deleted from samples.

          2. samples_5min → samples_1hr
             Rows older than ``now - retention_5min_days * 86400`` are bucketed
             into 1-hr slots (AVG of AVGs / MIN of MINs / MAX of MAXes),
             INSERT OR REPLACE into samples_1hr, then deleted from samples_5min.

          3. samples_1hr prune
             If ``retention_1hr_days > 0``, rows older than the corresponding
             cutoff are deleted.  Zero means "keep forever".

        ``now`` defaults to ``time.time()``.  Override for deterministic tests.
        """
        if now is None:
            now = time.time()
        cutoff_raw = now - (max(retention_raw_days, 0) * 86400.0)
        cutoff_5min = now - (max(retention_5min_days, 0) * 86400.0)
        cutoff_1hr = now - (max(retention_1hr_days, 0) * 86400.0)

        result = {"raw_to_5min": 0, "5min_to_1hr": 0, "1hr_pruned": 0}

        with self._lock:
            conn = self._connect()
            try:
                # ── 1. samples → samples_5min ──
                if retention_raw_days > 0:
                    moved = self._roll_samples_to_5min(conn, cutoff_raw)
                    result["raw_to_5min"] = moved

                # ── 2. samples_5min → samples_1hr ──
                if retention_5min_days > 0:
                    moved = self._roll_5min_to_1hr(conn, cutoff_5min)
                    result["5min_to_1hr"] = moved

                # ── 3. prune samples_1hr ──
                if retention_1hr_days > 0:
                    cur = conn.execute("DELETE FROM samples_1hr WHERE ts < ?", (cutoff_1hr,))
                    result["1hr_pruned"] = cur.rowcount or 0
            finally:
                conn.close()

        return result

    @staticmethod
    def _roll_samples_to_5min(conn: sqlite3.Connection, cutoff: float) -> int:
        """Aggregate raw samples older than ``cutoff`` into samples_5min."""
        # Build the aggregating SELECT: for each (mac, 5-min bucket) compute
        # AVG / MIN / MAX of every metric column.  ``firmware_type`` doesn't
        # have a meaningful aggregate — pick any value via MAX (alphabetical
        # last), which keeps the column non-null for buckets with mixed
        # firmware types (the operator-side flash boundary scenario).
        agg_cols = []
        for m in _METRIC_COLS:
            agg_cols.append(f"AVG({m})")
            agg_cols.append(f"MIN({m})")
            agg_cols.append(f"MAX({m})")
        select_sql = (
            f"SELECT mac, "
            f"CAST(ts / {BUCKET_5MIN_SEC} AS INTEGER) * {BUCKET_5MIN_SEC} AS bucket_ts, "
            f"{', '.join(agg_cols)}, "
            f"MAX(firmware_type) AS firmware_type "
            f"FROM samples WHERE ts < ? "
            f"GROUP BY mac, bucket_ts"
        )
        rows = conn.execute(select_sql, (cutoff,)).fetchall()
        if not rows:
            return 0
        insert_sql = (
            "INSERT OR REPLACE INTO samples_5min ("
            "  mac, ts,"
            "  hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max,"
            "  power_w_avg, power_w_min, power_w_max,"
            "  efficiency_jth_avg, efficiency_jth_min, efficiency_jth_max,"
            "  temp_max_c_avg, temp_max_c_min, temp_max_c_max,"
            "  temp_avg_c_avg, temp_avg_c_min, temp_avg_c_max,"
            "  fan_speed_avg, fan_speed_min, fan_speed_max,"
            "  firmware_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.executemany(insert_sql, rows)
        cur = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0

    @staticmethod
    def _roll_5min_to_1hr(conn: sqlite3.Connection, cutoff: float) -> int:
        """Aggregate samples_5min older than ``cutoff`` into samples_1hr."""
        agg_cols = []
        for m in _METRIC_COLS:
            agg_cols.append(f"AVG({m}_avg)")
            agg_cols.append(f"MIN({m}_min)")
            agg_cols.append(f"MAX({m}_max)")
        select_sql = (
            f"SELECT mac, "
            f"CAST(ts / {BUCKET_1HR_SEC} AS INTEGER) * {BUCKET_1HR_SEC} AS bucket_ts, "
            f"{', '.join(agg_cols)}, "
            f"MAX(firmware_type) AS firmware_type "
            f"FROM samples_5min WHERE ts < ? "
            f"GROUP BY mac, bucket_ts"
        )
        rows = conn.execute(select_sql, (cutoff,)).fetchall()
        if not rows:
            return 0
        insert_sql = (
            "INSERT OR REPLACE INTO samples_1hr ("
            "  mac, ts,"
            "  hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max,"
            "  power_w_avg, power_w_min, power_w_max,"
            "  efficiency_jth_avg, efficiency_jth_min, efficiency_jth_max,"
            "  temp_max_c_avg, temp_max_c_min, temp_max_c_max,"
            "  temp_avg_c_avg, temp_avg_c_min, temp_avg_c_max,"
            "  fan_speed_avg, fan_speed_min, fan_speed_max,"
            "  firmware_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.executemany(insert_sql, rows)
        cur = conn.execute("DELETE FROM samples_5min WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0

    # ── B10: retention thread ───────────────────────────────────────────────
    def start_retention_thread(self) -> None:
        """Start a daemon thread that periodically calls ``compact()``.

        Cadence is read from ``state.CONFIG["fleet_ops"]["METRICS_COMPACT_INTERVAL_HOURS"]``
        on each iteration, so an operator config change takes effect on the
        next wake.  Idempotent — a running thread is left in place.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="metrics-retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the retention daemon to exit (non-blocking)."""
        self._stop_event.set()

    def _run(self) -> None:
        """Daemon main loop: sleep, then compact, then sleep again."""
        # Lazy import to avoid import-time circulars (state imports nothing
        # from metrics, but the retention thread reads CONFIG which is
        # populated by main.apply_defaults before the thread is started).
        from tuner_app import state

        while not self._stop_event.is_set():
            try:
                fo = state.CONFIG.get("fleet_ops", {})
                interval_h = float(fo.get("METRICS_COMPACT_INTERVAL_HOURS", 6) or 6)
                retention_raw = int(fo.get("METRICS_RETENTION_RAW_DAYS", 90) or 90)
                retention_5min = int(fo.get("METRICS_RETENTION_5MIN_DAYS", 365) or 365)
                retention_1hr = int(fo.get("METRICS_RETENTION_1HR_DAYS", 0) or 0)
            except Exception:
                interval_h = 6.0
                retention_raw, retention_5min, retention_1hr = 90, 365, 0

            try:
                self.compact(
                    retention_raw_days=retention_raw,
                    retention_5min_days=retention_5min,
                    retention_1hr_days=retention_1hr,
                )
            except Exception as exc:
                logger.exception("metrics compact failed: %s", exc)

            wait_sec = max(interval_h, 0.0) * 3600.0
            if wait_sec <= 0:
                wait_sec = 3600.0
            if self._stop_event.wait(timeout=wait_sec):
                return


# ── coercion helpers ───────────────────────────────────────────────────────
def _coerce_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
