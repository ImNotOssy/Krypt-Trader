from __future__ import annotations

import asyncio
import io
import json
import logging
import logging.handlers
import os
import random
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any



_log_q: asyncio.Queue | None = None


class _StdoutHandler(logging.Handler):

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        source = "backend"
        if record.name.startswith("trader"):
            source = "trader"
        elif record.name.startswith("scanner"):
            source = "whale" if "whale" in msg.lower()[:20] else "momentum"
        elif record.name.startswith("webhook") or record.name.startswith("discord"):
            source = "discord"
        try:
            evt = {
                "type": "log",
                "level": record.levelname,
                "source": source,
                "msg": msg,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            sys.stdout.write(json.dumps(evt) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def _setup_logging() -> None:
    log_dir_base = os.environ.get("KRYPT_TRADER_USERDATA")
    if log_dir_base:
        log_dir = Path(log_dir_base) / "logs"
    else:
        log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "backend.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = _StdoutHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)




_setup_logging()
logger = logging.getLogger("service")


import db
import kalshi_api
import kalshi_auth
import scanner
import crypto15m
import trader
import crypto15m_trader
import crypto15m_record
import crypto15m_hf_record
import perps_record
import perps_ws
import perps_farmer
import perps_strategy
import kalshi_perps_api
import webhook
import kalshi_ws
import spot_ws
from config import DEFAULT_CONFIG, merge_with_defaults


def _iso_utc(s: Any) -> Any:
    if not s:
        return s
    if not isinstance(s, str):
        return s
    s = s.strip()
    if not s:
        return s
    if s.endswith("Z") or "+" in s[10:] or s.count("-") > 2:
        return s.replace(" ", "T")
    return s.replace(" ", "T") + "Z"




class State:
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    auth_ok: bool = False
    paused: bool = False
    last_whale_scan_at: str | None = None
    last_momentum_scan_at: str | None = None
    last_trade_scan_at: str | None = None
    started_at: str = ""
    active_run_id: int = 0
    ws_fill_pending: bool = False
    ws_resolve_pending: bool = False
    ws_whale_pending: bool = False
    ws_c15_pending: bool = False


STATE = State()




_stdout_lock = asyncio.Lock()


async def _send(obj: dict) -> None:
    line = json.dumps(obj, default=str) + "\n"
    async with _stdout_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


async def emit_event(name: str, data: Any = None) -> None:
    await _send({"type": "event", "name": name, "data": data})


async def respond_ok(req_id: str, result: Any = None) -> None:
    await _send({"type": "rpc", "id": req_id, "ok": True, "result": result})


async def respond_err(req_id: str, msg: str) -> None:
    await _send({"type": "rpc", "id": req_id, "ok": False, "error": msg})




async def _start_run_if_balance_known(env: str, cents: int) -> None:
    """Start a bot_run only when the balance is actually known. A cold/failed
    balance fetch returns (0,0); recording that as start_total would poison
    bot_runs (the per-run P&L shown on History), so defer instead."""
    if trader.cached_balance(env) is None:
        logger.warning(f"deferring bot_run start ({env}): balance not yet known")
        return
    with db.get_db() as conn:
        stats = db.aggregate_stats(conn, env)
        port_usd = float(stats.get("open_cost") or 0.0) + db.open_crypto15m_filled_cost_usd(conn, env)
        STATE.active_run_id = db.start_bot_run(
            conn, env=env,
            cash_usd=cents / 100.0,
            portfolio_usd=port_usd,
            lifetime_trades=int(stats.get("total_opened") or 0),
            lifetime_wins=int(stats.get("wins") or 0),
            lifetime_losses=int(stats.get("losses") or 0),
        )
    logger.info(
        f"Bot run #{STATE.active_run_id} started "
        f"(env={env}, start_total=${cents / 100.0 + port_usd:.2f})"
    )


async def _build_account_snapshot() -> dict:
    env = kalshi_auth.get_env()
    cash_cents = 0
    if STATE.auth_ok:
        try:
            await trader.refresh_balance(STATE.cfg, force=False)
        except Exception:
            pass
        bal = trader.cached_balance(env)
        if bal is not None:
            cash_cents = int(bal.get("cents", 0))
    cash_usd = cash_cents / 100.0
    with db.get_db() as conn:
        stats_env = db.aggregate_stats(conn, env)
        stats_demo = db.aggregate_stats(conn, "demo")
        stats_prod = db.aggregate_stats(conn, "production")
        crypto_open_cost = db.open_crypto15m_filled_cost_usd(conn, env)
        balance_syncing = db.recent_balance_transition(conn, env)
    open_cost = stats_env["open_cost"]
    port_usd = open_cost + crypto_open_cost
    total = cash_usd + port_usd

    user_start = float(STATE.cfg.get("start_bankroll_usd", 0.0) or 0.0)
    if user_start > 0:
        baseline = user_start
        baseline_source = "user"
    else:
        with db.get_db() as conn:
            earliest = db.earliest_pnl_total(conn, env)
        if earliest and earliest > 0:
            baseline = earliest
            baseline_source = "auto"
        else:
            baseline = total if total > 0 else 0.0
            baseline_source = "live"
    roi = ((total - baseline) / baseline * 100.0) if baseline > 0 else 0.0

    wl = stats_env["wins"] + stats_env["losses"]
    wr = (stats_env["wins"] / wl * 100.0) if wl else 0.0
    unrealized = 0.0

    with db.get_db() as conn:
        first_today = db.first_snapshot_of_today(
            conn, env, int(STATE.cfg.get("trading_timezone_offset_min", 0) or 0))
        bankroll_baseline_snap = db.earliest_pnl_total(conn, env)
        active_run = db.get_active_run(conn, env) if STATE.active_run_id else None

    today_balance_baseline = (
        float(first_today["total_usd"]) if first_today else None
    )
    today_balance_pnl = (
        total - today_balance_baseline
        if today_balance_baseline is not None else 0.0
    )
    alltime_balance_baseline = (
        float(bankroll_baseline_snap)
        if bankroll_baseline_snap is not None else baseline
    )
    alltime_balance_pnl = total - alltime_balance_baseline

    if active_run:
        session_baseline = float(active_run.get("start_total_usd") or 0.0)
        session_started_at = _iso_utc(
            active_run.get("started_at") or STATE.started_at
        )
        session_run_id = int(active_run.get("id") or 0)
    else:
        session_baseline = total
        session_started_at = STATE.started_at
        session_run_id = 0
    session_pnl = total - session_baseline if session_baseline > 0 else 0.0
    session_roi = (
        (session_pnl / session_baseline * 100.0)
        if session_baseline > 0 else 0.0
    )

    return {
        "cashUsd": cash_usd,
        "portfolioUsd": port_usd,
        "totalUsd": total,
        "balanceSyncing": balance_syncing,
        "startBankrollUsd": baseline,
        "bankrollSource": baseline_source,
        "roiPct": roi,
        "realizedPnlUsd": stats_env["realized_pnl"],
        "todayPnlUsd": today_balance_pnl,
        "alltimePnlUsd": alltime_balance_pnl,
        "todayBaselineUsd": today_balance_baseline,
        "alltimeBaselineUsd": alltime_balance_baseline,
        "sessionPnlUsd": session_pnl,
        "sessionRoiPct": session_roi,
        "sessionBaselineUsd": session_baseline,
        "sessionStartedAt": session_started_at,
        "sessionRunId": session_run_id,
        "todayWins": stats_env["today_wins"],
        "todayLosses": stats_env["today_losses"],
        "unrealizedPnlUsd": unrealized,
        "openCostUsd": open_cost,
        "feesUsd": stats_env["fees"],
        "wins": stats_env["wins"],
        "losses": stats_env["losses"],
        "winRate": wr,
        "pendingCount": stats_env["pending"],
        "openCount": stats_env["open_filled"],
        "resolvedCount": stats_env["resolved_count"],
        "totalOpened": stats_env["total_opened"],
        "byEnv": {
            "demo": {
                "wins": stats_demo["wins"],
                "losses": stats_demo["losses"],
                "realizedPnl": stats_demo["realized_pnl"],
            },
            "production": {
                "wins": stats_prod["wins"],
                "losses": stats_prod["losses"],
                "realizedPnl": stats_prod["realized_pnl"],
            },
        },
    }




def _live_pnl_usd(r: dict) -> float | None:
    """Unrealized (mark-to-market) P&L for an OPEN filled position: held
    contracts valued at the live mark price minus cost. None for resolved rows
    (use realized pnl), unfilled rows, or rows without a mark yet."""
    if r.get("resolved"):
        return None
    mark = r.get("mark_price_cents")
    if mark is None:
        return None
    filled = int(r.get("filled_contracts") or 0)
    if filled <= 0:
        return None
    market_value = filled * float(mark) / 100.0
    return round(market_value - float(r.get("cost_usd") or 0.0), 2)


def _position_row_to_js(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "signalSource": r["signal_source"],
        "signalId": int(r["signal_id"]),
        "ticker": r["ticker"],
        "eventTicker": r.get("event_ticker") or "",
        "title": r.get("title") or "",
        "category": r.get("category") or "",
        "direction": r["direction"],
        "action": r.get("action") or "buy",
        "targetContracts": int(r.get("target_contracts") or 0),
        "limitPriceCents": int(r.get("limit_price_cents") or 0),
        "filledContracts": int(r.get("filled_contracts") or 0),
        "avgFillPriceCents": (
            float(r["avg_fill_price_cents"])
            if r.get("avg_fill_price_cents") is not None
            else None
        ),
        "costUsd": float(r.get("cost_usd") or 0),
        "feesUsd": float(r.get("fees_usd") or 0),
        "clientOrderId": r.get("client_order_id") or "",
        "kalshiOrderId": r.get("kalshi_order_id"),
        "status": r["status"],
        "confidence": float(r.get("confidence") or 0),
        "edgePts": float(r.get("edge_pts") or 0),
        "signalPriceCents": float(r.get("signal_price") or 0),
        "resolved": bool(r.get("resolved") or 0),
        "outcomeCorrect": (
            int(r["outcome_correct"])
            if r.get("outcome_correct") is not None
            else None
        ),
        "settlementUsd": (
            float(r["settlement_usd"])
            if r.get("settlement_usd") is not None
            else None
        ),
        "pnlUsd": float(r["pnl_usd"]) if r.get("pnl_usd") is not None else None,
        "markPriceCents": (
            float(r["mark_price_cents"])
            if r.get("mark_price_cents") is not None
            else None
        ),
        "livePnlUsd": _live_pnl_usd(r),
        "balanceBeforeUsd": (
            float(r["balance_before_usd"])
            if r.get("balance_before_usd") is not None
            else None
        ),
        "kalshiEnv": r.get("kalshi_env") or "demo",
        "createdAt": _iso_utc(r.get("created_at")) or "",
        "lastUpdated": _iso_utc(r.get("last_updated")) or "",
        "resolvedAt": _iso_utc(r.get("resolved_at")),
        "error": r.get("error"),
    }


def _signal_row_to_js(r: dict, source: str, traded: bool) -> dict:
    if source == "whale":
        price_frac = float(r.get("price") or 0)
        price_c = int(round(price_frac * 100))
        return {
            "id": int(r["id"]),
            "source": "whale",
            "ticker": r["ticker"],
            "eventTicker": r.get("event_ticker") or "",
            "title": r.get("title") or r.get("ticker", ""),
            "category": r.get("category") or "",
            "direction": (r.get("taker_side") or "yes").lower(),
            "priceCents": price_c,
            "confidence": float(r.get("confidence") or 0),
            "edgePts": float(r.get("confidence") or 0) - price_c,
            "dollarValue": float(r.get("dollar_value") or 0),
            "createdAt": _iso_utc(r.get("created_at")) or "",
            "resolved": bool(r.get("resolved") or 0),
            "outcomeCorrect": (
                int(r["outcome_correct"])
                if r.get("outcome_correct") is not None
                else None
            ),
            "pnlEstimate": (
                float(r["pnl_estimate"])
                if r.get("pnl_estimate") is not None
                else None
            ),
            "traded": traded,
        }
    direction = (r.get("direction") or "yes").lower()
    price_frac = float(r.get("price") or 0)
    yes_c = int(round(price_frac * 100))
    cost_c = yes_c if direction == "yes" else max(0, 100 - yes_c)
    implied = yes_c if direction == "yes" else 100 - yes_c
    return {
        "id": int(r["id"]),
        "source": "momentum",
        "ticker": r["ticker"],
        "eventTicker": r.get("event_ticker") or "",
        "title": r.get("title") or r.get("ticker", ""),
        "category": r.get("category") or "",
        "direction": direction,
        "priceCents": cost_c,
        "confidence": float(r.get("confidence") or 0),
        "edgePts": float(r.get("confidence") or 0) - implied,
        "signalType": r.get("signal_type") or "",
        "createdAt": _iso_utc(r.get("created_at")) or "",
        "resolved": bool(r.get("resolved") or 0),
        "outcomeCorrect": (
            int(r["outcome_correct"])
            if r.get("outcome_correct") is not None
            else None
        ),
        "pnlEstimate": (
            float(r["pnl_estimate"])
            if r.get("pnl_estimate") is not None
            else None
        ),
        "traded": traded,
    }




_loop_task: asyncio.Task | None = None
_c15_task: asyncio.Task | None = None
_c15_hf_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None

_bg_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Run a notification coroutine (Discord webhook) as a background task —
    a slow or unreachable Discord must never stall the trading loop (inline
    awaits cost up to 8s per send, multiplied by event bursts). Errors are
    swallowed (webhooks are best-effort); a strong reference is kept so the
    task can't be garbage-collected mid-flight."""
    async def _quiet():
        try:
            await coro
        except Exception:
            pass
    try:
        t = asyncio.get_event_loop().create_task(_quiet())
    except RuntimeError:
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


_event_webhook_last: dict[int, str] = {}


def _should_fire_event_webhook(pos_id: int, kind: str) -> bool:
    if not pos_id:
        return True
    if _event_webhook_last.get(pos_id) == kind:
        return False
    _event_webhook_last[pos_id] = kind
    if len(_event_webhook_last) > 2000:
        for old in list(_event_webhook_last)[:500]:
            _event_webhook_last.pop(old, None)
    return True


def _on_ws_fill(_msg: dict) -> None:
    """A WebSocket fill arrived — wake the order poll on the next loop tick
    instead of waiting out the 30s timer. REST poll remains the accounting
    truth; this only removes the detection latency."""
    STATE.ws_fill_pending = True
    STATE.ws_c15_pending = True


def _on_ws_lifecycle(msg: dict) -> None:
    """A market was determined/settled — wake the resolution check immediately."""
    if (msg or {}).get("event_type") in ("determined", "settled"):
        STATE.ws_resolve_pending = True


def _on_ws_trade(trade: dict) -> None:
    """Per-trade WS hook (sync, must stay cheap): flag whale-sized prints so the
    scanner runs immediately instead of waiting out whale_scan_interval."""
    try:
        count = float(trade.get("count_fp") or 0)
        side = trade.get("taker_side") or ""
        price = float(
            (trade.get("yes_price_dollars") if side == "yes"
             else trade.get("no_price_dollars")) or 0
        )
        if count * price >= float(STATE.cfg.get("min_whale_usd", 2500) or 2500):
            STATE.ws_whale_pending = True
    except (TypeError, ValueError):
        pass


def _ws_held_tickers() -> set[str]:
    """Tickers we currently hold/work (open bot positions + open 15m positions),
    for the env in play — the set the WS subscribes orderbook/ticker/lifecycle to."""
    env = kalshi_auth.get_env()
    out: set[str] = set()
    try:
        with db.get_db() as conn:
            for r in db.get_open_bot_positions(conn):
                if r.get("kalshi_env") == env and r.get("ticker"):
                    out.add(r["ticker"])
            for r in db.get_open_crypto15m(conn, env):
                if r.get("ticker"):
                    out.add(r["ticker"])
    except Exception:
        pass
    return out


async def _reverify_auth_if_needed() -> bool:
    """Self-heal a latched-off auth state.

    The one-shot startup verify in _main() sets STATE.auth_ok=False on a single
    transient failure (network stack not ready when Electron spawns Python at cold
    boot/resume, a Kalshi 5xx/429 burst that outlasts the in-call retry window).
    Every live path — trade scan, order poll, reconcile, resolution — is gated on
    STATE.auth_ok, and nothing else in the loop ever flips it back True, so one
    boot blip silently disables trading for the whole session until the user
    manually re-tests credentials. This re-primes and re-verifies against Kalshi;
    on success it re-enables trading. Returns True iff it flipped auth_ok True.
    """
    if STATE.auth_ok:
        return False
    if not kalshi_auth.credentials_present(kalshi_auth.get_env()):
        return False
    async with kalshi_auth.ENV_LOCK:
        env0 = kalshi_auth.get_env()
        kalshi_auth.prime_credentials(sync_time=False)
    await asyncio.to_thread(kalshi_auth.sync_server_time, True)
    bal = await kalshi_api.get_balance(pin_env=env0)
    int(bal.get("balance", 0))
    STATE.auth_ok = True
    await emit_event("backend:authChanged", {"authOk": True})
    return True


async def _scanner_and_trader_loop() -> None:
    last_whale = 0.0
    last_momentum = 0.0
    last_auth_retry = 0.0
    last_trade = 0.0
    last_poll = 0.0
    last_resolve = 0.0
    last_market_sync = 0.0
    last_event_sync = 0.0
    last_account_emit = 0.0
    last_snapshot_persist = 0.0
    last_reconcile = 0.0
    _consec_reconcile_fails = 0
    last_crypto15m_record = 0.0
    last_perps_record = 0.0
    last_perps_farm = 0.0
    last_perps_strat = 0.0
    last_ws_subs = 0.0
    last_cleanup = 0.0
    last_stats_push = asyncio.get_event_loop().time()

    try:
        cnt = await scanner.sync_markets(max_pages=10)
        logger.info(f"Initial market sync: {cnt} markets")
    except Exception as e:
        logger.warning(f"initial market sync failed: {e}")


    while not (_loop_stop and _loop_stop.is_set()):
        now = asyncio.get_event_loop().time()
        cfg = STATE.cfg

        kalshi_ws.set_env(kalshi_auth.get_env())
        perps_ws.set_env(kalshi_auth.get_env())

        if STATE.paused:
            await asyncio.sleep(1)
            continue

        try:
            if not STATE.auth_ok and now - last_auth_retry >= 60:
                last_auth_retry = now
                if await _reverify_auth_if_needed():
                    logger.info("auth re-verified — trading re-enabled")
        except Exception as e:
            logger.debug(f"auth re-verify failed (will retry): {e}")

        try:
            if now - last_market_sync >= float(cfg.get("market_refresh_interval", 300)):
                await scanner.sync_markets(max_pages=10)
                last_market_sync = now
            if now - last_event_sync >= 600:
                await scanner.sync_events()
                last_event_sync = now
        except Exception as e:
            logger.warning(f"sync error: {e}")

        try:
            collect_main = bool(cfg.get("main_record_signals", True)) or bool(
                cfg.get("enable_trading")
            )
            whale_due = collect_main and (
                now - last_whale >= float(cfg.get("whale_scan_interval", 120))
                or (STATE.ws_whale_pending and now - last_whale >= 2)
            )
            if whale_due:
                STATE.ws_whale_pending = False
                cnt, rows = await scanner.scan_whales(cfg)
                last_whale = now
                STATE.last_whale_scan_at = datetime.now(timezone.utc).isoformat()
                if cnt:
                    logger.info(f"whale scan: {cnt} new")
                    last_trade = 0.0
                with db.get_db() as conn:
                    seen = db.already_traded_signal_ids(
                        conn, "whale", kalshi_auth.get_env()
                    )
                for row in rows:
                    js = _signal_row_to_js(row, "whale", int(row["id"]) in seen)
                    await emit_event("signal:new", js)
                    if cfg.get("enable_discord"):
                        _fire_and_forget(webhook.send_whale(
                            cfg.get("whale_webhook_url", ""), row
                        ))
        except Exception as e:
            logger.warning(f"whale scan error: {e}")

        try:
            if collect_main and now - last_momentum >= float(cfg.get("momentum_scan_interval", 90)):
                cnt, rows = await scanner.scan_momentum(cfg)
                last_momentum = now
                STATE.last_momentum_scan_at = datetime.now(timezone.utc).isoformat()
                if cnt:
                    logger.info(f"momentum scan: {cnt} new")
                with db.get_db() as conn:
                    seen = db.already_traded_signal_ids(
                        conn, "momentum", kalshi_auth.get_env()
                    )
                for row in rows:
                    js = _signal_row_to_js(row, "momentum", int(row["id"]) in seen)
                    await emit_event("signal:new", js)
                    if cfg.get("enable_discord"):
                        _fire_and_forget(webhook.send_momentum(
                            cfg.get("momentum_webhook_url", ""), row
                        ))
        except Exception as e:
            logger.warning(f"momentum scan error: {e}")

        try:
            if (
                STATE.auth_ok
                and now - last_trade >= float(cfg.get("trade_scan_interval", 20))
            ):
                placed = await trader.scan_for_trades(cfg)
                last_trade = now
                STATE.last_trade_scan_at = datetime.now(timezone.utc).isoformat()
                for row in placed:
                    js = _position_row_to_js(row)
                    await emit_event("position:new", js)
                    if (
                        cfg.get("enable_discord")
                        and _should_fire_event_webhook(int(row.get("id") or 0), "placed")
                    ):
                        _fire_and_forget(webhook.send_event(
                            cfg.get("event_webhook_url", ""),
                            "placed", row, kalshi_auth.get_env(),
                        ))
        except Exception as e:
            logger.error(f"trade scan error: {e}", exc_info=True)

        try:
            if (
                STATE.auth_ok
                and (now - last_poll >= float(cfg.get("position_poll_interval", 30))
                     or STATE.ws_fill_pending)
            ):
                STATE.ws_fill_pending = False
                updated = await trader.poll_open_orders(cfg)
                last_poll = now
                for row in updated:
                    js = _position_row_to_js(row)
                    await emit_event("position:update", js)
                    if cfg.get("enable_discord"):
                        kind = row["status"]
                        if (
                            kind in ("filled", "partial", "canceled", "gone", "error")
                            and _should_fire_event_webhook(
                                int(row.get("id") or 0), kind
                            )
                        ):
                            _fire_and_forget(webhook.send_event(
                                cfg.get("event_webhook_url", ""),
                                kind, row, kalshi_auth.get_env(),
                            ))
        except Exception as e:
            logger.error(f"poll error: {e}", exc_info=True)

        try:
            if STATE.auth_ok and now - last_reconcile >= 30:
                summary, changed = await trader.reconcile_positions_with_kalshi()
                last_reconcile = now
                _consec_reconcile_fails = 0
                if any(summary.values()):
                    logger.info(f"reconcile: {summary}")
                    await emit_event("backend:reconciled", summary)
                for row in changed:
                    await emit_event(
                        "position:update", _position_row_to_js(row),
                    )
        except Exception as e:
            _consec_reconcile_fails += 1
            if _consec_reconcile_fails == 10:
                logger.warning(
                    f"periodic reconcile has failed {_consec_reconcile_fails}x "
                    f"in a row ({e}) — positions may be stale; open-count "
                    f"gating may block new entries"
                )
                await emit_event("backend:degraded", {
                    "component": "reconcile", "error": str(e)[:200],
                    "action": "retrying",
                })
            else:
                logger.debug(f"periodic reconcile failed: {e}")

        try:
            if (
                STATE.auth_ok
                and (now - last_resolve >= float(cfg.get("resolution_check_interval", 300))
                     or STATE.ws_resolve_pending)
            ):
                STATE.ws_resolve_pending = False
                resolved_pos = await trader.mark_resolved_positions(cfg)
                await scanner.resolve_alerts_from_markets()
                await scanner.resolve_whales_from_markets()
                last_resolve = now
                for row in resolved_pos:
                    js = _position_row_to_js(row)
                    await emit_event("position:update", js)
                    if cfg.get("enable_discord"):
                        kind = "won" if row.get("outcome_correct") == 1 else (
                            "lost" if row.get("outcome_correct") == 0 else "na"
                        )
                        if _should_fire_event_webhook(
                            int(row.get("id") or 0), kind
                        ):
                            _fire_and_forget(webhook.send_event(
                                cfg.get("event_webhook_url", ""),
                                kind, row, kalshi_auth.get_env(),
                            ))
        except Exception as e:
            logger.error(f"resolution error: {e}", exc_info=True)


        try:
            if (
                cfg.get("crypto15m_record_signals", True)
                and now - last_crypto15m_record >= 25
            ):
                await crypto15m_record.record_tick(cfg)
                last_crypto15m_record = now
        except Exception as e:
            logger.debug(f"crypto15m record error: {e}")

        try:
            perps_record.ensure_ws(cfg, kalshi_auth.get_env())
            if (
                cfg.get("perps_record_signals", False)
                and now - last_perps_record >= 15
            ):
                await perps_record.record_tick(cfg)
                last_perps_record = now
                if perps_record.backfill_needed():
                    _fire_and_forget(perps_record.backfill(cfg))
        except Exception as e:
            logger.debug(f"perps record error: {e}")

        try:
            if (
                cfg.get("perps_strat_enabled", False)
                and cfg.get("perps_record_signals", False)
                and now - last_perps_strat >= 5
            ):
                if not cfg.get("perps_strat_live") or STATE.auth_ok:
                    await perps_strategy.tick(cfg)
                last_perps_strat = now
        except Exception as e:
            logger.debug(f"perps strategy error: {e}")

        try:
            if (
                cfg.get("perps_farm_enabled", False)
                and cfg.get("perps_record_signals", False)
                and STATE.auth_ok
                and now - last_perps_farm >= 2.5
            ):
                await perps_farmer.farm_tick(cfg)
                last_perps_farm = now
            elif not cfg.get("perps_farm_enabled", False):
                await perps_farmer.ensure_stopped()
        except Exception as e:
            logger.debug(f"perps farm error: {e}")

        try:
            if now - last_cleanup >= float(cfg.get("db_cleanup_interval", 3600)):
                summary = await asyncio.get_event_loop().run_in_executor(
                    None, db.run_maintenance
                )
                if summary.get("deleted") or summary.get("vacuumed"):
                    logger.info(
                        f"db maintenance: pruned {summary['deleted']} rows, "
                        f"vacuumed={summary['vacuumed']} "
                        f"(reclaimable {summary['reclaimable_mb']}MB)"
                    )
                last_cleanup = now
        except Exception as e:
            logger.warning(f"db maintenance failed: {e}")

        try:
            if kalshi_ws.is_connected() and now - last_ws_subs >= 5:
                watch = _ws_held_tickers() | crypto15m.active_tickers()
                kalshi_ws.set_orderbook_markets(watch)
                kalshi_ws.set_ticker_markets(watch)
                kalshi_ws.set_lifecycle_markets(watch)
                last_ws_subs = now
        except Exception as e:
            logger.debug(f"ws subscription reconcile error: {e}")

        try:
            if now - last_account_emit >= 15:
                snap = await _build_account_snapshot()
                balance_known = trader.cached_balance(kalshi_auth.get_env()) is not None
                if STATE.auth_ok and balance_known and now - last_snapshot_persist >= 60:
                    last_snapshot_persist = now
                    with db.get_db() as conn:
                        db.insert_pnl_snapshot(
                            conn,
                            cash_usd=snap["cashUsd"],
                            portfolio_usd=snap["portfolioUsd"],
                            realized_pnl_usd=snap["realizedPnlUsd"],
                            wins=snap["wins"], losses=snap["losses"],
                            open_positions=snap["openCount"] + snap["pendingCount"],
                            env=kalshi_auth.get_env(),
                        )
                        if STATE.active_run_id:
                            db.heartbeat_bot_run(
                                conn, STATE.active_run_id,
                                cash_usd=snap["cashUsd"],
                                portfolio_usd=snap["portfolioUsd"],
                                lifetime_trades=snap["totalOpened"],
                                lifetime_wins=snap["wins"],
                                lifetime_losses=snap["losses"],
                            )
                await emit_event("account:update", snap)
                last_account_emit = now
        except Exception as e:
            logger.debug(f"account snapshot error: {e}")

        try:
            push_iv = float(cfg.get("stats_push_interval", 3600) or 3600)
            if (
                cfg.get("enable_discord")
                and cfg.get("stats_webhook_url")
                and now - last_stats_push >= push_iv
            ):
                snap_for_stats = await _build_account_snapshot()
                try:
                    await webhook.send_stats(
                        cfg.get("stats_webhook_url", ""),
                        snap_for_stats,
                        kalshi_auth.get_env(),
                    )
                    logger.info(
                        f"stats webhook fired (next in "
                        f"{int(push_iv // 60)}m)"
                    )
                except Exception as e:
                    logger.debug(f"stats webhook send failed: {e}")
                last_stats_push = now
        except Exception as e:
            logger.debug(f"stats webhook scheduler error: {e}")

        await asyncio.sleep(1)


async def _crypto15m_loop() -> None:
    """Dedicated 15m-executor loop, ISOLATED from the main scanner/trader loop.

    The main loop is one long serial iteration (market sync, scans, order poll,
    reconcile, resolution) — a Kalshi 5xx storm or a slow 10-page market sync
    stalled the 15m tick 5-80s, exactly the windows where a stop-loss needed
    its 4s cadence to protect capital in a fast drop. run_tick manages its own
    concurrency through DB state, and only this task calls it, so ticks never
    overlap. A WS fill wakes the next tick within ~0.5s (STATE.ws_c15_pending)
    so a just-filled entry arms its stop-loss/TP without waiting out the poll."""
    last_tick = 0.0
    while not (_loop_stop and _loop_stop.is_set()):
        try:
            cfg = STATE.cfg
            now = asyncio.get_event_loop().time()
            want_spot_ws = bool(cfg.get("crypto15m_spot_ws", True))
            kalshi_ws.set_cf_enabled(want_spot_ws)
            if want_spot_ws and not spot_ws.is_running():
                spot_ws.start()
            elif not want_spot_ws and spot_ws.is_running():
                await spot_ws.stop()
            due = (
                not STATE.paused
                and (
                    now - last_tick >= float(cfg.get("crypto15m_poll_sec", 4))
                    or (STATE.ws_c15_pending and now - last_tick >= 1)
                )
            )
            if due:
                STATE.ws_c15_pending = False
                changed = await crypto15m_trader.run_tick(
                    cfg, authed=STATE.auth_ok, session_start=STATE.started_at
                )
                last_tick = asyncio.get_event_loop().time()
                if changed:
                    _fire_and_forget(trader.refresh_balance(cfg, force=True))
        except Exception as e:
            logger.error(f"crypto15m tick error: {e}", exc_info=True)
        await asyncio.sleep(0.5)


async def _crypto15m_hf_loop() -> None:
    """Dedicated high-frequency book+spot recorder loop (research feed).

    Isolated from the executor loop so its ~5Hz cadence never rides the 0.5s
    trading tick, and a slow DB flush can't stall stop-loss management. Gated
    entirely on `crypto15m_hf_record`: when off, it idles at 1s and keeps the
    recorder flushed/closed so leaving it disabled costs nothing. The sampler
    period is `crypto15m_hf_interval_ms` (default 200ms; floor 50ms)."""
    was_on = False
    while not (_loop_stop and _loop_stop.is_set()):
        cfg = STATE.cfg
        on = bool(cfg.get("crypto15m_hf_record", False))
        try:
            if on:
                was_on = True
                await crypto15m_hf_record.sample(cfg)
            elif was_on:
                crypto15m_hf_record.close()
                was_on = False
        except Exception as e:
            logger.debug(f"crypto15m hf sample error: {e}")
        if on:
            iv = max(50, int(cfg.get("crypto15m_hf_interval_ms", 200) or 200))
            await asyncio.sleep(iv / 1000.0)
        else:
            await asyncio.sleep(1.0)
    try:
        crypto15m_hf_record.close()
    except Exception:
        pass


def _watchdog_restart(name: str, factory):
    """Done-callback for the core loop tasks: if one DIES (a loop-killing
    exception like MemoryError escaping the per-section try/excepts), the
    process previously kept serving RPCs — balances refreshed, the UI stayed
    green — while all scanning/trading/exits were silently dead. Log loud,
    tell the renderer, and restart the loop after a short breather."""
    def _cb(task: asyncio.Task) -> None:
        if task.cancelled() or (_loop_stop and _loop_stop.is_set()):
            return
        exc = task.exception()
        logger.critical(
            f"{name} DIED unexpectedly ({type(exc).__name__ if exc else 'no exception'}: "
            f"{exc}) — restarting in 5s", exc_info=exc,
        )

        async def _restart():
            await asyncio.sleep(5)
            if _loop_stop and _loop_stop.is_set():
                return
            await emit_event("backend:degraded", {
                "component": name, "error": str(exc)[:200] if exc else "",
                "action": "restarted",
            })
            factory()
        _fire_and_forget(_restart())
    return _cb


def _spawn_main_loop() -> None:
    global _loop_task
    _loop_task = asyncio.create_task(_scanner_and_trader_loop())
    _loop_task.add_done_callback(_watchdog_restart("trader loop", _spawn_main_loop))


def _spawn_c15_loop() -> None:
    global _c15_task
    _c15_task = asyncio.create_task(_crypto15m_loop())
    _c15_task.add_done_callback(_watchdog_restart("crypto15m loop", _spawn_c15_loop))


def _spawn_c15_hf_loop() -> None:
    global _c15_hf_task
    _c15_hf_task = asyncio.create_task(_crypto15m_hf_loop())
    _c15_hf_task.add_done_callback(_watchdog_restart("crypto15m hf loop", _spawn_c15_hf_loop))


async def _start_loop() -> None:
    global _loop_task, _c15_task, _c15_hf_task, _loop_stop
    if _loop_task and not _loop_task.done():
        return
    _loop_stop = asyncio.Event()
    _spawn_main_loop()
    _spawn_c15_loop()
    _spawn_c15_hf_loop()


async def _stop_loop() -> None:
    global _loop_task, _c15_task, _c15_hf_task, _loop_stop
    if _loop_stop:
        _loop_stop.set()
    for task in (_loop_task, _c15_task, _c15_hf_task):
        if task:
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                pass




async def _h_ping(_p: dict) -> dict:
    return {"pong": True, "ts": datetime.now(timezone.utc).isoformat()}


async def _h_setConfig(p: dict) -> dict:
    cfg = merge_with_defaults(p.get("config") or {})
    STATE.cfg = cfg
    logger.info(
        f"setConfig applied: enable_trading={cfg.get('enable_trading')} "
        f"trade_whales={cfg.get('trade_whales')} "
        f"trade_momentum={cfg.get('trade_momentum')} "
        f"max_open={cfg.get('max_open_positions')} "
        f"max_daily="
        f"{'∞' if cfg.get('unlimited_daily_new_positions') else cfg.get('max_daily_new_positions')} "
        f"stop_loss={cfg.get('stop_loss_on_day')} "
        f"env={cfg.get('kalshi_env')}"
    )
    new_env = cfg.get("kalshi_env", "demo")
    verify_auth = False
    async with kalshi_auth.ENV_LOCK:
        prev_env = kalshi_auth.get_env()
        kalshi_auth.set_env(new_env)
        env_changed = new_env != prev_env
        if env_changed:
            kalshi_auth.reset_credential_cache()
            if kalshi_auth.credentials_present(new_env):
                try:
                    kalshi_auth.prime_credentials(sync_time=False)
                    verify_auth = True
                except Exception as e:
                    logger.warning(f"env-switch auth failed: {e}")
                    STATE.auth_ok = False
            else:
                STATE.auth_ok = False

    if env_changed and verify_auth:
        try:
            await asyncio.to_thread(kalshi_auth.sync_server_time, True)
            bal = await kalshi_api.get_balance(pin_env=new_env)
            int(bal.get("balance", 0))
            STATE.auth_ok = True
        except Exception as e:
            logger.warning(f"env-switch auth failed: {e}")
            STATE.auth_ok = False

    if env_changed:
        await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})

        try:
            if STATE.active_run_id:
                with db.get_db() as conn:
                    db.end_bot_run(conn, STATE.active_run_id)
                STATE.active_run_id = 0
            if STATE.auth_ok:
                cents, _ = await trader.refresh_balance(STATE.cfg, force=True)
                await _start_run_if_balance_known(new_env, cents)
        except Exception as e:
            logger.warning(f"could not roll bot_run on env switch: {e}")
    return {"ok": True}


