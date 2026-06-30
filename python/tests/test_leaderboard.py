"""Tests for the anonymous community leaderboard reporter (leaderboard.py).

Pure helpers (profitability gate, secret-stripping, embed/payload shape, anon
id), plus the rate-limit failover transport and the maybe_report orchestration
— all with httpx fully stubbed (no network).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import leaderboard
from config import merge_with_defaults


def run_async(coro):
    return asyncio.run(coro)


# ───────── profitability gate ──────────────────────────────────────────


def test_is_profitable_session_green():
    assert leaderboard.is_profitable(
        {"totalUsd": 120, "sessionPnlUsd": 5, "alltimePnlUsd": -3}
    )


def test_not_profitable_when_session_not_green_even_if_alltime_green():
    # All-time green no longer qualifies — only a positive SESSION posts.
    assert not leaderboard.is_profitable(
        {"totalUsd": 120, "sessionPnlUsd": 0, "alltimePnlUsd": 40}
    )
    assert not leaderboard.is_profitable(
        {"totalUsd": 120, "sessionPnlUsd": -5.42, "alltimePnlUsd": 40}
    )


def test_not_profitable_when_both_red():
    assert not leaderboard.is_profitable(
        {"totalUsd": 80, "sessionPnlUsd": -5, "alltimePnlUsd": -20}
    )


def test_not_profitable_when_no_funds():
    assert not leaderboard.is_profitable(
        {"totalUsd": 0, "sessionPnlUsd": 5, "alltimePnlUsd": 5}
    )


def test_is_profitable_garbage_is_safe():
    assert not leaderboard.is_profitable({"totalUsd": "x", "sessionPnlUsd": None})


# ───────── secret / PII stripping ──────────────────────────────────────


def test_sanitize_strips_secrets_keeps_strategy():
    cfg = merge_with_defaults({})
    cfg["stats_webhook_url"] = "https://discord.com/api/webhooks/secret"
    clean = leaderboard.sanitize_profile(cfg)

    # secrets / identifiers gone
    for k in clean:
        kl = k.lower()
        assert "webhook" not in kl
        assert "url" not in kl
        assert "key" not in kl
    assert "stats_webhook_url" not in clean
    assert "event_webhook_url" not in clean

    # strategy settings preserved
    assert "trade_whales" in clean
    assert "trade_momentum" in clean
    assert "crypto15m_use_rules" in clean
    assert clean["crypto15m_enabled"] == cfg["crypto15m_enabled"]


def test_sanitize_handles_none():
    assert leaderboard.sanitize_profile(None) == {}


# ───────── embed shape ─────────────────────────────────────────────────


def test_build_embed_shape():
    snap = {
        "totalUsd": 142.5, "sessionPnlUsd": 12.3, "sessionRoiPct": 9.4,
        "alltimePnlUsd": 30.0, "roiPct": 25.0, "winRate": 60.0,
        "wins": 6, "losses": 4, "totalOpened": 10,
    }
    cfg = merge_with_defaults({})
    cfg["trade_whales"] = True
    cfg["crypto15m_enabled"] = True
    cfg["crypto15m_use_rules"] = True
    e = leaderboard.build_embed(snap, cfg)
    assert e["title"].startswith("🏆")
    assert "A user" in e["description"]
    names = {f["name"]: f["value"] for f in e["fields"]}
    assert "whale" in names["Engines"] and "crypto15m" in names["Engines"]
    assert names["Rules mode"] == "on"
    assert names["Trades"] == "10"


# ───────── transport: rate-limit failover ──────────────────────────────


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Returns queued responses in order, one per .post() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(url)
        if not self._responses:
            raise AssertionError("more posts than queued responses")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _patch_client(monkeypatch, responses):
    holder = {}

    def _factory(*a, **k):
        c = _FakeClient(responses)
        holder["client"] = c
        return c

    monkeypatch.setattr(leaderboard.httpx, "AsyncClient", _factory)
    # deterministic order: primary first, backup second
    monkeypatch.setattr(leaderboard.random, "shuffle", lambda seq: None)
    return holder


def test_failover_primary_429_uses_backup(monkeypatch):
    holder = _patch_client(monkeypatch, [_FakeResp(429, "rate limited"), _FakeResp(204)])
    ok = run_async(leaderboard._post_with_failover({"x": 1}, b"{}"))
    assert ok is True
    assert len(holder["client"].calls) == 2  # primary (429) then backup (204)


def test_failover_both_429_returns_false(monkeypatch):
    _patch_client(monkeypatch, [_FakeResp(429), _FakeResp(429)])
    ok = run_async(leaderboard._post_with_failover({"x": 1}, b"{}"))
    assert ok is False


def test_failover_first_success_skips_backup(monkeypatch):
    holder = _patch_client(monkeypatch, [_FakeResp(200)])
    ok = run_async(leaderboard._post_with_failover({"x": 1}, b"{}"))
    assert ok is True
    assert len(holder["client"].calls) == 1  # backup never touched


def test_failover_exception_then_success(monkeypatch):
    holder = _patch_client(monkeypatch, [RuntimeError("boom"), _FakeResp(200)])
    ok = run_async(leaderboard._post_with_failover({"x": 1}, b"{}"))
    assert ok is True
    assert len(holder["client"].calls) == 2


# ───────── maybe_report orchestration ──────────────────────────────────


def _capture_post(monkeypatch):
    sent = {}

    async def _fake(payload, file_bytes):
        sent["payload"] = payload
        sent["record"] = json.loads(file_bytes.decode("utf-8"))
        return True

    monkeypatch.setattr(leaderboard, "_post_with_failover", _fake)
    monkeypatch.setattr(leaderboard, "DISABLED", False)
    return sent


def test_maybe_report_posts_anonymous_sanitized(monkeypatch):
    sent = _capture_post(monkeypatch)
    cfg = merge_with_defaults({})
    cfg["stats_webhook_url"] = "https://discord.com/api/webhooks/leak"
    snap = {"totalUsd": 100, "sessionPnlUsd": 9, "alltimePnlUsd": 9, "wins": 1, "losses": 0}

    ok = run_async(leaderboard.maybe_report(snap, cfg, "production", authed=True))
    assert ok is True
    rec = sent["record"]
    # No per-install id is ever sent — reports are just "a user".
    assert "anonId" not in rec and rec["app"] == "krypt-trader"
    assert rec["pnl"]["sessionPnlUsd"] == 9
    # the user's own webhook URL never leaves the machine
    assert "stats_webhook_url" not in rec["profile"]
    assert json.dumps(rec).find("discord.com/api/webhooks/leak") == -1


def test_maybe_report_skips_red_session_even_when_alltime_green(monkeypatch):
    # The exact reported bug: a losing session (-5.42) leaked through because
    # all-time was green. Session is now the only gate — it must NOT post, and
    # there is no startup/always-send bypass.
    sent = _capture_post(monkeypatch)
    snap = {"totalUsd": 120, "sessionPnlUsd": -5.42, "alltimePnlUsd": 40}
    ok = run_async(leaderboard.maybe_report(snap, {}, "production", authed=True))
    assert ok is False
    assert "payload" not in sent


def test_maybe_report_skips_red(monkeypatch):
    # Every report is profitable-only (positive session) — there is no startup
    # or always-send path. A red session never posts.
    sent = _capture_post(monkeypatch)
    snap = {"totalUsd": 100, "sessionPnlUsd": -1, "alltimePnlUsd": -1}
    ok = run_async(leaderboard.maybe_report(snap, {}, "production", authed=True))
    assert ok is False
    assert "payload" not in sent


def test_maybe_report_skips_when_not_authed(monkeypatch):
    sent = _capture_post(monkeypatch)
    snap = {"totalUsd": 100, "sessionPnlUsd": 9, "alltimePnlUsd": 9}
    ok = run_async(leaderboard.maybe_report(snap, {}, "production", authed=False))
    assert ok is False
    assert "payload" not in sent


def test_maybe_report_skips_when_disabled(monkeypatch):
    sent = _capture_post(monkeypatch)
    monkeypatch.setattr(leaderboard, "DISABLED", True)
    snap = {"totalUsd": 100, "sessionPnlUsd": 9, "alltimePnlUsd": 9}
    ok = run_async(leaderboard.maybe_report(snap, {}, "production", authed=True))
    assert ok is False
    assert "payload" not in sent
