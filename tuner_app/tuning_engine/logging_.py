"""Per-engine JSONL logging: in-memory deque + best-effort disk append + rotation."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

from tuner_app.constants import _miner_data_path
from tuner_app.http_server.handlers.status_routes import format_log_entry
from tuner_app.privacy import redact_text, sanitize

_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


def _should_emit_to_stdout(entry: dict, threshold: str) -> bool:
    entry_level = entry.get("level", "INFO")
    entry_level_rank = _LEVEL_RANK.get(entry_level.upper(), 1)
    threshold_rank = _LEVEL_RANK.get(threshold.upper(), 1)
    return entry_level_rank >= threshold_rank


logger = logging.getLogger(__name__)


def log(engine, msg, level: str = "INFO"):
    ts = time.time()
    msg = redact_text(msg)
    level = level.upper()
    if level not in {"DEBUG", "INFO", "WARN", "ERROR"}:
        level = "INFO"

    dedup_window = engine.config.get("LOG_DEDUP_WINDOW_SEC", 5)
    if dedup_window > 0:
        # Suppress exact duplicate within the dedup window.
        if (
            engine._log_dedup_msg == str(msg)
            and engine._log_dedup_level == level
            and engine._log_dedup_first_ts is not None
            and ts - engine._log_dedup_first_ts <= dedup_window
        ):
            engine._log_dedup_count += 1
            return  # dropped — counted but not written to JSONL or stdout

        # Window expired or different msg — flush the suppressed-count entry first.
        if engine._log_dedup_count > 0:
            suppressed_entry = {
                "ts": ts,
                "voltage_mv": engine.current_sweep_voltage_mv,
                "phase": engine.phase,
                "msg": (
                    f"(suppressed {engine._log_dedup_count} duplicate(s):"
                    f" {engine._log_dedup_msg!r})"
                ),
                "level": "INFO",
                "firmware_type": engine.firmware_type,
            }
            suppressed_entry = sanitize(suppressed_entry)
            engine.log_lines.append(suppressed_entry)
            if _should_emit_to_stdout(
                suppressed_entry, engine.config.get("LOG_STDOUT_LEVEL", "INFO")
            ):
                print(f"[{engine.ip}] {format_log_entry(suppressed_entry)}", flush=True)
            if not engine._destroyed:
                try:
                    path = _miner_data_path(engine.mac, ".log.jsonl")
                    with engine.log_file_lock:
                        with open(path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(suppressed_entry, separators=(",", ":")) + "\n")
                        engine._log_appends_since_rotate_check += 1
                        if (
                            engine._log_appends_since_rotate_check
                            >= engine.LOG_ROTATE_CHECK_INTERVAL
                        ):
                            engine._log_appends_since_rotate_check = 0
                            _rotate_log_if_needed_locked(engine, path)
                except Exception as exc:
                    logger.error("[%s] log-write failed (%s)", engine.ip, type(exc).__name__)

    # Proceed with the new entry.
    entry = {
        "ts": ts,
        "voltage_mv": engine.current_sweep_voltage_mv,
        "phase": engine.phase,
        "msg": str(msg),
        "level": level,
        "firmware_type": engine.firmware_type,
    }
    entry = sanitize(entry)
    engine.log_lines.append(entry)
    if _should_emit_to_stdout(entry, engine.config.get("LOG_STDOUT_LEVEL", "INFO")):
        print(f"[{engine.ip}] {format_log_entry(entry)}", flush=True)
    # Don't resurrect a log file we're about to delete (or have already
    # deleted) — destroy() is called before file deletion in
    # _delete_profile_for_ip / /tuner/miners/remove.
    if engine._destroyed:
        # Still update dedup state so next log() call after un-destroy sees a
        # clean window. (Destroy is not truly permanent for retune scenarios.)
        engine._log_dedup_msg = str(msg)
        engine._log_dedup_level = level
        engine._log_dedup_first_ts = ts
        engine._log_dedup_count = 0
        return
    # Best-effort disk append — never let a log write kill the engine.
    try:
        path = _miner_data_path(engine.mac, ".log.jsonl")
        with engine.log_file_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            engine._log_appends_since_rotate_check += 1
            if engine._log_appends_since_rotate_check >= engine.LOG_ROTATE_CHECK_INTERVAL:
                engine._log_appends_since_rotate_check = 0
                _rotate_log_if_needed_locked(engine, path)
    except Exception as exc:
        logger.error("[%s] log-write failed (%s)", engine.ip, type(exc).__name__)
    # Update dedup state after successfully emitting the entry.
    engine._log_dedup_msg = str(msg)
    engine._log_dedup_level = level
    engine._log_dedup_first_ts = ts
    engine._log_dedup_count = 0


def load_log_from_disk(engine):
    """Populate self.log_lines from the JSONL file, keeping at most
    LOG_LINES_MAX_CAP most-recent entries. Silently ignores malformed lines
    (e.g. truncated final line from a crash)."""
    path = _miner_data_path(engine.mac, ".log.jsonl")
    if not os.path.exists(path):
        return
    try:
        with engine.log_file_lock, open(path, encoding="utf-8") as f:
            lines = f.readlines()
        # Deque's maxlen enforces the cap as we fill it; no slicing needed.
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and "msg" in entry:
                engine.log_lines.append(sanitize(entry))
    except Exception as exc:
        logger.error("[%s] log-load failed (%s)", engine.ip, type(exc).__name__)


def _rotate_log_if_needed_locked(engine, path):
    """Called with self.log_file_lock held. Trims the on-disk file back
    to LOG_ROTATE_TARGET lines when it exceeds LOG_LINES_MAX_CAP."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= engine.LOG_LINES_MAX_CAP:
            return
        tail = lines[-engine.LOG_ROTATE_TARGET :]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("[%s] log-rotate failed (%s)", engine.ip, type(exc).__name__)


def clear_log_entries_for_voltage(engine, voltage_mv):
    """Strip all persisted + in-memory log entries tagged with voltage_mv.
    Called by /tuner/retune_voltage so each retune starts with a clean log
    for that step."""
    path = _miner_data_path(engine.mac, ".log.jsonl")
    engine.log_lines = deque(
        (e for e in engine.log_lines if e.get("voltage_mv") != voltage_mv),
        maxlen=engine.LOG_LINES_MAX_CAP,
    )
    if not os.path.exists(path):
        return
    try:
        with engine.log_file_lock:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            kept = []
            for line in lines:
                s = line.strip()
                if not s:
                    continue
                try:
                    entry = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("voltage_mv") == voltage_mv:
                    continue
                if isinstance(entry, dict):
                    kept.append(json.dumps(sanitize(entry), separators=(",", ":")) + "\n")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, path)
    except Exception as exc:
        logger.error("[%s] log-clear-voltage failed (%s)", engine.ip, type(exc).__name__)
