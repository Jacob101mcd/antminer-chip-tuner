"""Validation bounds (CONFIG_BOUNDS) for safety-critical config values."""

from __future__ import annotations

# Validation bounds for safety-critical config values
CONFIG_BOUNDS = {
    "CHIP_FREQ_SPREAD_MHZ": (10, 200),
    "DEAD_CHIP_FREQ": (50, 500),
    "BASELINE_VOLTAGE_MV": (11000, 16000),
    "BASELINE_FREQ": (50, 700),
    "BOARD_MAX_TEMP": (50, 85),
    "CHIP_CRITICAL_TEMP": (50, 110),
    "FREQ_SEARCH_TOLERANCE_MHZ": (2, 25),
    "FREQ_STEP_EMERGENCY": (5, 100),
    "CHIP_TUNE_STEP_MHZ": (3.125, 25.0),
    "CHIP_TUNE_UP_TOLERANCE": (1, 100),
    "CHIP_TUNE_DOWN_TOLERANCE": (1, 100),
    "CHIP_TUNE_STILLNESS_STREAK": (1, 10),
    "STABILIZE_WAIT": (30, 31_536_000),
    "BASELINE_SAMPLES": (1, 200),
    "BASELINE_INTERVAL": (1, 31_536_000),
    "ROUND_SAMPLES": (1, 200),
    "ROUND_INTERVAL": (1, 31_536_000),
    "STOCK_BASELINE_SAMPLES": (1, 200),
    "STOCK_BASELINE_INTERVAL": (1, 31_536_000),
    "SWEEP_OVER_STOCK_MV": (-1000, 1000),
    "VF_EXPLORE_V_COUNT": (3, 50),
    "VF_EXPLORE_F_MIN": (50, 900),
    "VF_EXPLORE_F_MAX": (50, 900),
    "VF_EXPLORE_F_COUNT": (3, 20),
    "VF_EXPLORE_WAIT": (10, 31_536_000),
    "VF_EXPLORE_SAMPLES": (1, 20),
    "VF_EXPLORE_SAMPLE_INTERVAL": (1, 31_536_000),
    "VF_EXPLORE_FINE_COUNT": (
        0,
        49,
    ),  # Plus enum check in validate_config — only {0,3,5,9,25,49} accepted.
    "VF_EXPLORE_TOP_K": (1, 50),
    "VF_FINE_TOP_K": (1, 50),
    "VF_EXPLORE_TREND_CONFIRM": (1, 10),
    "VF_COARSE_TOP_K_RAYS": (1, 50),
    "STABILITY_POLISH_ROUNDS": (0, 10),
    "STABILITY_POLISH_STEP_MHZ": (3.125, 25.0),
    "STABILITY_POLISH_ROUND_SAMPLES": (5, 200),
    "STABILITY_POLISH_ROUND_INTERVAL": (5, 31_536_000),
    "STABILITY_POLISH_STABILIZE_WAIT": (30, 31_536_000),
    # Bixbit power cap bounds (ePIC ignores, no-op on ePIC miners)
    "POWER_LIMIT_W": (1500, 6000),
    # Braiins wattage-search bounds (Run 5)
    "BRAIINS_POWER_MIN_W": (500, 6000),
    "BRAIINS_POWER_MAX_W": (500, 6000),
    "BRAIINS_TUNER_STABILIZE_WAIT_SEC": (60, 31_536_000),
    "BRAIINS_BINARY_SEARCH_TOLERANCE_W": (10, 500),
    # Whatsminer (stock MicroBT) 2D power_limit × target_freq grid-search bounds
    "WHATSMINER_PL_MIN_W": (500, 6000),
    "WHATSMINER_PL_COUNT": (3, 10),
    "WHATSMINER_FREQ_MIN_MHZ": (200, 900),
    "WHATSMINER_FREQ_MAX_MHZ": (200, 900),
    "WHATSMINER_FREQ_COUNT": (3, 10),
    "WHATSMINER_FINE_COUNT": (0, 5),
    "WHATSMINER_FINE_TOP_K": (0, 5),
    "WHATSMINER_STABILIZE_SEC": (10, 600),
    "WHATSMINER_RESTART_WAIT_SEC": (10, 600),
    "WHATSMINER_UPFREQ_TIMEOUT_SEC": (30, 600),
    "WHATSMINER_SAMPLE_WINDOW_SEC": (10, 600),
    "WHATSMINER_SAMPLE_INTERVAL_SEC": (1, 60),
    "WHATSMINER_BASELINE_SAMPLES": (1, 30),
    "WHATSMINER_PERPETUAL_INTERVAL_SEC": (60, 86400),
    "WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT": (0.5, 50.0),
    "WHATSMINER_UPFREQ_SPEED": (1, 10),
    # Scanner bounds
    "SCAN_TIMEOUT_SEC": (0.5, 30.0),
    "SCAN_CONCURRENCY": (1, 1024),
    "SCAN_INTERVAL_MIN": (0, 525_600),
    "DEAD_CHIP_SCORE": (0.0, 100.0),
    "MAX_CONSECUTIVE_RETRIES": (1, 50),
    "OFFLINE_POLL_INTERVAL": (10, 31_536_000),
    "OFFLINE_FAILURE_THRESHOLD": (1, 20),
    "RESET_STOP_WAIT": (5, 31_536_000),
    "RESET_START_WAIT": (30, 31_536_000),
    "EFFICIENCY_MEASURE_WAIT": (30, 31_536_000),
    "MAX_PROFILING_ROUNDS": (20, 1000),
    "PERPETUAL_VOLTAGE_CHECK_MIN": (1, 525_600),
    "PERPETUAL_VOLTAGE_STEP_MV": (10, 200),
    "PERPETUAL_VOLTAGE_MAX_DELTA_MV": (50, 1000),
    "PERPETUAL_HASHRATE_DEADBAND_PCT": (0.1, 5.0),
    "PERPETUAL_RESTART_MIN_HOURS": (1, 8760),
    "SETTLE_MAX_ATTEMPTS": (5, 60),
    "SETTLE_VOLTAGE_TOLERANCE_MV": (50, 2000),
    "ELECTRIC_RATE_PER_KWH": (0.001, 2.0),
    "MINERSTAT_POLL_DAY": (0, 28),
    "INCOME_MODIFIER_PCT": (-100.0, 100.0),
    "MRR_HASHRATE_MODIFIER_PCT": (-50.0, 50.0),
    # Dedup window: 0 = disabled, up to 60 s. Bounds-checked; enum for 0 is
    # handled by the >= 0 lower bound (validate_config passes 0 through the
    # numeric path since CONFIG_DEFAULTS["LOG_DEDUP_WINDOW_SEC"] is an int).
    "LOG_DEDUP_WINDOW_SEC": (0, 60),
    # LuxOS connection-rate gate (issue #26): per-instance min interval (sec)
    # between TCP connection attempts. 0 disables the gate. Upper bound 5.0
    # caps how slow tuning can be made via misconfiguration.
    "LUXOS_MIN_CONN_INTERVAL_SEC": (0.0, 5.0),
    # LuxOS ConnectionRefusedError backoff window (issue #33): seconds to hold
    # off new TCP attempts after a port-refused error. 0 disables. Upper bound
    # 300 s (5 min) caps operator misconfiguration.
    "LUXOS_OFFLINE_BACKOFF_SEC": (0.0, 300.0),
    # Metrics retention horizons (Phase B). The lower bounds keep the downsample
    # pipeline meaningful; the upper bounds allow generous historical archives
    # without making operator misconfiguration silently dangerous.
    # METRICS_RETENTION_1HR_DAYS == 0 means "keep 1-hr buckets forever" — that's
    # an explicit allowed value, hence the lower bound is 0.
    "METRICS_RETENTION_RAW_DAYS": (1, 365),
    "METRICS_RETENTION_5MIN_DAYS": (30, 3650),
    "METRICS_RETENTION_1HR_DAYS": (0, 36500),
    "METRICS_COMPACT_INTERVAL_HOURS": (1, 168),
}
