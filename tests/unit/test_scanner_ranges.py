"""Unit tests for tuner_app.scanner.ranges.parse_ip_ranges."""

from __future__ import annotations

import ipaddress
import unittest

from tuner_app.scanner.ranges import parse_ip_ranges


class TestParseIpRanges(unittest.TestCase):
    # (a) CIDR /30 golden expansion — all 4 addresses
    def test_cidr_slash30(self):
        result = parse_ip_ranges(["192.0.2.0/30"])
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0], ipaddress.IPv4Address("192.0.2.0"))
        self.assertEqual(result[-1], ipaddress.IPv4Address("192.0.2.3"))

    # (b) Dash-range golden expansion
    def test_dash_range(self):
        result = parse_ip_ranges(["192.0.2.10-192.0.2.12"])
        self.assertEqual(len(result), 3)
        self.assertIn(ipaddress.IPv4Address("192.0.2.10"), result)
        self.assertIn(ipaddress.IPv4Address("192.0.2.11"), result)
        self.assertIn(ipaddress.IPv4Address("192.0.2.12"), result)

    # (c) Mixed list
    def test_mixed_list(self):
        result = parse_ip_ranges(["192.0.2.0/30", "192.168.7.1-192.168.7.2"])
        self.assertEqual(len(result), 6)

    # (d) Bad CIDR raises ValueError with row prefix
    def test_bad_cidr_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_ip_ranges(["not-a-cidr"])
        self.assertIn("row 0", str(ctx.exception))

    # (e) Reverse range raises
    def test_reverse_range_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_ip_ranges(["192.0.2.50-192.0.2.10"])
        self.assertIn("row 0", str(ctx.exception))
        self.assertIn("reverse", str(ctx.exception))

    # (f) IPv6 CIDR raises
    def test_ipv6_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_ip_ranges(["::1/128"])
        self.assertIn("IPv6", str(ctx.exception))

    # (g) Cap exceeded raises
    def test_cap_exceeded(self):
        # /16 has 65536 addresses, but adding one more should fail
        with self.assertRaises(ValueError) as ctx:
            parse_ip_ranges(["192.168.0.0/16", "10.0.0.1"])
        self.assertIn("65536", str(ctx.exception))

    # (h) Duplicate de-duplication
    def test_dedup(self):
        result = parse_ip_ranges(["192.0.2.1", "192.0.2.1", "192.0.2.1"])
        self.assertEqual(len(result), 1)

    # (i) Sorted output
    def test_sorted(self):
        result = parse_ip_ranges(["192.0.2.5", "192.0.2.1", "192.0.2.3"])
        self.assertEqual(result, sorted(result))

    # Empty input returns empty list
    def test_empty_input(self):
        self.assertEqual(parse_ip_ranges([]), [])

    # Single IP works
    def test_single_ip(self):
        result = parse_ip_ranges(["192.0.2.100"])
        self.assertEqual(result, [ipaddress.IPv4Address("192.0.2.100")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
