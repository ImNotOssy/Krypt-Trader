from __future__ import annotations

import threading
import time

import kalshi_auth


def test_now_ms_does_not_block_on_periodic_resync(monkeypatch):
    # now_ms() runs on the event loop for every signed request. When the resync
    # interval has elapsed it must offload the (up to 5s) HTTP HEAD to a
    # background thread and return immediately, not freeze the loop.
    monkeypatch.setattr(kalshi_auth, "_last_sync", 0.0)      # force interval elapsed
    monkeypatch.setattr(kalshi_auth, "_sync_in_progress", False)
    monkeypatch.setattr(kalshi_auth, "_server_offset_ms", 0)

    gate = threading.Event()
    calls = {"n": 0}

    def slow_sync(force=False):
        calls["n"] += 1
        gate.wait(2.0)   # simulate a slow/hanging HEAD
        return 0

    monkeypatch.setattr(kalshi_auth, "sync_server_time", slow_sync)

    t0 = time.perf_counter()
    val = kalshi_auth.now_ms()
    elapsed = time.perf_counter() - t0

    assert isinstance(val, int)
    assert elapsed < 0.5, f"now_ms blocked for {elapsed:.2f}s on the resync"

    # A second call while the sync is still in flight must NOT spawn another.
    kalshi_auth.now_ms()

    gate.set()  # let the background sync finish
    for _ in range(200):
        with kalshi_auth._sync_lock:
            done = not kalshi_auth._sync_in_progress
        if done:
            break
        time.sleep(0.01)

    assert calls["n"] == 1  # single-flight: exactly one background sync
    assert kalshi_auth._sync_in_progress is False


def test_now_ms_skips_resync_within_interval(monkeypatch):
    # Freshly synced (interval not elapsed) → no HEAD, no thread, just arithmetic.
    monkeypatch.setattr(kalshi_auth, "_last_sync", time.time())
    monkeypatch.setattr(kalshi_auth, "_sync_in_progress", False)
    monkeypatch.setattr(kalshi_auth, "_server_offset_ms", 1234)

    calls = {"n": 0}
    monkeypatch.setattr(
        kalshi_auth, "sync_server_time",
        lambda force=False: calls.__setitem__("n", calls["n"] + 1),
    )

    val = kalshi_auth.now_ms()
    assert isinstance(val, int)
    assert calls["n"] == 0  # no resync triggered