async def _h_setCredentials(p: dict) -> dict:
    p = p or {}
    api_key = p.get("apiKey", "")
    rsa_pem = p.get("rsaPem", "")
    env = p.get("env")
    kalshi_auth.save_credentials(api_key, rsa_pem, env)
    status = kalshi_auth.credentials_status_all()
    await emit_event("credentials:changed", status)
    return status


async def _h_clearCredentials(p: dict) -> dict:
    p = p or {}
    env = p.get("env")
    kalshi_auth.clear_credentials(env)
    if env in (None, kalshi_auth.get_env()):
        STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": False})
    status = kalshi_auth.credentials_status_all()
    await emit_event("credentials:changed", status)
    return status


async def _h_credentialStatus(_p: dict) -> dict:
    return kalshi_auth.credentials_status_all()


async def _h_testCredentials(p: dict) -> dict:
    p = p or {}
    target_env = p.get("env") or kalshi_auth.get_env()
    if not kalshi_auth.credentials_present(target_env):
        raise RuntimeError(f"credentials not set for {target_env}")

    async with kalshi_auth.ENV_LOCK:
        saved_env = kalshi_auth.get_env()
        if target_env != saved_env:
            kalshi_auth.set_env(target_env)
        kalshi_auth.reset_credential_cache()
        try:
            kalshi_auth.prime_credentials(sync_time=False)
            await asyncio.to_thread(kalshi_auth.sync_server_time, True)
            bal = await kalshi_api.get_balance()
        finally:
            if target_env != saved_env:
                kalshi_auth.set_env(saved_env)
                kalshi_auth.reset_credential_cache()
                try:
                    kalshi_auth.prime_credentials(sync_time=False)
                except Exception:
                    pass
    cents = int(bal.get("balance", 0))
    if target_env == saved_env:
        STATE.auth_ok = True
        await emit_event("backend:authChanged", {"authOk": True})
    return {"env": target_env, "balanceUsd": cents / 100.0}


