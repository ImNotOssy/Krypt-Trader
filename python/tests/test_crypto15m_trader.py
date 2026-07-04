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

    async def _absent(coid, **kw):
        return None  # lookup succeeds, order confirmed absent
    monkeypatch.setattr(kalshi_api, "find_order_by_client_id", _absent)

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
            ("exit_kalshi_order_id", "exit_filled_contracts", "proceeds_usd",
             "avg_entry_cents", "fees_usd", "exit_fees_usd", "exit_limit_cents")
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
    # Settlement now requires a CONFIRMED final read of the exit order (an
    # unreadable order defers settlement to the next tick instead of booking
    # stale numbers). Confirm the same 3 sold contracts the row already has.
    async def _cancel(_kid):
        return {}
    async def _final(_kid):
        return {"order": {"status": "canceled", "fill_count_fp": "3",
                          "initial_count_fp": "10", "remaining_count_fp": "0",
                          "taker_fill_cost_dollars": "2.07",
                          "maker_fill_cost_dollars": "0"}}
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)
    monkeypatch.setattr(kalshi_api, "get_order", _final)

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
    # Real Kalshi cancel semantics: a canceled order reports remaining_count 0
    # (the unfilled remainder is GONE), not initial − filled. Code must judge
    # "fully sold" by fills vs contracts held, never by remaining alone.
    async def _get(_oid):
        return {"order": {
            "fill_count_fp": filled, "remaining_count_fp": 0,
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


# ───────── canceled-partial exits, stale-dict races, dead-order guard ──────


def test_canceled_partial_exit_stays_open_for_settlement(fresh_db, env_demo, cfg, monkeypatch):
    # A stop-loss SELL for 10 fills 4 and is then canceled (chase / market close /
    # manual). Kalshi zeroes remaining_count on cancel, so "remaining <= 0" must
    # NOT be read as "fully sold": 6 contracts are still held on Kalshi. The row
    # must stay 'exiting' so settlement books partial proceeds + residual payout.
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, exit_reason="stop_loss",
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=0)

    async def _get(_kid):  # canceled after a 4-lot partial @ 55c (complement 1.80)
        return _order(4, 0, 1.80, status="canceled")
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_exit(pos))
    assert out is None
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "exiting" and r["resolved"] == 0
    assert r["exit_filled_contracts"] == 4
    assert r["proceeds_usd"] == pytest.approx(2.20)  # 4 - 1.80 complement

    # Market then settles YES: partial proceeds + residual 6 × $1 − cost.
    async def _settled(_ticker):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled)
    out = run_async(ct._settle_if_closed(r))
    assert out["status"] == "settled"
    assert out["settlement_usd"] == pytest.approx(6.0)
    assert out["pnl_usd"] == pytest.approx(2.20 + 6.0 - 8.80)


def test_manage_position_does_not_cancel_partially_filled_exit(fresh_db, env_demo, cfg, monkeypatch):
    # _poll_exit writes a first partial fill to the DB and returns None. The
    # chase must then see the FRESH row (exit_filled_contracts=3), not the stale
    # in-memory dict (0) — chasing on the stale dict canceled the live
    # partially-filled stop-loss and never re-placed it.
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, exit_reason="stop_loss",
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=0,
                    exit_limit_cents=60, close_time=_future())

    async def _get(_kid):  # resting exit, 3 sold so far @ 55c
        return _order(3, 7, 1.35, status="resting")
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    canceled = []

    async def _cancel(kid):
        canceled.append(kid)
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)

    async def _book(_t):  # bid dropped below our 60c limit → chase WOULD trigger
        return {"yes": [[50, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)

    async def _open(_t):  # market still open → no settlement
        return {"yes_bid_dollars": 0.50, "yes_ask_dollars": 0.55, "status": "open", "result": ""}
    monkeypatch.setattr(kalshi_api, "fetch_market", _open)
    calls = _capture_orders(monkeypatch)

    run_async(ct._manage_position(pos, cfg, "demo"))

    assert canceled == []       # partially-filled exit left alone
    assert calls == []          # nothing re-placed
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "exiting" and r["exit_filled_contracts"] == 3


def test_settle_if_closed_books_same_tick_partial_fill(fresh_db, env_demo, cfg, monkeypatch):
    # Exit partially fills on the same tick the market settles: the settle path
    # re-reads the canceled order's FINAL fills, so already-sold contracts are
    # booked at their sale price, not at the settlement payout. With stale zeros
    # this booked a −$0.15 trade as +$1.20.
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, exit_reason="stop_loss",
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=0)

    async def _settled(_ticker):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled)

    async def _cancel(_kid):
        return {"ok": True}
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)

    async def _get(_kid):  # final read: 3 sold @ 55c before the cancel took
        return _order(3, 0, 1.35, status="canceled")
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._settle_if_closed(pos))
    assert out["status"] == "settled"
    assert out["exit_filled_contracts"] == 3
    assert out["settlement_usd"] == pytest.approx(7.0)                 # residual 7 × $1
    assert out["pnl_usd"] == pytest.approx(1.65 + 7.0 - 8.80)          # = −0.15, not +1.20


