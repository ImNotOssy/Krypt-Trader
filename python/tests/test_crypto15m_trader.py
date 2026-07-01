from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import crypto15m
import crypto15m_trader as ct
import db
import kalshi_api
import trader
from config import merge_with_defaults




@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "c15-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture
def env_demo(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "demo")
    return "demo"


@pytest.fixture
def env_prod(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "production")
    return "production"


@pytest.fixture
def cfg():
    c = merge_with_defaults({})
    c["kalshi_env"] = "demo"
    c["crypto15m_enabled"] = True
    c["crypto15m_entry_style"] = "taker"
    return c


def _live_cfg(cfg):
    """Production + armed: the only mode that places 15m orders now that paper is gone."""
    cfg["kalshi_env"] = "production"
    cfg["crypto15m_live"] = True
    return cfg


def run_async(coro):
    return asyncio.run(coro)


def signal_asset(asset="BTC", favorite="up", entry_cost=0.86, signal=True, ticker="KXBTC15M-T1"):
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    up = 0.9 if favorite == "up" else 0.1
    return {
        "asset": asset, "series": f"KX{asset}15M", "spotUsd": 100.0,
        "open15mUsd": 100.0, "deltaUsd": 0.5, "hasMarket": True,
        "ticker": ticker, "closeTime": close, "minsLeft": 5.0,
        "upProb": up, "downProb": 1 - up,
        "favorite": favorite, "favoritePrice": 0.86, "entryCost": entry_cost,
        "yesBid": 0.85, "yesAsk": 0.87,
        "inWindow": True, "signal": signal, "openMarketCount": 1, "error": None,
    }


def _stub_snapshot(assets):
    async def _snap(_cfg):
        return {"assets": assets, "constants": {}, "fetchedAt": "",
                "spotOk": True, "spotSource": "stub"}
    return _snap




def test_direction_for_favorite():
    assert ct.direction_for_favorite("up") == "yes"
    assert ct.direction_for_favorite("down") == "no"


def test_entry_limit_cents_marks_up_and_clamps():
    assert ct.entry_limit_cents(0.86, 0.02) == 88
    assert ct.entry_limit_cents(0.98, 0.02) == 99


def test_side_prob_from_market_respects_direction():
    m = {"yes_bid_dollars": 0.29, "yes_ask_dollars": 0.31}
    assert ct.side_prob_from_market(m, "yes") == pytest.approx(0.30)
    assert ct.side_prob_from_market(m, "no") == pytest.approx(0.70)
    assert ct.side_prob_from_market(None, "yes") is None


def test_should_enter_gate_matrix(cfg):
    a = signal_asset()
    assert ct.should_enter(a, cfg, has_open=False, open_count=0) == (True, "ok")
    assert ct.should_enter(a, cfg, has_open=True, open_count=0)[0] is False
    assert ct.should_enter(a, cfg, has_open=False, open_count=7)[0] is False
    assert ct.should_enter(signal_asset(signal=False), cfg, has_open=False, open_count=0) == (False, "no signal")

    off = dict(cfg)
    off["crypto15m_enabled"] = False
    assert ct.should_enter(a, off, has_open=False, open_count=0) == (False, "disabled")


def test_should_stop_loss(cfg):
    pos = {"status": "filled", "filled_contracts": 1}
    assert ct.should_stop_loss(pos, 0.30, cfg) is True
    assert ct.should_stop_loss(pos, 0.55, cfg) is False
    assert ct.should_stop_loss(pos, None, cfg) is False
    assert ct.should_stop_loss({"status": "submitted", "filled_contracts": 1}, 0.1, cfg) is False