async def _h_account(_p: dict) -> dict:
    return await _build_account_snapshot()


async def _h_pnlSeries(p: dict) -> list:
    hours = int((p or {}).get("sinceHours", 168))
    env = kalshi_auth.get_env()
    with db.get_db() as conn:
        rows = db.get_pnl_snapshots(conn, since_hours=hours, env=env)
    return [
        {
            "at": _iso_utc(r["at"]),
            "cashUsd": float(r["cash_usd"] or 0),
            "portfolioUsd": float(r["portfolio_usd"] or 0),
            "totalUsd": float(r["total_usd"] or 0),
            "realizedPnlUsd": float(r["realized_pnl_usd"] or 0),
            "openPositions": int(r["open_positions"] or 0),
        }
        for r in rows
    ]


async def _h_positions(p: dict) -> list:
    f = p or {}
    status = f.get("status")
    resolved = f.get("resolved")
    src = f.get("signalSource")
    limit = int(f.get("limit") or 500)

    sql = "SELECT * FROM bot_positions WHERE 1=1"
    args: list = []
    if status:
        placeholders = ",".join("?" for _ in status)
        sql += f" AND status IN ({placeholders})"
        args.extend(status)
    if resolved is not None:
        sql += " AND resolved = ?"
        args.append(1 if resolved else 0)
    if src:
        sql += " AND signal_source = ?"
        args.append(src)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    with db.get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_position_row_to_js(dict(r)) for r in rows]


