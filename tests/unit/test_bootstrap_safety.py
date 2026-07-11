"""Startup must be import-pure and fail closed before authentication setup."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from tuner_app import main as main_mod
from tuner_app import state


def test_importing_main_does_not_create_runtime_files(tmp_path) -> None:
    data_dir = tmp_path / "runtime"
    env = os.environ.copy()
    env["ASIC_TUNER_DATA_DIR"] = str(data_dir)
    subprocess.run(
        [sys.executable, "-c", "import tuner_app.main; import tuner_app.__main__"],
        check=True,
        cwd=tmp_path,
        env=env,
    )
    assert not data_dir.exists()


def test_non_loopback_bind_refuses_unconfigured_auth() -> None:
    previous = dict(state.AUTH)
    state.AUTH.clear()
    state.AUTH.update({"password_hash": None, "created_at": None})
    try:
        with pytest.raises(RuntimeError, match="Refusing a non-loopback bind"):
            main_mod.build_server("0.0.0.0", 8099, manager_instance=object())
    finally:
        state.AUTH.clear()
        state.AUTH.update(previous)


def test_loopback_bind_is_allowed_before_setup() -> None:
    previous = dict(state.AUTH)
    state.AUTH.clear()
    state.AUTH.update({"password_hash": None, "created_at": None})
    sentinel = object()
    try:
        with patch("tuner_app.main.start_http_server", return_value=sentinel) as start:
            result = main_mod.build_server("127.0.0.1", 0, manager_instance=object())
        assert result is sentinel
        assert start.call_args.args[:2] == ("127.0.0.1", 0)
    finally:
        state.AUTH.clear()
        state.AUTH.update(previous)


def test_authenticated_non_loopback_bind_requires_explicit_host() -> None:
    previous = dict(state.AUTH)
    state.AUTH.clear()
    state.AUTH.update({"password_hash": "configured", "created_at": "now"})
    sentinel = object()
    try:
        with patch("tuner_app.main.start_http_server", return_value=sentinel):
            assert main_mod.build_server("0.0.0.0", 0, manager_instance=object()) is sentinel
    finally:
        state.AUTH.clear()
        state.AUTH.update(previous)
