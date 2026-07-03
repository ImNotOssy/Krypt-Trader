from __future__ import annotations

import asyncio
from collections import deque
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
    dbfile = tmp_path / "c15p-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture
def env_prod(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "production")
    return "production"


@pytest.fixture
def cfg():
    c = merge_with_defaults({})
    c["kalshi_env"] = "production"
    c["crypto15m_enabled"] = True
    c["crypto15m_live"] = True
    c["crypto15m_pairs_enabled"] = True
    c["crypto15m_entry_threshold"] = 0.999  # keep the directional leg quiet
    return c


@pytest.fixture(autouse=True)
def clear_hist():
    ct._pair_ask_hist.clear()
    yield
    ct._pair_ask_hist.clear()


def run_async(coro):
    return asyncio.run(coro)


def _future(mins=10):
    return (datetime.now(timezone.utc) + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _asset(*, ticker="KXBTC15M-P1", up_ask=0.48, down_ask=0.55, mins_left=10.0):
    return {
        "asset": "BTC", "series": "KXBTC15M", "spotUsd": 100.0,
        "hasMarket": True, "ticker": ticker, "closeTime": _future(mins_left),
        "minsLeft": mins_left, "upProb": 0.5, "downProb": 0.5,
        "favorite": "up", "favoritePrice": 0.5, "entryCost": up_ask,
        "yesBid": up_ask - 0.02, "yesAsk": up_ask,
        "upAsk": up_ask, "downAsk": down_ask,
        "inWindow": True, "signal": False, "openMarketCount": 1, "error": None,
    }


def _seed_hist(ticker: str, yes: list[float], no: list[float]):
    ct._pair_ask_hist[ticker] = {
        "yes": deque(yes, maxlen=45), "no": deque(no, maxlen=45),
    }


def _stub_snapshot(monkeypatch, assets):
    async def _snap(_cfg):
        return {"assets": assets, "constants": {}, "fetchedAt": "",
                "spotOk": True, "spotSource": "stub"}
    monkeypatch.setattr(crypto15m, "snapshot", _snap)


def _capture_orders(monkeypatch):
    calls = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": f"ord-{len(calls)}", "status": "resting"}}

    monkeypatch.setattr(kalshi_api, "place_limit_order", _place)
    return calls


# ───────── leg gate logic ────────────────────────────────────────────────────


def _hist(cents: float, n: int = 24) -> deque:
    return deque([cents] * n, maxlen=45)


def _seesaw_hist(lo: float, hi: float, n: int = 24) -> deque:
    """A realistic other-side history: the seesaw touches `lo` on this side's
    peaks and `hi` on its dips."""
    vals = [hi if i % 2 == 0 else lo for i in range(n)]
    return deque(vals, maxlen=45)


def test_first_leg_requires_a_dip():
    # Ask at its median — not a cheap moment.
    ok, why = ct._pair_leg_ok(
        48.0, _hist(48.0), _seesaw_hist(46.0, 56.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
    )
    assert ok is False and "no dip" in why
    # 3c below the median → dip; other side's recent low (46c) fits under
    # 95 − 48 = 47c, so completion is plausible.
    ok, _ = ct._pair_leg_ok(
        48.0, _hist(51.0), _seesaw_hist(46.0, 56.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
    )
    assert ok is True


def test_first_leg_needs_time_and_plausible_complement():
    # Too late to start a pair (< 5 min).
    ok, why = ct._pair_leg_ok(
        48.0, _hist(51.0), _hist(55.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=4.0,
    )
    assert ok is False and "too late" in why
    # First leg above the absolute cap.
    ok, why = ct._pair_leg_ok(
        65.0, _hist(70.0), _hist(30.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
    )
    assert ok is False and "max" in why
    # Complement implausible: the other side's recent LOW is 55c, so even on
    # its next dip the pair would cost ~48+55 > 95.
    ok, why = ct._pair_leg_ok(
        48.0, _hist(51.0), _seesaw_hist(55.0, 62.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
    )
    assert ok is False and "complement" in why


def test_second_leg_gates_on_actual_first_leg_cost():
    # First leg cost 46c → second leg allowed up to 95−46 = 49c.
    ok, _ = ct._pair_leg_ok(
        48.0, _hist(52.0), _hist(46.0), other_cost_cents=46.0,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=6,
    )
    assert ok is True
    ok, why = ct._pair_leg_ok(
        50.0, _hist(54.0), _hist(46.0), other_cost_cents=46.0,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=6,
    )
    assert ok is False and "ceiling" in why
    # Second leg has no first-leg time floor — only the final-minute cutoff.
    ok, _ = ct._pair_leg_ok(
        48.0, _hist(52.0), _hist(46.0), other_cost_cents=46.0,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=1.5,
    )
    assert ok is True
    ok, why = ct._pair_leg_ok(
        48.0, _hist(52.0), _hist(46.0), other_cost_cents=46.0,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=0.5,
    )
    assert ok is False and "final minute" in why


def test_leg_needs_warm_history():
    ok, why = ct._pair_leg_ok(
        48.0, deque([51.0] * 3), _hist(55.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
    )
    assert ok is False and "warming" in why


def test_first_leg_needs_a_settled_window():
    # ~40s of ticks is a dip during price discovery, not a seesaw — the
    # 04:45:54 window-open knife-catches. First legs wait for ~80s of history;
    # second legs (risk-reducing) keep the short warm-up.
    ok, why = ct._pair_leg_ok(
        44.0, _hist(48.0, n=10), _seesaw_hist(46.0, 56.0, n=10),
        other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=13,
    )
    assert ok is False and "young" in why
    ok, _ = ct._pair_leg_ok(
        44.0, _hist(48.0, n=10), _hist(46.0, n=10), other_cost_cents=46.0,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=13,
    )
    assert ok is True


# ───────── engine integration ────────────────────────────────────────────────


def test_first_leg_places_marketable_taker_order(fresh_db, env_prod, cfg, monkeypatch):
    a = _asset(up_ask=0.44, down_ask=0.55)
    # yes dipped 4c below its median; no has seesawed down to 48c recently, so
    # completing the pair under the 95c ceiling is plausible.
    _seed_hist(a["ticker"], yes=[48.0] * 24, no=[48.0, 55.0] * 12)
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["side"] == "yes" and calls[0]["action"] == "buy"
    assert calls[0]["price_cents"] == 45  # ask 44c + 1c marketable cap
    assert calls[0]["count"] == 5
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "production")[0]
    assert r["strategy"] == "pair"
    assert r["direction"] == "yes"


def test_second_leg_matches_first_fill_and_completes_pair(fresh_db, env_prod, cfg, monkeypatch):
    a = _asset(up_ask=0.52, down_ask=0.47)
    # First leg (yes) already filled: 4 contracts @ 45c avg.
    with db.get_db() as conn:
        db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": a["ticker"],
            "side": "up", "direction": "yes", "target_contracts": 5,
            "filled_contracts": 4, "avg_entry_cents": 45.0, "cost_usd": 1.80,
            "entry_limit_cents": 45, "client_order_id": "p-yes",
            "kalshi_order_id": "Y1", "status": "filled",
            "close_time": a["closeTime"], "kalshi_env": "production",
            "strategy": "pair",
        })
    _seed_hist(a["ticker"], yes=[52.0] * 10, no=[50.0] * 10)  # no dips 3c
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["side"] == "no"
    assert calls[0]["count"] == 4          # matches the first leg's FILL, not clip
    assert calls[0]["price_cents"] == 48   # 47c + 1
    # blended: 45 + 48 = 93c ≤ 95c ceiling → locked


def test_second_leg_blocked_when_pair_would_breach_ceiling(fresh_db, env_prod, cfg, monkeypatch):
    a = _asset(up_ask=0.52, down_ask=0.52)
    with db.get_db() as conn:
        db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": a["ticker"],
            "side": "up", "direction": "yes", "target_contracts": 5,
            "filled_contracts": 5, "avg_entry_cents": 45.0, "cost_usd": 2.25,
            "entry_limit_cents": 45, "client_order_id": "p-yes",
            "kalshi_order_id": "Y1", "status": "filled",
            "close_time": a["closeTime"], "kalshi_env": "production",
            "strategy": "pair",
        })
    _seed_hist(a["ticker"], yes=[52.0] * 10, no=[55.0] * 10)  # no dips 3c…
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []  # …but 45 + 52 = 97c > 95c ceiling → wait for cheaper


def test_pairs_skip_assets_owned_by_directional(fresh_db, env_prod, cfg, monkeypatch):
    a = _asset(up_ask=0.44)
    with db.get_db() as conn:
        db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": a["ticker"],
            "side": "up", "direction": "yes", "target_contracts": 1,
            "filled_contracts": 1, "cost_usd": 0.90, "entry_limit_cents": 90,
            "client_order_id": "d-1", "kalshi_order_id": "D1",
            "status": "filled", "close_time": a["closeTime"],
            "kalshi_env": "production",  # strategy '' = directional
        })
    _seed_hist(a["ticker"], yes=[48.0] * 10, no=[55.0] * 10)
    _stub_snapshot(monkeypatch, [a])

    async def _open(_t):
        return {"yes_bid_dollars": 0.85, "yes_ask_dollars": 0.9,
                "status": "open", "result": ""}
    monkeypatch.setattr(kalshi_api, "fetch_market", _open)
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []


