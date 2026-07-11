"""Unit tests for tuner_app.metrics.store.MetricsStore.

Covers Phase B units B1-B6:
  - B1: schema + WAL pragma confirmed
  - B2: record_sample INSERT OR REPLACE on duplicate (mac, ts)
  - B3: concurrent writes from multiple threads complete without OperationalError
  - B4: query_range raw passthrough — same metric in/out under no downsampling
  - B5: query_range downsampling produces correct AVG/MIN/MAX over manual buckets
  - B6: compact() rolls samples → samples_5min → samples_1hr; honors 0=forever
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from unittest import TestCase

from tuner_app.metrics.store import (
    BUCKET_1HR_SEC,
    BUCKET_5MIN_SEC,
    MetricsStore,
)


def _sample(ts: float, **overrides) -> dict:
    """Build a minimal sample dict with sane defaults; overrides win."""
    base = {
        "ts": ts,
        "hashrate_ths": 200.0,
        "power_w": 4200.0,
        "efficiency_jth": 21.0,
        "temp_max_c": 72.0,
        "temp_avg_c": 60.0,
        "fan_speed": 50,
        "firmware_type": "epic",
        "target_voltage_mv": 14630.0,
        "output_voltage_mv": 14600.0,
    }
    base.update(overrides)
    return base


class TestMetricsStoreSchema(TestCase):
    """B1 — schema + WAL."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)

    def tearDown(self) -> None:
        self.store.stop()
        self.tmpdir.cleanup()

    def test_schema_creates_three_tables(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        names = {r[0] for r in rows}
        self.assertIn("samples", names)
        self.assertIn("samples_5min", names)
        self.assertIn("samples_1hr", names)

    def test_samples_primary_key_is_mac_ts(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("PRAGMA table_info(samples)").fetchall()
        finally:
            conn.close()
        # rows: (cid, name, type, notnull, dflt_value, pk)
        pk_cols = sorted([r[1] for r in rows if r[5]])
        self.assertEqual(pk_cols, ["mac", "ts"])

    def test_samples_index_on_ts_exists(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='samples'"
            ).fetchall()
        finally:
            conn.close()
        names = {r[0] for r in rows}
        self.assertIn("idx_samples_ts", names)

    def test_wal_journal_mode_enabled(self) -> None:
        # Use the store's connection helper so we exercise the same PRAGMA path
        # the writer/reader paths use.
        conn = self.store._connect()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        # SQLite returns the mode lowercased.
        self.assertEqual(mode, "wal")

    def test_init_db_is_idempotent(self) -> None:
        # Re-opening an existing DB must not raise (CREATE TABLE IF NOT EXISTS).
        store2 = MetricsStore(self.db_path)
        self.assertIsNotNone(store2)


class TestRecordSample(TestCase):
    """B2 — record_sample INSERT OR REPLACE."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)

    def tearDown(self) -> None:
        self.store.stop()
        self.tmpdir.cleanup()

    def _count_rows(self, table: str = "samples") -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_records_a_single_sample(self) -> None:
        self.store.record_sample("aa:bb:cc:dd:ee:ff", _sample(1000.0))
        self.assertEqual(self._count_rows(), 1)

    def test_duplicate_mac_ts_replaces_in_place(self) -> None:
        mac = "aa:bb:cc:dd:ee:ff"
        self.store.record_sample(mac, _sample(1000.0, hashrate_ths=200.0))
        self.store.record_sample(mac, _sample(1000.0, hashrate_ths=205.0))
        # PK collision should REPLACE, not INSERT — count stays at 1.
        self.assertEqual(self._count_rows(), 1)
        # And the second value wins.
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT hashrate_ths FROM samples WHERE mac=? AND ts=?",
                (mac, 1000.0),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 205.0)

    def test_distinct_macs_keep_separate_rows(self) -> None:
        self.store.record_sample("aa:bb:cc:dd:ee:01", _sample(1000.0))
        self.store.record_sample("aa:bb:cc:dd:ee:02", _sample(1000.0))
        self.assertEqual(self._count_rows(), 2)

    def test_missing_optional_fields_become_null(self) -> None:
        sparse = {"ts": 1000.0, "hashrate_ths": 200.0}  # nothing else
        self.store.record_sample("aa:bb:cc:dd:ee:ff", sparse)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT power_w, target_voltage_mv, firmware_type FROM samples"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])

    def test_non_numeric_value_coerces_to_null(self) -> None:
        # Sampler should never produce these, but defensive coercion keeps the
        # writer crash-free if e.g. summary.power_w is "n/a" string.
        self.store.record_sample(
            "aa:bb:cc:dd:ee:ff",
            _sample(1000.0, power_w="not-a-number"),
        )
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT power_w FROM samples").fetchone()
        finally:
            conn.close()
        self.assertIsNone(row[0])


class TestConcurrentWrites(TestCase):
    """B3 — concurrent writes don't raise OperationalError."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)

    def tearDown(self) -> None:
        self.store.stop()
        self.tmpdir.cleanup()

    def test_four_threads_fifty_inserts_each_no_lock_errors(self) -> None:
        errors: list[Exception] = []
        # Each thread uses a distinct MAC so the inserts never collide on PK,
        # which lets us verify the application-level lock provides the
        # serialization SQLite needs (avoiding the "database is locked" error).
        macs = [
            "aa:bb:cc:dd:00:01",
            "aa:bb:cc:dd:00:02",
            "aa:bb:cc:dd:00:03",
            "aa:bb:cc:dd:00:04",
        ]

        def writer(mac: str, base_ts: float) -> None:
            try:
                for i in range(50):
                    self.store.record_sample(mac, _sample(base_ts + i))
            except Exception as exc:  # pragma: no cover — failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(macs[i], 10000.0 * (i + 1))) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 4 threads × 50 inserts = 200 rows total
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 200)


class TestQueryRangeRaw(TestCase):
    """B4 — query_range raw passthrough."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)
        # Fixed reference time so we don't depend on wall clock.
        self.t0 = 1_715_000_000.0
        self.mac = "aa:bb:cc:dd:ee:ff"

    def tearDown(self) -> None:
        self.store.stop()
        self.tmpdir.cleanup()

    def test_unknown_mac_returns_empty_series(self) -> None:
        result = self.store.query_range(
            "11:22:33:44:55:66",
            self.t0,
            self.t0 + 3600,
            metrics=["hashrate_ths"],
        )
        self.assertEqual(result["mac"], "11:22:33:44:55:66")
        self.assertEqual(result["series"]["hashrate_ths"]["avg"], [])
        self.assertEqual(result["series"]["hashrate_ths"]["min"], [])
        self.assertEqual(result["series"]["hashrate_ths"]["max"], [])

    def test_inverted_range_returns_empty_series(self) -> None:
        result = self.store.query_range(self.mac, self.t0 + 3600, self.t0, metrics=["hashrate_ths"])
        self.assertEqual(result["bucket_sec"], 0)
        self.assertEqual(result["series"]["hashrate_ths"]["avg"], [])

    def test_raw_passthrough_each_sample_in_its_own_bucket(self) -> None:
        # 5 samples over 5 min, target_points high enough that each sample
        # gets its own bucket (no aggregation collision).
        for i in range(5):
            self.store.record_sample(self.mac, _sample(self.t0 + i * 60, hashrate_ths=200.0 + i))
        result = self.store.query_range(
            self.mac,
            self.t0,
            self.t0 + 300,
            metrics=["hashrate_ths"],
            target_points=300,
        )
        avg_pts = result["series"]["hashrate_ths"]["avg"]
        # Range = 300 sec; with target_points=300 → bucket_sec = 1 sec → each
        # sample lands in its own integer bucket.
        self.assertEqual(len(avg_pts), 5)
        # AVG/MIN/MAX collapse to the same value when there's one sample per
        # bucket — shape consistency for the frontend.
        for i in range(5):
            self.assertAlmostEqual(avg_pts[i][1], 200.0 + i)
            self.assertAlmostEqual(result["series"]["hashrate_ths"]["min"][i][1], 200.0 + i)
            self.assertAlmostEqual(result["series"]["hashrate_ths"]["max"][i][1], 200.0 + i)

    def test_filters_by_mac(self) -> None:
        other_mac = "11:22:33:44:55:66"
        self.store.record_sample(self.mac, _sample(self.t0, hashrate_ths=200.0))
        self.store.record_sample(other_mac, _sample(self.t0, hashrate_ths=999.0))
        result = self.store.query_range(
            self.mac,
            self.t0 - 1,
            self.t0 + 1,
            metrics=["hashrate_ths"],
            target_points=300,
        )
        # Only the queried-mac value shows up.
        avg_pts = result["series"]["hashrate_ths"]["avg"]
        self.assertEqual(len(avg_pts), 1)
        self.assertAlmostEqual(avg_pts[0][1], 200.0)

    def test_filters_by_time_window(self) -> None:
        for i in range(10):
            self.store.record_sample(self.mac, _sample(self.t0 + i * 60))
        # Window covers samples 2-5 (inclusive of from, exclusive of to).
        result = self.store.query_range(
            self.mac,
            self.t0 + 2 * 60,
            self.t0 + 6 * 60,
            metrics=["hashrate_ths"],
            target_points=300,
        )
        self.assertEqual(len(result["series"]["hashrate_ths"]["avg"]), 4)


class TestQueryRangeDownsampling(TestCase):
    """B5 — bucketed AVG/MIN/MAX matches manual computation."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)
        # Align to a 1-hour boundary so AVG/MIN/MAX bucket math is unambiguous
        # in tests that use target_points=1 and a 600- or 3600-second range
        # (otherwise an arbitrary t0 may straddle two buckets).
        self.t0 = 1_715_000_400.0  # divisible by 3600
        self.mac = "aa:bb:cc:dd:ee:ff"

    def tearDown(self) -> None:
        self.store.stop()
        self.tmpdir.cleanup()

    def test_raw_bucket_aggregation_matches_manual_avg_min_max(self) -> None:
        # 10 samples in a single 1-hour bucket spanning 600 seconds.  We'll
        # query with target_points=1 → bucket_sec >= range, so all 10 values
        # collapse to one bucket whose AVG/MIN/MAX we compute manually.
        values = [200.0 + i for i in range(10)]
        for i, v in enumerate(values):
            self.store.record_sample(self.mac, _sample(self.t0 + i * 60, hashrate_ths=v))
        result = self.store.query_range(
            self.mac,
            self.t0,
            self.t0 + 600,
            metrics=["hashrate_ths"],
            target_points=1,
        )
        avg_pts = result["series"]["hashrate_ths"]["avg"]
        min_pts = result["series"]["hashrate_ths"]["min"]
        max_pts = result["series"]["hashrate_ths"]["max"]
        self.assertEqual(len(avg_pts), 1)
        # Manual: AVG = 204.5, MIN = 200, MAX = 209.
        self.assertAlmostEqual(avg_pts[0][1], sum(values) / len(values))
        self.assertAlmostEqual(min_pts[0][1], min(values))
        self.assertAlmostEqual(max_pts[0][1], max(values))

    def test_target_points_caps_bucket_count(self) -> None:
        # 100 samples spread across 1 hour — request target_points=10.
        # Allow up to target_points + 1 for partial leading/trailing buckets
        # when the range start doesn't land on a bucket boundary.
        for i in range(100):
            self.store.record_sample(self.mac, _sample(self.t0 + i * 36))
        result = self.store.query_range(
            self.mac,
            self.t0,
            self.t0 + 3600,
            metrics=["hashrate_ths"],
            target_points=10,
        )
        avg_pts = result["series"]["hashrate_ths"]["avg"]
        self.assertLessEqual(len(avg_pts), 11)

    def test_downsampled_table_used_for_long_range(self) -> None:
        # Insert directly into samples_5min.  query_range over a 7-day window
        # MUST read from the 5-min table (not raw samples), and aggregate
        # AVG/MIN/MAX of the per-bucket values correctly.
        conn = self.store._connect()
        try:
            for i in range(12):
                ts = self.t0 + i * BUCKET_5MIN_SEC
                # Each row: avg=10+i, min=8+i, max=12+i.
                conn.execute(
                    "INSERT INTO samples_5min ("
                    "  mac, ts,"
                    "  hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max,"
                    "  power_w_avg, power_w_min, power_w_max,"
                    "  efficiency_jth_avg, efficiency_jth_min, efficiency_jth_max,"
                    "  temp_max_c_avg, temp_max_c_min, temp_max_c_max,"
                    "  temp_avg_c_avg, temp_avg_c_min, temp_avg_c_max,"
                    "  fan_speed_avg, fan_speed_min, fan_speed_max,"
                    "  firmware_type"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.mac,
                        ts,
                        10.0 + i,
                        8.0 + i,
                        12.0 + i,
                        4000.0,
                        3900.0,
                        4100.0,
                        20.0,
                        19.0,
                        21.0,
                        70.0,
                        65.0,
                        75.0,
                        60.0,
                        55.0,
                        65.0,
                        50.0,
                        45,
                        55,
                        "epic",
                    ),
                )
        finally:
            conn.close()
        # 7-day query — picks samples_5min as the source.  target_points=1 so
        # all 12 input rows aggregate into one output bucket.
        result = self.store.query_range(
            self.mac,
            self.t0,
            self.t0 + 7 * 86400.0,
            metrics=["hashrate_ths"],
            target_points=1,
        )
        avg_pts = result["series"]["hashrate_ths"]["avg"]
        min_pts = result["series"]["hashrate_ths"]["min"]
        max_pts = result["series"]["hashrate_ths"]["max"]
        self.assertEqual(len(avg_pts), 1)
        # AVG of input AVGs: mean(10..21) = 15.5.
        # MIN of input MINs: 8.
        # MAX of input MAXes: 23.
        self.assertAlmostEqual(avg_pts[0][1], sum(10.0 + i for i in range(12)) / 12)
        self.assertAlmostEqual(min_pts[0][1], 8.0)
        self.assertAlmostEqual(max_pts[0][1], 12.0 + 11)


