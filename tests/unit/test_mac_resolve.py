"""
Tests for tuner_app/net/mac_resolve.py

Spec contract:
  resolve_mac(ip, source_ip=None) -> str | None
    - Reads /proc/net/arp first; col 0 = IP, col 3 = HW address.
    - Falls back to `arp -n <ip>` subprocess (2.0 s timeout) when /proc absent,
      IP not in table, or open() raises OSError.
    - Returns canonical lowercase colon-separated MAC.
    - Normalises hyphens to colons; lowercases.
    - Returns None for <incomplete>, 00:00:00:00:00:00, IP not found, all exceptions.
    - source_ip kwarg is reserved; no observable effect on return value.

  synthesize_mac_id(ip) -> str
    - Returns "syn-<ip-with-dots-as-dashes>-<8-lowercase-hex-chars>".
    - 8-hex suffix is random per call.
    - Never raises for non-empty string input.
"""

from __future__ import annotations

import subprocess
from unittest import TestCase
from unittest.mock import MagicMock, mock_open, patch

from tuner_app.net.mac_resolve import resolve_mac, synthesize_mac_id

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ARP_TABLE = (
    "IP address       HW type     Flags       HW address            Mask     Device\n"
    "192.0.2.122    0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
    "192.0.2.1      0x1         0x2         11:22:33:44:55:66     *        eth0\n"
)

_ARP_TABLE_HYPHEN = (
    "IP address       HW type     Flags       HW address            Mask     Device\n"
    "192.0.2.122    0x1         0x2         AA-BB-CC-DD-EE-FF     *        eth0\n"
)

_ARP_TABLE_ZEROS = (
    "IP address       HW type     Flags       HW address            Mask     Device\n"
    "192.0.2.122    0x1         0x2         00:00:00:00:00:00     *        eth0\n"
)

_ARP_TABLE_INCOMPLETE = (
    "IP address       HW type     Flags       HW address            Mask     Device\n"
    "192.0.2.122    0x1         0x0         <incomplete>          *        eth0\n"
)

_LINUX_ARP_STDOUT = (
    "Address                  HWtype  HWaddress           Flags Mask            Iface\n"
    "192.0.2.122            ether   aa:bb:cc:dd:ee:ff   C                     eth0\n"
)

_LINUX_ARP_STDOUT_HYPHEN = (
    "Address                  HWtype  HWaddress           Flags Mask            Iface\n"
    "192.0.2.122            ether   AA-BB-CC-DD-EE-FF   C                     eth0\n"
)

_LINUX_ARP_STDOUT_INCOMPLETE = (
    "Address                  HWtype  HWaddress           Flags Mask            Iface\n"
    "192.0.2.122            ether   <incomplete>        C                     eth0\n"
)

_LINUX_ARP_STDOUT_ZEROS = (
    "Address                  HWtype  HWaddress           Flags Mask            Iface\n"
    "192.0.2.122            ether   00:00:00:00:00:00   C                     eth0\n"
)

# Multi-IP stdout where one IP is a prefix-substring of another. Exercises the
# substring-collision regression: a naive `if ip in line` check would return
# .122's MAC when asked for .1.
_LINUX_ARP_STDOUT_MULTI = (
    "Address                  HWtype  HWaddress           Flags Mask            Iface\n"
    "192.0.2.122            ether   aa:bb:cc:dd:ee:ff   C                     eth0\n"
    "192.0.2.1              ether   11:22:33:44:55:66   C                     eth0\n"
)


