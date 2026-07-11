"""In-code CONFIG defaults — applied at startup before disk overlay.

_MINER_CONFIG_DEFAULTS holds per-miner-only defaults (not merged into fleet CONFIG).

CONFIG_DEFAULTS_PER_PLATFORM_KEYS is derived from CONFIG_DEFAULTS by excluding
FLEET_OPS_KEYS — these are the keys that go into state.CONFIG["defaults"][platform]
for each supported firmware platform. Used by persistence.py's v3 migration
partition and the HTTP handler's compat fan-out shim.
"""

from __future__ import annotations

from tuner_app import state
from tuner_app.constants import _PLATFORMS, FLEET_OPS_KEYS

CONFIG_DEFAULTS = {
    # Tuning parameters
    # Leave margin below common firmware shutdown / silicon limits. Operators
    # must verify model-specific limits before tuning.
    "BOARD_MAX_TEMP": 82,
    "CHIP_CRITICAL_TEMP": 97,
    # Cap on inter-chip frequency variance. Each alive chip's iterative window
    # is `[seed_f - spread/2, seed_f + spread/2]` centered on the Phase V
    # coarse-grid winner at the active voltage; `max(alive cur) - min(alive cur)
    # <= SPREAD` by construction. Dead chips (parked at DEAD_CHIP_FREQ) are
    # excluded from this calculation since their pinned freq would otherwise
    # blow the spread. Firmware-level safety (chips must stay >= 50 MHz) is
    # enforced via the FIRMWARE_FREQ_MIN_MHZ constant, not a tuning knob.
    "CHIP_FREQ_SPREAD_MHZ": 40,
    "BASELINE_VOLTAGE_MV": 15100,
    "BASELINE_FREQ": 200,
    "DEAD_CHIP_SCORE": 1.0,  # Chips with baseline AND current score <= this are dead — skip them
    "DEAD_CHIP_FREQ": 50,  # Frequency for truly dead chips (saves power). Firmware minimum is ~50 MHz.
    # Iterative chip-tune step + tolerance knobs (replaces the old binary search).
    # Each round measures per-chip health vs baseline. Chips at/near baseline
    # step UP by CHIP_TUNE_STEP_MHZ; chips well below baseline step DOWN.
    # Between is a hold band (no move). Loop exits after CHIP_TUNE_STILLNESS_STREAK
    # consecutive rounds with zero moves. Both UP and DOWN steps are clamped
    # to the per-chip seed window so the spread cap is enforced bidirectionally.
    "CHIP_TUNE_STEP_MHZ": 6.25,  # MHz per move; snapped to 3.125 grid.
    "CHIP_TUNE_UP_TOLERANCE": 5,  # Health within (baseline - this) -> step UP.
    "CHIP_TUNE_DOWN_TOLERANCE": 15,  # Health below (baseline - this) -> step DOWN. UP <= DOWN.
    "CHIP_TUNE_STILLNESS_STREAK": 2,  # Consecutive zero-move rounds before declaring done.
    # Cell-match tolerance — used by the dynamic state machine's
    # _chip_tune_already_done_for to decide whether a voltage_results entry
    # corresponds to a given fine/coarse cell, and by the dashboard cell-popup
    # for before/after lookup. Not a convergence threshold (the iterative loop
    # has no notion of one).
    "FREQ_SEARCH_TOLERANCE_MHZ": 7,
    "FREQ_STEP_EMERGENCY": 20,
    "STABILIZE_WAIT": 120,
    "BASELINE_SAMPLES": 20,
    "BASELINE_INTERVAL": 30,
    "ROUND_SAMPLES": 20,
    "ROUND_INTERVAL": 30,
    # Phase 0 stock baseline sampling. Captures the miner's pre-tune steady
    # state (hashrate / power / per-chip freqs+health+temps) by averaging
    # this many summary samples spaced this many seconds apart. Default 5×40s
    # = 4 sleeps × 40s = ~160s window, long enough for chip temps and hashrate
    # to stabilize after a fresh reboot. Lower values risk capturing a still-
    # ramping miner; higher values add startup latency.
    "STOCK_BASELINE_SAMPLES": 5,
    "STOCK_BASELINE_INTERVAL": 40,
    # When True, Phase 3 skips the stop_mining/start_mining cycle between
    # profiling rounds. Faster (~5-10 min saved per round), but unstable chips
    # don't get a clean chip-state reset between rounds — convergence may take
    # more rounds or settle at lower final freqs on noisy silicon.
    "SKIP_ROUND_RESTART": False,
    # Power cap (W) applied at Phase 1 on Bixbit miners; ePIC ignores
    # (set_power_limit is a no-op on ePIC). Fleet-wide — one cap applies
    # to all Bixbit miners; per-miner override is nonsensical (rejected by
    # FLEET_ONLY_KEYS). Default 3500 matches S21 baseline power draw.
    "POWER_LIMIT_W": 3500,
    # Perpetual tune (voltage-tracking) — adjusts voltage based on hashrate drift
    # against the value measured at the active sweep profile. Only changes chip
    # frequencies on thermal emergencies. Only restarts the miner when the
    # voltage adjuster reaches its positive cap AND the rate-limit has elapsed.
    "PERPETUAL_VOLTAGE_CHECK_MIN": 10,
    "PERPETUAL_VOLTAGE_STEP_MV": 50,
    "PERPETUAL_VOLTAGE_MAX_DELTA_MV": 300,
    "PERPETUAL_HASHRATE_DEADBAND_PCT": 0.5,
    "PERPETUAL_RESTART_MIN_HOURS": 24,
    "SETTLE_POLL_INTERVAL": 30,
    "SETTLE_MAX_ATTEMPTS": 20,
    "SETTLE_VOLTAGE_TOLERANCE_MV": 500,
    "RESET_STOP_WAIT": 30,
    "RESET_START_WAIT": 300,
    # Phase V: 2D (voltage, uniform-frequency) efficiency exploration.
    # Replaces the old 1D voltage descent. Samples a grid of (V, F_uniform)
    # points cheaply, picks the best operating region, then runs per-chip
    # Phase 3 at the top-K voltages with narrowed search bounds. The grid's
    # bottom-V is START_VOLTAGE_MV (PSU min); the top-V is stock +
    # SWEEP_OVER_STOCK_MV; the bounds are rechecked before every write.
    "SWEEP_OVER_STOCK_MV": 0,  # Offset added to stock voltage to pick grid's top-V (0 = exactly stock)
    "VF_EXPLORE_V_COUNT": 5,  # Voltages in the coarse grid (2–10)
    "VF_EXPLORE_F_MIN": 400,  # Lower bound of uniform-F coarse grid, MHz
    "VF_EXPLORE_F_MAX": 575,  # Upper bound of uniform-F coarse grid, MHz
    "VF_EXPLORE_F_COUNT": 5,  # Frequencies in the coarse grid (2–10)
    "VF_EXPLORE_WAIT": 90,  # Seconds to stabilize at each (V, F) before sampling
    "VF_EXPLORE_SAMPLES": 3,  # J/TH samples per grid point
    "VF_EXPLORE_SAMPLE_INTERVAL": 5,  # Seconds between per-point samples
    "VF_EXPLORE_FINE_COUNT": (
        0
    ),  # Fine grid dimension (N×N around coarse peak); 0 = disabled. Allowed: {0, 3, 5, 9, 25, 49} (odd squares so the anchor sits at the grid center for interior anchors).
    "VF_EXPLORE_TOP_K": 1,  # Number of top fine cells to run per-chip Phase 3 at (1–3). Selected from the union of fine cells inside top-VF_FINE_TOP_K coarse anchors' fine grids (or, when fine grids are disabled, from top coarse cells directly).
    "VF_FINE_TOP_K": 3,  # Number of top coarse cells (by current J/TH or $/day) that get fine-gridded. Independent of VF_EXPLORE_TOP_K — fine grids are exploration data, chip-tunes select from within them.
    "VF_EXPLORE_TREND_CONFIRM": 2,  # Consecutive worse-than-best points needed to stop a ray direction
    # The dynamic state machine walks rays out from the top
    # VF_COARSE_TOP_K_RAYS coarse cells (by current scoring) until each
    # direction either hits the grid edge or trend-stops. Default 3 to
    # match VF_FINE_TOP_K so every fine-gridded anchor has had rays walked
    # from it. 1 = walk rays only from the current global best (fastest,
    # but a single noisy outlier at the winner can terminate exploration
    # too early). 2-10 = walk from runners-up too — guards against that
    # outlier-stop case. Operator-decoupled from VF_FINE_TOP_K.
    "VF_COARSE_TOP_K_RAYS": 3,
    # Phase 3b — post-iterative stability polish. The iterative loop terminates
    # on per-round health snapshots that may be too short to catch slow drift;
    # this pass uses a longer sample window (its own _ROUND_SAMPLES /
    # _ROUND_INTERVAL knobs) and drops any chip whose health falls below
    # baseline by one POLISH_STEP. Decrement-only — never raises a chip's
    # frequency. Exits early when a round produces no changes.
    "STABILITY_POLISH_ROUNDS": 3,  # 0 disables the phase entirely; 1-10 polish passes
    "STABILITY_POLISH_STEP_MHZ": 6.25,  # MHz drop per unstable chip per round; snapped to 3.125 grid
    "STABILITY_POLISH_ROUND_SAMPLES": 40,  # Longer than ROUND_SAMPLES (default 2x) so polish catches slow drift Phase 3 misses.
    "STABILITY_POLISH_ROUND_INTERVAL": 30,  # Seconds between polish samples.
    "STABILITY_POLISH_STABILIZE_WAIT": 300,
    "EFFICIENCY_MEASURE_WAIT": 120,
    "MAX_CONSECUTIVE_RETRIES": 5,
    # Offline-handling — pause tuning cleanly when the miner drops off the network.
    "OFFLINE_POLL_INTERVAL": 30,
    "OFFLINE_FAILURE_THRESHOLD": 3,
    # Phase 3 safety cap — iterative loop converges in 10-20 typical, 30-40
    # worst-case. 60 gives ~5hr cap at default cadence; runaway oscillation
    # tripped much sooner. Operators can override up to the validation max.
    "MAX_PROFILING_ROUNDS": 60,
    # IP-range scanner — discovers supported miners on explicitly configured ranges.
    # SCAN_IP_RANGES: list of CIDR or dash-range strings to scan.
    # SCAN_IP_BLACKLIST: list of CIDR / dash-range / single-IP strings to skip
    #   during scanning. Same grammar as SCAN_IP_RANGES; takes precedence
    #   (blacklisted IPs are never probed even if they appear in a scan range).
    # SCAN_PASSWORDS: passwords to try (in order) when probing each discovered IP.
    # SCAN_TIMEOUT_SEC: per-probe HTTP timeout in seconds.
    # SCAN_CONCURRENCY: max concurrent probe threads.
    # SCAN_INTERVAL_MIN: minutes between automatic scan cycles (0 = disabled).
    # SCAN_AUTO_REGISTER: if True, found miners are registered automatically.
    "SCAN_IP_RANGES": [],
    "SCAN_IP_BLACKLIST": [],
    "SCAN_PASSWORDS": ["letmein"],
    "SCAN_TIMEOUT_SEC": 2.0,
    "SCAN_CONCURRENCY": 1024,
    "SCAN_INTERVAL_MIN": 0,
    "SCAN_AUTO_REGISTER": False,
    # Miner connection
    "MINER_IPS": [],
    # Stdout log level gate — entries below this level skip stdout but always
    # write to JSONL. Allowed: DEBUG, INFO, WARN, ERROR (case-insensitive).
    "LOG_STDOUT_LEVEL": "INFO",
    # Per-engine dedup window (seconds). Consecutive identical messages (same
    # msg + level) within this window are suppressed; a single "(suppressed N
    # duplicates)" line is emitted when the window expires or a different msg
    # arrives. 0 disables dedup entirely. Targets "Monitor: transient offline"
    # spam during sustained network outages.
    "LOG_DEDUP_WINDOW_SEC": 5,
    # Persistent multi-timeframe statistics retention (Phase B).
    # Three-tier downsampling pipeline: raw samples (~1 row/monitor cycle) →
    # 5-min buckets → 1-hr buckets. Each retention horizon controls when rows
    # roll forward to the next coarser table.
    #   METRICS_RETENTION_RAW_DAYS:    days of raw retention before 5-min downsample.
    #   METRICS_RETENTION_5MIN_DAYS:   days of 5-min retention before 1-hr downsample.
    #   METRICS_RETENTION_1HR_DAYS:    days of 1-hr retention; 0 = forever.
    #   METRICS_COMPACT_INTERVAL_HOURS: cadence of the retention sweep daemon.
    "METRICS_RETENTION_RAW_DAYS": 90,
    "METRICS_RETENTION_5MIN_DAYS": 365,
    "METRICS_RETENTION_1HR_DAYS": 0,
    "METRICS_COMPACT_INTERVAL_HOURS": 6,
    # Per-miner firmware auth password — derived from SCAN_PASSWORDS[0] at runtime
    # (PASSWORD->SCAN_PASSWORDS migration in persistence.py). Kept in CONFIG so
    # EffectiveConfig["PASSWORD"] works for engine MinerAPI authentication.
    # Do not set this directly; configure SCAN_PASSWORDS in the gear modal instead.
    "PASSWORD": "letmein",
    "API_PORT": 4028,
    # Source IP for outbound connections (bind to this local interface).
    # Empty = let OS routing decide, then auto-probe local interfaces on failure.
    # Set this manually if you're multi-homed (Wi-Fi + Ethernet, VPN, etc.) and
    # the OS is picking the wrong interface to reach the miner.
    "SOURCE_IP": "",
    # Starting voltage (mV, 0 = use PSU minimum from capabilities)
    "START_VOLTAGE_MV": 0,
    # Profitability tuning mode. When "efficiency" (default), the tuner ranks
    # cells by J/TH (lower = better). When "profitability", it ranks cells by
    # $/day using the live minerstat snapshot + ELECTRIC_RATE_PER_KWH. Per-miner
    # override supported — different miners can run different modes if e.g. one
    # is on a different coin/pool.
    "TARGET_MODE": "efficiency",
    # $/kWh paid for power. Per-miner overridable since some sites have
    # different rates per feed/phase. Default 0.10 is a generic US residential
    # rate; commercial mining hosts usually land in the 0.045–0.09 band.
    "ELECTRIC_RATE_PER_KWH": 0.10,
    # Coin identifier that minerstat returns pricing/network data for. Fleet-
    # wide: the snapshot is one shared pull used by all miners in profit mode,
    # so mixing coins across the fleet requires separate tuner processes (which
    # is already the pattern for mixing algos — S21 and L7 run distinct
    # tuner.py instances). Must match a coin id in the minerstat /coins
    # response. Edited via the Minerstat card's settings modal on the overview.
    "MINERSTAT_COIN": "BTC",
    # Day of month (1-28) the scheduler auto-fetches minerstat and applies
    # profit recompute to all profit-mode miners. 0 = disabled (manual only).
    # Fleet-wide setting — the schedule is global, not per-miner, because
    # electric billing is fleet-wide. The day should match the billing cycle
    # reset so any voltage increase hits at the start of a fresh demand-charge
    # window rather than mid-month.
    "MINERSTAT_POLL_DAY": 0,
    # Optional minerstat API key for higher rate limits. Free tier works
    # without a key. Empty string = no auth header sent.
    "MINERSTAT_API_KEY": "",
    # Revenue-side modifier applied to all $/day calculations, as a percentage.
    # Fleet-wide. Use positive values when actual earnings exceed raw-mining
    # revenue (e.g. +9.5 when renting rigs out via MiningRigRentals at a
    # premium over pool revenue) and negative when they fall short (e.g. -5
    # for pool-fee overhead the raw math doesn't capture). 0.0 = no modifier.
    # Applied only to revenue, never to power cost — the cost side is
    # already captured by ELECTRIC_RATE_PER_KWH.
    "INCOME_MODIFIER_PCT": 0.0,
    # MiningRigRentals auto-publish. When enabled, the tuner flips each
    # configured rig to "disabled" on tune start and "enabled" with an
    # advertised hashrate on Phase 6 entry / active-profile change. API
    # credentials are obtained at miningrigrentals.com → Account → API Keys
    # with the "rigs" permission. Fleet-wide (one account, N rigs); per-miner
    # rig ID mapping lives in MINER_CONFIGS[ip]["MRR_RIG_ID"].
    "MRR_ENABLED": False,
    "MRR_API_KEY": "",
    "MRR_API_SECRET": "",
    # Multiplicative percentage applied to sweep_hashrate_ths before pushing
    # to MRR. advertised = sweep_hashrate_ths * (1 + MRR_HASHRATE_MODIFIER_PCT/100).
    # Positive to advertise above measured (common when the rig historically
    # over-delivers by a few %), negative for a conservative haircut. Fleet
    # default with per-miner override supported — overrides in MINER_CONFIGS[ip].
    "MRR_HASHRATE_MODIFIER_PCT": 0.0,
    # Unit MRR expects in the `hash.type` field. S21 = SHA-256 = "th". Future
    # model support (L7/Scrypt would be "gh", etc.) can override per-fleet.
    # Valid values: hash, kh, mh, gh, th, ph, eh.
    "MRR_HASHRATE_UNIT": "th",
    # MRR account username used to build stratum logins. Each miner's login
    # is `{MRR_STRATUM_USERNAME}.{MRR_RIG_ID}`. Fleet-wide — all miners under
    # one MRR account share the username. Required when MRR_ENABLED=True.
    "MRR_STRATUM_USERNAME": "",
    # Coin the MRR pool config sets on the miner. ePIC /coin API enum —
    # accepts "BTC" (SHA-256, S21) or "LTC" (Scrypt, L7). Fleet-wide since a
    # single tuner process only runs one algo at a time.
    "MRR_COIN": "BTC",
    # When True, the engine fires mrr_sync("maintaining") once at Phase 3b
    # polish entry per chip-tune voltage (in addition to the existing Phase 6
    # entry sync). Useful when long-polishing miners should be advertised on
    # MRR before they reach steady state. Fleet-wide.
    "MRR_PUBLISH_DURING_POLISH": False,
    # Per-miner MRR rig ID (0 = not configured, positive int = MRR rig ID).
    # Stored in CONFIG so EffectiveConfig has a fleet default (always 0);
    # operators set the real ID via the per-miner override in MINER_CONFIGS.
    # A rig ID of 0 means "skip MRR for this miner" — the sync is a no-op.
    "MRR_RIG_ID": 0,
    # ── Braiins firmware tuning knobs ──
    # Operator-tunable bounds for the wattage binary-search algorithm in
    # tuner_app/tuning_engine/braiins_phases.py. ePIC and Bixbit miners ignore
    # these. BRAIINS_USERNAME is per-miner overrideable (e.g. for miners with
    # non-default Braiins login). The wattage knobs are fleet-only.
    "BRAIINS_POWER_MIN_W": 1500,
    "BRAIINS_POWER_MAX_W": 5000,
    "BRAIINS_TUNER_STABILIZE_WAIT_SEC": 600,
    "BRAIINS_BINARY_SEARCH_TOLERANCE_W": 100,
    "BRAIINS_USERNAME": "root",
    # ── LuxOS firmware tuning knobs ──
    # Minimum interval (seconds) between consecutive TCP connection attempts to
    # a single LuxOS miner. Issue #26: LuxOS port 4028 stops accepting any TCP
    # connections when stormed during Phase 0 (3 cmds back-to-back, retried 3x);
    # this gate spaces out the bursts. ePIC, Bixbit, Braiins ignore the knob.
    # 0.0 disables the gate. Per-platform default (state.CONFIG["defaults"]["luxos"]).
    "LUXOS_MIN_CONN_INTERVAL_SEC": 1.0,
    # Backoff window (seconds) applied after a ConnectionRefusedError from a
    # LuxOS miner. _apply_rate_limit will sleep until this deadline before
    # allowing the next TCP connection attempt. 0.0 disables the window.
    "LUXOS_OFFLINE_BACKOFF_SEC": 30.0,
    # ── Whatsminer (stock MicroBT) tuning knobs ──
    # Operator-tunable bounds for the 2D power_limit × target_freq grid-search
    # algorithm in tuner_app/tuning_engine/whatsminer_phases.py. ePIC, Bixbit,
    # LuxOS, and Braiins miners ignore these knobs. Per-platform default
    # (state.CONFIG["defaults"]["whatsminer"]).
    "WHATSMINER_PL_MIN_W": 1500,
    "WHATSMINER_PL_COUNT": 5,
    "WHATSMINER_FREQ_MIN_MHZ": 400,
    "WHATSMINER_FREQ_MAX_MHZ": 700,
    "WHATSMINER_FREQ_COUNT": 5,
    "WHATSMINER_FINE_COUNT": 3,
    "WHATSMINER_FINE_TOP_K": 2,
    "WHATSMINER_STABILIZE_SEC": 60,
    "WHATSMINER_RESTART_WAIT_SEC": 90,
    "WHATSMINER_UPFREQ_TIMEOUT_SEC": 180,
    "WHATSMINER_SAMPLE_WINDOW_SEC": 60,
    "WHATSMINER_SAMPLE_INTERVAL_SEC": 10,
    "WHATSMINER_BASELINE_SAMPLES": 5,
    "WHATSMINER_PERPETUAL_INTERVAL_SEC": 300,
    "WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT": 5.0,
    "WHATSMINER_UPFREQ_SPEED": 5,
}

