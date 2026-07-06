"""Passive Kalshi perps market-data recorder — no orders, ever.

Streams (WS ticker+trade via perps_ws, follows the active env) and polls
(REST public, ALWAYS production — unauthenticated) into perp_ticks /
perp_trades / perp_candles / perp_funding. The data feeds the perps research
program (lead-lag, stale-quote, funding, sniper-filter — see the swarm
reports): every series is recorded from day one so no experiment ever waits
on a re-collection.

Mirrors crypto15m_record.py's contract: async record_tick(cfg) awaited from
the service main loop on a >=15s gate; each phase try/excepted so a failure
never propagates (the recorder must never kill the trading loop). All DB
writes happen here, batched one transaction per flush — the WS task only
buffers in memory; single event loop, no locks.

Phases (module-level last-run timers):
  1. flush WS buffers        every call
  2. REST markets snapshot   every perps_rest_poll_sec (baseline when WS down)
     + funding estimate poll every perps_funding_est_sec (the estimate series
       exists nowhere historically — we create it)
  3. finalized-funding topup every perps_funding_poll_min
  4. 1m candle topup         every perps_candle_topup_min (doubles as gap
       repair after app downtime — the recorder only runs while the app runs)

backfill(cfg): one-shot deep fetch (candles perps_backfill_days + FULL
funding history, all markets) — idempotent via the tables' UNIQUE upserts;
kicked fire-and-forget from the service loop on first enable."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import db
import kalshi_auth
import kalshi_perps_api as papi
import perps_ws as pws

logger = logging.getLogger("perps_record")

DEFAULT_SYMBOLS = ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP", "KXDOGEPERP"]
# = spot_ws coverage, so every perp series has a Coinbase/BRTI-proxy twin.

_last_rest = 0.0
_last_est = 0.0
_last_funding = 0.0
_last_candles = 0.0
_est_cache: dict[str, dict] = {}  # symbol -> latest funding estimate payload
_backfill_state: dict = {"running": False, "done": False, "progress": "", "error": None}


def _symbols(cfg: dict) -> list[str]:
    syms = cfg.get("perps_symbols") or DEFAULT_SYMBOLS
    out = []
    for s in syms:
        s = str(s or "").strip().upper()
        if s.startswith("KX") and "PERP" in s:
            out.append(s.rstrip("1"))  # stored config is always prod symbols
    return list(dict.fromkeys(out))[:16] or list(DEFAULT_SYMBOLS)


def ensure_ws(cfg: dict, env: str) -> None:
    """Config-reconcile the WS accelerator: started iff recording is on, the
    WS is enabled, and credentials exist for the active env (the margin WS
    handshake is always signed). The REST poll is the baseline either way."""
    want = (
        bool(cfg.get("perps_record_signals", False))
        and bool(cfg.get("perps_ws_enabled", True))
        and kalshi_auth.credentials_present(env)
    )
    if want:
        pws.set_symbols(papi.env_ticker(s, env) for s in _symbols(cfg))
        pws.set_env(env)
        if not pws.is_running():
            pws.start(env)
    elif pws.is_running():
        # Toggled off — stop streaming; buffered rows are flushed on the next
        # record_tick before the toggle gate stops calling us... which it
        # won't. Flush synchronously-ish: drain to DB right here.
        try:
            _flush_ws()
        except Exception:
            pass
        asyncio.get_event_loop().create_task(pws.stop())


def _flush_ws() -> dict:
    ticks = pws.drain_ticks()
    trades = pws.drain_trades()
    if not ticks and not trades:
        return {"ticks": 0, "trades": 0}
    with db.get_db() as conn:
        n_ticks = db.insert_perp_ticks(conn, ticks)
        n_trades = db.insert_perp_trades(conn, trades)
    return {"ticks": n_ticks, "trades": n_trades}


def _market_to_tick_row(m: dict, est: dict | None) -> dict:
    ref = m.get("reference_price") or {}
    smark = m.get("settlement_mark_price") or {}
    lmark = m.get("liquidation_mark_price") or {}
    row = {
        "ticker": m.get("ticker", ""),
        "ts_ms": None,  # REST snapshot rows have no single wire event time
        "last_usd_micro": papi.usd_micro(m.get("price")),
        "bid_usd_micro": papi.usd_micro(m.get("bid")),
        "ask_usd_micro": papi.usd_micro(m.get("ask")),
        "bid_size_cc": None,
        "ask_size_cc": None,
        "volume_24h_cc": papi.cc(m.get("volume_24h")),
        "oi_cc": papi.cc(m.get("open_interest")),
        "ref_usd_micro": papi.usd_micro(ref.get("price") if isinstance(ref, dict) else ref),
        "ref_ts_ms": ref.get("ts_ms") if isinstance(ref, dict) else None,
        "settle_mark_usd_micro": papi.usd_micro(
            smark.get("price") if isinstance(smark, dict) else smark),
        "liq_mark_usd_micro": papi.usd_micro(
            lmark.get("price") if isinstance(lmark, dict) else lmark),
        "funding_rate": None,
        "next_funding_ms": None,
        "src": "rest",
        "kalshi_env": "production",  # public REST always reads prod
    }
    if est:
        try:
            row["funding_rate"] = float(est.get("funding_rate"))
        except (TypeError, ValueError):
            pass
        nft = est.get("next_funding_time")
        if nft:
            try:
                dt = datetime.fromisoformat(str(nft).replace("Z", "+00:00"))
                row["next_funding_ms"] = int(dt.timestamp() * 1000)
            except ValueError:
                pass
    return row


async def _rest_snapshot(cfg: dict) -> int:
    symbols = set(_symbols(cfg))
    markets = await papi.fetch_perps_markets()
    rows = [
        _market_to_tick_row(m, _est_cache.get(m.get("ticker", "")))
        for m in markets
        if m.get("ticker") in symbols and m.get("status") == "active"
    ]
    if not rows:
        return 0
    with db.get_db() as conn:
        return db.insert_perp_ticks(conn, rows)


async def _poll_estimates(cfg: dict) -> int:
    n = 0
    for s in _symbols(cfg):
        est = await papi.fetch_funding_rate_estimate(s)
        if est:
            _est_cache[s] = est
            n += 1
    return n


def _funding_to_row(f: dict) -> dict | None:
    ticker = f.get("market_ticker") or f.get("ticker") or ""
    ft = f.get("funding_time") or ""
    if not ticker or not ft:
        return None
    try:
        rate = float(f.get("funding_rate"))
    except (TypeError, ValueError):
        return None
    return {
        "ticker": ticker,
        "funding_time": papi.rfc3339_to_sqlite(str(ft)),
        "funding_rate": rate,
        "mark_usd_micro": papi.usd_micro(f.get("mark_price")),
        "kalshi_env": "production",
    }


async def _funding_topup(cfg: dict) -> int:
    # One unauthenticated call covers ALL markets; overlap is fine (OR IGNORE).
    with db.get_db() as conn:
        last = db.perp_last_funding_time(conn)
    start_ts = None
    if last:
        try:
            dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            start_ts = int(dt.timestamp()) - 86400
        except ValueError:
            start_ts = None
    rates = await papi.fetch_funding_rates_historical(start_ts=start_ts)
    rows = [r for r in (_funding_to_row(f) for f in rates) if r]
    if not rows:
        return 0
    with db.get_db() as conn:
        return db.upsert_perp_funding(conn, rows)


def _candle_to_row(ticker: str, period_min: int, c: dict) -> dict | None:
    try:
        end_ts = int(c.get("end_period_ts") or 0)
    except (TypeError, ValueError):
        return None
    if not end_ts:
        return None
    bid = c.get("bid") or {}
    ask = c.get("ask") or {}
    px = c.get("price") or {}
    u = papi.usd_micro
    return {
        "ticker": ticker,
        "period_min": int(period_min),
        "end_ts": end_ts,
        "bid_open_usd_micro": u(bid.get("open")), "bid_high_usd_micro": u(bid.get("high")),
        "bid_low_usd_micro": u(bid.get("low")), "bid_close_usd_micro": u(bid.get("close")),
        "ask_open_usd_micro": u(ask.get("open")), "ask_high_usd_micro": u(ask.get("high")),
        "ask_low_usd_micro": u(ask.get("low")), "ask_close_usd_micro": u(ask.get("close")),
        # trade OHLC/mean is NULL when no trades printed in the period
        "trade_open_usd_micro": u(px.get("open")), "trade_high_usd_micro": u(px.get("high")),
        "trade_low_usd_micro": u(px.get("low")), "trade_close_usd_micro": u(px.get("close")),
        "trade_mean_usd_micro": u(px.get("mean")),
        "volume_cc": papi.cc(c.get("volume")),
        "volume_notional_usd_micro": papi.usd_micro(c.get("volume_notional_value_dollars")),
        "oi_cc": papi.cc(c.get("open_interest")),
        "kalshi_env": "production",
    }


async def _candles_topup(cfg: dict) -> int:
    now = int(time.time())
    total = 0
    for s in _symbols(cfg):
        with db.get_db() as conn:
            last = db.perp_last_candle_end_ts(conn, s, 1)
        # Nothing stored yet → a short 6h seed window; backfill() owns the
        # deep history so a cold topup doesn't hammer the API.
        start = (last + 60) if last else (now - 6 * 3600)
        if start > now:
            continue
        candles = await papi.fetch_perps_candlesticks_range(s, start, now, 1)
        rows = [r for r in (_candle_to_row(s, 1, c) for c in candles) if r]
        if rows:
            with db.get_db() as conn:
                total += db.upsert_perp_candles(conn, rows)
    return total


async def record_tick(cfg: dict) -> dict:
    """Awaited from the service main loop every >=15s while the toggle is on.
    Never raises."""
    global _last_rest, _last_est, _last_funding, _last_candles
    now = time.monotonic()
    out = {"ticks": 0, "trades": 0, "restRows": 0, "funding": 0, "candles": 0}

    try:
        flushed = _flush_ws()
        out["ticks"], out["trades"] = flushed["ticks"], flushed["trades"]
    except Exception as e:
        logger.debug(f"perps ws flush failed: {e}")

    try:
        est_sec = max(30, int(cfg.get("perps_funding_est_sec", 60)))
        if now - _last_est >= est_sec:
            _last_est = now
            await _poll_estimates(cfg)
    except Exception as e:
        logger.debug(f"perps funding-estimate poll failed: {e}")

    try:
        poll_sec = max(10, int(cfg.get("perps_rest_poll_sec", 30)))
        if now - _last_rest >= poll_sec:
            _last_rest = now
            out["restRows"] = await _rest_snapshot(cfg)
    except Exception as e:
        logger.debug(f"perps REST snapshot failed: {e}")

    try:
        fund_min = max(15, int(cfg.get("perps_funding_poll_min", 60)))
        if now - _last_funding >= fund_min * 60:
            _last_funding = now
            out["funding"] = await _funding_topup(cfg)
    except Exception as e:
        logger.debug(f"perps funding topup failed: {e}")

    try:
        candle_min = max(5, int(cfg.get("perps_candle_topup_min", 10)))
        if now - _last_candles >= candle_min * 60:
            _last_candles = now
            out["candles"] = await _candles_topup(cfg)
    except Exception as e:
        logger.debug(f"perps candle topup failed: {e}")

    return out


def backfill_needed() -> bool:
    return not _backfill_state["done"] and not _backfill_state["running"]


async def backfill(cfg: dict) -> dict:
    """One-shot deep backfill, idempotent (UNIQUE upserts): perps_backfill_days
    of 1m candles per symbol + FULL funding history for all markets. Safe to
    re-run; guarded against concurrent runs."""
    if _backfill_state["running"]:
        return {"skipped": "already running"}
    _backfill_state.update(running=True, error=None, progress="starting")
    candles_n = 0
    funding_n = 0
    errors: list[str] = []
    try:
        days = max(1, min(90, int(cfg.get("perps_backfill_days", 14))))
        now = int(time.time())
        start = now - days * 86400
        for s in _symbols(cfg):
            _backfill_state["progress"] = f"candles {s}"
            try:
                candles = await papi.fetch_perps_candlesticks_range(s, start, now, 1)
                rows = [r for r in (_candle_to_row(s, 1, c) for c in candles) if r]
                if rows:
                    with db.get_db() as conn:
                        candles_n += db.upsert_perp_candles(conn, rows)
            except Exception as e:
                errors.append(f"candles {s}: {e}")
            await asyncio.sleep(0.3)

        _backfill_state["progress"] = "funding history"
        try:
            rates = await papi.fetch_funding_rates_historical()  # full history, all markets
            rows = [r for r in (_funding_to_row(f) for f in rates) if r]
            if rows:
                with db.get_db() as conn:
                    funding_n = db.upsert_perp_funding(conn, rows)
        except Exception as e:
            errors.append(f"funding: {e}")

        _backfill_state.update(done=True, progress="done")
    except Exception as e:  # belt-and-braces: state must never wedge "running"
        errors.append(str(e))
        _backfill_state["error"] = str(e)
    finally:
        _backfill_state["running"] = False
    if errors:
        _backfill_state["error"] = "; ".join(errors[:5])
        logger.warning(f"perps backfill finished with errors: {errors[:5]}")
    else:
        logger.info(f"perps backfill: {candles_n} candles, {funding_n} funding rows")
    return {"candles": candles_n, "funding": funding_n, "errors": errors}


def status(cfg: dict) -> dict:
    """Sync + cheap: powers the perpsStatus RPC / Perps page."""
    counts: dict = {}
    try:
        with db.get_db() as conn:
            counts = db.perp_collection_counts(conn)
    except Exception as e:
        counts = {"error": str(e)}
    env = kalshi_auth.get_env()
    quotes = []
    for s in _symbols(cfg):
        wire = papi.env_ticker(s, env)
        q = pws.quote(wire) or {}
        est = _est_cache.get(s) or {}
        m2u = papi.micro_to_usd
        quotes.append({
            "symbol": s,
            "last": m2u(q.get("last_usd_micro")),
            "bid": m2u(q.get("bid_usd_micro")),
            "ask": m2u(q.get("ask_usd_micro")),
            "ref": m2u(q.get("ref_usd_micro")),
            "fundingRate": q.get("funding_rate", est.get("funding_rate")),
            "nextFundingTime": est.get("next_funding_time"),
            "tsMs": q.get("ts_ms"),
        })
    return {
        "recording": bool(cfg.get("perps_record_signals", False)),
        "wsConnected": pws.is_connected(),
        "ws": pws.stats(),
        "symbols": _symbols(cfg),
        "quotes": quotes,
        "counts": counts,
        "backfill": dict(_backfill_state),
    }
