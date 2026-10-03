"""Exercise Windows socket cleanup without network calls or a live server."""

import asyncio
import inspect
import socket
from asyncio.proactor_events import _ProactorBasePipeTransport
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import server, windows_asyncio
from test_engine import FakeResolver


@pytest.fixture
def cleanup_callback(monkeypatch):
    """Restore the real CPython callback before each compatibility test."""
    original = inspect.unwrap(_ProactorBasePipeTransport._call_connection_lost)
    monkeypatch.setattr(_ProactorBasePipeTransport, "_call_connection_lost", original)
    monkeypatch.setattr(windows_asyncio, "sys", SimpleNamespace(platform="win32"))
    windows_asyncio.install_windows_socket_cleanup()
    return _ProactorBasePipeTransport._call_connection_lost


def reset_error(code):
    error = ConnectionResetError("Peer reset")
    if code is not None:
        error.winerror = code
    return error


def transport_with_socket(error=None, protocol_error=None):
    sock = Mock()
    sock.fileno.return_value = 42
    sock.shutdown.side_effect = error
    protocol = Mock()
    protocol.connection_lost.side_effect = protocol_error
    return (
        SimpleNamespace(
            _sock=sock,
            _protocol=protocol,
            _server=Mock(),
            _called_connection_lost=False,
        ),
        sock,
    )


@pytest.mark.parametrize("reset", [False, True])
def test_cleanup_closes_socket_and_detaches_server(cleanup_callback, reset):
    """A peer reset must finish cleanup, with no asyncio callback exception."""
    transport, sock = transport_with_socket(reset_error(10054) if reset else None)
    protocol, attached_server = transport._protocol, transport._server

    async def run():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        loop.call_soon(cleanup_callback, transport, None)
        await asyncio.sleep(0)
        assert errors == []

    asyncio.run(run())
    sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    sock.close.assert_called_once_with()
    protocol.connection_lost.assert_called_once_with(None)
    attached_server._detach.assert_called_once()
    assert transport._sock is None
    assert transport._server is None
    assert transport._called_connection_lost
    cleanup_callback(transport, None)
    protocol.connection_lost.assert_called_once_with(None)


@pytest.mark.parametrize("code", [10053, None])
def test_other_shutdown_errors_still_propagate(cleanup_callback, code):
    error = reset_error(code)
    transport, sock = transport_with_socket(error)
    with pytest.raises(ConnectionResetError) as caught:
        cleanup_callback(transport, None)
    assert caught.value is error
    assert transport._sock is sock
    sock.close.assert_not_called()


def test_protocol_reset_still_reaches_asyncio_handler(cleanup_callback):
    """Even error 10054 remains visible when raised by the protocol callback."""
    error = reset_error(10054)
    transport, sock = transport_with_socket(reset_error(10054), error)

    async def run():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        loop.call_soon(cleanup_callback, transport, error)
        await asyncio.sleep(0)
        assert len(errors) == 1
        assert errors[0]["exception"] is error

    asyncio.run(run())
    sock.close.assert_called_once_with()
    assert transport._sock is None


def test_socket_close_errors_still_propagate(cleanup_callback):
    transport, sock = transport_with_socket(reset_error(10054))
    error = OSError("Close failed")
    sock.close.side_effect = error
    with pytest.raises(OSError) as caught:
        cleanup_callback(transport, None)
    assert caught.value is error
    assert transport._sock is sock


def test_installation_is_idempotent(cleanup_callback):
    windows_asyncio.install_windows_socket_cleanup()
    assert _ProactorBasePipeTransport._call_connection_lost is cleanup_callback


def test_other_platforms_are_unchanged(monkeypatch):
    original = _ProactorBasePipeTransport._call_connection_lost
    monkeypatch.setattr(windows_asyncio, "sys", SimpleNamespace(platform="linux"))
    windows_asyncio.install_windows_socket_cleanup()
    assert _ProactorBasePipeTransport._call_connection_lost is original


def test_app_lifespan_installs_cleanup(monkeypatch):
    install = Mock()
    monkeypatch.setattr(server, "install_windows_socket_cleanup", install)
    app = server.create_app(FakeResolver)

    async def run():
        async with app.router.lifespan_context(app):
            install.assert_called_once_with()

    asyncio.run(run())