# Per-miner-only defaults — NOT merged into fleet CONFIG.
# Used as the migration seed when loading old config.json files that lack
# firmware_type per miner. Importable as:
#   from tuner_app.config.defaults import _MINER_CONFIG_DEFAULTS
_MINER_CONFIG_DEFAULTS: dict = {
    "firmware_type": "epic",
}

# Derived: keys from CONFIG_DEFAULTS that belong in the per-platform buckets
# (i.e. everything that is NOT a fleet_ops singleton). Computed once at module
# load time so persistence.py and the HTTP handler compat shim can import it
# without re-deriving inline.
CONFIG_DEFAULTS_PER_PLATFORM_KEYS: frozenset = frozenset(
    k for k in CONFIG_DEFAULTS if k not in FLEET_OPS_KEYS
)

# Sanity: the two key partitions must not overlap. Any future maintainer who
# adds a key to BOTH FLEET_OPS_KEYS and CONFIG_DEFAULTS_PER_PLATFORM_KEYS would
# create a silent reader divergence (validation._lookup_default checks the
# platform bucket first; EffectiveConfig.__getitem__ also checks the platform
# bucket first — but any future out-of-order check would silently disagree).
assert frozenset() == FLEET_OPS_KEYS & CONFIG_DEFAULTS_PER_PLATFORM_KEYS, (
    "FLEET_OPS_KEYS and per-platform default keys must be disjoint partitions"
)


