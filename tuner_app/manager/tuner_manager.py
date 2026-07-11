"""TunerManager: lifecycle owner for per-miner TuningEngine instances.

v4: ``self.engines`` is keyed by canonical MAC (or synth ID for L3-isolated
miners). Public methods accept an *identifier* (IP for legacy / pre-A12
HTTP route handlers, MAC for v4 callers) and resolve internally to MAC via
``canonical_miner_key``. The transitional layer goes away in PR4 once HTTP
routes pass MAC end-to-end.
"""

from __future__ import annotations

import re
import threading

from tuner_app import state
from tuner_app.config.effective import EffectiveConfig, canonical_miner_key
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.registry import MINER_API_REGISTRY
from tuner_app.mrr.rental_cache import rental_cache
from tuner_app.profit.compute import compute_profit_usd_per_day, score_cell
from tuner_app.profit.minerstat import get_minerstat_snapshot_copy

_IPV4_RE_LOCAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _canonical_mac_for_overview(key):
    """Return a canonical MAC/synth identifier for the overview wire shape.

    A v4 entry (real MAC like ``aa:bb:cc:dd:ee:ff`` or synth ``syn-...``) is
    returned unchanged. A legacy v3 IPv4-keyed entry or a MINER_IPS-only
    fallback IP is converted to a stable synth ID of the form
    ``syn-<dashed-ip>``, which `_normalize_mac` accepts and the reverse-lookup
    in `canonical_miner_key` translates back to the IP for engine routing.
    """
    if isinstance(key, str) and _IPV4_RE_LOCAL.match(key):
        return f"syn-{key.replace('.', '-')}"
    return key


