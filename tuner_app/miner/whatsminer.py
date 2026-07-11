"""Whatsminer (MicroBT) stock firmware TCP/JSON API client.

Wire format mirrors the Bixbit TCP/JSON pattern but adds a session-token /
encrypted-write layer for mutating commands.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import socket
import time
from typing import Any

from cryptography.hazmat.primitives import padding as _crypto_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tuner_app.miner.base import MinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.types import HardwareTopology, MinerSummary
from tuner_app.net.response_limits import MINER_RECV_CHUNK_BYTES, append_capped_response

logger = logging.getLogger(__name__)


# ─── Pure auth helpers ────────────────────────────────────────────────────────


def _md5crypt_hash(password: str, salt: str) -> str:
    """Compute the trailing field of `openssl passwd -1 -salt {salt} {password}` —
    i.e. the part after the third '$'. Pure function. No I/O.

    The MD5-based crypt(3) algorithm (Poul-Henning Kamp, 1994). The Python
    stdlib `crypt` module is gone in Python 3.13+; this is a plain
    implementation against the published RFC-style algorithm.
    """
    # Standard md5crypt algorithm. Only the salt and password are used; the $1$
    # tag and the salt's $-delimited prefix are added by the openssl wrapper —
    # we compute only the trailing 22-char hash field.
    salt = salt[:8]  # md5crypt salt is at most 8 chars
    pw = password.encode("utf-8")
    s = salt.encode("utf-8")

    # Step 1: prefix MD5 of password + salt + password
    inner = hashlib.md5(pw + s + pw).digest()

    # Step 2: outer MD5 of password + "$1$" + salt + repeated inner-bytes
    h = hashlib.md5()
    h.update(pw + b"$1$" + s)
    pl = len(pw)
    while pl > 0:
        h.update(inner if pl > 16 else inner[:pl])
        pl -= 16
    # Step 3: weird bit-toggle inner mix
    pl = len(pw)
    i = pl
    while i:
        h.update(b"\0" if i & 1 else pw[:1])
        i >>= 1
    final = h.digest()

    # Step 4: 1000 rounds of further mixing
    for i in range(1000):
        h2 = hashlib.md5()
        if i & 1:
            h2.update(pw)
        else:
            h2.update(final)
        if i % 3:
            h2.update(s)
        if i % 7:
            h2.update(pw)
        if i & 1:
            h2.update(final)
        else:
            h2.update(pw)
        final = h2.digest()

    # Step 5: base64-like encoding with non-standard alphabet
    itoa64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

    def _to64(v: int, n: int) -> str:
        out = []
        while n > 0:
            out.append(itoa64[v & 0x3F])
            v >>= 6
            n -= 1
        return "".join(out)

    out = ""
    out += _to64((final[0] << 16) | (final[6] << 8) | final[12], 4)
    out += _to64((final[1] << 16) | (final[7] << 8) | final[13], 4)
    out += _to64((final[2] << 16) | (final[8] << 8) | final[14], 4)
    out += _to64((final[3] << 16) | (final[9] << 8) | final[15], 4)
    out += _to64((final[4] << 16) | (final[10] << 8) | final[5], 4)
    out += _to64(final[11], 2)
    return out


def _compute_aeskey(password: str, salt: str) -> bytes:
    """SHA-256 of _md5crypt_hash(password, salt) — 32 bytes for AES-256-ECB."""
    return hashlib.sha256(_md5crypt_hash(password, salt).encode("utf-8")).digest()


def _compute_sign(password: str, salt: str, newsalt: str, time_str: str) -> str:
    """md5crypt(md5crypt(password, salt) + time_str[-4:], newsalt) — trailing field."""
    inner = _md5crypt_hash(password, salt)
    return _md5crypt_hash(inner + time_str[-4:], newsalt)


def _encrypt(plaintext: str, aeskey: bytes) -> str:
    """PKCS7-pad to 16, AES-256-ECB encrypt, base64-encode (no newlines)."""
    padder = _crypto_padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES256(aeskey), modes.ECB())
    enc = cipher.encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def _decrypt(b64: str, aeskey: bytes) -> str:
    """Base64-decode, AES-256-ECB decrypt, strip PKCS7 padding, decode UTF-8."""
    ct = base64.b64decode(b64)
    cipher = Cipher(algorithms.AES256(aeskey), modes.ECB())
    dec = cipher.decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = _crypto_padding.PKCS7(128).unpadder()
    pt = unpadder.update(padded) + unpadder.finalize()
    return pt.decode("utf-8")


# ─── WhatsminerMinerAPI class ────────────────────────────────────────────────


class WhatsminerMinerAPI(MinerAPI):
    """TCP/JSON socket client for stock Whatsminer (MicroBT) firmware.

    Read commands are plaintext; mutating commands are AES-256-ECB encrypted
    with a per-session token derived from a get_token challenge. Default
    password is "admin" (NOT "letmein" like Bixbit).
    """

    _TOKEN_TTL_SEC = 25 * 60  # 25 min — 5 min slack under firmware's documented 30 min

    def __init__(self, ip, port=4028, password="admin"):
        self.ip = ip
        self.port = port
        self.base = f"http://{ip}:{port}"
        self.password = password
        self._token_cache = None
        self._token_acquired_at = 0.0
        self._aeskey: bytes | None = None
        self._sign: str | None = None
        self._topology_cache: HardwareTopology | None = None

    def _send_plaintext(self, cmd: str, **params) -> dict:
        payload = {"cmd": cmd, **params}
        message = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            with socket.create_connection((self.ip, self.port), timeout=15) as sock:
                sock.sendall(message)
                response = b""
                while True:
                    try:
                        chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                        if not chunk:
                            break
                        response = append_capped_response(response, chunk, command=cmd)
                        try:
                            result = json.loads(response.decode("utf-8"))
                            if isinstance(result, dict) and result.get("STATUS") == "E":
                                msg = result.get("Msg", "unknown error")
                                desc = result.get("Description", "")
                                raise MinerCommandError(f"{cmd}: {msg} ({desc})".strip(" ()"))
                            return result
                        except json.JSONDecodeError:
                            continue
                    except TimeoutError:
                        break
                raise MinerCommandError(f"{cmd}: incomplete response ({len(response)} bytes)")
        except (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionError,
            socket.gaierror,
            TimeoutError,
            OSError,
        ) as e:
            raise MinerOfflineError(f"{cmd}: {e}") from e

    def _send_raw(self, payload: dict) -> dict:
        """Send `payload` exactly as given (no `cmd` injection) and return the parsed response.

        Mirrors `_send_plaintext` socket transport but bypasses the cmd-wrapper
        that `_send_plaintext` always prepends. Used by `_send_encrypted` to put
        an `{"enc": 1, "data": "..."}` envelope on the wire — the Whatsminer
        encrypted-command protocol does NOT carry a top-level `cmd` field.
        """
        message = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            with socket.create_connection((self.ip, self.port), timeout=15) as sock:
                sock.sendall(message)
                response = b""
                while True:
                    try:
                        chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                        if not chunk:
                            break
                        response = append_capped_response(response, chunk, command="raw")
                        try:
                            result = json.loads(response.decode("utf-8"))
                            if isinstance(result, dict) and result.get("STATUS") == "E":
                                msg = result.get("Msg", "unknown error")
                                desc = result.get("Description", "")
                                raise MinerCommandError(f"raw: {msg} ({desc})".strip(" ()"))
                            return result
                        except json.JSONDecodeError:
                            continue
                    except TimeoutError:
                        break
                raise MinerCommandError(f"raw: incomplete response ({len(response)} bytes)")
        except (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionError,
            socket.gaierror,
            TimeoutError,
            OSError,
        ) as e:
            raise MinerOfflineError(f"raw: {e}") from e

    def _get_token(self, force_refresh: bool = False) -> tuple[bytes, str]:
        if (
            not force_refresh
            and self._token_cache is not None
            and time.monotonic() - self._token_acquired_at < self._TOKEN_TTL_SEC
        ):
            return self._token_cache

        result = self._send_plaintext("get_token")
        # H616 stock firmware (M66S++_VM30 fw 20251209.16.Rel3) returns
        # `{"STATUS":"S","Code":131,"Msg":{salt, newsalt, time}}` for
        # successful get_token — the original Code 133 check was for an
        # older firmware revision that no longer matches in the field.
        # Validate the Msg shape directly so any future Code shift doesn't
        # bite again.
        msg = result.get("Msg") if isinstance(result, dict) else None
        if (
            result.get("STATUS") != "S"
            or not isinstance(msg, dict)
            or "salt" not in msg
            or "newsalt" not in msg
            or "time" not in msg
        ):
            raise MinerCommandError(f"get_token: {result}")

        salt = msg["salt"]
        newsalt = msg["newsalt"]
        time_str = msg["time"]

        aeskey = _compute_aeskey(self.password, salt)
        sign = _compute_sign(self.password, salt, newsalt, time_str)

        self._token_cache = (aeskey, sign)
        self._token_acquired_at = time.monotonic()
        self._aeskey = aeskey
        self._sign = sign
        return aeskey, sign

    def _send_encrypted(self, cmd: str, **params) -> dict:
        aeskey, sign = self._get_token()
        # H616 btminer rejects encrypted cmds with `json token err` when the
        # inner payload lacks the signed-token field. Embed `token: sign`
        # alongside the cmd + params so the firmware's auth check passes.
        api_str = json.dumps({"cmd": cmd, "token": sign, **params})
        encrypted = _encrypt(api_str, aeskey)
        payload = {"enc": 1, "data": encrypted}
        response = self._send_raw(payload)
        if "data" in response:
            decrypted = _decrypt(response["data"], aeskey)
            response = json.loads(decrypted)
        if response.get("STATUS") == "E":
            code = response.get("Code")
            if code in (135, 136):
                # Token expired or invalid, refresh and retry once. Rebuild
                # the inner payload with the NEW sign — without this, the
                # retry still carries the stale token and the firmware
                # rejects it again.
                aeskey, sign = self._get_token(force_refresh=True)
                api_str = json.dumps({"cmd": cmd, "token": sign, **params})
                encrypted = _encrypt(api_str, aeskey)
                payload = {"enc": 1, "data": encrypted}
                response = self._send_raw(payload)
                if "data" in response:
                    decrypted = _decrypt(response["data"], aeskey)
                    response = json.loads(decrypted)
                if response.get("STATUS") == "E":
                    code = response.get("Code")
                    if code == 45:
                        raise MinerCommandError(
                            f"{cmd}: {response.get('Msg', 'Permission denied')}"
                        )
                    elif code in (135, 136):
                        raise MinerCommandError(f"{cmd}: Token expired after retry")
                    elif response.get("Msg") == "enc json load err":
                        raise MinerCommandError(
                            f"{cmd}: encrypted command rejected by miner — likely "
                            f"wrong PASSWORD (re-scan via the dashboard's Scan now "
                            f"to revalidate against the password list)"
                        )
                    else:
                        raise MinerCommandError(f"{cmd}: {response.get('Msg', 'Unknown error')}")
            elif code == 45:
                raise MinerCommandError(f"{cmd}: {response.get('Msg', 'Permission denied')}")
            elif response.get("Msg") == "enc json load err":
                raise MinerCommandError(
                    f"{cmd}: encrypted command rejected by miner — likely "
                    f"wrong PASSWORD (re-scan via the dashboard's Scan now "
                    f"to revalidate against the password list)"
                )
            else:
                raise MinerCommandError(f"{cmd}: {response.get('Msg', 'Unknown error')}")
        return response

    def summary(self) -> MinerSummary:
        summary_resp = self._send_plaintext("summary")
        try:
            raw_devs = self._send_plaintext("devs")
        except (MinerCommandError, MinerOfflineError):
            raw_devs = None
        # H616 btminer firmware omits Miner Type / hostname / MAC from
        # `summary` — they live on the separate `get_version` and
        # `get_miner_info` plaintext cmds. Best-effort: a failure here is
        # non-fatal (the DTO degrades to model=None / hostname=None).
        try:
            raw_version = self._send_plaintext("get_version")
        except (MinerCommandError, MinerOfflineError):
            raw_version = None
        try:
            raw_miner_info = self._send_plaintext("get_miner_info")
        except (MinerCommandError, MinerOfflineError):
            raw_miner_info = None
        return MinerSummary.from_whatsminer(summary_resp, raw_devs, raw_version, raw_miner_info)

    def clocks(self) -> list[int]:
        return []

    def temps(self) -> list[int]:
        return []

    def temps_chip(self) -> list[list[int]]:
        return []

    def hashrate(self) -> float:
        return 0.0

    def capabilities(self) -> dict[str, Any]:
        return {}

    def voltages(self) -> list[float]:
        return []

    def set_voltage(self, voltage: float) -> bool:
        raise NotImplementedError("Whatsminer does not support voltage adjustment")

    def set_clock_all(self, clock: int) -> bool:
        raise NotImplementedError("Whatsminer does not support global clock adjustment")

    def set_clock_board(self, clocks: list[int]) -> bool:
        raise NotImplementedError("Whatsminer does not support board-level clock adjustment")

    def set_clock_chip(self, board: int, clocks: list[int]) -> bool:
        raise NotImplementedError("Whatsminer does not support chip-level clock adjustment")

    def set_coin(self, coin: str, pools: list[dict[str, Any]], unique_id: bool = False) -> bool:
        raise NotImplementedError("Whatsminer does not support coin switching")

    def set_perpetualtune(self, enabled: bool) -> bool:
        return True

    def set_power_limit(self, watts: int) -> bool:
        # _send_encrypted handles Code 135/136 token refresh + one retry
        # internally; an outer try/except retry here would double the socket
        # budget on every transient token-expired event (PR4 Hazard 2).
        result = self._send_encrypted("adjust_power_limit", power_limit=str(int(watts)))
        return result.get("STATUS") == "S"

    def set_power_mode(self, mode: str) -> bool:
        mode = mode.lower()
        if mode == "low":
            cmd = "set_low_power"
        elif mode == "normal":
            cmd = "set_normal_power"
        elif mode == "high":
            cmd = "set_high_power"
        else:
            raise ValueError(f"Invalid power mode: {mode}")
        result = self._send_encrypted(cmd)
        return result.get("STATUS") == "S"

    def set_target_freq(self, percent: float) -> bool:
        result = self._send_encrypted("set_target_freq", percent=str(int(percent)))
        return result.get("STATUS") == "S"

    def adjust_upfreq_speed(self, speed: int) -> bool:
        result = self._send_encrypted("adjust_upfreq_speed", upfreq_speed=str(int(speed)))
        return result.get("STATUS") == "S"

    def start_mining(self) -> bool:
        result = self._send_encrypted("start_mining")
        return result.get("STATUS") == "S"

    def stop_mining(self) -> bool:
        result = self._send_encrypted("stop_mining")
        return result.get("STATUS") == "S"

    def reboot(self, delay: int = 0) -> bool:
        # delay arg accepted for API compatibility with the abstract base; ignored
        # (Whatsminer firmware reboots immediately).
        result = self._send_encrypted("reboot")
        return result.get("STATUS") == "S"

    def authenticate(self) -> bool:
        try:
            self._send_plaintext("get_version")
            self._get_token()
            return True
        except (MinerOfflineError, MinerCommandError):
            return False

    def firmware_type(self) -> str:
        return "whatsminer"

    def tuning_strategy(self) -> str:
        return "power_limit_freq_search"

    def supports_per_chip_tuning(self) -> bool:
        return False

    def has_external_power_limit(self) -> bool:
        return True

    def has_capabilities_endpoint(self) -> bool:
        return False

    def has_internal_perpetual_tune(self) -> bool:
        return True

    def hardware_topology(self) -> HardwareTopology:
        if self._topology_cache is not None:
            return self._topology_cache
        try:
            response = self._send_plaintext("devs")
            num_boards = len(response.get("DEVS", []))
        except (MinerCommandError, MinerOfflineError):
            num_boards = 3
        topo = HardwareTopology(
            num_boards=num_boards,
            chips_per_board=0,
            psu_min_mv=11877,
            psu_max_mv=15182,
            psu_bounds_verified=False,
            psu_bounds_source="not-applicable:firmware-owned-vf",
        )
        self._topology_cache = topo
        return topo

    def devs(self) -> dict:
        return self._send_plaintext("devs")
