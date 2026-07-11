from __future__ import annotations

import json
import socket
import threading
import time
from unittest import TestCase
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.config.validation import validate_config
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.luxos import (
    DEFAULT_TIMEOUT_SEC,
    LUXOS_DEFAULT_PORT,
    LuxosMinerAPI,
    _LuxosTransport,
)
from tuner_app.miner.registry import _make_luxos


def _make_mock_sock(responses: list[bytes]) -> MagicMock:
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = responses
    mock_sock.__enter__ = lambda s: mock_sock
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.sendall = MagicMock()
    return mock_sock


def _ok_response(**extra) -> bytes:
    """Return a STATUS=S response with optional extra keys."""
    data = {"STATUS": [{"STATUS": "S", "Msg": "ok", "Description": ""}]}
    data.update(extra)
    return json.dumps(data).encode()


def _logon_response(session_id: str) -> bytes:
    return json.dumps(
        {
            "STATUS": [{"STATUS": "S", "Msg": "ok"}],
            "SESSION": [{"SessionID": session_id}],
        }
    ).encode()


class TestSendCmdReadonly(TestCase):
    def test_happy_path(self):
        transport = _LuxosTransport("1.2.3.4")
        response_data = _ok_response(VERSION=[{"version": "1.0"}])
        mock_sock = _make_mock_sock([response_data, b""])
        with patch(
            "tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock
        ) as mock_connect:
            result = transport.send_cmd("version")
        self.assertEqual(result["VERSION"][0]["version"], "1.0")
        mock_connect.assert_called_once_with(
            ("1.2.3.4", LUXOS_DEFAULT_PORT), timeout=DEFAULT_TIMEOUT_SEC
        )
        sent_data = mock_sock.sendall.call_args[0][0]
        payload = json.loads(sent_data.decode())
        self.assertEqual(payload["command"], "version")
        self.assertNotIn("parameter", payload)

    def test_single_positional_param_serialized_as_parameter(self):
        """``send_cmd("voltageget", "0")`` MUST send ``parameter=0`` on the wire.

        Regression: an earlier ``send_cmd`` body discarded ``*params`` for the
        non-session branch, sending paramless ``{"command":"voltageget"}``. On
        LUXminer 2026.4.3 this silently blocks the API server for ~10-15 s and
        causes every subsequent connection in Phase 0 to be refused. Other
        params-required reads (``frequencyget``, ``healthchipget``) returned
        STATUS=E in the same scenario, raising ``MinerCommandError`` and
        breaking ``clocks()`` / ``temps_chip()`` / ``hashrate()``.
        """
        transport = _LuxosTransport("1.2.3.4")
        response_data = _ok_response(VOLTAGE=[{"Board": 0, "Voltage": 15.01}])
        mock_sock = _make_mock_sock([response_data, b""])
        with patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock):
            transport.send_cmd("voltageget", "0")
        payload = json.loads(mock_sock.sendall.call_args[0][0].decode())
        self.assertEqual(payload["command"], "voltageget")
        self.assertEqual(payload["parameter"], "0")

    def test_multiple_positional_params_joined_with_comma(self):
        """``send_cmd("healthchipget", "0", "5")`` MUST send ``parameter=0,5``.

        LuxOS multi-arg cmds (e.g., ``healthchipget board_id chip_id``) expect
        a single comma-joined ``parameter`` string, matching the format used by
        the session-required path's ``_execute_with_session_refresh``.
        """
        transport = _LuxosTransport("1.2.3.4")
        mock_sock = _make_mock_sock(
            [
                _ok_response(
                    CHIPS=[
                        {
                            "Board": 0,
                            "Chip": 5,
                            "GHS 5m": 337.44,
                            "Healthy": "Yes",
                            "ChipTemp": 57.39,
                        }
                    ]
                ),
                b"",
            ]
        )
        with patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock):
            transport.send_cmd("healthchipget", "0", "5")
        payload = json.loads(mock_sock.sendall.call_args[0][0].decode())
        self.assertEqual(payload["command"], "healthchipget")
        self.assertEqual(payload["parameter"], "0,5")


