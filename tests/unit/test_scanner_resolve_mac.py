"""Unit tests for _resolve_mac_or_synth resolution-priority contract."""

from unittest.mock import patch

from tuner_app.scanner.discover import _resolve_mac_or_synth

PATCH_RESOLVE = "tuner_app.scanner.discover.resolve_mac"
PATCH_SYNTH = "tuner_app.scanner.discover.synthesize_mac_id"


def test_vendor_mac_wins_over_arp():
    """vendor_mac is returned when ARP would also succeed; ARP and synth are not called."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = "99:88:77:66:55:44"
        result = _resolve_mac_or_synth(ip="10.0.0.1", source_ip="", vendor_mac="aa:bb:cc:dd:ee:ff")
    assert result == ("aa:bb:cc:dd:ee:ff", False)
    mock_arp.assert_not_called()
    mock_synth.assert_not_called()


def test_vendor_mac_wins_when_arp_fails():
    """vendor_mac is returned even when ARP returns None; synth is not called."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = None
        result = _resolve_mac_or_synth(ip="10.0.0.1", source_ip="", vendor_mac="aa:bb:cc:dd:ee:ff")
    assert result == ("aa:bb:cc:dd:ee:ff", False)
    mock_arp.assert_not_called()
    mock_synth.assert_not_called()


def test_arp_wins_when_vendor_mac_is_none():
    """ARP result is returned when vendor_mac=None; synth is not called."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = "aa:bb:cc:dd:ee:ff"
        result = _resolve_mac_or_synth(ip="10.0.0.1", source_ip="", vendor_mac=None)
    assert result == ("aa:bb:cc:dd:ee:ff", False)
    mock_synth.assert_not_called()


def test_synth_used_when_vendor_mac_none_and_arp_fails():
    """Synthesized ID is returned with id_synthesized=True when vendor_mac=None and ARP fails."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = None
        mock_synth.return_value = "syn-10-0-0-1-deadbeef"
        result = _resolve_mac_or_synth(ip="10.0.0.1", source_ip="", vendor_mac=None)
    assert result == ("syn-10-0-0-1-deadbeef", True)
    mock_synth.assert_called_once_with("10.0.0.1")


def test_empty_vendor_mac_falls_through_to_arp():
    """Empty string for vendor_mac is treated as absent; ARP result is used."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = "aa:bb:cc:dd:ee:ff"
        result = _resolve_mac_or_synth(ip="10.0.0.1", source_ip="", vendor_mac="")
    assert result == ("aa:bb:cc:dd:ee:ff", False)
    mock_synth.assert_not_called()


def test_backward_compat_two_arg_call_arp_succeeds():
    """Two-positional-arg callers without vendor_mac keyword get unchanged ARP→synth behavior."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = "aa:bb:cc:dd:ee:ff"
        result = _resolve_mac_or_synth("10.0.0.1", "")
    assert result == ("aa:bb:cc:dd:ee:ff", False)
    mock_synth.assert_not_called()


def test_synth_format_vendor_mac_trusted_as_is():
    """A synth-format string passed as vendor_mac is returned verbatim with id_synthesized=False."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = None
        result = _resolve_mac_or_synth(
            ip="10.0.0.1",
            source_ip="",
            vendor_mac="syn-10-0-0-1-aabbccdd",
        )
    assert result == ("syn-10-0-0-1-aabbccdd", False)
    mock_arp.assert_not_called()
    mock_synth.assert_not_called()


def test_backward_compat_two_arg_call_arp_fails():
    """Two-positional-arg call (no vendor_mac kwarg) falls through to synth on ARP failure."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = None
        mock_synth.return_value = "syn-10-0-0-1-deadbeef"
        result = _resolve_mac_or_synth("10.0.0.1", "")
    assert result == ("syn-10-0-0-1-deadbeef", True)
    mock_synth.assert_called_once_with("10.0.0.1")


def test_empty_vendor_mac_falls_through_to_synth():
    """Empty-string vendor_mac="" is treated as absent; synth is used when ARP returns None."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = None
        mock_synth.return_value = "syn-10-0-0-1-deadbeef"
        result = _resolve_mac_or_synth(ip="10.0.0.1", source_ip="", vendor_mac="")
    assert result == ("syn-10-0-0-1-deadbeef", True)
    mock_synth.assert_called_once_with("10.0.0.1")


def test_arp_called_when_vendor_mac_falsy_chain():
    """ARP is invoked when vendor_mac is None or empty (proves ARP isn't skipped)."""
    with patch(PATCH_RESOLVE) as mock_arp, patch(PATCH_SYNTH) as mock_synth:
        mock_arp.return_value = "aa:bb:cc:dd:ee:ff"
        _resolve_mac_or_synth(ip="10.0.0.1", source_ip="src", vendor_mac=None)
        mock_arp.assert_called_once_with("10.0.0.1", source_ip="src")
        mock_synth.assert_not_called()
        # And again for empty-string variant
        mock_arp.reset_mock()
        mock_synth.reset_mock()
        _resolve_mac_or_synth(ip="10.0.0.1", source_ip="src", vendor_mac="")
        mock_arp.assert_called_once_with("10.0.0.1", source_ip="src")
        mock_synth.assert_not_called()