class TestCompactRetention(TestCase):
    """B6 — compact() rolls raw → 5-min → 1-hr; honors 0=forever."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)
        self.now = 1_715_000_000.0
        self.mac = "aa:bb:cc:dd:ee:ff"

    def tearDown(self) -> None:
        self.store.stop()
        self.tmpdir.cleanup()

    def _count(self, table: str) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_raw_rows_older_than_cutoff_roll_into_5min_table(self) -> None:
        # Fill 12 raw samples covering 1 hour, all OLDER than the retention cutoff.
        old_base = self.now - 100 * 86400.0  # 100 days ago, > 90-day retention
        for i in range(12):
            self.store.record_sample(self.mac, _sample(old_base + i * 300))
        # Add some recent rows that should NOT be touched.
        for i in range(5):
            self.store.record_sample(self.mac, _sample(self.now - i * 60))
        # Compact with default 90-day raw retention.
        moved = self.store.compact(now=self.now, retention_raw_days=90)
        self.assertGreater(moved["raw_to_5min"], 0)
        # Old raws are gone, recent raws survive.
        # 12 old samples spaced 300s apart fall into 12 distinct 5-min buckets.
        self.assertEqual(self._count("samples"), 5)
        # samples_5min should have 12 rows (one per 5-min bucket of old data).
        self.assertEqual(self._count("samples_5min"), 12)

    def test_retention_zero_disables_raw_roll(self) -> None:
        # With retention_raw_days=0 the cutoff is "now" but the gate disables
        # the entire roll.  Old samples stay in raw.
        old_base = self.now - 100 * 86400.0
        for i in range(5):
            self.store.record_sample(self.mac, _sample(old_base + i * 300))
        moved = self.store.compact(now=self.now, retention_raw_days=0)
        self.assertEqual(moved["raw_to_5min"], 0)
        self.assertEqual(self._count("samples"), 5)
        self.assertEqual(self._count("samples_5min"), 0)

    def test_5min_rows_older_than_cutoff_roll_into_1hr_table(self) -> None:
        # Insert 12 5-min rows directly into samples_5min covering 1 hour,
        # all OLDER than the 5-min retention cutoff.  Snap to a 1-hour
        # boundary so the 12 rows collapse to exactly one 1-hr bucket.
        approx_old = self.now - 400 * 86400.0  # 400 days ago, > 365-day retention
        old_base = float(int(approx_old / BUCKET_1HR_SEC) * BUCKET_1HR_SEC)
        conn = self.store._connect()
        try:
            for i in range(12):
                ts = old_base + i * BUCKET_5MIN_SEC
                conn.execute(
                    "INSERT INTO samples_5min ("
                    "  mac, ts,"
                    "  hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max,"
                    "  power_w_avg, power_w_min, power_w_max,"
                    "  efficiency_jth_avg, efficiency_jth_min, efficiency_jth_max,"
                    "  temp_max_c_avg, temp_max_c_min, temp_max_c_max,"
                    "  temp_avg_c_avg, temp_avg_c_min, temp_avg_c_max,"
                    "  fan_speed_avg, fan_speed_min, fan_speed_max,"
                    "  firmware_type"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.mac,
                        ts,
                        200.0,
                        195.0,
                        205.0,
                        4000.0,
                        3900.0,
                        4100.0,
                        20.0,
                        19.0,
                        21.0,
                        70.0,
                        65.0,
                        75.0,
                        60.0,
                        55.0,
                        65.0,
                        50.0,
                        45,
                        55,
                        "epic",
                    ),
                )
        finally:
            conn.close()
        moved = self.store.compact(now=self.now, retention_raw_days=90, retention_5min_days=365)
        # 12 5-min rows spanning 1 hour collapse to ONE 1-hr bucket.
        self.assertEqual(moved["5min_to_1hr"], 12)  # rows DELETED, not output rows
        self.assertEqual(self._count("samples_5min"), 0)
        self.assertEqual(self._count("samples_1hr"), 1)

    def test_1hr_retention_zero_means_keep_forever(self) -> None:
        # Insert one ancient 1-hr row.  With retention_1hr_days=0 (default),
        # the row must NOT be deleted.
        ancient_ts = self.now - 100_000 * 86400.0  # 100 millennia ago
        conn = self.store._connect()
        try:
            conn.execute(
                "INSERT INTO samples_1hr ("
                "  mac, ts,"
                "  hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max,"
                "  power_w_avg, power_w_min, power_w_max,"
                "  efficiency_jth_avg, efficiency_jth_min, efficiency_jth_max,"
                "  temp_max_c_avg, temp_max_c_min, temp_max_c_max,"
                "  temp_avg_c_avg, temp_avg_c_min, temp_avg_c_max,"
                "  fan_speed_avg, fan_speed_min, fan_speed_max,"
                "  firmware_type"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.mac,
                    ancient_ts,
                    200.0,
                    195.0,
                    205.0,
                    4000.0,
                    3900.0,
                    4100.0,
                    20.0,
                    19.0,
                    21.0,
                    70.0,
                    65.0,
                    75.0,
                    60.0,
                    55.0,
                    65.0,
                    50.0,
                    45,
                    55,
                    "epic",
                ),
            )
        finally:
            conn.close()
        result = self.store.compact(now=self.now, retention_1hr_days=0)
        self.assertEqual(result["1hr_pruned"], 0)
        self.assertEqual(self._count("samples_1hr"), 1)

    def test_1hr_retention_positive_prunes_old_rows(self) -> None:
        # One ancient row, one recent row.  retention_1hr_days=30 → only the
        # ancient row is pruned.
        ancient_ts = self.now - 365 * 86400.0
        recent_ts = self.now - 5 * 86400.0
        conn = self.store._connect()
        try:
            for ts in (ancient_ts, recent_ts):
                conn.execute(
                    "INSERT INTO samples_1hr ("
                    "  mac, ts,"
                    "  hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max,"
                    "  power_w_avg, power_w_min, power_w_max,"
                    "  efficiency_jth_avg, efficiency_jth_min, efficiency_jth_max,"
                    "  temp_max_c_avg, temp_max_c_min, temp_max_c_max,"
                    "  temp_avg_c_avg, temp_avg_c_min, temp_avg_c_max,"
                    "  fan_speed_avg, fan_speed_min, fan_speed_max,"
                    "  firmware_type"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.mac,
                        ts,
                        200.0,
                        195.0,
                        205.0,
                        4000.0,
                        3900.0,
                        4100.0,
                        20.0,
                        19.0,
                        21.0,
                        70.0,
                        65.0,
                        75.0,
                        60.0,
                        55.0,
                        65.0,
                        50.0,
                        45,
                        55,
                        "epic",
                    ),
                )
        finally:
            conn.close()
        result = self.store.compact(now=self.now, retention_1hr_days=30)
        self.assertEqual(result["1hr_pruned"], 1)
        self.assertEqual(self._count("samples_1hr"), 1)

    def test_compact_aggregates_avg_min_max_correctly(self) -> None:
        # 12 raw samples in a single 5-min bucket with known values.  Compact
        # rolls them into samples_5min with the correct AVG/MIN/MAX.
        old_base = self.now - 100 * 86400.0
        # Snap to 5-min boundary so the bucket math is unambiguous.
        bucket_start = int(old_base / BUCKET_5MIN_SEC) * BUCKET_5MIN_SEC
        values = [200.0 + i for i in range(12)]  # 200..211
        for i, v in enumerate(values):
            self.store.record_sample(
                self.mac,
                _sample(bucket_start + i * 25, hashrate_ths=v),
            )
        self.store.compact(now=self.now, retention_raw_days=90)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT hashrate_ths_avg, hashrate_ths_min, hashrate_ths_max "
                "FROM samples_5min WHERE mac=?",
                (self.mac,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], sum(values) / len(values))
        self.assertAlmostEqual(row[1], min(values))
        self.assertAlmostEqual(row[2], max(values))


class TestRetentionThread(TestCase):
    """B10 sneak-peek — start_retention_thread + stop join cleanly."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "metrics.db")
        self.store = MetricsStore(self.db_path)

    def tearDown(self) -> None:
        # Must JOIN the daemon thread before cleaning up the tmpdir.  On
        # Linux's strict rmtree, the daemon's open SQLite connection (during
        # the first compact() call) holds a write lock on metrics.db-wal —
        # the rmdir then fails with ENOTEMPTY.  Mac is forgiving and
        # silently lazy-cleans, masking the race.  Always join with a
        # bounded timeout.
        self.store.stop()
        if self.store._thread is not None:
            self.store._thread.join(timeout=2.0)
        self.tmpdir.cleanup()

    def test_start_then_stop_joins(self) -> None:
        self.store.start_retention_thread()
        # Brief sanity check: thread is alive and named.
        self.assertIsNotNone(self.store._thread)
        self.assertTrue(self.store._thread.is_alive())
        self.store.stop()
        # Wait briefly for daemon to react.  stop() is non-blocking by design;
        # the daemon wakes on the wait timeout or the stop event.
        self.store._thread.join(timeout=2.0)
        self.assertFalse(self.store._thread.is_alive())

    def test_start_is_idempotent(self) -> None:
        self.store.start_retention_thread()
        first = self.store._thread
        self.store.start_retention_thread()
        self.assertIs(self.store._thread, first)