def test_should_stop_loss_pct(cfg):
    # Entry: 10 contracts for $8.00 -> 80c/contract cost basis. Keep side_prob
    # ABOVE the default cents stop (exit_threshold 0.40) so we isolate the % stop.
    pos = {"status": "filled", "filled_contracts": 10, "cost_usd": 8.00}

    cfg["crypto15m_stop_loss_pct"] = 0.0                    # off
    assert ct.should_stop_loss(pos, 0.45, cfg) is False     # down 43.75% but % stop OFF

    cfg["crypto15m_stop_loss_pct"] = 0.20                   # stop at -20% of entry
    assert ct.should_stop_loss(pos, 0.65, cfg) is False     # -18.75% -> not yet
    assert ct.should_stop_loss(pos, 0.63, cfg) is True      # -21.25% -> past the line
    assert ct.should_stop_loss(pos, 0.60, cfg) is True      # -25% -> stop

    # The cents/price stop still fires independently of the % stop.
    cfg["crypto15m_stop_loss_pct"] = 0.0
    assert ct.should_stop_loss(pos, 0.30, cfg) is True      # 30c < exit_threshold 40c

    # Guards.
    cfg["crypto15m_stop_loss_pct"] = 0.20
    assert ct.should_stop_loss(pos, None, cfg) is False
    assert ct.should_stop_loss(
        {"status": "filled", "filled_contracts": 0, "cost_usd": 8.0}, 0.60, cfg) is False


def test_stop_loss_pct_helper_and_clamp():
    assert ct.stop_loss_pct({"crypto15m_stop_loss_pct": 0.2}) == 0.2
    assert ct.stop_loss_pct({"crypto15m_stop_loss_pct": 5.0}) == 1.0     # clamp high
    assert ct.stop_loss_pct({"crypto15m_stop_loss_pct": -1}) == 0.0      # clamp low
    assert ct.stop_loss_pct({"crypto15m_stop_loss_pct": "abc"}) == 0.0   # garbage -> off
    assert ct.stop_loss_pct({}) == 0.0                                    # missing -> off
    # merge_with_defaults clamps the raw config value into 0..1 too.
    assert merge_with_defaults({"crypto15m_stop_loss_pct": 9})["crypto15m_stop_loss_pct"] == 1.0


def test_compute_entry_contracts_fixed(cfg):
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_order_size"] = 3
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=88, balance_usd=0, order_size=3) == 3
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=88, balance_usd=1000, order_size=3) == 3


def test_compute_entry_contracts_balance_pct(cfg):
    cfg["crypto15m_sizing_mode"] = "balance_pct"
    cfg["crypto15m_balance_pct"] = 0.10
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=50, balance_usd=100, order_size=1) == 20
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=50, balance_usd=0, order_size=4) == 4


def test_compute_entry_contracts_max_loss_cap(cfg):
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_max_loss_pct"] = 0.05
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=50, balance_usd=100, order_size=100) == 10
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=90, balance_usd=10, order_size=5) == 0


def test_balance_pct_entry_sizes_the_live_order(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    cfg["crypto15m_sizing_mode"] = "balance_pct"
    cfg["crypto15m_balance_pct"] = 0.10

    async def _bank(_cfg, _authed):
        return 100.0
    monkeypatch.setattr(ct, "_bankroll_usd", _bank)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["count"] == 11
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "production")[0]
    assert r["target_contracts"] == 11
    assert r["status"] == "submitted"




def test_disabled_does_nothing(fresh_db, env_demo, cfg, monkeypatch):
    cfg["crypto15m_enabled"] = False
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    out = run_async(ct.run_tick(cfg, authed=False))
    assert out == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "demo") == 0


def test_demo_opens_no_positions(fresh_db, env_demo, cfg, monkeypatch):
    # Demo can't trade 15m markets and paper simulation was removed, so a
    # signal on demo opens nothing — the executor only monitors.
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    out = run_async(ct.run_tick(cfg, authed=True))
    assert out == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "demo") == 0


def test_one_position_per_asset(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    _capture_orders(monkeypatch)

    async def _open(_ticker):
        return {"yes_bid_dollars": 0.85, "yes_ask_dollars": 0.87, "status": "open", "result": ""}
    monkeypatch.setattr(kalshi_api, "fetch_market", _open)

    run_async(ct.run_tick(cfg, authed=True))
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "production") == 1