async def _h_signals(p: dict) -> list:
    f = p or {}
    src = f.get("source")
    min_conf = float(f.get("minConfidence") or 0)
    limit = int(f.get("limit") or 200)

    out: list[dict] = []
    env = kalshi_auth.get_env()
    with db.get_db() as conn:
        if src in (None, "whale"):
            rows = conn.execute(
                """SELECT * FROM whale_trades
                   WHERE confidence >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (min_conf, limit),
            ).fetchall()
            seen = db.already_traded_signal_ids(conn, "whale", env)
            for r in rows:
                d = dict(r)
                out.append(_signal_row_to_js(d, "whale", int(d["id"]) in seen))
        if src in (None, "momentum"):
            rows = conn.execute(
                """SELECT * FROM alerts
                   WHERE confidence >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (min_conf, limit),
            ).fetchall()
            seen = db.already_traded_signal_ids(conn, "momentum", env)
            for r in rows:
                d = dict(r)
                out.append(_signal_row_to_js(d, "momentum", int(d["id"]) in seen))
    out.sort(key=lambda s: s["createdAt"], reverse=True)
    return out[:limit]


async def _h_scannerStats(_p: dict) -> dict:
    with db.get_db() as conn:
        markets = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE status IN ('active','open')"
        ).fetchone()[0]
        wt = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN discord_sent=1 THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS wins
               FROM whale_trades"""
        ).fetchone()
        al = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN discord_sent=1 THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS wins
               FROM alerts"""
        ).fetchone()

    def wr(d) -> dict:
        total = int(d["total"] or 0)
        resolved = int(d["resolved"] or 0)
        wins = int(d["wins"] or 0)
        return {
            "total": total,
            "sent": int(d["sent"] or 0),
            "resolved": resolved,
            "winRate": (wins / resolved * 100.0) if resolved else 0.0,
        }

    return {
        "whales": wr(wt),
        "momentum": wr(al),
        "marketsTracked": int(markets or 0),
        "lastWhaleScanAt": STATE.last_whale_scan_at,
        "lastMomentumScanAt": STATE.last_momentum_scan_at,
        "lastTradeScanAt": STATE.last_trade_scan_at,
    }


