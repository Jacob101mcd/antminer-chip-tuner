"""Per-IP bulk operations: profile reset (scope-aware), file deletion, and the
generic bulk action runner used by /tuner/bulk/* endpoints.

The helpers accept canonical MACs while retaining legacy IP lookup for migrated
callers. File-path operations cover the v4 per-platform shape
({mac}.{firmware}.{ext}) for every supported firmware type and legacy IP-keyed
files so old or hand-edited orphans can be cleaned up.
"""

from __future__ import annotations

import contextlib
import logging
import os

from tuner_app import state
from tuner_app.config.effective import canonical_miner_key
from tuner_app.config.persistence import save_config_to_disk
from tuner_app.constants import (
    _PLATFORMS,
    DATA_DIR,
    RESET_SCOPES,
    _mac_for_filename,
    _miner_data_path,
    _miner_platform_path,
)

logger = logging.getLogger(__name__)


def _delete_per_platform_tuning_files(mac, suffixes=(".profile.json", ".checkpoint.json")):
    """Remove per-platform tuning files for *mac* across every firmware variant.

    Used by the full-reset and remove paths so a reflashed-then-removed miner
    doesn't leave behind orphan profiles / checkpoints under the old firmware.
    The ``.stock.json`` is omitted from the default suffixes so partial resets
    preserve the one-shot factory baseline (use the explicit ``stock=True``
    arg in callers that want it gone).
    """
    for fw in _PLATFORMS:
        for suffix in suffixes:
            try:
                path = _miner_platform_path(mac, fw, suffix)
            except (TypeError, ValueError):
                # Legacy fallback — IP-keyed mac fails MAC validation
                legacy_suffix = {".profile.json": ".json"}.get(suffix, suffix)
                path = _miner_data_path(mac, legacy_suffix)
            if os.path.exists(path):
                os.remove(path)


def _delete_legacy_ip_keyed_files(ip):
    """Best-effort sweep for any orphan files still under the pre-A5 IP-keyed
    naming scheme. A clean fleet post-migration will have none of these, but
    the sweep guarantees idempotent cleanup for hand-restored backups.
    """
    for suffix in (".json", ".checkpoint.json", ".stock.json", ".log.jsonl"):
        path = _miner_data_path(ip, suffix)
        if os.path.exists(path):
            os.remove(path)


def _delete_profile_for_ip(ip, scope="all"):
    """Scope-aware reset. `all` matches the historical behavior — stop the
    engine, delete profile + checkpoint + log, replace the engine with a
    fresh instance. The three partial scopes keep progressively more of the
    existing tune so the next Start Tuning resumes at the correct phase:

        chip              — keep Phase V surface + baseline; redo Phase 3/3b/4
        chip_fine         — keep coarse surface + baseline; redo fine + Phase 3/3b/4
        chip_fine_coarse  — keep baseline + parked_chips; redo Phase V onwards

    For every partial scope we rewrite the checkpoint after mutating the
    engine — a process restart before the next Start Tuning sees the
    scope-correct state because `_restore_saved_state` reloads from disk.
    Always preserves the .stock.json file (captured once per physical
    miner; use /tuner/reset_stock to re-capture).
    """
    from tuner_app.main import manager
    from tuner_app.tuning_engine.reset import (
        reset_chip_tuning_fields,
        reset_coarse_grid_fields,
        reset_fine_grid_fields,
    )

    if scope not in RESET_SCOPES:
        raise ValueError(f"invalid reset scope: {scope!r}")

    old_engine = manager.peek_engine(ip)

    if scope == "all" or old_engine is None:
        # Full reset (or no engine yet — fall through so the caller still
        # gets a clean slate). destroy() (not stop()) latches _destroyed=True
        # BEFORE we delete files, so any orphan-thread _save_checkpoint() /
        # _save_profile() / log() that fires after our join(timeout=5) returns
        # is a no-op instead of resurrecting the deleted state. Without this,
        # the orphan thread can complete its current measurement (sample loops
        # only check self.running every 5–10 s) and write a fresh checkpoint
        # AFTER we've deleted the previous one — which then loads back into
        # the dashboard on the next process restart.
        if old_engine:
            old_engine.destroy()
            if old_engine.thread and old_engine.thread.is_alive():
                old_engine.thread.join(timeout=5)
            mac = old_engine.mac
        else:
            mac = canonical_miner_key(ip)
        # Per-platform profile + checkpoint for every firmware variant — a
        # reflashed-then-reset miner shouldn't keep its prior firmware's
        # tuning state. Stock baseline is preserved (use /tuner/reset_stock).
        _delete_per_platform_tuning_files(mac)
        # Cross-platform log lives at {mac}.log.jsonl
        log_path = _miner_data_path(mac, ".log.jsonl")
        if os.path.exists(log_path):
            os.remove(log_path)
        # Legacy IP-keyed orphan sweep (idempotent for clean fleets).
        _delete_legacy_ip_keyed_files(ip)
        manager.reset_engine(ip)
        return

    # Partial reset: keep the engine instance (we mutate it in place so
    # num_boards / chips_per_board / stock_baseline survive without
    # re-derivation, and write a new scope-correct checkpoint at the end).
    # stop() rather than destroy() so the post-mutation _save_checkpoint can
    # actually persist.
    old_engine.stop()
    if old_engine.thread and old_engine.thread.is_alive():
        old_engine.thread.join(timeout=5)

    with old_engine._control_lock:
        reset_chip_tuning_fields(old_engine)
        if scope in ("chip_fine", "chip_fine_coarse"):
            reset_fine_grid_fields(old_engine)
        if scope == "chip_fine_coarse":
            reset_coarse_grid_fields(old_engine)
        # Profile file represents a completed tune. Any partial reset means
        # the tune isn't complete anymore — delete it (current firmware only,
        # since partial resets preserve other firmwares' tuning data).
        from tuner_app.tuning_engine.persistence import profile_path

        prof = profile_path(old_engine)
        if os.path.exists(prof):
            os.remove(prof)
        # Persist the mutated state so a process restart replays correctly.
        old_engine._save_checkpoint()
        old_engine.log(
            f"Partial reset ({scope}): saved checkpoint reflects "
            f"the reduced state. Next Start Tuning will resume at "
            f"the scope-appropriate phase."
        )


