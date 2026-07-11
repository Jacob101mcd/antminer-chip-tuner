from unittest.mock import MagicMock

from tuner_app.http_server.handlers._mac_helpers import parse_macs_body_field


def test_logs_received_count_on_normal_input(caplog):
    handler = MagicMock()
    data = {"macs": ["aa:bb:cc:dd:ee:01", "aa-bb-cc-dd-ee-02", "aabbccddee03"]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert any("received 3 raw macs" in record.message for record in caplog.records)
    assert len(result) == 3
    assert all(isinstance(mac, str) and ":" in mac for mac in result)


def test_logs_dropped_empty_strings(caplog):
    handler = MagicMock()
    data = {"macs": ["aa:bb:cc:dd:ee:ff", "", "  "]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert len([r for r in caplog.records if "empty-or-whitespace" in r.message]) == 2
    assert result == ["aa:bb:cc:dd:ee:ff"]
    handler._json_response.assert_not_called()


def test_logs_warning_when_all_filtered(caplog):
    handler = MagicMock()
    data = {"macs": ["", "  "]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert any("ALL 2 macs filtered out" in record.message for record in caplog.records)
    # Post-fix: the all-filtered path now returns HTTP 400 instead of silently
    # returning [] (which surfaced as the misleading "0/0 succeeded" modal).
    assert result is None
    handler._json_response.assert_called_once()
    assert handler._json_response.call_args[1]["status"] == 400


def test_logs_normalize_failures_before_400(caplog):
    handler = MagicMock()
    data = {"macs": ["192.168.1.1"]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert any("normalize-failed" in record.message for record in caplog.records)
    assert result is None
    handler._json_response.assert_called_once()
    call_kwargs = handler._json_response.call_args[1]
    assert call_kwargs.get("status") == 400


def test_existing_behavior_preserved_ips_field(caplog):
    handler = MagicMock()
    data = {"ips": ["aa:bb:cc:dd:ee:ff"]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert result is None
    handler._json_response.assert_called_once()
    call_kwargs = handler._json_response.call_args[1]
    assert call_kwargs.get("status") == 400
    assert "'ips' body field is no longer accepted" in str(handler._json_response.call_args[0])


def test_existing_behavior_preserved_returns_canonical_macs(caplog):
    handler = MagicMock()
    data = {"macs": ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-02"]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert result == ["aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:02"]
    handler._json_response.assert_not_called()


def test_returns_400_when_all_macs_filtered_as_empty_strings(caplog):
    handler = MagicMock()
    data = {"macs": ["", "  ", "\t"]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert result is None
    handler._json_response.assert_called_once()
    call_kwargs = handler._json_response.call_args[1]
    assert call_kwargs.get("status") == 400
    body = handler._json_response.call_args[0][0]
    assert "summary" in body
    assert body["summary"]["total"] == 0
    assert "errors" in body
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) > 0
    assert any(word in str(body).lower() for word in ["all", "filtered", "empty"])


def test_returns_400_when_mix_of_empty_and_invalid(caplog):
    handler = MagicMock()
    data = {"macs": ["", "not-a-mac", "  "]}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert result is None
    handler._json_response.assert_called_once()
    call_kwargs = handler._json_response.call_args[1]
    assert call_kwargs.get("status") == 400
    body = handler._json_response.call_args[0][0]
    assert "summary" in body
    assert body["summary"]["total"] == 0
    assert "errors" in body
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) > 0


def test_legitimate_empty_macs_array_still_returns_empty_list(caplog):
    handler = MagicMock()
    data = {"macs": []}
    caplog.set_level("DEBUG", logger="tuner_app.http_server.handlers._mac_helpers")
    result = parse_macs_body_field(handler, data)
    assert result == []
    handler._json_response.assert_not_called()