def test_contrarian_mode_buys_the_underdog(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    cfg["crypto15m_direction_mode"] = "contrarian"
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(favorite="up")]))
    _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "production")[0]
    assert r["side"] == "down"
    assert r["direction"] == "no"
    assert r["entry_limit_cents"] == 16




def _capture_orders(monkeypatch):
    calls = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "ord-1", "status": "resting"}}

    monkeypatch.setattr(kalshi_api, "place_limit_order", _place)
    return calls


def test_live_gate_is_independent_of_main_bot(fresh_db, env_prod, cfg, monkeypatch):
    cfg["crypto15m_live"] = True
    cfg["enable_trading"] = False
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["action"] == "buy"
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "production")[0]
    assert r["status"] == "submitted"
    assert r["dry_run"] == 0
    assert r["kalshi_order_id"] == "ord-1"


def test_main_bot_live_does_not_arm_15m(fresh_db, env_demo, cfg, monkeypatch):
    cfg["crypto15m_live"] = False
    cfg["enable_trading"] = True
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "demo") == 0


def test_live_armed_without_auth_does_not_trade(fresh_db, env_demo, cfg, monkeypatch):
    cfg["crypto15m_live"] = True
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=False))

    assert calls == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "demo") == 0


def test_demo_never_trades_even_when_live_armed(fresh_db, env_demo, cfg, monkeypatch):
    async def _bal(_cfg, force=False):
        return 10_000, 0
    monkeypatch.setattr(trader, "refresh_balance", _bal)
    cfg["crypto15m_live"] = True
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "demo") == 0

    st = run_async(ct.status(cfg, authed=True))
    assert st["liveSupported"] is False
    assert st["live"] is False
    assert st["liveArmed"] is True


def test_status_reports_live_armed_and_authed(fresh_db, env_prod, cfg, monkeypatch):
    async def _bal(_cfg, force=False):
        return 10_000, 0
    monkeypatch.setattr(trader, "refresh_balance", _bal)

    cfg["crypto15m_live"] = True
    st = run_async(ct.status(cfg, authed=True))
    assert st["live"] is True
    assert st["liveArmed"] is True
    assert st["authed"] is True

    st = run_async(ct.status(cfg, authed=False))
    assert st["live"] is False
    assert st["liveArmed"] is True
    assert st["authed"] is False




def test_failed_entry_resolves_immediately_and_blocks_retry(fresh_db, env_prod, cfg, monkeypatch):
    cfg["crypto15m_live"] = True

    async def _paused(**kw):
        raise RuntimeError("HTTP 409: exchange is paused")
    monkeypatch.setattr(kalshi_api, "place_limit_order", _paused)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))

    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "production") == 0
        r = dict(conn.execute("SELECT * FROM crypto15m_positions").fetchone())
    assert r["status"] == "error"
    assert r["resolved"] == 1

    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 1

    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(ticker="KXBTC15M-T2")]))
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 2


def test_exiting_position_settles_when_sell_never_fills(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=1,
                    filled_contracts=1, cost_usd=0.88, exit_reason="stop_loss",
                    exit_kalshi_order_id="ord-x1", exit_filled_contracts=0,
                    close_time=_future())
    pid = pos["id"]

    canceled = []

    async def _order_unfilled(_kid):
        return {"order": {"fill_count_fp": "0", "remaining_count_fp": "1"}}

    async def _cancel(kid):
        canceled.append(kid)

    async def _settled_no(_ticker):
        return {"result": "no", "status": "finalized"}

    monkeypatch.setattr(kalshi_api, "get_order", _order_unfilled)
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled_no)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=False))
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "demo") == 0
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE id=?", (pid,)).fetchone())
    assert r["status"] == "settled"
    assert r["resolved"] == 1
    assert r["exit_reason"] == "stop_loss"
    assert r["outcome_correct"] == 0
    assert r["pnl_usd"] == pytest.approx(-0.88)
    assert canceled == ["ord-x1"]