def _make_proc_result(stdout):
    """Return a mock subprocess.CompletedProcess-like object with the given stdout."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = 0
    return result


def _resolve_timeout(call_args):
    """Extract subprocess.run timeout from call_args, regardless of positional/kwarg form."""
    _args, kwargs = call_args
    if "timeout" in kwargs:
        return kwargs["timeout"]
    return None


# ---------------------------------------------------------------------------
# resolve_mac — /proc/net/arp happy path
# ---------------------------------------------------------------------------


class TestResolveMacProcArp(TestCase):
    """resolve_mac reads /proc/net/arp before attempting subprocess."""

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE))
    def test_returns_lowercase_colon_mac(self):
        """IP found in /proc/net/arp returns lowercase colon-separated MAC."""
        result = resolve_mac("192.0.2.122")
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE))
    def test_second_ip_in_table_also_resolves(self):
        """A second IP in /proc/net/arp resolves independently of the first row."""
        result = resolve_mac("192.0.2.1")
        self.assertEqual(result, "11:22:33:44:55:66")

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE_HYPHEN))
    def test_hyphenated_mac_in_proc_arp_normalized(self):
        """Hyphen-separated MAC in /proc/net/arp col 3 normalised to colon-separated lowercase."""
        result = resolve_mac("192.0.2.122")
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE_ZEROS))
    def test_all_zeros_mac_in_proc_arp_returns_none(self):
        """All-zeros MAC in /proc/net/arp col 3 is treated as a placeholder and returns None."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE_INCOMPLETE))
    def test_incomplete_entry_returns_none(self):
        """<incomplete> MAC in /proc/net/arp returns None per spec placeholder rule."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run")
    @patch("builtins.open", mock_open(read_data=_ARP_TABLE))
    def test_subprocess_not_called_when_proc_arp_hit(self, mock_run):
        """subprocess.run must NOT be called when the IP is found in /proc/net/arp."""
        resolve_mac("192.0.2.122")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_mac — /proc/net/arp miss → subprocess fallback
# ---------------------------------------------------------------------------


class TestResolveMacFallback(TestCase):
    """resolve_mac falls back to `arp -n` subprocess when /proc/net/arp misses."""

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT))
    @patch("builtins.open", mock_open(read_data=_ARP_TABLE))
    def test_ip_not_in_table_falls_back(self, mock_run):
        """When IP is absent from /proc/net/arp the subprocess fallback is invoked
        AND the result is None when the subprocess output also doesn't contain that IP."""
        result = resolve_mac("10.0.0.99")
        self.assertTrue(
            mock_run.called,
            "subprocess.run must be called when IP not found in /proc/net/arp",
        )
        self.assertIsNone(
            result,
            "IP not found in either /proc/net/arp or subprocess output must return None",
        )

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT))
    @patch("builtins.open", side_effect=OSError("no proc"))
    def test_proc_arp_oserror_falls_through_to_subprocess(self, _mock_open_, mock_run):
        """OSError opening /proc/net/arp causes fallback to subprocess and returns its MAC."""
        result = resolve_mac("192.0.2.122")
        self.assertTrue(mock_run.called, "subprocess.run must be called on OSError from open()")
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT))
    @patch("builtins.open", side_effect=FileNotFoundError("/proc/net/arp absent"))
    def test_proc_arp_filenotfound_falls_through_to_subprocess(self, _mock_open_, mock_run):
        """FileNotFoundError for /proc/net/arp causes fallback to subprocess."""
        result = resolve_mac("192.0.2.122")
        self.assertTrue(mock_run.called)
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_invoked_with_arp_command_and_timeout(self, _mock_open_, mock_run):
        """subprocess.run is called with the `arp` command and a numeric timeout."""
        resolve_mac("192.0.2.122")
        self.assertTrue(mock_run.called)
        call_args = mock_run.call_args
        positional_args = call_args[0]
        cmd = positional_args[0]
        self.assertIsInstance(cmd, (list, tuple), "subprocess.run first arg must be a sequence")
        self.assertEqual(cmd[0], "arp", "first element of command must be 'arp'")
        self.assertIn("-n", cmd, "arp must be invoked with -n to suppress DNS lookups (per spec)")
        self.assertIn("192.0.2.122", cmd, "target IP must appear in command")
        timeout = _resolve_timeout(call_args)
        self.assertIsNotNone(timeout, "subprocess.run must be called with a timeout kwarg")
        self.assertIsInstance(timeout, (int, float))
        self.assertEqual(float(timeout), 2.0, "subprocess timeout must be exactly 2.0 s per spec")


# ---------------------------------------------------------------------------
# resolve_mac — subprocess output parsing
# ---------------------------------------------------------------------------


