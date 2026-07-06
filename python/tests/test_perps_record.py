"""perps_record + db perp_* helpers + backtest/replay loaders: flush batching,
REST snapshot rows, dedup/idempotency, backfill state, retention, reset, and
float conversion on the way out. No network — papi/pws are monkeypatched."""
from __future__ import annotations

import asyncio
import time

import pytest

import backtest
import db
import kalshi_perps_api as papi
import perps_record
import perps_ws as pws


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture(autouse=True)
def _reset_recorder_state(monkeypatch):
    monkeypatch.setattr(perps_record, "_last_rest", 0.0)
    monkeypatch.setattr(perps_record, "_last_est", 0.0)
    monkeypatch.setattr(perps_record, "_last_funding", 0.0)
    monkeypatch.setattr(perps_record, "_last_candles", 0.0)
    perps_record._est_cache.clear()
    perps_record._backfill_state.update(
        running=False, done=False, progress="", error=None)
    yield


def _tick_row(ticker="KXBTCPERP", ts_ms=1783295841500, env="production", src="ws"):
    return {
        "ticker": ticker, "ts_ms": ts_ms,
        "last_usd_micro": 6_354_200, "bid_usd_micro": 6_349_500,
        "ask_usd_micro": 6_353_000, "bid_size_cc": 120_000,
        "ask_size_cc": 80_000, "volume_24h_cc": 523_186_600,
        "oi_cc": 71_871_900, "ref_usd_micro": 6_352_900,
        "ref_ts_ms": 1783295841000, "settle_mark_usd_micro": 6_353_600,
        "liq_mark_usd_micro": 6_353_000, "funding_rate": 0.0002,
        "next_funding_ms": 1783310400000, "src": src, "kalshi_env": env,
    }


def _trade_row(trade_id="t1", env="production"):
    return {
        "trade_id": trade_id, "ticker": "KXBTCPERP", "ts_ms": 1783295841000,
        "price_usd_micro": 6_352_100, "count_cc": 500,
        "taker_side": "bid", "kalshi_env": env,
    }


# ───────── db helpers ────────────────────────────────────────────────────────

def test_insert_and_count(fresh_db):
    with db.get_db() as conn:
        assert db.insert_perp_ticks(conn, [_tick_row(), _tick_row(ts_ms=2)]) == 2
        assert db.insert_perp_trades(conn, [_trade_row()]) == 1
    with db.get_db() as conn:
        counts = db.perp_collection_counts(conn)
    assert counts["ticks"] == 2
    assert counts["trades"] == 1
    assert counts["byTicker"][0]["ticker"] == "KXBTCPERP"


def test_trade_dedup_on_trade_id(fresh_db):
    with db.get_db() as conn:
        db.insert_perp_trades(conn, [_trade_row("dup")])
        n2 = db.insert_perp_trades(conn, [_trade_row("dup"), _trade_row("new")])
        assert n2 == 1  # duplicate ignored
        total = conn.execute("SELECT COUNT(*) FROM perp_trades").fetchone()[0]
    assert total == 2
    # same trade_id under a DIFFERENT env is a distinct row (demo vs prod)
    with db.get_db() as conn:
        assert db.insert_perp_trades(conn, [_trade_row("dup", env="demo")]) == 1


def _candle_row(end_ts, close=6_350_000, env="production"):
    return {
        "ticker": "KXBTCPERP", "period_min": 1, "end_ts": end_ts,
        "bid_open_usd_micro": 6_349_000, "bid_high_usd_micro": 6_351_000,
        "bid_low_usd_micro": 6_348_000, "bid_close_usd_micro": close - 500,
        "ask_open_usd_micro": 6_350_000, "ask_high_usd_micro": 6_352_000,
        "ask_low_usd_micro": 6_349_000, "ask_close_usd_micro": close + 500,
        "trade_open_usd_micro": None, "trade_high_usd_micro": None,
        "trade_low_usd_micro": None, "trade_close_usd_micro": close,
        "trade_mean_usd_micro": None,
        "volume_cc": 1000, "volume_notional_usd_micro": 63_500_000,
        "oi_cc": 500, "kalshi_env": env,
    }


def test_candle_upsert_idempotent(fresh_db):
    with db.get_db() as conn:
        db.upsert_perp_candles(conn, [_candle_row(600)])
        db.upsert_perp_candles(conn, [_candle_row(600, close=6_400_000)])
        rows = conn.execute("SELECT COUNT(*), MAX(trade_close_usd_micro) "
                            "FROM perp_candles").fetchone()
    assert rows[0] == 1                # no duplicate period
    assert rows[1] == 6_400_000        # latest write wins
    with db.get_db() as conn:
        assert db.perp_last_candle_end_ts(conn, "KXBTCPERP", 1) == 600
        assert db.perp_last_candle_end_ts(conn, "KXBTCPERP", 60) is None


