"""Tests use isolated settings, never the user's passwords or live backend."""

import pytest
import socket
from threading import local
from pathlib import Path
from tempfile import TemporaryDirectory

from core.config import LLMConfig, ServerConfig, settings


def pytest_addoption(parser):
    parser.addoption("--allow-network", action="store_true", help="Opt in to live network tests")


@pytest.fixture(autouse=True)
def prohibit_network(request, monkeypatch):
    """Catch accidental SDK, discovery, DNS and UDP traffic in offline tests."""
    if request.config.getoption("--allow-network"):
        return

    internal = local()
    socketpair = socket.socketpair

    def internal_socketpair(*args, **kwargs):
        # Windows builds asyncio's self-pipe using a private loopback socket pair.
        # Only this synchronous operation may connect; backend loopback is blocked.
        internal.socketpair = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            internal.socketpair = False

    def guard(original):
        def forbidden(*args, **kwargs):
            if getattr(internal, "socketpair", False):
                return original(*args, **kwargs)
            raise AssertionError("Live network access is forbidden in the normal test suite")

        return forbidden

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, guard(getattr(socket.socket, name)))
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, guard(getattr(socket, name)))
    monkeypatch.setattr(socket, "socketpair", internal_socketpair)


@pytest.fixture
def tmp_path():
    """Remove each test's temporary files on teardown, even after a failure."""
    with TemporaryDirectory(prefix="anyworld-test-") as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    """Contain default transcript/debug paths and restore cwd before deleting them."""
    monkeypatch.chdir(tmp_path)
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Replace the global settings with isolated test values."""
    monkeypatch.setattr(
        settings,
        "server",
        ServerConfig(
            host_password="test-host-password",
            player_password="test-player-password",
            allow_missing_origin=True,
        ),
    )
    monkeypatch.setattr(
        settings,
        "llm",
        LLMConfig(
            provider="openai",
            api_key="test-only",
            model_name="test-only",
            system_prompt="Keep coherent public outcomes; never expose private DM guidance.",
            tokenizer_encoding=None,
            initial_output_tokens=1024,
            round_output_tokens=2048,
            summary_output_tokens=1024,
        ),
    )