async def _h_cancelAllOpen(_p: dict) -> dict:
    if not STATE.auth_ok:
        raise RuntimeError("not authenticated")
    n = await trader.cancel_all_open()
    return {"canceled": n}


async def _h_flatten(_p: dict) -> dict:
    if not STATE.auth_ok:
        raise RuntimeError("not authenticated")
    canceled = await trader.cancel_all_open()
    return {"closed": canceled}


async def _h_runOnce(p: dict) -> dict:
    action = (p or {}).get("action")
    if action == "syncMarkets":
        cnt = await scanner.sync_markets(max_pages=10)
        return {"summary": f"Synced {cnt} markets"}
    if action == "pollOrders":
        upd = await trader.poll_open_orders(STATE.cfg)
        return {"summary": f"Polled, {len(upd)} updates"}
    if action == "resolveAll":
        rp = await trader.mark_resolved_positions(STATE.cfg)
        ra = await scanner.resolve_alerts_from_markets()
        rw = await scanner.resolve_whales_from_markets()
        return {"summary": f"Positions:{len(rp)} alerts:{ra} whales:{rw}"}
    if action == "reconcilePositions":
        s, changed = await trader.reconcile_positions_with_kalshi()
        for row in changed:
            await emit_event("position:update", _position_row_to_js(row))
        return {
            "summary": (
                f"Reconciled — rescued {s.get('rescued', 0)}, "
                f"resurrected {s.get('resurrected', 0)}, "
                f"imported {s.get('imported_unknowns', 0)}"
            ),
        }
    if action == "recomputePnl":
        s = await trader.recompute_pnl_from_kalshi()
        return {
            "summary": f"Re-resolved {s['recomputed']} of {s['cleared']} positions from Kalshi",
        }
    if action == "reconcileFills":
        s = await trader.reconcile_fills_from_kalshi()
        return {
            "summary": (
                f"Reconciled {s['fills_reconciled']} orders from Kalshi fills, "
                f"re-resolved {s['pnl_recomputed']} of {s['pnl_cleared']}"
            ),
        }
    if action == "auditPnl":
        s = await trader.audit_pnl(200)
        worst_lines = []
        for x in s.get("samples", [])[:8]:
            worst_lines.append(
                f"  {x['ticker']} {x['direction']}: stored ${x['stored_pnl']:+.2f} → fresh ${x['fresh_pnl']:+.2f} (Δ ${x['delta']:+.2f})"
            )
        msg = (
            f"Audited {s['checked']} resolved positions: {s['flagged']} flagged. "
            f"Sum stored=${s['sum_stored_pnl']:+.2f} vs fresh=${s['sum_recompute_pnl']:+.2f} "
            f"(Δ ${s['delta']:+.2f})"
        )
        if worst_lines:
            msg += "\n" + "\n".join(worst_lines)
        return {"summary": msg, "audit": s}
    raise ValueError(f"unknown action: {action}")