def test_pair_rows_do_not_consume_directional_concurrency(fresh_db, env_prod, cfg, monkeypatch):
    # Two open pair legs + max_concurrent=1: a directional signal on another
    # asset must still be allowed (pairs are market-neutral once matched).
    cfg["crypto15m_max_concurrent"] = 1
    cfg["crypto15m_entry_threshold"] = 0.70
    cfg["crypto15m_pairs_enabled"] = False  # isolate the directional path
    with db.get_db() as conn:
        for d, s in (("yes", "up"), ("no", "down")):
            db.insert_crypto15m_position(conn, {
                "asset": "ETH", "series": "KXETH15M", "ticker": "KXETH15M-P1",
                "side": s, "direction": d, "target_contracts": 5,
                "filled_contracts": 5, "cost_usd": 2.25, "entry_limit_cents": 45,
                "client_order_id": f"p-{d}", "kalshi_order_id": f"K-{d}",
                "status": "filled", "close_time": _future(9),
                "kalshi_env": "production", "strategy": "pair",
            })
    btc = _asset(ticker="KXBTC15M-D1", up_ask=0.87, mins_left=5.0)
    btc.update({"favorite": "up", "favoritePrice": 0.86, "entryCost": 0.87,
                "signal": True, "yesBid": 0.85, "yesAsk": 0.87})
    _stub_snapshot(monkeypatch, [btc])

    async def _open(_t):
        return {"yes_bid_dollars": 0.85, "yes_ask_dollars": 0.87,
                "status": "open", "result": ""}
    monkeypatch.setattr(kalshi_api, "fetch_market", _open)
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    directional = [c for c in calls if c["ticker"] == "KXBTC15M-D1"]
    assert len(directional) == 1