def _delete_all_miner_files_for_mac(mac, ip=None):
    """Removes ALL on-disk state for a miner — per-platform profile +
    checkpoint + stock baseline for every firmware variant, plus the
    cross-platform log. Used by /tuner/miners/remove and /tuner/bulk/remove
    so a re-added miner starts fresh without resurrecting orphan state.

    *ip* is optional; when provided, also sweeps any legacy IP-keyed orphan
    files (idempotent for clean fleets, useful when removing a miner whose
    pre-A5 files survived migration).
    """
    _delete_per_platform_tuning_files(
        mac, suffixes=(".profile.json", ".checkpoint.json", ".stock.json")
    )
    log_path = _miner_data_path(mac, ".log.jsonl")
    if os.path.exists(log_path):
        os.remove(log_path)
    if ip:
        _delete_legacy_ip_keyed_files(ip)


# Backward-compat shim: pre-A9 callers used the IP-named helper. Keep the
# symbol so external scripts referencing it (none in-tree as of A9) don't
# break, and route through the canonical MAC-aware path.
def _delete_all_miner_files_for_ip(ip):
    """Deprecated alias preserved for back-compat. Translates IP → MAC and
    routes through ``_delete_all_miner_files_for_mac``.
    """
    mac = canonical_miner_key(ip)
    _delete_all_miner_files_for_mac(mac, ip=ip)


def _remove_miner(manager, identifier):
    """Atomically remove a miner: drop it from MINER_IPS / MINER_CONFIGS,
    persist config, destroy + join the engine, and wipe all per-miner files.

    *identifier* may be a MAC (v4 canonical key — preferred), an IP (legacy /
    transitional callers), or a synth ID. Internally resolved to MAC via
    ``canonical_miner_key`` so a v3 fallback path still works in tests that
    inject IP-keyed entries.

    Shared by single-miner /tuner/miners/remove and /tuner/bulk/remove so the
    fresh-state guarantee is identical on both paths.

    destroy() (not stop()) latches _destroyed=True BEFORE we delete files, so
    any orphan-thread _save_checkpoint() / _save_profile() / log() that fires
    after our join(timeout=5) returns is a no-op instead of resurrecting the
    deleted state. Without this, the orphan thread can complete its current
    measurement (sample loops only check self.running every 5–10 s) and write
    a fresh checkpoint AFTER we've deleted the previous one.
    """
    mac = canonical_miner_key(identifier)
    with state.config_lock:
        # Resolve the per-miner IP from the v4 entry so we can remove it
        # from MINER_IPS and sweep any orphan IP-keyed files. Falls back to
        # the identifier itself when the v4 entry has no ``ip`` field
        # (test fixtures injecting flat v3 entries by IP) so the legacy
        # path still cleans up correctly.
        prior_entry = state.MINER_CONFIGS.get(mac, {}) if isinstance(mac, str) else {}
        miner_ip = (prior_entry.get("ip") if isinstance(prior_entry, dict) else None) or (
            identifier if identifier != mac else None
        )
        if miner_ip and miner_ip in state.CONFIG["fleet_ops"]["MINER_IPS"]:
            state.CONFIG["fleet_ops"]["MINER_IPS"].remove(miner_ip)
        state.MINER_CONFIGS.pop(mac, None)
        save_config_to_disk()
    old_engine = manager.pop_engine(mac)
    if old_engine:
        old_engine.destroy()
        if old_engine.thread and old_engine.thread.is_alive():
            old_engine.thread.join(timeout=5)
    _delete_all_miner_files_for_mac(mac, ip=miner_ip)


