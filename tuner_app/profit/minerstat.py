"""Minerstat /coins fetch + snapshot persistence + monthly auto-poll scheduler."""

from __future__ import annotations

import copy
import http.client
import json
import logging
import os
import threading
from datetime import UTC, datetime

from tuner_app import state
from tuner_app.constants import MINERSTAT_FILE
from tuner_app.miner.exceptions import MinerCommandError
from tuner_app.net.response_limits import read_capped_http_response

logger = logging.getLogger(__name__)


def _atomic_json_write(path: str, payload: object, indent: int | None = 2) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=indent)
    os.replace(tmp, path)


# Rough block-time defaults (seconds). Minerstat doesn't always return this —
# hardcoded per-coin table is simpler and more reliable than parsing algo →
# block-time mappings on the fly. Extend as more coins get targeted.
COIN_BLOCK_TIME_S = {
    "BTC": 600,  # 10 min
    "LTC": 150,  # 2.5 min
    "ETH": 13,  # pre-merge; PoS no longer mined but kept for fallback safety
    "ETC": 13,
    "BCH": 600,
    "DOGE": 60,
    "DASH": 150,
}


class MinerstatError(Exception):
    """Raised when the minerstat fetch fails in a way the caller can't retry
    safely (HTTP 4xx, malformed JSON, unknown coin). Transient errors
    (timeout, 5xx) should still raise this — the scheduler's retry loop
    treats every failure identically."""

    pass


