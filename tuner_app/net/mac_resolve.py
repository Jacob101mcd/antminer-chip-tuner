"""
MAC address resolution utilities.

Provides resolve_mac() to look up the MAC address for a given IP
(via /proc/net/arp first, then subprocess `arp -n` fallback), and
synthesize_mac_id() to generate a synthetic identifier when no real
MAC is available (e.g., L3-isolated miners across a router boundary).

Used by the scanner to identify miners by their stable hardware MAC
rather than by DHCP-assignable IP, so per-miner data persists across
IP renumbering.
"""

from __future__ import annotations

import re
import secrets
import subprocess

_MAC_RE = re.compile(r"[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}")
_ZEROS_MAC = "00:00:00:00:00:00"


def _normalize_mac(raw: str) -> str | None:
    """Lowercase and colon-normalize a MAC string; return None if all-zeros placeholder."""
    normalized = raw.lower().replace("-", ":")
    if normalized == _ZEROS_MAC:
        return None
    return normalized


def _parse_proc_arp(ip: str) -> tuple[str | None, bool]:
    """Parse /proc/net/arp for the given IP.

    Returns (mac_or_none, found):
      - (mac, True)   IP found, valid MAC
      - (None, True)  IP found but incomplete/zeros placeholder
      - (None, False) IP absent from table

    Raises OSError / FileNotFoundError on read failure; callers fall through.
    """
    with open("/proc/net/arp") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 4:
                continue
            row_ip = parts[0]
            if row_ip == "IP":
                continue
            if row_ip != ip:
                continue
            hw_addr = parts[3]
            if hw_addr == "<incomplete>":
                return None, True
            if _MAC_RE.fullmatch(hw_addr):
                return _normalize_mac(hw_addr), True
            return None, True
    return None, False


def _parse_arp_stdout(ip: str, stdout: str) -> str | None:
    """Scan `arp -n` stdout for a line whose IP token equals *ip* and extract its MAC.

    Uses a word-boundary regex (rejecting digit/dot continuation) so that a
    query for `192.0.2.1` does not match a row for `192.0.2.122`.
    """
    _ip_re = re.compile(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])")
    for line in stdout.splitlines():
        if not _ip_re.search(line):
            continue
        if "<incomplete>" in line:
            return None
        match = _MAC_RE.search(line)
        if match:
            return _normalize_mac(match.group(0))
    return None


def resolve_mac(ip: str, source_ip: str | None = None) -> str | None:
    """Resolve the MAC address for *ip* via the OS ARP table.

    Strategy:
      1. Read /proc/net/arp (Linux kernel ARP cache).
         - If IP present with a valid non-placeholder MAC, return it.
         - If IP present but <incomplete> or all-zeros, return None
           (no fallback — the kernel knows the IP isn't responding).
         - If IP absent, fall through to subprocess.
         - On any read error, fall through to subprocess.
      2. Run `arp -n <ip>` with a 2.0s timeout and parse stdout.
         - Returns the normalized MAC, or None on parse failure / placeholder /
           subprocess error.

    The *source_ip* parameter is reserved for future multi-NIC selection;
    it has no observable effect in this version.

    Never raises.
    """
    try:
        mac, found = _parse_proc_arp(ip)
        if found:
            return mac
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["arp", "-n", ip],
            capture_output=True,
            text=True,
            timeout=2.0,
            shell=False,
        )
        return _parse_arp_stdout(ip, result.stdout)
    except Exception:
        return None


def synthesize_mac_id(ip: str) -> str:
    """Return a synthetic identifier for miners where ARP can't resolve a real MAC.

    Format: ``syn-<ip-with-dots-as-dashes>-<8 lowercase hex chars>``
    Example: ``syn-192-0-2-122-a1b2c3d4``

    The 8-char hex suffix is random per call (`secrets.token_hex(4)`).

    NOTE: The returned value is NOT stable across calls — each invocation generates
    a new random suffix. Callers MUST persist the result on first synthesis and reuse
    the stored value on all subsequent references for the same IP. The intended
    contract is for the scanner to call this exactly once per first-sighted IP that
    lacks ARP resolution, persist the synth id into MINER_CONFIGS, and on later
    probes look up the existing entry by IP rather than re-synthesizing.

    Never raises.
    """
    try:
        return f"syn-{ip.replace('.', '-')}-{secrets.token_hex(4)}"
    except Exception:
        return f"syn-error-{secrets.token_hex(4)}"