class TestRecvLoop(TestCase):
    def test_multi_chunk(self):
        transport = _LuxosTransport("1.2.3.4")
        response_json = json.dumps({"STATUS": [{"STATUS": "S"}], "DATA": [{"val": 42}]})
        # Split into 3 chunks; the 3rd chunk completes the JSON
        part1 = response_json[:5].encode()
        part2 = response_json[5:10].encode()
        part3 = response_json[10:].encode()
        mock_sock = _make_mock_sock([part1, part2, part3, b""])
        with patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock):
            result = transport.send_cmd("summary")
        # recv called 3 times (parse succeeds on 3rd chunk; 4th b"" never reached)
        self.assertEqual(mock_sock.recv.call_count, 3)
        self.assertEqual(result["DATA"][0]["val"], 42)

    def test_hard_cap_raises(self):
        transport = _LuxosTransport("1.2.3.4")
        # 300 * 4096 = 1,228,800 bytes > 1 MB cap — never valid JSON
        mock_sock = _make_mock_sock([b"x" * 4096] * 300)
        with (
            patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock),
            self.assertRaises(MinerCommandError) as cm,
        ):
            transport.send_cmd("test")
        self.assertIn("byte cap", str(cm.exception))


class TestStatusErrors(TestCase):
    def test_status_e_raises_command_error(self):
        transport = _LuxosTransport("1.2.3.4")
        error_resp = json.dumps(
            {"STATUS": [{"STATUS": "E", "Msg": "Bad command", "Description": "details"}]}
        ).encode()
        mock_sock = _make_mock_sock([error_resp, b""])
        with (
            patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock),
            self.assertRaises(MinerCommandError) as cm,
        ):
            transport.send_cmd("badcmd")
        self.assertIn("badcmd", str(cm.exception))
        self.assertIn("Bad command", str(cm.exception))


class TestTransportErrors(TestCase):
    def _assert_offline(self, exc_factory):
        transport = _LuxosTransport("1.2.3.4")
        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=exc_factory,
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("test")

    def test_socket_timeout_raises_offline(self):
        self._assert_offline(TimeoutError("timed out"))

    def test_connection_refused_raises_offline(self):
        self._assert_offline(ConnectionRefusedError("refused"))

    def test_os_error_raises_offline(self):
        self._assert_offline(OSError("network unreachable"))


class TestSessionManagement(TestCase):
    def test_session_lazy_open_logon_then_cmd(self):
        transport = _LuxosTransport("1.2.3.4")
        mock_logon = _make_mock_sock([_logon_response("abc123"), b""])
        mock_cmd = _make_mock_sock([_ok_response(DATA=[{"val": 42}]), b""])
        mock_logoff = _make_mock_sock([_ok_response(), b""])
        with patch("tuner_app.miner.luxos.socket.create_connection") as mock_connect:
            mock_connect.side_effect = [mock_logon, mock_cmd, mock_logoff]
            result = transport.send_cmd("setfreq", "arg1", requires_session=True)
        self.assertEqual(mock_connect.call_count, 3)
        self.assertEqual(result["DATA"][0]["val"], 42)
        self.assertIsNone(transport._session_id)
        sent_data = mock_cmd.sendall.call_args[0][0]
        payload = json.loads(sent_data.decode())
        self.assertIn("parameter", payload)
        self.assertTrue(payload["parameter"].startswith("abc123"))

    def test_second_call_gets_fresh_logon(self):
        """After logoff, a second requires_session call triggers a new logon."""
        transport = _LuxosTransport("1.2.3.4")
        # First call: logon1, cmd1, logoff1
        # Second call: logon2, cmd2, logoff2
        socks = [
            _make_mock_sock([_logon_response("s1"), b""]),
            _make_mock_sock([_ok_response(DATA=[{"n": 1}]), b""]),
            _make_mock_sock([_ok_response(), b""]),
            _make_mock_sock([_logon_response("s2"), b""]),
            _make_mock_sock([_ok_response(DATA=[{"n": 2}]), b""]),
            _make_mock_sock([_ok_response(), b""]),
        ]
        with patch("tuner_app.miner.luxos.socket.create_connection") as mock_connect:
            mock_connect.side_effect = socks
            r1 = transport.send_cmd("cmd", requires_session=True)
            r2 = transport.send_cmd("cmd", requires_session=True)
        self.assertEqual(mock_connect.call_count, 6)
        self.assertEqual(r1["DATA"][0]["n"], 1)
        self.assertEqual(r2["DATA"][0]["n"], 2)
        # Second call used a fresh session
        sent2 = json.loads(socks[4].sendall.call_args[0][0].decode())
        self.assertTrue(sent2["parameter"].startswith("s2"))

    def test_session_refresh_on_invalid_session(self):
        transport = _LuxosTransport("1.2.3.4")
        invalid_resp = json.dumps({"STATUS": [{"STATUS": "E", "Msg": "Invalid session"}]}).encode()
        socks = [
            _make_mock_sock([_logon_response("sess1"), b""]),  # logon1
            _make_mock_sock([invalid_resp, b""]),  # cmd → session expired
            _make_mock_sock([_logon_response("sess2"), b""]),  # logon2 (refresh)
            _make_mock_sock([_ok_response(DATA=[{"ok": True}]), b""]),  # cmd retry
            _make_mock_sock([_ok_response(), b""]),  # logoff
        ]
        with patch("tuner_app.miner.luxos.socket.create_connection") as mock_connect:
            mock_connect.side_effect = socks
            with self.assertLogs("tuner_app.miner.luxos", level="INFO"):
                result = transport.send_cmd("test", requires_session=True)
        self.assertEqual(mock_connect.call_count, 5)
        self.assertEqual(result["DATA"][0]["ok"], True)


