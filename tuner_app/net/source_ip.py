"""
Network interface selection for outbound connections to miners.

Machines with multiple networks (Wi-Fi + Ethernet, VPN + physical NIC, etc.)
can have the OS routing table pick the wrong outbound interface to reach the
miner, causing silent connection failures. The helpers below let the user
override the source IP via CONFIG["SOURCE_IP"], and auto-detect by probing
local interfaces when the OS-chosen route can't reach the miner.
"""

from __future__ import annotations

import socket
import threading

from tuner_app import state

# Cache: miner_ip -> source_ip to bind ("" = OS default works)
_source_ip_cache: dict[str, str] = {}
_source_ip_cache_lock = threading.Lock()


def _list_local_ipv4_addresses() -> set[str]:
    """Return a set of non-loopback local IPv4 addresses on this host.

    Uses multiple methods to maximize coverage on Windows (which may have
    Hyper-V, WSL, Docker, and VPN virtual adapters that don't always show up
    via gethostname)."""
    addrs = set()
    # Method 1: resolve our own hostname (works on most systems)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except Exception:
        pass
    # Method 2: ask gethostbyname_ex for all aliases/addresses
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except Exception:
        pass
    # Method 3: trick the OS into telling us its preferred source for a public
    # destination — gives us at least one usable address even if hostname
    # resolution is broken.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            addrs.add(ip)
    except Exception:
        pass
    return addrs


def resolve_source_ip(miner_ip: str, miner_port: int) -> str:
    """Figure out which local IP to bind outgoing connections to.

    Returns the source IP string to bind (or "" to let the OS pick).
    Logic:
      1. If CONFIG["SOURCE_IP"] is set, use it (user override).
      2. Otherwise check cache — if we've probed for this miner before, reuse.
      3. Otherwise try OS default route; if it works, cache "".
      4. Otherwise probe each local interface and return the first that reaches
         the miner on miner_port. Cache the winner (or "" if nothing worked).
    """
    configured = ""
    try:
        with state.config_lock:
            configured = state.CONFIG["fleet_ops"].get("SOURCE_IP", "").strip()
    except Exception:
        pass
    if configured:
        return configured
    with _source_ip_cache_lock:
        if miner_ip in _source_ip_cache:
            return _source_ip_cache[miner_ip]
    # Try OS default first (fast path)
    try:
        s = socket.create_connection((miner_ip, miner_port), timeout=3)
        s.close()
        with _source_ip_cache_lock:
            _source_ip_cache[miner_ip] = ""
        return ""
    except Exception:
        pass
    # OS default failed — probe each local interface
    for src_ip in sorted(_list_local_ipv4_addresses()):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.bind((src_ip, 0))
            s.connect((miner_ip, miner_port))
            s.close()
            with _source_ip_cache_lock:
                _source_ip_cache[miner_ip] = src_ip
            print(f"[source-ip] auto-detected {src_ip} -> {miner_ip}:{miner_port}", flush=True)
            return src_ip
        except Exception:
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
            continue
    # Nothing worked — cache empty to avoid re-probing on every call
    with _source_ip_cache_lock:
        _source_ip_cache[miner_ip] = ""
    return ""


def clear_source_ip_cache(miner_ip: str | None = None) -> None:
    """Clear cached source IP resolution. Call on config change or recovery."""
    with _source_ip_cache_lock:
        if miner_ip is None:
            _source_ip_cache.clear()
        else:
            _source_ip_cache.pop(miner_ip, None)
