// v4 (A13): the per-miner detail view tracks the canonical MAC instead of
// the IP. ``currentMiner.mac`` is the colon-form canonical MAC (or synth ID);
// ``currentMiner.ip`` is the current IP — populated from the per-miner row in
// /tuner/overview so an IP change picked up by the scanner is visible without
// a hash-route change. ``currentMiner.hostname`` carries the model-specific
// label for the detail-page title.
let currentMiner = {mac: '', ip: '', hostname: ''};
function currentMac(){ return currentMiner.mac; }
function currentIp(){ return currentMiner.ip; }
// Dashed form is what the v4 HTTP routes accept in the URL path. Convert
// once at call time so we don't mutate currentMiner. Synth IDs already use
// dashes throughout and pass through unchanged.
function currentMacDashes(){ return (currentMiner.mac || '').replace(/:/g, '-'); }
let minerList = [];
// Two independent heatmap panes: left = live, right = baselines (Phase 2 + stock)
let heatmapModeLeft = 'freq';   // freq | health | temp | hashrate
let heatmapModeRight = 'p2_freq'; // p2_freq | p2_health | p2_temp | p2_hashrate | stock_freq | stock_health | stock_temp | stock_hashrate
let heatmapData = {
  clocks:null, hashrate:null, chip_temps:null, baseline:null,
  // Phase 2 baseline per-chip arrays — extracted from /tuner/status alongside
  // baseline_scores. Each is shape [num_boards][chip].
  p2_freq:null, p2_temp:null, p2_hashrate:null,
  // Stock baseline pointer — full stock_baseline dict from /tuner/status. The
  // per-chip arrays inside (chip_freqs, chip_health, chip_temps, chip_hashrates)
  // may be missing on legacy stock.json files; right-pane render falls back to
  // gray cells when so.
  stock:null,
};
let heatmapPreview = null; // {voltage_mv, stable_freq_arrays} — drives freq-mode heatmap off a voltage_results snapshot instead of live state
let tunerStatus = {};
// Tracks the capabilities object of the currently-displayed miner (e.g. {supports_per_chip_tuning: true, ...}).
// Set by updateStatus on each poll; used by loadConfig to capability-gate the config form.
let currentDetailCapabilities = null;
let scanStatusPollTimer = null;
const MAX_CHART_POINTS = 120;

// ─── Persistent multi-timeframe statistics (Phase B / B12) ──────────────────
// Range presets that drive both the GET /tuner/metrics request and the chart
// caps.  '1h' is the only mode that continues live-pushing samples on top of
// the seeded history; longer ranges are read-only snapshots refreshed by
// re-selecting the range or navigating between miners.
const METRICS_RANGE_STORAGE_KEY = 'metricsRange';
const METRICS_RANGE_PRESETS = {
  '1h':  { points: 120, showBand: false },
  '24h': { points: 288, showBand: false },
  '7d':  { points: 504, showBand: true },
  '30d': { points: 720, showBand: true },
};
let currentMetricsRange = (function(){
  try {
    const saved = localStorage.getItem(METRICS_RANGE_STORAGE_KEY);
    return (saved && (saved in METRICS_RANGE_PRESETS || saved === 'custom')) ? saved : '1h';
  } catch { return '1h'; }
})();
// Custom-range epoch seconds (only meaningful when currentMetricsRange === 'custom').
let customMetricsRange = { from: null, to: null };

// ─── Config field metadata ──────────────────────────────────────────────────
// One source of truth for every tuning parameter: label, type, tooltip.
// CONFIG_CATEGORIES defines the collapsible group layout on the detail-page
// config tab AND the overview defaults accordion — both call buildConfigForm
// with different prefixes so the two forms stay in lockstep.
// Every tooltip below aims to give: what the setting is in physical terms,
// the unit (if not in the label), the default value, and — when useful — a
// concrete range instead of vague "higher/lower" language.
const CFG_META = {
  // Core tuning — Phase 3 iterative per-chip health loop
  CHIP_FREQ_SPREAD_MHZ:  {label:'Chip Freq Spread (MHz)',     type:'number', requires: 'supports_per_chip_tuning', tooltip:'Cap on inter-chip frequency variance. Each alive chip\'s window = [seed_f - SPREAD/2, seed_f + SPREAD/2] centered on the Phase V winner; UP and DOWN moves clamp at those bounds. Dead chips (parked at Dead Chip Freq) are excluded from the spread metric. Default 40. Must be >= 2 × Chip Tune Step. Wider = chips have more headroom to explore at the cost of more per-chip variance; narrower = tighter fleet but caps fast chips down.'},
  CHIP_TUNE_STEP_MHZ:    {label:'Chip Tune Step (MHz)',       type:'number', step:'0.125', requires: 'supports_per_chip_tuning', tooltip:'MHz per per-chip move (UP or DOWN) in the Phase 3 iterative loop. Symmetric — same step in both directions, no S19-style asymmetry that drove the death spiral. Auto-snapped to a multiple of 3.125 (firmware grid). Default 6.25 (two grid cells). Smaller = finer convergence but more rounds; larger = faster but coarser final freqs.'},
  CHIP_TUNE_UP_TOLERANCE:{label:'Chip Tune Up Tolerance (pts)', type:'number', requires: 'supports_per_chip_tuning', tooltip:'Health-score points below baseline a chip is allowed and still counted as STABLE (eligible to step UP). Default 5. Smaller = more conservative on increases (only step UP if very close to baseline); larger = more aggressive. Must satisfy Up <= Down.'},
  CHIP_TUNE_DOWN_TOLERANCE:{label:'Chip Tune Down Tolerance (pts)', type:'number', requires: 'supports_per_chip_tuning', tooltip:'Health-score points below baseline that triggers a DOWN step. Below this is unstable. Default 15. The hold band between Up Tolerance and Down Tolerance (10 health points wide at defaults) absorbs sample noise — chips inside it neither step up nor down. Must satisfy Up <= Down.'},
  CHIP_TUNE_STILLNESS_STREAK:{label:'Chip Tune Stillness Streak', type:'number', requires: 'supports_per_chip_tuning', tooltip:'Consecutive zero-move rounds required before the iterative loop declares done. Default 2. A single zero-move round could be coincidental sample noise (every alive chip happened to land in its hold zone). Higher = more conservative termination; 1 = exit at the first quiet round.'},
  FREQ_SEARCH_TOLERANCE_MHZ:{label:'Cell-Match Tolerance (MHz)',type:'number', requires: 'supports_per_chip_tuning', tooltip:'Tolerance (in MHz) used to match voltage_results entries against fine/coarse surface cells, both for "is this cell already chip-tuned?" deduplication and the dashboard cell-popup before/after lookup. NOT a convergence threshold (the iterative loop has no notion of one). Default 7 MHz. Larger = more lenient matching (a chip-tune at 412 MHz counts as the cell at 405 MHz); smaller = stricter.'},
  DEAD_CHIP_FREQ:        {label:'Dead Chip Freq (MHz)',       type:'number', requires: 'supports_per_chip_tuning', tooltip:'Frequency to park dead chips at (ones whose averaged baseline health score is at or below Dead Chip Score). Default 50. Firmware minimum is ~50 MHz. Parked chips are excluded from the iterative loop entirely — they never enter the spread cap calculation or get health-evaluated.'},
  DEAD_CHIP_SCORE:       {label:'Dead Chip Score',            type:'number', step:'0.1', requires: 'supports_per_chip_tuning', tooltip:'Health-score cutoff (0-100 range) below which a chip is considered dead. Default 1.0. Detection happens once per voltage step, at the end of baseline collection, using the averaged Phase 2 score. A low score during the iterative loop counts as instability (DOWN branch), not deadness — a chip that was alive at baseline stays alive for the step.'},
  MAX_PROFILING_ROUNDS:  {label:'Max Profiling Rounds',       type:'number', requires: 'supports_per_chip_tuning', tooltip:'Integer hard cap on Phase 3 profiling rounds per voltage step. Default 60 (~5 hr at default cadence). The iterative loop normally converges in 10-20 rounds via the stillness streak; this cap only fires on pathological hardware that won\'t settle. Operators can raise to 1000 for genuinely flaky silicon.'},
  // Baseline
  BASELINE_VOLTAGE_MV:   {label:'Baseline Voltage (mV)',      type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Voltage at which Phase 2 collects the reference health baseline. Default 15100 mV — intentionally high-margin so the reference score reflects healthy silicon, not stress.'},
  BASELINE_FREQ:         {label:'Baseline Frequency (MHz)',   type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Frequency at which Phase 2 collects the reference health baseline. Default 200 MHz.'},
  BASELINE_SAMPLES:      {label:'Baseline Samples',           type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Integer count of health samples collected for the Phase 2 reference. Default 20 (≈10 min at 30 s interval). 40+ = tighter reference but slower startup; 10 = minimal.'},
  BASELINE_INTERVAL:     {label:'Baseline Interval (s)',      type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds between Phase 2 baseline samples. Default 30. Firmware updates /hashrate every ~10 s — values below 10 s alias (sample twice per underlying update).'},
  STABILIZE_WAIT:        {label:'Stabilize Wait (s)',         type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds the tuner waits for chips to thermally stabilize after any voltage or frequency change, before sampling health. Default 120. Used by Phase 2 baseline, Phase 3 between-round transitions, and mid-Phase-3 resume.'},
  ROUND_SAMPLES:         {label:'Round Samples',              type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Integer count of health samples per Phase 3 profiling round. Default 20 (same as baseline). 10 = faster rounds but noisier scores; 30+ = slower but steadier.'},
  ROUND_INTERVAL:        {label:'Round Interval (s)',         type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds between per-round health samples. Default 30 (match Baseline Interval to keep score comparisons apples-to-apples).'},
  STOCK_BASELINE_SAMPLES:{label:'Stock Baseline Samples',     type:'number', tooltip:'Integer count of summary samples averaged in Phase 0 to capture the miner\'s pre-tune steady state (hashrate / power / per-chip freqs+health+temps). Default 5. Lower values risk capturing a still-ramping miner; higher values add startup latency.'},
  STOCK_BASELINE_INTERVAL:{label:'Stock Baseline Interval (s)', type:'number', tooltip:'Seconds between Phase 0 stock baseline samples. Default 40. Total capture window = (Stock Baseline Samples − 1) × this interval (≈160 s at defaults). Set higher for miners that take longer to stabilize after reboot; lower for faster startup.'},
  SKIP_ROUND_RESTART:    {label:'Skip round restart',         type:'checkbox', requires: 'supports_per_chip_tuning', tooltip:'Skips the stop/start miner cycle between Phase 3 iterative-loop rounds. Faster (~5-10 min saved per round, ~2 hr total over a 20-round tune), but unstable chips don\'t get a clean chip-state reset — convergence may take more rounds or produce lower final freqs on noisy silicon. Default off (keep restarts).'},
  // Settle (the ePIC firmware handles chip-by-chip freq ramping internally —
  // no tuner-side ramp config lives here anymore.)
  SETTLE_POLL_INTERVAL:  {label:'Settle Poll Interval (s)',   type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds between /summary polls while waiting for Output Voltage to reach target. Default 30. 10-15 = faster detection on responsive PSUs but 2-3× the API traffic.'},
  SETTLE_MAX_ATTEMPTS:   {label:'Settle Max Attempts',        type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Integer cap on settle polls before giving up. Default 20. Total timeout = Max Attempts × Settle Poll Interval (default 20 × 30 s = 10 min). Raising this tolerates slow PSUs; lowering catches stuck voltage faster.'},
  SETTLE_VOLTAGE_TOLERANCE_MV:{label:'Settle V Tolerance (mV)', type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'How close Output Voltage must be to the target (mV) before voltage is considered settled. Default 500 mV. PSUs rarely hit exact targets — loosen this if /summary reports 200-400 mV offsets even at steady state.'},
  // Phase V: 2D (voltage, uniform-frequency) efficiency exploration.
  START_VOLTAGE_MV:      {label:'Start Voltage (mV, 0=auto)', type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Lower bound for the PSU voltage used in all phases (Phase V\'s grid bottom AND Phase 6\'s voltage-adjuster floor). Default 0 = auto-detect PSU minimum from /capabilities.'},
  SWEEP_OVER_STOCK_MV:   {label:'Start Offset vs Stock (mV)', type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Offset added to stock voltage to pick Phase V\'s top-V. Default 0 (start exactly at stock). Negative = skip the top of the efficiency curve; positive = include V above stock. Top-V is clamped to the PSU max.'},
  VF_EXPLORE_V_COUNT:    {label:'V/F: voltage count',         type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Number of voltages in the Phase V coarse grid (range: stock+SWEEP_OVER_STOCK_MV → START_VOLTAGE_MV). Default 5. Bounds 3-20. Denser = better minimum detection, but each extra voltage costs ~(F_count × WAIT) seconds.'},
  VF_EXPLORE_F_MIN:      {label:'V/F: freq min (MHz)',        type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Lower bound of the Phase V uniform-frequency grid. Default 400. Keep it above the firmware minimum (50 MHz) with a realistic margin — a too-low min wastes grid points in a known-unstable region.'},
  VF_EXPLORE_F_MAX:      {label:'V/F: freq max (MHz)',        type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Upper bound of the Phase V uniform-frequency grid. Default 575. For BTC-economics default chips, exploring above ~580 MHz usually crashes the miner.'},
  VF_EXPLORE_F_COUNT:    {label:'V/F: freq count',            type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Number of frequencies in the Phase V coarse grid. Default 5. Bounds 3-20. Each value is snapped to the 3.125 MHz firmware grid.'},
  VF_EXPLORE_WAIT:       {label:'V/F: stabilize wait (s)',    type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds to wait at each (V, F) point before sampling. Default 90. Lower = faster grid but may sample before chips stabilize; higher = more reliable per-point measurements.'},
  VF_EXPLORE_SAMPLES:    {label:'V/F: samples per point',     type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'J/TH samples collected per Phase V grid point. Default 3. Bounds 1-20. Higher = tighter measurement at each point but longer runtime.'},
  VF_EXPLORE_SAMPLE_INTERVAL:{label:'V/F: sample interval (s)', type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds between per-point samples. Default 5. Firmware updates /summary every ~5-10 s — below that aliases.'},
  VF_EXPLORE_FINE_COUNT: {label:'V/F: fine grid dim',         type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Dimension of the optional N×N fine grid around each top-fine coarse anchor. Allowed values: 0 (disabled, default), 3, 5, 9, 25, 49 — odd squares so the anchor sits at the center of the grid for interior anchors. Adds ~((N²-1) × per-point time) to total runtime per top-fine anchor (the anchor cell itself is reused, not re-measured). The original coarse measurement is converted in place to the anchor cell of the fine grid. For corner/edge anchors the grid shifts/compresses inward to fit inside the global VF bounds; spacing is smaller than for interior anchors.'},
  VF_FINE_TOP_K:         {label:'V/F: fine top-K',             type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Number of top coarse cells (by current J/TH or $/day) that get fine-gridded. Default 3. Bounds 1-50. The dynamic state machine never starts fine-gridding until every one of the top-VF_COARSE_TOP_K_RAYS cells has had its 8-ray walk completed, then fine-grids exactly the top-N. If profit/J-TH ranking shifts (e.g. minerstat update), the next iteration may start fine-gridding a different anchor.'},
  VF_EXPLORE_TOP_K:      {label:'V/F: chip-tune top-K',         type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Number of fine cells (or coarse cells, when fine grids are disabled) selected for atomic Phase 3 + Phase 3b + Phase 4 chip-tuning. Default 1 (just the best). Candidates are pulled from inside the top-VF_FINE_TOP_K coarse anchors\' fine grids; ranked by current scoring; ties broken by insertion order. Each extra cell adds ~30-60 min of chip-tune time.'},
  VF_EXPLORE_TREND_CONFIRM:{label:'V/F: trend confirm (N)',  type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Consecutive points worse than the current best J/TH (or $/day) needed to stop expanding a ray direction. Default 2. Bounds 1-10. The dynamic loop\'s find_next_coarse logic walks rays from each top-VF_COARSE_TOP_K_RAYS coarse cell until a direction either hits the grid edge or trend-stops here. Lower = more aggressive pruning; higher = more conservative.'},
  VF_COARSE_TOP_K_RAYS:  {label:'V/F: coarse top-K rays',      type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Number of top coarse cells (by current scoring) the dynamic loop walks 8 rays from on every iteration of "find next coarse cell". Default 1 (rays from the global best only). 2-50 = walk rays from runners-up too — guards against a single noisy outlier at the winner stopping exploration early.'},
  STABILITY_POLISH_ROUNDS:{label:'Polish: rounds',            type:'number', requires: 'supports_per_chip_tuning', tooltip:'Phase 3b (stability polish) round cap. The Phase 3 iterative loop terminates on per-round health snapshots that may be too short to catch slow drift; Phase 3b uses a longer dedicated sample window and drops any chip whose averaged health falls below baseline by more than Chip Tune Down Tolerance. Decrement-only — never raises a chip\'s freq. Rounds with zero drops exit early. Default 3. 0 disables the phase entirely.'},
  STABILITY_POLISH_STEP_MHZ:{label:'Polish: step (MHz)',      type:'number', step:'0.125', requires: 'supports_per_chip_tuning', tooltip:'MHz drop per unstable chip per Phase 3b polish round. Default 6.25 (two 3.125 MHz firmware grid cells). Auto-snapped to a multiple of 3.125. Smaller = finer corrective pass but more rounds needed; larger = faster but more hashrate lost per correction.'},
  STABILITY_POLISH_ROUND_SAMPLES:{label:'Polish: samples per round', type:'number', requires: 'supports_per_chip_tuning', tooltip:'Health samples collected per Phase 3b polish round. Default 40 (2× the Phase 3 Round Samples) — the longer sample window is what lets polish catch slow drift Phase 3\'s shorter window misses. Higher = more reliable drift detection but slower per round.'},
  STABILITY_POLISH_ROUND_INTERVAL:{label:'Polish: sample interval (s)', type:'number', requires: 'supports_per_chip_tuning', tooltip:'Seconds between Phase 3b polish samples. Default 30 (matches Round Interval). Total per-round sampling time = Polish Samples × Polish Sample Interval. Firmware updates /hashrate every ~10 s — values below 10 s alias.'},
  STABILITY_POLISH_STABILIZE_WAIT:{label:'Polish Stabilize Wait (s)', type:'number', requires: 'supports_per_chip_tuning', tooltip:'Seconds to thermally stabilize before sampling each Phase 3b polish round. Defaults to a longer window than the shared STABILIZE_WAIT so polish can stress-test silicon over a long, unbroken interval. Default 300.'},
  EFFICIENCY_MEASURE_WAIT:{label:'Efficiency Measure Wait (s)',type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Seconds the tuner waits at final tuned frequencies before averaging Phase 4 efficiency samples. Default 120. Ensures chip temps and hashrate have stabilized so J/TH is accurate.'},
  // Thermal limits
  BOARD_MAX_TEMP:        {label:'Board Max Temp (C)',         type:'number', tooltip:'Board temperature in C that triggers emergency throttling of every chip on that board. Conservative default 82. Verify the shutdown threshold for the exact miner and keep a safety margin below it.'},
  CHIP_CRITICAL_TEMP:    {label:'Chip Critical Temp (C)',     type:'number', tooltip:'Per-chip temperature in C that triggers throttling for that chip only. Conservative default 97. Verify the silicon and firmware limits for the exact model before changing it.'},
  FREQ_STEP_EMERGENCY:   {label:'Freq Step Emergency (MHz)',  type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'MHz decrease applied each time a chip or board crosses its temp limit. Default 20. Larger = faster cooling but more hashrate lost per event; smaller = more gradual, potentially multiple throttle events per incident.'},
  // Perpetual tune
  PERPETUAL_VOLTAGE_CHECK_MIN:{label:'Voltage Check (min)',   type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Minutes between Phase 6 voltage adjuster evaluations. Default 10. 5 = more reactive to hashrate drift; 30 = calmer, averages over longer windows. Each evaluation reads /hashrate/history and decides whether to nudge voltage.'},
  PERPETUAL_VOLTAGE_STEP_MV:{label:'Voltage Step (mV)',       type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'mV per Phase 6 voltage adjustment (one step per evaluation cycle). Default 50. 25 = gentler tracking; 100 = faster recovery from drift but more PSU wear.'},
  PERPETUAL_VOLTAGE_MAX_DELTA_MV:{label:'Max Delta (mV)',     type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Maximum ± mV Phase 6 can move from the sweep-profile voltage before triggering a rate-limited miner restart. Default 300. Must be >= Voltage Step.'},
  PERPETUAL_HASHRATE_DEADBAND_PCT:{label:'Hashrate Deadband (%)', type:'number', step:'0.1', requires: 'voltage_chip_tune_strategy', tooltip:'Hashrate % deadband around the sweep profile\'s measured TH/s. Inside the band Phase 6 makes NO voltage change. Default 0.5. 0.1 = very twitchy; 2 = tolerates large drift before acting.'},
  PERPETUAL_RESTART_MIN_HOURS:{label:'Restart Min (hrs)',     type:'number', requires: 'voltage_chip_tune_strategy', tooltip:'Minimum hours between miner restarts triggered by Phase 6 voltage saturation. Default 24. Prevents restart thrashing on a miner that saturates at +max delta repeatedly. 1-8760 hrs allowed.'},
  // Resilience & recovery
  MAX_CONSECUTIVE_RETRIES:{label:'Max Consecutive Retries',   type:'number', tooltip:'Integer. Consecutive non-offline recovery attempts before the tuner gives up and enters PHASE_ERROR. Default 5. Resets to 0 after any successful run > 5 min (so periodic chainbreaks never exhaust the budget over days).'},
  RESET_STOP_WAIT:       {label:'Reset Stop Wait (s)',        type:'number', tooltip:'Seconds to wait after sending stop_mining before the next tuner phase runs. Default 30. Gives the miner time to cleanly halt.'},
  RESET_START_WAIT:      {label:'Reset Start Wait (s)',       type:'number', tooltip:'Seconds to wait after sending start_mining for the chips to come back online and report usable /hashrate. Default 300. The miner sometimes needs several minutes before per-chip data is accurate.'},
  OFFLINE_POLL_INTERVAL: {label:'Offline Poll Interval (s)',  type:'number', tooltip:'Seconds between reconnect polls while the tuner is paused in PHASE_OFFLINE. Default 30. Lower = faster resume detection, higher = lighter network traffic. Bounds 10-31536000.'},
  OFFLINE_FAILURE_THRESHOLD:{label:'Offline Failure Threshold',type:'number', tooltip:'Integer count of back-to-back connection failures required before the tuner flips to PHASE_OFFLINE. Default 3 (≈30 s of failures). 1 = flip offline on the first failed API call (twitchy — single packet loss can trigger); 5-10 = absorb longer network glitches silently.'},
  // Profitability Mode
  TARGET_MODE:           {label:'Tuning Target',               type:'select', options:['efficiency','profitability'], tooltip:'What the tuner optimizes for. "efficiency" (default) ranks cells by J/TH (lower = better, classic behavior). "profitability" ranks cells by $/day using the minerstat snapshot + Electric Rate. Per-miner setting — mixed-mode fleets are OK. Changes take effect on the next tune / next retune / next profit recompute (whichever comes first); existing measurements are reused.'},
  ELECTRIC_RATE_PER_KWH: {label:'Electric Rate ($/kWh)',       type:'number', step:'0.001', tooltip:'$/kWh paid for power. Used only in profit mode to compute each cell\'s $/day. Default 0.10 (generic US residential). Commercial mining hosts usually pay 0.045-0.09. Per-miner so different feeds/phases can carry different rates.'},
  MINERSTAT_COIN:        {label:'Minerstat Coin',              type:'text',   tooltip:'Coin ID used to look up price/network-hashrate/reward from the minerstat snapshot. Default "BTC". Fleet-wide: one snapshot is shared across all miners in profit mode so the API call budget is bounded. Edited on the Minerstat card\'s settings button (overview only).'},
  MINERSTAT_POLL_DAY:    {label:'Auto-poll Day (1-28, 0=off)', type:'number', tooltip:'Day of month the tuner auto-fetches minerstat and applies profit recompute to all profit-mode miners. 0 disables auto-polling (manual button only). Set to your electric-billing cycle reset day so any voltage increase hits at the start of a fresh demand-charge window rather than mid-month. Fleet-wide.'},
  MINERSTAT_API_KEY:     {label:'Minerstat API Key',            type:'password', tooltip:'Minerstat API key — required for every /v2/coins fetch (minerstat no longer offers unauthenticated free tier). Get one at api.minerstat.com by connecting a Developer account. The key permits 100 calls/day — plenty for one monthly auto-poll plus a few manual "Fetch & auto-apply" clicks. Fleet-wide.'},
  // MRR per-miner fields. Fleet-level MRR_ENABLED / MRR_API_KEY /
  // MRR_API_SECRET / MRR_HASHRATE_UNIT live on the overview's MRR card's
  // settings modal and are NOT rendered on the per-miner tab. MRR_RIG_ID is
  // strictly per-miner (0 = skip MRR for this miner); MRR_HASHRATE_MODIFIER_PCT
  // overrides the fleet default. An empty MRR_HASHRATE_MODIFIER_PCT field
  // (null) drops the per-miner override, falling back to the fleet default.
  MRR_RIG_ID:            {label:'MRR Rig ID',                    type:'number', placeholder:'0 = not configured', tooltip:'MiningRigRentals rig ID this miner maps to. Use the "⚙ Pick from my rigs" button to populate from your MRR account, or paste an ID. 0 = skip MRR auto-publish for this miner. Per-miner only — each miner has its own MRR rig.'},
  MRR_HASHRATE_MODIFIER_PCT:{label:'MRR Hashrate Modifier (%)',  type:'number', step:'0.1', placeholder:'inherit fleet default', tooltip:'Multiplicative percentage applied to sweep_hashrate_ths before advertising to MRR. advertised = sweep_hashrate_ths × (1 + modifier/100). Positive = advertise above measured (common when rig over-delivers); negative = conservative haircut. Leave blank to inherit the fleet default from the Overview MRR card.'},
  MRR_PUBLISH_DURING_POLISH:{label:'MRR: publish during polish', type:'bool',   tooltip:"When enabled, the engine fires mrr_sync('maintaining') at the start of Phase 3b stability polish IN ADDITION to the existing Phase 6 (perpetual) entry sync. Useful when long-polishing miners should be advertised on MRR before they reach steady state. Note: polish decrements per-chip frequencies, so the advertised hashrate at polish entry is a pre-polish snapshot — typically <2% over-stated; the perpetual-entry sync re-publishes the corrected hashrate later. Fleet-only."},
  // Fleet-network fields — fleet-only (see CONFIG_CATEGORIES fleetOnly flag).
  // Rendered on the overview defaults accordion and collected from the Add
  // Miner modal for PASSWORD; never in the per-miner config tab.
  MINER_IPS:             {label:'Miner IPs (comma-sep)',      type:'text',   tooltip:'Comma-separated list of miner IPs the tuner manages. Managed via the add/remove buttons on the overview — this field on the defaults accordion is informational.'},
  API_PORT:              {label:'API Port',                   type:'number', tooltip:'Miner HTTP API port. Default 4028 (ePIC UMC). Only change if firmware has been reconfigured. Fleet-wide.'},
  SOURCE_IP:             {label:'Source IP (optional)',       type:'text',   placeholder:'auto-detect', tooltip:'Local interface IP to bind outgoing connections to. Leave blank = auto-detect (probes interfaces on connection failure). Set this only if you\'re multi-homed (Wi-Fi + Ethernet, VPN) and the OS routes the wrong way. Fleet-wide.'},
  POWER_LIMIT_W:         {label:'Power Limit (W)',            type:'number', requires: 'has_external_power_limit', tooltip:'Maximum power draw in Watts the Bixbit, LuxOS, and Whatsminer (Stock) firmware enforce on this miner. Range 1500–6000 W. Default 3500. Not applicable to ePIC firmware (set_power_limit is a no-op on ePIC; this field is dimmed on ePIC miners). Braiins OS uses its own wattage-search algorithm and ignores this knob. Fleet-wide — one cap applies to all Bixbit, LuxOS, and Whatsminer (Stock) miners in the fleet. Also acts as the upper bound of the grid-search power axis on Whatsminer stock firmware.'},
  // Braiins OS firmware knobs — binary-search wattage tuner.
  BRAIINS_POWER_MIN_W:   {label:'Braiins Power Min (W)',      type:'number', requires: 'wattage_search_strategy', tooltip:'Lower bound for the wattage binary-search algorithm on Braiins miners. Range 500–6000 W. Default 1500. Must be < Braiins Power Max.'},
  BRAIINS_POWER_MAX_W:   {label:'Braiins Power Max (W)',      type:'number', requires: 'wattage_search_strategy', tooltip:'Upper bound for the wattage binary-search algorithm on Braiins miners. Range 500–6000 W. Default 5000. Must be > Braiins Power Min.'},
  BRAIINS_TUNER_STABILIZE_WAIT_SEC: {label:'Braiins Stabilize Wait (s)', type:'number', requires: 'wattage_search_strategy', tooltip:'Seconds to wait at each wattage point for the BOS internal V/F tuner to settle before sampling. Range 60–31536000. Default 600 (10 min). Longer = more stable measurements but slower convergence.'},
  BRAIINS_BINARY_SEARCH_TOLERANCE_W: {label:'Braiins Search Tolerance (W)', type:'number', requires: 'wattage_search_strategy', tooltip:'Wattage range size at which the binary search converges. Range 10–500 W. Default 100. When (high - low) <= tolerance, the search picks the recorded sample with highest profit and enters perpetual mode.'},
  BRAIINS_USERNAME:      {label:'Braiins Username',           type:'text',   requires: 'wattage_search_strategy', tooltip:'Login username for the Braiins OS REST API on this miner. Default "root". Per-miner overrideable.'},
  // Whatsminer (stock MicroBT) firmware knobs — 2D power_limit × target_freq grid-search tuner.
  WHATSMINER_PL_MIN_W:   {label:'Whatsminer Power Limit Min (W)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Lower bound for the power-limit axis in the Whatsminer 2D grid-search algorithm. Range 500–6000 W. Default 1500. Must be < POWER_LIMIT_W.'},
  WHATSMINER_PL_COUNT:   {label:'Whatsminer Power Limit Count',   type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Number of power-limit grid points to sample between Min and Max (inclusive). Range 3–10. Default 5.'},
  WHATSMINER_FREQ_MIN_MHZ: {label:'Whatsminer Freq Min (MHz)',     type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Lower bound for the target-frequency axis. Range 200–900 MHz. Default 400. Must be < Whatsminer Freq Max.'},
  WHATSMINER_FREQ_MAX_MHZ: {label:'Whatsminer Freq Max (MHz)',     type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Upper bound for the target-frequency axis. Range 200–900 MHz. Default 700. Must be > Whatsminer Freq Min.'},
  WHATSMINER_FREQ_COUNT: {label:'Whatsminer Freq Count',         type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Number of frequency grid points between Min and Max (inclusive). Range 3–10. Default 5.'},
  WHATSMINER_FINE_COUNT: {label:'Whatsminer Fine Count',         type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Side length of the NxN fine grid placed around each top-K coarse anchor. 0 disables the fine pass. Range 0–5. Default 3.'},
  WHATSMINER_FINE_TOP_K: {label:'Whatsminer Fine Top-K',         type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Number of coarse-grid winners to refine with a fine pass. 0 disables the fine pass. Range 0–5. Default 2.'},
  WHATSMINER_STABILIZE_SEC: {label:'Whatsminer Stabilize (s)',    type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Seconds to wait after upfreq complete before sampling each cell. Range 10–600. Default 60.'},
  WHATSMINER_RESTART_WAIT_SEC: {label:'Whatsminer Restart Wait (s)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Seconds to wait for the miner to come back online after a power_mode change (which triggers a restart). Range 10–600. Default 90.'},
  WHATSMINER_UPFREQ_TIMEOUT_SEC: {label:'Whatsminer Upfreq Timeout (s)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Maximum seconds to wait for all chips to report Upfreq Complete before giving up. Range 30–28800 (8 hr). Default 180. Higher values let chip-tuning-during-upfreq complete on miners that need hours; the poll loop honors stop signals in 1 s slices so a long timeout does not block engine shutdown.'},
  WHATSMINER_SAMPLE_WINDOW_SEC: {label:'Whatsminer Sample Window (s)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Total sampling window per cell. Range 10–600. Default 60.'},
  WHATSMINER_SAMPLE_INTERVAL_SEC: {label:'Whatsminer Sample Interval (s)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Interval between consecutive summary samples within the window. Range 1–60. Default 10.'},
  WHATSMINER_BASELINE_SAMPLES: {label:'Whatsminer Baseline Samples', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Number of summary samples taken at each mode (low/normal/high) during discovery to estimate the baseline. Range 1–30. Default 5.'},
  WHATSMINER_PERPETUAL_INTERVAL_SEC: {label:'Whatsminer Perpetual Interval (s)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Seconds between perpetual-phase re-samples at the best cell. Range 60–86400 (1 min – 1 day). Default 300 (5 min).'},
  WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT: {label:'Whatsminer Drift Threshold (%)', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Efficiency drift percentage that re-triggers a full pass. Two consecutive perpetual samples beyond this threshold reset the search. Range 0.5–50.0. Default 5.0.'},
  WHATSMINER_UPFREQ_SPEED: {label:'Whatsminer Upfreq Speed', type:'number', requires: 'power_limit_freq_search_strategy', tooltip:'Whatsminer firmware-internal upfreq speed setting (1 = slowest, 10 = fastest). Range 1–10. Default 5.'},
};

// Categories with `fleetOnly: true` render only on the overview defaults
// accordion — hidden from the per-miner config tab and the bulk-apply modal.
// Minerstat settings have their own dedicated modal (gear button on the
// Minerstat card), so they're not in CONFIG_CATEGORIES at all.
const CONFIG_CATEGORIES = [
  {name:'Baseline', keys:['BASELINE_VOLTAGE_MV','BASELINE_FREQ','BASELINE_SAMPLES','BASELINE_INTERVAL','STABILIZE_WAIT','ROUND_SAMPLES','ROUND_INTERVAL','STOCK_BASELINE_SAMPLES','STOCK_BASELINE_INTERVAL']},
  {name:'Voltage Settle', keys:['SETTLE_POLL_INTERVAL','SETTLE_MAX_ATTEMPTS','SETTLE_VOLTAGE_TOLERANCE_MV']},
  {name:'V/F Exploration (dynamic state machine)', keys:['START_VOLTAGE_MV','SWEEP_OVER_STOCK_MV','VF_EXPLORE_V_COUNT','VF_EXPLORE_F_MIN','VF_EXPLORE_F_MAX','VF_EXPLORE_F_COUNT','VF_EXPLORE_WAIT','VF_EXPLORE_SAMPLES','VF_EXPLORE_SAMPLE_INTERVAL','VF_COARSE_TOP_K_RAYS','VF_EXPLORE_TREND_CONFIRM','VF_EXPLORE_FINE_COUNT','VF_FINE_TOP_K','VF_EXPLORE_TOP_K','EFFICIENCY_MEASURE_WAIT']},
  {name:'Per-Chip Tune (Phase 3 iterative loop)', keys:['CHIP_FREQ_SPREAD_MHZ','CHIP_TUNE_STEP_MHZ','CHIP_TUNE_UP_TOLERANCE','CHIP_TUNE_DOWN_TOLERANCE','CHIP_TUNE_STILLNESS_STREAK','MAX_PROFILING_ROUNDS','DEAD_CHIP_FREQ','DEAD_CHIP_SCORE','FREQ_SEARCH_TOLERANCE_MHZ','SKIP_ROUND_RESTART']},
  {name:'Phase 3b: Stability Polish', keys:['STABILITY_POLISH_ROUNDS','STABILITY_POLISH_STEP_MHZ','STABILITY_POLISH_ROUND_SAMPLES','STABILITY_POLISH_ROUND_INTERVAL','STABILITY_POLISH_STABILIZE_WAIT']},
  {name:'Perpetual Tune', keys:['PERPETUAL_VOLTAGE_CHECK_MIN','PERPETUAL_VOLTAGE_STEP_MV','PERPETUAL_VOLTAGE_MAX_DELTA_MV','PERPETUAL_HASHRATE_DEADBAND_PCT','PERPETUAL_RESTART_MIN_HOURS']},
  {name:'Profitability Mode', keys:['TARGET_MODE','ELECTRIC_RATE_PER_KWH']},
  {name:'Thermal Limits', keys:['BOARD_MAX_TEMP','CHIP_CRITICAL_TEMP','FREQ_STEP_EMERGENCY']},
  // 'Power' and 'Wattage Search' are per-platform in v3 — POWER_LIMIT_W and
  // BRAIINS_* knobs were removed from FLEET_OPS_KEYS in the v3 schema split.
  // Each entry carries a `requires:` capability filter so it only renders on
  // platforms where the firmware actually consumes the knob (e.g. POWER_LIMIT_W
  // is dimmed on ePIC's set_power_limit no-op path).
  {name:'Power', keys:['POWER_LIMIT_W']},
  {name:'Power Limit / Frequency Search', keys:['WHATSMINER_PL_MIN_W','WHATSMINER_PL_COUNT','WHATSMINER_FREQ_MIN_MHZ','WHATSMINER_FREQ_MAX_MHZ','WHATSMINER_FREQ_COUNT','WHATSMINER_FINE_COUNT','WHATSMINER_FINE_TOP_K','WHATSMINER_STABILIZE_SEC','WHATSMINER_RESTART_WAIT_SEC','WHATSMINER_UPFREQ_TIMEOUT_SEC','WHATSMINER_SAMPLE_WINDOW_SEC','WHATSMINER_SAMPLE_INTERVAL_SEC','WHATSMINER_BASELINE_SAMPLES','WHATSMINER_PERPETUAL_INTERVAL_SEC','WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT','WHATSMINER_UPFREQ_SPEED']},
  {name:'Wattage Search', keys:['BRAIINS_POWER_MIN_W','BRAIINS_POWER_MAX_W','BRAIINS_TUNER_STABILIZE_WAIT_SEC','BRAIINS_BINARY_SEARCH_TOLERANCE_W','BRAIINS_USERNAME']},
  {name:'Resilience & Recovery', keys:['MAX_CONSECUTIVE_RETRIES','RESET_STOP_WAIT','RESET_START_WAIT','OFFLINE_POLL_INTERVAL','OFFLINE_FAILURE_THRESHOLD']},
  // `hideFromDefaults` — category shows on per-miner tab + bulk-apply but
  // NOT on the overview defaults accordion. MRR fleet defaults
  // (MRR_HASHRATE_MODIFIER_PCT) are on the MRR card's settings modal; only
  // the per-miner override belongs on the config tab. MRR_RIG_ID is strictly
  // per-miner (no meaningful fleet default).
  {name:'MiningRigRentals', hideFromDefaults:true, keys:['MRR_RIG_ID','MRR_HASHRATE_MODIFIER_PCT']},
  {name:'MRR Fleet Options', fleetOnly:true, keys:['MRR_PUBLISH_DURING_POLISH']},
  {name:'Fleet Network', fleetOnly:true, hideFromDefaults:true, keys:['MINER_IPS','API_PORT','SOURCE_IP']},
];

// Flat list of keys the per-miner config tab and bulk-apply modal render +
// save. Excludes fleet-only categories entirely (Connection fields never
// appear per-miner) and the three special-handled keys that have their own
// DOM handlers on the defaults accordion. MINERSTAT_* dropped here too — they
// live on the Minerstat card's settings modal.
const CFG_KEYS = CONFIG_CATEGORIES
  .filter(c => !c.fleetOnly)
  .flatMap(c => c.keys)
  .filter(k => !['MINER_IPS','SOURCE_IP','PASSWORD'].includes(k));

// All iterable keys for the defaults accordion (which edits everything except
// the three special-handled keys above). Includes fleet-only categories,
// excludes categories marked hideFromDefaults (MRR — handled via its card
// modal + per-miner tab).
// Kept for any internal callers that need the union of platform + fleet-ops keys.
const CFG_KEYS_DEFAULTS = CONFIG_CATEGORIES
  .filter(c => !c.hideFromDefaults)
  .flatMap(c => c.keys)
  .filter(k => !['MINER_IPS','SOURCE_IP','PASSWORD'].includes(k));

// Authoritative frontend mirror of tuner_app/constants.py:FLEET_OPS_KEYS.
// Keys in this set live in cfg.fleet_ops (not cfg.defaults[platform]).
// Platform-agnostic singletons: scanner, MRR fleet creds, network,
// minerstat, logging, and the auth-internal PASSWORD derived key.
const FLEET_OPS_KEYS_FRONTEND = new Set([
  'SCAN_IP_RANGES', 'SCAN_IP_BLACKLIST', 'SCAN_PASSWORDS', 'SCAN_TIMEOUT_SEC',
  'SCAN_CONCURRENCY', 'SCAN_INTERVAL_MIN', 'SCAN_AUTO_REGISTER',
  'MRR_ENABLED', 'MRR_API_KEY', 'MRR_API_SECRET', 'MRR_HASHRATE_UNIT',
  'MRR_STRATUM_USERNAME', 'MRR_COIN', 'MRR_PUBLISH_DURING_POLISH',
  'MINER_IPS', 'SOURCE_IP', 'API_PORT',
  'MINERSTAT_COIN', 'MINERSTAT_POLL_DAY', 'MINERSTAT_API_KEY', 'INCOME_MODIFIER_PCT',
  'LOG_STDOUT_LEVEL', 'LOG_DEDUP_WINDOW_SEC',
  'PASSWORD',
]);

// Per-platform tuning keys: rendered in the defaults accordion's platform
// dropdown form. Excludes fleet-ops keys and keys excluded from the defaults
// view (hideFromDefaults categories like MRR per-miner keys).
const CFG_KEYS_PLATFORM_DEFAULTS = CONFIG_CATEGORIES
  .filter(c => !c.hideFromDefaults)
  .flatMap(c => c.keys)
  .filter(k => !FLEET_OPS_KEYS_FRONTEND.has(k))
  .filter(k => !['MINER_IPS', 'SOURCE_IP', 'PASSWORD'].includes(k));

// Platform capability table for the Fleet Defaults accordion — mirrors backend MinerAPI capability methods.
// Audit-grep-tied to MINER_API_REGISTRY via tests/unit/test_platform_capabilities_consistency.py.
const PLATFORM_CAPABILITIES = {
  epic: {
    supports_per_chip_tuning: true,
    has_external_power_limit: false,
    has_capabilities_endpoint: true,
    has_internal_perpetual_tune: false,
    voltage_chip_tune_strategy: true,
    power_limit_freq_search_strategy: false,
    wattage_search_strategy: false,
  },
  bixbit: {
    supports_per_chip_tuning: false,
    has_external_power_limit: true,
    has_capabilities_endpoint: false,
    has_internal_perpetual_tune: true,
    voltage_chip_tune_strategy: true,
    power_limit_freq_search_strategy: false,
    wattage_search_strategy: false,
  },
  luxos: {
    supports_per_chip_tuning: true,
    has_external_power_limit: true,
    has_capabilities_endpoint: true,
    has_internal_perpetual_tune: true,
    voltage_chip_tune_strategy: true,
    power_limit_freq_search_strategy: false,
    wattage_search_strategy: false,
  },
  braiins: {
    supports_per_chip_tuning: false,
    has_external_power_limit: true,
    has_capabilities_endpoint: false,
    has_internal_perpetual_tune: true,
    voltage_chip_tune_strategy: false,
    power_limit_freq_search_strategy: false,
    wattage_search_strategy: true,
  },
  whatsminer: {
    supports_per_chip_tuning: false,
    has_external_power_limit: true,
    has_capabilities_endpoint: false,
    has_internal_perpetual_tune: true,
    voltage_chip_tune_strategy: false,
    power_limit_freq_search_strategy: true,
    wattage_search_strategy: false,
  },
};
function _escAttr(s){ return String(s).replace(/"/g, '&quot;'); }

// `opts.includeFleetOnly` controls rendering of categories marked fleetOnly:
// the overview defaults accordion passes true (it edits every fleet-wide
// field); the per-miner config tab and the bulk-apply modal leave it false so
// Connection fields never appear where they have no effect.
// `opts.capabilities` (an object like {supports_per_chip_tuning: true, has_external_power_limit: false, ...}) — when provided, CFG_META entries
// whose `requires` property names a capability that is false in opts.capabilities
// are hidden entirely from the rendered form; categories whose every key is so
// hidden are also omitted. Fleet defaults accordion omits capabilities so every
// entry renders.
function buildConfigForm(root, prefix, opts){
  if (!root) return;
  const includeFleet = !!(opts && opts.includeFleetOnly);
  const capabilities = opts && opts.capabilities;
  // includeFleet (per-platform defaults accordion): hide hideFromDefaults but
  // include fleetOnly categories. Exclude fleet-ops keys — those are surfaced
  // via dedicated modals (gear icon, Minerstat ⚙, MRR ⚙), not in this form.
  // Per-miner / bulk-apply: hide categories marked fleetOnly.
  let cats;
  if (includeFleet) {
    cats = CONFIG_CATEGORIES.filter(c => !c.hideFromDefaults && !c.keys.every(k => FLEET_OPS_KEYS_FRONTEND.has(k)));
  } else {
    cats = CONFIG_CATEGORIES.filter(c => !c.fleetOnly);
  }
  const html = cats.map(cat => {
    const rows = cat.keys.map(k => {
      const meta = CFG_META[k];
      if (!meta) return '';
      const id = `${prefix}${k}`;
      const requiresCapability = meta.requires;
      const vendorMismatch = !!(requiresCapability && capabilities && !capabilities[requiresCapability]);
      if (vendorMismatch) return '';
      let tooltip = meta.tooltip || '';
      const tooltipAttr = _escAttr(tooltip);
      const help = tooltip ? `<span class="cfg-help" data-tooltip="${tooltipAttr}">?</span>` : '';
      if (meta.type === 'checkbox') {
        return `<div class="cfg-checkbox-row"><input type="checkbox" id="${id}"><label for="${id}" style="margin:0">${meta.label}${help}</label></div>`;
      }
      if (meta.type === 'select') {
        const selOpts = (meta.options || []).map(o => `<option value="${_escAttr(o)}">${o}</option>`).join('');
        return `<div><label for="${id}">${meta.label}${help}</label><select id="${id}">${selOpts}</select></div>`;
      }
      const step = meta.step ? ` step="${meta.step}"` : '';
      const placeholder = meta.placeholder ? ` placeholder="${_escAttr(meta.placeholder)}"` : '';
      return `<div><label for="${id}">${meta.label}${help}</label><input id="${id}" type="${meta.type}"${step}${placeholder}></div>`;
    });
    const nonEmpty = rows.filter(r => r !== '');
    if (nonEmpty.length === 0) return '';
    return `
      <details class="config-cat" data-cat-name="${_escAttr(cat.name)}">
        <summary>${cat.name}</summary>
        <div class="config-cat-body"><div class="form-row" style="flex-wrap:wrap">${nonEmpty.join('')}</div></div>
      </details>`;
  }).join('');
  root.innerHTML = html;
}

function setFormValue(id, val, type) {
  const el = document.getElementById(id);
  if (!el) return;
  if (type === 'checkbox') {
    el.checked = !!val;
  } else if (val !== undefined && val !== null) {
    el.value = val;
  }
}

function readFormValue(id, type) {
  const el = document.getElementById(id);
  if (!el) return undefined;
  if (type === 'checkbox') return el.checked;
  if (el.value === '') return undefined;
  if (type === 'number') {
    const n = parseFloat(el.value);
    return isNaN(n) ? undefined : n;
  }
  return el.value;
}

// ─── Router ──────────────────────────────────────────────────────────────────
// Hash-based routing keeps the app in a single file. Overview lives at #/ and
// per-miner detail at #/miner/<mac-dashes>. Back/forward buttons work via
// hashchange. The dashed MAC form is canonical in URLs (colons would need
// percent-encoding); the route handler normalizes back to colon form for any
// JS-side comparison against ``currentMiner.mac``.

function _macDashesToColons(s){
  // Synth IDs stay dashed; "syn-..." passthrough. Real MACs are
  // exactly 17 chars (12 hex + 5 dashes); convert dashes to colons.
  if (!s) return '';
  if (s.startsWith('syn-')) return s;
  return s.replace(/-/g, ':');
}

function parseHash(){
  const h = location.hash || '#/';
  const m = h.match(/^#\/miner\/(.+)$/);
  if (!m) return { view: 'overview' };
  const raw = decodeURIComponent(m[1]);
  return { view: 'detail', mac: _macDashesToColons(raw) };
}

function showView(name){
  ['login','overview','detail'].forEach(v => {
    const el = document.getElementById('view-'+v);
    if (el) el.style.display = 'none';
  });
  const target = document.getElementById('view-'+name);
  if (target) target.style.display = (name === 'login' ? 'flex' : '');
}

function resetDetailView(){
  // Fully reset per-miner DOM + JS state so switching miners doesn't bleed
  // state (voltage-results, stock/tuned, phase, log, heatmap) from the old one.
  Object.values(charts).forEach(c => { c.data.labels=[]; c.data.datasets[0].data=[]; c.update(); });
  heatmapData = {
    clocks:null, hashrate:null, chip_temps:null, baseline:null,
    p2_freq:null, p2_temp:null, p2_hashrate:null, stock:null,
  };
  heatmapPreview = null;
  tunerStatus = {};
  currentDetailCapabilities = null;
  // Clear the config form root so it rebuilds with the correct capability filter
  // on next loadConfig call (capabilities may differ between miners).
  const cfgRoot = document.getElementById('config-form-root');
  if (cfgRoot) cfgRoot.innerHTML = '';
  const vr = document.getElementById('voltage-results');
  if (vr) vr.innerHTML = '<div class="stat-row"><span class="stat-label">No voltage sweep data yet</span></div>';
  ['c-hashrate','c-power','c-efficiency'].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = '--';
  });
  const imp = document.getElementById('c-improvement');
  if (imp) { imp.textContent = '--'; imp.className = 'stat-value good'; }
  const log = document.getElementById('log-container');
  if (log) log.innerHTML = '';
  const dots = document.getElementById('phase-dots');
  if (dots) dots.innerHTML = '';
  const pd = document.getElementById('phase-detail');
  if (pd) pd.textContent = 'Idle';
  const banner = document.getElementById('offline-banner');
  if (banner) banner.style.display = 'none';
  const sg = document.getElementById('detail-stats-grid');
  if (sg) sg.classList.remove('offline-muted');
  const rentalEl = document.getElementById('detail-rental-status');
  if (rentalEl) rentalEl.innerHTML = '';
}

function route(){
  if (!authReady) return;
  const parsed = parseHash();
  if (parsed.view === 'detail' && parsed.mac) {
    // Switching miners mid-session: reset all per-miner DOM/JS state so the
    // detail view doesn't show stale data from a different miner.
    if (currentMiner.mac !== parsed.mac) {
      resetDetailView();
    }
    // The IP is filled in from the next /tuner/overview poll (each row
    // carries .mac and .ip); seed from minerList if we already have it
    // so the badge label isn't blank for a flicker.
    const seed = (overviewData && overviewData.miners || [])
      .find(m => m.mac === parsed.mac) || {};
    currentMiner = {
      mac: parsed.mac,
      ip: seed.ip || '',
      hostname: seed.hostname || '',
    };
    const badge = document.getElementById('detail-ip-badge');
    if (badge) {
      badge.textContent = currentMiner.ip || parsed.mac;
      badge.href = currentMiner.ip ? ('http://' + currentMiner.ip + '/') : '#';
    }
    document.getElementById('detail-hostname').textContent = '';
    const rentalSpan = document.getElementById('detail-rental-status');
    if (rentalSpan) rentalSpan.innerHTML = '';
    showView('detail');
    loadConfig();
    drawHeatmap();
    // Sync the range <select> with the persisted choice and seed the
    // charts so navigating to a detail page lands on a populated view
    // instead of empty axes.
    const rangeSelect = document.getElementById('metrics-range');
    if (rangeSelect) rangeSelect.value = currentMetricsRange;
    const customRow = document.getElementById('metrics-range-custom');
    if (customRow) customRow.style.display = (currentMetricsRange === 'custom') ? 'inline-flex' : 'none';
    if (currentMetricsRange !== 'custom') {
      seedChartsFromHistory(parsed.mac, currentMetricsRange);
    }
  } else {
    currentMiner = {mac: '', ip: '', hostname: ''};
    showView('overview');
  }
  poll();
}

// Both forms accepted for the navigation argument: dashed MAC (URL canonical)
// or colon-form MAC. Synth IDs use dashes throughout.
function navigateToDetail(mac){
  const dashed = (mac || '').replace(/:/g, '-');
  location.hash = '#/miner/' + encodeURIComponent(dashed);
}
function navigateToOverview(){ location.hash = '#/'; }
window.addEventListener('hashchange', route);

// ─── Miner list management (used by overview and detail config) ──────────────
async function removeMiner(mac, displayLabel){
  const label = displayLabel || mac;
  if (!confirm(`Remove ${label}?\n\nThis will also delete any saved tuning profile, sweep checkpoint, and stock baseline for this miner. This action cannot be undone.`)) return;
  const resp = await fetchJSON('/tuner/miners/remove', {
    method:'POST', body: JSON.stringify({mac}),
    headers:{'Content-Type':'application/json'}
  });
  if (resp && resp.ok) {
    minerList = resp.miners;
    // If the user removed the miner whose detail page they're on, bounce
    // them back to the overview.
    if (currentMiner.mac === mac) navigateToOverview();
    poll();
  }
}

// Rotates the per-miner firmware password (POST /tuner/config/miner/{mac}).
// The only per-miner password surface in the UI — the config tab no longer
// carries a PASSWORD field since the connection detail is collected at Add
// Miner time. Empty submit drops the override so the miner falls back to the
// fleet default PASSWORD.
function changeMinerPassword(){
  if (!currentMiner.mac) return;
  const label = currentMiner.ip || currentMiner.mac;
  openModal(`Change password — ${label}`, `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.85em">
      Updates the firmware password the tuner uses to reach <b>${escapeHTML(label)}</b>. Leave blank to remove the per-miner override and fall back to the fleet default.
    </div>
    <div><label for="cmp-pw">New password</label>
      <input id="cmp-pw" type="password" autocomplete="new-password" placeholder="blank = remove override" autofocus>
    </div>
    <div id="cmp-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label:'Cancel', action: closeModal},
    {label:'Save', action: submitChangeMinerPassword},
  ]);
  setTimeout(() => { const el = document.getElementById('cmp-pw'); if (el) el.focus(); }, 0);
}

// Manual MAC override (A13). When the scanner couldn't read the real MAC
// (L3-isolated miners; ARP probe failed), it falls back to a synthesized
// ``syn-...`` ID. The operator pastes the real MAC from the miner's label,
// and the tuner re-keys MINER_CONFIGS, renames per-platform files, and moves
// the engine registry slot — all behind /tuner/miners/set_mac.
//
// The Set real MAC button is hidden by default and is unhidden by
// updateStatus when the per-miner entry's ``id_synthesized`` flag is true.
function openSetMacModal(){
  if (!currentMac()) return;
  if (!currentMac().startsWith('syn-')) {
    // The button shouldn't be visible in this case, but defense-in-depth
    // guard prevents an accidental real-MAC re-key.
    alert('This miner already has a real MAC recorded — no override needed.');
    return;
  }
  const label = currentIp() || currentMac();
  openModal(`Set real MAC — ${label}`, `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.85em">
      The scanner couldn't read this miner's hardware MAC. Paste the real MAC printed on the miner's chassis label so future ARP-discovery and per-platform tuning data follow the device.
    </div>
    <div><label for="setmac-new">New MAC</label>
      <input id="setmac-new" type="text" placeholder="aa:bb:cc:dd:ee:ff" autocomplete="off" autofocus>
    </div>
    <div style="color:var(--text2);font-size:0.78em;margin-top:6px">
      Accepted formats: <code>aa:bb:cc:dd:ee:ff</code>, <code>aa-bb-cc-dd-ee-ff</code>, or <code>aabbccddeeff</code>.
    </div>
    <div id="setmac-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Save', action: submitSetMacModal},
  ]);
  setTimeout(() => { const el = document.getElementById('setmac-new'); if (el) el.focus(); }, 0);
}

async function submitSetMacModal(){
  const errEl = document.getElementById('setmac-error');
  const newEl = document.getElementById('setmac-new');
  if (errEl) errEl.textContent = '';
  const newMac = (newEl ? newEl.value : '').trim();
  if (!newMac) {
    if (errEl) errEl.textContent = 'Enter the new MAC.';
    return;
  }
  const oldMac = currentMac();
  const r = await fetchJSON('/tuner/miners/set_mac', {
    method: 'POST',
    body: JSON.stringify({old_mac: oldMac, new_mac: newMac}),
    headers: {'Content-Type': 'application/json'},
  });
  if (r && r.ok) {
    closeModal();
    // Bounce to the new MAC's detail page; the engine registry has already
    // been re-keyed server-side so /tuner/live etc. resolve correctly.
    navigateToDetail(r.new_mac || newMac);
  } else if (r && r.error) {
    if (errEl) errEl.textContent = r.error;
  } else if (errEl) {
    errEl.textContent = 'Save failed (server error)';
  }
}

async function submitChangeMinerPassword(){
  const errEl = document.getElementById('cmp-error');
  const pwEl = document.getElementById('cmp-pw');
  if (errEl) errEl.textContent = '';
  // Empty string explicitly means "drop the override" — send null.
  const pw = pwEl ? pwEl.value : '';
  const payload = {PASSWORD: pw === '' ? null : pw};
  const r = await fetchJSON(`/tuner/config/miner/${currentMacDashes()}`, {
    method:'POST', body: JSON.stringify(payload), headers:{'Content-Type':'application/json'}
  });
  if (r && r.updated) {
    closeModal();
  } else if (r && r.errors && r.errors.length) {
    if (errEl) errEl.textContent = r.errors.join('; ');
  } else if (errEl) {
    errEl.textContent = 'Save failed (server error)';
  }
}

// Phase ordering as shown in the detail-view step indicators. 'phase_v_exploration'
// is the new Phase V slot (formerly 'phase4_voltage_sweep' in pre-Phase-V builds).
// The aliases list preserves dashboard rendering for any old checkpoints that
// stamped the legacy string.
const PHASES = ['phase0_discovery','phase1_set_voltage','phase2_baseline','phase_v_exploration','phase3_profiling','phase3b_polish','phase4_measure','phase5_save','phase6_perpetual'];
const PHASE_LABELS = ['0','1','2','V','3','3b','4','5','6'];
const PHASE_ALIASES = {'phase4_voltage_sweep': 'phase_v_exploration'};

// Charts
// Three-dataset shape: AVG (visible line), MAX (transparent), MIN (transparent
// with `fill:'-1'` to fill the band between MAX and MIN).  For live / short
// ranges only AVG is populated; long ranges (>=7d) use the band.
const chartOpts = (label, color) => ({
  type:'line',
  data:{
    labels:[],
    datasets:[
      { label, data:[], borderColor:color, borderWidth:1.5, pointRadius:0, tension:0.3, fill:false },
      { label:label+' max', data:[], borderColor:'transparent', backgroundColor:'rgba(255,255,255,0)', pointRadius:0, fill:false, hidden:true },
      { label:label+' min', data:[], borderColor:'transparent', backgroundColor:color+'33', pointRadius:0, fill:'-1', hidden:true },
    ]
  },
  options:{ responsive:true, maintainAspectRatio:false, animation:false, scales:{ x:{display:false}, y:{ticks:{color:'#8b8fa3',font:{size:10}},grid:{color:'#2d3142'}} }, plugins:{legend:{display:false}} }
});
let charts = {};
function initCharts(){
  charts.hashrate = new Chart(document.getElementById('chart-hashrate'), chartOpts('TH/s','#5b8af5'));
  charts.power = new Chart(document.getElementById('chart-power'), chartOpts('Watts','#ff9800'));
  charts.efficiency = new Chart(document.getElementById('chart-efficiency'), chartOpts('J/TH','#4caf50'));
  charts.temp = new Chart(document.getElementById('chart-temp'), chartOpts('Celsius','#f44336'));
}
function pushChart(chart, label, value){
  const d = chart.data;
  d.labels.push(label); d.datasets[0].data.push(value);
  // Mirror the cap onto the optional MIN/MAX datasets so a pre-existing
  // band stays aligned during the partial 1h live-mode push (rare —
  // band is only populated for >=7d, but defense-in-depth).
  if(d.labels.length > MAX_CHART_POINTS){
    d.labels.shift();
    d.datasets[0].data.shift();
    if(d.datasets[1] && d.datasets[1].data.length) d.datasets[1].data.shift();
    if(d.datasets[2] && d.datasets[2].data.length) d.datasets[2].data.shift();
  }
  chart.update();
}

// ─── seedChartsFromHistory: Phase B / B12 ──────────────────────────────────
// Replace each chart's data with a server-side query result for the chosen
// range.  The 1h mode keeps live-push enabled on top; longer ranges disable
// it via the `currentMetricsRange !== '1h'` gate in updateStatus.
async function seedChartsFromHistory(mac, range){
  if (!mac) return;
  const macDashes = mac.replace(/:/g, '-');
  let url = `/tuner/metrics/${encodeURIComponent(macDashes)}?metrics=hashrate_ths,power_w,efficiency_jth,temp_max_c`;
  if (range === 'custom') {
    if (!customMetricsRange.from || !customMetricsRange.to) return;
    url += `&range=custom&from=${customMetricsRange.from}&to=${customMetricsRange.to}`;
  } else {
    const preset = METRICS_RANGE_PRESETS[range];
    const points = (preset && preset.points) || 300;
    url += `&range=${encodeURIComponent(range)}&target_points=${points}`;
  }
  const status = document.getElementById('metrics-range-status');
  if (status) status.textContent = 'Loading…';
  const resp = await fetchJSON(url);
  if (!resp || !resp.series) {
    if (status) status.textContent = 'No data';
    return;
  }
  const showBand = (range === 'custom') || (METRICS_RANGE_PRESETS[range] && METRICS_RANGE_PRESETS[range].showBand);
  const seedOne = (chart, metric) => {
    const series = resp.series[metric] || {avg:[], min:[], max:[]};
    const labels = (series.avg || []).map(([ts]) => new Date(ts * 1000).toLocaleString());
    chart.data.labels = labels;
    chart.data.datasets[0].data = (series.avg || []).map(p => p[1]);
    if (showBand) {
      chart.data.datasets[1].data = (series.max || []).map(p => p[1]);
      chart.data.datasets[2].data = (series.min || []).map(p => p[1]);
      chart.data.datasets[1].hidden = false;
      chart.data.datasets[2].hidden = false;
    } else {
      chart.data.datasets[1].data = [];
      chart.data.datasets[2].data = [];
      chart.data.datasets[1].hidden = true;
      chart.data.datasets[2].hidden = true;
    }
    chart.update();
  };
  seedOne(charts.hashrate, 'hashrate_ths');
  seedOne(charts.power, 'power_w');
  seedOne(charts.efficiency, 'efficiency_jth');
  seedOne(charts.temp, 'temp_max_c');
  if (status) {
    const n = (resp.series.hashrate_ths && resp.series.hashrate_ths.avg || []).length;
    status.textContent = n ? `${n} buckets · bucket_sec=${resp.bucket_sec}` : 'No samples in range';
  }
}

// User-driven range change.  Persists to localStorage + re-seeds charts.
function setMetricsRange(args){
  const select = document.getElementById('metrics-range');
  const range = (args && args.value) || (select ? select.value : '1h');
  currentMetricsRange = range;
  try { localStorage.setItem(METRICS_RANGE_STORAGE_KEY, range); } catch {}
  const customRow = document.getElementById('metrics-range-custom');
  if (customRow) customRow.style.display = (range === 'custom') ? 'inline-flex' : 'none';
  if (range !== 'custom') {
    seedChartsFromHistory(currentMac(), range);
  }
}

function applyCustomMetricsRange(){
  const fromEl = document.getElementById('metrics-range-from');
  const toEl = document.getElementById('metrics-range-to');
  if (!fromEl || !toEl || !fromEl.value || !toEl.value) return;
  customMetricsRange = {
    from: Math.floor(new Date(fromEl.value).getTime() / 1000),
    to: Math.floor(new Date(toEl.value).getTime() / 1000),
  };
  seedChartsFromHistory(currentMac(), 'custom');
}

function switchTab(name){
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  // Phase 7: tabs are wired via data-action="switchTab" data-arg-tab="<name>"
  // (previously onclick="switchTab('<name>')"). Match on data-arg-tab now.
  document.querySelector(`.tab[data-arg-tab="${name}"]`).classList.add('active');
}

function formatDuration(sec){
  if(sec === undefined || sec === null || sec <= 0) return '—';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if(h > 0) return `${h}h ${m}m`;
  if(m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
// Auth state: true once the user is known-authenticated, false when we've
// shown the login view. Prevents the poll loop from spamming 401s while the
// login overlay is up.
let authReady = false;

async function fetchJSON(url, opts){
  try {
    const r = await fetch(url, opts);
    if (r.status === 401) {
      authReady = false;
      showLogin();
      return null;
    }
    return await r.json();
  } catch { return null; }
}
// Disable both buttons immediately on click so rapid double-clicks don't
// queue a second POST before the next poll rewrites button state. The
// backend _control_lock serializes start()/stop() so duplicates are
// harmless at the engine layer, but the extra roundtrips are wasteful
// and create confusing UI flicker. Force an immediate poll after the
// POST returns so buttons re-settle to the real state without waiting
// for the 10s interval to tick.
async function startTuning(){
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled = true;
  await fetchJSON('/tuner/start', {method:'POST', body:JSON.stringify({mac:currentMac()}), headers:{'Content-Type':'application/json'}});
  poll();
}
async function stopTuning(){
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled = true;
  await fetchJSON('/tuner/stop', {method:'POST', body:JSON.stringify({mac:currentMac()}), headers:{'Content-Type':'application/json'}});
  poll();
}
async function deleteProfile(){
  const label = currentMiner.ip || currentMac();
  openResetScopeModal({
    title: `Reset Profile — ${label}`,
    intro: 'Pick how much of the tune to clear. Smaller scopes let you redo only the expensive tail instead of the full ~2-hour pipeline.',
    onConfirm: async (scope) => {
      closeModal();
      const r = await fetchJSON('/tuner/delete_profile', {
        method:'POST', body:JSON.stringify({mac:currentMac(), scope}),
        headers:{'Content-Type':'application/json'},
      });
      if (r && r.deleted) poll();
      else if (r && r.error) openModal('Reset failed', `<div style="color:var(--red)">${escapeHTML(r.error)}</div>`, [{label:'Close', action: closeModal}]);
    },
  });
}
async function resetStockBaseline(){
  // Prefill manual inputs from the current baseline so tweaking a single
  // field (e.g. correcting just the wattage) is fast. Fetching status is
  // cheap — one call, no caching needed.
  const status = await fetchJSON('/tuner/status');
  const ip = currentIp();
  const cur = (status && status[ip] && status[ip].stock_baseline) || {};
  const curTHs = cur.hashrate_ths ? cur.hashrate_ths.toFixed(1) : '';
  const curW   = cur.power_w      ? cur.power_w.toFixed(0)      : '';
  const curV   = cur.voltage_mv   ? Math.round(cur.voltage_mv)  : '';
  openModal(`Reset stock baseline — ${ip || currentMac()}`, `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.9em">
      Stock baseline is the reference "before tuning" reading that every efficiency comparison is measured against. Only touch it after a hashboard swap, PSU change, firmware flash, or if the captured values are wrong.
    </div>
    <label class="scope-option scope-selected" data-mode="recapture">
      <input type="radio" name="stock-mode" value="recapture" checked>
      <div>
        <div style="font-weight:600">Re-capture from miner on next tune</div>
        <div style="color:var(--text2);font-size:0.85em">Delete the saved baseline. The next Start Tuning samples live for 40 s before Phase 0 disables the stock perpetual tune.</div>
      </div>
    </label>
    <label class="scope-option" data-mode="manual">
      <input type="radio" name="stock-mode" value="manual">
      <div style="flex:1">
        <div style="font-weight:600">Set manually</div>
        <div style="color:var(--text2);font-size:0.85em;margin-bottom:8px">Type the stock values directly. Efficiency is derived. Persists across Reset Profile like a live capture.</div>
        <div class="form-row" id="stock-manual-inputs" style="margin-bottom:0">
          <div><label>Hashrate (TH/s)</label><input id="stock-ths" type="number" step="0.1" value="${curTHs}" placeholder="200.0"></div>
          <div><label>Power (W)</label><input id="stock-w" type="number" step="1" value="${curW}" placeholder="3500"></div>
          <div><label>Voltage (mV)</label><input id="stock-mv" type="number" step="1" value="${curV}" placeholder="14000"></div>
        </div>
      </div>
    </label>
    <div id="stock-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Save', danger: true, action: submitStockBaseline},
  ]);
  // Wire up radio-selection visuals (same pattern as openResetScopeModal).
  document.querySelectorAll('.scope-option[data-mode]').forEach(el => {
    el.addEventListener('click', () => {
      document.querySelectorAll('.scope-option[data-mode]').forEach(e => e.classList.remove('scope-selected'));
      el.classList.add('scope-selected');
      const r = el.querySelector('input[type=radio]');
      if (r) r.checked = true;
    });
  });
  // Selecting "Set manually" should focus the TH/s field for fast entry.
  document.querySelector('.scope-option[data-mode="manual"]').addEventListener('click', () => {
    const f = document.getElementById('stock-ths');
    if (f) f.focus();
  });
}

async function submitStockBaseline(){
  const picked = document.querySelector('input[name="stock-mode"]:checked');
  const mode = picked ? picked.value : 'recapture';
  const errEl = document.getElementById('stock-error');
  if (errEl) errEl.textContent = '';
  const body = {mac: currentMac()};
  if (mode === 'manual') {
    const ths = parseFloat(document.getElementById('stock-ths').value);
    const w = parseFloat(document.getElementById('stock-w').value);
    const mv = parseFloat(document.getElementById('stock-mv').value);
    if (!(ths > 0) || !(w > 0) || !(mv > 0)) {
      if (errEl) errEl.textContent = 'Enter positive numbers for all three fields.';
      return;
    }
    body.baseline = {hashrate_ths: ths, power_w: w, voltage_mv: mv};
  }
  const resp = await fetchJSON('/tuner/reset_stock', {
    method:'POST', body: JSON.stringify(body), headers:{'Content-Type':'application/json'},
  });
  if (resp && resp.ok) {
    closeModal();
    poll();
  } else if (errEl) {
    errEl.textContent = (resp && resp.error) || 'Request failed.';
  }
}
async function retuneVoltage(voltage_mv){
  if(!confirm(`Retune ${voltage_mv} mV?\n\nThis will re-run Phases 1-4 at this voltage level and overwrite the existing entry. Typical runtime: 45-60 min. The engine must be stopped.`)) return;
  const resp = await fetchJSON('/tuner/retune_voltage', {method:'POST', body:JSON.stringify({mac:currentMac(), voltage_mv}), headers:{'Content-Type':'application/json'}});
  if(resp && !resp.ok){ alert('Retune failed: ' + (resp.error || 'unknown error')); return; }
  // Snapshot about to be overwritten — clear any active preview of this row.
  if(heatmapPreview && heatmapPreview.voltage_mv === voltage_mv) clearHeatmapPreview();
  poll();
}
async function selectVoltageProfile(voltage_mv){
  if(!confirm(`Switch active sweep profile to ${voltage_mv} mV?\n\nVoltage adjustment will reset to 0. Phase 6 will start using this voltage as its reference.`)) return;
  const resp = await fetchJSON('/tuner/select_voltage_profile', {method:'POST', body:JSON.stringify({mac:currentMac(), voltage_mv}), headers:{'Content-Type':'application/json'}});
  if(resp && !resp.ok){ alert('Select profile failed: ' + (resp.error || 'unknown error')); return; }
  clearHeatmapPreview(); // applied profile becomes live state
  poll();
}
function togglePreview(voltage_mv){
  // Toggle the per-chip heatmap's freq source between live state and this
  // voltage_results entry's stable_freq_arrays snapshot. Board/chip dims
  // come from the miner's /capabilities via tunerStatus; not hardcoded.
  if(heatmapPreview && heatmapPreview.voltage_mv === voltage_mv){
    clearHeatmapPreview();
    return;
  }
  const vr = (tunerStatus.voltage_results || []).find(r => r.voltage_mv === voltage_mv);
  if(!vr || !Array.isArray(vr.stable_freq_arrays) || !vr.stable_freq_arrays.length){
    alert('No per-chip snapshot available for this entry.');
    return;
  }
  heatmapPreview = { voltage_mv, stable_freq_arrays: vr.stable_freq_arrays };
  // Preview only applies to the LEFT pane's freq mode — if the left pane is
  // on another mode, auto-switch and update only that pane's active class.
  // Right pane is unaffected.
  if(heatmapModeLeft !== 'freq'){
    heatmapModeLeft = 'freq';
    // Find the left pane's controls row (the one whose first button calls
    // setHeatmapMode('freq','left')) and reset active class scoped to it.
    const leftPane = document.getElementById('heatmap-left')?.closest('.heatmap-pane');
    const leftRow = leftPane?.querySelector('.heatmap-controls');
    if(leftRow){
      leftRow.querySelectorAll('button').forEach(b => b.classList.remove('active'));
      const freqBtn = leftRow.querySelector('button');
      if(freqBtn) freqBtn.classList.add('active');
    }
  }
  updatePreviewBanner();
  drawHeatmap('left');
  pollDetail(); // re-render the row buttons so the clicked one flips to "Previewing ●"
}
function clearHeatmapPreview(){
  if(!heatmapPreview) return;
  heatmapPreview = null;
  updatePreviewBanner();
  drawHeatmap('left');
  pollDetail();
}
function updatePreviewBanner(){
  const el = document.getElementById('heatmap-preview-banner');
  if(!el) return;
  if(heatmapPreview){
    const vEl = document.getElementById('heatmap-preview-voltage');
    if(vEl) vEl.textContent = `${heatmapPreview.voltage_mv} mV`;
    el.style.display = 'flex';
  } else {
    el.style.display = 'none';
  }
}
async function enqueueRemeasure(voltage_mv, freq_mhz){
  const resp = await fetchJSON('/tuner/remeasure_cell', {
    method:'POST',
    body:JSON.stringify({mac:currentMac(), voltage_mv, freq_mhz}),
    headers:{'Content-Type':'application/json'},
  });
  if(resp && !resp.ok){
    alert('Queue remeasure failed: ' + (resp.error || 'unknown error'));
  } else if(resp && resp.added === false){
    alert(`Already queued — queue size: ${resp.queue_size}`);
  }
  poll();
}
async function clearRemeasureQueue(){
  if(!confirm('Clear the remeasure queue? Cells already being measured will finish.')) return;
  const resp = await fetchJSON('/tuner/remeasure_queue/clear', {
    method:'POST', body:JSON.stringify({mac:currentMac()}),
    headers:{'Content-Type':'application/json'},
  });
  if(resp && !resp.ok) alert('Clear failed: ' + (resp.error || 'unknown error'));
  poll();
}
async function processRemeasureQueue(){
  if(!confirm('Process remeasure queue now?\n\nRequires the engine to be stopped. The tuner will apply each queued (V, F) and take a measurement, then leave the engine stopped. Press Start afterwards to resume mining.')) return;
  const resp = await fetchJSON('/tuner/remeasure_queue/process', {
    method:'POST', body:JSON.stringify({mac:currentMac()}),
    headers:{'Content-Type':'application/json'},
  });
  if(resp && !resp.ok) alert('Process queue failed: ' + (resp.error || 'unknown error'));
  poll();
}
// Per-voltage log modal — fetches /tuner/log/{ip}?voltage_mv=X and shows the
// filtered entries in a scrollable <pre>. The backend strips pre-retune
// entries when the Retune button is clicked, so the modal always reflects
// the active (or most-recent) tune at that step.
let _voltageLogTimer = null;
let _voltageLogOpen = null;
async function showVoltageLogModal(voltage_mv){
  _voltageLogOpen = voltage_mv;
  const title = `Tuning log — ${voltage_mv} mV step`;
  const bodyPlaceholder = `<div id="vlog-status" style="color:var(--text2);font-size:0.85em;margin-bottom:8px">Loading…</div>
    <pre id="vlog-pre" style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:8px;max-height:400px;overflow:auto;font-family:monospace;font-size:0.75em;line-height:1.4;white-space:pre-wrap;color:var(--text2);margin:0"></pre>`;
  openModal(title, bodyPlaceholder, [
    {label:'Download', action: () => downloadVoltageLog(voltage_mv)},
    {label:'Close', action: () => { _voltageLogOpen = null; closeModal(); }},
  ]);
  await _refreshVoltageLogModal(voltage_mv);
  // Live-refresh the modal every 5s in case the step is currently running.
  if (_voltageLogTimer) clearInterval(_voltageLogTimer);
  _voltageLogTimer = setInterval(() => {
    if (_voltageLogOpen !== voltage_mv) { clearInterval(_voltageLogTimer); _voltageLogTimer = null; return; }
    _refreshVoltageLogModal(voltage_mv);
  }, 5000);
}

async function _refreshVoltageLogModal(voltage_mv){
  const pre = document.getElementById('vlog-pre');
  const status = document.getElementById('vlog-status');
  if (!pre) return;
  const resp = await fetchJSON(`/tuner/log/${currentMacDashes()}?voltage_mv=${voltage_mv}`);
  if (!resp) {
    if (status) status.textContent = 'Failed to fetch log.';
    return;
  }
  const lines = resp.lines || [];
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 10;
  pre.textContent = lines.length ? lines.join('\n') : '(no log entries for this voltage step yet)';
  if (atBottom) pre.scrollTop = pre.scrollHeight;
  if (status) status.textContent = `${lines.length} entries · auto-refresh every 5s`;
}

function downloadVoltageLog(voltage_mv){
  const pre = document.getElementById('vlog-pre');
  if (!pre) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([pre.textContent], {type:'text/plain'}));
  a.download = `${currentIp() || currentMac()}-${voltage_mv}mV-log.txt`;
  a.click();
}
// ─── Per-miner config overrides ──────────────────────────────────────────────
// In the detail view, the config tab edits THIS miner's overrides. Each field
// that differs from the global default shows an ● indicator that also acts as
// a "revert to default" button (click → POST null to drop the override).
// Fleet-network settings (MINER_IPS, SOURCE_IP, API_PORT, PASSWORD) do not
// render here — they're set on the overview defaults accordion, collected at
// Add Miner time, or rotated via the detail-view "Change password" button.
// Minerstat settings live on the Minerstat card's settings modal.
const MINER_OVERRIDE_KEYS = new Set(CFG_KEYS);

async function saveConfig(){
  const errEl = document.getElementById('config-errors');
  const okEl = document.getElementById('config-success');
  errEl.style.display = 'none';
  okEl.style.display = 'none';

  const minerOv = {};

  CFG_KEYS.forEach(k => {
    const meta = CFG_META[k];
    if (!meta) return;
    const v = readFormValue('cfg-'+k, meta.type);
    if (v !== undefined) minerOv[k] = v;
  });

  const changes = Object.entries(minerOv).map(([k,v]) => `${k}: ${v}`);
  if (!changes.length) {
    errEl.innerHTML = 'No changes to save.';
    errEl.style.display = 'block';
    return;
  }
  if (!confirm(`Apply these per-miner overrides to ${currentIp() || currentMac()}?\n\n` + changes.join('\n'))) return;

  const r = await fetchJSON(`/tuner/config/miner/${currentMacDashes()}`, {
    method:'POST', body: JSON.stringify(minerOv), headers:{'Content-Type':'application/json'}
  });
  if (r && r.updated) {
    okEl.style.display = 'block';
    setTimeout(() => okEl.style.display = 'none', 3000);
    loadConfig();
  } else if (r && r.errors && r.errors.length) {
    errEl.innerHTML = '<b>Validation errors:</b><br>' + r.errors.map(escapeHTML).join('<br>');
    errEl.style.display = 'block';
  } else {
    errEl.innerHTML = 'Save failed (server error)';
    errEl.style.display = 'block';
  }
}

async function loadConfig(){
  // Ensure the form is built before populating (lazy-built on first entry
  // to the config tab, or always-built if the tab was already mounted).
  const root = document.getElementById('config-form-root');
  if (root && !root.firstElementChild) {
    buildConfigForm(root, 'cfg-', {capabilities: currentDetailCapabilities});
    // One-time decoration: add a "Pick from my rigs" button next to the
    // MRR_RIG_ID field so operators don't have to copy IDs by hand.
    const mrrInput = document.getElementById('cfg-MRR_RIG_ID');
    if (mrrInput && !mrrInput.parentElement.querySelector('.mrr-picker-btn')) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'secondary mrr-picker-btn';
      btn.textContent = '⚙ Pick from my rigs';
      btn.style.cssText = 'margin-top:4px;font-size:0.78em;padding:2px 8px';
      btn.onclick = () => openMRRRigPicker();
      mrrInput.parentElement.appendChild(btn);
    }
  }

  const cfg = await fetchJSON('/tuner/config');
  if (!cfg) return;
  // v4: cfg.miner_configs is keyed by MAC. The per-miner entry has a v4
  // shape: cross-platform keys at top level (PASSWORD, MRR_RIG_ID, hostname,
  // current_firmware) and per-platform tuning overrides nested under
  // ``platforms[<firmware>]``. Flatten both into a single overrides map for
  // the form-population loop below.
  const minerEntry = (cfg.miner_configs && cfg.miner_configs[currentMac()]) || {};
  // Show the Set real MAC button only when this miner's identifier was
  // synthesized (ARP probe failed). The flag is set by the scanner at
  // registration time and cleared after a successful /tuner/miners/set_mac.
  const setMacBtn = document.getElementById('btn-set-mac');
  if (setMacBtn) {
    const synth = !!minerEntry.id_synthesized || (currentMac() || '').startsWith('syn-');
    setMacBtn.style.display = synth ? '' : 'none';
  }
  // Determine miner's platform: per-miner override wins, then last-known
  // status from poll, then fall back to 'epic' (matches EffectiveConfig default).
  const minerPlatform =
    minerEntry.current_firmware ||
    minerEntry.firmware_type ||
    (tunerStatus && tunerStatus.firmware_type) ||
    'epic';
  const platformOverrides = (minerEntry.platforms && minerEntry.platforms[minerPlatform]) || {};
  // Cross-platform keys live at the top level of the v4 entry; per-platform
  // tuning keys live in the platforms bucket. Flatten so the CFG_KEYS loop
  // below sees one merged dict.
  const overrides = {...platformOverrides};
  ['PASSWORD', 'MRR_RIG_ID', 'hostname', 'current_firmware'].forEach(k => {
    if (minerEntry[k] !== undefined) overrides[k] = minerEntry[k];
  });
  // For backward-compat with form fields that still use ``firmware_type``,
  // mirror current_firmware → firmware_type.
  if (overrides.current_firmware !== undefined) {
    overrides.firmware_type = overrides.current_firmware;
  }
  // v4 nested schema: per-platform tuning defaults under cfg.defaults[platform];
  // singleton fleet-ops keys under cfg.fleet_ops.  Merge into a single lookup so
  // the CFG_KEYS iteration below works without branching per key.
  // Platform wins on key overlap — the two namespaces are disjoint by design.
  const platformDefaults = (cfg.defaults && cfg.defaults[minerPlatform]) || {};
  const fleetOps = cfg.fleet_ops || {};
  const defaults = {...fleetOps, ...platformDefaults};

  CFG_KEYS.forEach(k => {
    const meta = CFG_META[k];
    if (!meta) return;
    const val = (overrides[k] !== undefined) ? overrides[k] : defaults[k];
    setFormValue('cfg-'+k, val, meta.type);
    markOverride(k, overrides[k] !== undefined, defaults[k]);
  });

  // MINER_IPS lives in cfg.fleet_ops under the v3 schema (Phase 1+).
  minerList = fleetOps.MINER_IPS || [];
}

function markOverride(key, isOverridden, defaultVal) {
  const el = document.getElementById('cfg-'+key);
  if (!el) return;
  const parent = el.parentElement;
  if (!parent) return;
  // Remove any previous indicator for this field
  const existing = parent.querySelector('.override-indicator');
  if (existing) existing.remove();
  if (!isOverridden) return;
  const label = parent.querySelector('label');
  if (!label) return;
  const ind = document.createElement('span');
  ind.className = 'override-indicator';
  ind.textContent = ' ● revert';
  ind.title = `Override in effect on ${currentIp() || currentMac()}. Default: ${defaultVal === undefined ? '(none)' : defaultVal}. Click to revert.`;
  ind.style.cssText = 'color:var(--accent); margin-left:6px; cursor:pointer; font-size:0.75em; font-weight:600';
  ind.onclick = (e) => { e.preventDefault(); revertOverride(key); };
  label.appendChild(ind);
}

async function revertOverride(key) {
  if (!currentMac()) return;
  const label = currentIp() || currentMac();
  if (!confirm(`Revert ${key} for ${label} to the default value?`)) return;
  const payload = {}; payload[key] = null;
  const r = await fetchJSON(`/tuner/config/miner/${currentMacDashes()}`, {
    method:'POST', body: JSON.stringify(payload), headers:{'Content-Type':'application/json'}
  });
  if (r && r.updated) loadConfig();
}

// ─── Defaults form (overview accordion) ──────────────────────────────────────
// Per-platform tuning defaults. Uses `def-` ID prefix to avoid collisions with
// the per-miner config form. POSTs {platform, defaults: {...}} to
// /tuner/config/defaults (NEW shape). Fleet-ops keys are NOT in this payload —
// they belong to the Fleet Operations accordion below.

function buildDefaultsForm(){
  const root = document.getElementById('defaults-form');
  if (!root) return;
  // Build the categorized form (shared helper, platform keys only — no fleet-ops),
  // wrapped with status banners and a save button.
  root.innerHTML = `
    <div id="def-errors" style="display:none;background:#5c1a1a;border:1px solid #ff4444;border-radius:6px;padding:8px 12px;margin-bottom:12px;color:#ff8888;font-size:13px"></div>
    <div id="def-success" style="display:none;background:#1a3d1a;border:1px solid #44aa44;border-radius:6px;padding:8px 12px;margin-bottom:12px;color:#88cc88;font-size:13px">Defaults saved</div>
    <div id="def-form-root"></div>
    <div style="margin-top:12px;display:flex;justify-content:flex-end">
      <button data-action="saveDefaults">Save Defaults</button>
    </div>`;
  // includeFleetOnly:true so per-platform categories like "Power" and
  // "Wattage Search" (marked fleetOnly) appear, but fleet-ops-only categories
  // are filtered out by the updated buildConfigForm logic.
  // capabilities: pass the selected platform's capability dict so CFG_META
  // entries whose `requires` flag is false for this firmware render disabled.
  // Fallback to epic ensures the form still builds if the dropdown is missing.
  buildConfigForm(document.getElementById('def-form-root'), 'def-', {includeFleetOnly:true, capabilities: PLATFORM_CAPABILITIES[_getSelectedPlatform()] || PLATFORM_CAPABILITIES.epic});
}

// Return the currently selected platform from the dropdown (default 'epic').
function _getSelectedPlatform(){
  const sel = document.getElementById('defaults-platform');
  return (sel && sel.value) || 'epic';
}

async function loadDefaults(){
  const cfg = await fetchJSON('/tuner/config');
  if (!cfg) return;
  const platform = _getSelectedPlatform();
  // cfg.defaults[platform] holds per-platform tuning values (v3 schema).
  // Fall back to cfg.defaults (flat v2 shape) if the nested shape is absent —
  // so an in-flight migration or dev server without Phase 1 still works.
  const defaults = (cfg.defaults && cfg.defaults[platform]) || cfg.defaults || {};
  CFG_KEYS_PLATFORM_DEFAULTS.forEach(k => {
    const meta = CFG_META[k];
    if (!meta) return;
    setFormValue('def-'+k, defaults[k], meta.type);
  });
}

async function saveDefaults(){
  const errEl = document.getElementById('def-errors');
  const okEl = document.getElementById('def-success');
  if (!errEl || !okEl) return;
  errEl.style.display = 'none';
  okEl.style.display = 'none';

  const platform = _getSelectedPlatform();
  const payload = {};
  CFG_KEYS_PLATFORM_DEFAULTS.forEach(k => {
    const meta = CFG_META[k];
    if (!meta) return;
    const v = readFormValue('def-'+k, meta.type);
    if (v !== undefined) payload[k] = v;
  });

  if (!Object.keys(payload).length) {
    errEl.innerHTML = 'No changes to save.';
    errEl.style.display = 'block';
    return;
  }
  if (!confirm(`Apply these fleet defaults for the "${platform}" platform?\n\nFleet defaults are INHERITED: every miner without a per-miner override for an affected key will pick up the new value immediately — existing miners and newly-added miners alike. Miners with explicit per-miner overrides keep their override.\n\nTo lock an existing miner against future fleet-default changes for a key, set that key explicitly on the per-miner config tab.`)) return;

  // POST new shape: {platform, defaults: {...per-platform keys only...}}
  const r = await fetchJSON('/tuner/config/defaults', {
    method:'POST', body: JSON.stringify({platform, defaults: payload}),
    headers:{'Content-Type':'application/json'}
  });
  if (r && r.updated) {
    okEl.style.display = 'block';
    setTimeout(() => okEl.style.display = 'none', 3000);
    loadDefaults();
  } else if (r && r.errors && r.errors.length) {
    errEl.innerHTML = '<b>Validation errors:</b><br>' + r.errors.map(escapeHTML).join('<br>');
    errEl.style.display = 'block';
  } else {
    errEl.innerHTML = 'Save failed.';
    errEl.style.display = 'block';
  }
}

// Called when the platform dropdown changes — REBUILD the form structure
// so the new platform's capability gating (vendorMismatch in buildConfigForm)
// takes effect, then populate values from the new platform's defaults.
async function defaultsPlatformChange(_value){
  const root = document.getElementById('def-form-root');
  if (root) root.innerHTML = '';
  buildDefaultsForm();
  await loadDefaults();
}

function onDefaultsToggle(details){
  if (!details.open) return;
  const root = document.getElementById('defaults-form');
  if (!root) return;
  // Lazy-build on first open; refresh values every time so external edits
  // (per-miner revert → defaults) show up without a page reload.
  if (!root.firstElementChild) buildDefaultsForm();
  loadDefaults();
}

function updateStatus(data){
  // /tuner/status is keyed by IP (manager.get_all_status preserves the v3
  // wire shape for the dashboard). Resolve via the current IP; if currentIp()
  // is empty (just navigated), peek at the overview to pull the IP for the MAC.
  const ip = currentIp();
  if(!data || !ip || !data[ip]) return;
  const s = data[ip];
  tunerStatus = s;

  // Update capability-gate state for config form. If capabilities changed
  // (e.g. first poll after navigation), force a config form rebuild so
  // capability-disabled inputs reflect the miner's actual firmware family.
  const newCaps = s.capabilities;
  if (JSON.stringify(newCaps) !== JSON.stringify(currentDetailCapabilities)) {
    currentDetailCapabilities = newCaps;
    const cfgRoot = document.getElementById('config-form-root');
    if (cfgRoot) {
      cfgRoot.innerHTML = '';
      loadConfig(); // rebuild now with the correct capabilities + repopulate values
    }
  }
  // Toggle hide-no-per-chip-tuning class on the detail view container — CSS uses this
  // to hide the chip heatmap tab and chip-tune comparison bar for miners without per-chip tuning.
  // Toggle hide-no-wattage-search — hides the Wattage Search (Braiins) chart card for
  // miners that do not use the wattage_search tuning strategy (i.e. all non-Braiins miners).
  const detailView = document.getElementById('view-detail');
  if (detailView) {
    detailView.classList.toggle('hide-no-per-chip-tuning', !s.capabilities || !s.capabilities.supports_per_chip_tuning);
    detailView.classList.toggle('hide-no-wattage-search', !s.capabilities || !s.capabilities.wattage_search_strategy);
  }

  const isOffline = s.phase === 'offline';
  const dotsEl = document.getElementById('phase-dots');
  // When offline, highlight the phase we WILL resume to (pre_offline_phase)
  // with the offline class so it pulses amber. Otherwise render normally.
  const rawPhaseForDots = isOffline ? (s.pre_offline_phase || s.phase) : s.phase;
  const phaseForDots = PHASE_ALIASES[rawPhaseForDots] || rawPhaseForDots;
  const pidx = PHASES.indexOf(phaseForDots);
  // Active-dot badge — shows progress for the long Phase V scan (% of grid
  // points measured) and Phase 3 refinement (iterative round / cap). Empty
  // for other phases. Kept under 4 chars so the tiny badge doesn't overflow.
  const phaseBadge = (phaseStr) => {
    if(isOffline) return '';
    if(phaseStr === 'phase_v_exploration'){
      const measured = (s.vf_surface || []).length;
      const skipped = (s.vf_skipped || []).length;
      const planned = (s.vf_planned_grid || []).length;
      if(planned > 0){
        const pct = Math.round((measured + skipped) / planned * 100);
        return `${Math.min(99, pct)}%`;
      }
      return '';
    }
    if(phaseStr === 'phase3_profiling'){
      const cfg = s.config || {};
      const max = cfg.max_profiling_rounds || 60;
      return `${s.profiling_round || 0}/${max}`;
    }
    return '';
  };
  // Per-dot tooltip — summarizes what each phase is doing. Static, reads
  // nothing dynamic, so no refresh churn.
  const PHASE_TOOLTIPS = {
    phase0_discovery: 'Phase 0 — discovery: connect, capture stock baseline, disable built-in perpetual tune',
    phase1_set_voltage: 'Phase 1 — apply voltage and baseline frequency with direction-aware ordering',
    phase2_baseline: 'Phase 2 — collect per-chip baseline health scores at a known-stable V/F',
    phase_v_exploration: 'Phase V — 2D (voltage, frequency) efficiency surface exploration',
    phase3_profiling: 'Phase 3 — iterative per-chip health-based tune at each top-K voltage (step UP if stable, DOWN if below baseline)',
    phase4_measure: 'Phase 4 — measure hashrate/power/J/TH at each top-K voltage with tuned per-chip freqs',
    phase5_save: 'Phase 5 — save tuning profile to disk',
    phase6_perpetual: 'Phase 6 — perpetual voltage-tracking tune (monitors hashrate drift, throttles hot chips)',
  };
  dotsEl.innerHTML = PHASES.map((p,i) => {
    let cls = 'phase-dot';
    if(i < pidx) cls += ' done';
    else if(i === pidx) cls += isOffline ? ' offline' : ' active';
    const badge = (i === pidx) ? phaseBadge(p) : '';
    const badgeHtml = badge ? `<span class="phase-dot-badge">${badge}</span>` : '';
    const tip = PHASE_TOOLTIPS[p] || '';
    return `<div class="phase-dot-wrap" title="${tip.replace(/"/g,'&quot;')}"><div class="${cls}">${PHASE_LABELS[i]}</div>${badgeHtml}</div>`;
  }).join('');
  document.getElementById('phase-detail').textContent = s.phase_detail || s.phase || 'Idle';

  // Offline banner + grayed stats. Elapsed time ticks up from offline_since_ts.
  const banner = document.getElementById('offline-banner');
  const statsGrid = document.getElementById('detail-stats-grid');
  if (isOffline && banner) {
    const since = s.offline_since_ts || (Date.now()/1000);
    const elapsed = Math.max(0, Math.floor(Date.now()/1000 - since));
    const sinceStr = since ? new Date(since*1000).toLocaleTimeString() : '—';
    banner.innerHTML = `
      <div><strong>⚠ Miner offline</strong> — tuner paused, will resume automatically when the miner is reachable.</div>
      <div class="dim">Offline since ${sinceStr} (${formatDuration(elapsed)} elapsed). ${escapeHTML(s.phase_detail || '')}</div>`;
    banner.style.display = '';
    if (statsGrid) statsGrid.classList.add('offline-muted');
  } else if (banner) {
    banner.style.display = 'none';
    if (statsGrid) statsGrid.classList.remove('offline-muted');
  }

  // Disable Start while the engine is doing any kind of work, including
  // waiting offline — pressing Start would spawn a second thread.
  // engine_busy (= thread.is_alive()) matters independent of phase: stop()
  // flips phase to STOPPED immediately, but the worker thread can take up to
  // a sample interval (30s) to wake from sleep and exit. During that window
  // the backend start() silently refuses to spawn a new thread, so we must
  // reflect that by keeping Start disabled until the old thread dies.
  const engineBusy = !!s.engine_busy;
  const phaseRunning = s.phase && !['idle','stopped','error'].includes(s.phase);
  const isRunning = engineBusy || phaseRunning;
  document.getElementById('btn-start').disabled = isRunning;
  document.getElementById('btn-stop').disabled = !isRunning;

  // Detail-page bucket badge — small pill near the heading that reflects
  // the operator-facing tuner_bucket (idle/tuning/maintaining/offline/
  // error/stopped/stopping). Updated on every poll cycle.
  const bucketEl = document.getElementById('detail-tuner-bucket');
  if (bucketEl) {
    const bucket = s.tuner_bucket || 'idle';
    bucketEl.className = 'phase-pill ' + bucket;
    bucketEl.textContent = phaseLabel(s.phase);
  }

  const ts = s.tuned_stats || {};
  document.getElementById('s-state').textContent = ts.state || s.phase || '--';
  document.getElementById('s-hashrate').textContent = ts.hashrate_ths ? ts.hashrate_ths.toFixed(1)+' TH/s' : '--';
  document.getElementById('s-power').textContent = ts.power_w ? ts.power_w.toFixed(0)+' W' : '--';
  document.getElementById('s-efficiency').textContent = ts.efficiency_jth ? ts.efficiency_jth.toFixed(1)+' J/TH' : '--';
  document.getElementById('s-voltage').textContent = ts.voltage_mv ? ts.voltage_mv.toFixed(0)+' mV' : '--';
  document.getElementById('s-board-temp').textContent =
      (s.avg_board_temp_c != null) ? s.avg_board_temp_c.toFixed(1)+' °C' : '--';
  document.getElementById('s-chip-temp').textContent =
      (s.avg_chip_temp_c != null) ? s.avg_chip_temp_c.toFixed(1)+' °C' : '--';
  const fanUnit = (s.firmware_type === 'epic') ? '%' : ' RPM';
  document.getElementById('s-fan').textContent = ts.fan_speed ? ts.fan_speed + fanUnit : '--';

  // MRR status line — shown only when this miner has an MRR_RIG_ID configured.
  // Renders the last sync outcome (or "never synced") so the operator can see
  // whether MRR is in sync with the tuner's current state.
  try { renderMRRStatusLine(s); } catch(e) {
    console.warn('renderMRRStatusLine failed', e);
  }

  const stock = s.stock_baseline || {};
  if(stock.hashrate_ths && ts.hashrate_ths){
    const srcTag = stock.source === 'live' ? ' (live)' : (stock.source === 'spec' ? ' (spec)' : (stock.source === 'manual' ? ' (manual)' : ''));
    document.getElementById('c-hashrate').textContent = `${stock.hashrate_ths.toFixed(1)}${srcTag} / ${ts.hashrate_ths.toFixed(1)} TH/s`;
    document.getElementById('c-power').textContent = `${stock.power_w?.toFixed(0)||'--'} / ${ts.power_w?.toFixed(0)||'--'} W`;
    const se = stock.efficiency_jth || (stock.power_w/stock.hashrate_ths);
    const te = ts.efficiency_jth;
    document.getElementById('c-efficiency').textContent = `${se?.toFixed(1)||'--'} / ${te?.toFixed(1)||'--'} J/TH`;
    if(se && te){
      const pct = ((se-te)/se*100);
      const el = document.getElementById('c-improvement');
      el.textContent = pct > 0 ? `${pct.toFixed(1)}% more efficient` : `${(-pct).toFixed(1)}% less efficient`;
      el.className = 'stat-value ' + (pct > 0 ? 'good' : 'bad');
    } else {
      const el = document.getElementById('c-improvement');
      el.textContent = '--'; el.className = 'stat-value good';
    }
  } else {
    document.getElementById('c-hashrate').textContent = '--';
    document.getElementById('c-power').textContent = '--';
    document.getElementById('c-efficiency').textContent = '--';
    const imp = document.getElementById('c-improvement');
    imp.textContent = '--'; imp.className = 'stat-value good';
  }

  // Profit rows — shown only when minerstat has coin data AND both stock +
  // tuned (hashrate, power) are populated. Otherwise hide entirely so the
  // card doesn't render empty $-- rows for operators who aren't in profit mode
  // or haven't fetched minerstat yet. Stock profit uses the recorded baseline
  // numbers; tuned profit uses live tuned_stats (matches the live J/TH row).
  try {
    const profitRow = document.getElementById('c-profit-row');
    const deltaRow = document.getElementById('c-profit-delta-row');
    const profitEl = document.getElementById('c-profit');
    const deltaEl = document.getElementById('c-profit-delta');
    const stockProfit = (stock && stock.hashrate_ths != null && stock.power_w != null)
      ? computeProfitUsdPerDay(stock.hashrate_ths, stock.power_w, s) : null;
    const tunedProfit = (ts && ts.hashrate_ths != null && ts.power_w != null)
      ? computeProfitUsdPerDay(ts.hashrate_ths, ts.power_w, s) : null;
    if (stockProfit != null || tunedProfit != null) {
      if (profitRow) profitRow.style.display = '';
      if (deltaRow) deltaRow.style.display = '';
      if (profitEl) {
        profitEl.textContent = `${_fmtUSD(stockProfit)} / ${_fmtUSD(tunedProfit)}`;
      }
      if (deltaEl) {
        if (stockProfit != null && tunedProfit != null) {
          const delta = tunedProfit - stockProfit;
          deltaEl.innerHTML = _fmtDelta(delta, '/day');
        } else {
          deltaEl.textContent = '--';
        }
      }
    } else {
      // Hide both rows when we can't compute profit — e.g. minerstat not
      // fetched yet, or no stock baseline on a fresh install.
      if (profitRow) profitRow.style.display = 'none';
      if (deltaRow) deltaRow.style.display = 'none';
    }
  } catch(e) {
    console.warn('Stock vs Tuned profit render failed', e);
  }

  // Wrap renderVFSurface in try/catch — a JS exception here would otherwise
  // abort updateStatus BEFORE top_tunes renders and BEFORE pollDetail's
  // updateLog() call, producing the blank-cascade bug (logs + V/F surface +
  // top_tunes all blank while phase indicators keep updating). Any thrown
  // error is surfaced to the console AND to a visible banner above the V/F
  // surface card so we can diagnose without having to open DevTools.
  try {
    renderVFSurface(s);
  } catch (e) {
    const msg = (e && e.stack) ? String(e.stack) : String(e);
    console.error('renderVFSurface threw — continuing updateStatus:', e, msg);
    // Stamp a visible error banner onto the V/F card so the operator can
    // copy-paste the stack trace without needing DevTools.
    const card = document.getElementById('vf-surface-card');
    if (card) {
      card.style.display = '';
      let banner = document.getElementById('vf-error-banner');
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'vf-error-banner';
        banner.style.cssText = 'background:#3a1f1f;border:1px solid #e16969;color:#ffc0c0;padding:8px 12px;margin:8px 0;font-family:monospace;font-size:12px;white-space:pre-wrap;border-radius:4px;max-height:200px;overflow:auto';
        card.insertBefore(banner, card.firstChild);
      }
      banner.textContent = `renderVFSurface() threw:\n${msg}`;
    }
  }

  const vr = s.voltage_results || [];
  // R7: top_tunes is the unified top-3 list (coarse + fine + chip-tuned),
  // computed by the backend. Chip-tuned rows need lookup into voltage_results
  // for per-board data + stable_freq_arrays (those aren't in top_tunes).
  const topTunes = s.top_tunes || [];
  const vrByVoltage = new Map(vr.map(r => [r.voltage_mv, r]));
  const vrEl = document.getElementById('voltage-results');
  const activeSweep = s.phase && (/^phase[1234]/.test(s.phase) || s.phase === 'phase_v_exploration');
  // Target mode drives the "winner" pill + card title + column emphasis.
  // Profit mode picks max $/day; efficiency mode picks min J/TH.
  const targetMode = (s.config && s.config.target_mode) || 'efficiency';
  const isProfitMode = targetMode === 'profitability';
  // Title is static "Best Tunes" — the ranking column is already emphasized
  // in the table header based on mode, so the heading stays mode-neutral.
  if(topTunes.length > 0 || vr.length > 0 || activeSweep){
    // "Best" row pill — most efficient in efficiency mode, most profitable in
    // profit mode. topTunes rows always carry efficiency_jth; profit_usd_day
    // is populated only when minerstat data is available.
    const bestJth = topTunes.length > 0
      ? Math.min(...topTunes.map(r => r.efficiency_jth))
      : (vr.length > 0 ? Math.min(...vr.map(r => r.efficiency_jth)) : null);
    const profitValues = topTunes
      .map(r => r.profit_usd_day)
      .filter(v => typeof v === 'number');
    const bestProfit = profitValues.length > 0
      ? Math.max(...profitValues)
      : null;
    const fmtT = t => (t==null||isNaN(t)) ? '—' : t.toFixed(0);
    const engineBusy = !!s.engine_busy;
    const activeMv = s.active_sweep_voltage_mv;
    // Stock-baseline deltas — lets each row show Δ TH/s, Δ W, Δ J/TH vs the
    // captured pre-tune baseline. `stock_baseline` missing (e.g. a profile
    // from a prior version of the tuner) → deltas are suppressed gracefully.
    const stockB = s.stock_baseline || {};
    const stockThs = typeof stockB.hashrate_ths === 'number' ? stockB.hashrate_ths : null;
    const stockW = typeof stockB.power_w === 'number' ? stockB.power_w : null;
    const stockJth = typeof stockB.efficiency_jth === 'number'
      ? stockB.efficiency_jth
      : (stockW && stockThs ? stockW / stockThs : null);
    // Delta chip builder. `higherIsBetter=true` → positive delta is green.
    const delta = (cur, base, unit, higherIsBetter, decimals) => {
      if(base == null || cur == null || !isFinite(base) || !isFinite(cur)) return '';
      const d = cur - base;
      if(Math.abs(d) < 0.01 && !isFinite(d/base)) return '';
      const pct = base !== 0 ? (d / base) * 100 : null;
      const good = higherIsBetter ? d > 0 : d < 0;
      const bad = higherIsBetter ? d < 0 : d > 0;
      const cls = good ? 'up' : (bad ? 'down' : '');
      const sign = d > 0 ? '+' : '';
      const dFmt = Math.abs(d) >= 10 ? d.toFixed(0) : d.toFixed(decimals || 1);
      const pctStr = pct != null ? ` (${pct > 0 ? '+' : ''}${pct.toFixed(1)}%)` : '';
      return `<span class="delta-chip ${cls}">${sign}${dFmt}${unit}${pctStr}</span>`;
    };
    // Inline crown — indicates the most-efficient row. Uses currentColor so
    // the fill picks up the surrounding `color:gold` style.
    const CROWN_SVG = '<svg class="crown" viewBox="0 0 24 20" fill="currentColor" aria-label="Most efficient"><path d="M3 6l4 4 5-7 5 7 4-4v10H3V6z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg>';

    // Source pill helper — small colored chip for coarse/fine/chip-tuned.
    const sourcePill = (source) => {
      if(source === 'chip-tuned'){
        return `<span class="pill" style="background:var(--accent);color:#fff;padding:2px 8px;font-size:0.72em;font-weight:600">CHIP-TUNED</span>`;
      }
      if(source === 'fine'){
        return `<span class="pill" style="background:rgba(88,166,255,0.18);color:var(--accent);padding:2px 8px;font-size:0.72em;font-weight:600;border:1px solid rgba(88,166,255,0.5)">FINE</span>`;
      }
      return `<span class="pill" style="background:var(--bg3);color:var(--text2);padding:2px 8px;font-size:0.72em;font-weight:600;border:1px solid var(--border)">COARSE</span>`;
    };

    // Column header styling — emphasize the active target's column so the
    // operator can see at-a-glance which metric is driving ranking.
    const jthHeaderStyle = isProfitMode ? 'color:var(--text2)' : 'color:var(--text);font-weight:600';
    const profitHeaderStyle = isProfitMode ? 'color:var(--text);font-weight:600' : 'color:var(--text2)';
    const header = '<div class="stat-row" style="font-weight:500">' +
      '<span class="stat-label" style="flex:0 0 110px">Source</span>' +
      '<span class="stat-label" style="flex:0 0 80px">Voltage</span>' +
      '<span class="stat-label" style="flex:0 0 120px">Freq</span>' +
      '<span class="stat-label" style="flex:0 0 110px">TH/s</span>' +
      '<span class="stat-label" style="flex:0 0 110px">Watts</span>' +
      `<span class="stat-label" style="flex:0 0 120px;${jthHeaderStyle}">J/TH</span>` +
      `<span class="stat-label" style="flex:0 0 120px;${profitHeaderStyle}">$/day</span>` +
      '<span class="stat-label" style="flex:1">Per-board (click row to expand)</span>' +
      '<span class="stat-label" style="flex:0 0 260px; text-align:right">Actions</span>' +
      '</div>';

    // R7: Render up to 3 top-efficiency rows — chip-tuned / fine / coarse mix.
    // Chip-tuned rows cross-look-up voltage_results for per-board detail +
    // preview-snapshot data; coarse/fine rows render Retune-this-voltage.
    const completedRows = topTunes.map((t, idx) => {
      const vrEntry = t.source === 'chip-tuned' ? vrByVoltage.get(t.voltage_mv) : null;
      // "Best" badge: in efficiency mode the lowest J/TH wins; in profit
      // mode the highest $/day wins. Fall back to J/TH if profit data is
      // missing (minerstat not fetched yet) so the badge always shows.
      const isBest = isProfitMode
        ? (bestProfit != null && t.profit_usd_day === bestProfit)
        : (t.efficiency_jth === bestJth);
      const isActive = activeMv === t.voltage_mv;
      const freqVal = t.freq_mhz != null ? `${Number(t.freq_mhz).toFixed(0)} MHz` : '—';
      // Seed→tuned chip — only meaningful for chip-tuned rows.
      const seedChip = (vrEntry && vrEntry.seed_f_mhz != null && vrEntry.avg_freq_mhz != null)
        ? `<span class="seed-chip" title="Phase V uniform-F seed">seed ${Number(vrEntry.seed_f_mhz).toFixed(0)} → ${(vrEntry.avg_freq_mhz - Number(vrEntry.seed_f_mhz) >= 0 ? '+' : '')}${(vrEntry.avg_freq_mhz - Number(vrEntry.seed_f_mhz)).toFixed(0)}</span>`
        : '';
      const pb = vrEntry && Array.isArray(vrEntry.per_board) ? vrEntry.per_board : [];
      const rowId = `ht-pb-${idx}`;
      const rowBg = isActive ? 'background:rgba(88,166,255,0.12);' : '';
      const activeChip = isActive ? '<span class="pill" style="background:var(--accent);color:#fff;margin-left:4px">ACTIVE</span>' : '';
      const winnerChip = isBest ? `<span class="pill winner" style="margin-left:4px" title="Most efficient J/TH">${CROWN_SVG}winner</span>` : '';
      const disabledAttr = engineBusy ? 'disabled' : '';

      let buttons;
      if(t.source === 'chip-tuned' && vrEntry){
        const logBtn = `<button class="secondary" data-action="showVoltageLogModal" data-stop-propagation="true" data-arg-voltage="${t.voltage_mv}" title="View tuning log entries for this voltage step" style="font-size:0.75em;padding:2px 6px">Log</button>`;
        const retuneBtn = `<button class="secondary" ${disabledAttr} data-action="retuneVoltage" data-stop-propagation="true" data-arg-voltage="${t.voltage_mv}" title="Re-run Phases 1-4 at this voltage (overwrites this entry, wipes this step's log)" style="font-size:0.75em;padding:2px 6px">Retune</button>`;
        const hasSfa = Array.isArray(vrEntry.stable_freq_arrays) && vrEntry.stable_freq_arrays.length > 0;
        const isPreviewing = heatmapPreview && heatmapPreview.voltage_mv === t.voltage_mv;
        const previewBtn = chipTuned.length >= 2
          ? `<button class="secondary" ${hasSfa ? '' : 'disabled'} data-action="togglePreview" data-stop-propagation="true" data-arg-voltage="${t.voltage_mv}" title="${hasSfa ? 'Show this row\u0027s per-chip freqs on the heatmap without applying the profile' : 'No per-chip snapshot for this entry'}" style="font-size:0.75em;padding:2px 6px${isPreviewing ? ';color:var(--accent);border-color:var(--accent)' : ''}">${isPreviewing ? 'Previewing \u25cf' : 'Preview'}</button>`
          : '';
        const useBtnChip = activeMv !== t.voltage_mv
          ? `<button class="secondary" ${disabledAttr} data-action="selectVoltageProfile" data-stop-propagation="true" data-arg-voltage="${t.voltage_mv}" title="Use this profile for perpetual tune" style="font-size:0.75em;padding:2px 6px">Use</button>`
          : '';
        buttons = `${logBtn}${previewBtn}${retuneBtn}${useBtnChip}`;
      } else {
        // R7 extended retune — coarse/fine rows let you seed a fresh Phase 3+4
        // at this voltage using the V/F surface reading as the starting point.
        const retuneBtn = `<button class="secondary" ${disabledAttr} data-action="retuneVoltage" data-stop-propagation="true" data-arg-voltage="${t.voltage_mv}" title="Run per-chip Phase 3 + Phase 4 at this voltage seeded from the ${t.source} cell at ${Number(t.freq_mhz).toFixed(0)} MHz" style="font-size:0.75em;padding:2px 6px">Retune this voltage</button>`;
        buttons = retuneBtn;
      }

      const dTh = delta(t.hashrate_ths, stockThs, ' TH/s', true, 1);
      const dW = delta(t.power_w, stockW, ' W', false, 0);
      const dJth = delta(t.efficiency_jth, stockJth, ' J/TH', false, 2);
      const thsStr = t.hashrate_ths != null ? Number(t.hashrate_ths).toFixed(1) : '—';
      const wStr = t.power_w != null ? Number(t.power_w).toFixed(0) : '—';
      const jthStr = t.efficiency_jth != null ? Number(t.efficiency_jth).toFixed(2) : '—';
      const profitStr = t.profit_usd_day != null
        ? `$${Number(t.profit_usd_day).toFixed(2)}`
        : '<span style="color:var(--text2)">—</span>';
      // Crown only on the active-mode column so operators see what's
      // driving ranking; the other column stays quiet.
      const jthCellExtras = !isProfitMode && isBest ? winnerChip : '';
      const profitCellExtras = isProfitMode && isBest ? winnerChip : '';
      const jthCellClass = !isProfitMode && isBest ? 'good' : '';
      const profitCellClass = isProfitMode && isBest ? 'good' : '';
      const clickable = pb.length ? `cursor:pointer` : '';
      const onClickAttr = pb.length ? `data-action="togglePerBoard" data-arg-row-id="${rowId}"` : '';
      const mainRow =
        `<div class="stat-row" ${onClickAttr} style="${rowBg}${clickable}">` +
        `<span class="stat-value" style="flex:0 0 110px">${sourcePill(t.source)}</span>` +
        `<span class="stat-value" style="flex:0 0 80px">${t.voltage_mv} mV${activeChip}</span>` +
        `<span class="stat-value" style="flex:0 0 120px">${freqVal}${seedChip}</span>` +
        `<span class="stat-value" style="flex:0 0 110px">${thsStr}${dTh}</span>` +
        `<span class="stat-value" style="flex:0 0 110px">${wStr}${dW}</span>` +
        `<span class="stat-value ${jthCellClass}" style="flex:0 0 120px">${jthStr}${dJth}${jthCellExtras}</span>` +
        `<span class="stat-value ${profitCellClass}" style="flex:0 0 120px">${profitStr}${profitCellExtras}</span>` +
        `<span class="stat-value" style="flex:1; color:var(--text2); font-size:0.8em">${pb.length ? pb.length + ' boards ▸ click to expand' : '—'}</span>` +
        `<span class="stat-value" style="flex:0 0 260px; text-align:right; display:flex; gap:4px; justify-content:flex-end">${buttons}</span>` +
        `</div>`;
      const boardRows = pb.map(b =>
        `<div class="stat-row" style="padding-left:20px; background:rgba(255,255,255,0.02)">` +
        `<span class="stat-value" style="flex:0 0 110px; color:var(--text2)">Board ${b.index}</span>` +
        `<span class="stat-value" style="flex:0 0 80px"></span>` +
        `<span class="stat-value" style="flex:0 0 120px">${b.avg_clock_mhz?.toFixed(0)||'—'} MHz</span>` +
        `<span class="stat-value" style="flex:0 0 110px">${b.hashrate_ths?.toFixed(1)||'—'} TH/s</span>` +
        `<span class="stat-value" style="flex:0 0 110px; color:var(--text2)">${b.power_w?.toFixed(0)||'—'} W · ${b.board_temp_c?.toFixed(1)||'—'}°C</span>` +
        `<span class="stat-value" style="flex:0 0 120px; color:var(--text2)">chips ${fmtT(b.chip_temp_min_c)}/${fmtT(b.chip_temp_avg_c)}/${fmtT(b.chip_temp_max_c)}°C</span>` +
        `<span class="stat-value" style="flex:0 0 120px; color:var(--text2)">health ${b.health_pct?.toFixed(0)||'—'}%</span>` +
        `<span class="stat-value" style="flex:1; color:var(--text2); font-size:0.8em">inlet ${b.inlet_temp_c?.toFixed(1)||'—'}°C / outlet ${b.outlet_temp_c?.toFixed(1)||'—'}°C</span>` +
        `<span class="stat-value" style="flex:0 0 260px"></span>` +
        `</div>`
      ).join('');
      return mainRow + (pb.length ? `<div id="${rowId}" style="display:none">${boardRows}</div>` : '');
    }).join('');
    let progressRow = '';
    if(activeSweep){
      const stepV = s.current_step_voltage_mv || (s.config && s.config.min_voltage_mv) || 0;
      let progress = '';
      let label = 'Tuning';
      let donePct = 0; // 0..1 — drives the striped live progress bar
      const safePhaseDetail = escapeHTML(s.phase_detail || '');
      const safePhase = escapeHTML(s.phase || '');
      if(s.phase === 'phase_v_exploration'){
        label = 'Phase V';
        const surface = s.vf_surface || [];
        const plannedN = (s.vf_planned_grid || []).length;
        const skippedN = (s.vf_skipped || []).length;
        if(plannedN > 0){
          donePct = Math.min(1, (surface.length + skippedN) / plannedN);
          progress = `${surface.length}/${plannedN} measured · ${skippedN} skipped · ${safePhaseDetail}`;
        } else {
          progress = `Grid points: ${surface.length} · ${safePhaseDetail}`;
        }
      } else if(s.phase === 'phase3_profiling' && typeof s.profiling_completion_pct === 'number'){
        const cfg = s.config || {};
        // Phase 3 is an iterative health-based loop. profiling_completion_pct
        // and chips_stable_pct both hold the % of alive chips that can no
        // longer step UP (chip_max bracketed or pinned to F-grid ceiling) —
        // monotonic within a tune; resets per top-K voltage. Drives the bar
        // fill directly. The round counter is supplemental context only.
        const maxRounds = cfg.max_profiling_rounds || 60;
        const stillness = (s.stillness_streak || 0);
        const stillnessTarget = cfg.chip_tune_stillness_streak || 2;
        const topK = s.vf_top_k_voltages || [];
        const conv = s.chips_converged || 0;
        const alive = s.chips_alive || 0;
        const which = s.vf_refinement_index != null && topK.length
          ? ` top-${topK.length} #${(s.vf_refinement_index|0)+1}` : '';
        progress = `${conv}/${alive} chips converged${which} · round ${s.profiling_round || 0}/${maxRounds} · stillness ${stillness}/${stillnessTarget}`;
        donePct = Math.min(1, s.profiling_completion_pct / 100);
      } else if(s.phase === 'phase3b_polish'){
        const cfg = s.config || {};
        const rounds = cfg.stability_polish_rounds || 3;
        const topK = s.vf_top_k_voltages || [];
        const which = s.vf_refinement_index != null && topK.length
          ? ` (top-${topK.length} #${(s.vf_refinement_index|0)+1})` : '';
        progress = `Polish round ${s.polish_round || 0}/${rounds}${which}: ${safePhaseDetail}`;
        donePct = rounds > 0 ? Math.min(1, (s.polish_round || 0) / rounds) : 0;
      } else {
        progress = safePhaseDetail || safePhase;
      }
      const startedAt = s.current_step_started_at;
      const elapsed = startedAt ? formatDuration(Math.max(0, Math.floor(Date.now()/1000 - startedAt))) : '—';
      const inProgressLogBtn = stepV > 0
        ? `<button class="secondary" data-action="showVoltageLogModal" data-stop-propagation="true" data-arg-voltage="${stepV}" title="Watch this step's log live" style="font-size:0.75em;padding:2px 6px">Log</button>`
        : '';
      const bar = donePct > 0
        ? `<div class="progress-bar" style="width:140px;height:10px;display:inline-block;vertical-align:middle;margin-right:8px"><div class="progress-bar-fill striped" style="width:${(donePct*100).toFixed(1)}%"></div></div>`
        : '';
      progressRow = `<div class="stat-row" style="background:#1a2a3a">` +
        `<span class="stat-value" style="flex:0 0 110px"><span class="pill" style="background:var(--accent);color:#fff;padding:2px 8px;font-size:0.72em;font-weight:600">TUNING</span></span>` +
        `<span class="stat-value" style="flex:0 0 80px">${stepV} mV</span>` +
        `<span class="stat-value" style="flex:0 0 120px">${label}</span>` +
        `<span class="stat-value" style="flex:0 0 110px">—</span>` +
        `<span class="stat-value" style="flex:0 0 110px">—</span>` +
        `<span class="stat-value" style="flex:0 0 130px">${elapsed}</span>` +
        `<span class="stat-value" style="flex:1; text-align:right; color:var(--text2); font-weight:500">${bar}${progress}</span>` +
        `<span class="stat-value" style="flex:0 0 260px; text-align:right">${inProgressLogBtn}</span>` +
        `</div>`;
    }
    vrEl.innerHTML = header + completedRows + progressRow;
  } else {
    vrEl.innerHTML = '<div class="stat-row"><span class="stat-label">No voltage sweep data yet</span></div>';
  }

  // Live push only in '1h' mode — longer ranges are seeded snapshots and
  // would visually shift if we appended live samples on top.
  if(ts.hashrate_ths && currentMetricsRange === '1h'){
    const t = new Date().toLocaleTimeString();
    pushChart(charts.hashrate, t, ts.hashrate_ths);
    pushChart(charts.power, t, ts.power_w||0);
    pushChart(charts.efficiency, t, ts.efficiency_jth||0);
  }
  const rentalEl = document.getElementById('detail-rental-status');
  if (rentalEl) rentalEl.innerHTML = renderMrrPill(s.mrr_rental_status, currentMac());
}

// Heatmap
// ─── topology + cell dispatch helpers (shared by left/right panes) ────────
// Visual grid is 9 cols × 12 rows = 108 chips per board (S21-family default layout).
// physical "9 chips per domain × 12 domains" layout. Hardcoded for now —
// other miner models would need a per-model derivation.
const HM_COLS = 9, HM_CELL_W = 28, HM_CELL_H = 22, HM_GAP = 2, HM_BOARD_GAP = 16, HM_LABEL_H = 20;
function _hmTopology(){
  // Board/chip counts come from the backend's status payload (backend reads
  // them from the miner's /capabilities). Fall back to live data length, then
  // legacy 3×108 — belt-and-suspenders for the first-paint race where status
  // hasn't landed yet but /tuner/live/{ip} has. Never hardcode these;
  // different miner models have different topologies.
  const boards = tunerStatus?.num_boards
              || heatmapData.clocks?.length
              || heatmapData.hashrate?.length
              || heatmapData.chip_temps?.length
              || 3;
  const chips  = tunerStatus?.chips_per_board
              || heatmapData.clocks?.[0]?.Data?.length
              || heatmapData.hashrate?.[0]?.Data?.length
              || 108;
  const cols = HM_COLS;
  const rows = Math.ceil(chips/cols);
  return { boards, chips, cols, rows };
}
// Single dispatch table mapping (mode, board, chip, previewArr) → {value, color, valid}.
// Keeps drawHeatmap and the tooltip handler in lockstep; adding a new mode means
// adding one entry here and one button in the HTML.
function _hmCell(mode, b, c, previewArr){
  const stock = heatmapData.stock || {};
  const sFreq = stock.chip_freqs?.[b]?.[c];
  const sHealth = stock.chip_health?.[b]?.[c];
  const sTemp = stock.chip_temps?.[b]?.[c];
  const sHr = stock.chip_hashrates?.[b]?.[c];
  switch(mode){
    case 'freq': {
      const v = previewArr?.[b]?.[c] ?? heatmapData.clocks?.[b]?.Data?.[c];
      return Number.isFinite(v) ? {value:v, color:freqColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'health': {
      const cd = heatmapData.hashrate?.[b]?.Data?.[c];
      const v = cd ? cd[1] : null;
      return Number.isFinite(v) ? {value:v, color:healthColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'temp': {
      const v = heatmapData.chip_temps?.[b]?.Data?.[c];
      return Number.isFinite(v) ? {value:v, color:tempColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'hashrate': {
      const cd = heatmapData.hashrate?.[b]?.Data?.[c];
      const v = cd ? cd[0]/1000 : null;
      return Number.isFinite(v) ? {value:v, color:hashrateColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'p2_freq': {
      const v = heatmapData.p2_freq?.[b]?.[c];
      return Number.isFinite(v) ? {value:v, color:freqColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'p2_health': {
      const v = heatmapData.baseline?.[b]?.[c];
      return Number.isFinite(v) ? {value:v, color:healthColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'p2_temp': {
      const v = heatmapData.p2_temp?.[b]?.[c];
      return Number.isFinite(v) ? {value:v, color:tempColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'p2_hashrate': {
      const v = heatmapData.p2_hashrate?.[b]?.[c];
      return Number.isFinite(v) ? {value:v, color:hashrateColor(v), valid:true} : {value:0, color:'#333', valid:false};
    }
    case 'stock_freq':
      return Number.isFinite(sFreq) ? {value:sFreq, color:freqColor(sFreq), valid:true} : {value:0, color:'#333', valid:false};
    case 'stock_health':
      return Number.isFinite(sHealth) ? {value:sHealth, color:healthColor(sHealth), valid:true} : {value:0, color:'#333', valid:false};
    case 'stock_temp':
      return Number.isFinite(sTemp) ? {value:sTemp, color:tempColor(sTemp), valid:true} : {value:0, color:'#333', valid:false};
    case 'stock_hashrate':
      return Number.isFinite(sHr) ? {value:sHr, color:hashrateColor(sHr), valid:true} : {value:0, color:'#333', valid:false};
    default:
      return {value:0, color:'#333', valid:false};
  }
}
// Returns the per-board array for label-row stats (avg/min/max/spread) so a
// Board N: ... summary line can be rendered consistently across all modes.
function _hmBoardArray(mode, b, previewArr){
  const stock = heatmapData.stock || {};
  switch(mode){
    case 'freq':       return (previewArr?.[b]) || (heatmapData.clocks?.[b]?.Data) || null;
    case 'health':     return (heatmapData.hashrate?.[b]?.Data || []).map(cd => cd ? cd[1] : null).filter(v => Number.isFinite(v));
    case 'temp':       return heatmapData.chip_temps?.[b]?.Data || null;
    case 'hashrate':   return (heatmapData.hashrate?.[b]?.Data || []).map(cd => cd ? cd[0]/1000 : null).filter(v => Number.isFinite(v));
    case 'p2_freq':    return heatmapData.p2_freq?.[b] || null;
    case 'p2_health':  return heatmapData.baseline?.[b] || null;
    case 'p2_temp':    return heatmapData.p2_temp?.[b] || null;
    case 'p2_hashrate':return heatmapData.p2_hashrate?.[b] || null;
    case 'stock_freq':     return stock.chip_freqs?.[b] || null;
    case 'stock_health':   return stock.chip_health?.[b] || null;
    case 'stock_temp':     return stock.chip_temps?.[b] || null;
    case 'stock_hashrate': return stock.chip_hashrates?.[b] || null;
    default: return null;
  }
}
// Unit suffix for the per-board label and tooltip — matches the cell value's units.
function _hmUnit(mode){
  if(mode === 'freq' || mode === 'p2_freq' || mode === 'stock_freq') return 'MHz';
  if(mode === 'temp' || mode === 'p2_temp' || mode === 'stock_temp') return 'C';
  if(mode === 'hashrate' || mode === 'p2_hashrate' || mode === 'stock_hashrate') return 'MH/s';
  return '%'; // health
}
function _hmIsPreview(mode){ return mode === 'freq'; }

function setHeatmapMode(mode, side){
  if(side === 'left'){ heatmapModeLeft = mode; }
  else { heatmapModeRight = mode; }
  // Scope active-class reset to the entire pane (covers both Phase 2 + Stock
  // rows on the right side) so only one button is highlighted per pane,
  // matching the single-canvas-shows-one-mode reality. Without pane-wide
  // scoping, both rows on the right would each keep their own "active" button
  // and operators couldn't tell which was actually displayed.
  const paneEl = event?.target?.closest?.('.heatmap-pane');
  if(paneEl){
    paneEl.querySelectorAll('.heatmap-controls button').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
  }
  // Freq preview only applies to live freq mode (left side). Switching the
  // left side off freq auto-clears the preview; right-side switches don't
  // affect it.
  if(side === 'left' && mode !== 'freq' && heatmapPreview){
    heatmapPreview = null;
    updatePreviewBanner();
    pollDetail();
  }
  drawHeatmap(side);
}
function drawHeatmap(side){
  // When called with no side (legacy/global poll), draw both panes.
  if(side === undefined){
    drawHeatmap('left');
    drawHeatmap('right');
    return;
  }
  const mode = (side === 'left') ? heatmapModeLeft : heatmapModeRight;
  const canvas = document.getElementById(side === 'left' ? 'heatmap-left' : 'heatmap-right');
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const { boards, chips, cols, rows } = _hmTopology();
  const cellW=HM_CELL_W, cellH=HM_CELL_H, gap=HM_GAP, boardGap=HM_BOARD_GAP, labelH=HM_LABEL_H;
  // Per-pane label container — independent rebuild so each side's row count
  // tracks its own pane state without crossing wires.
  const labelContainer = document.getElementById(side === 'left' ? 'hm-board-labels-left' : 'hm-board-labels-right');
  if(labelContainer && labelContainer.childElementCount !== boards){
    labelContainer.innerHTML = Array.from({length: boards}, (_, b) =>
      `<span id="hm-board-${side}-${b}">Board ${b}: --</span>`).join('');
  }
  const totalW = cols*(cellW+gap)+gap;
  const totalH = boards*(rows*(cellH+gap)+gap+labelH)+(boards-1)*boardGap;
  canvas.width=totalW*dpr; canvas.height=totalH*dpr;
  canvas.style.width=totalW+'px'; canvas.style.height=totalH+'px';
  ctx.scale(dpr,dpr); ctx.clearRect(0,0,totalW,totalH);

  // Preview source for left-side freq mode — falls back to live clocks when absent.
  const previewArr = (_hmIsPreview(mode) && heatmapPreview?.stable_freq_arrays) || null;
  const unit = _hmUnit(mode);

  for(let b=0;b<boards;b++){
    const yOff = b*(rows*(cellH+gap)+gap+labelH+boardGap);
    ctx.fillStyle='#8b8fa3'; ctx.font='11px system-ui';
    let lbl = `Board ${b}`;
    const arr = _hmBoardArray(mode, b, previewArr);
    if(Array.isArray(arr) && arr.length){
      const filtered = arr.filter(v => Number.isFinite(v));
      if(filtered.length){
        const avg = filtered.reduce((a,v) => a+v, 0)/filtered.length;
        const mn = Math.min(...filtered);
        const mx = Math.max(...filtered);
        if(_hmIsPreview(mode) && previewArr?.[b]){
          lbl += ` — [PREVIEW] avg ${avg.toFixed(0)} ${unit}, spread ${(mx-mn).toFixed(0)} ${unit}`;
        } else if(unit === 'MHz') {
          lbl += ` — avg ${avg.toFixed(0)} ${unit}, spread ${(mx-mn).toFixed(0)} ${unit}`;
        } else {
          lbl += ` — avg ${avg.toFixed(1)} ${unit}, min ${mn.toFixed(1)}, max ${mx.toFixed(1)}`;
        }
      }
    }
    ctx.fillText(lbl, gap, yOff+12);

    for(let c=0;c<chips;c++){
      const col=c%cols, row=Math.floor(c/cols);
      const x=gap+col*(cellW+gap), y=yOff+labelH+gap+row*(cellH+gap);
      const cell = _hmCell(mode, b, c, previewArr);
      ctx.fillStyle=cell.color; ctx.fillRect(x,y,cellW,cellH);
      if(cell.valid){
        ctx.fillStyle='#fff'; ctx.font='9px monospace';
        const txt = Math.round(cell.value);
        ctx.fillText(txt, x+2, y+14);
      }
    }
  }
  // Mirror the canvas's per-board summary into the inline labels row beneath.
  // Live clocks get an MHz summary; baseline modes show the metric the pane
  // is currently displaying so the operator can confirm what they're looking at.
  for(let b=0;b<boards;b++){
    const el=document.getElementById(`hm-board-${side}-${b}`);
    if(!el) continue;
    const arr = _hmBoardArray(mode, b, previewArr);
    if(Array.isArray(arr) && arr.length){
      const filtered = arr.filter(v => Number.isFinite(v));
      if(filtered.length){
        const avg = filtered.reduce((a,v) => a+v, 0)/filtered.length;
        el.textContent = `Board ${b}: avg ${avg.toFixed(1)} ${unit}`;
      } else {
        el.textContent = `Board ${b}: no data`;
      }
    } else {
      el.textContent = `Board ${b}: no data`;
    }
  }
}
function freqColor(v){ return v<400?'#1a237e':v<450?'#1565c0':v<500?'#2196f3':v<550?'#4caf50':v<600?'#ff9800':'#f44336'; }
function healthColor(v){ return v>=98?'#4caf50':v>=90?'#8bc34a':v>=80?'#ffc107':v>=60?'#ff9800':'#f44336'; }
function tempColor(v){ return v<70?'#1565c0':v<80?'#2196f3':v<85?'#4caf50':v<93?'#ff9800':'#f44336'; }
function hashrateColor(v){ return v<400?'#f44336':v<500?'#ff9800':v<600?'#ffc107':v<700?'#4caf50':'#2196f3'; }

// Tooltip — one registration per pane. Each handler reads its own canvas's
// rect and its own pane's mode so coordinates stay anchored to the right
// canvas. The body is rendered identically (same per-chip facts), with an
// extra "vs Live …" delta line on right-pane baseline modes when the live
// counterpart is available — that's the comparison-side-by-side affordance.
function _hmAttachTooltip(side){
  const canvas = document.getElementById(side === 'left' ? 'heatmap-left' : 'heatmap-right');
  const tooltip = document.getElementById(side === 'left' ? 'heatmap-tooltip-left' : 'heatmap-tooltip-right');
  if(!canvas || !tooltip) return;
  canvas.addEventListener('mousemove', function(e){
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const { boards, chips, cols, rows } = _hmTopology();
    const cellW=HM_CELL_W, cellH=HM_CELL_H, gap=HM_GAP, boardGap=HM_BOARD_GAP, labelH=HM_LABEL_H;
    const boardH = rows*(cellH+gap)+gap+labelH;
    for(let b=0;b<boards;b++){
      const yOff = b*(boardH+boardGap), ly = y-yOff-labelH;
      if(ly<0) continue;
      const col = Math.floor((x-gap)/(cellW+gap)), row = Math.floor((ly-gap)/(cellH+gap));
      if(col<0 || col>=cols || row<0) continue;
      const chip = row*cols+col;
      if(chip>=chips) continue;
      let html = `<b>Board ${b}, Chip ${chip}</b><br>`;
      // Live values (always shown — the operator wants live as a reference no
      // matter which pane they're on).
      if(heatmapPreview?.stable_freq_arrays?.[b]){
        html += `<span style="color:var(--accent)">[PREVIEW]</span> Freq: ${heatmapPreview.stable_freq_arrays[b][chip]?.toFixed(1)||'--'} MHz<br>`;
      } else if(heatmapData.clocks?.[b]){
        html += `Freq: ${(heatmapData.clocks[b].Data||[])[chip]?.toFixed(1)||'--'} MHz<br>`;
      }
      if(heatmapData.hashrate?.[b]){ const cd=(heatmapData.hashrate[b].Data||[])[chip]; if(cd) html+=`HR: ${(cd[0]/1000).toFixed(0)} MH/s | Eff: ${cd[1].toFixed(1)}% | HP: ${cd[2].toFixed(1)}%<br>`; }
      if(heatmapData.chip_temps?.[b]) html += `Temp: ${(heatmapData.chip_temps[b].Data||[])[chip]?.toFixed(1)||'--'}C<br>`;
      // Phase 2 baseline rows (only show when the per-chip array has data for
      // this chip — avoids "--" noise on fresh installs that haven't hit
      // Phase 2 yet).
      const p2Health = heatmapData.baseline?.[b]?.[chip];
      const p2Freq = heatmapData.p2_freq?.[b]?.[chip];
      const p2Temp = heatmapData.p2_temp?.[b]?.[chip];
      const p2Hr = heatmapData.p2_hashrate?.[b]?.[chip];
      if(Number.isFinite(p2Health)) html += `P2 HP: ${p2Health.toFixed(1)}<br>`;
      if(Number.isFinite(p2Freq)) html += `P2 Freq: ${p2Freq.toFixed(1)} MHz<br>`;
      if(Number.isFinite(p2Temp)) html += `P2 Temp: ${p2Temp.toFixed(1)}C<br>`;
      if(Number.isFinite(p2Hr)) html += `P2 HR: ${p2Hr.toFixed(0)} MH/s<br>`;
      // Stock baseline rows (same skip-when-missing pattern).
      const stock = heatmapData.stock || {};
      const sFreq = stock.chip_freqs?.[b]?.[chip];
      const sHealth = stock.chip_health?.[b]?.[chip];
      const sTemp = stock.chip_temps?.[b]?.[chip];
      const sHr = stock.chip_hashrates?.[b]?.[chip];
      if(Number.isFinite(sFreq)) html += `Stock Freq: ${sFreq.toFixed(1)} MHz<br>`;
      if(Number.isFinite(sHealth)) html += `Stock HP: ${sHealth.toFixed(1)}<br>`;
      if(Number.isFinite(sTemp)) html += `Stock Temp: ${sTemp.toFixed(1)}C<br>`;
      if(Number.isFinite(sHr)) html += `Stock HR: ${sHr.toFixed(0)} MH/s<br>`;
      if(tunerStatus.stable_freq_arrays?.[b]) html += `Stable (live): ${tunerStatus.stable_freq_arrays[b][chip]?.toFixed(1)||'--'} MHz`;
      tooltip.innerHTML = html;
      tooltip.style.display = 'block';
      tooltip.style.left = (e.clientX-rect.left+12)+'px';
      tooltip.style.top = (e.clientY-rect.top-10)+'px';
      return;
    }
    tooltip.style.display = 'none';
  });
  canvas.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
}
_hmAttachTooltip('left');
_hmAttachTooltip('right');

// Log
async function updateLog(){
  const pollMac = currentMac();
  if (!pollMac) return;
  const data = await fetchJSON(`/tuner/log/${currentMacDashes()}`);
  if(!data) return;
  // Drop stale data if the operator navigated away mid-poll.
  if (pollMac !== currentMac()) return;
  const el = document.getElementById('log-container');
  const wasBottom = el.scrollTop+el.clientHeight >= el.scrollHeight-20;
  el.innerHTML = (data.lines||[]).map(l=>`<div class="log-line">${escapeHTML(l)}</div>`).join('');
  if(wasBottom) el.scrollTop = el.scrollHeight;
}
function downloadLog(){
  const text = document.getElementById('log-container').innerText;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text],{type:'text/plain'}));
  a.download = `${currentIp() || currentMac()}-tune-log.txt`; a.click();
}

function togglePerBoard(id){
  const el = document.getElementById(id);
  if(el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

// ─── V/F Efficiency Surface (SVG) ──────────────────────────────────────────
// Renders the Phase V coarse (and optional fine) grid as an SVG heatmap.
// Rows are unique voltages (high → low), columns are unique frequencies
// (low → high). Cell states: stable-measured (viridis colored + J/TH label),
// unstable-measured (diagonal red hatch + ✗), fail-fast-skipped (diagonal
// gray hatch), pending (planned but not yet measured, dashed outline),
// measuring-now (pulsing accent ring + ⟳). Winner gets a gold overlay
// border; top-K seeds get a blue overlay border.
// Purple (worst) → green (best). Adapted from viridis with the top stop
// swapped from yellow to green for a clearer "good" signal at a glance.
const VIRIDIS = ['#440154','#3b528b','#21918c','#5ec962','#1fa34a'];
function vfViridis(t){
  t = Math.max(0, Math.min(1, t));
  if(!isFinite(t)) t = 0;
  const n = VIRIDIS.length - 1;
  const i = Math.min(n - 1, Math.floor(t * n));
  const local = t * n - i;
  const parse = h => [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)];
  const [ar,ag,ab] = parse(VIRIDIS[i]);
  const [br,bg,bb] = parse(VIRIDIS[i+1]);
  const mix = (x,y) => Math.round(x + (y - x) * local);
  return `rgb(${mix(ar,br)},${mix(ag,bg)},${mix(ab,bb)})`;
}
// Stash the last-rendered grid context so the click modal and keyboard nav
// can look up cell data without re-deriving it.
let _vfGridState = null;
const _vfEscape = s => String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderVFSurface(status){
  const card = document.getElementById('vf-surface-card');
  const gridEl = document.getElementById('vf-surface-grid');
  const progressEl = document.getElementById('vf-progress');
  const summaryEl = document.getElementById('vf-surface-summary');
  const legendEl = document.getElementById('vf-surface-legend');
  const descEl = document.getElementById('vf-surface-description');

  const surface = (status && status.vf_surface) || [];
  const planned = (status && status.vf_planned_grid) || [];
  const skipped = (status && status.vf_skipped) || [];
  let topK = (status && status.vf_top_k_voltages) || [];
  const topKIsBackend = topK.length > 0;
  const current = status && status.current_vf_point;
  const remeasureQueue = (status && status.remeasure_queue) || [];
  const inPhaseV = status && status.phase === 'phase_v_exploration';

  if(!surface.length && !planned.length){
    card.style.display = 'none';
    _vfGridState = null;
    return;
  }
  card.style.display = '';

  // Axis-mode pivot for vendors whose tuning sweeps wattage instead of
  // voltage (Whatsminer's `power_limit_freq_search` strategy). Whatsminer
  // cells in vf_surface carry `power_limit_w` populated + `voltage_mv: null`,
  // and the backend's planned-grid emitter mirrors that shape — so we just
  // need to read from a different field and label the axis differently. ePIC
  // / Bixbit / LuxOS stay as-is (voltage × frequency). Detected via the
  // capabilities block on status; falls back to `firmware_type === 'whatsminer'`
  // when capabilities haven't been populated (test fixtures with minimal
  // stub data).
  const isWattage = !!(
    (status && status.capabilities && status.capabilities.power_limit_freq_search_strategy)
    || (status && status.firmware_type === 'whatsminer')
  );
  const yField = isWattage ? 'power_limit_w' : 'voltage_mv';
  const yUnit = isWattage ? 'W' : 'mV';
  const yLabel = isWattage ? 'Power limit' : 'Voltage';
  const yAxisDesc = isWattage ? 'wattage' : 'voltage';

  // Axis built from COARSE cells only — fine measurements render as sub-cells
  // nested inside their owning coarse cell, not as new rows/columns. Fine
  // entries that coincide with a coarse (V, F) never get the fine: true flag
  // (tuner dedupes against the initial coarse planned set), so filtering
  // `fine` out here can't drop a needed axis value.
  const volSet = new Set();
  const freqSet = new Set();
  const addAxis = e => { volSet.add(e[yField]); freqSet.add(Number(e.freq_mhz)); };
  surface.filter(e => !e.fine).forEach(addAxis);
  planned.filter(p => !p.fine).forEach(addAxis);
  skipped.forEach(addAxis);
  if(current && !current.fine) addAxis(current);
  // `voltages` is the legacy local name; it now carries the Y-axis values
  // for whichever axis mode is active (mV when voltage, W when wattage).
  const voltages = [...volSet].filter(v => v != null).sort((a,b) => b - a);
  const freqs = [...freqSet].sort((a,b) => a - b);

  // Rewrite the chart description to match the active axis-mode. For
  // wattage miners, drop the top-K-voltages sentence — that concept is
  // chip-tune-specific and doesn't apply to power_limit_freq_search.
  if (descEl) {
    if (isWattage) {
      descEl.textContent =
        'Coarse grid — J/TH at each (wattage, freq) point. ' +
        'Best point bordered in gold. Click any cell for details.';
    } else {
      descEl.textContent =
        'Phase V coarse grid — uniform-frequency J/TH at each (voltage, freq) point. ' +
        'Best point bordered in gold; top-K voltages (selected for per-chip refinement) ' +
        'bordered in blue. Click any cell for details.';
    }
  }

  const measuredByKey = new Map();
  surface.forEach(e => measuredByKey.set(`${e[yField]}|${Number(e.freq_mhz)}`, e));
  const plannedKeys = new Set(planned.map(p => `${p[yField]}|${Number(p.freq_mhz)}`));
  const skippedKeys = new Set(skipped.map(s => `${s[yField]}|${Number(s.freq_mhz)}`));
  // topKSet is built below — after the preview topK computation — so
  // border rendering and the cell popup see the same membership.
  const remeasureKeys = new Set(remeasureQueue.map(q => `${q[yField]}|${Number(q.freq_mhz)}`));
  const currentKey = current ? `${current[yField]}|${Number(current.freq_mhz)}` : null;

  // Bucket fine entries (measured, pending, in-flight) by their owning coarse
  // cell. Ownership = nearest coarse (V, F); ties go to the first element
  // encountered (higher V since voltages is desc, lower F since freqs is asc).
  const findNearest = (arr, v) => {
    if(!arr.length) return null;
    let best = arr[0], bestD = Math.abs(v - best);
    for(let k = 1; k < arr.length; k++){
      const d = Math.abs(v - arr[k]);
      if(d < bestD){ best = arr[k]; bestD = d; }
    }
    return best;
  };
  const fineByCoarse = new Map();
  // R1 guard: after the backend's in-cell subdivision, fine dv/df should stay
  // strictly within ±coarse_step/2. Legacy profiles (pre-R1) can have fine
  // cells that land on or past neighbor coarse cells. findNearest still
  // buckets them into *some* coarse cell, so rendering stays sane, but we
  // warn once per offending cell so developers know when stale data is in play.
  const vSortedDesc = [...voltages];  // already desc
  const fSortedAsc = [...freqs];  // already asc
  const coarseVStep = vSortedDesc.length > 1
    ? (vSortedDesc[0] - vSortedDesc[vSortedDesc.length - 1]) / (vSortedDesc.length - 1)
    : Infinity;
  const coarseFStep = fSortedAsc.length > 1
    ? (fSortedAsc[fSortedAsc.length - 1] - fSortedAsc[0]) / (fSortedAsc.length - 1)
    : Infinity;
  const addFine = (fineV, fineF, payload) => {
    const cv = findNearest(voltages, fineV);
    const cf = findNearest(freqs, fineF);
    if(cv == null || cf == null) return;
    const dv = fineV - cv, df = fineF - cf;
    if(Math.abs(dv) > coarseVStep / 2 + 0.5 || Math.abs(df) > coarseFStep / 2 + 0.01){
      console.warn(`Fine cell (${fineV} mV, ${fineF} MHz) exceeds ±coarse_step/2 around `
        + `(${cv}, ${cf}) — legacy profile or config change? `
        + `dv=${dv.toFixed(1)} (bound ${(coarseVStep/2).toFixed(1)}), `
        + `df=${df.toFixed(2)} (bound ${(coarseFStep/2).toFixed(2)})`);
    }
    const ck = `${cv}|${cf}`;
    if(!fineByCoarse.has(ck)) fineByCoarse.set(ck, []);
    fineByCoarse.get(ck).push({ ...payload, fineV, fineF, dv, df });
  };
  surface.filter(e => e.fine).forEach(e =>
    addFine(e[yField], Number(e.freq_mhz), { kind: 'measured', entry: e }));
  const currentFineKey = (current && current.fine)
    ? `${current[yField]}|${Number(current.freq_mhz)}`
    : null;
  planned.filter(p => p.fine).forEach(p => {
    const mk = `${p[yField]}|${Number(p.freq_mhz)}`;
    if(measuredByKey.has(mk)) return;       // already measured
    if(mk === currentFineKey) return;       // currently in flight
    addFine(p[yField], Number(p.freq_mhz), { kind: 'pending' });
  });
  if(current && current.fine){
    addFine(current[yField], Number(current.freq_mhz), { kind: 'measuring' });
  }
  // Per-bucket sub-grid layout: unique dv desc (higher V on top), df asc
  // (lower F on left). Sub-grid tile size = cellW/subCols × cellH/subRows.
  const fineLayoutByCoarse = new Map();
  for(const [ck, items] of fineByCoarse){
    const dvSorted = [...new Set(items.map(it => it.dv))].sort((a,b) => b - a);
    const dfSorted = [...new Set(items.map(it => it.df))].sort((a,b) => a - b);
    fineLayoutByCoarse.set(ck, { items, dvSorted, dfSorted });
  }

  // Chip-tune override: when a voltage_results[] entry maps back to a surface
  // cell (via vf_source.freq_mhz or seed_f_mhz within FREQ_SEARCH_TOLERANCE),
  // substitute the chip-tune J/TH for display. A fine cell that got chip-tuned
  // then shows the post-tune efficiency (both in color and label), the cell
  // participates in winner selection at the better value, and the gold border
  // naturally follows. Same logic the cell-click modal uses for before/after.
  //
  // Skipped for wattage-axis miners (Whatsminer): the `power_limit_freq_search`
  // strategy doesn't run per-chip tuning, so `voltage_results` is always empty
  // and the inner loops would no-op. Skipping makes the intent explicit.
  const chipTuneByKey = new Map();
  if (!isWattage) {
    const vrList = (status && status.voltage_results) || [];
    const freqTol = (status && status.config && status.config.freq_search_tolerance_mhz) || 7;
    vrList.forEach(r => {
      if(r.efficiency_jth == null) return;
      const rv = Number(r.voltage_mv);
      const rf = r.vf_source && r.vf_source.freq_mhz != null
        ? Number(r.vf_source.freq_mhz)
        : (r.seed_f_mhz != null ? Number(r.seed_f_mhz) : null);
      if(rf == null) return;
      let bestKey = null, bestDelta = Infinity;
      for(const e of surface){
        if(Number(e.voltage_mv) !== rv) continue;
        const delta = Math.abs(Number(e.freq_mhz) - rf);
        if(delta <= freqTol && delta < bestDelta){
          bestKey = `${e.voltage_mv}|${Number(e.freq_mhz)}`;
          bestDelta = delta;
        }
      }
      if(bestKey){
        const existing = chipTuneByKey.get(bestKey);
        if(!existing || r.efficiency_jth < existing.efficiency_jth){
          chipTuneByKey.set(bestKey, r);
        }
      }
    });
  }
  const effectiveEntry = (e) => {
    if(!e) return e;
    const k = `${e[yField]}|${Number(e.freq_mhz)}`;
    const ct = chipTuneByKey.get(k);
    if(!ct || ct.efficiency_jth == null) return e;
    return Object.assign({}, e, {
      efficiency_jth: ct.efficiency_jth,
      hashrate_ths: ct.hashrate_ths != null ? ct.hashrate_ths : e.hashrate_ths,
      power_w: ct.power_w != null ? ct.power_w : e.power_w,
      _chipTune: ct,
      _originalJth: e.efficiency_jth,
    });
  };

  // J/TH range for color scale and winner (efficiency mode).
  // In profit mode, the heatmap colors cells by $/day instead: highest
  // profit maps to viridis t=1 (best), lowest to t=0 (worst). The engine
  // stamps profit_usd_day on top_tunes but NOT on raw vf_surface entries;
  // we compute it client-side from hashrate_ths + power_w + the cached
  // minerstat coin data so the color mapping matches what the engine
  // actually ranks.
  const withData = surface.filter(e => e.efficiency_jth != null).map(effectiveEntry);
  const jthValues = withData.map(e => e.efficiency_jth);
  const bestJth = jthValues.length ? Math.min(...jthValues) : null;
  const worstJth = jthValues.length ? Math.max(...jthValues) : null;
  // Profit-mode color-mapping support. If the engine is in profit mode
  // AND we have a fresh minerstat snapshot for its coin, we color cells
  // by $/day. Otherwise we fall back to J/TH (so an operator who flipped
  // the mode but hasn't fetched minerstat yet still sees a valid heatmap).
  // Read from `status` (the parameter) — the outer updateStatus scope's `s`
  // isn't in scope here.
  const vfTargetMode = (status && status.config && status.config.target_mode) || 'efficiency';
  const vfIsProfit = vfTargetMode === 'profitability';
  const vfCoinId = (status && status.config && status.config.minerstat_coin) || 'BTC';
  const vfRate = (status && status.config && status.config.electric_rate_per_kwh) || 0.10;
  // Revenue-side modifier (percent). 0 = no adjustment. Applied only to
  // revenue; cost math is untouched. Matches the backend's
  // compute_profit_usd_per_day semantics so heatmap colors and cell-modal
  // numbers agree with top_tunes / recompute-preview rows.
  const vfModifierPct = (status && status.config
    && Number.isFinite(status.config.income_modifier_pct))
    ? Number(status.config.income_modifier_pct) : 0;
  const vfCoinData = (minerstatSnapshot && minerstatSnapshot.coins)
    ? minerstatSnapshot.coins[vfCoinId] : null;
  const canColorByProfit = vfIsProfit && vfCoinData;
  // Computes $/day whenever we have coin data — NOT gated on profit mode —
  // so the cell detail modal can surface profit even when the heatmap is
  // coloring by J/TH. Heatmap color/label paths below still AND this against
  // `canColorByProfit` so efficiency mode still colors by J/TH.
  const vfProfitForEntry = (entry) => {
    if (!vfCoinData || !entry) return null;
    if (entry.hashrate_ths == null || entry.power_w == null) return null;
    try {
      const c = vfCoinData;
      const coinPerThDay = (86400 / c.block_time_s) * c.reward_block * (1e12 / c.network_hashrate);
      const revenue = entry.hashrate_ths * coinPerThDay * c.price_usd
        * (1 + vfModifierPct / 100);
      const cost = (entry.power_w * 24 / 1000) * vfRate;
      return revenue - cost;
    } catch(e) { return null; }
  };
  const profitValues = canColorByProfit
    ? withData.map(vfProfitForEntry).filter(p => p != null)
    : [];
  const bestProfit = profitValues.length ? Math.max(...profitValues) : null;
  const worstProfit = profitValues.length ? Math.min(...profitValues) : null;
  // "Best entry" for the gold-border marker: profit-wins in profit mode,
  // J/TH-wins otherwise. Falls back to J/TH when profit data is missing.
  // withData holds effective entries — when a chip-tune override was merged,
  // `.fine` is preserved from the source surface cell, so the fine-winner
  // sub-cell positioning logic still works below.
  const bestEntry = (canColorByProfit && bestProfit != null)
    ? withData.find(e => {
        const p = vfProfitForEntry(e);
        return p != null && p === bestProfit;
      })
    : (bestJth != null ? withData.find(e => e.efficiency_jth === bestJth) : null);

  // Live top-K preview. Backend only writes vf_top_k_voltages AFTER the
  // coarse walk converges, so during the long Phase V exploration the blue
  // top-K seed borders would otherwise be invisible. We compute a preview
  // from the current vf_surface: top-K distinct (V, F) cells ranked by the
  // active scoring function — matches the backend's post-convergence +
  // post-fine-refinement output (no voltage dedup — two cells at the same
  // voltage with different F can both be top-K; Phase 3 chip-tune at each
  // uses its own seed_f as the iterative loop's center). Fine cells ARE
  // eligible: a fine cell's J/TH reading is higher-resolution than its
  // parent coarse cell, and a fine-cell top-K renders as a sub-cell blue
  // border via the vf_source.kind==='fine' branch in the border-draw pass.
  // Refreshes every poll; falls back to the backend list the moment it's
  // populated.
  const topKTarget = (status && status.config && status.config.vf_explore_top_k) || 0;
  let topKIsPreview = false;
  // Whatsminer (wattage axis) skips top-K: the `power_limit_freq_search`
  // strategy doesn't have a chip-tune phase, so there's no "top-K seed"
  // concept. Leave `topK` empty so no gold/blue borders render.
  if (!isWattage && !topKIsBackend && topKTarget > 0 && withData.length > 0) {
    const scoreOf = (e) => {
      if (canColorByProfit) {
        const p = vfProfitForEntry(e);
        // In profit mode, higher $/day = better, so negate so min() picks best.
        return (p == null) ? Infinity : -p;
      }
      return e.efficiency_jth != null ? e.efficiency_jth : Infinity;
    };
    const ranked = withData
      .map(e => Object.assign({}, e, { _score: scoreOf(e) }))
      .filter(e => Number.isFinite(e._score))
      .sort((a, b) => a._score - b._score)
      .slice(0, topKTarget);
    if (ranked.length > 0) {
      topK = ranked.map(e => ({
        voltage_mv: e.voltage_mv,
        seed_f_mhz: Number(e.freq_mhz),
        vf_source: {
          kind: e.fine ? 'fine' : 'coarse',
          voltage_mv: e.voltage_mv,
          freq_mhz: Number(e.freq_mhz),
        },
        _preview: true,
      }));
      topKIsPreview = true;
    }
  }
  const topKSet = isWattage
    ? new Set()
    : new Set(topK.map(t => `${t.voltage_mv}|${Number(t.seed_f_mhz)}`));

  const cellFill = (entry) => {
    if (!entry) return 'var(--bg2)';
    if (canColorByProfit && bestProfit != null && worstProfit != null) {
      const p = vfProfitForEntry(entry);
      if (p == null) return 'var(--bg2)';
      if (bestProfit === worstProfit) return vfViridis(1);
      // HIGHER profit = t=1 (yellow = best). No inversion needed.
      return vfViridis((p - worstProfit) / (bestProfit - worstProfit));
    }
    const jth = entry.efficiency_jth;
    if (jth == null || bestJth == null || worstJth == null) return 'var(--bg2)';
    if (worstJth === bestJth) return vfViridis(1);
    // Efficiency mode: invert so lowest J/TH maps to t=1 (yellow).
    return vfViridis(1 - (jth - bestJth) / (worstJth - bestJth));
  };

  // Layout — responsive to container width, clamped to a readable range.
  const colCount = freqs.length;
  const rowCount = voltages.length;
  const labelW = 58;
  const topPad = 8;
  const axisH = 26;
  const gap = 2;
  const containerW = Math.max(400, gridEl.clientWidth || 600);
  const cellW = Math.max(34, Math.min(72,
    Math.floor((containerW - labelW - 18) / Math.max(1, colCount)) - gap));
  const cellH = 32;
  const svgW = labelW + colCount * (cellW + gap) + 6;
  const svgH = topPad + rowCount * (cellH + gap) + axisH;
  const xOf = i => labelW + i * (cellW + gap);
  const yOf = j => topPad + j * (cellH + gap);

  const parts = [];
  parts.push(`<svg id="vf-surface-svg" viewBox="0 0 ${svgW} ${svgH}" width="${svgW}" height="${svgH}">`);
  parts.push('<defs>');
  // Hatch pattern for trend-skipped cells (outside the ray-bounded rectangle).
  // Rotated via patternTransform so the diagonal is visible on small cells.
  parts.push(`<pattern id="hatch-skip" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">`
    + `<rect width="8" height="8" fill="#1e2230"/>`
    + `<line x1="0" y1="0" x2="0" y2="8" stroke="#3c4158" stroke-width="3"/></pattern>`);
  // Thermal-failed pattern — red base with darker hatching. Distinct from
  // the gray skip pattern so operators can spot overheated cells at a glance.
  parts.push(`<pattern id="hatch-thermal" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">`
    + `<rect width="8" height="8" fill="#7a1a1a"/>`
    + `<line x1="0" y1="0" x2="0" y2="8" stroke="#3a0808" stroke-width="3"/></pattern>`);
  parts.push('</defs>');

  // Axes.
  voltages.forEach((v, j) => {
    parts.push(`<text x="${labelW - 8}" y="${yOf(j) + cellH / 2}" class="vf-axis-label" text-anchor="end" dominant-baseline="central">${v} ${yUnit}</text>`);
  });
  freqs.forEach((f, i) => {
    parts.push(`<text x="${xOf(i) + cellW / 2}" y="${topPad + rowCount * (cellH + gap) + 12}" class="vf-axis-label" text-anchor="middle">${f.toFixed(0)}</text>`);
  });
  parts.push(`<text x="${xOf(colCount - 1) + cellW + 4}" y="${topPad + rowCount * (cellH + gap) + 12}" class="vf-axis-label" text-anchor="start">MHz</text>`);

  // Cells.
  voltages.forEach((v, j) => {
    freqs.forEach((f, i) => {
      const key = `${v}|${f}`;
      const entry = measuredByKey.get(key);
      const isPlanned = plannedKeys.has(key);
      const isSkipped = skippedKeys.has(key);
      const isMeasuring = currentKey === key;
      const isRemeasureQueued = remeasureKeys.has(key);
      const x = xOf(i), y = yOf(j);

      let fill, glyph = '', valueText = '', ariaLabel, hoverTip, stroke = 'var(--border)', strokeDash = '';
      if(isMeasuring){
        fill = 'var(--bg3)';
        stroke = 'var(--accent)';
        hoverTip = `${v} mV · ${f.toFixed(1)} MHz — measuring now`;
        ariaLabel = `${v} millivolt, ${f.toFixed(1)} megahertz, currently measuring`;
      } else if(entry){
        const eff = effectiveEntry(entry);
        if(entry.thermal_failed){
          // Thermal emergency aborted this cell's measurement. Distinct red
          // hatched fill + HOT glyph so it's instantly readable as
          // "overheated, can't measure". Operator can re-queue via the
          // remeasure button on the cell-popup.
          fill = 'url(#hatch-thermal)';
          glyph = 'HOT';
          hoverTip = `${v} mV · ${f.toFixed(1)} MHz — thermal emergency (chips overheated)\n`
            + `Click cell → "Remeasure this cell" to retry once cooling improves`;
          ariaLabel = `${v} millivolt, ${f.toFixed(1)} megahertz, thermal emergency`;
        } else if(eff.efficiency_jth != null){
          fill = cellFill(eff);
          // Cell value text shows the active-mode metric (J/TH or $/day).
          if (canColorByProfit) {
            const p = vfProfitForEntry(eff);
            valueText = p != null ? `$${p.toFixed(2)}` : eff.efficiency_jth.toFixed(2);
          } else {
            valueText = eff.efficiency_jth.toFixed(2);
          }
          // Hover tooltip shows $/day whenever coin data exists, regardless
          // of active mode — matches the cell modal + top_tunes table dual
          // display so operators can see both metrics at a glance.
          const profit = vfProfitForEntry(eff);
          const tunedMark = eff._chipTune ? ' (chip-tuned)' : (entry.fine ? ' (fine)' : '');
          const beforeLine = eff._chipTune && eff._originalJth != null
            ? `\nBefore chip tune: ${eff._originalJth.toFixed(2)} J/TH` : '';
          hoverTip = `${v} mV · ${f.toFixed(1)} MHz${tunedMark}\n`
            + `${eff.efficiency_jth.toFixed(2)} J/TH · `
            + `${eff.hashrate_ths != null ? eff.hashrate_ths.toFixed(2) : '—'} TH/s · `
            + `${eff.power_w != null ? eff.power_w.toFixed(0) : '—'} W`
            + beforeLine
            + (profit != null ? `\n$${profit.toFixed(2)}/day` : '');
          ariaLabel = `${v} millivolt, ${f.toFixed(1)} megahertz, ${eff.efficiency_jth.toFixed(2)} J/TH${eff._chipTune ? ', chip-tuned' : ''}`;
        } else {
          fill = 'var(--bg2)';
          glyph = '—';
          hoverTip = `${v} mV · ${f.toFixed(1)} MHz — no data (API error)`;
          ariaLabel = `${v} millivolt, ${f.toFixed(1)} megahertz, no data`;
        }
      } else if(isSkipped){
        fill = 'url(#hatch-skip)';
        hoverTip = `${v} mV · ${f.toFixed(1)} MHz — skipped (trend confirmed)`;
        ariaLabel = `${v} millivolt, ${f.toFixed(1)} megahertz, skipped by trend confirmation`;
      } else if(isPlanned){
        fill = 'var(--bg)';
        strokeDash = ' stroke-dasharray="3,3"';
        hoverTip = `${v} mV · ${f.toFixed(1)} MHz — pending`;
        ariaLabel = `${v} millivolt, ${f.toFixed(1)} megahertz, pending measurement`;
      } else {
        return; // No render — outside planned grid and not measured.
      }

      if(isRemeasureQueued && !isMeasuring){
        hoverTip += '\n(queued for remeasure)';
      }

      parts.push(`<g class="vf-cell" data-key="${_vfEscape(key)}" data-v="${v}" data-f="${f}" data-tip="${_vfEscape(hoverTip)}" tabindex="-1" role="gridcell" aria-label="${_vfEscape(ariaLabel)}">`);
      parts.push(`<rect x="${x}" y="${y}" width="${cellW}" height="${cellH}" fill="${fill}" stroke="${stroke}" stroke-width="1" rx="2"${strokeDash}/>`);
      if(valueText){
        parts.push(`<text x="${x + cellW / 2}" y="${y + cellH / 2}" class="vf-value-text" text-anchor="middle" dominant-baseline="central">${valueText}</text>`);
      } else if(isMeasuring){
        const cx = x + cellW / 2, cy = y + cellH / 2;
        parts.push(`<g class="vf-measuring-dots">`
          + `<circle cx="${cx - 6}" cy="${cy}" r="2.2"/>`
          + `<circle cx="${cx}" cy="${cy}" r="2.2"/>`
          + `<circle cx="${cx + 6}" cy="${cy}" r="2.2"/>`
          + `</g>`);
      } else if(glyph){
        parts.push(`<text x="${x + cellW / 2}" y="${y + cellH / 2}" class="vf-glyph" text-anchor="middle" dominant-baseline="central" fill="var(--text2)">${glyph}</text>`);
      }
      if(isRemeasureQueued && !isMeasuring){
        // Small accent-colored circle in the bottom-left corner marks cells
        // sitting in the remeasure queue. Skip for the in-flight cell — the
        // pulsing ring already signals that something is happening there.
        parts.push(`<circle cx="${x + 6}" cy="${y + cellH - 6}" r="3.2" class="vf-remeasure-marker"/>`);
      }
      parts.push('</g>');

      if(isMeasuring){
        parts.push(`<rect class="vf-measuring-ring" x="${x}" y="${y}" width="${cellW}" height="${cellH}" rx="2"/>`);
      }

      // Fine sub-cells — render on top of the coarse cell at its pixel rect.
      // Each sub-cell is its own vf-cell with its exact (V, F) as data-key,
      // so existing hover/click/modal code lights up the fine entry without
      // any special-casing.
      const fineBucket = fineLayoutByCoarse.get(key);
      if(fineBucket){
        const { items, dvSorted, dfSorted } = fineBucket;
        const subCols = dfSorted.length;
        const subRows = dvSorted.length;
        const subW = cellW / subCols;
        const subH = cellH / subRows;
        // R2: staged label decimals and lowered threshold. Fine subcells can
        // get small at N=4,5 (subW ~14-17 px) — shrink decimals so the number
        // still fits instead of dropping the label entirely.
        const showSubText = subW >= 14 && subH >= 10;
        const subDecimals = subW >= 30 ? 2 : (subW >= 20 ? 1 : 0);
        const subTextClass = subW >= 20 ? 'vf-subvalue' : 'vf-subvalue-tiny';
        items.forEach(it => {
          const row = dvSorted.indexOf(it.dv);
          const col = dfSorted.indexOf(it.df);
          const sx = x + col * subW;
          const sy = y + row * subH;
          const subKey = `${it.fineV}|${it.fineF}`;
          const subFQueued = remeasureKeys.has(subKey);
          let sFill, sGlyph = '', sValue = '', sTip, sAria, sStroke = 'rgba(255,255,255,0.55)', sDash = '';
          if(it.kind === 'measuring'){
            sFill = 'var(--bg3)';
            sStroke = 'var(--accent)';
            sTip = `${it.fineV} mV · ${it.fineF.toFixed(1)} MHz (fine) — measuring now`;
            sAria = `${it.fineV} millivolt, ${it.fineF.toFixed(1)} megahertz, fine grid, currently measuring`;
          } else if(it.kind === 'measured'){
            const e2 = effectiveEntry(it.entry);
            if(it.entry && it.entry.thermal_failed){
              sFill = 'url(#hatch-thermal)';
              sGlyph = '!';
              sTip = `${it.fineV} mV · ${it.fineF.toFixed(1)} MHz (fine) — thermal emergency`;
              sAria = `${it.fineV} millivolt, ${it.fineF.toFixed(1)} megahertz, fine grid, thermal emergency`;
            } else if(e2.efficiency_jth != null){
              sFill = cellFill(e2);
              if (canColorByProfit) {
                const p2 = vfProfitForEntry(e2);
                sValue = p2 != null ? `$${p2.toFixed(subDecimals)}` : e2.efficiency_jth.toFixed(subDecimals);
              } else {
                sValue = e2.efficiency_jth.toFixed(subDecimals);
              }
              const fineProfit = canColorByProfit ? vfProfitForEntry(e2) : null;
              const fineTunedMark = e2._chipTune ? ' (fine, chip-tuned)' : ' (fine)';
              const fineBeforeLine = e2._chipTune && e2._originalJth != null
                ? `\nBefore chip tune: ${e2._originalJth.toFixed(2)} J/TH` : '';
              sTip = `${it.fineV} mV · ${it.fineF.toFixed(1)} MHz${fineTunedMark}\n`
                + `${e2.efficiency_jth.toFixed(2)} J/TH · `
                + `${e2.hashrate_ths != null ? e2.hashrate_ths.toFixed(2) : '—'} TH/s · `
                + `${e2.power_w != null ? e2.power_w.toFixed(0) : '—'} W`
                + fineBeforeLine
                + (fineProfit != null ? `\n$${fineProfit.toFixed(2)}/day` : '');
              sAria = `${it.fineV} millivolt, ${it.fineF.toFixed(1)} megahertz, fine grid, ${e2.efficiency_jth.toFixed(2)} J/TH${e2._chipTune ? ', chip-tuned' : ''}`;
            } else {
              sFill = 'var(--bg2)';
              sGlyph = '—';
              sTip = `${it.fineV} mV · ${it.fineF.toFixed(1)} MHz (fine) — no data (API error)`;
              sAria = `${it.fineV} millivolt, ${it.fineF.toFixed(1)} megahertz, fine grid, no data`;
            }
          } else { // pending
            sFill = 'var(--bg)';
            sDash = ' stroke-dasharray="3,3"';
            sTip = `${it.fineV} mV · ${it.fineF.toFixed(1)} MHz (fine) — pending`;
            sAria = `${it.fineV} millivolt, ${it.fineF.toFixed(1)} megahertz, fine grid, pending measurement`;
          }
          if(subFQueued && it.kind !== 'measuring') sTip += '\n(queued for remeasure)';
          parts.push(`<g class="vf-cell vf-subcell" data-key="${_vfEscape(subKey)}" data-v="${it.fineV}" data-f="${it.fineF}" data-tip="${_vfEscape(sTip)}" tabindex="-1" role="gridcell" aria-label="${_vfEscape(sAria)}">`);
          parts.push(`<rect x="${sx}" y="${sy}" width="${subW}" height="${subH}" fill="${sFill}" stroke="${sStroke}" stroke-width="0.75"${sDash}/>`);
          if(sValue && showSubText){
            parts.push(`<text x="${sx + subW / 2}" y="${sy + subH / 2}" class="vf-value-text ${subTextClass}" text-anchor="middle" dominant-baseline="central">${sValue}</text>`);
          } else if(it.kind === 'measuring'){
            const cx = sx + subW / 2, cy = sy + subH / 2;
            parts.push(`<g class="vf-measuring-dots">`
              + `<circle cx="${cx - 4}" cy="${cy}" r="1.6"/>`
              + `<circle cx="${cx}" cy="${cy}" r="1.6"/>`
              + `<circle cx="${cx + 4}" cy="${cy}" r="1.6"/>`
              + `</g>`);
          } else if(sGlyph){
            // No-data glyph ('—') is one char — always fits even at 14px.
            parts.push(`<text x="${sx + subW / 2}" y="${sy + subH / 2}" class="vf-glyph ${subTextClass}" text-anchor="middle" dominant-baseline="central" fill="var(--text2)">${sGlyph}</text>`);
          }
          if(subFQueued && it.kind !== 'measuring'){
            parts.push(`<circle cx="${sx + 4}" cy="${sy + subH - 4}" r="2.2" class="vf-remeasure-marker"/>`);
          }
          parts.push('</g>');
          if(it.kind === 'measuring'){
            parts.push(`<rect class="vf-measuring-ring" x="${sx}" y="${sy}" width="${subW}" height="${subH}"/>`);
          }
        });
      }
    });
  });

  // Top-K seed borders (rendered on top of cells). When the seed is a fine
  // cell (vf_source.kind === 'fine'), wrap just its sub-rectangle inside the
  // owning coarse cell — mirrors the winner-border fine-cell logic below so
  // the highlight is visible at the fine scale regardless of sub-grid size.
  topK.forEach(t => {
    const seedV = t.voltage_mv;
    const seedF = Number(t.seed_f_mhz);
    const isFineSeed = t.vf_source && t.vf_source.kind === 'fine';
    if(isFineSeed){
      const cv = findNearest(voltages, seedV);
      const cf = findNearest(freqs, seedF);
      const j = voltages.findIndex(v => v === cv);
      const i = freqs.findIndex(f => Math.abs(f - cf) < 0.01);
      const bucket = cv != null && cf != null ? fineLayoutByCoarse.get(`${cv}|${cf}`) : null;
      if(i >= 0 && j >= 0 && bucket){
        const dv = seedV - cv;
        const df = seedF - cf;
        const row = bucket.dvSorted.indexOf(dv);
        const col = bucket.dfSorted.indexOf(df);
        if(row >= 0 && col >= 0){
          const subW = cellW / bucket.dfSorted.length;
          const subH = cellH / bucket.dvSorted.length;
          const sx = xOf(i) + col * subW;
          const sy = yOf(j) + row * subH;
          parts.push(`<rect class="vf-topk-border" x="${sx - 0.5}" y="${sy - 0.5}" width="${subW + 1}" height="${subH + 1}"/>`);
          return;
        }
      }
    }
    const i = freqs.findIndex(f => Math.abs(f - seedF) < 0.01);
    const j = voltages.findIndex(v => v === seedV);
    if(i >= 0 && j >= 0){
      parts.push(`<rect class="vf-topk-border" x="${xOf(i) - 0.5}" y="${yOf(j) - 0.5}" width="${cellW + 1}" height="${cellH + 1}" rx="2"/>`);
    }
  });
  // Winner — overwrites top-K border if the winner is also top-K. If the
  // winner is a fine cell, the border wraps just its sub-rectangle inside
  // the owning coarse cell (not the whole coarse cell).
  if(bestEntry){
    if(bestEntry.fine){
      const cv = findNearest(voltages, bestEntry[yField]);
      const cf = findNearest(freqs, Number(bestEntry.freq_mhz));
      const j = voltages.findIndex(v => v === cv);
      const i = freqs.findIndex(f => Math.abs(f - cf) < 0.01);
      const bucket = cv != null && cf != null ? fineLayoutByCoarse.get(`${cv}|${cf}`) : null;
      if(i >= 0 && j >= 0 && bucket){
        const dv = bestEntry[yField] - cv;
        const df = Number(bestEntry.freq_mhz) - cf;
        const row = bucket.dvSorted.indexOf(dv);
        const col = bucket.dfSorted.indexOf(df);
        if(row >= 0 && col >= 0){
          const subW = cellW / bucket.dfSorted.length;
          const subH = cellH / bucket.dvSorted.length;
          const sx = xOf(i) + col * subW;
          const sy = yOf(j) + row * subH;
          parts.push(`<rect class="vf-winner-border" x="${sx - 1}" y="${sy - 1}" width="${subW + 2}" height="${subH + 2}"/>`);
        }
      }
    } else {
      const i = freqs.findIndex(f => Math.abs(f - Number(bestEntry.freq_mhz)) < 0.01);
      const j = voltages.findIndex(v => v === bestEntry[yField]);
      if(i >= 0 && j >= 0){
        parts.push(`<rect class="vf-winner-border" x="${xOf(i) - 1}" y="${yOf(j) - 1}" width="${cellW + 2}" height="${cellH + 2}" rx="2"/>`);
      }
    }
  }

  parts.push('</svg>');
  gridEl.innerHTML = parts.join('');

  // Summary + legend.
  const measuredCount = surface.length;
  const withDataCount = withData.length;
  const noDataCount = measuredCount - withDataCount;
  const fineCount = surface.filter(e => e.fine).length;
  const plannedCount = planned.length;
  const skippedCount = skipped.length;
  summaryEl.textContent = plannedCount
    ? `${measuredCount}/${plannedCount} planned · ${withDataCount} measured${noDataCount ? ' · ' + noDataCount + ' no data' : ''} · ${skippedCount} skipped${fineCount ? ' · ' + fineCount + ' fine' : ''}`
    : `${measuredCount} point${measuredCount === 1 ? '' : 's'}${noDataCount ? ' · ' + noDataCount + ' no data' : ''}${fineCount ? ' · ' + fineCount + ' fine' : ''}`;

  const legendBits = [];
  if(bestJth != null && worstJth != null && bestJth !== worstJth){
    // Gradient runs worst (left, purple) → best (right, yellow) — matches cell colors.
    legendBits.push(
      `<div style="display:flex;align-items:center;gap:6px">`
      + `<span style="font-variant-numeric:tabular-nums">${worstJth.toFixed(2)}</span>`
      + `<div class="vf-scale-bar" style="background:linear-gradient(to right, ${VIRIDIS.join(',')})" title="J/TH scale (lower = more efficient)"></div>`
      + `<span style="font-variant-numeric:tabular-nums;color:var(--text)"><b>${bestJth.toFixed(2)}</b> J/TH</span>`
      + `</div>`);
  } else if(bestJth != null){
    legendBits.push(`<span>Best J/TH: <b>${bestJth.toFixed(2)}</b></span>`);
  }
  legendBits.push(`<span><span class="legend-swatch" style="background:#1e2230;background-image:repeating-linear-gradient(45deg,#3c4158 0 2px,transparent 2px 5px)"></span>skipped (trend)</span>`);
  legendBits.push(`<span><span class="legend-swatch" style="background:var(--bg);border:1px dashed var(--border)"></span>pending</span>`);
  legendBits.push(`<span><span class="legend-swatch" style="background:transparent;border:2px solid gold"></span>winner</span>`);
  if(topK.length) legendBits.push(`<span><span class="legend-swatch" style="background:transparent;border:2px solid var(--accent)"></span>top-${topK.length} ${topKIsPreview ? 'preview' : 'seed'}</span>`);
  if(fineCount) legendBits.push(`<span><span class="legend-swatch" style="position:relative;background:transparent;border:1px solid var(--border);overflow:hidden"><span style="position:absolute;inset:0;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:1px;background:var(--border)"><span style="background:var(--bg3)"></span><span style="background:var(--bg2)"></span><span style="background:var(--bg2)"></span><span style="background:var(--bg3)"></span></span></span>fine sub-grid</span>`);
  if(remeasureQueue.length) legendBits.push(`<span><span class="legend-swatch" style="background:var(--bg3);position:relative"><span style="position:absolute;bottom:1px;left:1px;width:4px;height:4px;background:var(--accent);border-radius:50%"></span></span>queued remeasure</span>`);
  legendEl.innerHTML = legendBits.join('');

  // Phase V progress bar — only shown while the scan is running.
  if(inPhaseV && plannedCount > 0){
    progressEl.style.display = '';
    const cfg = (status && status.config) || {};
    const perPoint = (cfg.vf_explore_wait || 90)
      + (cfg.vf_explore_samples || 3) * (cfg.vf_explore_sample_interval || 5)
      + 10;
    const remaining = Math.max(0, plannedCount - measuredCount - skippedCount);
    const etaMin = Math.round((remaining * perPoint) / 60);
    const donePct = Math.min(100, (measuredCount + skippedCount) / plannedCount * 100);
    const best = bestEntry
      ? ` · running best <b>${bestJth.toFixed(2)}</b> J/TH @ ${bestEntry[yField]} ${yUnit} / ${Number(bestEntry.freq_mhz).toFixed(1)} MHz`
      : '';
    progressEl.innerHTML =
      `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">`
      + `<div class="progress-bar" style="flex:1;min-width:160px;height:14px">`
      + `<div class="progress-bar-fill striped" style="width:${donePct.toFixed(1)}%"></div>`
      + `</div>`
      + `<div style="white-space:nowrap;font-variant-numeric:tabular-nums">`
      + `<b>${measuredCount}</b>/${plannedCount} measured · ${skippedCount} skipped · <b>${donePct.toFixed(0)}%</b>`
      + (remaining > 0 ? ` · ETA ~${etaMin} min` : '')
      + `</div></div>`
      + (best ? `<div style="margin-top:4px;color:var(--text2)">${best}</div>` : '');
  } else {
    progressEl.style.display = 'none';
  }

  // Remeasure queue bar — visible whenever the queue is non-empty.
  const remeasureBar = document.getElementById('vf-remeasure-bar');
  const remeasureCount = document.getElementById('vf-remeasure-count');
  const remeasureItems = document.getElementById('vf-remeasure-items');
  const processBtn = document.getElementById('vf-remeasure-process-btn');
  if(remeasureQueue.length){
    remeasureBar.style.display = 'flex';
    const n = remeasureQueue.length;
    remeasureCount.textContent = `Remeasure queue: ${n} cell${n !== 1 ? 's' : ''}`;
    const preview = remeasureQueue.slice(0, 4)
      .map(q => `${q[yField]} ${yUnit} / ${Number(q.freq_mhz).toFixed(1)} MHz`)
      .join(' · ');
    remeasureItems.textContent = n > 4 ? `${preview} · +${n - 4} more` : preview;
    // Auto-draining while Phase V is active — no manual Process needed.
    const busy = status && status.engine_busy;
    const autoDraining = inPhaseV;
    processBtn.disabled = !!busy;
    processBtn.title = busy
      ? (autoDraining
          ? 'Queue drains automatically during Phase V (between rays / fill rows)'
          : 'Engine is busy — stop the current tune first')
      : 'Drain the queue now (engine is stopped)';
  } else {
    remeasureBar.style.display = 'none';
  }

  // Stash data for click / keyboard handlers. R6: include voltage_results +
  // freq-search tolerance so the cell modal can cross-link a coarse/fine cell
  // to its chip-tuned voltage_results entry (before/after J/TH display).
  // Profit-display fields are stashed so the modal can render $/day for any
  // cell whenever a minerstat snapshot exists — irrespective of target_mode —
  // matching the top_tunes table's dual-metric behavior.
  _vfGridState = {
    voltages, freqs, measuredByKey, plannedKeys, skippedKeys, topKSet,
    topKIsPreview,
    bestEntry, currentKey, cellW, cellH,
    remeasureQueue,
    voltageResults: (status && status.voltage_results) || [],
    freqTol: (status && status.config && status.config.freq_search_tolerance_mhz) || 7,
    targetMode: vfTargetMode,
    coinData: vfCoinData,
    electricRate: vfRate,
    profitForEntry: vfProfitForEntry,
    canColorByProfit,
    isWattage, yField, yUnit, yLabel, yAxisDesc,
  };
  wireVFInteractivity();
}

// Event wiring for the V/F surface — hover tooltip, click modal, keyboard nav.
// Idempotent: called on every render, uses delegation so listeners don't
// accumulate.
function wireVFInteractivity(){
  const gridEl = document.getElementById('vf-surface-grid');
  const svg = gridEl.querySelector('#vf-surface-svg');
  if(!svg) return;

  // Reuse the shared tooltip div (created once).
  let tip = document.getElementById('vf-tooltip');
  if(!tip){
    tip = document.createElement('div');
    tip.id = 'vf-tooltip';
    tip.className = 'tooltip';
    tip.style.cssText = 'position:fixed;display:none;white-space:pre-line;z-index:2000;max-width:280px';
    document.body.appendChild(tip);
  }

  // Delegation handlers attached once on the grid container.
  if(!gridEl._vfWired){
    gridEl._vfWired = true;
    gridEl.addEventListener('mousemove', (ev) => {
      const cell = ev.target.closest('.vf-cell');
      if(!cell){ tip.style.display = 'none'; return; }
      const text = cell.getAttribute('data-tip');
      if(!text){ tip.style.display = 'none'; return; }
      tip.textContent = text;
      tip.style.display = '';
      const pad = 14;
      tip.style.left = Math.min(window.innerWidth - 300, ev.clientX + pad) + 'px';
      tip.style.top = Math.min(window.innerHeight - 100, ev.clientY + pad) + 'px';
    });
    gridEl.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
    gridEl.addEventListener('click', (ev) => {
      const cell = ev.target.closest('.vf-cell');
      if(!cell) return;
      openVFCellModal(cell.getAttribute('data-key'));
    });
    gridEl.addEventListener('keydown', (ev) => {
      if(!_vfGridState) return;
      const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest('.vf-cell');
      const key = ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Enter',' '].includes(ev.key);
      if(!key) return;
      ev.preventDefault();
      // Keyboard nav stays on the coarse grid — sub-cells are clickable but
      // not reachable via arrows (their count would break the `cols`-based
      // row/column math below).
      const cells = Array.from(gridEl.querySelectorAll('.vf-cell:not(.vf-subcell)'));
      if(!cells.length) return;
      let idx = focused ? cells.indexOf(focused) : 0;
      if(idx < 0) idx = 0;
      const cols = _vfGridState.freqs.length;
      if(ev.key === 'ArrowLeft') idx = Math.max(0, idx - 1);
      else if(ev.key === 'ArrowRight') idx = Math.min(cells.length - 1, idx + 1);
      else if(ev.key === 'ArrowUp') idx = Math.max(0, idx - cols);
      else if(ev.key === 'ArrowDown') idx = Math.min(cells.length - 1, idx + cols);
      else if(ev.key === 'Enter' || ev.key === ' '){
        openVFCellModal(cells[idx].getAttribute('data-key'));
        return;
      }
      cells.forEach(c => c.classList.remove('vf-focus'));
      cells[idx].classList.add('vf-focus');
      cells[idx].focus();
    });
  }
}

// Click-cell details modal — shows full measurement record + Retune button
// for top-K cells.
function openVFCellModal(key){
  if(!_vfGridState) return;
  const [vStr, fStr] = key.split('|');
  const v = Number(vStr), f = Number(fStr);
  const entry = _vfGridState.measuredByKey.get(key);
  const isSkipped = _vfGridState.skippedKeys.has(key);
  const isPending = _vfGridState.plannedKeys.has(key) && !entry && !isSkipped;
  const isTopK = _vfGridState.topKSet.has(key);
  const yLabel = _vfGridState.yLabel || 'Voltage';
  const yUnit = _vfGridState.yUnit || 'mV';
  const isWattage = !!_vfGridState.isWattage;

  let body = `<div class="stat-row"><span class="stat-label">${yLabel}</span><span class="stat-value">${v} ${yUnit}</span></div>`;
  body += `<div class="stat-row"><span class="stat-label">Frequency (uniform)</span><span class="stat-value">${f.toFixed(1)} MHz</span></div>`;

  if(entry){
    const isThermalFailed = !!entry.thermal_failed;
    const hasData = !isThermalFailed && entry.efficiency_jth != null;
    let stateLabel, stateClass;
    if(isThermalFailed){
      stateLabel = 'Thermal emergency (chips overheated)';
      stateClass = 'bad';
    } else if(hasData){
      stateLabel = 'Measured';
      stateClass = 'good';
    } else {
      stateLabel = 'No data (API error)';
      stateClass = '';
    }
    body += `<div class="stat-row"><span class="stat-label">State</span><span class="stat-value ${stateClass}">${stateLabel}${entry.fine ? ' (fine grid)' : ' (coarse grid)'}</span></div>`;
    if(isThermalFailed){
      body += `<div style="color:var(--text2);font-size:0.85em;margin-top:8px">Phase V/4 detected a chip or board over its critical-temp threshold while measuring this cell. The tuner stopped mining, marked the cell as thermal-failed, and continued with the next cell. This cell is excluded from ranking, fine-grid selection, and chip-tune candidate selection. Click "Remeasure this cell" to retry once cooling improves.</div>`;
    }
    if(hasData){
      body += `<div class="stat-row"><span class="stat-label">Efficiency</span><span class="stat-value">${entry.efficiency_jth.toFixed(2)} J/TH</span></div>`;
      body += `<div class="stat-row"><span class="stat-label">Hashrate</span><span class="stat-value">${entry.hashrate_ths != null ? entry.hashrate_ths.toFixed(2) + ' TH/s' : '—'}</span></div>`;
      body += `<div class="stat-row"><span class="stat-label">Power</span><span class="stat-value">${entry.power_w != null ? entry.power_w.toFixed(0) + ' W' : '—'}</span></div>`;
      // Profit row surfaces $/day whenever minerstat has coin data — both
      // efficiency and profit modes show it so operators see both metrics.
      if(_vfGridState.profitForEntry){
        const profit = _vfGridState.profitForEntry(entry);
        if(profit != null){
          body += `<div class="stat-row"><span class="stat-label">Profit</span><span class="stat-value">$${profit.toFixed(2)}/day</span></div>`;
        }
      }
    }
    if(entry.measured_at){
      body += `<div class="stat-row"><span class="stat-label">${isThermalFailed ? 'Failed at' : 'Measured at'}</span><span class="stat-value" style="font-weight:400;color:var(--text2);font-size:0.85em">${_vfEscape(entry.measured_at)}</span></div>`;
    }
  } else if(isSkipped){
    body += `<div class="stat-row"><span class="stat-label">State</span><span class="stat-value" style="color:var(--text2)">Skipped (trend confirmed)</span></div>`;
    body += `<div style="color:var(--text2);font-size:0.85em;margin-top:8px">Phase V's center-out walk confirmed this direction was trending worse in efficiency, so this cell is outside the ray-bounded rectangle and was never measured.</div>`;
  } else if(isPending){
    body += `<div class="stat-row"><span class="stat-label">State</span><span class="stat-value" style="color:var(--text2)">Pending</span></div>`;
    body += `<div style="color:var(--text2);font-size:0.85em;margin-top:8px">Phase V hasn't reached this cell yet.</div>`;
  }

  if(isTopK){
    const topKLabel = _vfGridState.topKIsPreview
      ? 'Top-K candidate (preview — walk still running)'
      : 'Selected for per-chip refinement';
    body += `<div class="stat-row"><span class="stat-label">Top-K seed</span><span class="stat-value" style="color:var(--accent)">${topKLabel}</span></div>`;
  }
  if(_vfGridState.bestEntry
      && _vfGridState.bestEntry[_vfGridState.yField || 'voltage_mv'] === v
      && Math.abs(Number(_vfGridState.bestEntry.freq_mhz) - f) < 0.01){
    const winnerMetric = _vfGridState.canColorByProfit ? 'Best $/day on the grid' : 'Best J/TH on the grid';
    body += `<div class="stat-row"><span class="stat-label">Winner</span><span class="stat-value" style="color:gold">${winnerMetric}</span></div>`;
  }

  // R6: if a chip-tune was seeded from this cell, show before/after J/TH.
  // Lookup priority: exact vf_source match → seed_f_mhz fallback → voltage-only
  // for pre-Phase V entries. Tolerance is the backend's FREQ_SEARCH_TOLERANCE_MHZ.
  const vrList = _vfGridState.voltageResults || [];
  const tol = _vfGridState.freqTol || 7;
  let chipTune = null;
  for(const r of vrList){
    if(Number(r.voltage_mv) !== v) continue;
    if(r.vf_source && r.vf_source.freq_mhz != null){
      if(Math.abs(Number(r.vf_source.freq_mhz) - f) <= tol){ chipTune = r; break; }
    } else if(r.seed_f_mhz != null){
      if(Math.abs(Number(r.seed_f_mhz) - f) <= tol){ chipTune = r; break; }
    }
  }
  // Fallback: same voltage, no freq info on either side.
  if(!chipTune){
    chipTune = vrList.find(r => Number(r.voltage_mv) === v
      && r.vf_source == null && r.seed_f_mhz == null) || null;
  }
  if(chipTune){
    body += `<hr style="border:none;border-top:1px solid var(--border);margin:10px 0">`;
    body += `<div class="stat-row"><span class="stat-label">Chip tune at this voltage</span><span class="stat-value" style="color:var(--accent)">Applied</span></div>`;
    const before = entry && entry.efficiency_jth != null ? entry.efficiency_jth : null;
    const after = chipTune.efficiency_jth;
    if(before != null && after != null){
      const pct = ((before - after) / before) * 100;
      const sign = pct >= 0 ? '+' : '';
      const improvedCls = pct >= 0 ? 'good' : '';
      body += `<div class="stat-row"><span class="stat-label">Before chip tune</span><span class="stat-value">${before.toFixed(2)} J/TH</span></div>`;
      body += `<div class="stat-row"><span class="stat-label">After chip tune</span><span class="stat-value ${improvedCls}">${after.toFixed(2)} J/TH</span></div>`;
      body += `<div class="stat-row"><span class="stat-label">Improvement</span><span class="stat-value ${improvedCls}">${sign}${pct.toFixed(1)}%</span></div>`;
    } else if(after != null){
      body += `<div class="stat-row"><span class="stat-label">After chip tune</span><span class="stat-value">${after.toFixed(2)} J/TH</span></div>`;
    }
    if(chipTune.avg_freq_mhz != null){
      body += `<div class="stat-row"><span class="stat-label">Avg freq after tune</span><span class="stat-value">${Number(chipTune.avg_freq_mhz).toFixed(0)} MHz</span></div>`;
    }
    if(chipTune.hashrate_ths != null){
      body += `<div class="stat-row"><span class="stat-label">Hashrate after tune</span><span class="stat-value">${Number(chipTune.hashrate_ths).toFixed(2)} TH/s</span></div>`;
    }
    // Profit before/after mirrors the J/TH triplet above. Computed from the
    // same coin data the heatmap uses; shown whenever the minerstat snapshot
    // has data. Higher profit is better, so the improvement sign is reversed
    // from J/TH (which we want to go down).
    if(_vfGridState.profitForEntry){
      const profitBefore = entry ? _vfGridState.profitForEntry(entry) : null;
      const profitAfter = _vfGridState.profitForEntry(chipTune);
      if(profitBefore != null && profitAfter != null){
        const pct = profitBefore !== 0 ? ((profitAfter - profitBefore) / Math.abs(profitBefore)) * 100 : null;
        const sign = pct != null && pct >= 0 ? '+' : '';
        const improvedCls = pct != null && pct >= 0 ? 'good' : '';
        body += `<div class="stat-row"><span class="stat-label">Profit before</span><span class="stat-value">$${profitBefore.toFixed(2)}/day</span></div>`;
        body += `<div class="stat-row"><span class="stat-label">Profit after</span><span class="stat-value ${improvedCls}">$${profitAfter.toFixed(2)}/day</span></div>`;
        if(pct != null){
          body += `<div class="stat-row"><span class="stat-label">Profit change</span><span class="stat-value ${improvedCls}">${sign}${pct.toFixed(1)}%</span></div>`;
        }
      } else if(profitAfter != null){
        body += `<div class="stat-row"><span class="stat-label">Profit after</span><span class="stat-value">$${profitAfter.toFixed(2)}/day</span></div>`;
      }
    }
  }

  // Surface the queue state so the operator knows whether this cell is
  // already on deck before they click Remeasure.
  const queuedCell = (_vfGridState.remeasureQueue || []).find(
    q => Number(q.voltage_mv) === v && Math.abs(Number(q.freq_mhz) - f) < 0.01);
  if(queuedCell){
    body += `<div class="stat-row"><span class="stat-label">Remeasure queue</span><span class="stat-value" style="color:var(--accent)">Queued</span></div>`;
  }

  const actions = [{label:'Close', action: closeModal}];
  // Remeasure is always offered — works for no-data cells (retry the API
  // failure), skipped cells (operator override), pending cells (jump the
  // queue), and measured cells (re-sample if the reading looks wrong).
  if(!queuedCell){
    actions.unshift({
      label:'Remeasure this cell',
      action: () => { closeModal(); enqueueRemeasure(v, f); },
    });
  }
  // Retune offered for (a) top-K cells with an existing chip-tune and (b)
  // any cell with data at a voltage that has vf_surface measurements (R7
  // extended retune). Backend start_retune accepts either path. Thermal-
  // failed cells are excluded — there's no measurement data to retune
  // against; operator must remeasure first.
  // Whatsminer (wattage axis) has no per-voltage retune concept — the
  // `power_limit_freq_search` strategy doesn't run chip-tune at any cell.
  // Skip the retune action entirely for wattage-axis miners.
  const isThermalFailed = entry && entry.thermal_failed;
  const retuneAllowed = !isWattage && !isThermalFailed && (
    isTopK || chipTune || (entry && entry.efficiency_jth != null));
  if(retuneAllowed){
    actions.unshift({
      label: chipTune ? `Retune at ${v} mV` : `Retune this voltage (${v} mV)`,
      action: () => { closeModal(); retuneVoltage(v); },
    });
  }
  openModal(`V/F cell: ${v} ${yUnit} · ${f.toFixed(1)} MHz`, body, actions);
}

function downloadBlob(data, filename, mime){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([data], {type:mime}));
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function csvEscape(v){
  if(v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s;
}

function voltageSweepToCSV(bundle){
  const rows = [['voltage_mv','avg_freq_mhz','hashrate_ths','power_w','efficiency_jth','duration_sec','measured_at',
                 'board_index','board_hashrate_ths','board_avg_clock_mhz','board_tuned_avg_freq_mhz',
                 'board_temp_c','inlet_temp_c','outlet_temp_c',
                 'chip_temp_min_c','chip_temp_avg_c','chip_temp_max_c','health_pct','input_voltage_v']];
  (bundle.voltage_results||[]).forEach(r => {
    const base = [r.voltage_mv, r.avg_freq_mhz, r.hashrate_ths, r.power_w, r.efficiency_jth, r.duration_sec, r.measured_at||''];
    const pb = Array.isArray(r.per_board) ? r.per_board : [];
    if(!pb.length){ rows.push([...base, '','','','','','','','','','','','','']); return; }
    pb.forEach(b => {
      rows.push([...base, b.index, b.hashrate_ths, b.avg_clock_mhz, b.tuned_avg_freq_mhz,
                 b.board_temp_c, b.inlet_temp_c, b.outlet_temp_c,
                 b.chip_temp_min_c, b.chip_temp_avg_c, b.chip_temp_max_c, b.health_pct, b.input_voltage_v]);
    });
  });
  return rows.map(r => r.map(csvEscape).join(',')).join('\r\n');
}

async function exportResults(format){
  if(!currentMac()){ alert('No miner selected'); return; }
  const bundle = await fetchJSON(`/tuner/export/${currentMacDashes()}`);
  if(!bundle){ alert('Export failed — check /tuner/export endpoint'); return; }
  const ts = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
  const tag = (currentIp() || currentMac()).replace(/\./g,'-').replace(/:/g, '-');
  if(format === 'csv'){
    downloadBlob(voltageSweepToCSV(bundle), `${tag}-voltage-sweep-${ts}.csv`, 'text/csv');
  } else {
    downloadBlob(JSON.stringify(bundle, null, 2), `${tag}-tune-export-${ts}.json`, 'application/json');
  }
}

// Poll — dispatches based on which view is active.
async function poll(){
  if (!authReady) return;  // suppress traffic while the login overlay is up
  const parsed = parseHash();
  if (parsed.view === 'detail' && currentMac()) {
    await pollDetail();
  } else {
    await pollOverview();
  }
}

async function pollDetail(){
  // Capture the miner we're polling for; if the user navigates away mid-poll,
  // drop the stale response instead of rendering miner A's data on miner B's page.
  const pollMac = currentMac();
  // Seed the minerstat snapshot on first detail-view entry. Overview polls
  // refresh it, but navigating straight to a detail URL would otherwise leave
  // minerstatSnapshot=null — which makes the V/F heatmap's profit-mode
  // coloring silently fall back to J/TH because vfCoinData reads null.
  if (!minerstatSnapshot) {
    await pollMinerstatCard();
    if (pollMac !== currentMac()) return;
  }
  const status = await fetchJSON('/tuner/status');
  if (pollMac !== currentMac()) return;
  if (status) {
    // Resolve the IP for this MAC from the status payload (which is keyed by
    // IP). The /tuner/overview poll on the same cycle keeps currentMiner.ip
    // fresh; this updates it from /tuner/status as a defense-in-depth refresh.
    if (!currentIp()) {
      // Try to find the IP via reverse-lookup over status keys; a v3-style
      // dashboard fallback that we may retire once /tuner/status is keyed by MAC.
      const ipKey = Object.keys(status).find(k => {
        const row = status[k];
        return row && (row.mac === pollMac || row.firmware_type !== undefined);
      });
      if (ipKey && status[ipKey] && status[ipKey].mac === pollMac) {
        currentMiner.ip = ipKey;
        const badge = document.getElementById('detail-ip-badge');
        if (badge) {
          badge.textContent = ipKey;
          badge.href = 'http://' + ipKey + '/';
        }
      }
    }
    updateStatus(status);
    const s = status[currentIp()];
    const bs = s ? (s.baseline_scores || []) : [];
    if (bs.some(b => b && b.length > 0)) heatmapData.baseline = bs;
    // New per-chip Phase 2 baseline arrays (added alongside baseline_scores).
    // Same "only assign when there's at least one populated board" pattern so
    // a fresh-start engine doesn't blow away in-memory state with empty arrays.
    if (s) {
      const bft = s.baseline_freq_arrays || [];
      if (bft.some(b => b && b.length > 0)) heatmapData.p2_freq = bft;
      const bct = s.baseline_chip_temps || [];
      if (bct.some(b => b && b.length > 0)) heatmapData.p2_temp = bct;
      const bch = s.baseline_chip_hashrates || [];
      if (bch.some(b => b && b.length > 0)) heatmapData.p2_hashrate = bch;
      // Stock baseline pointer — full dict from get_status. Per-chip arrays
      // inside may be missing on legacy stock.json; the right-pane render
      // falls back to gray cells for chips lacking data.
      if (s.stock_baseline) heatmapData.stock = s.stock_baseline;
    }
  }
  const live = await fetchJSON(`/tuner/live/${currentMacDashes()}`);
  if (pollMac !== currentMac()) return;
  if (live) {
    heatmapData.clocks = live.clocks; heatmapData.hashrate = live.hashrate; heatmapData.chip_temps = live.chip_temps;
    // Live push only in '1h' mode — see updateStatus block for rationale.
    if (live.temps && currentMetricsRange === '1h') {
      pushChart(charts.temp, new Date().toLocaleTimeString(),
                Math.max(...live.temps.map(b => Math.max(...(b.Data || [0])))));
    }
    // Both panes redraw on every poll — left pulls live data, right pulls
    // baseline data from status (which was already extracted above).
    drawHeatmap('left');
    drawHeatmap('right');
  }
  updateLog();
}

// ─── Overview: state + rendering ─────────────────────────────────────────────
let overviewData = { miners: [], state_counts: {}, mining_counts: {} };
let selectedMacs = new Set();
// `currentFilter.models` is null when no model filtering is active (show
// all). When the operator interacts with the dropdown for the first time,
// it becomes a Set of model names that should remain visible. New models
// discovered later (e.g. a fresh miner appears in the fleet) default to
// hidden when the set is non-null — the operator chose an explicit subset
// and should see new additions only after re-opening the dropdown.
let currentFilter = { tuner: 'all', mining: 'all', models: null };
const MODEL_FILTER_STORAGE_KEY = 'tunerFleetModelFilter';
const MODEL_FILTER_UNKNOWN = '(unknown)';
let tunerStateChart = null;
let miningStateChart = null;

function _makeDoughnut(canvasId, labels, colors){
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: labels.map(() => 0),
        backgroundColor: colors,
        borderColor: '#1a1d27', borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { position: 'right', labels: { color: '#e0e0e0', font: { size: 10 }, boxWidth: 10, padding: 6 } } },
    },
  });
}

function ensureStateCharts(){
  // Labels match the filter chips below (lines ~159-174) so legend + filter
  // vocabulary stay in sync — easier to read the chart then click the matching chip.
  if (!tunerStateChart) {
    tunerStateChart = _makeDoughnut('chart-tuner-state',
      ['No tuner', 'Tuning', 'Maintaining', 'Offline', 'Error', 'Stopped'],
      ['#8b8fa3', '#5b8af5', '#4caf50', '#ff9800', '#ff6b6b', '#6c757d']);
  }
  if (!miningStateChart) {
    miningStateChart = _makeDoughnut('chart-mining-state',
      ['Mining', 'Not mining', 'Unknown'],
      ['#4caf50', '#f44336', '#8b8fa3']);
  }
}

function matchesFilter(m){
  if (currentFilter.tuner !== 'all' && m.tuner_bucket !== currentFilter.tuner) return false;
  if (currentFilter.mining !== 'all' && m.mining_bucket !== currentFilter.mining) return false;
  if (currentFilter.models !== null) {
    const modelKey = m.model || MODEL_FILTER_UNKNOWN;
    if (!currentFilter.models.has(modelKey)) return false;
  }
  return true;
}

function _allFleetModels(){
  const models = new Set();
  (overviewData.miners || []).forEach(m => models.add(m.model || MODEL_FILTER_UNKNOWN));
  return [...models].sort((a, b) => a.localeCompare(b));
}

function loadModelFilter(){
  try {
    const raw = localStorage.getItem(MODEL_FILTER_STORAGE_KEY);
    if (raw === null) {
      currentFilter.models = null;
      return;
    }
    const arr = JSON.parse(raw);
    currentFilter.models = new Set(Array.isArray(arr) ? arr : []);
  } catch (_e) {
    currentFilter.models = null;
  }
}

function saveModelFilter(){
  try {
    if (currentFilter.models === null) {
      localStorage.removeItem(MODEL_FILTER_STORAGE_KEY);
    } else {
      localStorage.setItem(MODEL_FILTER_STORAGE_KEY, JSON.stringify([...currentFilter.models]));
    }
  } catch (_e) {
    // localStorage unavailable (private mode, quota exceeded) — non-fatal
  }
}

function populateModelFilterOptions(){
  const container = document.getElementById('model-filter-options');
  if (!container) return;
  const models = _allFleetModels();
  if (models.length === 0) {
    container.innerHTML = '<div class="model-filter-empty">No miners loaded</div>';
    updateModelFilterSummary();
    return;
  }
  container.innerHTML = models.map(model => {
    const checked = (currentFilter.models === null || currentFilter.models.has(model)) ? 'checked' : '';
    return `<label class="model-filter-option">
      <input type="checkbox" ${checked} data-change-action="onModelFilterToggle" data-arg-model="${escapeHTML(model)}">
      <span>${escapeHTML(model)}</span>
    </label>`;
  }).join('');
  updateModelFilterSummary();
}

function updateModelFilterSummary(){
  const summary = document.getElementById('model-filter-summary');
  if (!summary) return;
  if (currentFilter.models === null) {
    summary.textContent = 'All models';
    return;
  }
  const fleetModels = _allFleetModels();
  // Restrict the visible-count to models actually present in the current
  // fleet so a stale localStorage entry doesn't inflate the number.
  const visible = fleetModels.filter(m => currentFilter.models.has(m)).length;
  if (visible === 0) summary.textContent = 'No models';
  else if (visible === fleetModels.length) summary.textContent = 'All models';
  else summary.textContent = `${visible} of ${fleetModels.length} models`;
}

function onModelFilterToggle(args, target){
  const model = args.model;
  if (model === undefined) return;
  // Lazily promote null → explicit Set on first interaction. The implicit
  // "show all" state stays null in storage until the operator commits.
  if (currentFilter.models === null) {
    currentFilter.models = new Set(_allFleetModels());
  }
  if (target.checked) currentFilter.models.add(model);
  else currentFilter.models.delete(model);
  saveModelFilter();
  updateModelFilterSummary();
  renderTable();
}

function modelFilterSelectAll(){
  currentFilter.models = null;
  saveModelFilter();
  populateModelFilterOptions();
  renderTable();
}

function modelFilterClearAll(){
  currentFilter.models = new Set();
  saveModelFilter();
  populateModelFilterOptions();
  renderTable();
}

function fmtNum(v, unit, digits=1){
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v);
  if (n === 0) return '—';
  return n.toFixed(digits) + (unit ? ' ' + unit : '');
}

function phaseLabel(phase){
  if (!phase) return '—';
  // Strip the "phaseN_" or "phase_X_" prefix (e.g. "phase_v_exploration" → "exploration").
  return phase.replace(/^phase(?:\d+|_[a-z])_/, '').replace(/_/g, ' ');
}

// ─── Customizable columns (Track 1 of dashboard improvements) ──────────────
// FLEET_COLUMNS is the source of truth for which data columns the operator
// can show/hide/reorder via the "Columns" filter button. Pinned columns
// (checkbox, IP, Actions ×) are NOT in this list — they're rendered by
// renderTable() outside the FLEET_COLUMNS map.
function statePillFor(m) {
  // Renders the State column cell. Mirrors the inline template that lived
  // in renderTable() pre-Unit-3-refactor: when the tuner has detected the
  // miner offline, overlays an explicit "Offline" pill instead of the
  // (now-stale) last-known operating state.
  const offline = m.tuner_bucket === 'offline';
  return offline
    ? `<span class="state-pill state-offline" title="${escapeHTML(m.tuner_phase_detail || '')}">Offline</span>`
    : `<span class="state-pill state-${m.mining_bucket}">${escapeHTML(m.operating_state || m.mining_bucket || '')}</span>`;
}

function phasePillFor(m) {
  return `<span class="phase-pill ${m.tuner_bucket}">${escapeHTML(phaseLabel(m.tuner_phase))}</span>`;
}

function formatProfit(m) {
  return (m.profit_usd_day != null) ? '$' + m.profit_usd_day.toFixed(2) : '—';
}

const COLUMN_PREFS_STORAGE_KEY = 'tuner.fleetTable.columnPrefs.v1';

const FLEET_COLUMNS = [
  {key: 'mac', label: 'MAC', defaultVisible: false, render: (m) => `<span style="font-family:monospace;white-space:nowrap;font-size:0.9em">${escapeHTML(m.mac || '—')}</span>`},
  {key: 'mrr', label: 'MRR', render: (m) => renderMrrPill(m.mrr_rental_status, m.mac)},
  {key: 'hostname', label: 'Hostname', render: (m) => escapeHTML(m.hostname || '—')},
  {key: 'model', label: 'Model', render: (m) => escapeHTML(m.model || '—')},
  {key: 'state', label: 'State', render: (m) => statePillFor(m)},
  {key: 'phase', label: 'Phase', render: (m) => phasePillFor(m)},
  {key: 'hashrate', label: 'Hashrate', render: (m, ctx) => fmtNum(m.hashrate_ths, 'TH/s') + (ctx && ctx.staleTag || ''), muted: true},
  {key: 'power', label: 'Power', render: (m) => fmtNum(m.power_w, 'W', 0), muted: true},
  {key: 'efficiency', label: 'J/TH', render: (m) => fmtNum(m.efficiency_jth, 'J/TH'), muted: true},
  {key: 'profit', label: '$/day', render: (m) => formatProfit(m), muted: true},
  {key: 'voltage', label: 'Voltage', render: (m) => fmtNum(m.voltage_mv, 'mV', 0), muted: true},
  {key: 'board_t', label: 'Board T', render: (m) => fmtNum(m.avg_board_temp_c, '°C'), muted: true},
  {key: 'chip_t', label: 'Chip T', render: (m) => fmtNum(m.avg_chip_temp_c, '°C'), muted: true}
];

function normalizeColumnPrefs(raw) {
  if (raw === null || raw === undefined || typeof raw !== 'object') {
    return {
      version: 1,
      columns: FLEET_COLUMNS.map(c => ({key: c.key, visible: true}))
    };
  }
  if (raw.version !== 1) {
    return {
      version: 1,
      columns: FLEET_COLUMNS.map(c => ({key: c.key, visible: true}))
    };
  }
  if (!Array.isArray(raw.columns)) {
    return {
      version: 1,
      columns: FLEET_COLUMNS.map(c => ({key: c.key, visible: true}))
    };
  }
  const validColumns = raw.columns
    .filter(col => typeof col === 'object' && col !== null && typeof col.key === 'string' && typeof col.visible === 'boolean')
    .filter(col => FLEET_COLUMNS.some(fcol => fcol.key === col.key));
  const existingKeys = new Set(validColumns.map(col => col.key));
  const missingColumns = FLEET_COLUMNS.filter(col => !existingKeys.has(col.key));
  const resultColumns = [...validColumns, ...missingColumns.map(col => ({key: col.key, visible: col.defaultVisible !== false}))];
  return {
    version: 1,
    columns: resultColumns
  };
}

function loadColumnPrefs() {
  try {
    const raw = localStorage.getItem(COLUMN_PREFS_STORAGE_KEY);
    if (raw === null) {
      return normalizeColumnPrefs(null);
    }
    const parsed = JSON.parse(raw);
    return normalizeColumnPrefs(parsed);
  } catch (_e) {
    return normalizeColumnPrefs(null);
  }
}

function saveColumnPrefs(prefs) {
  try {
    localStorage.setItem(COLUMN_PREFS_STORAGE_KEY, JSON.stringify(prefs));
  } catch (_e) {
    // localStorage unavailable (private mode, quota exceeded) — non-fatal
  }
}

let activeColumnPrefs = null;

// ─── Customizable columns: dynamic-header rendering helpers ────────────────
// Read activeColumnPrefs (loaded at boot) and FLEET_COLUMNS (canonical
// definitions) and produce the ordered list of currently-visible data
// columns. Pinned columns (checkbox, IP, Actions) are NOT in this list —
// they're hardcoded in renderTable() and renderTableHeader() outside the
// FLEET_COLUMNS map.
function getActiveColumns() {
  if (!activeColumnPrefs) {
    activeColumnPrefs = loadColumnPrefs();
  }
  const byKey = Object.fromEntries(FLEET_COLUMNS.map(c => [c.key, c]));
  return activeColumnPrefs.columns
    .filter(p => p.visible && byKey[p.key])
    .map(p => byKey[p.key]);
}

function renderTableHeader() {
  const thead = document.querySelector('#miner-table thead tr');
  if (!thead) return;
  const cols = getActiveColumns();
  const fixedLeft = `<th style="width:28px"><input type="checkbox" id="select-all" data-change-action="toggleSelectAll"></th>`
                  + `<th>IP</th>`;
  const dynamic = cols.map(c => `<th data-col="${c.key}">${escapeHTML(c.label)}</th>`).join('');
  const fixedRight = `<th></th>`;
  thead.innerHTML = fixedLeft + dynamic + fixedRight;
}

function renderTable(){
  const tbody = document.getElementById('miner-tbody');
  if (!tbody) return;
  const miners = (overviewData.miners || []).filter(matchesFilter);
  const activeCols = getActiveColumns();
  const totalCols = 2 + activeCols.length + 1;  // checkbox + IP + dynamic + actions

  if (!miners.length) {
    tbody.innerHTML = `<tr><td colspan="${totalCols}" style="padding:24px;text-align:center;color:var(--text2)">No miners match the current filters.</td></tr>`;
  } else {
    tbody.innerHTML = miners.map(m => {
      // v4 fleet table: MAC is the canonical row key (selection, navigation,
      // bulk actions); the IP column still renders the human-friendly IP for
      // identification. Each row carries data-mac so the dispatcher can find
      // the row when the operator clicks a per-row action.
      const macAttr = escapeHTML(String(m.mac || ''));
      const selected = selectedMacs.has(m.mac) ? 'checked' : '';
      const offline = m.tuner_bucket === 'offline';
      const staleTag = offline ? '<span class="offline-muted-tag">(last known)</span>' : '';
      const scanRanges = (mrrCachedDefaults && Array.isArray(mrrCachedDefaults.SCAN_IP_RANGES)) ? mrrCachedDefaults.SCAN_IP_RANGES : [];
      const outOfRangeBadge = (scanRanges.length > 0 && !ipInAnyRange(m.ip, scanRanges))
        ? `<span style="font-size:0.72em;padding:1px 5px;background:var(--yellow,#e1b969);color:#000;border-radius:3px;margin-left:4px" title="IP is outside all configured scan ranges">out of range</span>`
        : '';
      const ctx = {staleTag, offline};

      const checkboxTd = `<td><input type="checkbox" ${selected} data-change-action="toggleSelect" data-arg-mac="${macAttr}"></td>`;
      const safeIp = escapeHTML(String(m.ip || ''));
      const ipTd = `<td><a href="http://${safeIp}/" target="_blank" rel="noopener" title="Open miner web UI in a new tab">${safeIp}</a>${outOfRangeBadge}`
                 + `<button class="secondary" data-action="navigateToDetail" data-arg-mac="${macAttr}" title="Open tuner detail view" style="font-size:0.75em;padding:2px 6px;margin-left:6px">Details</button></td>`;
      const dynamicTds = activeCols.map(col => {
        const cls = (col.muted && offline) ? 'offline-muted' : '';
        return `<td data-col="${col.key}"${cls ? ` class="${cls}"` : ''}>${col.render(m, ctx)}</td>`;
      }).join('');
      const actionsTd = `<td><button class="secondary" title="Remove this miner" data-action="removeMiner" data-arg-mac="${macAttr}" data-arg-ip="${escapeHTML(String(m.ip || ''))}">×</button></td>`;

      return `<tr data-mac="${macAttr}">${checkboxTd}${ipTd}${dynamicTds}${actionsTd}</tr>`;
    }).join('');
  }

  // Prune stale selections (a MAC got removed from the fleet).
  const existing = new Set((overviewData.miners || []).map(m => m.mac));
  for (const mac of [...selectedMacs]) if (!existing.has(mac)) selectedMacs.delete(mac);
  updateBulkToolbar();
}

// ─── Customizable columns: modal UI + drag/drop + presets ──────────────────
const COLUMN_PRESETS = {
  default: ['mrr','hostname','model','state','phase','hashrate','power','efficiency','profit','voltage','board_t','chip_t'],
  compact: ['hostname','model','state','phase'],
  thermals: ['state','power','board_t','chip_t'],
  profitability: ['hashrate','power','efficiency','profit'],
};

function openColumnFilterModal() {
  // Build the modal body: 4 preset buttons, sortable list of 12 columns,
  // Save/Cancel actions. Modal-local state lives in the DOM until Save —
  // applyColumnPreset() and the drag-drop handler mutate the <li> order
  // and checkbox state directly; submitColumnPrefs() reads the DOM at
  // commit time.
  const cols = activeColumnPrefs ? activeColumnPrefs.columns : loadColumnPrefs().columns;
  const byKey = Object.fromEntries(FLEET_COLUMNS.map(c => [c.key, c]));
  const rowsHtml = cols.map(p => {
    const def = byKey[p.key];
    if (!def) return '';
    const checked = p.visible ? 'checked' : '';
    return `<li draggable="true" data-col-key="${escapeHTML(p.key)}">
      <span class="drag-handle" title="Drag to reorder">⋮⋮</span>
      <input type="checkbox" id="cp-${escapeHTML(p.key)}" ${checked}>
      <label for="cp-${escapeHTML(p.key)}">${escapeHTML(def.label)}</label>
    </li>`;
  }).join('');

  const body = `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.85em">
      Pinned columns (checkbox, IP, Actions) are always visible. Drag the rows below to reorder; tick the boxes to choose which data columns to show.
    </div>
    <div class="col-prefs-presets">
      <button type="button" class="secondary" data-action="applyColumnPreset" data-arg-name="default">Default</button>
      <button type="button" class="secondary" data-action="applyColumnPreset" data-arg-name="compact">Compact</button>
      <button type="button" class="secondary" data-action="applyColumnPreset" data-arg-name="thermals">Thermals</button>
      <button type="button" class="secondary" data-action="applyColumnPreset" data-arg-name="profitability">Profitability</button>
    </div>
    <ul class="col-prefs-list" id="col-prefs-list">${rowsHtml}</ul>
  `;
  openModal('Customize columns', body, [
    {label: 'Cancel', action: closeModal},
    {label: 'Save', primary: true, action: submitColumnPrefs},
  ]);
  // Wire drag-and-drop AFTER the modal body is in the DOM.
  wireColumnPrefsDrag();
}

function applyColumnPreset(name) {
  const visible = COLUMN_PRESETS[name];
  if (!visible) return;
  const visibleSet = new Set(visible);
  const list = document.getElementById('col-prefs-list');
  if (!list) return;
  // Reorder: visible-set keys first (in preset order), then remaining
  // canonical keys.
  const items = new Map();
  list.querySelectorAll('li').forEach(li => items.set(li.dataset.colKey, li));
  const canonical = FLEET_COLUMNS.map(c => c.key);
  const ordered = [...visible, ...canonical.filter(k => !visibleSet.has(k))];
  ordered.forEach(k => {
    const li = items.get(k);
    if (li) list.appendChild(li);
  });
  // Tick the visible ones, untick the rest.
  list.querySelectorAll('li').forEach(li => {
    const cb = li.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = visibleSet.has(li.dataset.colKey);
  });
}

function submitColumnPrefs() {
  const list = document.getElementById('col-prefs-list');
  if (!list) return;
  const newColumns = [...list.querySelectorAll('li')].map(li => {
    const cb = li.querySelector('input[type="checkbox"]');
    return {key: li.dataset.colKey, visible: !!(cb && cb.checked)};
  });
  const newPrefs = normalizeColumnPrefs({version: 1, columns: newColumns});
  saveColumnPrefs(newPrefs);
  activeColumnPrefs = newPrefs;
  renderTableHeader();
  renderTable();
  closeModal();
}

function wireColumnPrefsDrag() {
  const list = document.getElementById('col-prefs-list');
  if (!list) return;
  let dragged = null;
  list.querySelectorAll('li').forEach(li => {
    li.addEventListener('dragstart', (e) => {
      dragged = li;
      li.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', li.dataset.colKey || '');
    });
    li.addEventListener('dragend', () => {
      li.classList.remove('dragging');
      list.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
      dragged = null;
    });
    li.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragged || dragged === li) return;
      li.classList.add('drag-over');
      e.dataTransfer.dropEffect = 'move';
    });
    li.addEventListener('dragleave', () => li.classList.remove('drag-over'));
    li.addEventListener('drop', (e) => {
      e.preventDefault();
      li.classList.remove('drag-over');
      if (!dragged || dragged === li) return;
      const rect = li.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      list.insertBefore(dragged, after ? li.nextSibling : li);
    });
  });
}

function updateKPIs(){
  const d = overviewData;
  document.getElementById('kpi-hashrate').textContent = fmtNum(d.total_hashrate_ths, 'TH/s');
  document.getElementById('kpi-power').textContent = fmtNum(d.total_power_w, 'W', 0);
  document.getElementById('kpi-efficiency').textContent = fmtNum(d.avg_efficiency_jth, 'J/TH');
  document.getElementById('kpi-profit').textContent =
      (d.total_profit_usd_day != null) ? '$' + d.total_profit_usd_day.toFixed(2) : '--';
  const total = (d.miners || []).length;
  const mining = (d.mining_counts && d.mining_counts.mining) || 0;
  document.getElementById('kpi-miner-count').textContent = `${mining}/${total} miners mining`;
  ensureStateCharts();
  const sc = d.state_counts || {};
  const mc = d.mining_counts || {};
  if (tunerStateChart) {
    // Ordering matches ensureStateCharts labels: No tuner, Tuning, Maintaining, Offline, Error, Stopped
    tunerStateChart.data.datasets[0].data = [
      sc.idle || 0, sc.tuning || 0, sc.maintaining || 0, sc.offline || 0, sc.error || 0, sc.stopped || 0,
    ];
    tunerStateChart.update('none');
  }
  if (miningStateChart) {
    // Ordering: Mining, Not mining (stopped), Unknown
    miningStateChart.data.datasets[0].data = [
      mc.mining || 0, mc.stopped || 0, mc.unknown || 0,
    ];
    miningStateChart.update('none');
  }
}

function setupFilterChips(){
  document.querySelectorAll('#filter-row .chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const group = btn.dataset.filter;
      const val = btn.dataset.value;
      currentFilter[group] = val;
      document.querySelectorAll(`#filter-row .chip[data-filter="${group}"]`).forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderTable();
    });
  });
}

function toggleSelect(mac, checked){
  if (checked) selectedMacs.add(mac);
  else selectedMacs.delete(mac);
  updateBulkToolbar();
}

function toggleSelectAll(cb){
  if (cb.checked) {
    (overviewData.miners || []).filter(matchesFilter).forEach(m => selectedMacs.add(m.mac));
  } else {
    (overviewData.miners || []).filter(matchesFilter).forEach(m => selectedMacs.delete(m.mac));
  }
  renderTable();
}

function clearSelection(){ selectedMacs.clear(); renderTable(); }

function updateBulkToolbar(){
  const n = selectedMacs.size;
  const bar = document.getElementById('bulk-toolbar');
  if (bar) bar.style.display = n > 0 ? 'flex' : 'none';
  const c = document.getElementById('bulk-count');
  if (c) c.textContent = `${n} selected`;
  const all = document.getElementById('select-all');
  if (all) {
    const visible = (overviewData.miners || []).filter(matchesFilter);
    all.checked = visible.length > 0 && visible.every(m => selectedMacs.has(m.mac));
  }
}

// ─── Reset-scope picker ──────────────────────────────────────────────────────
// Shared between the single-miner Reset Profile button and the bulk toolbar.
// Four scopes map to backend `/tuner/delete_profile` semantics:
//   chip              — keep Phase V surface + baseline; redo Phase 3/3b/4
//   chip_fine         — keep coarse surface + baseline; redo fine + Phase 3/3b/4
//   chip_fine_coarse  — keep baseline only; redo Phase V onwards
//   all               — full reset (historical behavior)
const RESET_SCOPES = [
  { value: 'chip',             label: 'Chip tuning',                  hint: 'Keep Phase V surface and baseline. Redoes per-chip Phase 3/3b/4 at each top-K voltage.' },
  { value: 'chip_fine',        label: 'Chip tuning + fine grid',      hint: 'Keep coarse surface and baseline. Redoes fine grid, top-K pick, and chip tuning.' },
  { value: 'chip_fine_coarse', label: 'Chip tuning + fine + coarse',  hint: 'Keep Phase 2 baseline only. Redoes the full Phase V surface and everything after.' },
  { value: 'all',              label: 'All (full reset)',             hint: 'Delete profile + checkpoint + log. Stock baseline is preserved. Next tune starts from Phase 0.' },
];
function openResetScopeModal({title, intro, onConfirm}){
  const rows = RESET_SCOPES.map((s, i) => `
    <label class="scope-option${i === 0 ? ' scope-selected' : ''}" data-scope="${s.value}">
      <input type="radio" name="reset-scope" value="${s.value}" ${i === 0 ? 'checked' : ''}>
      <div>
        <div style="font-weight:600">${escapeHTML(s.label)}</div>
        <div style="color:var(--text2);font-size:0.85em">${escapeHTML(s.hint)}</div>
      </div>
    </label>`).join('');
  openModal(title, `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.9em">${escapeHTML(intro)}</div>
    ${rows}
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Reset', danger: true, action: () => {
      const picked = document.querySelector('input[name="reset-scope"]:checked');
      onConfirm(picked ? picked.value : 'chip');
    }},
  ]);
  document.querySelectorAll('.scope-option').forEach(el => {
    el.addEventListener('click', () => {
      document.querySelectorAll('.scope-option').forEach(e => e.classList.remove('scope-selected'));
      el.classList.add('scope-selected');
    });
  });
}

// ─── Modal helper ────────────────────────────────────────────────────────────
function openModal(title, bodyHTML, actions){
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = bodyHTML;
  const actionsEl = document.getElementById('modal-actions');
  actionsEl.innerHTML = '';
  (actions || [{label:'Close', action: closeModal}]).forEach(a => {
    const btn = document.createElement('button');
    btn.textContent = a.label;
    btn.className = a.danger ? 'danger' : (a.primary ? '' : 'secondary');
    btn.onclick = (e) => { e.preventDefault(); a.action(); };
    actionsEl.appendChild(btn);
  });
  document.getElementById('modal-backdrop').style.display = 'flex';
}
function closeModal(){
  document.getElementById('modal-backdrop').style.display = 'none';
  stopScanStatusPolling();
  // Stop any live-refresh timer started by a voltage-log modal so it doesn't
  // keep polling in the background after the modal is dismissed.
  if (typeof _voltageLogTimer !== 'undefined' && _voltageLogTimer) {
    clearInterval(_voltageLogTimer);
    _voltageLogTimer = null;
  }
  if (typeof _voltageLogOpen !== 'undefined') _voltageLogOpen = null;
}

function renderMrrPill(rentalStatus, mac){
  // When `mac` is provided, the pill is a clickable trigger for the MRR action
  // popup (change rig / open rig page / toggle fleet enable). Without it, the
  // pill is plain text. Fleet table + detail page both pass mac; legacy
  // call sites that don't pass mac still render correctly as a static badge.
  const macAttr = mac ? `data-action="openMrrPillModal" data-arg-mac="${escapeHTML(String(mac))}"` : '';
  if (!rentalStatus) {
    if (!mac) return '—';
    return `<span class="mrr-pill-empty" ${macAttr} title="No MRR rig configured. Click to set one up.">+ MRR</span>`;
  }
  // Diagnostic states surface why the rental status isn't live (e.g. operator
  // set MRR_RIG_ID per-miner but never enabled MRR globally). Without these,
  // the column would render an opaque em-dash and the operator wouldn't know
  // which knob to flip.
  if (rentalStatus.state === 'disabled') {
    const tip = escapeHTML(String(rentalStatus.error || 'MRR is disabled')) + (mac ? ' — click to manage' : '');
    return `<span class="mrr-pill-disabled" ${macAttr} title="${tip}">MRR off</span>`;
  }
  if (rentalStatus.state === 'no_creds') {
    const tip = escapeHTML(String(rentalStatus.error || 'MRR credentials not configured')) + (mac ? ' — click to manage' : '');
    return `<span class="mrr-pill-disabled" ${macAttr} title="${tip}">No creds</span>`;
  }
  if (rentalStatus.error) {
    const tip = escapeHTML(String(rentalStatus.error)) + (mac ? ' — click to manage' : '');
    return `<span class="mrr-pill-error" ${macAttr} title="${tip}">MRR error</span>`;
  }
  if (rentalStatus.rented === true) {
    return `<span class="mrr-pill-rented" ${macAttr} title="Currently rented${mac ? ' — click to manage' : ''}">Rented</span>`;
  }
  if (rentalStatus.rented === false) {
    return `<span class="mrr-pill-available" ${macAttr} title="Available${mac ? ' — click to manage' : ''}">Available</span>`;
  }
  return ip ? `<span class="mrr-pill-empty" ${ipAttr} title="Click to manage MRR for this miner">+ MRR</span>` : '—';
}
function escapeHTML(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[c])); }

function isValidMacForBulk(s) {
  // Returns true for canonical-MAC formats (colon, dash, bare 12-hex) and synth IDs.
  // Mirrors tuner_app/constants.py _normalize_mac's accepted forms.
  if (typeof s !== 'string') return false;
  return /^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$|^[0-9a-fA-F]{12}$|^syn-[0-9a-fA-F][0-9a-fA-F\-]*$/.test(s.trim());
}

function showBulkResults(title, resp){
  const results = resp.results || {};
  const summary = resp.summary || {};
  const rows = Object.entries(results).map(([ip, r]) => {
    if (r.ok) return `<li style="color:var(--green)">✓ ${escapeHTML(ip)}</li>`;
    // Platform-mismatch (Phase 2 wire contract): r.detail.reason === "platform_mismatch"
    if (r.detail && r.detail.reason === 'platform_mismatch') {
      const expected = escapeHTML(String(r.detail.expected || ''));
      const actual = escapeHTML(String(r.detail.actual || ''));
      return `<li style="color:var(--yellow)">⊘ ${escapeHTML(ip)} — skipped (template ${expected}, miner ${actual})</li>`;
    }
    return `<li style="color:var(--red)">✗ ${escapeHTML(ip)}: ${escapeHTML(r.error || 'unknown error')}</li>`;
  }).join('');
  const total = summary.total || 0;
  const succeeded = summary.succeeded || 0;
  const failed = summary.failed || 0;
  // Count platform_mismatch separately for the subtitle annotation.
  // Note: backend's _bulk_run_platform_aware counts platform_mismatch as failed,
  // so the skipped count is informational client-side gloss only.
  const skipped = Object.values(results).filter(r => r.detail && r.detail.reason === 'platform_mismatch').length;
  let subtitle = `${succeeded}/${total} succeeded`;
  if (failed > 0) subtitle += `, ${failed} failed`;
  if (skipped > 0) subtitle += ` (${skipped} skipped — platform mismatch)`;
  openModal(title, `
    <div style="color:var(--text2);margin-bottom:10px">${subtitle}</div>
    <ul style="list-style:none;padding:0;margin:0;font-size:0.9em">${rows || '<li style="color:var(--text2)">(no results)</li>'}</ul>
  `, [{label:'Close', action: closeModal}]);
}

// ─── Bulk actions: start / stop / reset_profile / mining / reboot / mrr / retune ───
const BULK_LABELS = {
  start: 'Start Tuning',
  stop: 'Stop Tuning',
  reset_profile: 'Reset Profile',
  start_mining: 'Start Mining',
  stop_mining: 'Stop Mining',
  reboot: 'Reboot',
  set_power_limit: 'Set Power Limit',
  mrr_resync: 'MRR Resync',
  retune_voltage: 'Retune Voltage',
};
const BULK_WARNINGS = {
  stop: 'Engines stop gracefully after the current polling step completes. The miner itself keeps mining.',
  reset_profile: 'This deletes each selected miner\'s saved tuning profile and checkpoint files. Their engines will be recreated in the idle state. This cannot be undone.',
  stop_mining: 'Stops the miner\'s hashing process directly. The tuner thread keeps running but has no live data to react to.',
  reboot: 'Reboots each selected miner immediately (delay=0). Hashing pauses for ~60–90 seconds while the firmware restarts.',
  set_power_limit: 'Applies a fleet-wide power cap. Vendors without an external power-limit endpoint (ePIC) are skipped automatically.',
  retune_voltage: 'Schedules a remeasure at each engine\'s currently-active voltage. Miners without a profile yet are reported as failed.',
};

function _macToIpDisplay(mac){
  // Render the operator-friendly identifier for a selected MAC: prefer the
  // current IP from the overview snapshot; fall back to the MAC itself when
  // no row is in flight.
  const row = (overviewData.miners || []).find(m => m.mac === mac);
  return row && row.ip ? row.ip : mac;
}

function bulkAction(action){
  if (selectedMacs.size === 0) return;
  const label = BULK_LABELS[action] || action;
  const allMacs = [...selectedMacs];
  // Defensive filter: drop entries whose format won't survive the backend's
  // _normalize_mac validator. Stale synth-MAC re-keys (PR #52) and legacy
  // IP-as-MAC entries get silently filtered server-side, surfacing as the
  // dreaded "0/0 miners succeeded" empty-result modal.
  const macs = allMacs.filter(isValidMacForBulk);
  const dropped = allMacs.length - macs.length;
  if (dropped > 0) {
    // Diagnostic: surface what got filtered so future "0/0 succeeded"
    // reports can be traced from browser DevTools without server logs.
    const droppedValues = allMacs.filter(m => !isValidMacForBulk(m));
    console.warn(`[bulk] ${action}: dropped ${dropped} stale/malformed selection(s):`, droppedValues);
  }
  const droppedNote = dropped > 0
    ? `<div style="color:var(--yellow);font-size:0.85em;margin-bottom:8px">⚠ Skipped ${dropped} stale or malformed selection(s) — please reselect by reloading the page if this happens repeatedly.</div>`
    : '';

  if (macs.length === 0) {
    openModal('No valid selections',
      `<div style="color:var(--red);margin-bottom:8px">All ${allMacs.length} selection(s) had stale or malformed identifiers. Please reselect by reloading the page.</div>`,
      [{label: 'Close', action: closeModal}]);
    return;
  }

  if (action === 'reset_profile') {
    openResetScopeModal({
      title: `${label} — ${macs.length} miner${macs.length === 1 ? '' : 's'}`,
      intro: 'Pick how much of each miner\'s tune to clear. Smaller scopes let you redo only the expensive tail.',
      onConfirm: (scope) => runBulk('reset_profile', macs, {scope}),
    });
    return;
  }

  const warn = BULK_WARNINGS[action] || '';
  const body = `
    ${droppedNote}
    <div style="color:var(--text2);margin-bottom:8px">Applying <strong>${escapeHTML(label)}</strong> to ${macs.length} miner${macs.length === 1 ? '' : 's'}:</div>
    <ul style="max-height:240px;overflow:auto;padding-left:20px;margin-bottom:12px">${macs.map(m => `<li>${escapeHTML(_macToIpDisplay(m))}</li>`).join('')}</ul>
    ${warn ? `<div style="color:var(--yellow);font-size:0.85em">${escapeHTML(warn)}</div>` : ''}
  `;
  openModal(`${label} — confirm`, body, [
    {label: 'Cancel', action: closeModal},
    {label: 'Confirm', danger: action !== 'start', action: () => runBulk(action, macs)},
  ]);
}

async function runBulk(action, macs, extraBody){
  closeModal();
  openModal(`${BULK_LABELS[action] || action} — in progress`,
            `<div style="color:var(--text2)">Dispatching to ${macs.length} miner${macs.length === 1 ? '' : 's'}…</div>`, []);
  const body = Object.assign({macs}, extraBody || {});
  const resp = await fetchJSON(`/tuner/bulk/${action}`, {
    method: 'POST', body: JSON.stringify(body), headers: {'Content-Type': 'application/json'},
  });
  if (!resp) {
    openModal('Error', '<div style="color:var(--red)">Server unreachable.</div>', [{label:'Close', action: closeModal}]);
    return;
  }
  // Diagnostic: if we POSTed a non-empty mac list but the response shows
  // total: 0, log the request body + response so the operator can paste
  // the DevTools console into a bug report. The post-PR-#54 backend now
  // returns HTTP 400 with a clear errors[] payload for the all-empty path,
  // but other 0/total/0 shapes (e.g. legacy clients) may still surface here.
  const total = (resp.summary && resp.summary.total) || 0;
  if (macs.length > 0 && total === 0) {
    console.warn(`[bulk] ${action}: server returned total=0 despite POSTing ${macs.length} mac(s). Request body: ${JSON.stringify(body)}. Response: ${JSON.stringify(resp).slice(0,500)}`);
  }
  showBulkResults(`${BULK_LABELS[action] || action} — results`, resp);
  // Refresh the overview so the table reflects the new state.
  pollOverview();
}

// ─── Bulk: remove selected miners ──────────────────────────────────────────
// Wipes per-miner config + ALL on-disk state (.json, .checkpoint.json,
// .stock.json, .log.jsonl) for every selected IP. Fresh state — re-discovery
// via the scanner starts the miner from zero. Mirrors the single-miner Remove
// (×) button but loops via /tuner/bulk/remove so the same config_lock
// discipline applies per IP.
function bulkRemove(){
  if (selectedMacs.size === 0) return;
  const macs = [...selectedMacs];
  const body = `
    <div style="color:var(--text2);margin-bottom:8px">
      Remove <strong>${macs.length}</strong> miner${macs.length === 1 ? '' : 's'}:
    </div>
    <ul style="max-height:240px;overflow:auto;padding-left:20px;margin-bottom:12px">${macs.map(m => `<li>${escapeHTML(_macToIpDisplay(m))}</li>`).join('')}</ul>
    <div style="color:var(--yellow);font-size:0.85em">
      This deletes the saved tuning profile, sweep checkpoint, stock baseline, and tuner log for every selected miner. The scanner may re-discover them on the next pass if their IPs are still in the configured ranges. This action cannot be undone.
    </div>
  `;
  openModal(`Remove ${macs.length} miner${macs.length === 1 ? '' : 's'} — confirm`, body, [
    {label: 'Cancel', action: closeModal},
    {label: 'Remove', danger: true, action: () => runBulkRemove(macs)},
  ]);
}

async function runBulkRemove(macs){
  closeModal();
  openModal('Remove — in progress',
            `<div style="color:var(--text2)">Removing ${macs.length} miner${macs.length === 1 ? '' : 's'}…</div>`, []);
  const resp = await fetchJSON('/tuner/bulk/remove', {
    method: 'POST', body: JSON.stringify({macs}),
    headers: {'Content-Type': 'application/json'},
  });
  if (!resp) {
    openModal('Error', '<div style="color:var(--red)">Server unreachable.</div>', [{label:'Close', action: closeModal}]);
    return;
  }
  // Drop removed MACs from the selection so the toolbar count reflects reality
  // even if some failed (failed MACs stay selected so the operator can retry).
  const removedSet = new Set();
  Object.entries(resp.results || {}).forEach(([mac, r]) => { if (r && r.ok) removedSet.add(mac); });
  removedSet.forEach(mac => selectedMacs.delete(mac));
  // If the user removed the miner whose detail page they're on, bounce them
  // back to the overview before the next poll runs.
  if (currentMac() && removedSet.has(currentMac())) navigateToOverview();
  showBulkResults('Remove — results', resp);
  pollOverview();
}

// ─── Bulk: apply config to selected miners ──────────────────────────────────
// Platform selector + category checkboxes (opt-in, default ALL UNCHECKED).
// Only checked categories' fields are sent. Miners whose firmware_type doesn't
// match the selected template platform are skipped by the backend and shown as
// ⊘ in the result modal.

const _BULK_PLATFORMS = ['epic', 'bixbit', 'luxos', 'braiins', 'whatsminer'];
// Module-level state for the bulk-apply modal.
let bulkApplyState = null;

function _pickDefaultPlatform(macs, minerConfigs) {
  const counts = {};
  for (const mac of macs) {
    const entry = (minerConfigs && minerConfigs[mac]) || {};
    const ft = entry.current_firmware || entry.firmware_type || 'epic';
    counts[ft] = (counts[ft] || 0) + 1;
  }
  const sorted = Object.entries(counts).sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    return a[0].localeCompare(b[0]);
  });
  if (sorted.length === 0) return 'epic';
  return sorted[0][0];
}

async function openBulkConfigModal(){
  if (selectedMacs.size === 0) return;
  const cfg = await fetchJSON('/tuner/config');
  const minerConfigs = (cfg && cfg.miner_configs) || {};
  const macs = [...selectedMacs];
  const defaultPlatform = _pickDefaultPlatform(macs, minerConfigs);
  bulkApplyState = {
    platform: defaultPlatform,
    enabledCategories: new Set(), // default ALL UNCHECKED — opt-in semantics
    cfg,
    macs,
  };

  // Categories visible in the bulk-apply form: not fleetOnly (hideFromDefaults still included — MRR etc.)
  const cats = CONFIG_CATEGORIES.filter(c => !c.fleetOnly);
  const platformOptions = _BULK_PLATFORMS.map(p =>
    `<option value="${p}"${p === defaultPlatform ? ' selected' : ''}>${escapeHTML(p)}</option>`
  ).join('');
  const checkboxRow = cats.map(c => `
    <label class="bulk-cat-checkbox">
      <input type="checkbox" data-change-action="toggleBulkConfigCategory" data-arg-cat="${_escAttr(c.name)}">
      ${escapeHTML(c.name)} (${c.keys.length})
    </label>
  `).join('');

  // Count miners per platform among selected (for the info breakdown).
  const platformCounts = {};
  for (const mac of macs) {
    const entry = minerConfigs[mac] || {};
    const ft = entry.current_firmware || entry.firmware_type || 'epic';
    platformCounts[ft] = (platformCounts[ft] || 0) + 1;
  }
  const platformBreakdown = Object.entries(platformCounts)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([p, n]) => `${escapeHTML(p)}: ${n}`)
    .join(' / ');

  openModal(`Apply Config to ${macs.length} miner${macs.length === 1 ? '' : 's'}`, `
    <div style="color:var(--text2);margin-bottom:8px;font-size:0.85em">
      Pick a template platform and which groups to apply. Miners whose firmware platform doesn't match the template will be skipped.
    </div>
    <div class="bulk-platform-bar" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <label for="bulkcfg-platform" style="margin:0">Template:</label>
      <select id="bulkcfg-platform" data-change-action="bulkPlatformChange">${platformOptions}</select>
      <span style="color:var(--text2);font-size:0.85em;margin-left:auto">Selected: ${platformBreakdown}</span>
    </div>
    <div class="bulk-cat-toolbar" style="margin-bottom:8px;padding:8px;border:1px solid var(--border);border-radius:6px">
      <div style="display:flex;gap:6px;margin-bottom:6px">
        <button class="secondary" data-action="bulkConfigSelectAll" type="button">Select all</button>
        <button class="secondary" data-action="bulkConfigSelectNone" type="button">Select none</button>
      </div>
      <div id="bulkcfg-cat-checkboxes" style="display:flex;flex-wrap:wrap;gap:8px">${checkboxRow}</div>
    </div>
    <div id="bulkcfg-form-root" style="max-height:380px;overflow-y:auto;padding-right:4px"></div>
    <div id="bulkcfg-footer" style="color:var(--text2);font-size:0.85em;margin-top:6px;min-height:1em"></div>
    <div id="bulkcfg-error" style="color:var(--red);font-size:0.85em;margin-top:6px;min-height:1em"></div>
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Apply', action: () => submitBulkConfig(macs)},
  ]);

  // Build form after modal is attached so IDs resolve.
  _bulkApplyRebuildForm();
}

function _bulkApplyRebuildForm(){
  if (!bulkApplyState) return;
  const root = document.getElementById('bulkcfg-form-root');
  if (!root) return;
  // Filter rendered knobs by the selected platform's capabilities so e.g. a
  // Whatsminer-platform bulk apply doesn't expose ePIC chip-tune knobs that
  // do nothing on that vendor. Mirrors the per-miner config tab's pattern.
  const capabilities = PLATFORM_CAPABILITIES[bulkApplyState.platform] || PLATFORM_CAPABILITIES.epic;
  buildConfigForm(root, 'bulkcfg-', { capabilities });
  // Prefill from the selected platform's defaults
  const platformDefaults = (bulkApplyState.cfg && bulkApplyState.cfg.defaults && bulkApplyState.cfg.defaults[bulkApplyState.platform]) || {};
  CFG_KEYS.forEach(k => {
    const meta = CFG_META[k];
    if (!meta) return;
    setFormValue('bulkcfg-' + k, platformDefaults[k], meta.type);
  });
  _bulkApplyUpdateUI();
}

function _bulkApplyUpdateUI(){
  if (!bulkApplyState) return;
  const cats = CONFIG_CATEGORIES.filter(c => !c.fleetOnly);

  // Sync checkbox checked state with bulkApplyState
  cats.forEach(c => {
    // querySelector scoped to the checkbox toolbar; match input by data-arg-cat
    const cb = document.querySelector(`#bulkcfg-cat-checkboxes input[data-arg-cat="${CSS.escape(c.name)}"]`);
    if (cb) cb.checked = bulkApplyState.enabledCategories.has(c.name);
  });

  // Dim excluded <details class="config-cat"> blocks by name (data-cat-name).
  // Name-based lookup is robust to additional categories rendered by buildConfigForm
  // that are NOT in the checkbox-filtered list (e.g. hideFromDefaults categories).
  // Categories without a checkbox are treated as always-enabled so they stay visible.
  const detailsBlocks = document.querySelectorAll('#bulkcfg-form-root details.config-cat[data-cat-name]');
  detailsBlocks.forEach(block => {
    const catName = block.dataset.catName;
    const hasCb = cats.some(c => c.name === catName);
    // Only dim categories that have a checkbox; always-on categories stay at full opacity.
    if (!hasCb) return;
    const enabled = bulkApplyState.enabledCategories.has(catName);
    block.classList.toggle('cat-excluded', !enabled);
  });

  // Update footer count
  const ftr = document.getElementById('bulkcfg-footer');
  if (ftr) {
    const eligibleKeys = cats
      .filter(c => bulkApplyState.enabledCategories.has(c.name))
      .flatMap(c => c.keys);
    const filledKeys = eligibleKeys.filter(k => {
      const meta = CFG_META[k];
      if (!meta) return false;
      const v = readFormValue('bulkcfg-' + k, meta.type);
      return v !== undefined;
    });
    // Count miners that match the selected platform (v4 entries are MAC-keyed).
    const minerConfigs = (bulkApplyState.cfg && bulkApplyState.cfg.miner_configs) || {};
    const matching = bulkApplyState.macs.filter(mac => {
      const entry = minerConfigs[mac] || {};
      const ft = entry.current_firmware || entry.firmware_type || 'epic';
      return ft === bulkApplyState.platform;
    });
    const skipped = bulkApplyState.macs.length - matching.length;
    const skippedNote = skipped > 0 ? ` (${skipped} will be skipped — platform mismatch)` : '';
    ftr.textContent = `Will push ${filledKeys.length} key${filledKeys.length === 1 ? '' : 's'} to ${matching.length} miner${matching.length === 1 ? '' : 's'}${skippedNote}.`;
  }
}

function toggleBulkConfigCategory(args, input){
  if (!bulkApplyState || !input) return;
  const cat = args.cat;
  if (!cat) return;
  if (input.checked) bulkApplyState.enabledCategories.add(cat);
  else bulkApplyState.enabledCategories.delete(cat);
  _bulkApplyUpdateUI();
}

function bulkConfigSelectAll(){
  if (!bulkApplyState) return;
  const cats = CONFIG_CATEGORIES.filter(c => !c.fleetOnly);
  bulkApplyState.enabledCategories = new Set(cats.map(c => c.name));
  _bulkApplyUpdateUI();
}

function bulkConfigSelectNone(){
  if (!bulkApplyState) return;
  bulkApplyState.enabledCategories = new Set();
  _bulkApplyUpdateUI();
}

function bulkPlatformChange(_args, input){
  if (!bulkApplyState || !input) return;
  bulkApplyState.platform = input.value;
  _bulkApplyRebuildForm();
}

async function submitBulkConfig(macs){
  if (!bulkApplyState) return;
  const errEl = document.getElementById('bulkcfg-error');
  if (errEl) errEl.textContent = '';
  const cats = CONFIG_CATEGORIES.filter(c => !c.fleetOnly);
  const eligibleCats = cats.filter(c => bulkApplyState.enabledCategories.has(c.name));
  if (eligibleCats.length === 0) {
    if (errEl) errEl.textContent = 'Check at least one category to apply.';
    return;
  }
  const payload = {};
  for (const c of eligibleCats) {
    for (const k of c.keys) {
      const meta = CFG_META[k];
      if (!meta) continue;
      const v = readFormValue('bulkcfg-' + k, meta.type);
      if (v !== undefined) payload[k] = v;
    }
  }
  if (Object.keys(payload).length === 0) {
    if (errEl) errEl.textContent = 'No values filled in for the checked categories.';
    return;
  }
  const platform = bulkApplyState.platform;
  closeModal();
  openModal('Apply Config — in progress',
    `<div style="color:var(--text2)">Pushing ${Object.keys(payload).length} ${escapeHTML(platform)} override(s) to up to ${macs.length} miner(s)…</div>`, []);
  const resp = await fetchJSON('/tuner/bulk/apply_config', {
    method: 'POST',
    body: JSON.stringify({macs, platform, config: payload}),
    headers: {'Content-Type': 'application/json'},
  });
  if (!resp) {
    openModal('Error', '<div style="color:var(--red)">Server unreachable.</div>', [{label:'Close', action: closeModal}]);
    return;
  }
  if (resp.errors && resp.errors.length) {
    openModal('Apply Config — validation failed',
      '<ul>' + resp.errors.map(e => `<li style="color:var(--red)">${escapeHTML(e)}</li>`).join('') + '</ul>',
      [{label: 'Close', action: closeModal}]);
    return;
  }
  showBulkResults('Apply Config — results', resp);
  pollOverview();
}

// ─── Bulk: set pools on selected miners ─────────────────────────────────────
function openBulkPoolsModal(){
  if (selectedMacs.size === 0) return;
  const macs = [...selectedMacs];
  const row = (i) => `
    <div style="margin-bottom:8px;padding:8px;border:1px solid var(--border);border-radius:6px">
      <div style="color:var(--text2);font-size:0.8em;margin-bottom:4px">Pool ${i+1}${i === 0 ? ' (primary)' : ''}</div>
      <div class="form-row">
        <div><label>URL</label><input class="pool-url" data-i="${i}" type="text" placeholder="stratum+tcp://pool.example.com:3333"></div>
      </div>
      <div class="form-row">
        <div><label>Worker / Login</label><input class="pool-login" data-i="${i}" type="text" placeholder="wallet.worker"></div>
        <div><label>Password</label><input class="pool-password" data-i="${i}" type="text" placeholder="x" value="${i === 0 ? 'x' : ''}"></div>
      </div>
    </div>`;
  openModal(`Set Pools on ${macs.length} miner${macs.length === 1 ? '' : 's'}`, `
    <div style="color:var(--text2);margin-bottom:8px;font-size:0.85em">
      Up to 3 pools (first is primary). Empty rows are dropped. Miners are sent a <code>/coin</code> POST directly — the tuner itself does not persist the pool config.
    </div>
    ${row(0)}${row(1)}${row(2)}
    <div class="form-row" style="margin-top:4px">
      <div><label>Coin</label><select id="pool-coin"><option value="BTC" selected>BTC</option></select></div>
    </div>
    <div id="bulkpools-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Apply', action: () => submitBulkPools(macs)},
  ]);
}

async function submitBulkPools(macs){
  const stratums = [];
  for (let i = 0; i < 3; i++) {
    const url = document.querySelector(`.pool-url[data-i="${i}"]`)?.value?.trim();
    const login = document.querySelector(`.pool-login[data-i="${i}"]`)?.value?.trim();
    const password = document.querySelector(`.pool-password[data-i="${i}"]`)?.value?.trim();
    if (!url) continue;
    stratums.push({ pool: url, login: login || '', password: password || 'x' });
  }
  const errEl = document.getElementById('bulkpools-error');
  if (errEl) errEl.textContent = '';
  if (!stratums.length) {
    if (errEl) errEl.textContent = 'Enter at least one pool URL.';
    return;
  }
  const coin = document.getElementById('pool-coin')?.value || 'BTC';
  closeModal();
  openModal('Set Pools — in progress',
            `<div style="color:var(--text2)">Sending pool config to ${macs.length} miner(s)…</div>`, []);
  const resp = await fetchJSON('/tuner/bulk/pools', {
    method: 'POST', body: JSON.stringify({macs, stratum_configs: stratums, coin}),
    headers: {'Content-Type': 'application/json'},
  });
  if (!resp) {
    openModal('Error', '<div style="color:var(--red)">Server unreachable.</div>', [{label:'Close', action: closeModal}]);
    return;
  }
  if (resp.errors && resp.errors.length) {
    openModal('Set Pools — validation failed',
              '<ul>' + resp.errors.map(e => `<li style="color:var(--red)">${escapeHTML(e)}</li>`).join('') + '</ul>',
              [{label: 'Close', action: closeModal}]);
    return;
  }
  showBulkResults('Set Pools — results', resp);
  pollOverview();
}

// ─── Bulk: set power limit (capability-gated; ePIC is no-op) ──────────────
function openBulkSetPowerLimitModal(){
  if (selectedMacs.size === 0) return;
  const macs = [...selectedMacs];
  openModal(`Set Power Limit on ${macs.length} miner${macs.length === 1 ? '' : 's'}`, `
    <div style="color:var(--text2);margin-bottom:8px;font-size:0.85em">
      Applies a fleet-wide watts cap via each miner's vendor API. Miners on firmware
      without an external power-limit endpoint (ePIC) are reported as
      <code>capability_unsupported</code> in the results modal.
    </div>
    <div class="form-row">
      <div>
        <label>Watts (500–10000)</label>
        <input id="bulkpower-watts" type="number" min="500" max="10000" step="50" value="3500">
      </div>
    </div>
    <div id="bulkpower-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Apply', action: () => submitBulkSetPowerLimit(macs)},
  ]);
}

async function submitBulkSetPowerLimit(macs){
  const wattsRaw = document.getElementById('bulkpower-watts')?.value;
  const errEl = document.getElementById('bulkpower-error');
  if (errEl) errEl.textContent = '';
  const watts = parseInt(wattsRaw, 10);
  if (!Number.isFinite(watts) || watts < 500 || watts > 10000) {
    if (errEl) errEl.textContent = 'Watts must be an integer in [500, 10000].';
    return;
  }
  await runBulk('set_power_limit', macs, {watts});
}

// ─── Minerstat snapshot card ────────────────────────────────────────────────

let minerstatSnapshot = null;

function formatMinerstatAge(capturedAt){
  if (!capturedAt) return 'never';
  try {
    const then = new Date(capturedAt);
    const ageSec = (Date.now() - then.getTime()) / 1000;
    if (ageSec < 60) return `${Math.round(ageSec)}s ago`;
    if (ageSec < 3600) return `${Math.round(ageSec/60)}m ago`;
    if (ageSec < 86400) return `${Math.round(ageSec/3600)}h ago`;
    return `${Math.round(ageSec/86400)}d ago`;
  } catch(e){ return 'unknown'; }
}

function renderMinerstatCard(snap, pollDay, modifierPct){
  const meta = document.getElementById('minerstat-meta');
  const coinsDiv = document.getElementById('minerstat-coins');
  if (!meta || !coinsDiv) return;
  if (!snap || !snap.captured_at) {
    meta.textContent = 'No data — click "Fetch & auto-apply" to load pricing & network hashrate.';
    coinsDiv.innerHTML = '';
    return;
  }
  const capturedLocal = (()=>{
    try { return new Date(snap.captured_at).toLocaleString(); }
    catch(e){ return snap.captured_at; }
  })();
  const apiCalls = snap.api_calls_this_month || 0;
  const budgetClass = apiCalls >= 80 ? 'danger' : apiCalls >= 50 ? 'warn' : 'ok';
  const budgetColor = budgetClass === 'danger' ? '#e16969' : budgetClass === 'warn' ? '#e1b969' : '#69e189';
  const pollLine = pollDay > 0
    ? `Auto-poll day ${pollDay} of each month`
    : `Auto-poll disabled`;
  const mod = Number.isFinite(modifierPct) ? Number(modifierPct) : 0;
  const modLine = mod !== 0
    ? ` &nbsp;·&nbsp; <span style="color:${mod > 0 ? '#69e189' : '#e1b969'}" title="Revenue-side adjustment applied to all $/day calculations">Income modifier: ${mod > 0 ? '+' : ''}${mod.toFixed(2)}%</span>`
    : '';
  meta.innerHTML = `
    <span>Captured: ${escapeHTML(capturedLocal)} (${escapeHTML(formatMinerstatAge(snap.captured_at))})</span>
     &nbsp;·&nbsp;
    <span style="color:${budgetColor}">${apiCalls} API call${apiCalls === 1 ? '' : 's'} this month</span>
     &nbsp;·&nbsp;
    <span>${pollLine}</span>${modLine}`;
  const coins = snap.coins || {};
  const rows = Object.keys(coins).sort().map(coinId => {
    const c = coins[coinId];
    const price = typeof c.price_usd === 'number' ? `$${c.price_usd.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : '—';
    const hs = typeof c.network_hashrate === 'number' ? formatHashrate(c.network_hashrate) : '—';
    const algo = c.algorithm || '';
    return `<span style="margin-right:16px"><strong>${escapeHTML(coinId)}</strong> ${price} · ${hs}${algo ? ' · ' + escapeHTML(algo) : ''}</span>`;
  }).join('');
  coinsDiv.innerHTML = rows;
}

function formatHashrate(hs){
  // Accepts H/s, returns human-readable (EH/s, TH/s, etc.)
  if (!hs || hs <= 0) return '—';
  const units = [
    {v: 1e18, u: 'EH/s'}, {v: 1e15, u: 'PH/s'}, {v: 1e12, u: 'TH/s'},
    {v: 1e9, u: 'GH/s'}, {v: 1e6, u: 'MH/s'}, {v: 1e3, u: 'KH/s'},
  ];
  for (const {v, u} of units) {
    if (hs >= v) return `${(hs / v).toFixed(2)} ${u}`;
  }
  return `${hs.toFixed(0)} H/s`;
}

async function pollMinerstatCard(){
  try {
    const r = await fetch('/tuner/minerstat/snapshot');
    if (!r.ok) return;
    const d = await r.json();
    minerstatSnapshot = d.snapshot || null;
    renderMinerstatCard(minerstatSnapshot, d.poll_day, d.income_modifier_pct);
  } catch(e) {
    console.warn('Minerstat poll failed', e);
  }
}

async function minerstatFetchNow(){
  const btns = document.querySelectorAll('#minerstat-card button');
  btns.forEach(b => b.disabled = true);
  try {
    const r = await fetch('/tuner/minerstat/fetch_now', {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      minerstatSnapshot = d.snapshot;
      await pollMinerstatCard();
      const coins = Object.keys(d.snapshot?.coins || {}).length;
      const apiCalls = d.snapshot?.api_calls_this_month || 0;
      const a = d.auto_apply || {applied: 0, skipped: 0, failures: []};
      const failTail = (a.failures && a.failures.length)
        ? `\n${a.failures.length} failed:\n${a.failures.join('\n')}`
        : '';
      alert(
        `Fetched minerstat data for ${coins} coin(s). This month: ${apiCalls} API call(s).\n` +
        `Auto-apply: ${a.applied} applied, ${a.skipped} skipped${failTail}`
      );
    } else {
      alert(`Fetch failed: ${d.error}`);
    }
  } catch(e){
    alert(`Fetch errored: ${e}`);
  } finally {
    btns.forEach(b => b.disabled = false);
  }
}

// ─── IP-range helper (client-side) ──────────────────────────────────────────
function ipToInt(ip) {
  const parts = ip.split('.');
  if (parts.length !== 4) return NaN;
  return parts.reduce((acc, p) => {
    const n = parseInt(p, 10);
    return (acc * 256) + n;
  }, 0);
}

function ipInAnyRange(ip, ranges) {
  const ipInt = ipToInt(ip);
  if (isNaN(ipInt)) return false;
  for (const range of (ranges || [])) {
    try {
      if (range.includes('/')) {
        const [base, bits] = range.split('/');
        const mask = ~((1 << (32 - parseInt(bits, 10))) - 1) >>> 0;
        const netInt = ipToInt(base) >>> 0;
        if ((ipInt >>> 0 & mask) === (netInt & mask)) return true;
      } else if (range.includes('-')) {
        const [startStr, endStr] = range.split('-');
        if (ipToInt(startStr) <= ipInt && ipInt <= ipToInt(endStr)) return true;
      } else {
        if (ipToInt(range) === ipInt) return true;
      }
    } catch (_) { /* skip malformed */ }
  }
  return false;
}

// ─── Scan status helpers ─────────────────────────────────────────────────────
function renderScanStatus(st) {
  if (!st) return '<span style="color:var(--text2)">Loading…</span>';
  const state = st.state || 'idle';
  const lastRun = st.last_run_finished_at || st.last_run_started_at || '—';
  const found = (st.discovered || []).length;
  const errs = (st.errors || []).length;
  const total = st.total != null ? st.total : null;
  const progress = st.progress != null ? st.progress : null;
  let html = `State: <strong>${escapeHTML(state)}</strong> \xb7 Last run: ${escapeHTML(String(lastRun))} \xb7 Found: ${found} \xb7 Errors: ${errs}`;
  if (total != null && progress != null) {
    html += ` \xb7 Progress: ${progress}/${total}`;
  }
  if ((st.discovered || []).length > 0) {
    html += `<ul style="margin:4px 0 0 0;padding:0;list-style:none;font-size:0.85em">` +
      st.discovered.map(d => `<li>✓ ${escapeHTML(d.ip)}${d.hostname ? ' (' + escapeHTML(d.hostname) + ')' : ''}</li>`).join('') +
      `</ul>`;
  }
  return html;
}

async function refreshScanStatusInModal() {
  const target = document.getElementById('ns-scan-status');
  if (!target) {
    stopScanStatusPolling();
    return;
  }
  try {
    const st = await fetchJSON('/tuner/scanner/status');
    target.innerHTML = renderScanStatus(st);
    if (st && st.state !== 'scanning') {
      stopScanStatusPolling();
    }
  } catch (_) {}
}

function startScanStatusPolling() {
  if (scanStatusPollTimer != null) return;
  scanStatusPollTimer = setInterval(refreshScanStatusInModal, 2000);
}

function stopScanStatusPolling() {
  if (scanStatusPollTimer != null) {
    clearInterval(scanStatusPollTimer);
    scanStatusPollTimer = null;
  }
}

// ─── Network & Scanner settings modal ───────────────────────────────────────
async function openNetworkSettings() {
  const cfg = await fetchJSON('/tuner/config');
  // Fleet-ops keys now live in cfg.fleet_ops (v3 schema). Fall back to
  // cfg.defaults for servers that haven't migrated to v3 yet.
  const defaults = (cfg && cfg.fleet_ops) || (cfg && cfg.defaults) || {};
  const scanRanges = Array.isArray(defaults.SCAN_IP_RANGES) ? defaults.SCAN_IP_RANGES.join('\n') : '';
  const scanBlacklist = Array.isArray(defaults.SCAN_IP_BLACKLIST) ? defaults.SCAN_IP_BLACKLIST.join('\n') : '';
  const scanPasswords = Array.isArray(defaults.SCAN_PASSWORDS) ? defaults.SCAN_PASSWORDS.join('\n') : 'letmein';
  const scanInterval = defaults.SCAN_INTERVAL_MIN != null ? defaults.SCAN_INTERVAL_MIN : 30;
  const scanTimeout = defaults.SCAN_TIMEOUT_SEC != null ? defaults.SCAN_TIMEOUT_SEC : 2.0;
  const scanConcurrency = defaults.SCAN_CONCURRENCY != null ? defaults.SCAN_CONCURRENCY : 1024;
  const scanAuto = defaults.SCAN_AUTO_REGISTER !== false;
  const apiPort = defaults.API_PORT != null ? defaults.API_PORT : 4028;
  const sourceIP = defaults.SOURCE_IP || '';

  // Fetch last scan status
  let scanStatusHTML = '<span style="color:var(--text2)">Loading…</span>';
  try {
    const st = await fetchJSON('/tuner/scanner/status');
    if (st) {
      scanStatusHTML = renderScanStatus(st);
      if (st.state === 'scanning') {
        // Will start polling after openModal completes.
        setTimeout(startScanStatusPolling, 100);
      }
    }
  } catch (_) {}

  openModal('Network &amp; Scanner Settings', `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.85em">
      Fleet-wide — scanner discovers supported miners on configured IP ranges.
    </div>
    <details open style="margin-bottom:8px">
      <summary style="cursor:pointer;font-weight:bold">Scanner</summary>
      <div style="margin-top:8px">
        <label for="ns-ranges">IP Ranges <span style="color:var(--text2);font-size:0.85em">— one CIDR or dash-range per line (e.g. 192.0.2.0/24)</span></label>
        <textarea id="ns-ranges" rows="4" style="width:100%;font-family:monospace;font-size:0.9em">${escapeHTML(scanRanges)}</textarea>
      </div>
      <div style="margin-top:8px">
        <label for="ns-blacklist">IP Blacklist <span style="color:var(--text2);font-size:0.85em">— one CIDR, dash-range, or single IP per line; these are skipped during scanning</span></label>
        <textarea id="ns-blacklist" rows="3" style="width:100%;font-family:monospace;font-size:0.9em" placeholder="(empty)">${escapeHTML(scanBlacklist)}</textarea>
      </div>
      <div style="margin-top:8px">
        <label for="ns-passwords">Passwords to try <span style="color:var(--text2);font-size:0.85em">— one per line, tried in order</span></label>
        <textarea id="ns-passwords" rows="3" style="width:100%;font-family:monospace;font-size:0.9em">${escapeHTML(scanPasswords)}</textarea>
      </div>
      <div style="margin-top:8px;display:flex;gap:16px;flex-wrap:wrap">
        <div><label for="ns-interval">Scan interval (min)</label>
          <input id="ns-interval" type="number" min="0" max="525600" value="${scanInterval}" style="width:80px">
        </div>
        <div><label for="ns-timeout">Probe timeout (sec)</label>
          <input id="ns-timeout" type="number" min="0.5" max="30" step="0.5" value="${scanTimeout}" style="width:80px">
        </div>
        <div><label for="ns-concurrency">Concurrency</label>
          <input id="ns-concurrency" type="number" min="1" max="1024" value="${scanConcurrency}" style="width:80px">
        </div>
      </div>
      <div style="margin-top:8px">
        <label><input id="ns-auto" type="checkbox" ${scanAuto ? 'checked' : ''}> Auto-register discovered miners</label>
      </div>
    </details>
    <details style="margin-bottom:8px">
      <summary style="cursor:pointer;font-weight:bold">Connection</summary>
      <div style="margin-top:8px;display:flex;gap:16px;flex-wrap:wrap">
        <div><label for="ns-port">API Port</label>
          <input id="ns-port" type="number" min="1" max="65535" value="${apiPort}" style="width:80px">
        </div>
        <div style="flex:1"><label for="ns-source">Source IP <span style="color:var(--text2);font-size:0.85em">— blank = auto</span></label>
          <input id="ns-source" type="text" placeholder="auto-detect" value="${escapeHTML(sourceIP)}" style="width:160px">
        </div>
      </div>
    </details>
    <details>
      <summary style="cursor:pointer;font-weight:bold">Last Scan Results</summary>
      <div id="ns-scan-status" style="margin-top:8px;font-size:0.88em">${scanStatusHTML}</div>
      <button class="secondary" style="margin-top:8px" data-action="scanNowFromModal">Scan now</button>
    </details>
    <div id="ns-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label: 'Cancel', action: closeModal},
    {label: 'Save', action: submitNetworkSettings},
  ]);
}

async function scanNowFromModal() {
  const st = await fetchJSON('/tuner/scanner/scan_now', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const target = document.getElementById('ns-scan-status');
  if (target) {
    target.innerHTML = st && st.ok ? '<span style="color:var(--green)">Scan triggered — refreshing status…</span>' : '<span style="color:var(--red)">Trigger failed.</span>';
  }
  if (st && st.ok) {
    startScanStatusPolling();
  }
}

async function submitNetworkSettings() {
  const errEl = document.getElementById('ns-error');
  if (errEl) errEl.textContent = '';
  const payload = {};

  const rangesEl = document.getElementById('ns-ranges');
  if (rangesEl) {
    payload.SCAN_IP_RANGES = rangesEl.value.split('\n').map(s => s.trim()).filter(Boolean);
  }
  const blacklistEl = document.getElementById('ns-blacklist');
  if (blacklistEl) {
    payload.SCAN_IP_BLACKLIST = blacklistEl.value.split('\n').map(s => s.trim()).filter(Boolean);
  }
  const pwdsEl = document.getElementById('ns-passwords');
  if (pwdsEl) {
    payload.SCAN_PASSWORDS = pwdsEl.value.split('\n').map(s => s.trim()).filter(Boolean);
  }
  const intervalEl = document.getElementById('ns-interval');
  if (intervalEl) {
    const n = parseInt(intervalEl.value, 10);
    if (!Number.isFinite(n) || n < 0 || n > 525600) {
      if (errEl) errEl.textContent = 'Scan interval must be 0-525600';
      return;
    }
    payload.SCAN_INTERVAL_MIN = n;
  }
  const timeoutEl = document.getElementById('ns-timeout');
  if (timeoutEl) {
    const n = parseFloat(timeoutEl.value);
    if (!Number.isFinite(n) || n < 0.5 || n > 30) {
      if (errEl) errEl.textContent = 'Probe timeout must be 0.5-30';
      return;
    }
    payload.SCAN_TIMEOUT_SEC = n;
  }
  const concEl = document.getElementById('ns-concurrency');
  if (concEl) {
    const n = parseInt(concEl.value, 10);
    if (!Number.isFinite(n) || n < 1 || n > 1024) {
      if (errEl) errEl.textContent = 'Concurrency must be 1-1024';
      return;
    }
    payload.SCAN_CONCURRENCY = n;
  }
  const autoEl = document.getElementById('ns-auto');
  if (autoEl) payload.SCAN_AUTO_REGISTER = !!autoEl.checked;
  const portEl = document.getElementById('ns-port');
  if (portEl) {
    const n = parseInt(portEl.value, 10);
    if (Number.isFinite(n) && n >= 1 && n <= 65535) payload.API_PORT = n;
  }
  const sourceEl = document.getElementById('ns-source');
  if (sourceEl) payload.SOURCE_IP = sourceEl.value.trim();

  // Fleet-ops keys POST to /tuner/config/fleet_ops (v3 schema).
  const r = await fetchJSON('/tuner/config/fleet_ops', {
    method: 'POST',
    body: JSON.stringify(payload),
    headers: {'Content-Type': 'application/json'},
  });
  if (r && r.updated) {
    closeModal();
  } else if (r && r.errors && r.errors.length) {
    if (errEl) errEl.textContent = r.errors.join('; ');
  } else if (errEl) {
    errEl.textContent = 'Save failed (server error)';
  }
}

// Fleet-wide minerstat settings. Editable only on the overview via the
// Minerstat card's gear button — these are not per-miner overridable because
// a single shared snapshot is the whole point (one API call fetches data for
// the fleet's coin rather than N calls racing each other through the rate
// budget). API key is never echoed back; leaving the field blank preserves
// the currently saved value.
async function openMinerstatSettings(){
  const cfg = await fetchJSON('/tuner/config');
  // Fleet-ops keys now live in cfg.fleet_ops (v3 schema). Fall back to
  // cfg.defaults for servers that haven't migrated to v3 yet.
  const defaults = (cfg && cfg.fleet_ops) || (cfg && cfg.defaults) || {};
  const coin = escapeHTML(defaults.MINERSTAT_COIN || 'BTC');
  const pollDay = Number.isFinite(defaults.MINERSTAT_POLL_DAY) ? defaults.MINERSTAT_POLL_DAY : 0;
  const hasKey = !!(defaults.MINERSTAT_API_KEY);
  const modifier = Number.isFinite(defaults.INCOME_MODIFIER_PCT) ? defaults.INCOME_MODIFIER_PCT : 0;
  openModal('Minerstat Settings', `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.85em">
      Fleet-wide — one shared snapshot for every miner in profit mode.
    </div>
    <div><label for="ms-coin">Coin</label>
      <input id="ms-coin" type="text" value="${coin}" placeholder="BTC">
    </div>
    <div style="margin-top:8px"><label for="ms-poll">Auto-poll Day (1-28, 0 = off)</label>
      <input id="ms-poll" type="number" min="0" max="28" value="${pollDay}">
    </div>
    <div style="margin-top:8px"><label for="ms-key">API Key ${hasKey ? '<span style="color:var(--text2);font-size:0.85em">— saved; leave blank to keep</span>' : ''}</label>
      <input id="ms-key" type="password" autocomplete="new-password" placeholder="${hasKey ? 'keep current' : 'paste minerstat API key'}">
    </div>
    <div style="margin-top:8px"><label for="ms-modifier">Income Modifier (%) <span style="color:var(--text2);font-size:0.85em">— e.g. +9.5 for rental premium, -5 for pool fees</span></label>
      <input id="ms-modifier" type="number" step="0.1" min="-100" max="100" value="${modifier}">
    </div>
    <div id="ms-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label:'Cancel', action: closeModal},
    {label:'Save', action: submitMinerstatSettings},
  ]);
}

async function submitMinerstatSettings(){
  const errEl = document.getElementById('ms-error');
  if (errEl) errEl.textContent = '';
  const payload = {};
  const coinEl = document.getElementById('ms-coin');
  const pollEl = document.getElementById('ms-poll');
  const keyEl = document.getElementById('ms-key');
  const modEl = document.getElementById('ms-modifier');
  if (coinEl) {
    const v = coinEl.value.trim().toUpperCase();
    if (!v) { if (errEl) errEl.textContent = 'Coin is required'; return; }
    payload.MINERSTAT_COIN = v;
  }
  if (pollEl) {
    const n = parseInt(pollEl.value, 10);
    if (!Number.isFinite(n) || n < 0 || n > 28) {
      if (errEl) errEl.textContent = 'Auto-poll day must be 0-28'; return;
    }
    payload.MINERSTAT_POLL_DAY = n;
  }
  // API key: only send if the user typed something. Empty field = keep current.
  if (keyEl && keyEl.value) payload.MINERSTAT_API_KEY = keyEl.value;
  if (modEl) {
    const m = parseFloat(modEl.value);
    if (!Number.isFinite(m) || m < -100 || m > 100) {
      if (errEl) errEl.textContent = 'Income modifier must be between -100 and 100'; return;
    }
    payload.INCOME_MODIFIER_PCT = m;
  }
  // Fleet-ops keys POST to /tuner/config/fleet_ops (v3 schema).
  const r = await fetchJSON('/tuner/config/fleet_ops', {
    method:'POST', body: JSON.stringify(payload), headers:{'Content-Type':'application/json'}
  });
  if (r && r.updated) {
    closeModal();
    pollMinerstatCard();
  } else if (r && r.errors && r.errors.length) {
    if (errEl) errEl.textContent = r.errors.join('; ');
  } else if (errEl) {
    errEl.textContent = 'Save failed (server error)';
  }
}

// ─── MiningRigRentals card ───────────────────────────────────────────────────
//
// Fleet-wide auto-publish: while a miner is tuning, its MRR rig is flipped to
// "disabled"; on Phase 6 entry the rig goes "enabled" with advertised hashrate =
// sweep_hashrate_ths × (1 + MRR_HASHRATE_MODIFIER_PCT/100). Credentials,
// enable toggle, default modifier, and hashrate unit live on this card's
// Settings modal. Per-miner rig ID + modifier override live on the detail view.

let mrrCachedDefaults = null;

function renderMRRCard(defaults){
  const meta = document.getElementById('mrr-meta');
  if (!meta) return;
  if (!defaults) {
    meta.textContent = 'Loading…';
    return;
  }
  const enabled = !!defaults.MRR_ENABLED;
  const hasKey = !!(defaults.MRR_API_KEY);
  const hasSecret = !!(defaults.MRR_API_SECRET);
  const hasUsername = !!(defaults.MRR_STRATUM_USERNAME);
  const modifier = Number.isFinite(defaults.MRR_HASHRATE_MODIFIER_PCT)
    ? defaults.MRR_HASHRATE_MODIFIER_PCT : 0;
  const unit = (defaults.MRR_HASHRATE_UNIT || 'th').toUpperCase();
  const coin = (defaults.MRR_COIN || 'BTC').toUpperCase();
  if (!enabled) {
    const credState = (hasKey && hasSecret && hasUsername)
      ? 'credentials saved'
      : 'credentials incomplete';
    meta.innerHTML = `<span style="color:var(--text2)">Disabled</span> · ${credState}`;
    return;
  }
  const missing = [];
  if (!hasKey) missing.push('API key');
  if (!hasSecret) missing.push('API secret');
  if (!hasUsername) missing.push('stratum username');
  if (missing.length) {
    meta.innerHTML = `<span style="color:#e16969">Enabled but ${missing.join(', ')} missing</span> — auto-sync will be skipped`;
    return;
  }
  const modNote = modifier !== 0
    ? ` · <span style="color:${modifier > 0 ? '#69e189' : '#e1b969'}" title="Applied to sweep_hashrate_ths before advertising to MRR">Default modifier: ${modifier > 0 ? '+' : ''}${modifier.toFixed(2)}%</span>`
    : '';
  meta.innerHTML = `<span style="color:#69e189">Enabled</span> · ${escapeHTML(coin)} · advertising in ${escapeHTML(unit)}/s${modNote} · per-miner rig ID on detail view`;
}

async function pollMRRCard(){
  try {
    const cfg = await fetchJSON('/tuner/config');
    // Fleet-ops keys (MRR_ENABLED, MRR_API_KEY, etc.) live in cfg.fleet_ops.
    // MRR_HASHRATE_MODIFIER_PCT is a per-platform key — not in fleet_ops.
    // The MRR fleet card sets all 4 platforms identically; read from ePIC
    // as the canonical fleet-card display value.
    const fleetOps = (cfg && cfg.fleet_ops) || (cfg && cfg.defaults) || {};
    const epicDefaults = (cfg && cfg.defaults && cfg.defaults.epic) || {};
    mrrCachedDefaults = Object.assign({}, fleetOps, {
      MRR_HASHRATE_MODIFIER_PCT: epicDefaults.MRR_HASHRATE_MODIFIER_PCT,
    });
    renderMRRCard(mrrCachedDefaults);
  } catch(e) {
    console.warn('MRR card poll failed', e);
  }
}

async function openMRRSettings(){
  // Always refetch so stale values don't mask a recent save.
  const cfg = await fetchJSON('/tuner/config');
  // Fleet-ops keys (MRR_ENABLED, MRR_API_KEY, etc.) live in cfg.fleet_ops.
  // MRR_HASHRATE_MODIFIER_PCT is a per-platform key — not in fleet_ops.
  // The MRR fleet card sets all 4 platforms identically; read from ePIC
  // as the canonical fleet-card display value.
  const fleetOps = (cfg && cfg.fleet_ops) || (cfg && cfg.defaults) || {};
  const epicDefaults = (cfg && cfg.defaults && cfg.defaults.epic) || {};
  const defaults = Object.assign({}, fleetOps, {
    MRR_HASHRATE_MODIFIER_PCT: epicDefaults.MRR_HASHRATE_MODIFIER_PCT,
  });
  mrrCachedDefaults = defaults;
  const enabled = !!defaults.MRR_ENABLED;
  const hasKey = !!(defaults.MRR_API_KEY);
  const hasSecret = !!(defaults.MRR_API_SECRET);
  const username = defaults.MRR_STRATUM_USERNAME || '';
  const modifier = Number.isFinite(defaults.MRR_HASHRATE_MODIFIER_PCT)
    ? defaults.MRR_HASHRATE_MODIFIER_PCT : 0;
  const unit = (defaults.MRR_HASHRATE_UNIT || 'th');
  const coin = (defaults.MRR_COIN || 'BTC').toUpperCase();
  const unitOptions = ['hash','kh','mh','gh','th','ph','eh'].map(u =>
    `<option value="${u}"${u === unit ? ' selected' : ''}>${u.toUpperCase()}/s</option>`
  ).join('');
  const coinOptions = ['BTC','LTC'].map(c =>
    `<option value="${c}"${c === coin ? ' selected' : ''}>${c}</option>`
  ).join('');
  openModal('MiningRigRentals Settings', `
    <div style="color:var(--text2);margin-bottom:10px;font-size:0.85em">
      Fleet-wide credentials + auto-publish toggle. Per-miner rig ID and
      modifier override live on each miner's detail view.
    </div>
    <div><label><input id="mrr-enabled" type="checkbox" ${enabled ? 'checked' : ''}> Enable auto-publish</label>
      <div style="color:var(--text2);font-size:0.8em;margin-top:2px">When enabled: tuner flips rig status on tune / Phase 6 transitions AND auto-configures the 3 MRR stratum pools on each miner at Phase 0 start.</div>
    </div>
    <div style="margin-top:8px"><label for="mrr-key">API Key ${hasKey ? '<span style="color:var(--text2);font-size:0.85em">— saved; leave blank to keep</span>' : ''}</label>
      <input id="mrr-key" type="text" placeholder="${hasKey ? 'keep current' : 'paste MRR API key'}">
    </div>
    <div style="margin-top:8px"><label for="mrr-secret">API Secret ${hasSecret ? '<span style="color:var(--text2);font-size:0.85em">— saved; leave blank to keep</span>' : ''}</label>
      <input id="mrr-secret" type="password" autocomplete="new-password" placeholder="${hasSecret ? 'keep current' : 'paste MRR API secret'}">
    </div>
    <div style="margin-top:8px"><label for="mrr-username">Stratum Username <span style="color:var(--text2);font-size:0.85em">— your MRR account username; feeds stratum login '{username}.{rig_id}'</span></label>
      <input id="mrr-username" type="text" value="${escapeHTML(String(username))}" placeholder="your MRR username">
    </div>
    <div style="margin-top:8px"><label for="mrr-coin">Mining Coin <span style="color:var(--text2);font-size:0.85em">— firmware /coin field; depends on miner model</span></label>
      <select id="mrr-coin">${coinOptions}</select>
    </div>
    <div style="margin-top:8px"><label for="mrr-modifier">Default Hashrate Modifier (%) <span style="color:var(--text2);font-size:0.85em">— e.g. +2 to advertise 102% of sweep hashrate</span></label>
      <input id="mrr-modifier" type="number" step="0.1" min="-50" max="50" value="${modifier}">
    </div>
    <div style="margin-top:8px"><label for="mrr-unit">Hashrate Unit <span style="color:var(--text2);font-size:0.85em">— depends on miner model</span></label>
      <select id="mrr-unit">${unitOptions}</select>
    </div>
    <div style="margin-top:12px;padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;font-size:0.8em">
      <div style="color:var(--text2);margin-bottom:4px">Auto-applied to each configured miner at Phase 0:</div>
      <div style="font-family:monospace;color:var(--text)">
        stratum+tcp://us-east01.miningrigrentals.com:3311#xnsub<br>
        stratum+tcp://us-central01.miningrigrentals.com:3311#xnsub<br>
        stratum+tcp://us-west01.miningrigrentals.com:3311#xnsub
      </div>
      <div style="color:var(--text2);margin-top:4px">Worker Unique ID: <strong>off</strong> · Password: <code>x</code></div>
    </div>
    <div id="mrr-error" style="color:var(--red);font-size:0.85em;margin-top:8px;min-height:1em"></div>
  `, [
    {label:'Cancel', action: closeModal},
    {label:'Save', action: submitMRRSettings},
  ]);
}

async function submitMRRSettings(){
  const errEl = document.getElementById('mrr-error');
  if (errEl) errEl.textContent = '';
  // Fleet-ops subset: keys that live in state.CONFIG["fleet_ops"].
  const fleetOpsPayload = {};
  const enabledEl = document.getElementById('mrr-enabled');
  const keyEl = document.getElementById('mrr-key');
  const secretEl = document.getElementById('mrr-secret');
  const userEl = document.getElementById('mrr-username');
  const coinEl = document.getElementById('mrr-coin');
  const unitEl = document.getElementById('mrr-unit');
  if (enabledEl) fleetOpsPayload.MRR_ENABLED = !!enabledEl.checked;
  if (keyEl && keyEl.value) fleetOpsPayload.MRR_API_KEY = keyEl.value.trim();
  if (secretEl && secretEl.value) fleetOpsPayload.MRR_API_SECRET = secretEl.value.trim();
  if (userEl) fleetOpsPayload.MRR_STRATUM_USERNAME = userEl.value.trim();
  if (coinEl) fleetOpsPayload.MRR_COIN = coinEl.value;
  if (unitEl) fleetOpsPayload.MRR_HASHRATE_UNIT = unitEl.value;
  // Per-platform subset: MRR_HASHRATE_MODIFIER_PCT lives in
  // state.CONFIG["defaults"][platform], NOT in fleet_ops. The MRR fleet card
  // sets all 4 platforms identically — read from ePIC as the canonical value.
  const modEl = document.getElementById('mrr-modifier');
  let modifier;
  if (modEl) {
    const m = parseFloat(modEl.value);
    if (!Number.isFinite(m) || m < -50 || m > 50) {
      if (errEl) errEl.textContent = 'Modifier must be between -50 and 50';
      return;
    }
    modifier = m;
  }
  // Step 1: POST fleet-ops keys to /tuner/config/fleet_ops.
  const r1 = await fetchJSON('/tuner/config/fleet_ops', {
    method: 'POST', body: JSON.stringify(fleetOpsPayload),
    headers: {'Content-Type': 'application/json'},
  });
  if (!r1 || !r1.updated) {
    if (errEl) errEl.textContent = (r1 && r1.errors && r1.errors.join('; ')) || 'Save failed (server error)';
    return;
  }
  // Step 2: POST MRR_HASHRATE_MODIFIER_PCT to each platform's defaults bucket.
  // Applying to all 4 platforms preserves the pre-v3 UX (single fleet-wide value).
  if (modifier !== undefined) {
    for (const platform of ['epic', 'bixbit', 'luxos', 'braiins', 'whatsminer']) {
      const r2 = await fetchJSON('/tuner/config/defaults', {
        method: 'POST',
        body: JSON.stringify({platform, defaults: {MRR_HASHRATE_MODIFIER_PCT: modifier}}),
        headers: {'Content-Type': 'application/json'},
      });
      if (!r2 || !r2.updated) {
        if (errEl) errEl.textContent = `Modifier save failed for ${platform}: ${(r2 && r2.errors && r2.errors.join('; ')) || 'unknown'}`;
        return;
      }
    }
  }
  closeModal();
  pollMRRCard();
}

async function mrrTestConnection(){
  const btns = document.querySelectorAll('#mrr-card button');
  btns.forEach(b => b.disabled = true);
  try {
    const r = await fetch('/tuner/mrr/whoami');
    const d = await r.json();
    if (d.ok && d.data) {
      const perms = d.data.permissions || {};
      const rigsOk = perms.rigs === 'yes' || perms.rigs === true;
      const rentOk = perms.rent === 'yes' || perms.rent === true;
      const userId = d.data.userid || '(unknown)';
      const warn = !rigsOk ? '\n\n⚠ Your API key LACKS the "rigs" permission. Auto-publish will fail. Re-generate the key with rigs enabled.' : '';
      alert(`Connected to MRR.\n\nUser ID: ${userId}\nPerms: rigs=${perms.rigs || '?'}, rent=${perms.rent || '?'}, withdraw=${perms.withdraw || '?'}${warn}`);
    } else {
      alert(`Test failed: ${d.error || 'unknown error'}`);
    }
  } catch(e){
    alert(`Test errored: ${e}`);
  } finally {
    btns.forEach(b => b.disabled = false);
  }
}

// ─── MRR detail-view helpers ─────────────────────────────────────────────────

function _mrrAgeShort(ts){
  if (!ts) return 'never';
  const ageSec = (Date.now() / 1000) - Number(ts);
  if (!Number.isFinite(ageSec) || ageSec < 0) return 'just now';
  if (ageSec < 60) return `${Math.round(ageSec)}s ago`;
  if (ageSec < 3600) return `${Math.round(ageSec/60)}m ago`;
  if (ageSec < 86400) return `${Math.round(ageSec/3600)}h ago`;
  return `${Math.round(ageSec/86400)}d ago`;
}

function renderMRRStatusLine(status){
  const row = document.getElementById('s-mrr-row');
  const val = document.getElementById('s-mrr');
  const actions = document.getElementById('s-mrr-actions');
  if (!row || !val || !actions) return;
  const cfg = (status && status.config) || {};
  const rigId = Number(cfg.mrr_rig_id) || 0;
  // Hide the row entirely when this miner has no rig mapping — no point
  // showing "Not configured" next to every real stat for most operators.
  if (rigId <= 0) {
    row.style.display = 'none';
    actions.style.display = 'none';
    return;
  }
  row.style.display = 'flex';
  actions.style.display = 'block';
  const fleetEnabled = !!cfg.mrr_enabled;
  const last = status && status.mrr_last_sync;
  const bits = [];
  bits.push(`Rig <strong>#${rigId}</strong>`);
  if (!fleetEnabled) {
    bits.push(`<span style="color:var(--text2)">fleet auto-publish off</span>`);
  }
  if (!last) {
    bits.push(`<span style="color:var(--text2)">never synced</span>`);
    val.innerHTML = bits.join(' · ');
    return;
  }
  const result = last.result || 'unknown';
  const ts = last.ts;
  const age = _mrrAgeShort(ts);
  let resultLabel = '';
  if (result === 'ok') {
    const status_ = last.target_status || '—';
    const color = status_ === 'enabled' ? '#69e189' : '#e1b969';
    let rate = '';
    if (last.advertised_ths != null) {
      const unit = (last.advertised_unit || 'th').toUpperCase();
      rate = ` @ ${Number(last.advertised_ths).toFixed(2)} ${unit}/s`;
    }
    resultLabel = `<span style="color:${color}">${status_}</span>${rate}`;
  } else if (result === 'skipped_rented') {
    resultLabel = `<span style="color:#e1b969">skipped — rig rented</span>`;
  } else if (result === 'skipped') {
    const why = last.error ? ` (${escapeHTML(String(last.error))})` : '';
    resultLabel = `<span style="color:var(--text2)">skipped${why}</span>`;
  } else if (result === 'error') {
    const why = last.error ? ` — ${escapeHTML(String(last.error)).slice(0,80)}` : '';
    resultLabel = `<span style="color:#e16969">error${why}</span>`;
  } else {
    resultLabel = `<span style="color:var(--text2)">${escapeHTML(result)}</span>`;
  }
  bits.push(resultLabel);
  bits.push(`<span style="color:var(--text2)" title="${escapeHTML(last.reason || '')}">${age}</span>`);
  val.innerHTML = bits.join(' · ');
}

async function mrrResyncNow(){
  if (!currentMac()) return;
  const btn = document.querySelector('#s-mrr-actions button');
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
  try {
    const r = await fetch('/tuner/mrr/resync', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac: currentMac()}),
    });
    const d = await r.json();
    if (d.ok) {
      // Next status poll picks up the new mrr_last_sync; nudge it by calling
      // pollDetail immediately so the user sees the new state without waiting.
      if (typeof pollDetail === 'function') pollDetail();
    } else {
      alert(`Resync failed: ${d.error || 'unknown error'}`);
    }
  } catch(e){
    alert(`Resync errored: ${e}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Resync MRR'; }
  }
}

// Target input id for the next _mrrPickerSelect dispatch. Default targets the
// per-miner config tab; the fleet-pill action popup overrides it before opening
// the picker so the click writes back to the popup's input instead.
let _mrrPickerTargetInputId = 'cfg-MRR_RIG_ID';
// Optional callback fired after _mrrPickerSelect closes the picker modal — used
// by the fleet-pill popup to re-open itself with the new rig pre-filled.
let _mrrPickerOnClose = null;

async function openMRRRigPicker(targetInputId, onClose){
  // Populate a rig-ID input by picking from the operator's MRR rig list.
  // Convenience helper so operators don't have to copy-paste rig IDs from the
  // MRR dashboard. Default target is the per-miner config tab; callers can
  // pass a different input id (e.g. the fleet-pill popup's own input).
  _mrrPickerTargetInputId = targetInputId || 'cfg-MRR_RIG_ID';
  _mrrPickerOnClose = (typeof onClose === 'function') ? onClose : null;
  openModal('Pick from my MRR rigs',
    '<div id="mrr-picker-body" style="color:var(--text2)">Loading rigs…</div>', [
      {label: 'Cancel', action: () => { const cb = _mrrPickerOnClose; _mrrPickerOnClose = null; closeModal(); if (cb) cb(null); }},
    ]);
  let rigs = [];
  try {
    const r = await fetch('/tuner/mrr/rigs');
    const d = await r.json();
    if (!d.ok) {
      const body = document.getElementById('mrr-picker-body');
      if (body) body.innerHTML = `<span style="color:var(--red)">${escapeHTML(d.error || 'failed to fetch')}</span>`;
      return;
    }
    rigs = d.rigs || [];
  } catch(e){
    const body = document.getElementById('mrr-picker-body');
    if (body) body.innerHTML = `<span style="color:var(--red)">Error: ${escapeHTML(String(e))}</span>`;
    return;
  }
  const body = document.getElementById('mrr-picker-body');
  if (!body) return;
  if (!rigs.length) {
    body.innerHTML = '<em>No rigs found on your MRR account.</em>';
    return;
  }
  const rows = rigs.map(r => {
    const parsedId = Number(r.id || r.rigid || 0);
    const id = Number.isSafeInteger(parsedId) && parsedId > 0 ? parsedId : 0;
    const name = r.name || '(unnamed)';
    const type = r.type || r.algo || '';
    const hashRaw = (r.hash && r.hash.hash) || '';
    const hashType = (r.hash && r.hash.type) || '';
    const hashPretty = hashRaw ? `${hashRaw} ${hashType.toUpperCase()}/s` : '';
    const status = (r.status && (r.status.status || r.status)) || r.available_status || '';
    return `<tr style="cursor:pointer" data-action="_mrrPickerSelect" data-arg-id="${id}">
      <td style="padding:4px 8px"><strong>#${id}</strong></td>
      <td style="padding:4px 8px">${escapeHTML(String(name))}</td>
      <td style="padding:4px 8px;color:var(--text2)">${escapeHTML(String(type))}</td>
      <td style="padding:4px 8px;color:var(--text2)">${escapeHTML(String(hashPretty))}</td>
      <td style="padding:4px 8px;color:var(--text2)">${escapeHTML(String(status))}</td>
    </tr>`;
  }).join('');
  body.innerHTML = `
    <div style="color:var(--text2);margin-bottom:8px;font-size:0.85em">Click a rig to assign its ID to this miner.</div>
    <table style="width:100%;border-collapse:collapse;font-size:0.88em">
      <thead><tr style="text-align:left;border-bottom:1px solid var(--border)">
        <th style="padding:4px 8px">ID</th>
        <th style="padding:4px 8px">Name</th>
        <th style="padding:4px 8px">Algo</th>
        <th style="padding:4px 8px">Advertised</th>
        <th style="padding:4px 8px">Status</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function _mrrPickerSelect(rigId){
  // Picker target id is set by openMRRRigPicker — defaults to the per-miner
  // config tab field. When a different target is set (e.g. by the fleet-pill
  // popup), the value goes there instead.
  const targetId = _mrrPickerTargetInputId || 'cfg-MRR_RIG_ID';
  const input = document.getElementById(targetId);
  if (input) {
    input.value = String(rigId);
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }
  const cb = _mrrPickerOnClose;
  _mrrPickerOnClose = null;
  _mrrPickerTargetInputId = 'cfg-MRR_RIG_ID';
  closeModal();
  if (cb) cb(rigId);
}

// ─── MRR fleet-pill action popup ─────────────────────────────────────────────
// Opens from a click on the fleet-table MRR pill (or the detail-page pill).
// Surfaces three actions without forcing the operator into the per-miner config
// tab: change/clear the rig ID, open the rig page on MRR, and toggle the
// fleet-wide MRR_ENABLED knob (the gear-modal switch). The popup re-fetches
// /tuner/config on open so it's always in sync with the latest defaults +
// per-miner overrides.
let _mrrPillModalState = null;

async function openMrrPillModal(mac, pendingRigId){
  if (!mac) return;
  // Fetch latest config — pulls fleet defaults (incl. MRR_ENABLED) AND the
  // per-miner override slice for this MAC. Same endpoint openMRRSettings uses.
  //
  // `pendingRigId` is set when the popup is being re-opened after the rig
  // picker fired (the picker replaces the modal DOM, so the picker's click
  // handler can't write to the popup's input directly). Treating it as an
  // unsaved override keeps the picked value visible across the re-render —
  // input, "Open rig page" link, and the state pointer all reflect it until
  // the operator clicks Save.
  let cfg;
  try {
    cfg = await fetchJSON('/tuner/config');
  } catch (e) {
    openModal('MRR — Error',
      `<div style="color:var(--red)">Failed to load config: ${escapeHTML(String(e))}</div>`,
      [{label:'Close', action: closeModal}]);
    return;
  }
  // Fleet-ops keys live in cfg.fleet_ops (v3+).
  const defaults = (cfg && cfg.fleet_ops) || (cfg && cfg.defaults) || {};
  // v4: cfg.miner_configs is keyed by MAC. The MRR_RIG_ID override is at the
  // top level of the v4 entry (cross-platform); fall back to the platforms
  // bucket for entries created via the bulk-apply endpoint pre-A12.
  const minerEntry = (cfg && cfg.miner_configs && cfg.miner_configs[mac]) || {};
  const platforms = minerEntry.platforms || {};
  const fwBucket =
    platforms[minerEntry.current_firmware] || platforms[minerEntry.firmware_type] || {};
  const overrides = {...fwBucket, ...minerEntry};
  const fleetEnabled = !!defaults.MRR_ENABLED;
  const hasKey = !!defaults.MRR_API_KEY;
  const hasSecret = !!defaults.MRR_API_SECRET;
  const hasUser = !!defaults.MRR_STRATUM_USERNAME;
  const credsOk = hasKey && hasSecret && hasUser;
  // Per-miner override wins; fall back to fleet default. 0 = unset.
  // pendingRigId (from the picker callback) takes precedence over both.
  const savedRigId = Number(
    (overrides.MRR_RIG_ID !== undefined ? overrides.MRR_RIG_ID : defaults.MRR_RIG_ID) || 0,
  );
  const hasPending = (pendingRigId != null && Number.isFinite(Number(pendingRigId)));
  const currentRigId = hasPending ? Number(pendingRigId) : savedRigId;
  const isUnsaved = hasPending && currentRigId !== savedRigId;
  // Latest rental status for this MAC (drives the status banner inside the modal).
  const row = (overviewData && overviewData.miners || []).find(m => m.mac === mac);
  const ip = (row && row.ip) || minerEntry.ip || '';
  const rental = row ? row.mrr_rental_status : null;
  _mrrPillModalState = {mac, ip, currentRigId, fleetEnabled, credsOk};

  let statusBanner;
  if (!rental) {
    statusBanner = `<span style="color:var(--text2)">No rig configured</span>`;
  } else if (rental.state === 'disabled') {
    statusBanner = `<span style="color:var(--text2)">MRR off (fleet)</span>`;
  } else if (rental.state === 'no_creds') {
    statusBanner = `<span style="color:var(--yellow,#e1b969)">MRR credentials missing</span>`;
  } else if (rental.error) {
    statusBanner = `<span style="color:var(--yellow,#e1b969)">Error: ${escapeHTML(String(rental.error))}</span>`;
  } else if (rental.rented === true) {
    statusBanner = `<span style="color:var(--green,#4caf50);font-weight:600">Currently rented</span>`;
  } else if (rental.rented === false) {
    statusBanner = `<span style="color:var(--text)">Available</span>`;
  } else {
    statusBanner = `<span style="color:var(--text2)">—</span>`;
  }

  const rigUrl = currentRigId > 0
    ? `https://www.miningrigrentals.com/rigs/${encodeURIComponent(String(currentRigId))}`
    : '';
  const openRigBtn = currentRigId > 0
    ? `<a class="secondary" href="${rigUrl}" target="_blank" rel="noopener" style="display:inline-block;padding:4px 10px;font-size:0.78em;background:var(--bg3);color:var(--text);border:1px solid var(--border);border-radius:4px;text-decoration:none;margin-left:8px">Open rig page ↗</a>`
    : '';

  let fleetSection;
  if (!credsOk) {
    fleetSection = `
      <div style="margin-top:14px;padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;font-size:0.85em">
        <div style="color:var(--yellow,#e1b969);margin-bottom:4px">Fleet MRR credentials are not configured.</div>
        <div style="color:var(--text2);font-size:0.85em">Set API key, secret, and stratum username via the gear icon → MRR Settings before auto-publish can run.</div>
      </div>`;
  } else if (fleetEnabled) {
    fleetSection = `
      <div style="margin-top:14px;color:var(--text2);font-size:0.85em">
        Fleet auto-publish: <span style="color:var(--green,#4caf50);font-weight:600">Enabled</span>
        <button class="secondary" data-action="mrrPillToggleFleet" data-arg-enable="false" style="margin-left:8px;font-size:0.78em;padding:2px 8px">Disable for fleet</button>
      </div>`;
  } else {
    fleetSection = `
      <div style="margin-top:14px;color:var(--text2);font-size:0.85em">
        Fleet auto-publish: <span style="color:var(--text2)">Disabled</span>
        <button data-action="mrrPillToggleFleet" data-arg-enable="true" style="margin-left:8px;font-size:0.78em;padding:2px 8px">Enable for fleet</button>
      </div>
      <div style="color:var(--text2);font-size:0.78em;margin-top:4px">Affects every miner with a rig configured. Per-miner rig assignment below works independently.</div>`;
  }

  const unsavedHint = isUnsaved
    ? `<div style="color:var(--accent);font-size:0.78em;margin-top:4px">Picked rig <strong>#${currentRigId}</strong> — click <em>Save</em> to apply (saved: ${savedRigId || 'none'}).</div>`
    : '';
  const body = `
    <div style="color:var(--text2);font-size:0.85em;margin-bottom:6px">${escapeHTML(ip || mac)}</div>
    <div style="margin-bottom:12px">
      Status: ${statusBanner}${openRigBtn}
    </div>
    <div>
      <label for="mrr-pill-rig-id" style="display:block;margin-bottom:4px">Rig ID
        <span style="color:var(--text2);font-size:0.8em">— per-miner. 0 = no MRR for this miner.</span>
      </label>
      <input id="mrr-pill-rig-id" type="number" min="0" step="1" value="${currentRigId}" style="width:160px">
      <button class="secondary" data-action="mrrPillPickRig" style="margin-left:8px;font-size:0.8em;padding:3px 10px">⚙ Pick from my rigs</button>
      ${unsavedHint}
    </div>
    ${fleetSection}
    <div id="mrr-pill-error" style="color:var(--red);font-size:0.85em;margin-top:10px;min-height:1em"></div>
  `;
  openModal(`MRR — ${ip || mac}`, body, [
    {label: 'Cancel', action: closeModal},
    {label: 'Clear rig', danger: true, action: () => submitMrrPillModal({clear: true})},
    {label: 'Save', action: () => submitMrrPillModal({})},
  ]);
}

async function mrrPillToggleFleet(enable){
  // Flip MRR_ENABLED via the fleet-ops endpoint (MRR_ENABLED is a fleet-ops key),
  // then re-render this popup so the operator sees the new state without leaving the modal.
  const errEl = document.getElementById('mrr-pill-error');
  if (errEl) errEl.textContent = '';
  const newVal = (enable === 'true' || enable === true);
  try {
    const r = await fetchJSON('/tuner/config/fleet_ops', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({MRR_ENABLED: newVal}),
    });
    if (!r || !r.updated) {
      const msg = (r && r.errors && r.errors.length) ? r.errors.join('; ') : 'Save failed';
      if (errEl) errEl.textContent = msg;
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = `Save errored: ${e}`;
    return;
  }
  // Refresh the popup against the same MAC so the fleet line reflects the flip.
  const mac = (_mrrPillModalState && _mrrPillModalState.mac) || '';
  if (mac) await openMrrPillModal(mac);
}

function mrrPillPickRig(){
  // Hand off to the rig picker, targeting THIS popup's input. The picker's
  // close-callback re-opens our modal with the picked rig threaded in as a
  // pendingRigId — openMrrPillModal treats that as an unsaved override so the
  // value persists across the re-render. Without this, the picker's input
  // write hits a missing DOM node (the popup was replaced by the picker
  // modal) and the picked rig silently reverts on re-open.
  const mac = (_mrrPillModalState && _mrrPillModalState.mac) || '';
  if (!mac) return;
  openMRRRigPicker('mrr-pill-rig-id', (pickedId) => {
    if (pickedId != null) {
      openMrrPillModal(mac, pickedId);
    } else {
      // Cancelled — re-open with the saved value, no pending override.
      openMrrPillModal(mac);
    }
  });
}

async function submitMrrPillModal(opts){
  const errEl = document.getElementById('mrr-pill-error');
  if (errEl) errEl.textContent = '';
  const mac = (_mrrPillModalState && _mrrPillModalState.mac) || '';
  if (!mac) { closeModal(); return; }
  let payload;
  if (opts && opts.clear) {
    // Null deletes the override; backend prunes the entry if no overrides remain.
    payload = {MRR_RIG_ID: null};
  } else {
    const input = document.getElementById('mrr-pill-rig-id');
    const raw = input ? input.value.trim() : '';
    const n = parseInt(raw, 10);
    if (!Number.isFinite(n) || n < 0) {
      if (errEl) errEl.textContent = 'Rig ID must be a non-negative integer (0 to clear).';
      return;
    }
    // 0 == clear; the backend triggers _mrr_apply_pool_config either way.
    payload = (n === 0) ? {MRR_RIG_ID: null} : {MRR_RIG_ID: n};
  }
  try {
    const macDashed = (mac || '').replace(/:/g, '-');
    const r = await fetchJSON('/tuner/config/miner/' + macDashed, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!r || !r.updated) {
      const msg = (r && r.errors && r.errors.length) ? r.errors.join('; ') : 'Save failed';
      if (errEl) errEl.textContent = msg;
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = `Save errored: ${e}`;
    return;
  }
  closeModal();
  // Refresh the fleet table immediately so the pill state is in sync.
  if (typeof pollOverview === 'function') pollOverview();
  if (typeof pollDetail === 'function' && currentMac() === mac) pollDetail();
}

function _fmtUSD(u){ return u == null ? '—' : `$${Number(u).toFixed(2)}`; }

// Top-level profit helper. Mirrors the backend `compute_profit_usd_per_day`
// and the V/F heatmap's `vfProfitForEntry` closure — keep all three in sync.
// Reads minerstatSnapshot (module-level) + status.config for rate/coin/modifier.
// Returns $/day (can be negative) or null when inputs are missing.
function computeProfitUsdPerDay(hashrateThs, powerW, status){
  if (hashrateThs == null || powerW == null) return null;
  if (!minerstatSnapshot || !minerstatSnapshot.coins) return null;
  const cfg = (status && status.config) || {};
  const coinId = (cfg.minerstat_coin || 'BTC').toUpperCase();
  const coin = minerstatSnapshot.coins[coinId];
  if (!coin) return null;
  const rate = Number.isFinite(cfg.electric_rate_per_kwh)
    ? Number(cfg.electric_rate_per_kwh) : 0.10;
  const modifier = Number.isFinite(cfg.income_modifier_pct)
    ? Number(cfg.income_modifier_pct) : 0;
  try {
    const coinPerThDay = (86400 / coin.block_time_s) * coin.reward_block
      * (1e12 / coin.network_hashrate);
    const revenue = hashrateThs * coinPerThDay * coin.price_usd
      * (1 + modifier / 100);
    const cost = (powerW * 24 / 1000) * rate;
    return revenue - cost;
  } catch(e) { return null; }
}
function _fmtDelta(d, unit, invertColor){
  if (d == null) return '—';
  const sign = d > 0 ? '+' : '';
  // For profit, + is green (more profit); for power, + is red (more draw).
  const isGood = invertColor ? d <= 0 : d >= 0;
  const color = Math.abs(d) < 0.001 ? 'var(--text2)' : (isGood ? '#69e189' : '#e16969');
  return `<span style="color:${color}">${sign}${Number(d).toFixed(2)}${unit}</span>`;
}

async function pollOverview(){
  const d = await fetchJSON('/tuner/overview');
  if (!d) return;
  overviewData = d;
  minerList = (d.miners || []).map(m => m.ip);
  updateKPIs();
  // Rebuild the model-filter dropdown from the current fleet so newly
  // discovered models become checkboxes. Must run before renderTable so
  // the summary count reflects the latest distinct-model set.
  populateModelFilterOptions();
  renderTable();
  // Refresh minerstat + MRR cards alongside overview polls. Each is one
  // JSON read — MRR reuses the /tuner/config payload rather than hitting
  // MRR on every poll (that'd burn nonces for no reason).
  pollMinerstatCard();
  pollMRRCard();
}

// ─── Auth: login / setup / logout ─────────────────────────────────────────────
let authMode = 'login';  // 'login' or 'setup'

function showLogin() {
  showView('login');
  document.getElementById('login-password').focus();
}

function showMain() {
  // Used right after successful login — delegate to the router so it picks
  // the right view based on the current hash (overview or a detail URL).
  route();
}

function applyAuthModeUI() {
  const isSetup = authMode === 'setup';
  const passwordInput = document.getElementById('login-password');
  const confirmInput = document.getElementById('login-confirm');
  document.getElementById('login-title').textContent = isSetup ? 'Set tuner password' : 'Sign in';
  document.getElementById('login-sub').textContent = isSetup
    ? 'First-run setup. Choose at least 12 characters; this password is required for every sign-in.'
    : 'Enter the tuner password to continue.';
  document.getElementById('login-password-label').textContent = isSetup ? 'New password' : 'Password';
  passwordInput.setAttribute('autocomplete', isSetup ? 'new-password' : 'current-password');
  if (isSetup) {
    passwordInput.setAttribute('minlength', '12');
    confirmInput.setAttribute('minlength', '12');
  } else {
    passwordInput.removeAttribute('minlength');
    confirmInput.removeAttribute('minlength');
  }
  document.getElementById('login-confirm-row').style.display = isSetup ? '' : 'none';
  document.getElementById('login-submit').textContent = isSetup ? 'Create password' : 'Sign in';
  document.getElementById('login-error').textContent = '';
}

async function submitAuth(e) {
  e.preventDefault();
  const pw = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  if (authMode === 'setup') {
    const confirm = document.getElementById('login-confirm').value;
    if (pw.length < 12) { errEl.textContent = 'Password must be at least 12 characters.'; return; }
    if (pw !== confirm) { errEl.textContent = 'Passwords do not match.'; return; }
  }
  const url = authMode === 'setup' ? '/tuner/setup' : '/tuner/login';
  let r;
  try {
    r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
  } catch {
    errEl.textContent = 'Server unreachable.';
    return;
  }
  let body = null;
  try { body = await r.json(); } catch {}
  if (r.status === 429) {
    errEl.textContent = 'Too many failed attempts. Wait 5 minutes and try again.';
    return;
  }
  if (!r.ok || (body && body.ok === false)) {
    errEl.textContent = (body && body.error) || (authMode === 'setup' ? 'Setup failed.' : 'Invalid password.');
    return;
  }
  // Success — wipe the form, become authenticated, kick off the real init.
  document.getElementById('login-password').value = '';
  if (document.getElementById('login-confirm')) document.getElementById('login-confirm').value = '';
  authReady = true;
  authMode = 'login';
  showMain();
  await startApp();
}

async function logout() {
  try { await fetch('/tuner/logout', {method:'POST'}); } catch {}
  authReady = false;
  showLogin();
}

async function checkAuth() {
  // Don't use fetchJSON here — we want to see the response body even if 401.
  try {
    const r = await fetch('/tuner/auth/status');
    if (!r.ok) return { authenticated: false };
    return await r.json();
  } catch {
    return { authenticated: false };
  }
}

// ─── App init: auth check first, then the dashboard ──────────────────────────
let appStarted = false;

async function startApp() {
  if (appStarted) { route(); return; }
  appStarted = true;
  initCharts();
  setupFilterChips();
  // Hydrate the model-filter selection from localStorage before the first
  // overview poll so the table renders with the saved filter applied.
  loadModelFilter();
  // Hydrate column preferences from localStorage and populate the static
  // (now-empty) <thead> so the table header has labels before the first
  // poll fills the body.
  activeColumnPrefs = loadColumnPrefs();
  renderTableHeader();
  await loadConfig();
  route();  // picks overview or detail based on current hash
  setInterval(poll, 10000);
}

async function init() {
  const auth = await checkAuth();
  if (auth && auth.setup_required) {
    authMode = 'setup';
    applyAuthModeUI();
    showLogin();
    return;
  }
  if (auth && auth.authenticated) {
    authReady = true;
    await startApp();
    return;
  }
  authMode = 'login';
  applyAuthModeUI();
  showLogin();
}

// Action dispatcher: replaces inline event handlers via data-attribute lookup.
function collectArgs(el) {
  const args = {};
  for (const key in el.dataset) {
    if (key.startsWith('arg') && key.length > 3) {
      const k = key[3].toLowerCase() + key.slice(4);
      args[k] = el.dataset[key];
    }
  }
  return args;
}

function installActionDispatcher() {
  // Toggle events on <details> don't bubble — wire them directly per element
  // that has data-toggle-action. Do an initial sweep on install plus a
  // mutation observer in case rendered markup adds more later.
  const wireToggle = (el) => {
    if (el._toggleWired) return;
    el._toggleWired = true;
    el.addEventListener('toggle', () => {
      const name = el.dataset.toggleAction;
      const action = ACTIONS[name];
      if (action) {
        action(collectArgs(el), el);
      } else {
        console.warn('Unknown data-toggle-action:', name);
      }
    });
  };
  const sweepToggle = () => {
    document.querySelectorAll('[data-toggle-action]').forEach(wireToggle);
  };
  sweepToggle();
  // Re-sweep when DOM mutates (the defaults accordion renders inputs lazily,
  // and the modal infra injects markup that may carry data-toggle-action).
  if (typeof MutationObserver !== 'undefined') {
    const mo = new MutationObserver(sweepToggle);
    mo.observe(document.body || document.documentElement, {
      childList: true, subtree: true,
    });
  }

  document.addEventListener('click', (event) => {
    const el = event.target.closest('[data-action]');
    if (!el) return;

    if (el.dataset.stopPropagation === 'true') {
      event.stopPropagation();
    }

    const name = el.dataset.action;
    const args = collectArgs(el);
    const action = ACTIONS[name];
    if (action) {
      action(args);
    } else {
      console.warn('Unknown data-action:', name);
    }
  });

  document.addEventListener('change', (event) => {
    const el = event.target.closest('[data-change-action]');
    if (!el) return;

    const name = el.dataset.changeAction;
    const args = collectArgs(el);
    const action = ACTIONS[name];
    if (action) {
      action(args, event.target);
    } else {
      console.warn('Unknown data-change-action:', name);
    }
  });

  document.addEventListener('submit', (event) => {
    const el = event.target.closest('[data-form-action]');
    if (!el) return;

    event.preventDefault();
    const name = el.dataset.formAction;
    const action = ACTIONS[name];
    if (action) {
      action({}, event);
    } else {
      console.warn('Unknown data-form-action:', name);
    }
  });
}

const ACTIONS = {
  openColumnFilterModal: () => openColumnFilterModal(),
  applyColumnPreset: (args) => applyColumnPreset(args.name),
  bulkAction: (args) => bulkAction(args.name),
  bulkRemove: () => bulkRemove(),
  navigateToDetail: (args) => navigateToDetail(args.mac),
  navigateToOverview: () => navigateToOverview(),
  removeMiner: (args) => removeMiner(args.mac, args.ip),
  changeMinerPassword: () => changeMinerPassword(),
  openSetMacModal: () => openSetMacModal(),
  startTuning: () => startTuning(),
  stopTuning: () => stopTuning(),
  deleteProfile: () => deleteProfile(),
  resetStockBaseline: () => resetStockBaseline(),
  retuneVoltage: (args) => retuneVoltage(parseFloat(args.voltage)),
  selectVoltageProfile: (args) => selectVoltageProfile(parseFloat(args.voltage)),
  togglePreview: (args) => togglePreview(parseFloat(args.voltage)),
  clearHeatmapPreview: () => clearHeatmapPreview(),
  showVoltageLogModal: (args) => showVoltageLogModal(parseFloat(args.voltage)),
  clearRemeasureQueue: () => clearRemeasureQueue(),
  processRemeasureQueue: () => processRemeasureQueue(),
  saveConfig: () => saveConfig(),
  saveDefaults: () => saveDefaults(),
  clearSelection: () => clearSelection(),
  closeModal: () => closeModal(),
  openBulkConfigModal: () => openBulkConfigModal(),
  toggleBulkConfigCategory: (args, target) => toggleBulkConfigCategory(args, target),
  bulkConfigSelectAll: () => bulkConfigSelectAll(),
  bulkConfigSelectNone: () => bulkConfigSelectNone(),
  bulkPlatformChange: (args, target) => bulkPlatformChange(args, target),
  openBulkPoolsModal: () => openBulkPoolsModal(),
  openBulkSetPowerLimitModal: () => openBulkSetPowerLimitModal(),
  openNetworkSettings: () => openNetworkSettings(),
  scanNowFromModal: () => scanNowFromModal(),
  openMinerstatSettings: () => openMinerstatSettings(),
  minerstatFetchNow: () => minerstatFetchNow(),
  openMRRSettings: () => openMRRSettings(),
  mrrResyncNow: () => mrrResyncNow(),
  mrrTestConnection: () => mrrTestConnection(),
  _mrrPickerSelect: (args) => _mrrPickerSelect(parseInt(args.id)),
  openMrrPillModal: (args) => openMrrPillModal(args.mac),
  mrrPillToggleFleet: (args) => mrrPillToggleFleet(args.enable),
  mrrPillPickRig: () => mrrPillPickRig(),
  setHeatmapMode: (args) => setHeatmapMode(args.mode, args.side),
  switchTab: (args) => switchTab(args.tab),
  togglePerBoard: (args) => togglePerBoard(args.rowId),
  downloadLog: () => downloadLog(),
  exportResults: (args) => exportResults(args.format),
  logout: () => logout(),
  toggleSelect: (args, target) => toggleSelect(args.mac, target.checked),
  toggleSelectAll: (_, target) => toggleSelectAll(target),
  submitAuth: (_, event) => submitAuth(event),
  onDefaultsToggle: (_, target) => onDefaultsToggle(target),
  defaultsPlatformChange: (_, target) => defaultsPlatformChange(target.value),
  modelFilterSelectAll: () => modelFilterSelectAll(),
  modelFilterClearAll: () => modelFilterClearAll(),
  onModelFilterToggle: (args, target) => onModelFilterToggle(args, target),
  setMetricsRange: (args, target) => setMetricsRange({value: target ? target.value : (args && args.value)}),
  applyCustomMetricsRange: () => applyCustomMetricsRange()
};

// Action dispatcher installed when DOM is ready (called from init).
// Wire up the data-action dispatcher BEFORE init() runs the auth check.
installActionDispatcher();
init();