def test_chase_exit_aborts_when_order_state_unknown(fresh_db, env_prod, cfg, monkeypatch):
    # Cancel times out AND the re-read fails: the old sell may still be live, so
    # placing a second full-size sell could OVERSELL into an opposite position.
    # The chase must skip this tick and retry.
    async def _book(_t):
        return {"yes": [[50, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)

    async def _boom(_kid):
        raise RuntimeError("gateway timeout")
    monkeypatch.setattr(kalshi_api, "cancel_order", _boom)
    monkeypatch.setattr(kalshi_api, "get_order", _boom)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._chase_exit(_exiting_position(60), cfg))

    assert row is None
    assert calls == []


def test_chase_exit_aborts_when_cancel_did_not_take(fresh_db, env_prod, cfg, monkeypatch):
    # The re-read shows the old sell STILL RESTING (cancel 5xx'd but the order
    # survived): re-placing would leave two live full-size sells.
    async def _book(_t):
        return {"yes": [[50, 100]], "no": []}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)

    async def _cancel(_kid):
        raise RuntimeError("http 502")
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)

    async def _get(_kid):
        return _order(0, 10, 0, status="resting")
    monkeypatch.setattr(kalshi_api, "get_order", _get)
    calls = _capture_orders(monkeypatch)

    row = run_async(ct._chase_exit(_exiting_position(60), cfg))

    assert row is None
    assert calls == []


# ───────── fees are booked into P&L ─────────────────────────────────────────


def _order_with_fees(filled, remaining, cost_dollars, fees_dollars, status="resting") -> dict:
    o = _order(filled, remaining, cost_dollars, status=status)
    o["order"]["taker_fees_dollars"] = f"{fees_dollars}"
    return o


def test_parse_kalshi_order_extracts_fees():
    parsed = trader._parse_kalshi_order({
        "fill_count_fp": "10", "remaining_count_fp": "0",
        "taker_fill_cost_dollars": "5.00", "maker_fill_cost_dollars": "0",
        "taker_fees_dollars": "0.18", "status": "executed",
    })
    assert parsed["fees_usd"] == pytest.approx(0.18)
    parsed = trader._parse_kalshi_order({
        "taker_fill_count": 10, "taker_fill_cost": 500,
        "taker_fees": 18, "status": "executed",
    })
    assert parsed["fees_usd"] == pytest.approx(0.18)


def test_entry_poll_records_fees(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="submitted", kalshi_order_id="OID-E",
                    target_contracts=10, filled_contracts=0, close_time=_future())

    async def _get(_kid):
        return _order_with_fees(10, 0, 8.80, 0.12, status="executed")
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_entry(pos, cfg))
    assert out["status"] == "filled"
    assert out["fees_usd"] == pytest.approx(0.12)


def test_exit_pnl_is_net_of_entry_and_exit_fees(fresh_db, env_demo, cfg, monkeypatch):
    # 10 @ 88c entry ($8.80 + $0.12 fee); full exit at 85c → proceeds $8.50,
    # exit fee $0.09. Displayed P&L must be the NET cash change:
    # 8.50 − 8.80 − 0.12 − 0.09 = −0.51 (gross-of-fee math said −0.30).
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, fees_usd=0.12,
                    exit_kalshi_order_id="OID-X", exit_filled_contracts=0)

    async def _get(_kid):  # sold 10 @ 85c → complement fill_cost $1.50
        return _order_with_fees(10, 0, 1.50, 0.09, status="executed")
    monkeypatch.setattr(kalshi_api, "get_order", _get)

    out = run_async(ct._poll_exit(pos))
    assert out["status"] == "exited"
    assert out["proceeds_usd"] == pytest.approx(8.50)
    assert out["exit_fees_usd"] == pytest.approx(0.09)
    assert out["pnl_usd"] == pytest.approx(-0.51)


def test_settlement_pnl_subtracts_entry_fees(fresh_db, env_demo, cfg, monkeypatch):
    pos = _seed_c15(status="filled", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, fees_usd=0.12)

    async def _settled(_t):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled)

    out = run_async(ct._manage_position(pos, cfg, "demo"))
    assert out["status"] == "settled"
    assert out["pnl_usd"] == pytest.approx(10.0 - 8.80 - 0.12)


# ───────── unacked entry order recovery ─────────────────────────────────────


def test_entry_recovers_unacked_order_by_client_id(fresh_db, env_prod, cfg, monkeypatch):
    # The order POST raises (timeout / duplicate-coid on retry) but the order IS
    # live on Kalshi. Booking it 'error' would leave real contracts trading with
    # no stop-loss and no settlement booking — recover it via client_order_id.
    _live_cfg(cfg)

    async def _boom(**kw):
        raise RuntimeError("read timeout")
    monkeypatch.setattr(kalshi_api, "place_limit_order", _boom)

    async def _find(coid, ticker=""):
        return {"order_id": "REC-1", "client_order_id": coid, "status": "resting"}
    monkeypatch.setattr(kalshi_api, "find_order_by_client_id", _find)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))

    run_async(ct.run_tick(cfg, authed=True))

    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions").fetchone())
    assert r["status"] == "submitted"
    assert r["resolved"] == 0
    assert r["kalshi_order_id"] == "REC-1"


# ───────── strict entry threshold (hard floor on the price paid) ────────────