def test_pair_leg_has_no_stop_loss(fresh_db, env_prod, cfg, monkeypatch):
    # A filled pair leg deep underwater mid-window must NOT be sold — the
    # matched pair pays $1 at settlement; selling un-locks the margin.
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-P1",
            "side": "up", "direction": "yes", "target_contracts": 5,
            "filled_contracts": 5, "cost_usd": 2.25, "entry_limit_cents": 45,
            "client_order_id": "p-yes", "kalshi_order_id": "Y1",
            "status": "filled", "close_time": _future(8),
            "kalshi_env": "production", "strategy": "pair",
        })
        pos = db.fetch_crypto15m_by_id(conn, pid)

    fetched = {"n": 0}

    async def _crashed(_t):  # yes side collapsed to 5c — directional would stop out
        fetched["n"] += 1
        return {"yes_bid_dollars": 0.04, "yes_ask_dollars": 0.06,
                "status": "open", "result": ""}
    monkeypatch.setattr(kalshi_api, "fetch_market", _crashed)
    calls = _capture_orders(monkeypatch)

    out = run_async(ct._manage_position(pos, cfg, "production"))
    assert out is None
    assert calls == []          # no SELL placed
    assert fetched["n"] == 0    # market open → not even a settlement fetch yet


def test_pair_legs_settle_and_net_the_locked_profit(fresh_db, env_prod, cfg, monkeypatch):
    # 4 yes @ 45c + 4 no @ 48c = blended 93c → locked 7c × 4 = $0.28 gross.
    close = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    with db.get_db() as conn:
        for d, s, avg, cost in (("yes", "up", 45.0, 1.80), ("no", "down", 48.0, 1.92)):
            pid = db.insert_crypto15m_position(conn, {
                "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-P1",
                "side": s, "direction": d, "target_contracts": 4,
                "filled_contracts": 4, "avg_entry_cents": avg, "cost_usd": cost,
                "entry_limit_cents": int(avg), "client_order_id": f"p-{d}",
                "kalshi_order_id": f"K-{d}", "status": "filled",
                "close_time": close, "kalshi_env": "production", "strategy": "pair",
            })
            rows.append(db.fetch_crypto15m_by_id(conn, pid))

    async def _settled(_t):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(kalshi_api, "fetch_market", _settled)

    outs = [run_async(ct._manage_pair(r)) for r in rows]
    assert all(o and o["status"] == "settled" for o in outs)
    total = sum(o["pnl_usd"] for o in outs)
    # yes leg: 4×$1 − 1.80 = +2.20; no leg: 0 − 1.92 = −1.92 → net +0.28
    assert total == pytest.approx(0.28)


