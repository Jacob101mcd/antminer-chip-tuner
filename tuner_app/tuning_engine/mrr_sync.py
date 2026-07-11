"""
MRR auto-publish state flips.
"""

from __future__ import annotations

import os
import time

from tuner_app.constants import MRR_STRATUM_PASSWORD, MRR_STRATUM_POOLS
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError, MRRError
from tuner_app.mrr.client import MRRClient
from tuner_app.mrr.helpers import is_rig_rented
from tuner_app.tuning_engine import persistence as _engine_persistence


def mrr_set_last_sync(engine, intent, rig_id, result, reason="", **extra):
    """Build and persist mrr_last_sync. Best-effort profile save so the
    dashboard shows the latest sync state on restart. Never raises."""
    payload = {
        "intent": intent,
        "rig_id": int(rig_id or 0),
        "result": result,
        "reason": reason,
        "ts": time.time(),
    }
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    engine.mrr_last_sync = payload
    # Persist so a process restart surfaces the last sync on the dashboard
    # without waiting for the next natural save point.
    try:
        if engine.tuning_complete and os.path.exists(_engine_persistence.profile_path(engine)):
            engine._save_profile()
        elif os.path.exists(_engine_persistence.checkpoint_path(engine)):
            # Avoid spamming log from inside _save_checkpoint (which logs
            # its own "Checkpoint saved" line) — keep it silent here by
            # calling the atomic writer directly on the minimum shape we
            # need. Falling back to _save_checkpoint is fine but noisy.
            pass
    except Exception:
        pass


def mrr_sync(engine, intent, reason=""):
    """Push current engine state to MiningRigRentals. Always a no-op
    when MRR is disabled or not configured for this miner. Errors are
    logged and stashed in mrr_last_sync — never propagate into the
    tuning thread.

    Args:
        intent: one of "tuning", "maintaining", "stopped", "error".
                "maintaining" → status=enabled + push advertised hashrate
                (sweep_hashrate_ths × (1 + modifier_pct/100)).
                Any other intent → status=disabled, hashrate unchanged.
        reason: short human-readable string for the log + last_sync record.
    """
    try:
        if not engine.config.get("MRR_ENABLED", False):
            return
        rig_id = int(engine.config.get("MRR_RIG_ID", 0) or 0)
        if rig_id <= 0:
            return
        api_key = str(engine.config.get("MRR_API_KEY", "") or "").strip()
        api_secret = str(engine.config.get("MRR_API_SECRET", "") or "").strip()
        if not api_key or not api_secret:
            now = time.time()
            if now - (engine._mrr_last_warn_ts or 0) > 3600:
                engine.log(f"MRR: rig #{rig_id} configured but credentials missing — sync skipped")
                engine._mrr_last_warn_ts = now
            engine._mrr_set_last_sync(
                intent, rig_id, "skipped", reason=reason, error="credentials not configured"
            )
            return

        client = MRRClient(api_key, api_secret)

        # Rented-rig guard — don't touch config mid-rental. MRR returns
        # `status.rented=true` (or available_status="rented") while a
        # rental is active; flipping to disabled then would strand the
        # renter.
        try:
            rig = client.get_rig(rig_id)
        except MRRError as e:
            engine.log(f"MRR: get_rig({rig_id}) failed: {e}")
            engine._mrr_set_last_sync(intent, rig_id, "error", reason=reason, error=str(e))
            return
        if is_rig_rented(rig):
            engine.log(f"MRR: rig #{rig_id} is currently rented — skipping {intent} sync")
            engine._mrr_set_last_sync(intent, rig_id, "skipped_rented", reason=reason, rented=True)
            return

        # Map intent → target status + advertised hashrate.
        unit = str(engine.config.get("MRR_HASHRATE_UNIT", "th") or "th").lower()
        if intent == "maintaining":
            target_status = "enabled"
            try:
                modifier = float(engine.config.get("MRR_HASHRATE_MODIFIER_PCT", 0.0) or 0.0)
            except (TypeError, ValueError):
                modifier = 0.0
            try:
                base_ths = float(engine.sweep_hashrate_ths or 0.0)
            except (TypeError, ValueError):
                base_ths = 0.0
            if base_ths <= 0:
                # No stable hashrate reference yet — refuse to push
                # "enabled" at 0 TH/s (which would advertise a broken
                # rig). Skip instead; the next sync trigger will retry.
                engine.log(
                    "MRR: sweep_hashrate_ths unavailable; cannot push advertised rate — skipping"
                )
                engine._mrr_set_last_sync(
                    intent, rig_id, "skipped", reason=reason, error="sweep_hashrate_ths=0"
                )
                return
            advertised = base_ths * (1.0 + modifier / 100.0)
        else:
            target_status = "disabled"
            advertised = None

        try:
            client.update_rig(
                rig_id, status=target_status, hashrate_value=advertised, hashrate_unit=unit
            )
        except MRRError as e:
            engine.log(f"MRR: update_rig({rig_id}) failed: {e}")
            engine._mrr_set_last_sync(
                intent,
                rig_id,
                "error",
                reason=reason,
                error=str(e),
                target_status=target_status,
                advertised_ths=advertised,
                advertised_unit=unit,
            )
            return

        ads_msg = f" @ {advertised:.2f} {unit.upper()}/s" if advertised is not None else ""
        # Use '->' (ASCII) not 'U+2192 right arrow' — cp1252-encoded
        # stdout on Windows can't print the Unicode arrow and would
        # throw UnicodeEncodeError out of log()'s print call.
        engine.log(
            f"MRR: rig #{rig_id} -> {target_status}{ads_msg} ({reason})"
            if reason
            else f"MRR: rig #{rig_id} -> {target_status}{ads_msg}"
        )
        engine._mrr_set_last_sync(
            intent,
            rig_id,
            "ok",
            reason=reason,
            target_status=target_status,
            advertised_ths=advertised,
            advertised_unit=unit,
        )
    except Exception as e:
        # Safety net — never leak anything into the tuning loop.
        try:
            engine.log(f"MRR sync raised unexpectedly: {e}")
            engine._mrr_set_last_sync(intent, 0, "error", reason=reason, error=str(e))
        except Exception:
            pass