def test_legacy_stuck_error_row_is_swept(fresh_db, env_demo, cfg, monkeypatch):
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BNB", "series": "KXBNB15M", "ticker": "KXBNB15M-OLD",
            "side": "up", "direction": "yes", "target_contracts": 12,
            "entry_limit_cents": 90, "client_order_id": "c1",
            "close_time": "2026-06-10T15:15:00Z", "confidence": 90,
            "kalshi_env": "demo", "status": "error", "dry_run": False,
        })
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    run_async(ct.run_tick(cfg, authed=False))
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE id=?", (pid,)).fetchone())
    assert r["resolved"] == 1
    assert r["status"] == "error"




def test_maker_limit_cents_joins_the_bid():
    assert ct.maker_limit_cents("up", 0.85, 0.87, 0.87) == 85
    assert ct.maker_limit_cents("down", 0.85, 0.87, 0.15) == 13
    assert ct.maker_limit_cents("up", None, None, 0.87) == 86
    assert ct.maker_limit_cents("up", None, None, 0.01) == 1


def test_live_maker_entry_places_resting_order_at_bid(fresh_db, env_prod, cfg, monkeypatch):
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["price_cents"] == 85
    assert calls[0]["action"] == "buy"



_c15_seq = 0


def _seed_c15(**over) -> dict:
    global _c15_seq
    _c15_seq += 1
    n = _c15_seq
    row = {
        "asset": over.get("asset", "BTC"),
        "series": over.get("series", "KXBTC15M"),
        "ticker": over.get("ticker", f"KXBTC15M-T{n}"),
        "side": over.get("side", "up"),
        "direction": over.get("direction", "yes"),
        "target_contracts": over.get("target_contracts", 10),
        "filled_contracts": over.get("filled_contracts", 0),
        "cost_usd": over.get("cost_usd", 0.0),
        "entry_limit_cents": over.get("entry_limit_cents", 88),
        "client_order_id": f"co-{n}",
        "kalshi_order_id": over.get("kalshi_order_id"),
        "status": over.get("status", "submitted"),
        "exit_reason": over.get("exit_reason"),
        "close_time": over.get("close_time", ""),
        "kalshi_env": over.get("kalshi_env", "demo"),
        "dry_run": over.get("dry_run", False),
    }
    post = {k: over[k] for k in
            ("exit_kalshi_order_id", "exit_filled_contracts", "proceeds_usd", "avg_entry_cents")
            if k in over}
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, row)
        if post:
            db.update_crypto15m_position(conn, pid, **post)
        return db.fetch_crypto15m_by_id(conn, pid)


def _order(filled, remaining, cost_dollars, status="resting") -> dict:
    return {"order": {
        "fill_count_fp": f"{filled}", "remaining_count_fp": f"{remaining}",
        "taker_fill_cost_dollars": f"{cost_dollars}", "maker_fill_cost_dollars": "0",
        "status": status,
    }}


def _future():
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past():
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_partial_entry_keeps_polling_then_completes(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="submitted", kalshi_order_id="OID-E",
                    target_contracts=10, filled_contracts=0, close_time=_future())

    async def _get3(_kid):
        return _order(3, 7, 2.64)
    monkeypatch.setattr(kalshi_api, "get_order", _get3)

    out = run_async(ct._poll_entry(pos, cfg))
    assert out is None
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "submitted" and r["resolved"] == 0
    assert r["filled_contracts"] == 3
    assert r["cost_usd"] == pytest.approx(2.64)

    async def _get10(_kid):
        return _order(10, 0, 8.80, status="executed")
    monkeypatch.setattr(kalshi_api, "get_order", _get10)
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    out = run_async(ct._poll_entry(r, cfg))
    assert out["status"] == "filled"
    assert out["filled_contracts"] == 10
    assert out["cost_usd"] == pytest.approx(8.80)


def test_partial_entry_then_expiry_keeps_filled_portion(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="submitted", kalshi_order_id="OID-E", target_contracts=10,
                    filled_contracts=3, cost_usd=2.64, close_time=_past())
    canceled = {"v": False}

    async def _cancel(_kid):
        canceled["v"] = True

    async def _get(_kid):
        return _order(3, 7, 2.64)
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_entry(pos, cfg))
    assert canceled["v"] is True
    assert out["status"] == "filled"
    assert out["filled_contracts"] == 3
    assert out["resolved"] == 0
    assert out["exit_reason"] is None