def test_pairs_respect_aggregate_budget(fresh_db, env_prod, cfg, monkeypatch):
    cfg["crypto15m_max_total_pct"] = 0.10
    cfg["crypto15m_pairs_clip"] = 50  # 50 × ~45c ≈ $22.50 > 10% of $100

    async def _bank(_cfg, _authed):
        return 100.0
    monkeypatch.setattr(ct, "_bankroll_usd", _bank)

    a = _asset(up_ask=0.44)
    _seed_hist(a["ticker"], yes=[48.0] * 24, no=[48.0, 50.0] * 12)
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []


def test_hist_prunes_dead_windows():
    ct._pair_ask_hist["KXBTC15M-OLD"] = {"yes": deque([50.0]), "no": deque([50.0])}
    ct._update_pair_hist({"BTC": _asset(ticker="KXBTC15M-NEW")})
    assert "KXBTC15M-OLD" not in ct._pair_ask_hist
    assert "KXBTC15M-NEW" in ct._pair_ask_hist
    assert list(ct._pair_ask_hist["KXBTC15M-NEW"]["yes"])[-1] == pytest.approx(48.0)


# ───────── first-leg band + unmatched cap + pairs-only mode (UI feedback) ────


def test_first_leg_band_blocks_knife_catches():
    # The real-world failure: UP "dips" to 33c in a trending window (strong
    # DOWN favorite). Below the 35c floor → blocked even though it's a dip
    # and the complement technically fits.
    ok, why = ct._pair_leg_ok(
        33.0, _hist(37.0), _seesaw_hist(55.0, 62.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
        first_leg_min_cents=35.0,
    )
    assert ok is False and "floor" in why
    # 20c BTC leg from the same session — also blocked.
    ok, why = ct._pair_leg_ok(
        20.0, _hist(24.0), _seesaw_hist(70.0, 78.0), other_cost_cents=None,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=10,
        first_leg_min_cents=35.0,
    )
    assert ok is False and "floor" in why
    # The floor applies to FIRST legs only: completing an existing pair at a
    # now-cheap price is risk-REDUCING and must stay allowed.
    ok, _ = ct._pair_leg_ok(
        33.0, _hist(37.0), _hist(50.0), other_cost_cents=50.0,
        ceiling_cents=95, dip_cents=2, first_leg_max_cents=60, mins_left=6,
        first_leg_min_cents=35.0,
    )
    assert ok is True


def test_unmatched_first_leg_cap(fresh_db, env_prod, cfg, monkeypatch):
    # Two windows already waiting on their complements → a third first leg is
    # refused (unmatched legs cluster in trends = one correlated reversal bet).
    with db.get_db() as conn:
        for i, (asset, series) in enumerate((("ETH", "KXETH15M"), ("SOL", "KXSOL15M"))):
            db.insert_crypto15m_position(conn, {
                "asset": asset, "series": series, "ticker": f"{series}-U{i}",
                "side": "up", "direction": "yes", "target_contracts": 5,
                "filled_contracts": 5, "avg_entry_cents": 45.0, "cost_usd": 2.25,
                "entry_limit_cents": 45, "client_order_id": f"u-{i}",
                "kalshi_order_id": f"KU-{i}", "status": "filled",
                "close_time": _future(9), "kalshi_env": "production",
                "strategy": "pair",
            })
    a = _asset(up_ask=0.44, down_ask=0.55)
    _seed_hist(a["ticker"], yes=[48.0] * 24, no=[48.0, 55.0] * 12)
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []

    # …but a SECOND leg on one of those waiting windows is always allowed.
    b = _asset(ticker="KXETH15M-U0", up_ask=0.60, down_ask=0.47)
    b["asset"] = "ETH"
    b["series"] = "KXETH15M"
    _seed_hist(b["ticker"], yes=[60.0] * 10, no=[50.0] * 10)
    _stub_snapshot(monkeypatch, [b])
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1 and calls[0]["side"] == "no" and calls[0]["count"] == 5


def test_directional_off_is_pairs_only_mode(fresh_db, env_prod, cfg, monkeypatch):
    # Direction = "Off — pairs only": a perfect favorite signal opens nothing,
    # while a pair dip still fires.
    cfg["crypto15m_directional_enabled"] = False
    cfg["crypto15m_entry_threshold"] = 0.70
    a = _asset(up_ask=0.44, down_ask=0.55)
    a.update({"favorite": "up", "favoritePrice": 0.86, "entryCost": 0.87,
              "signal": True})
    _seed_hist(a["ticker"], yes=[48.0] * 24, no=[48.0, 55.0] * 12)
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    with db.get_db() as conn:
        rows = db.get_open_crypto15m(conn, "production")
    assert all(r["strategy"] == "pair" for r in rows)


def test_pairs_respect_trading_hours(fresh_db, env_prod, cfg, monkeypatch):
    hour_now = datetime.now(timezone.utc).hour
    cfg["crypto15m_hours_start_utc"] = (hour_now + 2) % 24
    cfg["crypto15m_hours_end_utc"] = (hour_now + 3) % 24
    a = _asset(up_ask=0.44, down_ask=0.55)
    _seed_hist(a["ticker"], yes=[48.0] * 24, no=[48.0, 55.0] * 12)
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []


def test_first_leg_band_validation_swaps_inverted_bounds():
    from config import merge_with_defaults
    c = merge_with_defaults({
        "crypto15m_pairs_first_leg_min_cents": 70,
        "crypto15m_pairs_first_leg_max_cents": 40,
    })
    assert c["crypto15m_pairs_first_leg_min_cents"] == 40.0
    assert c["crypto15m_pairs_first_leg_max_cents"] == 70.0


# ───────── post-mortem fixes: unfilled-first-leg loophole, TTL, model gate ───


def test_second_leg_never_placed_against_unfilled_first_leg(fresh_db, env_prod, cfg, monkeypatch):
    # Real trade #290: first leg (down@48) resting UNFILLED, up dips → the old
    # code hedged the phantom inventory; the first leg then died and the
    # "hedge" was a naked 23c knife. No fills = no second leg.
    a = _asset(up_ask=0.29, down_ask=0.55)
    with db.get_db() as conn:
        db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": a["ticker"],
            "side": "down", "direction": "no", "target_contracts": 5,
            "filled_contracts": 0, "entry_limit_cents": 48,
            "client_order_id": "p-no", "kalshi_order_id": "N1",
            "status": "submitted", "close_time": a["closeTime"],
            "kalshi_env": "production", "strategy": "pair",
        })
    _seed_hist(a["ticker"], yes=[33.0] * 24, no=[50.0] * 24)  # up "dipped" 4c
    _stub_snapshot(monkeypatch, [a])

    async def _order(_kid):  # entry poll: still resting, zero fills
        return {"order": {"fill_count_fp": "0", "remaining_count_fp": "5",
                          "status": "resting"}}
    monkeypatch.setattr(kalshi_api, "get_order", _order)

    async def _cancel(_kid):
        return {"ok": True}
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []


def test_pair_entry_ttl_cancels_stale_marketable_order(fresh_db, env_prod, cfg, monkeypatch):
    # A marketable pair entry that hasn't filled in 30s means the book moved —
    # cancel it instead of letting it rest into the window as an adverse bid.
    a = _asset()
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": a["ticker"],
            "side": "down", "direction": "no", "target_contracts": 5,
            "filled_contracts": 0, "entry_limit_cents": 48,
            "client_order_id": "p-ttl", "kalshi_order_id": "N1",
            "status": "submitted", "close_time": a["closeTime"],
            "kalshi_env": "production", "strategy": "pair",
        })
        conn.execute(
            "UPDATE crypto15m_positions SET created_at=datetime('now','-60 seconds') WHERE id=?",
            (pid,))
        pos = db.fetch_crypto15m_by_id(conn, pid)

    canceled = []

    async def _cancel(kid):
        canceled.append(kid)
    monkeypatch.setattr(kalshi_api, "cancel_order", _cancel)

    async def _order(_kid):
        return {"order": {"fill_count_fp": "0", "remaining_count_fp": "5",
                          "status": "resting"}}
    monkeypatch.setattr(kalshi_api, "get_order", _order)

    out = run_async(ct._poll_entry(pos, cfg))
    assert canceled == ["N1"]
    assert out["status"] == "canceled" and out["resolved"] == 1

    # A fresh pair entry (age < TTL) is NOT canceled by the TTL.
    assert ct._pair_entry_stale({"strategy": "pair",
                                 "created_at": "2099-01-01 00:00:00"}) is False
    # Directional rows are never TTL'd (maker style rests by design).
    assert ct._pair_entry_stale({"strategy": "", "created_at": "2000-01-01 00:00:00"}) is False