class TestResolveMacSubprocessParsing(TestCase):
    """resolve_mac correctly parses and normalises MAC from subprocess `arp -n` output."""

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_mac_returned_lowercase_colon(self, _mock_open, _mock_run):
        """subprocess arp output MAC is returned as lowercase colon-separated."""
        result = resolve_mac("192.0.2.122")
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT_HYPHEN))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_hyphen_mac_normalized(self, _mock_open, _mock_run):
        """Hyphen-separated MAC from subprocess is normalised to lowercase colon form."""
        result = resolve_mac("192.0.2.122")
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT_INCOMPLETE))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_incomplete_returns_none(self, _mock_open, _mock_run):
        """<incomplete> placeholder in subprocess output returns None."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT_ZEROS))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_zeros_returns_none(self, _mock_open, _mock_run):
        """All-zeros MAC from subprocess is treated as a placeholder and returns None."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run", return_value=_make_proc_result(""))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_empty_stdout_returns_none(self, _mock_open, _mock_run):
        """Empty subprocess stdout returns None (IP not found)."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("arp", 2.0))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_timeout_returns_none(self, _mock_open, _mock_run):
        """subprocess.TimeoutExpired is caught and returns None per all-exceptions rule."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run", side_effect=FileNotFoundError("arp not found"))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_filenotfound_returns_none(self, _mock_open, _mock_run):
        """FileNotFoundError from subprocess (arp binary absent) returns None."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run", side_effect=OSError("unexpected"))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_oserror_returns_none(self, _mock_open, _mock_run):
        """Any exception from subprocess.run returns None per all-exceptions rule."""
        result = resolve_mac("192.0.2.122")
        self.assertIsNone(result)

    @patch("subprocess.run", return_value=_make_proc_result(_LINUX_ARP_STDOUT_MULTI))
    @patch("builtins.open", side_effect=OSError)
    def test_subprocess_substring_ip_does_not_collide(self, _mock_open, _mock_run):
        """resolve_mac for an IP that is a prefix-substring of another IP returns the correct MAC.

        Regression guard: naive `if ip in line` would match .122's row when querying .1.
        The /proc/net/arp path uses parts[0] equality and is not affected; the subprocess
        path must apply the same word-boundary discipline to its line scan.
        """
        self.assertEqual(resolve_mac("192.0.2.1"), "11:22:33:44:55:66")


# ---------------------------------------------------------------------------
# resolve_mac — source_ip kwarg
# ---------------------------------------------------------------------------


class TestResolveMacSourceIp(TestCase):
    """source_ip kwarg is accepted and has no observable effect on the return value."""

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE))
    def test_source_ip_kwarg_accepted_returns_same_result(self):
        """Calls with and without source_ip return the same MAC from /proc/net/arp."""
        result_no_source = resolve_mac("192.0.2.122")
        result_with_source = resolve_mac("192.0.2.122", source_ip="10.0.0.1")
        self.assertEqual(result_no_source, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(result_with_source, "aa:bb:cc:dd:ee:ff")

    @patch("builtins.open", mock_open(read_data=_ARP_TABLE))
    def test_source_ip_none_default_accepted(self):
        """source_ip=None (default) is accepted without error."""
        result = resolve_mac("192.0.2.122", source_ip=None)
        self.assertEqual(result, "aa:bb:cc:dd:ee:ff")


# ---------------------------------------------------------------------------
# synthesize_mac_id
# ---------------------------------------------------------------------------


class TestSynthesizeMacId(TestCase):
    """synthesize_mac_id returns a deterministic prefix with a random 8-hex suffix."""

    def test_prefix_matches_spec(self):
        """Return value starts with syn- followed by ip-with-dashes."""
        result = synthesize_mac_id("192.0.2.122")
        self.assertTrue(result.startswith("syn-192-0-2-122-"), f"unexpected prefix: {result!r}")

    def test_suffix_is_8_lowercase_hex_chars(self):
        """8-character hex suffix is lowercase hexadecimal."""
        result = synthesize_mac_id("192.0.2.122")
        parts = result.split("-")
        suffix = parts[-1]
        self.assertEqual(len(suffix), 8, f"suffix length wrong: {suffix!r}")
        self.assertRegex(suffix, r"^[0-9a-f]{8}$", f"suffix not lowercase hex: {suffix!r}")

    def test_full_format(self):
        """Full return value matches syn-<ip-dashes>-<8-hex> pattern."""
        result = synthesize_mac_id("10.0.0.1")
        self.assertRegex(result, r"^syn-10-0-0-1-[0-9a-f]{8}$")

    def test_suffix_is_random_per_call(self):
        """Two consecutive calls return different 8-hex suffixes (probabilistically)."""
        results = {synthesize_mac_id("192.0.2.122") for _ in range(10)}
        self.assertGreater(
            len(results),
            1,
            "synthesize_mac_id returned the same value 10 times in a row",
        )

    def test_does_not_raise_for_nonempty_input(self):
        """synthesize_mac_id never raises for any non-empty string IP."""
        for ip in ("1.2.3.4", "255.255.255.255", "::1", "10.0.0.1"):
            try:
                synthesize_mac_id(ip)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"synthesize_mac_id({ip!r}) raised unexpectedly: {exc!r}")

    def test_dots_replaced_by_dashes_in_middle(self):
        """IP dots become dashes in the returned string body."""
        result = synthesize_mac_id("1.2.3.4")
        self.assertIn("syn-1-2-3-4-", result)
        self.assertNotIn("1.2.3.4", result)