def test_funding_upsert_idempotent(fresh_db):
    row = {"ticker": "KXBTCPERP", "funding_time": "2026-07-05 20:00:00",
           "funding_rate": 0.0001, "mark_usd_micro": 6_273_900,
           "kalshi_env": "production"}
    with db.get_db() as conn:
        assert db.upsert_perp_funding(conn, [row]) == 1
        assert db.upsert_perp_funding(conn, [row]) == 0  # finalized rates never change
        assert db.perp_last_funding_time(conn) == "2026-07-05 20:00:00"


def test_retention_prunes_perp_tables(fresh_db):
    with db.get_db() as conn:
        db.insert_perp_ticks(conn, [_tick_row()])
        db.insert_perp_trades(conn, [_trade_row()])
        db.upsert_perp_candles(conn, [_candle_row(1_000)])  # epoch 1970 → ancient
        conn.execute("UPDATE perp_ticks SET observed_at='2020-01-01 00:00:00'")
        conn.execute("UPDATE perp_trades SET observed_at='2020-01-01 00:00:00'")
    db.cleanup_old_data()
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM perp_ticks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM perp_trades").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM perp_candles").fetchone()[0] == 0


def test_factory_reset_wipes_perp_tables(fresh_db):
    with db.get_db() as conn:
        db.insert_perp_ticks(conn, [_tick_row()])
    summary = db.factory_reset()
    assert summary.get("perp_ticks") == 1
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM perp_ticks").fetchone()[0] == 0


# ───────── recorder ──────────────────────────────────────────────────────────

def _quiet_papi(monkeypatch):
    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return []

    monkeypatch.setattr(papi, "fetch_perps_markets", empty)
    monkeypatch.setattr(papi, "fetch_funding_rate_estimate", none)
    monkeypatch.setattr(papi, "fetch_funding_rates_historical", empty)
    monkeypatch.setattr(papi, "fetch_perps_candlesticks_range", empty)


def test_flush_ws_batches_to_db(fresh_db, monkeypatch):
    _quiet_papi(monkeypatch)
    monkeypatch.setattr(pws, "drain_ticks", lambda: [_tick_row(), _tick_row(ts_ms=2)])
    monkeypatch.setattr(pws, "drain_trades", lambda: [_trade_row()])
    out = asyncio.run(perps_record.record_tick({"perps_record_signals": True}))
    assert out["ticks"] == 2
    assert out["trades"] == 1
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM perp_ticks").fetchone()[0] == 2


def test_rest_snapshot_rows_always_production(fresh_db, monkeypatch):
    """Public REST reads always hit prod — rows must say so even if the app
    is in demo env."""
    async def fake_markets(*a, **k):
        return [{
            "ticker": "KXBTCPERP", "status": "active",
            "price": "6.3542", "bid": "6.3495", "ask": "6.3530",
            "volume_24h": "5231866.00", "open_interest": "718719.00",
            "reference_price": {"price": "6.3529", "ts_ms": 1},
            "settlement_mark_price": {"price": "6.3536", "ts_ms": 2},
            "liquidation_mark_price": {"price": "6.3530", "ts_ms": 3},
        }]

    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return []

    monkeypatch.setattr(papi, "fetch_perps_markets", fake_markets)
    monkeypatch.setattr(papi, "fetch_funding_rate_estimate", none)
    monkeypatch.setattr(papi, "fetch_funding_rates_historical", empty)
    monkeypatch.setattr(papi, "fetch_perps_candlesticks_range", empty)
    monkeypatch.setattr(pws, "drain_ticks", lambda: [])
    monkeypatch.setattr(pws, "drain_trades", lambda: [])

    asyncio.run(perps_record.record_tick(
        {"perps_record_signals": True, "perps_symbols": ["KXBTCPERP"]}))
    with db.get_db() as conn:
        row = conn.execute("SELECT src, kalshi_env, bid_usd_micro FROM perp_ticks").fetchone()
    assert row[0] == "rest"
    assert row[1] == "production"
    assert row[2] == 6_349_500