async def _h_pause(p: dict) -> dict:
    STATE.paused = bool((p or {}).get("paused", False))
    return {"paused": STATE.paused}


def _run_row_to_js(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "kalshiEnv": r.get("kalshi_env") or "demo",
        "startedAt": _iso_utc(r.get("started_at")) or "",
        "endedAt": _iso_utc(r.get("ended_at")),
        "startCashUsd": float(r.get("start_cash_usd") or 0),
        "startPortfolioUsd": float(r.get("start_portfolio_usd") or 0),
        "startTotalUsd": float(r.get("start_total_usd") or 0),
        "endCashUsd": (
            float(r["end_cash_usd"])
            if r.get("end_cash_usd") is not None else None
        ),
        "endPortfolioUsd": (
            float(r["end_portfolio_usd"])
            if r.get("end_portfolio_usd") is not None else None
        ),
        "endTotalUsd": (
            float(r["end_total_usd"])
            if r.get("end_total_usd") is not None else None
        ),
        "pnlUsd": float(r.get("pnl_usd") or 0),
        "tradesOpened": int(r.get("trades_opened") or 0),
        "tradesWon": int(r.get("trades_won") or 0),
        "tradesLost": int(r.get("trades_lost") or 0),
        "isActive": r.get("ended_at") is None,
    }


async def _h_botRuns(p: dict) -> dict:
    env = (p or {}).get("env")
    limit = int((p or {}).get("limit") or 100)
    with db.get_db() as conn:
        rows = db.get_recent_runs(conn, env=env, limit=limit)
        active = (
            db.get_active_run(conn, kalshi_auth.get_env())
            if STATE.active_run_id else None
        )
    return {
        "runs": [_run_row_to_js(r) for r in rows],
        "activeRunId": STATE.active_run_id,
        "activeRun": _run_row_to_js(active) if active else None,
    }


async def _h_shutdown(_p: dict) -> dict:
    asyncio.create_task(_shutdown())
    return {"shutting_down": True}


async def _h_factoryReset(_p: dict) -> dict:
    logger.warning("factory reset: STARTING — pausing trader loop")
    await _stop_loop()

    if STATE.active_run_id:
        try:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
        except Exception as e:
            logger.warning(f"factoryReset: end_bot_run: {e}")
        STATE.active_run_id = 0

    summary = await asyncio.to_thread(db.factory_reset)
    deleted_total = sum(
        v for k, v in summary.items()
        if not k.startswith("_") and isinstance(v, int) and v > 0
    )
    if summary.get("_errors"):
        logger.error(
            f"factory reset: PARTIAL — deleted {deleted_total} rows, "
            f"errors={summary['_errors']}"
        )
    else:
        logger.warning(
            f"factory reset: COMPLETE — deleted {deleted_total} rows "
            f"({summary})"
        )

    STATE.last_whale_scan_at = ""
    STATE.last_momentum_scan_at = ""
    STATE.last_trade_scan_at = ""

    if STATE.auth_ok:
        try:
            cents, _ = await trader.refresh_balance(STATE.cfg, force=True)
            await _start_run_if_balance_known(kalshi_auth.get_env(), cents)
        except Exception as e:
            logger.warning(f"factoryReset: post-reset run start: {e}")

    await emit_event("data:reset", {"summary": summary})
    snap = await _build_account_snapshot()
    await emit_event("account:update", snap)

    await _start_loop()
    logger.info("factory reset: trader loop resumed")

    return {"ok": True, "deleted": summary}


async def _h_crypto15m(_p: dict) -> dict:
    return await crypto15m.snapshot(STATE.cfg)


async def _h_crypto15mStatus(_p: dict) -> dict:
    return await crypto15m_trader.status(
        STATE.cfg, authed=STATE.auth_ok, session_start=STATE.started_at
    )


async def _h_kalshiMarketUrl(p: dict) -> dict:
    url = await kalshi_api.web_market_url(
        event_ticker=str(p.get("eventTicker") or ""),
        ticker=str(p.get("ticker") or ""),
        env=str(p.get("env") or "production"),
    )
    return {"url": url}



async def _h_trading_status(p: dict) -> dict:
    """Ordered gate checklist for both engines — the "why isn't it trading"
    panel. Each row: {id, label, state: ok|blocked|off, reason}. The first
    blocked row is the answer."""
    cfg = STATE.cfg
    env = trader.get_env()
    main: list[dict] = []

    def gate(gid: str, label: str, ok: bool, reason: str = "", off: bool = False) -> None:
        main.append({
            "id": gid, "label": label,
            "state": "off" if off else ("ok" if ok else "blocked"),
            "reason": reason if not ok else "",
        })

    gate("paused", "Engine not paused", not STATE.paused, "paused by user")
    gate("auth", "Kalshi auth", bool(STATE.auth_ok), "auth failed — check API keys")
    enabled = bool(cfg.get("enable_trading"))
    gate("master", "Trading enabled", enabled, "master switch is OFF", off=not enabled)
    try:
        blocked, why = trader._is_blocked_by_daily_risk(cfg, env)
        gate("dailyRisk", "Daily stop/take-profit", not blocked, why or "")
    except Exception:
        gate("dailyRisk", "Daily stop/take-profit", True)
    try:
        hblocked, hwhy = trader._is_blocked_by_trading_hours(cfg)
        gate("hours", "Trading hours", not hblocked, hwhy or "")
    except Exception:
        gate("hours", "Trading hours", True)
    lc = dict(getattr(trader, "last_cycle", {}) or {})
    if lc.get("skipReason"):
        gate("cycle", "Last scan cycle", False, str(lc.get("skipReason")))
    else:
        gate("cycle", "Last scan cycle", True)

    c15 = await crypto15m_trader.status(cfg, authed=STATE.auth_ok, session_start=STATE.started_at)
    return {
        "main": main,
        "mainFilterCounts": lc.get("filterCounts") or {},
        "mainCandidates": lc.get("candidates") or 0,
        "mainPlaced": lc.get("placed") or 0,
        "c15": {
            "enabled": c15.get("enabled"),
            "live": c15.get("live"),
            "authed": bool(STATE.auth_ok),
            "env": env,
            "blockReasons": c15.get("blockReasons") or {},
            "takeProfitHalted": c15.get("takeProfitHalted"),
        },
    }



async def _h_c15_backtest(p: dict) -> dict:
    """Replay the CURRENT (or supplied) 15m config over recorded ticks using
    the live entry gates. Read-heavy — run off the event loop."""
    import replay
    from config import merge_with_defaults as _merge
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = _merge(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or "production")
    return await asyncio.to_thread(replay.replay, cfg, env=env, since_days=since)


async def _h_c15_optimize(p: dict) -> dict:
    """Grid-search 15m parameters over recorded ticks with split/robustness
    diagnostics. Read-heavy — run off the event loop."""
    import optimizer
    from config import merge_with_defaults as _merge
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = _merge(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or "production")
    grid = (p or {}).get("paramGrid")
    if not isinstance(grid, dict):
        grid = None
    min_trades = int((p or {}).get("minTrades") or 30)
    top_n = int((p or {}).get("topN") or 20)
    boot = int((p or {}).get("bootstrapSamples") or 200)
    base_profile_id = str((p or {}).get("baseProfileId") or "")
    base_profile_name = str((p or {}).get("baseProfileName") or "")
    return await asyncio.to_thread(
        optimizer.optimize_crypto15m,
        cfg,
        env=env,
        since_days=since,
        param_grid=grid,
        min_trades=min_trades,
        top_n=top_n,
        bootstrap_samples=boot,
        base_profile_id=base_profile_id,
        base_profile_name=base_profile_name,
    )


