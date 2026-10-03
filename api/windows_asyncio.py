"""Compatibility for Windows Proactor socket cleanup after a peer reset."""

import logging
import sys
from functools import wraps

logger = logging.getLogger(__name__)


class _ResetSafeSocket:
    """Delegate socket operations, tolerating only a reset during shutdown."""

    def __init__(self, sock):
        self._socket = sock

    def __getattr__(self, name):
        return getattr(self._socket, name)

    def shutdown(self, how):
        try:
            self._socket.shutdown(how)
        except ConnectionResetError as exc:
            if getattr(exc, "winerror", None) != 10054:
                raise
            logger.debug("Peer reset during Windows socket cleanup; closing socket.")


def install_windows_socket_cleanup() -> None:
    """Let Proactor cleanup finish when shutdown hits Windows error 10054.

    CPython's callback can stop at socket.shutdown(), before close() and server
    bookkeeping. Wrap only its socket shutdown operation rather than filtering
    asyncio logs or suppressing errors from the protocol's connection_lost().
    Keep the original callback so CPython owns the remaining cleanup steps.
    """
    if sys.platform != "win32":
        return

    from asyncio.proactor_events import _ProactorBasePipeTransport

    original = _ProactorBasePipeTransport._call_connection_lost
    if getattr(original, "_anyworld_reset_safe", False):
        return

    @wraps(original)
    def connection_lost(transport, exc):
        sock = transport._sock
        if sock is None or not hasattr(sock, "shutdown"):
            return original(transport, exc)
        safe_socket = _ResetSafeSocket(sock)
        transport._sock = safe_socket
        try:
            return original(transport, exc)
        finally:
            # Successful cleanup sets _sock to None. Preserve the real socket
            # if an unrelated error interrupted cleanup instead.
            if transport._sock is safe_socket:
                transport._sock = sock

    connection_lost._anyworld_reset_safe = True
    _ProactorBasePipeTransport._call_connection_lost = connection_lost