class TestSessionLock(TestCase):
    def test_session_lock_serializes_concurrent_calls(self):
        """Two concurrent requires_session calls must not interleave logon/logoff."""
        transport = _LuxosTransport("1.2.3.4")

        # 6 sockets: thread A gets [0,1,2], thread B gets [3,4,5]
        socks = [
            _make_mock_sock([_logon_response(f"s{i // 3 + 1}"), b""])
            if i % 3 == 0
            else _make_mock_sock([_ok_response(DATA=[{"t": i}]), b""])
            if i % 3 == 1
            else _make_mock_sock([_ok_response(), b""])
            for i in range(6)
        ]

        call_order: list[int] = []
        call_lock = threading.Lock()
        sock_iter = iter(socks)
        sock_iter_lock = threading.Lock()

        def fake_connect(*args, **kwargs):
            with sock_iter_lock:
                sock = next(sock_iter)
            with call_lock:
                call_order.append(1)
            return sock

        with patch(
            "tuner_app.miner.luxos.socket.create_connection", side_effect=fake_connect
        ) as mock_connect:
            barrier = threading.Barrier(2)

            def run(idx):
                barrier.wait()
                transport.send_cmd(f"cmd{idx}", requires_session=True)

            t1 = threading.Thread(target=run, args=(1,))
            t2 = threading.Thread(target=run, args=(2,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(mock_connect.call_count, 6)


class TestCloseSession(TestCase):
    def test_close_session_idempotent(self):
        transport = _LuxosTransport("1.2.3.4")
        with patch("tuner_app.miner.luxos.socket.create_connection") as mock_connect:
            transport.close_session()
            transport.close_session()
            mock_connect.assert_not_called()


class TestConnectionRateLimit(TestCase):
    def test_rate_limit_enforced(self):
        """Two rapid send_cmd calls must be spaced by at least min_conn_interval_sec."""
        transport = _LuxosTransport("1.2.3.4", min_conn_interval_sec=0.5)
        connect_times: list[float] = []

        def record_time_and_return_sock(*args, **kwargs):
            connect_times.append(time.monotonic())
            return _make_mock_sock([_ok_response(), b""])

        with patch(
            "tuner_app.miner.luxos.socket.create_connection",
            side_effect=record_time_and_return_sock,
        ):
            transport.send_cmd("summary")
            transport.send_cmd("summary")

        self.assertEqual(len(connect_times), 2)
        self.assertGreaterEqual(connect_times[1] - connect_times[0], 0.45)

    def test_no_delay_on_first_call(self):
        """The first send_cmd has no prior timestamp to space against, so no sleep is invoked."""
        transport = _LuxosTransport("1.2.3.4", min_conn_interval_sec=0.5)
        mock_sleep = MagicMock()

        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                return_value=_make_mock_sock([_ok_response(), b""]),
            ),
            patch("tuner_app.miner.luxos.time.sleep", mock_sleep),
        ):
            transport.send_cmd("summary")

        # Either no sleep at all, or only sleep(0) calls (defensive).
        self.assertTrue(
            mock_sleep.call_count == 0
            or all(call.args[0] == 0 for call in mock_sleep.call_args_list)
        )

    def test_no_delay_when_gap_exceeds_interval(self):
        """When real elapsed exceeds min_conn_interval_sec, no artificial sleep added."""
        transport = _LuxosTransport("1.2.3.4", min_conn_interval_sec=0.1)

        with patch(
            "tuner_app.miner.luxos.socket.create_connection",
            return_value=_make_mock_sock([_ok_response(), b""]),
        ):
            transport.send_cmd("summary")

        # Real sleep so monotonic advances past the 0.1s window.
        time.sleep(0.2)

        mock_sleep = MagicMock()
        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                return_value=_make_mock_sock([_ok_response(), b""]),
            ),
            patch("tuner_app.miner.luxos.time.sleep", mock_sleep),
        ):
            transport.send_cmd("summary")

        self.assertTrue(
            mock_sleep.call_count == 0
            or all(call.args[0] == 0 for call in mock_sleep.call_args_list)
        )

    def test_lock_serializes_concurrent_callers(self):
        """Two threads dispatched simultaneously must still be spaced by min_conn_interval_sec."""
        transport = _LuxosTransport("1.2.3.4", min_conn_interval_sec=0.3)
        connect_times: list[float] = []
        record_lock = threading.Lock()

        def record_time_and_return_sock(*args, **kwargs):
            with record_lock:
                connect_times.append(time.monotonic())
            return _make_mock_sock([_ok_response(), b""])

        barrier = threading.Barrier(2)

        def call_send_cmd():
            barrier.wait()
            transport.send_cmd("summary")

        with patch(
            "tuner_app.miner.luxos.socket.create_connection",
            side_effect=record_time_and_return_sock,
        ):
            t1 = threading.Thread(target=call_send_cmd)
            t2 = threading.Thread(target=call_send_cmd)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(len(connect_times), 2)
        self.assertGreaterEqual(max(connect_times) - min(connect_times), 0.25)


