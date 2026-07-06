"""perps_strategy: feature computation, gates, honest backtest fills
(taker-at-ask, maker trade-through, SL-first, fees, funding, liquidation),
and the paper engine lifecycle. Synthetic bars; no network."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

import db
import kalshi_auth
import perps_strategy as ps
import perps_ws as pws


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _bar(end_ts, px, *, spread=0.002, vol=10.0, oi=100.0, trade=True):
    """Synthetic loader-shaped bar around price px (USD floats)."""
    bid, ask = px - spread / 2, px + spread / 2
    return {
        "end_ts": end_ts,
        "bid_open": bid, "bid_high": bid, "bid_low": bid, "bid_close": bid,
        "ask_open": ask, "ask_high": ask, "ask_low": ask, "ask_close": ask,
        "open": px if trade else None, "high": px if trade else None,
        "low": px if trade else None, "close": px if trade else None,
        "mean": None, "volume": vol, "volume_usd": vol * px, "oi": oi,
    }


def _bars(n=120, base=6.35, start_ts=1_783_200_000):
    return [_bar(start_ts + i * 60, base) for i in range(n)]


BASE_CFG = {
    "perps_strat_symbol": "KXBTCPERP",
    "perps_strat_direction": "long",
    "perps_strat_entry_style": "taker",
    "perps_strat_rules": [{"field": "ret5mBps", "op": ">", "value": 5.0}],
    "perps_strat_contracts": 1,
    "perps_strat_leverage": 1.0,
    "perps_strat_tp_bps": 30.0,
    "perps_strat_sl_bps": 20.0,
    "perps_strat_max_hold_min": 60.0,
    "perps_strat_exit_on_rules_fail": False,
    "perps_strat_daily_loss_usd": 5.0,
    "perps_strat_max_notional_usd": 100.0,
    "perps_strat_fee_era": "jul8",
}


# ───────── features + gates ──────────────────────────────────────────────────

def test_compute_features_basics():
    bars = _bars(120)
    bars[-1] = _bar(bars[-1]["end_ts"], 6.35 * 1.001)  # +10bps last minute
    f = ps.compute_features(bars, len(bars) - 1, funding_rate_bps=1.5)
    assert f is not None
    assert f["ret1mBps"] == pytest.approx(10.0, abs=0.5)
    assert f["spreadBps"] == pytest.approx(0.002 / 6.35 * 10_000, rel=0.05)
    assert f["fundingRateBps"] == 1.5
    assert 0 <= f["hourUtc"] <= 23
    assert 0 <= f["minsToFunding"] <= 480


def test_compute_features_needs_window():
    assert ps.compute_features(_bars(30), 29, None) is None


def test_should_enter_rules_and_empty():
    f = {"ret5mBps": 10.0}
    ok, _ = ps.should_enter(f, {"perps_strat_rules": [{"field": "ret5mBps", "op": ">", "value": 5}]})
    assert ok
    ok, why = ps.should_enter(f, {"perps_strat_rules": []})
    assert not ok and "no entry rules" in why


def test_should_exit_tp_sl_hold():
    cfg = dict(BASE_CFG)
    f = {"price": 6.35 * (1 + 0.0035)}  # +35bps
    done, r = ps.should_exit(f, side="long", entry_px=6.35, held_min=5, cfg=cfg)
    assert done and r == "tp"
    f = {"price": 6.35 * (1 - 0.0025)}  # -25bps
    done, r = ps.should_exit(f, side="long", entry_px=6.35, held_min=5, cfg=cfg)
    assert done and r == "sl"
    # short direction mirrors: -35bps move = +35bps gain for a short → tp
    f = {"price": 6.35 * (1 - 0.0035)}
    done, r = ps.should_exit(f, side="short", entry_px=6.35, held_min=5, cfg=cfg)
    assert done and r == "tp"
    f = {"price": 6.35}
    done, r = ps.should_exit(f, side="long", entry_px=6.35, held_min=90, cfg=cfg)
    assert done and r == "max_hold"


# ───────── backtest ──────────────────────────────────────────────────────────

def _seed_candles(bars, ticker="KXBTCPERP"):
    u = lambda v: int(round(v * 1e6)) if v is not None else None
    rows = []
    for b in bars:
        rows.append({
            "ticker": ticker, "period_min": 1, "end_ts": b["end_ts"],
            "bid_open_usd_micro": u(b["bid_open"]), "bid_high_usd_micro": u(b["bid_high"]),
            "bid_low_usd_micro": u(b["bid_low"]), "bid_close_usd_micro": u(b["bid_close"]),
            "ask_open_usd_micro": u(b["ask_open"]), "ask_high_usd_micro": u(b["ask_high"]),
            "ask_low_usd_micro": u(b["ask_low"]), "ask_close_usd_micro": u(b["ask_close"]),
            "trade_open_usd_micro": u(b["open"]), "trade_high_usd_micro": u(b["high"]),
            "trade_low_usd_micro": u(b["low"]), "trade_close_usd_micro": u(b["close"]),
            "trade_mean_usd_micro": None,
            "volume_cc": int((b["volume"] or 0) * 100),
            "volume_notional_usd_micro": u(b["volume_usd"]),
            "oi_cc": int((b["oi"] or 0) * 100),
            "kalshi_env": "production",
        })
    with db.get_db() as conn:
        db.upsert_perp_candles(conn, rows)


def _recent_ts() -> int:
    # within the loader's since_days window
    return (int(time.time()) // 60) * 60 - 5 * 86400


def test_backtest_taker_entry_next_bar_and_tp(fresh_db):
    start = _recent_ts()
    bars = [_bar(start + i * 60, 6.35) for i in range(80)]
    # bar 65: +10bps 5m return triggers rules; entry should fill at bar 66 ASK
    for i in range(61, 70):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * (1 + 0.0012 * (i - 60)))
    # bar 70+: price jumps so bid_high clears TP
    for i in range(70, 80):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * 1.02)
    _seed_candles(bars)
    res = ps.backtest(BASE_CFG, since_days=14)
    assert res["n"] >= 1
    t = res["trades"][0]
    assert t["side"] == "long"
    assert t["won"] is True
    assert t["reason"] == "tp"
    assert res["windowsScanned"] > 0
    assert any("Fills are honest" in c for c in res["caveats"])


def test_backtest_sl_first_when_both_hit(fresh_db):
    start = _recent_ts()
    lvl = 6.35 * 1.001
    bars = [_bar(start + i * 60, 6.35) for i in range(80)]
    # single rules trigger at bar 65 (+10bps 5m return); entry fills at bar 66
    for i in range(65, 80):
        bars[i] = _bar(bars[i]["end_ts"], lvl)
    # bar 67: bid range hits BOTH the SL and the TP → SL must win (conservative)
    wild = _bar(bars[67]["end_ts"], lvl)
    wild["bid_low"] = lvl * (1 - 0.01)
    wild["bid_high"] = lvl * (1 + 0.01)
    bars[67] = wild
    _seed_candles(bars)
    res = ps.backtest(BASE_CFG, since_days=14)
    assert res["n"] >= 1
    assert res["trades"][0]["reason"] == "sl"
    assert res["trades"][0]["won"] is False


def test_backtest_fees_scale_with_era(fresh_db):
    start = _recent_ts()
    bars = [_bar(start + i * 60, 6.35) for i in range(90)]
    for i in range(61, 70):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * (1 + 0.0012 * (i - 60)))
    _seed_candles(bars)
    res_cheap = ps.backtest({**BASE_CFG, "perps_strat_fee_era": "jul8",
                             "perps_strat_tp_bps": 0, "perps_strat_sl_bps": 0,
                             "perps_strat_max_hold_min": 5}, since_days=14)
    res_dear = ps.backtest({**BASE_CFG, "perps_strat_fee_era": "today",
                            "perps_strat_tp_bps": 0, "perps_strat_sl_bps": 0,
                            "perps_strat_max_hold_min": 5}, since_days=14)
    assert res_cheap["n"] == res_dear["n"] >= 1
    assert res_dear["totalPnlUsd"] < res_cheap["totalPnlUsd"]  # 80bps vs 12bps taker


def test_backtest_maker_requires_trade_through(fresh_db):
    start = _recent_ts()
    bars = [_bar(start + i * 60, 6.35) for i in range(80)]
    # monotone ramp; every bar's trade low stays AT its close — the resting
    # bid one bar back is never traded through, so a maker entry never fills
    for i in range(61, 80):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * (1 + 0.0012 * (i - 60)))
        bars[i]["low"] = bars[i]["close"]
    _seed_candles(bars)
    res = ps.backtest({**BASE_CFG, "perps_strat_entry_style": "maker"}, since_days=14)
    assert res["n"] == 0  # touched is not filled


def test_backtest_funding_applied(fresh_db):
    start = _recent_ts()
    # place a funding stamp inside the hold window
    stamp_ts = start + 66 * 60
    stamp_dt = datetime.fromtimestamp(stamp_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_db() as conn:
        db.upsert_perp_funding(conn, [{
            "ticker": "KXBTCPERP", "funding_time": stamp_dt,
            "funding_rate": 0.01,  # absurdly large to dominate the P&L
            "mark_usd_micro": 6_350_000, "kalshi_env": "production",
        }])
    bars = [_bar(start + i * 60, 6.35) for i in range(90)]
    for i in range(61, 64):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * (1 + 0.002 * (i - 60)))
    _seed_candles(bars)
    cfg = {**BASE_CFG, "perps_strat_tp_bps": 0, "perps_strat_sl_bps": 0,
           "perps_strat_max_hold_min": 10}
    res = ps.backtest(cfg, since_days=14)
    assert res["n"] >= 1
    # long through a +1% funding stamp pays ≈ $0.0635 on one $6.35 contract
    assert res["trades"][0]["pnlUsd"] < -0.05


def test_backtest_liquidation_at_leverage(fresh_db):
    start = _recent_ts()
    bars = [_bar(start + i * 60, 6.35) for i in range(80)]
    for i in range(61, 66):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * (1 + 0.0015 * (i - 60)))
    crash_i = 68
    crash = _bar(bars[crash_i]["end_ts"], 6.35 * 0.75)  # −25% > 18% liq at 5x
    crash["bid_low"] = 6.35 * 0.70
    bars[crash_i] = crash
    for i in range(crash_i + 1, 80):
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * 0.75)
    _seed_candles(bars)
    res = ps.backtest({**BASE_CFG, "perps_strat_leverage": 5.0,
                       "perps_strat_sl_bps": 0, "perps_strat_tp_bps": 0,
                       "perps_strat_max_hold_min": 0}, since_days=14)
    assert res["n"] >= 1
    assert res["trades"][0]["reason"] == "liquidated"


def test_backtest_empty_rules_no_trades(fresh_db):
    _seed_candles(_bars(120, start_ts=_recent_ts()))
    res = ps.backtest({**BASE_CFG, "perps_strat_rules": []}, since_days=14)
    assert res["n"] == 0


# ───────── paper engine ──────────────────────────────────────────────────────

@pytest.fixture
def engine(monkeypatch, fresh_db):
    eng = ps._Engine()
    monkeypatch.setattr(ps, "_engine", eng)
    monkeypatch.setattr(kalshi_auth, "get_env", lambda: "production")
    # seed candles so the engine warm-starts past the 60-bar window
    start = (int(time.time()) // 60) * 60 - 100 * 60
    bars = [_bar(start + i * 60, 6.35) for i in range(95)]
    for i in range(89, 95):  # momentum into the present → rules fire
        bars[i] = _bar(bars[i]["end_ts"], 6.35 * (1 + 0.0015 * (i - 88)))
    _seed_candles(bars)
    return eng


def _live_quote(px=6.36, spread=0.002):
    return {
        "ts_ms": int(time.time() * 1000),
        "recv_ms": int(time.time() * 1000),
        "bid_usd_micro": int((px - spread / 2) * 1e6),
        "ask_usd_micro": int((px + spread / 2) * 1e6),
        "last_usd_micro": int(px * 1e6),
        "oi_cc": 10_000,
    }


def test_paper_entry_and_flatten(engine, monkeypatch):
    cfg = dict(BASE_CFG, perps_strat_enabled=True)
    monkeypatch.setattr(pws, "quote", lambda t: _live_quote())
    asyncio.run(engine.tick(cfg))
    with db.get_db() as conn:
        pos = db.get_open_perp_position(conn, "production")
    assert pos is not None
    assert pos["dry_run"] == 1
    assert pos["side"] == "long"
    st = engine.status(cfg)
    assert st["openPosition"]["contracts"] == 1
    # flatten closes it at the live bid with fees booked
    out = asyncio.run(engine.flatten(cfg))
    assert out["closed"] == 1
    with db.get_db() as conn:
        assert db.get_open_perp_position(conn, "production") is None
        rows = db.recent_perp_positions(conn, "production")
    assert rows[0]["exit_reason"] == "flatten"
    assert rows[0]["pnl_usd_micro"] is not None


def test_paper_no_entry_when_rules_fail(engine, monkeypatch):
    cfg = dict(BASE_CFG, perps_strat_enabled=True,
               perps_strat_rules=[{"field": "ret5mBps", "op": ">", "value": 10_000}])
    monkeypatch.setattr(pws, "quote", lambda t: _live_quote())
    asyncio.run(engine.tick(cfg))
    with db.get_db() as conn:
        assert db.get_open_perp_position(conn, "production") is None
    assert engine.last_reason  # surfaced why-not for the UI


def test_paper_daily_loss_halts(engine, monkeypatch):
    # book a big closed loss today, then tick — engine must halt, no entry
    with db.get_db() as conn:
        pid = db.open_perp_position(conn, {
            "ticker": "KXBTCPERP", "side": "long", "dry_run": True,
            "count_cc": 100, "entry_usd_micro": 6_350_000, "kalshi_env": "production",
        })
        db.close_perp_position(conn, pid, exit_usd_micro=6_000_000,
                               fees_usd_micro=0, funding_usd_micro=0,
                               pnl_usd_micro=-6_000_000, exit_reason="sl")
    cfg = dict(BASE_CFG, perps_strat_enabled=True)
    monkeypatch.setattr(pws, "quote", lambda t: _live_quote())
    asyncio.run(engine.tick(cfg))
    assert engine._halted()
    with db.get_db() as conn:
        assert db.get_open_perp_position(conn, "production") is None


def test_stale_quote_stands_down(engine, monkeypatch):
    cfg = dict(BASE_CFG, perps_strat_enabled=True)
    q = _live_quote()
    # Not received for well over the freshness window → treated as stream-dead.
    # (A merely old wire ts_ms with fresh receipt is a normal slow book, below.)
    q["recv_ms"] = int(time.time() * 1000) - 200_000
    monkeypatch.setattr(pws, "quote", lambda t: q)
    asyncio.run(engine.tick(cfg))
    assert "stale" in engine.last_reason