def test_unfilled_entry_then_expiry_still_cancels(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="submitted", kalshi_order_id="OID-E",
                    target_contracts=10, filled_contracts=0, close_time=_past())

    async def _cancel(_kid):
        pass

    async def _get(_kid):
        return _order(0, 10, 0)
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_entry(pos, cfg))
    assert out["status"] == "canceled"
    assert out["resolved"] == 1
    assert out["exit_reason"] == "unfilled_expired"


def test_partial_stop_loss_sell_stays_exiting(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80,
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=0)

    async def _get(_kid):
        return _order(3, 7, 0.93)
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_exit(pos))
    assert out is None
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "exiting" and r["resolved"] == 0
    assert r["exit_filled_contracts"] == 3
    # proceeds = cash received = sold(3) - offsetting fill_cost(0.93) = 2.07
    assert r["proceeds_usd"] == pytest.approx(2.07)


def test_stop_loss_exit_books_cash_not_offsetting_cost(fresh_db, env_demo, cfg, monkeypatch):
    # Regression: bought 1 @ 71c (cost $0.71); stop-loss SELLS into a 12c bid.
    # Kalshi reports a SELL's fill_cost as the OFFSETTING-leg cost basis
    # = 1*(100-12) = $0.88, NOT the $0.12 cash received. Proceeds must be booked as
    # the cash ($0.12) -> a real LOSS, not the +$0.17 "win" the old complement math
    # produced (the source of the fabricated all-wins 15m track record).
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=1,
                    filled_contracts=1, cost_usd=0.71,
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=0)

    async def _get(_kid):
        return _order(1, 0, 0.88)   # Kalshi sell fill_cost = complement of a 12c sale
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_exit(pos))
    assert out["status"] == "exited"
    assert out["proceeds_usd"] == pytest.approx(0.12)   # cash received, NOT 0.88
    assert out["pnl_usd"] == pytest.approx(-0.59)        # 0.12 - 0.71
    assert out["outcome_correct"] == 0                    # booked as a LOSS, not a win


def test_partial_stop_then_settlement_accounts_for_sold_portion(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, exit_reason="stop_loss",
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=3, proceeds_usd=0.93)

    async def _settled(_ticker):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled)

    out = run_async(ct._settle_if_closed(pos))
    assert out["status"] == "settled" and out["resolved"] == 1
    assert out["settlement_usd"] == pytest.approx(7.0)
    assert out["pnl_usd"] == pytest.approx(0.93 + 7.0 - 8.80)
    assert out["outcome_correct"] == 1




def test_hours_ok_default_is_always_on(cfg):
    for h in range(24):
        assert crypto15m.hours_ok(cfg, hour=h) is True


def test_hours_ok_simple_window(cfg):
    cfg["crypto15m_hours_start_utc"] = 6
    cfg["crypto15m_hours_end_utc"] = 12
    assert crypto15m.hours_ok(cfg, hour=6) is True
    assert crypto15m.hours_ok(cfg, hour=11) is True
    assert crypto15m.hours_ok(cfg, hour=12) is False
    assert crypto15m.hours_ok(cfg, hour=23) is False


def test_hours_ok_overnight_wrap(cfg):
    cfg["crypto15m_hours_start_utc"] = 22
    cfg["crypto15m_hours_end_utc"] = 6
    assert crypto15m.hours_ok(cfg, hour=23) is True
    assert crypto15m.hours_ok(cfg, hour=2) is True
    assert crypto15m.hours_ok(cfg, hour=6) is False
    assert crypto15m.hours_ok(cfg, hour=12) is False


# ───────── stop-loss exit chase (the "stop-loss didn't fill" fix) ──────────