class TunerManager:
    def __init__(self, config):
        self.config = config
        # Keyed by canonical MAC / synth ID in v4. Legacy v3 fallback paths
        # may key by IP transitionally — both flow through canonical_miner_key.
        self.engines = {}
        # ThreadedHTTPServer runs every request on its own thread, so every
        # read or mutation of self.engines crosses a thread boundary. Without
        # this lock, two concurrent /tuner/start calls for a fresh MAC can
        # both pass the `mac not in self.engines` check and each instantiate
        # an engine — the loser's thread runs orphaned. RLock so methods that
        # take the lock can call other locked methods (start_tuning →
        # get_engine).
        self._lock = threading.RLock()

    def _to_mac(self, identifier):
        """Resolve *identifier* (IP or MAC/synth) to canonical engines-dict key.

        Wrapper around ``canonical_miner_key`` so call sites stay terse.
        """
        return canonical_miner_key(identifier)

    def get_engine(self, identifier):
        mac = self._to_mac(identifier)
        with self._lock:
            if mac not in self.engines:
                # Each engine reads through an EffectiveConfig bound to its
                # canonical key so per-miner overrides in MINER_CONFIGS[mac]
                # take precedence over the global defaults.
                from tuner_app.tuning_engine.engine import TuningEngine

                self.engines[mac] = TuningEngine(mac, EffectiveConfig(mac))
            return self.engines[mac]

    def pop_engine(self, identifier):
        mac = self._to_mac(identifier)
        with self._lock:
            return self.engines.pop(mac, None)

    def peek_engine(self, identifier):
        mac = self._to_mac(identifier)
        with self._lock:
            return self.engines.get(mac)

    def reset_engine(self, identifier):
        """Replace engine at *identifier* with a fresh instance. Caller must
        have already stopped+joined the old engine."""
        mac = self._to_mac(identifier)
        with self._lock:
            if mac in self.engines:
                from tuner_app.tuning_engine.engine import TuningEngine

                self.engines[mac] = TuningEngine(mac, EffectiveConfig(mac))

    def refresh_engine_ip(self, mac, new_ip):
        """Retarget the engine at *mac* to *new_ip* without teardown.

        Triggered by the scanner when a known MAC is seen at a new IP (DHCP
        move). Updates ``engine.ip`` and rebinds ``engine.api`` to a new
        MinerAPI instance with the new base URL. The tuning thread keeps
        running uninterrupted; password and firmware are unchanged.

        Caller passes a canonical MAC (the scanner has it from the
        ProbeResult). No-op when no engine has been spawned yet for *mac*
        or when *new_ip* matches the current engine.ip.
        """
        with self._lock:
            engine = self.engines.get(mac)
            if engine is None:
                return
            if engine.ip == new_ip:
                return
            engine.ip = new_ip
            firmware_type = engine.firmware_type
            engine.api = MINER_API_REGISTRY[firmware_type](new_ip, engine.config)

    def start_tuning(self, identifier):
        return self.get_engine(identifier).start()

    def stop_tuning(self, identifier):
        self.get_engine(identifier).stop()

    def get_all_status(self):
        """Return per-miner status keyed by IP for backward-compat dashboards.

        Iterates the canonical fleet roster (``MINER_CONFIGS.keys()``) and
        emits IP-keyed rows for the wire shape consumers expect today; PR4
        will switch the wire shape to MAC-keyed. Legacy fallback: any
        ``MINER_IPS`` entries without a ``MINER_CONFIGS`` row still render
        (test fixtures use this shape).
        """
        result = {}
        with state.config_lock:
            seen_ips = set()
            roster = []
            for mac, ov in state.MINER_CONFIGS.items():
                if not isinstance(ov, dict):
                    continue
                row_ip = ov.get("ip") or mac
                roster.append((mac, row_ip))
                seen_ips.add(row_ip)
            for legacy_ip in state.CONFIG["fleet_ops"].get("MINER_IPS", []):
                if legacy_ip not in seen_ips:
                    roster.append((legacy_ip, legacy_ip))
        for mac, ip in roster:
            result[ip] = self.get_engine(mac).get_status()
        return result

    def get_overview(self):
        """Aggregate dashboard data — cheap to compute, polled frequently.

        Buckets tuner-phase into 7 states (idle, tuning, maintaining, offline,
        error, stopped, stopping) and mining-state into 3 (mining, stopped,
        unknown). The tuner-bucket value comes from each engine's get_status()
        payload (key "tuner_bucket"); compute_tuner_bucket in status.py owns
        the (phase, engine_busy) → bucket derivation. Returns fleet totals
        plus a brief per-miner record for the table.
        """

        def mining_bucket(op_state):
            if not op_state:
                return "unknown"
            s = str(op_state).strip().lower()
            if "mining" in s:
                return "mining"
            if s in ("stopped", "idle", "halted", "paused"):
                return "stopped"
            return "unknown"

        state_counts = {
            "idle": 0,
            "tuning": 0,
            "maintaining": 0,
            "offline": 0,
            "error": 0,
            "stopped": 0,
            "stopping": 0,
        }
        mining_counts = {"mining": 0, "stopped": 0, "unknown": 0}
        total_hashrate = 0.0
        total_power = 0.0
        total_profit = 0.0
        profit_available = False
        miners = []

        # v4 fleet roster: iterate MINER_CONFIGS.keys() (MAC-keyed) and emit
        # the IP from each entry's "ip" field for the dashboard table. Legacy
        # v3 / pre-migration test fixtures may populate MINER_IPS without
        # MINER_CONFIGS entries; fold those in so they still render.
        with state.config_lock:
            seen_ips = set()
            roster = []
            for mac, ov in state.MINER_CONFIGS.items():
                if not isinstance(ov, dict):
                    continue
                row_ip = ov.get("ip") or mac
                roster.append((mac, row_ip))
                seen_ips.add(row_ip)
            for legacy_ip in state.CONFIG["fleet_ops"].get("MINER_IPS", []):
                if legacy_ip not in seen_ips:
                    # No MINER_CONFIGS entry — engine identifier IS the IP
                    # (canonical_miner_key returns IP unchanged in that case).
                    roster.append((legacy_ip, legacy_ip))
        for mac, ip in roster:
            # Transform legacy IP keys to canonical synth form for overview wire shape
            canonical_mac = _canonical_mac_for_overview(mac)
            engine = self.get_engine(mac)
            # When the engine isn't actively tuning (idle after restart, stopped,
            # error), no phase loop is calling _update_live_data and last_summary
            # stays None — the dashboard would otherwise render em-dashes for
            # hostname/model AND zeros for hashrate/power/voltage on every miner.
            # Refresh on-demand BEFORE get_status() so tuned_stats sees the fresh
            # DTO. The internal 5-second rate-limit in _update_live_data caps cost;
            # offline miners fail fast on the first endpoint and don't make all
            # 5 calls. Mirrors the get_live_data pattern in status.py.
            if engine.last_summary is None:
                try:  # noqa: SIM105
                    engine._update_live_data()
                except (MinerOfflineError, MinerCommandError):
                    pass
            status = engine.get_status()
            ts = status.get("tuned_stats") or {}
            summary = engine.last_summary
            op_state = ts.get("state") or (summary.operating_state if summary else None)
            hostname = summary.hostname if summary else None
            if isinstance(hostname, str):
                hostname = hostname.strip() or None
            # ePIC PowerPlay-BMS reports the hardware model under "Type"
            # (e.g. "Antminer S21"). Fall back to "Model" / "MinerType" for
            # firmware variants and forward compatibility (L7 etc.).
            model = summary.model if summary else None
            if isinstance(model, str):
                model = model.strip() or None

            hashrate = ts.get("hashrate_ths") or 0
            power = ts.get("power_w") or 0
            efficiency = ts.get("efficiency_jth") or 0
            voltage = ts.get("voltage_mv") or 0
            mbucket = mining_bucket(op_state)
            tbucket = status.get("tuner_bucket") or "idle"
            state_counts[tbucket] = state_counts.get(tbucket, 0) + 1
            mining_counts[mbucket] = mining_counts.get(mbucket, 0) + 1

            rate, coin_data, modifier = engine._get_profit_display_context()
            profit = None
            if coin_data is not None and hashrate > 0:
                profit = compute_profit_usd_per_day(hashrate, power, coin_data, rate, modifier)

            # Only roll non-trivial hashrate into the fleet totals so a bricked
            # miner (0 TH/s but nonzero idle draw) doesn't pollute avg J/TH.
            if hashrate > 0:
                total_hashrate += hashrate
                total_power += power
                if profit is not None:
                    total_profit += profit
                    profit_available = True

            miners.append(
                {
                    "ip": ip,
                    "mac": canonical_mac,
                    "hostname": hostname,
                    "model": model,
                    "firmware_type": status["firmware_type"],
                    "tuner_phase": status.get("phase"),
                    "tuner_phase_detail": status.get("phase_detail"),
                    "tuner_bucket": tbucket,
                    "operating_state": op_state,
                    "mining_bucket": mbucket,
                    "hashrate_ths": hashrate,
                    "power_w": power,
                    "efficiency_jth": efficiency,
                    "voltage_mv": voltage,
                    "selected_profile_voltage_mv": status.get("active_sweep_voltage_mv")
                    or status.get("sweep_voltage_mv")
                    or 0,
                    "tuning_complete": status.get("tuning_complete"),
                    "engine_busy": status.get("engine_busy"),
                    "offline_since_ts": status.get("offline_since_ts"),
                    "last_successful_contact_ts": status.get("last_successful_contact_ts"),
                    "avg_board_temp_c": status.get("avg_board_temp_c"),
                    "avg_chip_temp_c": status.get("avg_chip_temp_c"),
                    "profit_usd_day": profit,
                    "mrr_rental_status": rental_cache.get(mac),
                }
            )

        avg_eff = (total_power / total_hashrate) if total_hashrate > 0 else 0
        return {
            "total_hashrate_ths": total_hashrate,
            "total_power_w": total_power,
            "avg_efficiency_jth": avg_eff,
            "total_profit_usd_day": total_profit if profit_available else None,
            "state_counts": state_counts,
            "mining_counts": mining_counts,
            "miners": miners,
        }

    def retune_voltage(self, ip, voltage_mv):
        return self.get_engine(ip).start_retune(voltage_mv)

    def select_voltage_profile(self, ip, voltage_mv):
        self.get_engine(ip).select_voltage_profile(voltage_mv)

    def enqueue_remeasure(self, ip, voltage_mv, freq_mhz):
        return self.get_engine(ip).enqueue_remeasure(voltage_mv, freq_mhz)

    def clear_remeasure_queue(self, ip):
        self.get_engine(ip).clear_remeasure_queue()

    def start_remeasure_queue(self, ip):
        return self.get_engine(ip).start_remeasure_queue()

    def compute_profit_preview(self, ips):
        """For each listed IP in profit mode, compute the proposed action:
        "none" (current profile is already best), "switch" (chip-tuned entry
        at a different voltage wins), "retune" (fine cell at a different
        voltage wins — needs chip-tune), or "fine_then_retune" (coarse cell
        wins — needs fine-grid then chip-tune).

        Returns the full preview payload for the dashboard modal:
            {
              "snapshot": {...},  # current minerstat snapshot
              "miners": [{ip, current, proposed, delta_profit_usd_day,
                          delta_power_w, skipped_reason?}],
              "totals": {current_power_w, proposed_power_w,
                         current_profit_usd_day, proposed_profit_usd_day}
            }

        Miners not in profit mode are included with skipped_reason so the UI
        can show "skipped — not in profit mode" per-row. A miner with no
        voltage_results or no vf_surface data is also skipped.
        """
        snapshot = get_minerstat_snapshot_copy()
        payload_miners = []
        total_current_power = 0.0
        total_proposed_power = 0.0
        total_current_profit = 0.0
        total_proposed_profit = 0.0
        for ip in ips:
            try:
                engine = self.get_engine(ip)
            except Exception as ex:
                payload_miners.append(
                    {
                        "ip": ip,
                        "mac": None,
                        "skipped_reason": f"engine init failed: {ex}",
                    }
                )
                continue
            mode = engine.config.get("TARGET_MODE", "efficiency") or "efficiency"
            if mode != "profitability":
                payload_miners.append(
                    {
                        "ip": ip,
                        "mac": engine.mac,
                        "skipped_reason": "not in profit mode",
                    }
                )
                continue
            coin_id = (engine.config.get("MINERSTAT_COIN", "BTC") or "BTC").upper()
            coins = snapshot.get("coins", {}) if snapshot else {}
            coin_data = coins.get(coin_id)
            if coin_data is None:
                payload_miners.append(
                    {
                        "ip": ip,
                        "mac": engine.mac,
                        "skipped_reason": f"no minerstat data for {coin_id} — fetch first",
                    }
                )
                continue
            try:
                rate = float(engine.config.get("ELECTRIC_RATE_PER_KWH", 0.10) or 0.10)
            except (TypeError, ValueError):
                rate = 0.10
            try:
                modifier = float(engine.config.get("INCOME_MODIFIER_PCT", 0.0) or 0.0)
            except (TypeError, ValueError):
                modifier = 0.0
            ctx = ("profitability", rate, coin_data, modifier)

            # Current profile: the active voltage_results entry (if any).
            current_entry = None
            active_mv = engine.active_sweep_voltage_mv
            if active_mv is not None:
                current_entry = next(
                    (r for r in engine.voltage_results if r.get("voltage_mv") == active_mv), None
                )
            current_profit = (
                compute_profit_usd_per_day(
                    current_entry.get("hashrate_ths") if current_entry else None,
                    current_entry.get("power_w") if current_entry else None,
                    coin_data,
                    rate,
                    modifier,
                )
                if current_entry
                else None
            )
            current_block = (
                None
                if current_entry is None
                else {
                    "voltage_mv": int(current_entry["voltage_mv"]),
                    "freq_mhz": current_entry.get("avg_freq_mhz"),
                    "hashrate_ths": current_entry.get("hashrate_ths"),
                    "power_w": current_entry.get("power_w"),
                    "efficiency_jth": current_entry.get("efficiency_jth"),
                    "profit_usd_day": current_profit,
                }
            )

            # Score every cell; pick the winner.
            def sc(entry):
                return score_cell(entry, *ctx)  # noqa: B023

            all_cells = []
            for r in engine.voltage_results:
                s = sc(r)
                if s is not None:
                    all_cells.append(("chip_tuned", r, s))
            for e in engine.vf_surface:
                s = sc(e)
                if s is not None:
                    kind = "fine" if e.get("fine") else "coarse"
                    all_cells.append((kind, e, s))
            if not all_cells:
                payload_miners.append(
                    {
                        "ip": ip,
                        "mac": engine.mac,
                        "skipped_reason": "no measurement data — run a full tune first",
                        "current": current_block,
                    }
                )
                continue

            # Dedup by voltage (prefer chip_tuned, then best score).
            def cell_rank(c):
                kind, _entry, score = c
                # chip_tuned wins ties; within the same kind, lower score wins.
                kind_rank = 0 if kind == "chip_tuned" else (1 if kind == "fine" else 2)  # noqa: B023
                return (kind_rank, score)

            by_v = {}
            for c in all_cells:
                _k, entry, _s = c
                v = int(entry["voltage_mv"])
                existing = by_v.get(v)
                if existing is None or cell_rank(c) < cell_rank(existing):
                    by_v[v] = c
            ranked_cells = sorted(by_v.values(), key=lambda c: c[2])
            winner_kind, winner_entry, winner_score = ranked_cells[0]

            winner_profit = -winner_score  # score is -profit in profit mode

            # Determine action: is the winner already the current profile?
            same_profile = (
                current_entry is not None
                and winner_kind == "chip_tuned"
                and int(winner_entry["voltage_mv"]) == int(current_entry["voltage_mv"])
            )
            if same_profile:
                action = "none"
            elif winner_kind == "chip_tuned":
                action = "switch"
            elif winner_kind == "fine":
                action = "retune"
            else:
                action = "fine_then_retune"

            proposed_block = {
                "voltage_mv": int(winner_entry["voltage_mv"]),
                "freq_mhz": (
                    winner_entry.get("avg_freq_mhz")
                    if winner_kind == "chip_tuned"
                    else round(float(winner_entry["freq_mhz"]), 3)
                ),
                "hashrate_ths": winner_entry.get("hashrate_ths"),
                "power_w": winner_entry.get("power_w"),
                "efficiency_jth": winner_entry.get("efficiency_jth"),
                "profit_usd_day": winner_profit,
                "action": action,
                "source": winner_kind,
            }

            # Deltas (proposed - current). None if current is missing.
            delta_profit = None
            delta_power = None
            if current_profit is not None:
                delta_profit = winner_profit - current_profit
            if current_entry and current_entry.get("power_w") is not None:
                delta_power = float(winner_entry.get("power_w", 0) or 0) - float(
                    current_entry.get("power_w", 0) or 0
                )

            payload_miners.append(
                {
                    "ip": ip,
                    "mac": engine.mac,
                    "current": current_block,
                    "proposed": proposed_block,
                    "delta_profit_usd_day": delta_profit,
                    "delta_power_w": delta_power,
                    "coin": coin_id,
                    "electric_rate_per_kwh": rate,
                }
            )
            if current_entry and current_entry.get("power_w") is not None:
                total_current_power += float(current_entry["power_w"])
            if winner_entry.get("power_w") is not None:
                total_proposed_power += float(winner_entry["power_w"])
            if current_profit is not None:
                total_current_profit += float(current_profit)
            total_proposed_profit += float(winner_profit)

        return {
            "snapshot": snapshot,
            "miners": payload_miners,
            "totals": {
                "current_power_w": total_current_power,
                "proposed_power_w": total_proposed_power,
                "current_profit_usd_day": total_current_profit,
                "proposed_profit_usd_day": total_proposed_profit,
                "delta_profit_usd_day": (total_proposed_profit - total_current_profit),
                "delta_power_w": total_proposed_power - total_current_power,
            },
        }

    def apply_profit_action(self, ip, action, voltage_mv, freq_mhz=None):
        """Execute one profit-recompute action on a single miner. Returns
        (ok, err, detail) where detail is the action-type string or empty.

        Stops the engine thread first — every action path assumes no other
        tune thread is running. Caller coordinates the fleet-wide timing
        (the apply endpoint loops over miners and spawns each action)."""
        try:
            engine = self.get_engine(ip)
        except Exception as ex:
            return False, f"engine init failed: {ex}", ""
        # Stop any running tune so the action runner can take the thread slot.
        if engine.thread and engine.thread.is_alive():
            engine.stop()
            engine.thread.join(timeout=5)
            if engine.thread.is_alive():
                return False, "engine did not stop within 5s", ""
        if action == "none":
            return True, "", "none"
        if action == "switch":
            try:
                engine.select_voltage_profile(int(voltage_mv))
                return True, "", "switch"
            except ValueError as ex:
                return False, str(ex), "switch"
            except Exception as ex:
                return False, f"switch failed: {ex}", "switch"
        if action == "retune":
            ok, err = engine.start_retune(int(voltage_mv))
            return ok, err, "retune"
        if action == "fine_then_retune":
            if freq_mhz is None:
                return False, "freq_mhz required for fine_then_retune", ""
            ok, err = engine.start_fine_then_retune(int(voltage_mv), float(freq_mhz))
            return ok, err, "fine_then_retune"
        return False, f"unknown action: {action}", ""