def test_record_tick_never_raises(fresh_db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    def boom_sync():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(papi, "fetch_perps_markets", boom)
    monkeypatch.setattr(papi, "fetch_funding_rate_estimate", boom)
    monkeypatch.setattr(papi, "fetch_funding_rates_historical", boom)
    monkeypatch.setattr(papi, "fetch_perps_candlesticks_range", boom)
    monkeypatch.setattr(pws, "drain_ticks", boom_sync)
    monkeypatch.setattr(pws, "drain_trades", boom_sync)
    out = asyncio.run(perps_record.record_tick({"perps_record_signals": True}))
    assert isinstance(out, dict)  # every phase failed; nothing propagated


def test_candle_topup_resumes_from_last(fresh_db, monkeypatch):
    seen = {}
    with db.get_db() as conn:
        db.upsert_perp_candles(conn, [_candle_row(int(time.time()) - 3600)])

    async def fake_range(ticker, start, end, period, **k):
        seen[ticker] = start
        return []

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    monkeypatch.setattr(papi, "fetch_perps_candlesticks_range", fake_range)
    monkeypatch.setattr(papi, "fetch_perps_markets", empty)
    monkeypatch.setattr(papi, "fetch_funding_rate_estimate", none)
    monkeypatch.setattr(papi, "fetch_funding_rates_historical", empty)
    monkeypatch.setattr(pws, "drain_ticks", lambda: [])
    monkeypatch.setattr(pws, "drain_trades", lambda: [])

    asyncio.run(perps_record.record_tick(
        {"perps_record_signals": True, "perps_symbols": ["KXBTCPERP"]}))
    with db.get_db() as conn:
        last = db.perp_last_candle_end_ts(conn, "KXBTCPERP", 1)
    assert seen["KXBTCPERP"] == last + 60


def test_backfill_idempotent_and_state(fresh_db, monkeypatch):
    now = int(time.time())

    async def fake_range(ticker, start, end, period, **k):
        return [{"end_period_ts": now - 60,
                 "bid": {"open": "6.0", "high": "6.1", "low": "5.9", "close": "6.05"},
                 "ask": {"open": "6.1", "high": "6.2", "low": "6.0", "close": "6.15"},
                 "price": {"open": None, "high": None, "low": None,
                           "close": None, "mean": None, "previous": "6.0"},
                 "volume": "0.00", "volume_notional_value_dollars": "0.00",
                 "open_interest": "10.00"}]

    async def fake_funding(*a, **k):
        return [{"market_ticker": "KXBTCPERP",
                 "funding_time": "2026-07-05T20:00:00Z",
                 "funding_rate": 0, "mark_price": "6.2739"}]

    monkeypatch.setattr(papi, "fetch_perps_candlesticks_range", fake_range)
    monkeypatch.setattr(papi, "fetch_funding_rates_historical", fake_funding)

    cfg = {"perps_symbols": ["KXBTCPERP"], "perps_backfill_days": 1}
    out1 = asyncio.run(perps_record.backfill(cfg))
    assert perps_record._backfill_state["done"] is True
    assert not perps_record._backfill_state["running"]
    assert out1["funding"] == 1
    # rerun: same counts requested, no duplicate rows
    perps_record._backfill_state["done"] = False
    asyncio.run(perps_record.backfill(cfg))
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM perp_candles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM perp_funding").fetchone()[0] == 1
        # trade OHLC survived as NULL (no-trade candle)
        assert conn.execute(
            "SELECT trade_close_usd_micro FROM perp_candles").fetchone()[0] is None


def test_funding_rows_normalized_rfc3339(fresh_db, monkeypatch):
    async def fake_funding(*a, **k):
        return [{"market_ticker": "KXBTCPERP",
                 "funding_time": "2026-07-05T12:00:00.500Z",
                 "funding_rate": -0.00104, "mark_price": "6.2619"}]

    monkeypatch.setattr(papi, "fetch_funding_rates_historical", fake_funding)
    asyncio.run(perps_record._funding_topup({}))
    with db.get_db() as conn:
        row = conn.execute("SELECT funding_time, funding_rate FROM perp_funding").fetchone()
    assert row[0] == "2026-07-05 12:00:00"
    assert row[1] == pytest.approx(-0.00104)


def test_status_shape(fresh_db):
    s = perps_record.status({"perps_record_signals": True})
    for key in ("recording", "wsConnected", "ws", "symbols", "counts", "backfill"):
        assert key in s
    assert s["recording"] is True


def test_symbols_sanitized():
    cfg = {"perps_symbols": ["kxbtcperp", "KXETHPERP1", "JUNK", "KXBTCPERP"]}
    syms = perps_record._symbols(cfg)
    assert syms == ["KXBTCPERP", "KXETHPERP"]


# ───────── loaders ───────────────────────────────────────────────────────────

def test_loaders_float_conversion_and_env_filter(fresh_db):
    with db.get_db() as conn:
        db.insert_perp_ticks(conn, [
            _tick_row(), _tick_row(ticker="KXBTCPERP1", env="demo"),
        ])
        db.insert_perp_trades(conn, [_trade_row()])
        db.upsert_perp_candles(conn, [_candle_row(int(time.time()) - 60)])
        db.upsert_perp_funding(conn, [{
            "ticker": "KXBTCPERP", "funding_time": "2026-07-05 20:00:00",
            "funding_rate": 0.0001, "mark_usd_micro": 6_273_900,
            "kalshi_env": "production",
        }])

    import sqlite3
    conn = sqlite3.connect(str(db.db_path()))
    try:
        ticks = backtest.load_perp_ticks(conn, "KXBTCPERP")
        assert len(ticks) == 1                      # demo row excluded
        assert ticks[0]["bid"] == pytest.approx(6.3495)
        assert ticks[0]["bid_size"] == pytest.approx(1200.0)

        demo = backtest.load_perp_ticks(conn, "KXBTCPERP1", env="demo")
        assert len(demo) == 1

        trades = backtest.load_perp_trades(conn, "KXBTCPERP")
        assert trades[0]["price"] == pytest.approx(6.3521)
        assert trades[0]["count"] == pytest.approx(5.0)

        candles = backtest.load_perp_candles(conn, "KXBTCPERP")
        assert candles[0]["ask_close"] == pytest.approx(6.3505)
        assert candles[0]["close"] == pytest.approx(6.35)
        assert candles[0]["mean"] is None           # None-safe nullable OHLC

        funding = backtest.load_perp_funding(conn, "KXBTCPERP")
        assert funding[0]["mark"] == pytest.approx(6.2739)

        summary = backtest.perp_dataset_summary(conn)
        assert summary["tickers"][0]["ticker"] == "KXBTCPERP"
        assert summary["tickers"][0]["ticks"] == 1
    finally:
        conn.close()


def test_perp_series_ships_caveats(fresh_db):
    with db.get_db() as conn:
        db.insert_perp_ticks(conn, [_tick_row()])
    import replay
    out = replay.perp_series({}, "KXBTCPERP")
    assert out["ticker"] == "KXBTCPERP"
    assert len(out["ticks"]) == 1
    assert out["caveats"], "honesty caveats must ship with the series"


# ───────── perps wallet (separate margin wallet) ─────────────────────────────

def _wallet_resp():
    # Kalshi /margin/balance shape (FixedPointDollars strings).
    return {
        "settled_funds": "10.5000",
        "subaccount_balances": [
            {"subaccount": 0, "available_balance": "8.2500",
             "position_value": "2.2500", "resting_orders_margin": "1.0000",
             "maintenance_margin": "0.5000"},
            {"subaccount": 1, "available_balance": "99.0000"},
        ],
    }


def test_wallet_parses_margin_balance(monkeypatch):
    import kalshi_auth
    monkeypatch.setattr(perps_record, "_wallet_cache", {"t": 0.0, "data": None})
    monkeypatch.setattr(kalshi_auth, "credentials_present", lambda env: True)

    async def bal(**kw):
        return _wallet_resp()

    monkeypatch.setattr(papi, "get_perps_balance", bal)
    w = asyncio.run(perps_record.wallet("production"))
    assert w == {
        "env": "production",
        "settledUsd": 10.5,
        "availableUsd": 8.25,          # subaccount 0 only, not the sub-1 $99
        "positionValueUsd": 2.25,
        "restingMarginUsd": 1.0,
        "maintenanceMarginUsd": 0.5,
    }


def test_wallet_no_creds_returns_none(monkeypatch):
    import kalshi_auth
    monkeypatch.setattr(perps_record, "_wallet_cache", {"t": 0.0, "data": None})
    monkeypatch.setattr(kalshi_auth, "credentials_present", lambda env: False)
    called = {"n": 0}

    async def bal(**kw):
        called["n"] += 1
        return _wallet_resp()

    monkeypatch.setattr(papi, "get_perps_balance", bal)
    assert asyncio.run(perps_record.wallet("production")) is None
    assert called["n"] == 0             # never hit the API without creds


def test_wallet_never_flashes_zero_on_failure(monkeypatch):
    """A good snapshot then a failed/malformed poll must serve last-known, not
    None/zero (balance-flash-zero guard)."""
    import kalshi_auth
    monkeypatch.setattr(perps_record, "_wallet_cache", {"t": 0.0, "data": None})
    monkeypatch.setattr(kalshi_auth, "credentials_present", lambda env: True)

    async def good(**kw):
        return _wallet_resp()

    monkeypatch.setattr(papi, "get_perps_balance", good)
    first = asyncio.run(perps_record.wallet("production"))
    assert first["settledUsd"] == 10.5

    # Expire the cache so the next call refetches, but make the fetch fail.
    perps_record._wallet_cache["t"] = time.monotonic() - 999

    async def boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(papi, "get_perps_balance", boom)
    assert asyncio.run(perps_record.wallet("production")) == first  # last-known

    # Malformed (no primary subaccount) must also not clobber good data.
    perps_record._wallet_cache["t"] = time.monotonic() - 999

    async def malformed(**kw):
        return {"subaccount_balances": [{"subaccount": 1, "available_balance": "1.0"}]}

    monkeypatch.setattr(papi, "get_perps_balance", malformed)
    assert asyncio.run(perps_record.wallet("production")) == first