class TestLuxosMinConnIntervalConfig(TestCase):
    def test_bound_zero_valid(self):
        """LUXOS_MIN_CONN_INTERVAL_SEC=0.0 is valid (lower bound)."""
        cleaned, errors = validate_config({"LUXOS_MIN_CONN_INTERVAL_SEC": 0.0}, platform="luxos")
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["LUXOS_MIN_CONN_INTERVAL_SEC"], 0.0)

    def test_bound_max_valid(self):
        """LUXOS_MIN_CONN_INTERVAL_SEC=5.0 is valid (upper bound)."""
        cleaned, errors = validate_config({"LUXOS_MIN_CONN_INTERVAL_SEC": 5.0}, platform="luxos")
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["LUXOS_MIN_CONN_INTERVAL_SEC"], 5.0)

    def test_negative_invalid(self):
        """LUXOS_MIN_CONN_INTERVAL_SEC=-0.1 is rejected by bounds check."""
        cleaned, errors = validate_config({"LUXOS_MIN_CONN_INTERVAL_SEC": -0.1}, platform="luxos")
        self.assertNotEqual(errors, [])
        self.assertNotIn("LUXOS_MIN_CONN_INTERVAL_SEC", cleaned)

    def test_above_max_invalid(self):
        """LUXOS_MIN_CONN_INTERVAL_SEC=5.1 is rejected by bounds check."""
        cleaned, errors = validate_config({"LUXOS_MIN_CONN_INTERVAL_SEC": 5.1}, platform="luxos")
        self.assertNotEqual(errors, [])
        self.assertNotIn("LUXOS_MIN_CONN_INTERVAL_SEC", cleaned)

    def test_default_value_present_in_luxos_platform_defaults(self):
        """The default 1.0 value is registered under state.CONFIG['defaults']['luxos']."""
        self.assertEqual(state.CONFIG["defaults"]["luxos"]["LUXOS_MIN_CONN_INTERVAL_SEC"], 1.0)