def iter_all_config_keys() -> frozenset:
    """Union of every key across fleet_ops and all platform-default buckets.

    Returns a frozenset of config key strings that can be iterated to build a
    flat key→value snapshot of the effective config across all platforms.
    Order is unspecified; consumers should treat as a set.

    This helper reads state.CONFIG directly (no lock) — callers that need
    atomicity must hold state.config_lock before calling.  The helper itself
    does NOT acquire the lock so it can be used both inside and outside
    existing lock contexts without double-acquisition.

    Used by:
      - phase_runners.phase0_discovery to build engine.config_snapshot
      - status_routes.export to build the flat live_cfg comparison dict
    """
    keys: set = set(state.CONFIG["fleet_ops"].keys())
    for platform in _PLATFORMS:
        keys |= set(state.CONFIG["defaults"][platform].keys())
    return frozenset(keys)


def apply_defaults() -> None:
    """Populate state.CONFIG with v3 nested defaults.

    Called by tuner_app.main at startup BEFORE load_config_from_disk(),
    so disk values overlay the in-code defaults rather than the other
    way around.

    Idempotent — clears and repopulates on every call; mutates the same
    state.CONFIG dict object in place (never rebinds state.CONFIG).
    """
    state.CONFIG.clear()
    state.CONFIG["defaults"] = {p: {} for p in _PLATFORMS}
    state.CONFIG["fleet_ops"] = {}
    for key, val in CONFIG_DEFAULTS.items():
        if key in FLEET_OPS_KEYS:
            state.CONFIG["fleet_ops"][key] = val
        elif key in CONFIG_DEFAULTS_PER_PLATFORM_KEYS:
            for platform in _PLATFORMS:
                state.CONFIG["defaults"][platform][key] = val