def _exiting_position(cur_limit: int, *, oid: str = "OLD") -> dict:
    """A filled position that has placed a resting stop-loss SELL @ cur_limit."""
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-T1",
            "side": "yes", "direction": "yes", "target_contracts": 10,
            "filled_contracts": 10, "entry_limit_cents": 70, "avg_entry_cents": 70,
            "cost_usd": 6.0, "client_order_id": f"c-{oid}", "kalshi_order_id": "E1",
            "status": "filled", "close_time": "", "confidence": 0,
            "entry_delta_usd": 0, "kalshi_env": "production", "dry_run": 0, "error": None,
        })
        db.update_crypto15m_position(
            conn, pid, status="exiting", exit_reason="stop_loss",
            exit_kalshi_order_id=oid, exit_limit_cents=cur_limit, exit_filled_contracts=0,
        )
        return db.fetch_crypto15m_by_id(conn, pid)


def _stub_order(monkeypatch, *, filled: int, cost_dollars: float = 0.0):
    async def _get(_oid):
        return {"order": {
            "fill_count_fp": filled, "remaining_count_fp": 10 - filled,
            "initial_count_fp": 10, "taker_fill_cost_dollars": cost_dollars,
            "maker_fill_cost_dollars": 0, "status": "canceled",
        }}
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    async def _cancel(_oid):
        return {"ok": True}
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)


