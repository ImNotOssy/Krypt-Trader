from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import db
import kalshi_api
import kalshi_ws
import scanner


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "scan-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def run_async(coro):
    return asyncio.run(coro)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────── whales with missing taker_side are skipped, not flipped ──────────


WHALE_CFG = {
    "min_whale_usd": 2500,
    "min_entry_price_frac": 0.50,
    "max_trade_age_min": 15,
}


def _tape_trade(trade_id: str, taker_side, ticker: str = "KXTEST-A") -> dict:
    t = {
        "trade_id": trade_id,
        "ticker": ticker,
        "count_fp": 5000,
        "yes_price_dollars": 0.40,
        "no_price_dollars": 0.60,
        "created_time": _now_iso(),
    }
    if taker_side is not None:
        t["taker_side"] = taker_side
    return t


def _stub_network(monkeypatch, tape: list[dict]):
    monkeypatch.setattr(kalshi_ws, "recent_trades", lambda limit=1000: tape)

    async def _no_markets(_tickers):
        return {}

    monkeypatch.setattr(kalshi_api, "fetch_markets_map", _no_markets)

    async def _no_series(_st):
        return None

    monkeypatch.setattr(kalshi_api, "fetch_series", _no_series)


def test_scan_whales_skips_sideless_tape_rows(fresh_db, monkeypatch):
    # kalshi_ws stamps taker_side='' when the WS field is absent. The scorer
    # priced such rows as NO flow while the executor defaulted '' to a YES buy
    # (wrong side, wrong cost basis) and resolution booked them as guaranteed
    # losses. They must never become whale signals.
    _stub_network(monkeypatch, [
        _tape_trade("t-blank", ""),
        _tape_trade("t-missing", None),
    ])
    n, rows = run_async(scanner.scan_whales(WHALE_CFG))
    assert n == 0 and rows == []
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM whale_trades").fetchone()[0] == 0


def test_scan_whales_keeps_valid_no_side(fresh_db, monkeypatch):
    # Control: an explicit NO whale over the same tape still lands, priced at
    # the NO price.
    _stub_network(monkeypatch, [_tape_trade("t-no", "no")])
    n, rows = run_async(scanner.scan_whales(WHALE_CFG))
    assert n == 1
    assert rows[0]["taker_side"] == "no"
    assert rows[0]["price"] == pytest.approx(0.60)
    assert rows[0]["dollar_value"] == pytest.approx(3000.0)


# ───────── momentum baseline: previous scan, not two scans back ─────────────


MOM_CFG = {
    "contrarian_only": False,
    "allowed_momentum_signal_types": ["price_move"],
    "max_trade_age_min": 15,
}


def _market_row(yes_bid: float, volume_24h: float = 1000.0) -> dict:
    return {
        "ticker": "KXMOM-A", "event_ticker": "KXMOM", "series_ticker": "KXMOM",
        "title": "Momentum test market", "yes_sub_title": "",
        "category": "sports", "status": "open", "close_time": "",
        "volume": 5000, "volume_24h": volume_24h, "open_interest": 100,
        "yes_bid": yes_bid, "yes_ask": yes_bid + 0.02, "last_price": yes_bid,
        "result": "", "settlement_value": None,
    }


def test_momentum_baseline_is_previous_scan_not_two_back(fresh_db, monkeypatch):
    monkeypatch.setattr(kalshi_ws, "recent_trades", lambda limit=1000: [])
    with db.get_db() as conn:
        db.save_snapshot(conn, "KXMOM-A", _market_row(0.36))  # two scans back
        db.save_snapshot(conn, "KXMOM-A", _market_row(0.40))  # previous scan
        db.upsert_market(conn, _market_row(0.45))             # current price

    # 0.45 vs the PREVIOUS scan (0.40) is a 5c drift — below the 8c threshold,
    # so no alert. The old read-rn=2-before-save baseline compared against
    # 0.36 (TWO scans back, a 9c doubled-window move) and fired.
    n, alerts = run_async(scanner.scan_momentum(MOM_CFG))
    assert n == 0 and alerts == []

    # A genuine >=8c single-scan move still fires: 0.55 vs the 0.45 snapshot
    # the previous scan_momentum call saved.
    with db.get_db() as conn:
        db.upsert_market(conn, _market_row(0.55))
    n, alerts = run_async(scanner.scan_momentum(MOM_CFG))
    assert n == 1
    assert alerts[0]["signal_type"] == "price_move"
    assert alerts[0]["direction"] == "yes"
    assert alerts[0]["price_change"] == pytest.approx(10.0, abs=0.01)


# ───────── unmapped Kalshi categories land in toggleable buckets ────────────


def test_resolve_category_unmapped_falls_to_keyword_bucket(monkeypatch):
    # An unmapped raw series category must NOT pass through as a raw slug —
    # that made an untoggleable bucket any category restriction silently
    # deny-listed. It falls back to the keyword buckets instead.
    async def _series(_st):
        return {"category": "Never Heard Of It"}

    monkeypatch.setattr(kalshi_api, "fetch_series", _series)
    cat = run_async(scanner._resolve_category("KXFOO-BAR", "Will the Fed cut rates?"))
    assert cat == "economics"
    cat = run_async(scanner._resolve_category("KXFOO-BAR", "nothing recognizable"))
    assert cat == "world"


def test_resolve_category_exotics_is_first_class(monkeypatch):
    # 'Exotics' is a real, high-volume Kalshi series category and the Edge
    # Stack preset allow-lists 'exotics' — it must map explicitly (and case-
    # insensitively), never depend on the raw.lower() fallback.
    async def _series(_st):
        return {"category": "Exotics"}

    monkeypatch.setattr(kalshi_api, "fetch_series", _series)
    assert run_async(scanner._resolve_category("KXEXOTIC-X", "whatever")) == "exotics"

    async def _series_caps(_st):
        return {"category": "EXOTICS"}

    monkeypatch.setattr(kalshi_api, "fetch_series", _series_caps)
    assert run_async(scanner._resolve_category("KXEXOTIC-X", "whatever")) == "exotics"
