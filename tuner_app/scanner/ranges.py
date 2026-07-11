"""Parse IP range strings into sorted lists of IPv4Address objects.

Supports CIDR notation and dash-range notation. Rejects IPv6, malformed
entries, reverse ranges, and range lists that exceed 65,536 total IPs.
"""

from __future__ import annotations

import ipaddress


def parse_ip_ranges(items: list[str]) -> list[ipaddress.IPv4Address]:
    """Parse a list of IP range strings into a sorted, de-duped list of IPv4Address.

    Accepted formats:
      - CIDR:       '192.0.2.0/24'
      - Dash-range: '192.0.2.10-192.0.2.50'
      - Single IP:  '192.0.2.5'

    Args:
        items: List of IP range strings.

    Returns:
        Sorted list of unique IPv4Address objects.

    Raises:
        ValueError: For IPv6 input, malformed entries, reverse ranges, or if
                    total IP count exceeds 65,536.

    Example:
        >>> parse_ip_ranges(['192.0.2.0/30'])
        [IPv4Address('192.0.2.0'), IPv4Address('192.0.2.1'),
         IPv4Address('192.0.2.2'), IPv4Address('192.0.2.3')]
    """
    if not items:
        return []

    result: set[ipaddress.IPv4Address] = set()
    total_count = 0

    for i, item in enumerate(items):
        item = item.strip()
        if not item:
            continue

        # Reject IPv6 early — presence of ':' is unambiguous
        if ":" in item:
            raise ValueError(f"row {i}: IPv6 not supported")

        if "/" in item:
            # CIDR notation
            try:
                network = ipaddress.IPv4Network(item, strict=False)
            except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as exc:
                raise ValueError(f"row {i}: invalid CIDR: {exc}") from exc
            count = network.num_addresses
            if total_count + count > 65536:
                raise ValueError("total IP count exceeds 65536")
            total_count += count
            result.update(network)  # includes network + broadcast addresses
        elif "-" in item:
            # Dash-range: start-end
            parts = item.split("-", 1)
            if len(parts) != 2:
                raise ValueError(f"row {i}: invalid range format")
            try:
                start = ipaddress.IPv4Address(parts[0].strip())
                end = ipaddress.IPv4Address(parts[1].strip())
            except (ipaddress.AddressValueError, ValueError) as exc:
                raise ValueError(f"row {i}: invalid IP address in range: {exc}") from exc
            if start > end:
                raise ValueError(f"row {i}: reverse range")
            count = int(end) - int(start) + 1
            if total_count + count > 65536:
                raise ValueError("total IP count exceeds 65536")
            total_count += count
            for offset in range(count):
                result.add(ipaddress.IPv4Address(int(start) + offset))
        else:
            # Single IP
            try:
                addr = ipaddress.IPv4Address(item)
            except (ipaddress.AddressValueError, ValueError) as exc:
                raise ValueError(f"row {i}: invalid IP address: {exc}") from exc
            if addr not in result:
                if total_count + 1 > 65536:
                    raise ValueError("total IP count exceeds 65536")
                total_count += 1
                result.add(addr)

    return sorted(result)