def test_stop_loss_chases_stale_resting_sell(fresh_db, env_prod, cfg, monkeypatch):
    # Bid has dropped to 50¢, below our 60¢ resting sell, which is unfilled.
    async def _book(_t):
        return {"yes": [[50, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    _stub_order(monkeypatch, filled=0)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._chase_exit(_exiting_position(60), cfg))

    # Re-priced down to the live bid so it actually fills, full size, as a SELL.
    assert len(calls) == 1
    assert calls[0]["action"] == "sell"
    assert calls[0]["price_cents"] == 50
    assert calls[0]["count"] == 10
    assert row["exit_limit_cents"] == 50


def test_chase_exit_never_double_sells_on_race(fresh_db, env_prod, cfg, monkeypatch):
    # Same stale book, but the cancelled order actually filled in the race.
    async def _book(_t):
        return {"yes": [[50, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    _stub_order(monkeypatch, filled=10, cost_dollars=5.0)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._chase_exit(_exiting_position(60), cfg))

    # No second sell placed; the position resolves from the race fill.
    assert calls == []
    assert row["status"] == "exited"
    assert row["resolved"]


def test_chase_exit_holds_when_still_marketable(fresh_db, env_prod, cfg, monkeypatch):
    # Bid (65¢) is still at/above our 60¢ sell → it's marketable, don't churn it.
    async def _book(_t):
        return {"yes": [[65, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._chase_exit(_exiting_position(60), cfg))

    assert calls == []
    assert row is None


def test_place_stop_loss_applies_slippage(fresh_db, env_prod, cfg, monkeypatch):
    cfg["crypto15m_stop_slippage_cents"] = 4
    async def _book(_t):
        return {"yes": [[55, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    calls = _capture_orders(monkeypatch)
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-T1",
            "side": "yes", "direction": "yes", "target_contracts": 10,
            "filled_contracts": 10, "entry_limit_cents": 70, "avg_entry_cents": 70,
            "cost_usd": 6.0, "client_order_id": "sl-c", "kalshi_order_id": "E1",
            "status": "filled", "close_time": "", "confidence": 0,
            "entry_delta_usd": 0, "kalshi_env": "production", "dry_run": 0, "error": None,
        })
        pos = db.fetch_crypto15m_by_id(conn, pid)

    row = run_async(ct._place_exit(pos, None, cfg, reason="stop_loss"))

    assert len(calls) == 1
    assert calls[0]["action"] == "sell"
    assert calls[0]["price_cents"] == 51  # 55 bid − 4 slippage
    assert row["status"] == "exiting"
    assert row["exit_limit_cents"] == 51
    assert row["exit_reason"] == "stop_loss"


def test_stop_slippage_prices_chase_through_the_bid(fresh_db, env_prod, cfg, monkeypatch):
    cfg["crypto15m_stop_slippage_cents"] = 3
    async def _book(_t):
        return {"yes": [[50, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    _stub_order(monkeypatch, filled=0)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._chase_exit(_exiting_position(60), cfg))

    assert len(calls) == 1
    assert calls[0]["price_cents"] == 47  # 50 bid − 3 slippage
    assert row["exit_limit_cents"] == 47


# ───────── per-bet take-profit ────────────────────────────────────────────


def test_should_take_profit(cfg):
    pos = {"status": "filled", "filled_contracts": 5}
    cfg["crypto15m_take_profit_cents"] = 0  # off
    assert ct.should_take_profit(pos, 0.99, cfg) is False
    cfg["crypto15m_take_profit_cents"] = 95
    assert ct.should_take_profit(pos, 0.96, cfg) is True   # 96¢ ≥ 95¢
    assert ct.should_take_profit(pos, 0.95, cfg) is True   # exactly at the line
    assert ct.should_take_profit(pos, 0.94, cfg) is False  # 94¢ < 95¢
    assert ct.should_take_profit(pos, None, cfg) is False
    assert ct.should_take_profit({"status": "submitted", "filled_contracts": 5}, 0.99, cfg) is False
    assert ct.should_take_profit({"status": "filled", "filled_contracts": 0}, 0.99, cfg) is False


def test_take_profit_sells_winner_at_the_bid_without_slippage(fresh_db, env_demo, cfg, monkeypatch):
    cfg["crypto15m_take_profit_cents"] = 95
    cfg["crypto15m_stop_slippage_cents"] = 4  # stop-loss slippage must NOT apply to a take-profit
    pos = _seed_c15(status="filled", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.00)

    async def _open(_t):  # unresolved + held side worth ~97¢ → take-profit fires
        return {"yes_bid_dollars": 0.96, "yes_ask_dollars": 0.98, "status": "open", "result": ""}
    monkeypatch.setattr(kalshi_api, "fetch_market", _open)

    async def _book(_t):
        return {"yes": [[96, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._manage_position(pos, cfg, "demo"))

    assert len(calls) == 1
    assert calls[0]["action"] == "sell"
    assert calls[0]["price_cents"] == 96       # sells AT the bid; stop slippage ignored
    assert row["status"] == "exiting"
    assert row["exit_reason"] == "take_profit"


# ───────── overall (session) take-profit ──────────────────────────────────


def _seed_resolved_pnl(pnl_usd: float, *, ago_sql: str = "now", env: str = "production",
                       ticker: str = "KXETH15M-DONE") -> None:
    """A resolved 15m position with a known realized P&L, settled `ago_sql`
    (a SQLite datetime modifier like 'now' or '-1 hour')."""
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "ETH", "series": "KXETH15M", "ticker": ticker,
            "side": "up", "direction": "yes", "target_contracts": 10,
            "filled_contracts": 10, "entry_limit_cents": 80, "avg_entry_cents": 80,
            "cost_usd": 8.0, "client_order_id": f"done-{ticker}", "kalshi_order_id": "D1",
            "status": "settled", "close_time": "", "confidence": 0,
            "entry_delta_usd": 0, "kalshi_env": env, "dry_run": 0, "error": None,
        })
        db.update_crypto15m_position(conn, pid, resolved=1, pnl_usd=pnl_usd, outcome_correct=1)
        conn.execute(
            f"UPDATE crypto15m_positions SET resolved_at=datetime('{ago_sql}') WHERE id=?",
            (pid,),
        )


def test_session_take_profit_halts_new_entries(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    cfg["crypto15m_session_take_profit_usd"] = 5.0
    _seed_resolved_pnl(6.0, ago_sql="now")  # +$6 realized this session ≥ $5 target

    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_async(ct.run_tick(cfg, authed=True, session_start=since))

    assert calls == []  # session take-profit reached → no NEW entries opened
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "production") == 0


def test_session_take_profit_ignores_pnl_before_session_start(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    cfg["crypto15m_session_take_profit_usd"] = 5.0
    _seed_resolved_pnl(6.0, ago_sql="-1 hour")  # the +$6 was realized BEFORE this session

    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_async(ct.run_tick(cfg, authed=True, session_start=since))

    assert len(calls) == 1  # pre-session profit doesn't count → entry still opens


def test_status_exposes_take_profit_fields(fresh_db, env_prod, cfg, monkeypatch):
    async def _bal(*_a, **_k):
        return (10000, 10000)
    monkeypatch.setattr(trader, "refresh_balance", _bal)
    cfg["crypto15m_take_profit_cents"] = 92
    cfg["crypto15m_session_take_profit_usd"] = 25.0

    st = run_async(ct.status(cfg, authed=True, session_start=None))

    assert st["takeProfitCents"] == 92
    assert st["sessionTakeProfitUsd"] == 25.0
    assert st["sessionPnlUsd"] == 0.0
    assert st["takeProfitHalted"] is False


# ───────── direction-aware RSI / MACD entry filters ───────────────────────


def test_momentum_filters_off_by_default_passes(cfg):
    a = signal_asset(favorite="up")
    a["rsi"], a["macdHist"] = 20.0, -5.0  # ugly momentum, but filters are off
    ok, _ = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is True


def test_momentum_filter_rsi_and_macd_are_direction_aware(cfg):
    cfg["crypto15m_min_rsi"] = 50
    cfg["crypto15m_min_macd_hist"] = 10

    up = signal_asset(favorite="up"); up["rsi"], up["macdHist"] = 60.0, 22.0
    assert ct.momentum_filters_ok(up, cfg)[0] is True            # bullish confirms an up-bet

    up_rsi = signal_asset(favorite="up"); up_rsi["rsi"], up_rsi["macdHist"] = 45.0, 22.0
    assert ct.momentum_filters_ok(up_rsi, cfg)[0] is False       # rsi 45 < 50

    up_macd = signal_asset(favorite="up"); up_macd["rsi"], up_macd["macdHist"] = 60.0, 5.0
    assert ct.momentum_filters_ok(up_macd, cfg)[0] is False      # macd 5 < 10

    dn = signal_asset(favorite="down"); dn["rsi"], dn["macdHist"] = 30.0, -22.0
    assert ct.momentum_filters_ok(dn, cfg)[0] is True            # mirror: bearish confirms a down-bet

    dn_macd = signal_asset(favorite="down"); dn_macd["rsi"], dn_macd["macdHist"] = 30.0, -5.0
    assert ct.momentum_filters_ok(dn_macd, cfg)[0] is False      # -5 not ≤ -10

    dn_rsi = signal_asset(favorite="down"); dn_rsi["rsi"], dn_rsi["macdHist"] = 60.0, -22.0
    assert ct.momentum_filters_ok(dn_rsi, cfg)[0] is False       # rsi 60 not ≤ 50


def test_momentum_filter_missing_indicator_rejects(cfg):
    cfg["crypto15m_min_rsi"] = 50
    a = signal_asset(favorite="up"); a["rsi"], a["macdHist"] = None, 22.0
    assert ct.momentum_filters_ok(a, cfg)[0] is False            # rsi not populated → reject


def test_momentum_filter_confirms_the_bought_side_in_contrarian(cfg):
    cfg["crypto15m_direction_mode"] = "contrarian"
    cfg["crypto15m_min_rsi"] = 50
    # favorite up → contrarian BUYS down → wants bearish confirmation
    buy_down_ok = signal_asset(favorite="up"); buy_down_ok["rsi"] = 30.0
    assert ct.momentum_filters_ok(buy_down_ok, cfg)[0] is True   # rsi 30 ≤ 50 confirms the down buy
    buy_down_bad = signal_asset(favorite="up"); buy_down_bad["rsi"] = 70.0
    assert ct.momentum_filters_ok(buy_down_bad, cfg)[0] is False


def test_momentum_filter_blocks_entry_through_should_enter(cfg):
    cfg["crypto15m_min_rsi"] = 55
    weak = signal_asset(favorite="up"); weak["rsi"], weak["macdHist"] = 40.0, 1.0
    ok, why = ct.should_enter(weak, cfg, has_open=False, open_count=0)
    assert ok is False and "rsi" in why