def _rekey_miner(old_mac: str, new_mac: str, *, manager) -> dict:
    """Re-key a miner from old_mac to new_mac.

    Used by the operator-driven /tuner/miners/set_mac HTTP handler and (in
    Unit 6) the scanner's opportunistic synth-to-real upgrade path. Mutates
    state.MINER_CONFIGS, the engine registry, and on-disk per-platform files
    in one coordinated step.

    Returns ``{"renamed": list[str], "engine_rekeyed": bool, "noop": bool}``.

    Raises ``ValueError`` when ``new_mac`` is already registered in
    MINER_CONFIGS — caller maps to HTTP 409 or its own error path.

    Idempotent paths (return noop=True with empty renamed/no engine work):
      - ``old_mac == new_mac`` (self re-key).
      - ``old_mac`` not in MINER_CONFIGS (concurrent re-key already won).

    Lock discipline: state.config_lock is held ONLY for the dict mutation +
    save_config_to_disk(). It is RELEASED before manager.pop_engine and the
    manager._lock acquisition for the engine re-key. File rename happens
    last, lock-free. The two locks (config_lock, manager._lock) are NEVER
    held simultaneously, preserving the module's lock-order invariant.
    """
    if old_mac == new_mac:
        return {"renamed": [], "engine_rekeyed": False, "noop": True}

    with state.config_lock:
        if old_mac not in state.MINER_CONFIGS:
            return {"renamed": [], "engine_rekeyed": False, "noop": True}
        if new_mac in state.MINER_CONFIGS:
            raise ValueError(f"target MAC {new_mac} already exists")
        entry = state.MINER_CONFIGS.pop(old_mac)
        entry["id_synthesized"] = False
        state.MINER_CONFIGS[new_mac] = entry
        save_config_to_disk()
    # Lock released — engine + file work happens here.

    engine_rekeyed = False
    old_engine = manager.pop_engine(old_mac)
    if old_engine is not None:
        with contextlib.suppress(AttributeError):
            old_engine.mac = new_mac
        with manager._lock:
            manager.engines[new_mac] = old_engine
        engine_rekeyed = True

    try:
        old_dash = _mac_for_filename(old_mac)
    except (TypeError, ValueError):
        old_dash = None
    try:
        new_dash = _mac_for_filename(new_mac)
    except (TypeError, ValueError):
        new_dash = None

    renamed: list[str] = []
    if old_dash and new_dash and os.path.isdir(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if fname.startswith(old_dash + "."):
                src = os.path.join(DATA_DIR, fname)
                dst = os.path.join(DATA_DIR, new_dash + fname[len(old_dash) :])
                try:
                    os.replace(src, dst)
                    renamed.append(fname)
                except OSError as ex:
                    logger.warning("rekey_miner: failed to rename %s -> %s: %s", src, dst, ex)

    return {"renamed": renamed, "engine_rekeyed": engine_rekeyed, "noop": False}


def _make_remove_action(manager):
    """Return a per-IP closure that calls `_remove_miner(manager, ip)` and
    reports a `{removed: True}` detail. Used by /tuner/bulk/remove via
    `_bulk_run`. Per-IP exceptions surface as the standard bulk-result error
    string; one bad IP doesn't abort the batch."""

    def action(ip):
        _remove_miner(manager, ip)
        return {"removed": True}

    return action


def _bulk_run(ips, action_fn):
    """Run `action_fn(ip)` for each ip in `ips`, collecting per-IP results.

    Always returns the shape: {results: {ip: {ok, error, detail}}, summary: {...}}.
    Actions can raise — the exception is caught and surfaced as a per-IP error
    so one bad IP doesn't abort the whole batch.
    """
    results = {}
    succeeded = 0
    failed = 0
    seen = set()
    for ip in ips:
        if not isinstance(ip, str) or not ip.strip():
            continue
        ip = ip.strip()
        if ip in seen:
            continue
        seen.add(ip)
        try:
            detail = action_fn(ip)
            results[ip] = {"ok": True, "error": None, "detail": detail}
            succeeded += 1
        except Exception as ex:
            results[ip] = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "detail": None}
            failed += 1
    return {
        "results": results,
        "summary": {"total": len(results), "succeeded": succeeded, "failed": failed},
    }