def test_model_conviction_gate_blocks_run_over_first_legs(fresh_db, env_prod, cfg, monkeypatch):
    # Model says P(up)=0.85 (trending up): a DOWN "dip" is a trend leg making
    # new lows — first leg blocked. The UP side stays allowed.
    a = _asset(up_ask=0.44, down_ask=0.42)
    a["modelProb"] = 0.85
    _seed_hist(a["ticker"], yes=[48.0] * 24, no=[46.0, 56.0] * 12)
    _stub_snapshot(monkeypatch, [a])
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["side"] == "yes"   # only the model-approved side opened

    # Fail-open: no model → both sides eligible again (band still applies).
    ct._pair_ask_hist.clear()
    with db.get_db() as conn:
        conn.execute("DELETE FROM crypto15m_positions")
    b = _asset(ticker="KXBTC15M-P2", up_ask=0.44, down_ask=0.42)
    b["modelProb"] = None
    _seed_hist(b["ticker"], yes=[48.0] * 24, no=[46.0, 56.0] * 12)
    _stub_snapshot(monkeypatch, [b])
    calls.clear()
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) >= 1


def test_matched_pairs_do_not_double_count_in_account_value(fresh_db, env_prod):
    close = _future(9)
    with db.get_db() as conn:
        # Fully matched pair: 5 yes @44c + 5 no @49c — Kalshi already redeemed
        # $5 cash at pairing, so held value must be ZERO, not $4.65.
        for d, s, avg, cost in (("yes", "up", 44.0, 2.20), ("no", "down", 49.0, 2.45)):
            db.insert_crypto15m_position(conn, {
                "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-M1",
                "side": s, "direction": d, "target_contracts": 5,
                "filled_contracts": 5, "avg_entry_cents": avg, "cost_usd": cost,
                "entry_limit_cents": int(avg), "client_order_id": f"m-{d}",
                "kalshi_order_id": f"M-{d}", "status": "filled",
                "close_time": close, "kalshi_env": "production", "strategy": "pair",
            })
        # Half-matched: 5 yes @40c, only 2 no filled @50c → 2 pairs redeemed;
        # residual = 3 yes @40c ($1.20) + 0 no.
        for d, s, f, avg, cost in (("yes", "up", 5, 40.0, 2.00), ("no", "down", 2, 50.0, 1.00)):
            db.insert_crypto15m_position(conn, {
                "asset": "ETH", "series": "KXETH15M", "ticker": "KXETH15M-M2",
                "side": s, "direction": d, "target_contracts": 5,
                "filled_contracts": f, "avg_entry_cents": avg, "cost_usd": cost,
                "entry_limit_cents": int(avg), "client_order_id": f"h-{d}",
                "kalshi_order_id": f"H-{d}", "status": "filled",
                "close_time": close, "kalshi_env": "production", "strategy": "pair",
            })
        # Plain solo directional: unchanged at cost.
        db.insert_crypto15m_position(conn, {
            "asset": "SOL", "series": "KXSOL15M", "ticker": "KXSOL15M-M3",
            "side": "up", "direction": "yes", "target_contracts": 5,
            "filled_contracts": 5, "avg_entry_cents": 90.0, "cost_usd": 4.50,
            "entry_limit_cents": 90, "client_order_id": "solo",
            "kalshi_order_id": "S1", "status": "filled",
            "close_time": close, "kalshi_env": "production",
        })
        held = db.open_crypto15m_filled_cost_usd(conn, "production")
        committed = db.open_crypto15m_committed_usd(conn, "production")
    # matched pair 0 + residual 3×40c=1.20 + 2/5 of no cost 0 + solo 4.50
    assert held == pytest.approx(0.0 + 1.20 + 0.60 + 4.50 - 0.60)  # = 5.70
    assert committed == pytest.approx(5.70)