async def _h_main_optimize(p: dict) -> dict:
    """Grid-search main-engine gates over recorded whale/momentum signals."""
    import optimizer
    from config import merge_with_defaults as _merge
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = _merge(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or "production")
    grid = (p or {}).get("paramGrid")
    if not isinstance(grid, dict):
        grid = None
    min_trades = int((p or {}).get("minTrades") or 30)
    top_n = int((p or {}).get("topN") or 20)
    boot = int((p or {}).get("bootstrapSamples") or 200)
    source_mode = str((p or {}).get("sourceMode") or "whale")
    fixed_risk = float((p or {}).get("fixedRiskUsd") or 1.0)
    slip = float((p or {}).get("slippageCents") or 1.0)
    exec_model = str((p or {}).get("executionModel") or "recorded_ask")
    train_pct = float((p or {}).get("trainPct") or 0.6)
    validation_pct = float((p or {}).get("validationPct") or 0.2)
    walk_folds = int((p or {}).get("walkForwardFolds") or 4)
    base_profile_id = str((p or {}).get("baseProfileId") or "")
    base_profile_name = str((p or {}).get("baseProfileName") or "")
    categories = (p or {}).get("categories")
    common = dict(
        env=env,
        since_days=since,
        min_trades=min_trades,
        top_n=top_n,
        bootstrap_samples=boot,
        fixed_risk_usd=fixed_risk,
        slippage_cents=slip,
        execution_model=exec_model,
        train_pct=train_pct,
        validation_pct=validation_pct,
        walk_forward_folds=walk_folds,
        base_profile_id=base_profile_id,
        base_profile_name=base_profile_name,
    )
    if source_mode in {"category", "category_filter", "categories"}:
        cats = [str(c) for c in categories] if isinstance(categories, list) else None
        return await asyncio.to_thread(
            optimizer.optimize_main_category_filters,
            cfg,
            categories=cats,
            param_grid=grid,
            **common,
        )
    if source_mode == "whale":
        return await asyncio.to_thread(
            optimizer.optimize_main_whales,
            cfg,
            param_grid=grid,
            **common,
        )
    if source_mode == "momentum":
        return await asyncio.to_thread(
            optimizer.optimize_main_momentum,
            cfg,
            param_grid=grid,
            **common,
        )
    return await asyncio.to_thread(
        optimizer.optimize_main_engine,
        cfg,
        env=env,
        since_days=since,
        param_grid=grid,
        source_mode=source_mode,
        min_trades=min_trades,
        top_n=top_n,
        bootstrap_samples=boot,
        fixed_risk_usd=fixed_risk,
        slippage_cents=slip,
        execution_model=exec_model,
        train_pct=train_pct,
        validation_pct=validation_pct,
        walk_forward_folds=walk_folds,
        base_profile_id=base_profile_id,
        base_profile_name=base_profile_name,
    )