def fetch_minerstat_coins(coin_list, api_key="", timeout=15):
    """Fetch current minerstat /coins data for the requested coin IDs.

    Args:
        coin_list: list of uppercase coin ids (e.g. ["BTC", "LTC"]).
        api_key: optional minerstat key. Free tier works without it; with a
                 key you get higher per-IP rate limits.
        timeout: seconds before the HTTPS call gives up.

    Returns:
        dict keyed by coin id, with fields suitable for compute_profit_usd_per_day:
        {coin_id: {price_usd, reward_block, network_hashrate, block_time_s,
                   algorithm, name}}.

    Raises:
        MinerstatError on any non-2xx HTTP, timeout, JSON parse error, or
        a response shape that doesn't contain the requested coins.
    """
    if not coin_list:
        raise MinerstatError("coin_list is empty")
    params = ",".join(sorted(set(c.strip().upper() for c in coin_list if c.strip())))
    path = f"/v2/coins?list={params}"
    headers = {"Accept": "application/json", "User-Agent": "antminer-chip-tuner/0.1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        conn = http.client.HTTPSConnection("api.minerstat.com", timeout=timeout)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            try:
                body = read_capped_http_response(resp)
            except MinerCommandError as exc:
                raise MinerstatError(
                    "minerstat response exceeded the network response limit"
                ) from exc
            status = resp.status
        finally:
            conn.close()
    except (TimeoutError, OSError) as e:
        raise MinerstatError(f"network error contacting minerstat: {e}")  # noqa: B904
    if status == 429:
        raise MinerstatError(
            "rate-limited by minerstat (HTTP 429) — free tier is 100 calls/day per key"
        )
    if status == 401:
        # Minerstat now requires an API key on every /v2/coins request.
        raise MinerstatError(
            "minerstat HTTP 401 — API key required. Set MINERSTAT_API_KEY in fleet settings."
        )
    if status < 200 or status >= 300:
        raise MinerstatError(f"minerstat returned HTTP {status}")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise MinerstatError(f"failed to parse minerstat JSON: {e}")  # noqa: B904
    if not isinstance(parsed, list):
        raise MinerstatError(f"minerstat response was not a list (got {type(parsed).__name__})")

    result = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        coin_id = str(entry.get("coin") or entry.get("id") or "").upper().strip()
        if not coin_id:
            continue
        try:
            price = float(entry.get("price"))
        except (TypeError, ValueError):
            continue
        try:
            reward_block = float(entry.get("reward_block", entry.get("block_reward", 0)))
        except (TypeError, ValueError):
            reward_block = 0.0
        try:
            network_hs = float(entry.get("network_hashrate", 0))
        except (TypeError, ValueError):
            network_hs = 0.0
        algo = str(entry.get("algorithm", "")).strip()
        name = str(entry.get("name", coin_id)).strip()
        block_time = COIN_BLOCK_TIME_S.get(coin_id, 600)
        result[coin_id] = {
            "price_usd": price,
            "reward_block": reward_block,
            "network_hashrate": network_hs,
            "block_time_s": block_time,
            "algorithm": algo,
            "name": name,
        }
    missing = [c for c in (params.split(",")) if c and c not in result]
    if missing:
        raise MinerstatError(f"minerstat response missing requested coins: {missing}")
    return result


def load_minerstat_snapshot():
    """Load the minerstat snapshot from disk into MINERSTAT_SNAPSHOT. Silent
    no-op if the file doesn't exist (first run) or is corrupted. Called once
    at startup and after every save as a cheap consistency check."""
    if not os.path.exists(MINERSTAT_FILE):
        return
    try:
        with open(MINERSTAT_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with state.minerstat_lock:
        state.MINERSTAT_SNAPSHOT.clear()
        state.MINERSTAT_SNAPSHOT.update(data)


def save_minerstat_snapshot(coins, api_calls_increment=1):
    """Persist a fresh coin snapshot to disk. Handles monthly rollover of the
    api_calls_this_month counter: if the current YYYY-MM differs from the
    saved one, the counter resets before the increment.

    Args:
        coins: dict from fetch_minerstat_coins().
        api_calls_increment: how many API calls this save represents. Usually
                             1 (one fetch = one save). Pass 0 if you're
                             rewriting the snapshot without having called
                             minerstat (e.g. schema migration).

    Updates the in-memory MINERSTAT_SNAPSHOT and writes to disk. Acquires
    minerstat_lock internally."""
    now = datetime.now(UTC)
    captured_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    current_month = now.strftime("%Y-%m")
    with state.minerstat_lock:
        prev_month = state.MINERSTAT_SNAPSHOT.get("last_poll_month")
        prev_calls = int(state.MINERSTAT_SNAPSHOT.get("api_calls_this_month", 0) or 0)
        if prev_month != current_month:
            prev_calls = 0
        payload = {
            "captured_at": captured_at,
            "last_poll_month": current_month,
            "api_calls_this_month": prev_calls + int(api_calls_increment),
            "coins": dict(coins),
        }
        _atomic_json_write(MINERSTAT_FILE, payload)
        state.MINERSTAT_SNAPSHOT.clear()
        state.MINERSTAT_SNAPSHOT.update(payload)
    return payload


def get_minerstat_snapshot_copy():
    """Return a deep copy of the current snapshot for safe reads outside the
    lock. Engines call this once per scoring session (not per-cell)."""
    with state.minerstat_lock:
        return copy.deepcopy(dict(state.MINERSTAT_SNAPSHOT))


class MinerstatScheduler(threading.Thread):
    """Fleet-wide scheduler that fires the profit recompute on the operator-
    configured day of the month. Designed to play nice with the 100-call/day
    free-tier limit: one fetch per scheduled day, rate-limited to once per
    calendar day by checking the snapshot's captured_at.

    Wakes hourly and decides whether today's schedule has already been
    honored. When MINERSTAT_POLL_DAY == 0 the whole mechanism is inert —
    operators use manual "Fetch now" / "Recompute & Apply" instead.

    Auto-applies on scheduled days per the plan: the operator configuring a
    poll day is explicit consent for unattended rebalancing on that day,
    which should coincide with their electric-billing cycle reset."""

    def __init__(self, mgr):
        super().__init__(daemon=True)
        self.mgr = mgr
        self.running = False
        self._wake = threading.Event()

    def stop(self):
        self.running = False
        self._wake.set()

    def run(self):
        self.running = True
        while self.running:
            try:
                self._tick()
            except Exception as e:
                logger.exception("[minerstat-scheduler] tick failed: %s", e)
            # Wake hourly so operators can change the poll-day config without
            # a process restart and have it take effect within an hour.
            self._wake.wait(timeout=3600)
            self._wake.clear()

    def _tick(self):
        poll_day = int(state.CONFIG["fleet_ops"].get("MINERSTAT_POLL_DAY", 0) or 0)
        if poll_day <= 0:
            return
        now = datetime.now(UTC)
        if now.day != poll_day:
            return
        # Has today's fetch already happened? Compare the snapshot's
        # captured_at day to today.
        snap = get_minerstat_snapshot_copy()
        last_ts = snap.get("captured_at") if snap else None
        if last_ts:
            try:  # noqa: SIM105
                last_day = last_ts[:10]  # "YYYY-MM-DD"
                today = now.strftime("%Y-%m-%d")
                if last_day == today:
                    return  # already fired today
            except Exception:
                pass
        logger.info("[minerstat-scheduler] poll day %d reached — fetching and applying", poll_day)
        # Build coin list + fetch — single fleet coin (MINERSTAT_* is fleet-only).
        with state.config_lock:
            coin = (state.CONFIG["fleet_ops"].get("MINERSTAT_COIN", "BTC") or "BTC").upper()
            api_key = state.CONFIG["fleet_ops"].get("MINERSTAT_API_KEY", "") or ""
            fleet_ips = list(state.CONFIG["fleet_ops"].get("MINER_IPS", []))
        coins = {coin} if coin else set()
        if not coins or not fleet_ips:
            return
        try:
            result = fetch_minerstat_coins(sorted(coins), api_key=api_key)
            save_minerstat_snapshot(result, api_calls_increment=1)
        except MinerstatError as e:
            logger.exception(
                "[minerstat-scheduler] fetch failed: %s — will retry on next hourly wake", e
            )
            return
        # Compute preview + auto-apply via shared helper. Same path the
        # manual "Fetch now" handler uses — single source of truth.
        from tuner_app.profit.auto_apply import apply_profit_recompute

        summary = apply_profit_recompute(self.mgr, ips=fleet_ips)
        logger.info(
            "[minerstat-scheduler] auto-apply done: %d applied, %d skipped, %d failed",
            summary["applied"],
            summary["skipped"],
            len(summary["failures"]),
        )