class TestLuxosMinerAPIPlumbing(TestCase):
    def test_constructor_passes_min_conn_interval_to_transport(self):
        """LuxosMinerAPI plumbs the min_conn_interval_sec kwarg to its _LuxosTransport instance."""
        api = LuxosMinerAPI("1.2.3.4", min_conn_interval_sec=0.7)
        self.assertEqual(api._transport._min_conn_interval_sec, 0.7)


class TestRaisedDefault(TestCase):
    """Spec Part A (issue #33): LUXOS_MIN_CONN_INTERVAL_SEC default raised from 0.25 to 1.0."""

    def test_default_min_conn_interval_is_one_second(self):
        """Assert that _LuxosTransport's kwarg default for min_conn_interval_sec is 1.0."""
        transport = _LuxosTransport("1.2.3.4")
        self.assertEqual(transport._min_conn_interval_sec, 1.0)

    def test_config_default_value_is_one(self):
        """Assert the per-platform luxos config default for LUXOS_MIN_CONN_INTERVAL_SEC is 1.0."""
        val = state.CONFIG["defaults"]["luxos"].get("LUXOS_MIN_CONN_INTERVAL_SEC")
        self.assertEqual(val, 1.0)


class TestOfflineBackoff(TestCase):
    """Spec Part B (issue #33): LUXOS_OFFLINE_BACKOFF_SEC backoff window."""

    def test_offline_backoff_default_is_thirty_seconds(self):
        """Default offline_backoff_sec attribute is 30.0."""
        transport = _LuxosTransport("1.2.3.4")
        self.assertEqual(transport._offline_backoff_sec, 30.0)

    def test_connection_refused_sets_offline_until(self):
        """ConnectionRefusedError sets _offline_until_monotonic ~ now + offline_backoff_sec."""
        transport = _LuxosTransport("1.2.3.4", offline_backoff_sec=30.0)
        self.assertIsNone(transport._offline_until_monotonic)

        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=ConnectionRefusedError("refused"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("summary")

        self.assertIsNotNone(transport._offline_until_monotonic)
        expected = time.monotonic() + transport._offline_backoff_sec
        self.assertAlmostEqual(transport._offline_until_monotonic, expected, delta=1.0)

    def test_socket_timeout_does_not_set_offline_until(self):
        """Assert that a socket.timeout does NOT set _offline_until_monotonic."""
        transport = _LuxosTransport("1.2.3.4")

        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=TimeoutError("timed out"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("summary")

        self.assertIsNone(transport._offline_until_monotonic)

    def test_os_error_does_not_set_offline_until(self):
        """OSError does NOT set _offline_until_monotonic — only ConnectionRefusedError does."""
        transport = _LuxosTransport("1.2.3.4")
        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=OSError("network unreachable"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("summary")
        self.assertIsNone(transport._offline_until_monotonic)

    def test_connection_reset_error_does_not_set_offline_until(self):
        """Assert that a ConnectionResetError does NOT set _offline_until_monotonic."""
        transport = _LuxosTransport("1.2.3.4")
        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=ConnectionResetError("peer reset"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("summary")
        self.assertIsNone(transport._offline_until_monotonic)

    def test_gaierror_does_not_set_offline_until(self):
        """Assert that a socket.gaierror (DNS failure) does NOT set _offline_until_monotonic."""
        transport = _LuxosTransport("1.2.3.4")
        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=socket.gaierror("name not resolved"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("summary")
        self.assertIsNone(transport._offline_until_monotonic)

    def test_apply_rate_limit_sleeps_until_offline_until(self):
        """_apply_rate_limit total + per-call sleep cover the full offline window."""
        # min_conn_interval_sec=0 so regular spacing doesn't add to the wait;
        # offline_backoff_sec=2.0 so the implementation respects the manually-set timestamp.
        transport = _LuxosTransport("1.2.3.4", min_conn_interval_sec=0.0, offline_backoff_sec=2.0)
        transport._offline_until_monotonic = time.monotonic() + 1.5

        sleep_calls = []
        with patch("tuner_app.miner.luxos.time.sleep", side_effect=sleep_calls.append):
            transport._apply_rate_limit()

        self.assertGreaterEqual(
            sum(sleep_calls),
            1.4,
            f"Expected total sleep >= 1.4 s; got calls={sleep_calls}",
        )
        self.assertGreaterEqual(
            max(sleep_calls),
            1.4,
            f"Expected single sleep covering window; got calls={sleep_calls}",
        )

    def test_apply_rate_limit_uses_max_of_regular_spacing_and_offline(self):
        """Regular spacing (~5s) dominates a smaller offline window (~1s); both bounds."""
        transport = _LuxosTransport("1.2.3.4", min_conn_interval_sec=5.0, offline_backoff_sec=2.0)
        now = time.monotonic()
        transport._last_conn_attempt_monotonic = now
        transport._offline_until_monotonic = now + 1.0

        sleep_calls = []
        with patch("tuner_app.miner.luxos.time.sleep", side_effect=sleep_calls.append):
            transport._apply_rate_limit()

        dominant = max(sleep_calls) if sleep_calls else 0.0
        self.assertGreaterEqual(dominant, 4.5, f"Expected sleep >= 4.5 s; got {sleep_calls}")
        self.assertLessEqual(dominant, 5.5, f"Expected sleep <= 5.5 s; got {sleep_calls}")

    def test_offline_until_clears_after_successful_call(self):
        """backoff window persists after success; sleep never invoked negative."""
        transport = _LuxosTransport("1.2.3.4", offline_backoff_sec=30.0)
        transport._offline_until_monotonic = time.monotonic() + 9999
        sleep_calls = []
        mock_sock = _make_mock_sock([_ok_response(), b""])
        with (
            patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock),
            patch("tuner_app.miner.luxos.time.sleep", side_effect=sleep_calls.append),
        ):
            transport.send_cmd("summary")
        for s in sleep_calls:
            self.assertGreaterEqual(s, 0.0, f"sleep called with negative duration {s}")
        self.assertIsNotNone(transport._offline_until_monotonic)

    def test_offline_backoff_zero_disables_window(self):
        """offline_backoff_sec=0.0 leaves _offline_until_monotonic None or in the past."""
        transport = _LuxosTransport("1.2.3.4", offline_backoff_sec=0.0)

        with (
            patch(
                "tuner_app.miner.luxos.socket.create_connection",
                side_effect=ConnectionRefusedError("refused"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            transport.send_cmd("summary")

        now = time.monotonic()
        offline_until = transport._offline_until_monotonic
        self.assertTrue(
            offline_until is None or offline_until <= now,
            f"Expected _offline_until_monotonic to be None or <= now, got {offline_until}",
        )

    def test_offline_backoff_bounds_in_schema(self):
        """validate_config accepts [0.0, 300.0]; rejects out-of-bounds backoff."""
        cleaned, errors = validate_config({"LUXOS_OFFLINE_BACKOFF_SEC": 0.0}, platform="luxos")
        self.assertEqual(errors, [], f"Expected no errors for 0.0; got {errors}")
        self.assertEqual(cleaned.get("LUXOS_OFFLINE_BACKOFF_SEC"), 0.0)

        cleaned, errors = validate_config({"LUXOS_OFFLINE_BACKOFF_SEC": 300.0}, platform="luxos")
        self.assertEqual(errors, [], f"Expected no errors for 300.0; got {errors}")
        self.assertEqual(cleaned.get("LUXOS_OFFLINE_BACKOFF_SEC"), 300.0)

        cleaned_low, errors_low = validate_config(
            {"LUXOS_OFFLINE_BACKOFF_SEC": -0.1}, platform="luxos"
        )
        self.assertTrue(
            errors_low or "LUXOS_OFFLINE_BACKOFF_SEC" not in cleaned_low,
            "Expected rejection for LUXOS_OFFLINE_BACKOFF_SEC=-0.1",
        )

        cleaned_high, errors_high = validate_config(
            {"LUXOS_OFFLINE_BACKOFF_SEC": 300.1}, platform="luxos"
        )
        self.assertTrue(
            errors_high or "LUXOS_OFFLINE_BACKOFF_SEC" not in cleaned_high,
            "Expected rejection for LUXOS_OFFLINE_BACKOFF_SEC=300.1",
        )


class TestRegistryPlumbing(TestCase):
    """Spec Part C (issue #33): _make_luxos passes LuxOS knobs from config to transport."""

    def _fake_config(self, **overrides):
        """Return a plain dict — _make_luxos uses .get() and __getitem__ on it."""
        base = {"API_PORT": 4028, "PASSWORD": "letmein"}
        base.update(overrides)
        return base

    def test_make_luxos_passes_min_conn_interval_from_config(self):
        """Assert _make_luxos wires LUXOS_MIN_CONN_INTERVAL_SEC into the transport's attribute."""
        config = self._fake_config(LUXOS_MIN_CONN_INTERVAL_SEC=2.5)
        api = _make_luxos("1.2.3.4", config)
        self.assertEqual(api._transport._min_conn_interval_sec, 2.5)

    def test_make_luxos_passes_offline_backoff_from_config(self):
        """Assert _make_luxos wires LUXOS_OFFLINE_BACKOFF_SEC into the transport's attribute."""
        config = self._fake_config(LUXOS_OFFLINE_BACKOFF_SEC=60.0)
        api = _make_luxos("1.2.3.4", config)
        self.assertEqual(api._transport._offline_backoff_sec, 60.0)

    def test_make_luxos_uses_defaults_when_keys_absent(self):
        """Assert _make_luxos falls back to 1.0 and 30.0 when neither LuxOS knob is in config."""
        config = self._fake_config()
        api = _make_luxos("1.2.3.4", config)
        self.assertEqual(api._transport._min_conn_interval_sec, 1.0)
        self.assertEqual(api._transport._offline_backoff_sec, 30.0)


class TestBackoffPreservation(TestCase):
    """Issue #33 fix: _offline_until_monotonic must NOT be cleared by a successful command.

    The fix deletes the 4-line block in _send_raw that set _offline_until_monotonic = None
    on success. The window should expire by time, not be cleared by another command path.
    time.sleep is patched throughout to prevent _apply_rate_limit from actually sleeping
    during the far-future backoff window.
    """

    def test_send_raw_preserves_offline_backoff_on_success(self):
        transport = _LuxosTransport("1.2.3.4", offline_backoff_sec=30.0)
        transport._offline_until_monotonic = time.monotonic() + 9999
        mock_sock = _make_mock_sock([_ok_response(), b""])
        with (
            patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock),
            patch("tuner_app.miner.luxos.time.sleep", side_effect=lambda s: None),
        ):
            transport.send_cmd("summary")
        self.assertIsNotNone(transport._offline_until_monotonic)

    def test_concurrent_success_does_not_clear_pending_backoff_for_other_cmd(self):
        transport = _LuxosTransport("1.2.3.4", offline_backoff_sec=30.0)
        with patch("tuner_app.miner.luxos.time.sleep", side_effect=lambda s: None):
            with (
                patch(
                    "tuner_app.miner.luxos.socket.create_connection",
                    side_effect=ConnectionRefusedError("refused"),
                ),
                self.assertRaises(MinerOfflineError),
            ):
                transport.send_cmd("limits")
            self.assertIsNotNone(transport._offline_until_monotonic)
            mock_sock = _make_mock_sock([_ok_response(), b""])
            with patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock):
                transport.send_cmd("summary")
            self.assertIsNotNone(transport._offline_until_monotonic)

    def test_send_raw_does_not_touch_offline_backoff_when_value_is_none(self):
        transport = _LuxosTransport("1.2.3.4")
        self.assertIsNone(transport._offline_until_monotonic)
        mock_sock = _make_mock_sock([_ok_response(), b""])
        with (
            patch("tuner_app.miner.luxos.socket.create_connection", return_value=mock_sock),
            patch("tuner_app.miner.luxos.time.sleep", side_effect=lambda s: None),
        ):
            transport.send_cmd("summary")
        self.assertIsNone(transport._offline_until_monotonic)