async def _h_main_backtest(p: dict) -> dict:
    """Replay recorded whale/momentum signals through the live should_trade
    gates with full analytics: rejection funnel, category/source breakdown,
    calibration, entry-price/resolution/liquidity buckets, event concentration,
    sizing comparison, execution models, and time analysis."""
    import main_replay
    from config import merge_with_defaults as _merge
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = _merge(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    slip = float((p or {}).get("slippageCents") or 1.0)
    fixed_risk = (p or {}).get("fixedRiskUsd")
    if fixed_risk is not None:
        fixed_risk = float(fixed_risk)
    exec_model = str((p or {}).get("executionModel") or "recorded_ask")
    return await asyncio.to_thread(
        main_replay.replay_main_engine, cfg,
        since_days=since, slippage_cents=slip,
        fixed_risk_usd=fixed_risk, execution_model=exec_model,
    )


async def _h_main_export_research(p: dict) -> dict:
    """Export main-engine research data as a ZIP file."""
    import main_replay
    import main_export
    from config import merge_with_defaults as _merge
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = _merge(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    result = await asyncio.to_thread(
        main_replay.replay_main_engine, cfg, since_days=since,
    )
    out_dir = str(db._data_dir() / "exports")
    zip_path = main_export.export_zip(result, cfg, out_dir)
    return {"ok": True, "path": zip_path}


async def _h_ai_experiment_export(p: dict) -> dict:
    """Export an optimizer result and AI research layer as a ZIP file."""
    import ai_research
    opt = (p or {}).get("optimization") or {}
    if not isinstance(opt, dict) or not opt:
        return {"ok": False, "message": "missing optimization result", "error": "missing optimization result"}
    out_dir = str(db._data_dir() / "exports")
    zip_path = await asyncio.to_thread(ai_research.export_experiment_zip, opt, out_dir)
    return {
        "ok": True,
        "message": "Exported AI experiment ZIP",
        "path": zip_path,
        "data": {"path": zip_path},
    }



async def _h_collection_stats(p: dict) -> dict:
    """Inventory of the passively collected research data — what the Backtest
    page's "view data" area shows. Counts + spans + a recent sample per
    stream, all cheap indexed queries."""
    def _q() -> dict:
        with db.get_db() as conn:
            c15 = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(observed_at) a,
                          MAX(observed_at) b
                   FROM crypto15m_signals WHERE kalshi_env='production'"""
            ).fetchone()
            ticks = conn.execute(
                "SELECT COUNT(*) FROM crypto15m_ticks WHERE kalshi_env='production'"
            ).fetchone()[0]
            recent_c15 = conn.execute(
                """SELECT ticker, asset, favorite, favorite_price, up_won,
                          resolved, close_time
                   FROM crypto15m_signals WHERE kalshi_env='production'
                   ORDER BY id DESC LIMIT 12"""
            ).fetchall()
            wh = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(created_at) a,
                          MAX(created_at) b FROM whale_trades"""
            ).fetchone()
            al = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(created_at) a,
                          MAX(created_at) b FROM alerts"""
            ).fetchone()
            cats = conn.execute(
                """SELECT category, COUNT(*) n FROM whale_trades
                   GROUP BY category ORDER BY n DESC LIMIT 6"""
            ).fetchall()
            recent_main = conn.execute(
                """SELECT ticker, category, taker_side, price, dollar_value,
                          outcome_correct, resolved, created_at
                   FROM whale_trades ORDER BY id DESC LIMIT 12"""
            ).fetchall()
            coinbase = conn.execute(
                """SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b
                   FROM coinbase_candles"""
            ).fetchone()
            coinbase_assets = conn.execute(
                """SELECT asset, timeframe_sec, COUNT(*) candles,
                          MIN(ts) firstAt, MAX(ts) lastAt
                   FROM coinbase_candles
                   GROUP BY asset, timeframe_sec
                   ORDER BY candles DESC, asset, timeframe_sec
                   LIMIT 12"""
            ).fetchall()
            perps = db.perp_collection_counts(conn)
        return {
            "c15": {
                "windows": int(c15["n"] or 0),
                "resolved": int(c15["r"] or 0),
                "ticks": int(ticks or 0),
                "firstAt": c15["a"], "lastAt": c15["b"],
                "recent": [dict(r) for r in recent_c15],
            },
            "main": {
                "whales": int(wh["n"] or 0), "whalesResolved": int(wh["r"] or 0),
                "alerts": int(al["n"] or 0), "alertsResolved": int(al["r"] or 0),
                "firstAt": wh["a"] or al["a"], "lastAt": wh["b"] or al["b"],
                "topCategories": [dict(r) for r in cats],
                "recent": [dict(r) for r in recent_main],
            },
            "coinbase": {
                "candles": int(coinbase["n"] or 0),
                "firstAt": coinbase["a"],
                "lastAt": coinbase["b"],
                "byAsset": [dict(r) for r in coinbase_assets],
            },
            "perps": perps,
            "collecting": {
                "c15": bool((STATE.cfg or {}).get("crypto15m_record_signals", True)),
                "main": True,
                "perps": bool((STATE.cfg or {}).get("perps_record_signals", False)),
            },
        }
    return await asyncio.to_thread(_q)


async def _h_historical_download_coinbase(p: dict) -> dict:
    import historical_data
    return await asyncio.to_thread(
        historical_data.download_coinbase_candles,
        product_id=str((p or {}).get("productId") or "BTC-USD"),
        asset=str((p or {}).get("asset") or "BTC"),
        days=int((p or {}).get("days") or 7),
        granularity_sec=int((p or {}).get("granularitySec") or 60),
    )


async def _h_historical_download_kalshi(p: dict) -> dict:
    import historical_data
    env = str((p or {}).get("env") or (STATE.cfg or {}).get("kalshi_env") or "production")
    return await asyncio.to_thread(
        historical_data.download_kalshi_market_candles,
        ticker=str((p or {}).get("ticker") or ""),
        series_ticker=str((p or {}).get("seriesTicker") or ""),
        asset=str((p or {}).get("asset") or "BTC"),
        env=env,
        days=int((p or {}).get("days") or 7),
        period_interval=int((p or {}).get("periodInterval") or 1),
        historical=bool((p or {}).get("historical")),
    )


async def _h_historical_validate(p: dict) -> dict:
    import historical_data
    env = str((p or {}).get("env") or "production")
    since_days = int((p or {}).get("sinceDays") or 3650)
    max_gap = int((p or {}).get("maxGapSeconds") or 90)
    def _q() -> dict:
        db.init_db()
        with db.get_db() as conn:
            return historical_data.validate_dataset(
                conn,
                env=env,
                since_days=since_days,
                max_gap_seconds=max_gap,
            )
    return await asyncio.to_thread(_q)



async def _h_c15_history(p: dict) -> dict:
    limit = min(500, int((p or {}).get("limit") or 200))
    env = trader.get_env()
    def _q():
        with db.get_db() as conn:
            rows = db.recent_crypto15m_resolved(conn, env, limit=limit)
        return {"rows": [crypto15m_trader._pos_to_js(r) for r in rows]}
    return await asyncio.to_thread(_q)



async def _h_export_research(p: dict) -> dict:
    """Dump the collected research data as CSVs the user can analyze anywhere.
    Written under data/exports/<stamp>/; the renderer reveals the folder."""
    import csv
    def _dump() -> dict:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(os.path.dirname(str(db.db_path())), "exports", stamp)
        os.makedirs(out_dir, exist_ok=True)
        tables = ["crypto15m_signals", "crypto15m_ticks", "coinbase_candles",
                  "crypto15m_positions", "whale_trades", "alerts"]
        files = []
        with db.get_db() as conn:
            for t in tables:
                rows = conn.execute(f"SELECT * FROM {t}").fetchall()
                path = os.path.join(out_dir, f"{t}.csv")
                with open(path, "w", newline="", encoding="utf-8") as f:
                    if rows:
                        w = csv.DictWriter(f, fieldnames=list(dict(rows[0]).keys()))
                        w.writeheader()
                        for r in rows:
                            w.writerow(dict(r))
                    else:
                        f.write("")
                files.append(path)
        return {"dir": out_dir, "files": files}
    return await asyncio.to_thread(_dump)


async def _h_perps_status(p: dict) -> dict:
    cfg = STATE.cfg or {}
    out = await asyncio.to_thread(perps_record.status, cfg)
    out["farmer"] = await asyncio.to_thread(perps_farmer.status, cfg)
    out["strategy"] = await asyncio.to_thread(perps_strategy.status, cfg)
    out["wallet"] = await perps_record.wallet(kalshi_auth.get_env())
    return out


async def _h_perps_farm_flatten(p: dict) -> dict:
    return await perps_farmer.flatten(STATE.cfg or {})


async def _h_perps_backtest(p: dict) -> dict:
    """Backtest the user's perps strategy over their recorded candles. Accepts
    a config patch (merged over current settings for the run only) so the
    Backtest page can replay presets/profiles without touching live config."""
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 14)
    return await asyncio.to_thread(perps_strategy.run_backtest, cfg, since)


async def _h_perps_strat_flatten(p: dict) -> dict:
    return await perps_strategy.flatten(STATE.cfg or {})


async def _h_perps_history(p: dict) -> dict:
    limit = min(500, int((p or {}).get("limit") or 100))
    env = kalshi_auth.get_env()
    def _q():
        with db.get_db() as conn:
            rows = db.recent_perp_positions(conn, env, limit=limit)
        for r in rows:
            for k in ("entry_usd_micro", "exit_usd_micro", "fees_usd_micro",
                      "funding_usd_micro", "pnl_usd_micro"):
                r[k.replace("_usd_micro", "Usd")] = (
                    r[k] / 1e6 if r.get(k) is not None else None)
                r.pop(k, None)
            r["contracts"] = (r.pop("count_cc") or 0) / 100
        return {"rows": rows}
    return await asyncio.to_thread(_q)


async def _h_perps_backfill(p: dict) -> dict:
    """Manual backfill re-run (idempotent upserts; concurrent-run guarded)."""
    _fire_and_forget(perps_record.backfill(STATE.cfg or {}))
    return {"ok": True, "state": dict(perps_record._backfill_state)}


_HANDLERS = {
    "ping": _h_ping,
    "crypto15m": _h_crypto15m,
    "crypto15mStatus": _h_crypto15mStatus,
    "tradingStatus": _h_trading_status,
    "c15Backtest": _h_c15_backtest,
    "c15Optimize": _h_c15_optimize,
    "mainBacktest": _h_main_backtest,
    "mainOptimize": _h_main_optimize,
    "mainExportResearch": _h_main_export_research,
    "aiExperimentExport": _h_ai_experiment_export,
    "collectionStats": _h_collection_stats,
    "historicalDownloadCoinbase": _h_historical_download_coinbase,
    "historicalDownloadKalshi": _h_historical_download_kalshi,
    "historicalValidate": _h_historical_validate,
    "perpsStatus": _h_perps_status,
    "perpsBackfill": _h_perps_backfill,
    "perpsFarmFlatten": _h_perps_farm_flatten,
    "perpsBacktest": _h_perps_backtest,
    "perpsStratFlatten": _h_perps_strat_flatten,
    "perpsHistory": _h_perps_history,
    "c15History": _h_c15_history,
    "exportResearch": _h_export_research,
    "kalshiMarketUrl": _h_kalshiMarketUrl,
    "setConfig": _h_setConfig,
    "setCredentials": _h_setCredentials,
    "clearCredentials": _h_clearCredentials,
    "credentialStatus": _h_credentialStatus,
    "testCredentials": _h_testCredentials,
    "account": _h_account,
    "pnlSeries": _h_pnlSeries,
    "positions": _h_positions,
    "signals": _h_signals,
    "scannerStats": _h_scannerStats,
    "cancelAllOpen": _h_cancelAllOpen,
    "flatten": _h_flatten,
    "runOnce": _h_runOnce,
    "pause": _h_pause,
    "shutdown": _h_shutdown,
    "botRuns": _h_botRuns,
    "factoryReset": _h_factoryReset,
}


async def _dispatch_request(req: dict) -> None:
    rid = req.get("id", "")
    method = req.get("method", "")
    params = req.get("params") or {}
    h = _HANDLERS.get(method)
    if not h:
        await respond_err(rid, f"unknown method: {method}")
        return
    try:
        result = await h(params)
        await respond_ok(rid, result)
    except Exception as e:
        logger.warning(
            f"RPC {method} failed: {e}\n{traceback.format_exc(limit=3)}"
        )
        await respond_err(rid, f"{type(e).__name__}: {e}")




async def _stdin_reader() -> None:
    loop = asyncio.get_event_loop()

    def _readline() -> str:
        return sys.stdin.readline()

    while True:
        line = await loop.run_in_executor(None, _readline)
        if not line:
            await asyncio.sleep(0.1)
            await _shutdown()
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        if req.get("type") != "rpc":
            continue
        t = asyncio.create_task(_dispatch_request(req))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


_shutting_down = False


async def _shutdown() -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    try:
        await _stop_loop()
    except Exception:
        pass
    try:
        if STATE.active_run_id:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
            STATE.active_run_id = 0
    except Exception:
        pass
    try:
        await kalshi_api.close_clients()
    except Exception:
        pass
    try:
        await crypto15m.close_clients()
    except Exception:
        pass
    try:
        await kalshi_ws.stop()
    except Exception:
        pass
    try:
        await perps_farmer.ensure_stopped()
    except Exception:
        pass
    try:
        await perps_ws.stop()
    except Exception:
        pass
    try:
        await kalshi_perps_api.close_clients()
    except Exception:
        pass
    try:
        await spot_ws.stop()
    except Exception:
        pass
    await emit_event("backend:shutdown", {})
    sys.stdout.flush()
    await asyncio.sleep(0.1)
    os._exit(0)




async def _main() -> None:
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    STATE.started_at = datetime.now(timezone.utc).isoformat()
    db.init_db()
    logger.info("Krypt Trader backend starting")

    active_env = STATE.cfg.get("kalshi_env", "demo")
    try:
        kalshi_auth.set_env(active_env)
    except Exception:
        pass
    try:
        if kalshi_auth.migrate_legacy_credentials(active_env):
            logger.info(f"Migrated legacy credentials → {active_env}")
    except Exception as e:
        logger.warning(f"legacy credential migration failed: {e}")

    if kalshi_auth.credentials_present(active_env):
        try:
            kalshi_auth.prime_credentials(sync_time=True)
            bal = await kalshi_api.get_balance()
            int(bal.get("balance", 0))
            STATE.auth_ok = True
            logger.info("Saved credentials verified")
        except Exception as e:
            logger.warning(f"saved-credential verify failed: {e}")
            STATE.auth_ok = False

    await emit_event("backend:ready", {"startedAt": STATE.started_at})
    await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})
    try:
        await emit_event(
            "credentials:changed", kalshi_auth.credentials_status_all(),
        )
    except Exception:
        pass

    if STATE.auth_ok:
        try:
            summary, changed = await trader.reconcile_positions_with_kalshi()
            if any(summary.values()):
                logger.info(f"Eager startup reconcile: {summary}")
            await emit_event("backend:reconciled", summary)
            for row in changed:
                await emit_event("position:update", _position_row_to_js(row))
        except Exception as e:
            logger.warning(f"eager startup reconcile failed: {e}")

    if STATE.auth_ok:
        try:
            cents, _ = await trader.refresh_balance(STATE.cfg, force=True)
            await _start_run_if_balance_known(kalshi_auth.get_env(), cents)
        except Exception as e:
            logger.warning(f"could not open bot_run: {e}")

    try:
        kalshi_ws.start(
            kalshi_auth.get_env(),
            on_fill=_on_ws_fill, on_lifecycle=_on_ws_lifecycle,
            on_trade=_on_ws_trade,
        )
    except Exception as e:
        logger.warning(f"kalshi_ws start failed (staying on REST): {e}")

    try:
        if STATE.cfg.get("crypto15m_spot_ws", True):
            kalshi_ws.set_cf_enabled(True)
            spot_ws.start()
    except Exception as e:
        logger.warning(f"spot feeds start failed (staying on REST spots): {e}")

    await _start_loop()
    try:
        await _stdin_reader()
    except Exception as e:
        logger.error(f"reader crashed: {e}")
    finally:
        await _shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
