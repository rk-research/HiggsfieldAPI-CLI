# Copyright (c) 2026 Richard Knuchel
# SPDX-License-Identifier: BSD-2-Clause
"""Run both CLI and MCP tests with process-wide outbound networking blocked.

Use ``python test_offline_suite.py`` for the offline acceptance suite.  The
blocker is installed before test modules are imported so even a regression in
their mocks cannot contact a real provider or download media.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import unittest
import ipaddress
import threading
from unittest import mock


class UnexpectedNetworkAccess(AssertionError):
    """Raised when any test attempts external DNS or socket I/O."""


def _blocked(*_args: object, **_kwargs: object) -> object:
    raise UnexpectedNetworkAccess("outgoing network access is disabled in offline tests")


_socket_connect = socket.socket.connect
_socket_connect_ex = socket.socket.connect_ex
_socket_sendto = socket.socket.sendto
_socketpair = socket.socketpair
_socketpair_state = threading.local()


def _is_loopback_address(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        # AF_UNIX sockets and opaque local addresses cannot reach Higgsfield.
        return True
    try:
        return ipaddress.ip_address(str(address[0]).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _block_external_connect(sock: socket.socket, address: object) -> object:
    if getattr(_socketpair_state, "active", False) and _is_loopback_address(address):
        return _socket_connect(sock, address)
    return _blocked(sock, address)


def _block_external_connect_ex(sock: socket.socket, address: object) -> object:
    if getattr(_socketpair_state, "active", False) and _is_loopback_address(address):
        return _socket_connect_ex(sock, address)
    return _blocked(sock, address)


def _block_external_sendto(sock: socket.socket, *args: object) -> object:
    return _blocked(sock, *args)


def _guarded_socketpair(*args: object, **kwargs: object) -> object:
    previous = getattr(_socketpair_state, "active", False)
    _socketpair_state.active = True
    try:
        return _socketpair(*args, **kwargs)
    finally:
        _socketpair_state.active = previous


@contextlib.contextmanager
def network_blocked():
    """Block external DNS and sockets while allowing Windows loopback internals."""
    with mock.patch.object(socket.socket, "connect", _block_external_connect), \
            mock.patch.object(socket.socket, "connect_ex", _block_external_connect_ex), \
            mock.patch.object(socket.socket, "sendto", _block_external_sendto), \
            mock.patch.object(socket, "socketpair", _guarded_socketpair), \
            mock.patch.object(socket, "create_connection", _blocked), \
            mock.patch.object(socket, "getaddrinfo", _blocked):
        yield


def main() -> int:
    # Synthetic credentials ensure the CLI never consumes credentials from the
    # caller's process environment.  The test suite still blocks every socket.
    synthetic_env = {
        "HF_API_KEY_ID": "offline-test-id",
        "HF_API_KEY_SECRET": "offline-test-secret",
        "HF_API_BASE_URL": "https://offline.invalid",
    }
    with mock.patch.dict(os.environ, synthetic_env), network_blocked():
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        for filename in ("test_higgsfield_cli_sdk.py", "test_higgsfield_mcp.py"):
            try:
                suite.addTests(loader.discover(".", pattern=filename))
            except ImportError as exc:
                print(f"Could not load {filename}: {exc}", file=sys.stderr)
                return 2
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print("DNS and socket egress were blocked for the full test run; loopback connect is allowed only inside socketpair creation for Windows event-loop internals.")
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