def test_strict_threshold_blocks_cheap_executable_price(fresh_db, env_prod, cfg, monkeypatch):
    # Mid says 86c favorite (passes an 85c threshold) but the executable buy
    # price is only 71c — a thin book's phantom favorite. Strict mode must skip.
    _live_cfg(cfg)
    cfg["crypto15m_entry_threshold"] = 0.85
    a = signal_asset(entry_cost=0.71)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([a]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []

    cfg["crypto15m_strict_threshold"] = False   # legacy behaviour still available
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1
    assert calls[0]["price_cents"] == 73        # 71c ask + 2c markup


def test_strict_threshold_floors_the_maker_bid(fresh_db, env_prod, cfg, monkeypatch):
    # Maker entries rest at the bid; strict mode floors that bid AT the
    # threshold so no fill can land below the user's number.
    _live_cfg(cfg)
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_entry_threshold"] = 0.86
    a = signal_asset()          # yesBid 85c, ask/entryCost 86c–87c
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([a]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["price_cents"] == 86        # floored up from the 85c bid


def test_strict_threshold_leaves_rules_and_contrarian_alone(fresh_db, env_prod, cfg, monkeypatch):
    # Contrarian buys the CHEAP side by design — the floor must not apply.
    _live_cfg(cfg)
    cfg["crypto15m_direction_mode"] = "contrarian"
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(favorite="up")]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1
    assert calls[0]["price_cents"] == 16


def _snapshot_market(**over):
    m = {
        "ticker": "KXBTC15M-T1",
        "close_time": _future(),
        "yes_bid_dollars": 0.02, "yes_ask_dollars": 0.26,
        "no_ask_dollars": 0.72, "last_price_dollars": 0.15,
    }
    m.update(over)
    return m


def _run_asset_snapshot(cfg, monkeypatch, market):
    async def _markets(**kw):
        return [market], ""
    monkeypatch.setattr(kalshi_api, "fetch_markets", _markets)
    cfg["crypto15m_indicator_detect"] = False
    entry = {"asset": "BTC", "series": "KXBTC15M", "cg": "bitcoin"}
    now = datetime.now(timezone.utc).timestamp()
    return run_async(crypto15m._asset_snapshot(entry, None, cfg, now))


def test_snapshot_strict_requires_executable_price_at_threshold(cfg, monkeypatch):
    # The FAQ's "DOGE no @ 71c" shape: mid calls DOWN an 85c favorite while the
    # real NO ask is 72c. Strict → no signal; legacy → signal.
    cfg["crypto15m_entry_threshold"] = 0.85
    out = _run_asset_snapshot(cfg, monkeypatch, _snapshot_market())
    assert out["favorite"] == "down"
    assert out["favoritePrice"] == pytest.approx(0.86)
    assert out["entryCost"] == pytest.approx(0.72)
    assert out["signal"] is False

    cfg["crypto15m_strict_threshold"] = False
    out = _run_asset_snapshot(cfg, monkeypatch, _snapshot_market())
    assert out["signal"] is True


def test_snapshot_strict_requires_two_sided_book(cfg, monkeypatch):
    # One-sided book: mid degrades to last_price — exactly the degenerate
    # snapshot that mints phantom favorites. Strict refuses to signal on it.
    cfg["crypto15m_entry_threshold"] = 0.70
    market = _snapshot_market(
        yes_bid_dollars=0, yes_ask_dollars=0, no_ask_dollars=0.90,
        last_price_dollars=0.10,
    )
    out = _run_asset_snapshot(cfg, monkeypatch, market)
    assert out["favorite"] == "down"
    assert out["signal"] is False

    cfg["crypto15m_strict_threshold"] = False
    out = _run_asset_snapshot(cfg, monkeypatch, market)
    assert out["signal"] is True


# ───────── main engine keeps its hands off the 15m series ───────────────────


def test_main_engine_should_trade_skips_crypto15m_series(cfg):
    cfg["trade_whales"] = True
    sig = {"ticker": "KXBTC15M-26JUL011500", "confidence": 90, "price": 0.80}
    ok, why = trader.should_trade(sig, "whale", cfg)
    assert ok is False and "crypto15m" in why
    # gambling mode must not bypass the exclusion either
    cfg["gambling_mode"] = True
    cfg["gambling_trade_probability"] = 1.0
    ok, _ = trader.should_trade(sig, "whale", cfg)
    assert ok is False


# ───────── aggregate 15m exposure cap + stop-out cooldown (Tier 2) ──────────


def test_aggregate_15m_cap_trims_order_to_budget(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    cfg["crypto15m_max_total_pct"] = 0.10
    cfg["crypto15m_order_size"] = 50
    # $8.80 already committed on another asset this window.
    _seed_c15(status="filled", asset="ETH", ticker="KXETH15M-T9",
              filled_contracts=10, cost_usd=8.80, kalshi_env="production")
    calls = _capture_orders(monkeypatch)

    run_async(ct._open_entry(signal_asset(), cfg, "production", 100.0))

    # bankroll ≈ 100 cash + 8.80 committed → 10% cap = 10.88 → budget 2.08
    # at the 88c limit → 2 contracts, not the requested 50.
    assert len(calls) == 1
    assert calls[0]["count"] == 2


def test_aggregate_15m_cap_blocks_when_exhausted(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    cfg["crypto15m_max_total_pct"] = 0.10
    _seed_c15(status="filled", asset="ETH", ticker="KXETH15M-T9",
              filled_contracts=15, cost_usd=12.0, kalshi_env="production")
    calls = _capture_orders(monkeypatch)

    out = run_async(ct._open_entry(signal_asset(), cfg, "production", 100.0))

    assert out is None and calls == []   # (100+12)·10% − 12 < one contract


def test_aggregate_15m_cap_off_without_balance(fresh_db, env_prod, cfg, monkeypatch):
    # No balance available (unauthed / cold cache) → the cap can't be computed;
    # sizing falls back to the per-bet limits rather than blocking everything.
    _live_cfg(cfg)
    cfg["crypto15m_max_total_pct"] = 0.10
    cfg["crypto15m_order_size"] = 3
    calls = _capture_orders(monkeypatch)
    run_async(ct._open_entry(signal_asset(), cfg, "production", 0.0))
    assert len(calls) == 1 and calls[0]["count"] == 3


def test_stopped_out_ticker_is_not_reentered_same_window(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(cfg)
    pos = _seed_c15(status="exited", ticker="KXBTC15M-T1", exit_reason="stop_loss",
                    filled_contracts=10, cost_usd=8.80, kalshi_env="production")
    with db.get_db() as conn:
        db.update_crypto15m_position(conn, pos["id"], resolved=1, pnl_usd=-2.0)

    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []   # same window (= same ticker) → no fee-churning re-entry
    # A NEW window (different ticker) is fair game again.
    monkeypatch.setattr(crypto15m, "snapshot",
                        _stub_snapshot([signal_asset(ticker="KXBTC15M-T2")]))
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1


# ───────── spot-vs-strike settlement model (Tier 5, detection-only) ─────────


def test_sigma1m_measures_return_vol():
    import indicators
    flat = [100.0] * 40
    assert indicators.sigma1m(flat) == 0.0 or indicators.sigma1m(flat) < 1e-12
    # Alternating ±1% moves → σ ≈ 1%.
    closes, px = [], 100.0
    for i in range(40):
        px *= 1.01 if i % 2 == 0 else 0.99
        closes.append(px)
    s = indicators.sigma1m(closes)
    assert s is not None and 0.008 <= s <= 0.012
    assert indicators.sigma1m([100.0] * 10) is None  # not enough history


def test_model_up_prob_shape():
    # Spot exactly at the strike → coin flip.
    p = crypto15m.model_up_prob(100.0, 100.0, 0.001, 5.0)
    assert p == pytest.approx(0.5, abs=1e-6)
    # Spot far above the strike late in the window → near-certain up.
    p = crypto15m.model_up_prob(101.0, 100.0, 0.001, 2.0)
    assert p is not None and p > 0.99
    # Same distance but huge vol → much less certain.
    p_hi = crypto15m.model_up_prob(101.0, 100.0, 0.02, 2.0)
    assert p_hi is not None and 0.5 < p_hi < 0.9
    # Below the strike mirrors above.
    p_dn = crypto15m.model_up_prob(99.0, 100.0, 0.001, 2.0)
    assert p_dn is not None and p_dn < 0.01
    # Missing/degenerate inputs → None (treated like an unpopulated field).
    assert crypto15m.model_up_prob(None, 100.0, 0.001, 5.0) is None
    assert crypto15m.model_up_prob(100.0, None, 0.001, 5.0) is None
    assert crypto15m.model_up_prob(100.0, 100.0, 0.0, 5.0) is None
    assert crypto15m.model_up_prob(100.0, 100.0, None, 5.0) is None


def test_model_edge_net_cents_picks_best_side_after_fees():
    # Model says 97% up; up ask 90c → up edge = 97 − 90 − fee(90) ≈ +6.4c.
    e = crypto15m.model_edge_net_cents(0.97, 0.90, 0.12)
    assert e == pytest.approx(97 - 90 - 7 * 0.9 * 0.1, abs=0.05)
    # Model says 10% up; down side is the value: 90 − 85 − fee(85) ≈ +4.1c.
    e = crypto15m.model_edge_net_cents(0.10, 0.20, 0.85)
    assert e == pytest.approx(90 - 85 - 7 * 0.85 * 0.15, abs=0.05)
    # Fairly-priced market → negative edge (fees).
    e = crypto15m.model_edge_net_cents(0.50, 0.51, 0.51)
    assert e is not None and e < 0
    assert crypto15m.model_edge_net_cents(None, 0.5, 0.5) is None
    assert crypto15m.model_edge_net_cents(0.6, None, None) is None


def test_market_strike_prefers_floor_strike():
    assert crypto15m._market_strike({"floor_strike": 65432.1}) == pytest.approx(65432.1)
    assert crypto15m._market_strike({"floor_strike_dollars": "65000"}) == pytest.approx(65000.0)
    assert crypto15m._market_strike({"floor_strike": None, "cap_strike": 12.5}) == pytest.approx(12.5)
    assert crypto15m._market_strike({}) is None
    assert crypto15m._market_strike({"floor_strike": "garbage"}) is None


def test_snapshot_uses_strike_and_signed_delta(cfg, monkeypatch):
    # Market carries the real strike (100); spot is 0.5% ABOVE it. Favorite is
    # DOWN per the quotes, and min_delta_pct=0.3% is direction-aware: an
    # up-move must NOT confirm a down-favorite entry (the old abs() gate did).
    market = _snapshot_market(
        floor_strike=100.0,
        yes_bid_dollars=0.05, yes_ask_dollars=0.15, no_ask_dollars=0.90,
    )
    cfg["crypto15m_entry_threshold"] = 0.85
    cfg["crypto15m_strict_threshold"] = True
    cfg["crypto15m_min_delta_pct"] = 0.003

    async def _markets(**kw):
        return [market], ""
    monkeypatch.setattr(kalshi_api, "fetch_markets", _markets)
    cfg["crypto15m_indicator_detect"] = False
    entry = {"asset": "BTC", "series": "KXBTC15M", "cg": "bitcoin"}
    now = datetime.now(timezone.utc).timestamp()
    out = run_async(crypto15m._asset_snapshot(entry, 100.5, cfg, now))

    assert out["strikeUsd"] == pytest.approx(100.0)
    assert out["deltaSignedPct"] == pytest.approx(0.005)
    assert out["favorite"] == "down"
    assert out["signal"] is False           # up-move contradicts a down entry

    # Mirror: spot 0.5% BELOW the strike confirms the down favorite.
    out = run_async(crypto15m._asset_snapshot(entry, 99.5, cfg, now))
    assert out["deltaSignedPct"] == pytest.approx(-0.005)
    assert out["signal"] is True


# ───────── settlement sniper (model mode) ────────────────────────────────────


def _model_asset(mp=0.98, edge=3.5, up_ask=0.93, down_ask=0.09, mins_left=4.0):
    a = signal_asset()
    a.update({
        "modelProb": mp, "edgeNetCents": edge,
        "upAsk": up_ask, "downAsk": down_ask,
        "minsLeft": mins_left, "inWindow": True, "signal": False,
    })
    return a


def _sniper_cfg(cfg):
    cfg["crypto15m_direction_mode"] = "model"
    cfg["crypto15m_time_delay_min"] = 5.0
    cfg["crypto15m_entry_max"] = 0.97
    return cfg


def test_model_mode_config_validates():
    c = merge_with_defaults({"crypto15m_direction_mode": "model",
                             "crypto15m_model_min_prob": 2.0,
                             "crypto15m_model_min_edge_cents": -5})
    assert c["crypto15m_direction_mode"] == "model"
    assert c["crypto15m_model_min_prob"] == 1.0     # clamped
    assert c["crypto15m_model_min_edge_cents"] == 0.0
    assert merge_with_defaults({"crypto15m_direction_mode": "junk"})[
        "crypto15m_direction_mode"] == "favorite"


def test_bought_side_model_mode(cfg):
    _sniper_cfg(cfg)
    assert ct._bought_side(_model_asset(mp=0.98), cfg) == "up"
    assert ct._bought_side(_model_asset(mp=0.02), cfg) == "down"
    assert ct._bought_side(_model_asset(mp=None), cfg) is None


def test_sniper_gate_matrix(cfg):
    _sniper_cfg(cfg)
    ok, why = ct.should_enter(_model_asset(), cfg, has_open=False, open_count=0)
    assert ok is True

    # Certainty below the gate.
    ok, why = ct.should_enter(_model_asset(mp=0.93), cfg, has_open=False, open_count=0)
    assert ok is False and "model" in why
    # Down side works symmetrically: 0.03 → P(down)=0.97 ✓.
    ok, _ = ct.should_enter(_model_asset(mp=0.03, down_ask=0.93), cfg,
                            has_open=False, open_count=0)
    assert ok is True
    # Edge below the gate.
    ok, why = ct.should_enter(_model_asset(edge=1.0), cfg, has_open=False, open_count=0)
    assert ok is False and "edge" in why
    # Model missing (indicators/spot feed off) → block, never guess.
    ok, why = ct.should_enter(_model_asset(mp=None), cfg, has_open=False, open_count=0)
    assert ok is False and "unavailable" in why
    # Ask over the entry cap → nothing executable.
    ok, why = ct.should_enter(_model_asset(up_ask=0.99), cfg, has_open=False, open_count=0)
    assert ok is False and "ask" in why
    # Outside the entry window.
    a = _model_asset(); a["inWindow"] = False
    ok, why = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is False and "window" in why


def test_sniper_places_taker_order_on_model_side(fresh_db, env_prod, cfg, monkeypatch):
    _live_cfg(_sniper_cfg(cfg))
    cfg["crypto15m_entry_style"] = "maker"  # sniper must override to taker
    monkeypatch.setattr(crypto15m, "snapshot",
                        _stub_snapshot([_model_asset(mp=0.02, down_ask=0.91)]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["side"] == "no"            # the MODEL side, not the favorite
    assert calls[0]["action"] == "buy"
    assert calls[0]["price_cents"] == 93       # ask 91c + 2c entry markup... see note
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "production")[0]
    assert r["direction"] == "no"
    assert r["confidence"] == pytest.approx(98.0)


def test_sniper_ignores_strict_favorite_floor(fresh_db, env_prod, cfg, monkeypatch):
    # Strict threshold floors FAVORITE entries at entry_threshold — it must not
    # push a sniper limit up to the favorite gate.
    _live_cfg(_sniper_cfg(cfg))
    cfg["crypto15m_entry_threshold"] = 0.95
    cfg["crypto15m_strict_threshold"] = True
    monkeypatch.setattr(crypto15m, "snapshot",
                        _stub_snapshot([_model_asset(up_ask=0.90)]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["price_cents"] <= 92       # not floored up to 95


# ───────── final-minute sniper strikes ───────────────────────────────────────


def _fm_asset(mp=0.999, edge=3.0, prints=40, mins_left=0.5, up_ask=0.95, down_ask=0.06):
    a = _model_asset(mp=mp, edge=edge, up_ask=up_ask, down_ask=down_ask,
                     mins_left=mins_left)
    a["inWindow"] = False           # final minute is OUTSIDE the normal window
    a["settlePrints"] = prints
    return a


def test_final_minute_gate_matrix(cfg):
    _sniper_cfg(cfg)
    # All conditions met → enter.
    ok, why = ct.should_enter(_fm_asset(), cfg, has_open=False, open_count=0)
    assert ok is True, why
    # Not enough settlement prints locked yet.
    ok, why = ct.should_enter(_fm_asset(prints=20), cfg, has_open=False, open_count=0)
    assert ok is False and "prints" in why
    # Too close to the close for the order round-trip.
    ok, why = ct.should_enter(_fm_asset(prints=57), cfg, has_open=False, open_count=0)
    assert ok is False and "round-trip" in why
    # Certainty below the 3-sigma bar (0.997 passes the normal 0.97 gate but
    # NOT the final-minute one).
    ok, why = ct.should_enter(_fm_asset(mp=0.997), cfg, has_open=False, open_count=0)
    assert ok is False and "sigma" in why
    # Edge still required.
    ok, why = ct.should_enter(_fm_asset(edge=0.5), cfg, has_open=False, open_count=0)
    assert ok is False and "edge" in why
    # Feature toggled off → hard block returns.
    cfg["crypto15m_model_final_minute"] = False
    ok, why = ct.should_enter(_fm_asset(), cfg, has_open=False, open_count=0)
    assert ok is False and "disabled" in why


def test_final_minute_only_for_model_mode(cfg):
    # Favorite mode stays hard-blocked in the final minute: the snapshot's
    # in_window floor (1.0 <= mins_left) guarantees `signal` is False there,
    # so the favorite gate can never fire. Assert both halves of that chain.
    from datetime import datetime, timezone as _tz
    market = {
        "ticker": "KXBTC15M-FM", "yes_bid_dollars": 0.94, "yes_ask_dollars": 0.96,
        "no_ask_dollars": 0.07, "last_price_dollars": 0.95,
        "close_time": (datetime.now(_tz.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # in_window computation from the snapshot: 0.5 min left -> False.
    assert not (1.0 <= 0.5 <= 8.0)
    cfg["crypto15m_direction_mode"] = "favorite"
    a = _fm_asset()
    a["signal"] = False             # what the snapshot actually produces
    ok, _ = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is False


def test_final_minute_no_prints_means_no_trade(cfg):
    # cf feed cold + sampler cold → settlePrints 0 → never trade the final
    # minute on model diffusion alone.
    _sniper_cfg(cfg)
    ok, why = ct.should_enter(_fm_asset(prints=0), cfg, has_open=False, open_count=0)
    assert ok is False and "prints" in why


def test_sniper_model_prob_zero_buys_down_not_up(fresh_db, env_prod, cfg, monkeypatch):
    # Regression for trade #346: modelProb of EXACTLY 0.0 is falsy — the old
    # `or 0.5` fallback flipped the side to UP at 50/50 on the strongest DOWN
    # signal there is. It must buy DOWN.
    _live_cfg(_sniper_cfg(cfg))
    a = _model_asset(mp=0.0, edge=3.0, up_ask=0.02, down_ask=0.95)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([a]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["side"] == "no"
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "production")[0]
    assert r["direction"] == "no"
    assert r["confidence"] == pytest.approx(100.0)


# ───────── close-boundary hardening (real losses #402 / a18e5ca1) ────────────


def test_no_entry_within_10s_of_close(fresh_db, env_prod, cfg, monkeypatch):
    # Trade #402 went out at T-2s; its cancel raced a maker fill and 8
    # untracked contracts lost $6.56. Entries must be refused at ORDER time
    # when <10s remain, regardless of what the (possibly stale) snapshot says.
    from datetime import datetime, timedelta, timezone as _tz
    _live_cfg(_sniper_cfg(cfg))
    a = _fm_asset()  # passes every model/print gate
    close = datetime.now(_tz.utc) + timedelta(seconds=5)
    a["closeTime"] = close.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([a]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []


def test_expired_entry_never_booked_canceled_without_confirmed_read(
    fresh_db, env_prod, cfg, monkeypatch
):
    # If the post-cancel fill re-read fails every attempt, the row must stay
    # 'submitted' (retried next tick) — NOT be marked canceled, because the
    # order may in fact have filled (order a18e5ca1 did).
    _live_cfg(_sniper_cfg(cfg))
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "kalshi_env": "production", "asset": "BTC", "series": "KXBTC15M",
            "ticker": "KXBTC15M-TEST-RACE",
            "side": "down", "direction": "no", "strategy": "",
            "status": "submitted", "target_contracts": 8, "filled_contracts": 0,
            "entry_limit_cents": 83, "confidence": 100.0,
            "close_time": "2020-01-01T00:00:00Z",  # long past -> expired
            "kalshi_order_id": "kid-race-1", "client_order_id": "c-race-1",
        })
        pos = db.fetch_crypto15m_by_id(conn, pid)

    async def boom(*a, **kw):
        raise RuntimeError("kalshi 5xx")
    monkeypatch.setattr(ct.kalshi_api, "cancel_order", boom)
    monkeypatch.setattr(ct.kalshi_api, "get_order", boom)
    sleeps = []
    async def fast_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(ct.asyncio, "sleep", fast_sleep)

    run_async(ct._poll_entry(pos, cfg))

    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pid)
    assert r["status"] == "submitted"          # NOT canceled
    assert not r["resolved"]
    assert len(sleeps) == 2                     # retried the read

    # Now the read succeeds and reports the racy FILL -> booked as filled.
    async def ok_cancel(*a, **kw):
        return {}
    async def filled_order(*a, **kw):
        return {"order": {"status": "canceled", "fill_count_fp": "8",
                          "initial_count_fp": "8", "remaining_count_fp": "0",
                          "taker_fill_cost_dollars": "0",
                          "maker_fill_cost_dollars": "6.56",
                          "taker_fees_dollars": "0", "maker_fees_dollars": "0"}}
    monkeypatch.setattr(ct.kalshi_api, "cancel_order", ok_cancel)
    monkeypatch.setattr(ct.kalshi_api, "get_order", filled_order)
    run_async(ct._poll_entry(r, cfg))
    with db.get_db() as conn:
        r2 = db.fetch_crypto15m_by_id(conn, pid)
    assert r2["status"] == "filled"
    assert r2["filled_contracts"] == 8


def test_place_exit_recovers_lost_sell_response(fresh_db, env_prod, cfg, monkeypatch):
    # A timed-out-but-delivered stop-loss SELL must be ADOPTED, not error-noted
    # (re-firing a second sell would flip the account into the opposite side).
    pos = _seed_c15(status="filled", direction="yes", target_contracts=5,
                    filled_contracts=5, cost_usd=4.50, avg_entry_cents=90.0)

    async def _book(_t):
        return {"yes": [[80, 100]], "no": []}
    async def _boom(**kw):
        raise RuntimeError("read timeout")
    async def _found(coid, *, ticker="", **kw):
        return {"order_id": "RECOVERED-1", "client_order_id": coid}
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    monkeypatch.setattr(kalshi_api, "place_limit_order", _boom)
    monkeypatch.setattr(kalshi_api, "find_order_by_client_id", _found)

    out = run_async(ct._place_exit(pos, None, cfg, reason="stop_loss"))

    assert out["status"] == "exiting"
    assert out["exit_kalshi_order_id"] == "RECOVERED-1"


def test_place_exit_unconfirmed_parks_without_order_id(fresh_db, env_prod, cfg, monkeypatch):
    # Lookup also failing -> park as 'exiting' with no kid; _chase_exit must
    # NOT place a new sell against it, and _poll_exit resolves it later.
    pos = _seed_c15(status="filled", direction="yes", target_contracts=5,
                    filled_contracts=5, cost_usd=4.50, avg_entry_cents=90.0)

    async def _book(_t):
        return {"yes": [[80, 100]], "no": []}
    async def _boom(**kw):
        raise RuntimeError("network down")
    async def _boom2(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(kalshi_api, "get_orderbook", _book)
    monkeypatch.setattr(kalshi_api, "place_limit_order", _boom)
    monkeypatch.setattr(kalshi_api, "find_order_by_client_id", _boom2)

    out = run_async(ct._place_exit(pos, None, cfg, reason="stop_loss"))
    assert out["status"] == "exiting" and not out["exit_kalshi_order_id"]
    assert out["exit_client_order_id"]

    # chase must refuse to touch an unresolved lost order
    assert run_async(ct._chase_exit(out, cfg)) is None

    # once the lookup succeeds and confirms ABSENT, revert to 'filled' to retry
    async def _absent(coid, *, ticker="", **kw):
        return None
    monkeypatch.setattr(kalshi_api, "find_order_by_client_id", _absent)
    run_async(ct._poll_exit(out))
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, out["id"])
    assert r["status"] == "filled"


def test_disabled_engine_still_manages_open_positions(fresh_db, env_demo, cfg, monkeypatch):
    # Turning the 15m feature off must NOT abandon a live filled position —
    # settlement must still book when the market resolves.
    pos = _seed_c15(status="filled", direction="yes", target_contracts=5,
                    filled_contracts=5, cost_usd=4.50, avg_entry_cents=90.0)
    cfg["crypto15m_enabled"] = False

    async def _settled(_t):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled)

    out = run_async(ct.run_tick(cfg, authed=True))

    assert len(out) == 1 and out[0]["status"] == "settled"
    assert out[0]["pnl_usd"] == pytest.approx(5.0 - 4.50)


def test_rules_mode_respects_safety_rails(cfg):
    # Custom rules must not bypass the final-minute block or the entry cap.
    cfg["crypto15m_use_rules"] = True
    cfg["crypto15m_rules"] = [{"field": "minsLeft", "op": "<=", "value": 15}]
    cfg["crypto15m_entry_max"] = 0.97

    a = signal_asset()
    a.update({"minsLeft": 0.5, "signal": False})
    ok, why = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is False and "final minute" in why

    a = signal_asset()
    a.update({"minsLeft": 5.0, "upAsk": 0.99})
    ok, why = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is False and "entry cap" in why

    a = signal_asset()
    a.update({"minsLeft": 5.0, "upAsk": 0.90})
    ok, _ = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is True


def test_replay_parity_and_gate_integration(fresh_db, env_prod, cfg):
    # PARITY: every field the live gates read must exist in tick_to_asset's
    # output — a renamed column would otherwise silently produce "0 trades".
    import replay
    tick = {
        "ticker": "KXBTC15M-P", "asset": "BTC", "mins_left": 4.0,
        "yes_bid": 0.92, "yes_ask": 0.93, "up_prob": 0.93, "no_ask": None,
        "spot": 61000.0, "open_spot": 60900.0, "delta_pct": 0.16,
        "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.5, "macd_cross": 1,
        "rsi": 60.0, "strike": 60950.0, "delta_signed_pct": 0.08,
        "sigma1m": 0.001, "model_prob": 0.985, "edge_net_cents": 4.0,
        "settle_prints": 0, "observed_at": "2026-07-03 12:00:00",
    }
    a = replay.tick_to_asset(tick, cfg, "2026-07-03T12:04:00Z")
    for f in ("favorite", "favoritePrice", "minsLeft", "inWindow", "signal",
              "modelProb", "edgeNetCents", "settlePrints", "upAsk", "downAsk",
              "rsi", "macdHist", "hourUtc", "closeTime"):
        assert f in a, f
    assert a["downAsk"] == pytest.approx(0.08)  # complement of yes_bid

    # INTEGRATION: sniper config over a seeded winner tick -> 1 winning trade.
    _sniper_cfg(cfg)
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO crypto15m_signals (ticker, asset, series, favorite,
               favorite_price, entry_cost, resolved, up_won, close_time, kalshi_env)
               VALUES ('KXBTC15M-P','BTC','KXBTC15M','up',0.93,0.93,1,1,
                       '2026-07-03T12:04:00Z','production')""")
        db.insert_crypto15m_tick(conn, {**tick, "kalshi_env": "production"})
    import replay as rp
    out = rp.replay(cfg, env="production", since_days=3650)
    assert out["n"] == 1 and out["wins"] == 1
    assert out["trades"][0]["side"] == "up"
    assert out["netEvCentsPerContract"] > 0


def test_replay_buckets_and_main_engine(fresh_db, env_prod, cfg):
    # Buckets must attribute trades to the right hour/day, and the main-engine
    # replay must apply the live should_trade gates with follower economics.
    import replay
    trades = [
        {"asset": "BTC", "won": True, "pnlUsd": 0.30, "at": "2026-07-02 03:15:00"},
        {"asset": "BTC", "won": False, "pnlUsd": -4.50, "at": "2026-07-02 15:15:00"},
        {"asset": "ETH", "won": True, "pnlUsd": 0.40, "at": "2026-07-03 03:45:00"},
    ]
    b = replay._bucketize(trades)
    h3 = next(x for x in b["byHourUtc"] if x["hour"] == 3)
    assert h3["n"] == 2 and h3["wins"] == 2 and h3["pnlUsd"] == pytest.approx(0.70)
    h15 = next(x for x in b["byHourUtc"] if x["hour"] == 15)
    assert h15["pnlUsd"] == pytest.approx(-4.50)
    assert [d["day"] for d in b["byDay"]] == ["2026-07-02", "2026-07-03"]
    assert b["byDay"][0]["pnlUsd"] == pytest.approx(-4.20)

    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO whale_trades (ticker, event_ticker, title, category,
               taker_side, count_fp, price, dollar_value, market_volume,
               confidence, resolved, outcome_correct, created_at)
               VALUES ('KXT-1','KXT','t','sports','yes',100,0.75,3000,50000,
                       70,1,1,'2026-07-02 14:00:00')""")
    c = merge_with_defaults({"trade_whales": True, "min_confidence_whale": 0,
                             "min_edge_pts_whale": 0, "min_market_volume": 0,
                             "min_entry_price_cents": 1, "max_entry_price_cents": 99})
    out = replay.replay_main(c, since_days=3650)
    assert out["windowsScanned"] == 1
    if out["n"] == 1:  # gates beyond the ones relaxed may still filter
        t = out["trades"][0]
        assert t["won"] is True and t["costCents"] == pytest.approx(76.0)
        assert out["byHourUtc"][14]["n"] == 1


def test_calibration_autopause_gates_model_entries(cfg, monkeypatch):
    # Degraded calibration must block sniper entries with a clear reason;
    # recovery (hysteresis) must unblock them.
    _sniper_cfg(cfg)
    ct._CAL_CACHE.update({"at": ct.time.time(), "ok": False, "n": 40,
                          "rate": 0.90, "lb": 0.82})
    ok, why = ct.should_enter(_model_asset(), cfg, has_open=False, open_count=0)
    assert ok is False and "calibration" in why

    ct._CAL_CACHE.update({"ok": True, "rate": 0.99, "lb": 0.97})
    ok, _ = ct.should_enter(_model_asset(), cfg, has_open=False, open_count=0)
    assert ok is True

    # opting out of autopause bypasses the gate even when degraded
    ct._CAL_CACHE.update({"ok": False})
    cfg["crypto15m_model_autopause"] = False
    ok, _ = ct.should_enter(_model_asset(), cfg, has_open=False, open_count=0)
    assert ok is True
    ct._CAL_CACHE.update({"ok": True, "at": 0.0})  # reset for other tests


def test_wilson_lower_bound_sanity():
    assert ct._wilson_lb(40, 40) > 0.9         # perfect record, decent n
    assert ct._wilson_lb(30, 40) < 0.70        # 75% observed -> LB well below
    assert ct._wilson_lb(0, 0) == 0.0
    # more evidence tightens the bound upward at the same rate
    assert ct._wilson_lb(97, 100) > ct._wilson_lb(29, 30)


def test_perfect_record_never_pauses_and_can_resume():
    # Regression (live 2026-07-03): the old bars (pause LB 0.955 / resume
    # 0.965) sat ABOVE the ceiling a PERFECT record can reach inside the
    # 40-window cap — a flawless model's LB is n/(n+z²): 0.886 at 21/21,
    # 0.937 at 40/40 — so the sniper paused FOREVER at a 100% hit rate
    # ("hit only 100% over the last 21 windows"). A perfect record must
    # clear the pause bar at every reachable n, and clear the RESUME bar
    # by the time the rolling window fills.
    for n in range(ct._CAL_MIN_N, ct._CAL_WINDOW + 1):
        assert ct._wilson_lb(n, n) >= ct._CAL_PAUSE_LB, f"perfect {n}/{n} would pause"
    assert ct._wilson_lb(ct._CAL_WINDOW, ct._CAL_WINDOW) >= ct._CAL_RESUME_LB
    # …while a genuinely degraded record (90% observed at n=40, a real
    # money-loser at 93c entries) still pauses.
    assert ct._wilson_lb(36, 40) < ct._CAL_PAUSE_LB
