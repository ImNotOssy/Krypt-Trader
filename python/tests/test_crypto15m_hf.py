"""Tests for the high-frequency 15m recorder + exit-latency study.

Covers the DB plumbing (batch insert, count, retention), the recorder's
write-on-change dedup against a synthetic WS state, and the latency_study
analysis pipeline over synthetic HF rows.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

import cf_ws
import crypto15m
import crypto15m_hf_record
import db
import hf_study
import kalshi_auth
import kalshi_ws
import spot_ws


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _now_ms() -> int:
    return int(time.time() * 1000)


# ───────── DB plumbing ─────────────────────────────────────────────────────

def test_hf_insert_and_count(fresh_db):
    now = _now_ms()
    rows = [
        {"ticker": "KXBTC15M-A", "asset": "BTC", "recv_ms": now + i * 200,
         "quote_ts_ms": now + i * 200, "mins_left": 5.0,
         "yes_bid": 40 + i, "yes_ask": 42 + i, "spot": 60000.0 + i,
         "spot_ms": now + i * 200, "spot_source": "coinbase_ws",
         "kalshi_env": "production"}
        for i in range(5)
    ]
    with db.get_db() as conn:
        n = db.insert_crypto15m_hf_ticks(conn, rows)
        assert n == 5
        assert db.crypto15m_hf_tick_count(conn) == 5


def test_hf_insert_empty_is_noop(fresh_db):
    with db.get_db() as conn:
        assert db.insert_crypto15m_hf_ticks(conn, []) == 0


def test_hf_retention_prunes_old(fresh_db):
    now = _now_ms()
    old_ms = int((datetime.now(timezone.utc)
                  - timedelta(days=db._C15_HF_KEEP_DAYS + 1)).timestamp() * 1000)
    with db.get_db() as conn:
        db.insert_crypto15m_hf_ticks(conn, [
            {"ticker": "OLD", "asset": "BTC", "recv_ms": old_ms,
             "yes_bid": 40, "yes_ask": 42, "spot": 1.0},
            {"ticker": "NEW", "asset": "BTC", "recv_ms": now,
             "yes_bid": 40, "yes_ask": 42, "spot": 1.0},
        ])
    db.cleanup_old_data()
    with db.get_db() as conn:
        left = [r[0] for r in conn.execute("SELECT ticker FROM crypto15m_ticks_hf")]
    assert left == ["NEW"]


# ───────── recorder write-on-change ────────────────────────────────────────

def test_recorder_writes_on_change_only(fresh_db, monkeypatch):
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _snap(_cfg):
        return {}
    monkeypatch.setattr(crypto15m, "snapshot", _snap)
    monkeypatch.setattr(crypto15m, "active_market_meta",
                        lambda: [{"ticker": "KXBTC15M-Z", "asset": "BTC",
                                  "closeTime": close, "minsLeft": 5.0}])
    monkeypatch.setattr(crypto15m, "active_tickers", lambda: set())
    monkeypatch.setattr(kalshi_ws, "is_connected", lambda: True)
    monkeypatch.setattr(kalshi_ws, "add_ticker_markets", lambda *_: None)
    monkeypatch.setattr(kalshi_ws, "set_cf_enabled", lambda *_: None)
    monkeypatch.setattr(spot_ws, "is_running", lambda: True)
    monkeypatch.setattr(spot_ws, "start", lambda: None)
    monkeypatch.setattr(spot_ws, "spot", lambda _a: 60000.0)
    monkeypatch.setattr(cf_ws, "spot", lambda _a: None)
    monkeypatch.setattr(kalshi_auth, "get_env", lambda: "production")

    quote = {"yes_bid_cents": 45, "yes_ask_cents": 47, "ts_ms": _now_ms()}
    monkeypatch.setattr(kalshi_ws, "ticker_quote", lambda _t: quote)

    rec = crypto15m_hf_record._Recorder()
    asyncio.run(rec.sample({}))   # first: writes an anchor row
    asyncio.run(rec.sample({}))   # unchanged quote + spot: deduped, no new row
    rec.flush()
    with db.get_db() as conn:
        assert db.crypto15m_hf_tick_count(conn) == 1

    # Move the quote → a new row is written.
    quote2 = {"yes_bid_cents": 52, "yes_ask_cents": 54, "ts_ms": _now_ms()}
    monkeypatch.setattr(kalshi_ws, "ticker_quote", lambda _t: quote2)
    asyncio.run(rec.sample({}))
    rec.close()
    with db.get_db() as conn:
        assert db.crypto15m_hf_tick_count(conn) == 2
        bids = [r[0] for r in conn.execute(
            "SELECT yes_bid FROM crypto15m_ticks_hf ORDER BY recv_ms")]
    assert bids == [45, 52]


# ───────── latency study ───────────────────────────────────────────────────

def _seed_window(conn, ticker: str, up_won: int, bids: list[int], *,
                 spot_path: list[float], start_ms: int, gap_ms: int = 250):
    """Seed one resolved window with an HF bid/spot path. yes_ask = bid+2."""
    conn.execute(
        """INSERT INTO crypto15m_signals (ticker, asset, series, favorite,
           favorite_price, entry_cost, resolved, up_won, close_time, kalshi_env)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ticker, "BTC", "KXBTC15M", "up", 0.5, 0.5, 1, up_won,
         "2026-07-03T12:15:00Z", "production"))
    rows = []
    for i, (b, sp) in enumerate(zip(bids, spot_path)):
        rows.append({
            "ticker": ticker, "asset": "BTC", "recv_ms": start_ms + i * gap_ms,
            "quote_ts_ms": start_ms + i * gap_ms,
            "mins_left": 8.0 - i * 0.05,  # early, decreasing
            "yes_bid": b, "yes_ask": b + 2, "spot": sp,
            "spot_ms": start_ms + i * gap_ms, "spot_source": "coinbase_ws",
            "kalshi_env": "production",
        })
    db.insert_crypto15m_hf_ticks(conn, rows)


def test_latency_study_smoke(fresh_db):
    now = _now_ms()
    with db.get_db() as conn:
        # spot rises (side=up); bid runs 50->80 (overshoot) then reverts to 5
        # (up loses). Oracle should capture the +30c peak; settle loses.
        n = 40
        bids = [50 + min(i, 12) * 2 for i in range(n // 2)] + \
               [80 - i * 6 for i in range(n // 2)]
        bids = [max(2, min(96, b)) for b in bids]
        spot = [60000.0 + i for i in range(n)]  # monotone up
        for k in range(25):  # enough windows to clear the anecdote gate
            _seed_window(conn, f"KXBTC15M-{k}", up_won=0, bids=bids,
                         spot_path=spot, start_ms=now + k * 10_000_000)

    out = hf_study.latency_study(env="production", since_hours=24 * 365,
                                 intervals_ms=[0, 1000, 25000])
    assert out["windows"] == 25
    rows = {r["intervalMs"]: r for r in out["byInterval"]}
    assert set(rows) == {0, 1000, 25000}
    for iv, r in rows.items():
        assert r["n"] == 25
        # oracle is an upper bound on any exit: never worse than settle
        assert r["oracleEvCents"] >= r["settleEvCents"] - 1e-6
    # Finer sampling can only raise (or match) the oracle ceiling.
    assert rows[0]["oracleEvCents"] >= rows[25000]["oracleEvCents"] - 1e-6
    assert out["medianSampleGapMs"] is not None


def test_latency_study_no_data(fresh_db):
    out = hf_study.latency_study(env="production")
    assert out["windows"] == 0
    assert any("anecdote" in c or "Only 0" in c for c in out["caveats"])
