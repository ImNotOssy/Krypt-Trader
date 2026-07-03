import pytest

import kalshi_auth


@pytest.fixture(autouse=True)
def _reset_server_offset():
    """server_now() is local time + measured Kalshi offset; clock-sync tests
    set the offset and would silently skew every close-time guard in later
    tests. Pin it to zero around each test."""
    old = kalshi_auth._server_offset_ms
    kalshi_auth._server_offset_ms = 0
    yield
    kalshi_auth._server_offset_ms = old