def mrr_apply_pool_config(engine, reason=""):
    """Push the 3 MRR stratum pools + coin + unique_id=False to the miner
    via POST /coin. Idempotent — the miner accepts the same config
    repeatedly without side effects, so callers can fire this from
    multiple hook points (Phase 0, rig-ID change) without worrying about
    double-application.

    No-op when MRR isn't fully configured for this miner. Never raises —
    MRR-related failures must never kill a tune.
    """
    try:
        if not engine.config.get("MRR_ENABLED", False):
            return False
        rig_id = int(engine.config.get("MRR_RIG_ID", 0) or 0)
        if rig_id <= 0:
            return False
        username = str(engine.config.get("MRR_STRATUM_USERNAME", "") or "").strip()
        if not username:
            now = time.time()
            if now - (engine._mrr_last_warn_ts or 0) > 3600:
                engine.log("MRR: MRR_STRATUM_USERNAME not set — skipping pool config push")
                engine._mrr_last_warn_ts = now
            return False
        coin = str(engine.config.get("MRR_COIN", "BTC") or "BTC").upper()
        if coin not in ("BTC", "LTC"):
            engine.log(f"MRR: unknown MRR_COIN {coin!r}; defaulting to BTC")
            coin = "BTC"
        login = f"{username}.{rig_id}"
        stratum_configs = [
            {"pool": pool, "login": login, "password": MRR_STRATUM_PASSWORD}
            for pool in MRR_STRATUM_POOLS
        ]
        try:
            engine.api.set_coin(coin=coin, stratum_configs=stratum_configs, unique_id=False)
        except (MinerCommandError, MinerOfflineError, Exception) as ex:
            engine.log(f"MRR: pool config push failed: {ex}" + (f" ({reason})" if reason else ""))
            return False
        note = f" ({reason})" if reason else ""
        engine.log(
            f"MRR: pool config applied (coin={coin}, login={login}, "
            f"3 servers, unique_id=false){note}"
        )
        return True
    except Exception as e:
        try:  # noqa: SIM105
            engine.log(f"MRR pool config raised unexpectedly: {e}")
        except Exception:
            pass
        return False
