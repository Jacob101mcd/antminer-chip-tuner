"""Unit tests for MRR signing and nonce monotonicity.

Covers:
- HMAC-SHA1 sig is hmac(SECRET, KEY+nonce+endpoint).hexdigest()
- Endpoint is stripped of /api/v2/ prefix and trailing slash before signing
- Full path sent to the HTTPS connection includes /api/v2/ prefix
- Nonce is strictly monotonically increasing across calls
- Nonce stays monotonic even when clock drifts backward
- Nonce persists across MRRNonce instance recreation
"""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import unittest
from unittest.mock import patch

from tuner_app.mrr import client as client_mod
from tuner_app.mrr.nonce import MRRNonce


class _Fake:
    last_call: dict = {}

    def __init__(self, host, timeout=None):
        self.host = host

    def request(self, method, full_path, body=None, headers=None):
        _Fake.last_call.update(
            {
                "method": method,
                "full_path": full_path,
                "body": body,
                "headers": dict(headers or {}),
            }
        )

    def getresponse(self):
        class _Resp:
            status = 200

            def read(self, _size=-1):
                return b'{"success":true,"data":{}}'

        return _Resp()

    def close(self):
        pass


class TestMRRSigning(unittest.TestCase):
    def setUp(self):
        _Fake.last_call = {}

    def _make_expected_sig(self, key, secret, nonce, endpoint):
        msg = (key + nonce + endpoint).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), msg, hashlib.sha1).hexdigest()

    def test_signature_matches_hmac_sha1_formula(self):
        """Verify that the signature matches the HMAC-SHA1 formula."""
        client = client_mod.MRRClient("KEY", "SECRET")
        with patch("tuner_app.mrr.client.http.client.HTTPSConnection", _Fake):
            client.whoami()
        headers = _Fake.last_call["headers"]
        nonce = headers["x-api-nonce"]
        expected = self._make_expected_sig("KEY", "SECRET", nonce, "/whoami")
        self.assertEqual(headers["x-api-sign"], expected)

    def test_endpoint_path_stripped(self):
        """Verify that the endpoint path is stripped of /api/v2/ prefix and trailing slash."""
        client = client_mod.MRRClient("KEY", "SECRET")
        with patch("tuner_app.mrr.client.http.client.HTTPSConnection", _Fake):
            client._request("GET", "/whoami")
        self.assertEqual(_Fake.last_call["full_path"], "/api/v2/whoami")
        headers = _Fake.last_call["headers"]
        nonce = headers["x-api-nonce"]
        expected = self._make_expected_sig("KEY", "SECRET", nonce, "/whoami")
        self.assertEqual(headers["x-api-sign"], expected)

    def test_trailing_slash_stripped(self):
        """Verify that a trailing slash is stripped from the endpoint before signing."""
        client = client_mod.MRRClient("KEY", "SECRET")
        with patch("tuner_app.mrr.client.http.client.HTTPSConnection", _Fake):
            client._request("GET", "/whoami/")
        self.assertEqual(_Fake.last_call["full_path"], "/api/v2/whoami")
        headers = _Fake.last_call["headers"]
        nonce = headers["x-api-nonce"]
        expected = self._make_expected_sig("KEY", "SECRET", nonce, "/whoami")
        self.assertEqual(headers["x-api-sign"], expected)


class TestMRRNonce(unittest.TestCase):
    def _make_path(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)  # MRRNonce loads via try/except so missing is fine
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_monotonic_across_calls(self):
        """Verify that nonce values are strictly monotonically increasing across calls."""
        n = MRRNonce(self._make_path())
        prior = 0
        for _ in range(100):
            v = n.next()
            self.assertGreater(v, prior)
            prior = v

    def test_monotonic_after_clock_drift(self):
        """Verify that nonce stays monotonic even when clock drifts backward."""
        n = MRRNonce(self._make_path())
        v = n.next()
        with patch("tuner_app.mrr.nonce.time.time", return_value=1.0):
            v2 = n.next()
        self.assertEqual(v2, v + 1)

    def test_persisted_across_instances(self):
        """Verify that nonce values persist across MRRNonce instance recreation."""
        path = self._make_path()
        n1 = MRRNonce(path)
        last_v1 = 0
        for _ in range(10):
            last_v1 = n1.next()
        n2 = MRRNonce(path)
        self.assertGreater(n2.next(), last_v1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
