from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import crypto15m
import db
from kalshi_api import (
    KalshiAPIError, cancel_order, fetch_market, find_order_by_client_id,
    get_balance,
    get_fills_for_order, get_order, get_orderbook, get_positions,
    place_limit_order,
)

# Series the 15m-crypto executor owns; the main reconcile must not import these
# as "external" (it tracks them in its own table — see reconcile import branch).
_CRYPTO15M_SERIES = {s["series"] for s in crypto15m.SERIES}
from kalshi_auth import ENV_LOCK, get_env

logger = logging.getLogger(__name__)



_balance_cache: dict[str, dict] = {}
# One-time diagnostic: log the real /portfolio/balance response shape on first
# success so we can confirm it's cash-only (no portfolio_value) and whether a
# resting order reduces it — the assumptions the account total / P&L rest on.
_balance_shape_logged = False


async def refresh_balance(cfg: dict, force: bool = False) -> tuple[int, int]:
    interval = float(cfg.get("balance_poll_interval", 60))
    loop = asyncio.get_event_loop()
    now = loop.time()
    # Read the env + cache under ENV_LOCK (so a credential test's temporary env
    # flip is waited out, never observed mid-flip), but do the fetch OUTSIDE the
    # lock — holding it across get_balance's full timeout/retry ladder blocked
    # every order placement behind a slow balance poll (worst case ~80s, exactly
    # while a stop-loss wanted out). The cache write re-checks the env so a flip
    # during the fetch can only ever DROP an update, not file it under the
    # wrong env (the balance-flicker bug the lock originally fixed).
    async with ENV_LOCK:
        env = get_env()
        cached = _balance_cache.get(env)
        if not force and cached and (now - cached["at"]) < interval:
            return cached["cents"], cached["portfolio_cents"]
    try:
        # pin_env: if a credential test flips the env mid-fetch, the request
        # ABORTS instead of returning the other account's balance (which the
        # restored-env write guard below could not distinguish).
        data = await get_balance(pin_env=env)
        # A malformed/empty response (no 'balance' key) must NOT be cached as
        # $0 — that poisons the cache and makes the displayed balance flash to
        # zero. Treat it like a failed fetch and keep the last-known value.
        if not isinstance(data, dict) or "balance" not in data:
            raise ValueError("balance missing from response")
        global _balance_shape_logged
        if not _balance_shape_logged:
            _balance_shape_logged = True
            logger.info(f"Kalshi /portfolio/balance response shape: {dict(data)}")
        cents = int(data.get("balance", 0))
        port = int(data.get("portfolio_value", 0))
        async with ENV_LOCK:
            if get_env() == env:
                _balance_cache[env] = {
                    "cents": cents, "portfolio_cents": port, "at": now,
                }
        return cents, port
    except Exception as e:
        logger.warning(f"balance fetch failed: {e}")
        cached = _balance_cache.get(env)
        return (cached["cents"], cached["portfolio_cents"]) if cached else (0, 0)


def cached_balance(env: str | None = None) -> dict | None:
    """Last successfully-fetched balance for `env` (or the active env) as
    {cents, portfolio_cents, at}, or None if none has succeeded yet this session.
    Display code reads this so a transient failed poll (or a cold cache right
    after an env switch / backend restart) can't flash the balance to $0."""
    return _balance_cache.get(env or get_env())




def _compute_position_usd(balance_usd: float, edge_pts: float, cfg: dict) -> float:
    if cfg.get("sizing_mode") == "fixed":
        return min(
            float(cfg.get("fixed_trade_usd", 5.0) or 0.0),
            float(cfg["hard_max_position_usd"]),
        )
    lo_e = float(cfg["sizing_base_edge"])
    hi_e = float(cfg["sizing_max_edge"])
    lo_f = float(cfg["min_size_fraction"])
    hi_f = float(cfg["max_size_fraction"])
    if edge_pts <= lo_e:
        frac = lo_f
    elif edge_pts >= hi_e:
        frac = hi_f
    else:
        t = (edge_pts - lo_e) / (hi_e - lo_e)
        frac = lo_f + t * (hi_f - lo_f)
    return min(balance_usd * frac, float(cfg["hard_max_position_usd"]))




def _best_cross_price_cents(orderbook: dict, side: str) -> Optional[int]:
    side = side.lower()
    opposing_bids = orderbook.get("no" if side == "yes" else "yes") or []
    if not opposing_bids:
        return None
    try:
        best = max(int(b[0]) for b in opposing_bids if b and b[0] is not None)
    except (ValueError, TypeError):
        return None
    cross = 100 - best
    return cross if 1 <= cross <= 99 else None


async def _compute_limit_price_cents(
    ticker: str, direction: str, signal_price_cents: int, cfg: dict
) -> int:
    direction = direction.lower()
    style = cfg.get("order_style", "limit_cross")
    if style == "market":
        # Cross at the user's max entry price, not a hardcoded 99¢. Returning 99
        # made every market-style order fail the entry-price band check below
        # (and risked overpaying) whenever max_entry_price_cents < 99.
        return max(1, min(99, int(cfg.get("max_entry_price_cents", 99) or 99)))
    try:
        book = await get_orderbook(ticker)
        cross = _best_cross_price_cents(book, direction)
        if cross is not None:
            if style == "limit_mid":
                our_bids = book.get(direction) or []
                if our_bids:
                    best_ours = max(
                        int(b[0]) for b in our_bids if b and b[0] is not None
                    )
                    return max(1, min(99, (cross + best_ours) // 2))
            return cross
    except Exception as e:
        logger.warning(f"orderbook fetch failed for {ticker}: {e}")
    fallback = signal_price_cents + int(cfg.get("cross_spread_fallback_offset", 2))
    return max(1, min(99, fallback))




def _signal_cost_cents(signal: dict, source: str) -> tuple[str, int]:
    if source == "whale":
        direction = (signal.get("taker_side") or "yes").lower()
        price_frac = float(signal.get("price") or 0.0)
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    if source == "convergence":
        direction = (signal.get("direction") or "yes").lower()
        price_frac = float(signal.get("price") or 0.5)
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    direction = (signal.get("direction") or "yes").lower()
    yes_frac = float(signal.get("price") or 0.0)
    yes_cents = max(1, min(99, int(round(yes_frac * 100))))
    if direction == "yes":
        return direction, yes_cents
    return direction, max(1, min(99, 100 - yes_cents))


def _compute_edge(signal: dict, source: str) -> float:
    conf = float(signal.get("confidence") or 0.0)
    if source == "whale":
        implied = float(signal.get("price") or 0.0) * 100
    elif source == "convergence":
        implied = float(signal.get("price") or 0.5) * 100
    else:
        direction = (signal.get("direction") or "yes").lower()
        yes = float(signal.get("price") or 0.0)
        implied = (yes if direction == "yes" else (1.0 - yes)) * 100
    return conf - implied


def _taker_fee_cents(price_cents: int) -> float:
    """Kalshi taker fee in cents per contract at `price_cents` — the continuous
    marginal rate 7·p·(1−p) (per-order round-up is negligible at the main
    engine's multi-contract sizes). ~1.75c at 50c, ~0.9c at 85c."""
    p = max(1, min(99, int(price_cents))) / 100.0
    return 7.0 * p * (1.0 - p)


def _net_edge(signal: dict, source: str, cfg: dict) -> float:
    """Signal edge with the taker fee subtracted (when fee_aware_edge is on),
    so the min_edge gates compare NET expectation — a "5pt" gross edge at 50c
    is really ~3.3pts after the ~1.75c fee."""
    edge = _compute_edge(signal, source)
    if cfg.get("fee_aware_edge", True):
        _, cost_cents = _signal_cost_cents(signal, source)
        edge -= _taker_fee_cents(cost_cents)
    return edge


def _days_until_close(close_time: str) -> Optional[float]:
    """Days from now until a market's close/resolution time (ISO8601). Returns None
    when the time is missing or unparseable, which callers treat as 'unknown' and
    do NOT gate on."""
    if not close_time:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            return (ct - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except ValueError:
            continue
    return None


def _enrich_close_time(conn, sig: dict) -> None:
    """Attach the market's close_time to a signal for the resolution-horizon gate.
    Whale/momentum signal rows don't carry it; the markets table does."""
    if sig.get("close_time"):
        return
    m = db.get_market(conn, sig.get("ticker") or "")
    if m:
        sig["close_time"] = m.get("close_time") or ""


def should_trade(signal: dict, source: str, cfg: dict) -> tuple[bool, str]:
    # The 15m-crypto executor owns its series (own table + booking); a main-engine
    # position on the same ticker would double-count cost in the account total and
    # book P&L twice at settlement. Mirror of the reconcile-import exclusion.
    if (signal.get("ticker") or "").split("-")[0] in _CRYPTO15M_SERIES:
        return False, "crypto15m series (owned by the 15m executor)"

    # "Secret Strategy" — pure gambling. Ignore every gate (confidence, edge,
    # category, price); each fresh signal just gets a flat random roll.
    if cfg.get("gambling_mode"):
        prob = float(cfg.get("gambling_trade_probability", 0.10) or 0.0)
        pct = int(round(prob * 100))
        if random.random() < prob:
            return True, f"\U0001F3B0 gambling: HIT ({pct}%)"
        return False, f"\U0001F3B0 gambling: no hit ({pct}%)"

    conf = float(signal.get("confidence") or 0.0)
    # Gates compare NET edge (taker fee subtracted when fee_aware_edge is on) —
    # a gross gate lets through trades whose whole edge goes to fees.
    edge = _net_edge(signal, source, cfg)
    edge_tag = "net edge" if cfg.get("fee_aware_edge", True) else "edge"

    if source == "whale":
        if not cfg.get("trade_whales", False):
            return False, "whales disabled"
        if conf < cfg["min_confidence_whale"]:
            return False, f"conf {conf:.1f} < {cfg['min_confidence_whale']}"
        if edge < cfg["min_edge_pts_whale"]:
            return False, f"{edge_tag} {edge:.1f} < {cfg['min_edge_pts_whale']}"
    elif source == "momentum":
        if not cfg.get("trade_momentum", False):
            return False, "momentum disabled"
        if conf < cfg["min_confidence_momentum"]:
            return False, f"conf {conf:.1f} < {cfg['min_confidence_momentum']}"
        if edge < cfg["min_edge_pts_momentum"]:
            return False, f"{edge_tag} {edge:.1f} < {cfg['min_edge_pts_momentum']}"
        sig_type = (signal.get("signal_type") or "")
        allowed = set(cfg.get("allowed_momentum_signal_types", []))
        if sig_type not in allowed:
            return False, f"signal_type {sig_type!r} not allowed"
    elif source == "convergence":
        if not cfg.get("trade_convergence", False):
            return False, "convergence disabled"
        if conf < cfg["min_confidence_whale"]:
            return False, f"conf {conf:.1f} < {cfg['min_confidence_whale']}"
        if edge < cfg["min_edge_pts_whale"]:
            return False, f"{edge_tag} {edge:.1f} < {cfg['min_edge_pts_whale']}"

    cat = (signal.get("category") or "").lower()
    allowed_cats = cfg.get("allowed_categories")
    if allowed_cats is not None:
        if not allowed_cats:
            return False, "no categories enabled"
        if cat not in {c.lower() for c in allowed_cats}:
            return False, f"category {cat!r} not in allowed set"

    src_key = {
        "whale": "allowed_whale_categories",
        "convergence": "allowed_whale_categories",
        "momentum": "allowed_momentum_categories",
    }.get(source)
    src_cats = cfg.get(src_key) if src_key else None
    if src_cats is not None:
        if not src_cats:
            return False, f"no {source} categories enabled"
        if cat not in {c.lower() for c in src_cats}:
            return False, f"category {cat!r} not in {source} set"

    # Liquidity floor: thin markets mean wide spreads and adverse fills — the
    # signal rows already carry the market's volume, so gate on it before
    # paying to find out. Unknown volume fails OPEN (0 / missing).
    min_vol = float(cfg.get("min_market_volume", 0) or 0)
    if min_vol > 0:
        vol = 0.0
        for k in ("market_volume", "volume_24h"):
            v = signal.get(k)
            if v not in (None, ""):
                try:
                    vol = max(vol, float(v))
                except (TypeError, ValueError):
                    pass
        if 0 < vol < min_vol:
            return False, f"market volume {vol:.0f} < {min_vol:.0f}"

    _, cost_cents = _signal_cost_cents(signal, source)
    if cost_cents < cfg["min_entry_price_cents"]:
        return False, f"entry {cost_cents}c < {cfg['min_entry_price_cents']}c"
    if cost_cents > cfg["max_entry_price_cents"]:
        return False, f"entry {cost_cents}c > {cfg['max_entry_price_cents']}c"

    # Resolution-horizon cap: skip markets that won't resolve for a long time
    # (e.g. multi-month politics markets tie up capital). 0 = off. close_time is
    # enriched onto the signal in scan_for_trades; if it's missing/unparseable we
    # don't gate (fail-open — we'd rather trade than wrongly block on bad data).
    max_res_days = int(cfg.get("max_resolution_days", 0) or 0)
    if max_res_days > 0:
        days = _days_until_close(signal.get("close_time") or "")
        if days is not None and days > max_res_days:
            return False, f"resolves in ~{days:.0f}d > max {max_res_days}d"
    return True, "ok"




def _today_pnl_balance_delta(env: str, offset_min: int = 0) -> float | None:
    with db.get_db() as conn:
        first_today = db.first_snapshot_of_today(conn, env, offset_min)
        if not first_today:
            return None
        latest = db.latest_snapshot(conn, env)
    if not latest:
        return None
    today_baseline = float(first_today["total_usd"] or 0.0)
    today_total = float(latest["total_usd"] or 0.0)
    return today_total - today_baseline


# A daily-limit breach must PERSIST before it gates. The balance heartbeat
# reads cash and portfolio value at different moments, so around every
# settlement the payout is in flight between ledgers for a minute or two
# and the account total transiently reads low — observed live 2026-07-03:
# "today pnl=$-9.67, limit=$-1.45" fired inside the 2-minute window between
# the portfolio zeroing (00:16:03Z) and the cash payout landing (00:18:13Z)
# on an account that was UP on the day, blocking entries after EVERY
# settlement. A genuine drawdown keeps breaching and gates ~3 minutes
# later; a money-in-flight dip self-heals first.
_DAY_RISK_PERSIST_SEC = 180.0
# (env, kind) -> wall-clock (unix seconds) first-breach. Keyed by env so a
# demo<->production switch can't transfer or destroy a live breach streak.
# None = known-cleared; a MISSING key = unknown (fresh process), which is when
# the persisted copy in db.risk_state is consulted — the in-memory dict alone
# meant any backend restart mid-breach re-enabled both engines for another
# 180s while the account sat past its daily limit (and restarting the app is
# exactly what a user does after a losing streak).
_day_risk_breach: dict = {}


def _breach_persists(env: str, kind: str, breached: bool) -> bool:
    now = time.time()
    key = (env, kind)
    if not breached:
        # Clear the persisted latch too, but only on a transition (or on the
        # first observation after boot) so the hot path stays DB-free.
        if key not in _day_risk_breach or _day_risk_breach[key] is not None:
            _day_risk_breach[key] = None
            try:
                with db.get_db() as conn:
                    db.set_risk_breach_start(conn, env, kind, None)
            except Exception:
                pass
        return False
    first = _day_risk_breach.get(key)
    if first is None:
        persisted = None
        if key not in _day_risk_breach:
            # Fresh process observing an ongoing breach — restore the
            # persisted first-breach stamp instead of restarting the window.
            try:
                with db.get_db() as conn:
                    persisted = db.get_risk_breach_start(conn, env, kind)
            except Exception:
                persisted = None
        first = float(persisted) if (persisted and persisted <= now) else now
        _day_risk_breach[key] = first
        if persisted != first:
            try:
                with db.get_db() as conn:
                    db.set_risk_breach_start(conn, env, kind, first)
            except Exception:
                pass
    return (now - first) >= _DAY_RISK_PERSIST_SEC


def _is_blocked_by_daily_risk(cfg: dict, env: str) -> tuple[bool, str]:
    offset = int(cfg.get("trading_timezone_offset_min", 0) or 0)
    pnl = _today_pnl_balance_delta(env, offset)
    if pnl is None:
        return False, ""

    # The balance-delta P&L values open positions at COST — a day of positions
    # bleeding toward zero shows $0 until settlement, so the stop-loss fired
    # only after the money was gone. Add the live mark-to-market of open
    # positions (marks written by the 30s reconcile) for the stop decision.
    try:
        with db.get_db() as conn:
            unrealized = db.open_unrealized_pnl_usd(conn, env)
    except Exception:
        unrealized = 0.0
    pnl_mtm = pnl + unrealized

    # Two stop limits — flat dollars and % of the day-start account total —
    # whichever is TIGHTER binds. The % limit is what makes one default fit a
    # $100 account (flat -$50 = half the bankroll) and a $5000 one (noise).
    sl_limits: list[float] = []
    sl = float(cfg.get("stop_loss_on_day", 0))
    if sl < 0:
        sl_limits.append(sl)
    sl_pct = float(cfg.get("stop_loss_on_day_pct", 0) or 0)
    if sl_pct > 0:
        try:
            with db.get_db() as conn:
                first_today = db.first_snapshot_of_today(conn, env, offset)
        except Exception:
            first_today = None
        if first_today:
            day_start = float(first_today["total_usd"] or 0.0)
            if day_start > 0:
                sl_limits.append(-sl_pct * day_start)
    limit = max(sl_limits) if sl_limits else None  # closest to zero = tighter
    sl_hit = _breach_persists(env, "sl", limit is not None and pnl_mtm <= limit)
    tp = float(cfg.get("take_profit_on_day", 0))
    tp_hit = _breach_persists(env, "tp", tp > 0 and pnl >= tp)
    if sl_hit:
        return True, (
            f"daily stop-loss hit (today pnl=${pnl:+.2f}, "
            f"open mark-to-market=${unrealized:+.2f}, limit=${limit:+.2f})"
        )
    if tp_hit:
        return True, f"daily take-profit hit (today pnl=${pnl:+.2f}, target=${tp:+.2f})"
    return False, ""


_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _is_blocked_by_trading_hours(cfg: dict) -> tuple[bool, str]:
    if not cfg.get("trading_hours_enabled"):
        return False, ""
    from datetime import datetime, timedelta, timezone
    offset_min = int(cfg.get("trading_timezone_offset_min", 0) or 0)
    local_now = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    days = [d.lower() for d in (cfg.get("trading_days") or [])]
    weekday_short = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][local_now.weekday()]
    if weekday_short not in days:
        return True, f"trading-hours: {weekday_short} not in active days"
    try:
        s_h, s_m = (int(x) for x in str(cfg.get("trading_hours_start", "00:00")).split(":"))
        e_h, e_m = (int(x) for x in str(cfg.get("trading_hours_end", "23:59")).split(":"))
    except (ValueError, AttributeError):
        return False, ""
    cur_min = local_now.hour * 60 + local_now.minute
    start_min = s_h * 60 + s_m
    end_min = e_h * 60 + e_m
    in_window = (
        (start_min <= end_min and start_min <= cur_min <= end_min)
        or (start_min > end_min and (cur_min >= start_min or cur_min <= end_min))
    )
    if not in_window:
        return True, (
            f"trading-hours: outside window "
            f"({local_now.strftime('%H:%M')} not in "
            f"{s_h:02d}:{s_m:02d}-{e_h:02d}:{e_m:02d})"
        )
    return False, ""




async def execute_signal(
    signal: dict, source: str, cfg: dict, balance_usd: float
) -> dict | None:
    direction, signal_cost_cents = _signal_cost_cents(signal, source)
    edge_pts = _compute_edge(signal, source)
    env = get_env()

    with db.get_db() as conn:
        open_count = db.count_open_bot_positions(conn, env)
        if open_count >= cfg["max_open_positions"]:
            logger.info(
                f"[skip] {signal['ticker']}: MAX_OPEN_POSITIONS "
                f"({open_count}/{cfg['max_open_positions']} open)"
            )
            return None
        if not cfg.get("unlimited_daily_new_positions"):
            today_count = db.count_new_positions_today(
                conn, env, int(cfg.get("trading_timezone_offset_min", 0) or 0))
            daily_cap = int(cfg["max_daily_new_positions"])
            if today_count >= daily_cap:
                logger.info(
                    f"[skip] {signal['ticker']}: MAX_DAILY_NEW_POSITIONS "
                    f"({today_count}/{daily_cap} today). "
                    f"Toggle 'Unlimited daily new positions' in Settings → "
                    f"Concurrency to disable this cap."
                )
                return None
        max_per_event = int(cfg["max_positions_per_event"])
        if db.count_positions_in_event(
            conn, signal.get("event_ticker") or "", env
        ) >= max_per_event:
            logger.info(
                f"[skip] {signal['ticker']}: per-event cap reached "
                f"({max_per_event} on {signal.get('event_ticker') or '?'})"
            )
            return None
        if db.exists_position_in_market(conn, signal["ticker"], direction, env):
            logger.info(f"[skip] {signal['ticker']}: market/side already open")
            return None
        exposure = db.current_total_exposure_usd(conn, env)
        filled_cost = db.open_filled_cost_usd(conn, env)

    # Account value = cash + cost basis of FILLED positions. Kalshi's `balance`
    # still includes the cash for resting/unfilled orders (it isn't held), so we
    # must NOT add committed notional into the bankroll — that double-counts and
    # inflates the cap (which would over-deploy). `exposure` (committed incl.
    # resting orders) is the RISK we limit to a fraction of that account, so each
    # in-flight order still consumes headroom and the cap binds within one cycle.
    total_bankroll = max(0.0, balance_usd) + max(0.0, filled_cost)
    target_usd = _compute_position_usd(balance_usd, edge_pts, cfg)
    max_exposure = total_bankroll * float(cfg["max_total_exposure_fraction"])
    target_usd = min(target_usd, max(0.0, max_exposure - exposure))
    reserve = total_bankroll * float(cfg["min_cash_reserve_fraction"])
    target_usd = min(target_usd, max(0.0, balance_usd - reserve))

    if target_usd < 1.0:
        logger.info(f"[skip] {signal['ticker']}: size ${target_usd:.2f} < $1")
        return None

    limit_cents = await _compute_limit_price_cents(
        signal["ticker"], direction, signal_cost_cents, cfg
    )
    if not (cfg["min_entry_price_cents"] <= limit_cents <= cfg["max_entry_price_cents"]):
        logger.info(
            f"[skip] {signal['ticker']}: order price {limit_cents}c outside band "
            f"[{cfg['min_entry_price_cents']},{cfg['max_entry_price_cents']}]c "
            f"(book moved since signal)"
        )
        return None
    # Slippage cap: limit_cross pays whatever the live book asks — on a thin or
    # fast-moving book that can be far past the price the signal's edge was
    # computed at, silently spending the edge before settlement risk even
    # starts. The edge gate was passed at signal_cost_cents; refuse to pay more
    # than a few cents beyond it. 0 = off.
    max_slip = int(cfg.get("max_entry_slippage_cents", 0) or 0)
    if max_slip > 0 and limit_cents > signal_cost_cents + max_slip:
        logger.info(
            f"[skip] {signal['ticker']}: order price {limit_cents}c > signal "
            f"{signal_cost_cents}c + {max_slip}c slippage cap (edge already spent)"
        )
        return None
    contracts = max(1, int(target_usd * 100 // limit_cents))
    expected_cost_usd = contracts * limit_cents / 100.0
    client_order_id = f"krypt-{source}-{signal['id']}-{uuid.uuid4().hex[:8]}"

    logger.info(
        f"[{source}] {signal['ticker']} {direction} x{contracts} @ {limit_cents}c "
        f"= ${expected_cost_usd:.2f} conf={signal.get('confidence',0):.1f} edge={edge_pts:.1f}"
    )

    row = {
        "signal_source": source,
        "signal_id": signal["id"],
        "ticker": signal["ticker"],
        "event_ticker": signal.get("event_ticker", ""),
        "title": signal.get("title", ""),
        "category": signal.get("category", ""),
        "direction": direction,
        "action": "buy",
        "target_contracts": contracts,
        "limit_price_cents": limit_cents,
        "filled_contracts": 0,
        "cost_usd": 0.0,
        "client_order_id": client_order_id,
        "kalshi_order_id": None,
        "status": "submitted",
        "confidence": signal.get("confidence", 0.0),
        "edge_pts": edge_pts,
        "signal_price": (signal.get("price") or 0.0) * 100,
        "balance_before_usd": balance_usd,
        "kalshi_env": env,
    }

    if not cfg.get("enable_trading"):
        logger.info(f"[skip] {signal['ticker']}: trading disabled (enable_trading off)")
        return None

    try:
        resp = await place_limit_order(
            ticker=signal["ticker"],
            side=direction,
            action="buy",
            count=contracts,
            price_cents=limit_cents,
            client_order_id=client_order_id,
        )
    except KalshiAPIError as e:
        # 4xx = Kalshi REJECTED the order (confirmed not placed). 5xx and
        # transport errors may have DELIVERED it — look the coid up before
        # booking an error, or the resting order becomes an invisible position
        # that fills hours later on a stale limit while the engine re-fires
        # the same signal (double position).
        #
        # EXCEPTION: a duplicate client_order_id rejection is NOT "not
        # placed" — the transport layer retries a timed-out POST with the
        # SAME coid, so "duplicate" means attempt 1 WAS delivered and the
        # order is live (the exact shape crypto15m's entry path guards
        # against). Same for the synthesized env-flip 409: attempt 1 may have
        # been delivered before the abort. Route both through the coid-lookup
        # recovery instead of booking a phantom 'error' row that hides a live
        # order from the exposure/open-count/dup-market caps.
        body_l = str(e.body).lower()
        maybe_delivered = (
            "duplicate" in body_l
            or ("client_order_id" in body_l and "exist" in body_l)
            or (e.status == 409 and "env_changed" in body_l)
        )
        if e.status and 400 <= int(e.status) < 500 and not maybe_delivered:
            row["status"] = "error"
            row["error"] = f"HTTP {e.status}: {str(e.body)[:200]}"
            logger.error(f"[ORDER-FAIL] {signal['ticker']}: {row['error']}")
            with db.get_db() as conn:
                pid = db.insert_bot_position(conn, row)
                db.log_event(conn, pid, "error", note=row["error"])
                return db.fetch_position_by_id(conn, pid)
        return await _book_lost_entry(row, signal, client_order_id, f"HTTP {e.status}")
    except Exception as e:
        return await _book_lost_entry(
            row, signal, client_order_id, f"{type(e).__name__}: {str(e)[:160]}"
        )

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    order_id = order.get("order_id") if isinstance(order, dict) else None
    row["kalshi_order_id"] = order_id

    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, row)
        db.log_event(
            conn, pid, "placed", kalshi_status=order.get("status") if isinstance(order, dict) else None,
            note=f"order_id={order_id}",
        )
        return db.fetch_position_by_id(conn, pid)


async def _book_lost_entry(
    row: dict, signal: dict, client_order_id: str, err: str
) -> Optional[dict]:
    """An entry POST raised but may have been delivered. Adopt the live order
    if the coid lookup finds it; book 'error' only on a confirmed miss. On an
    unconfirmable lookup, book 'submitted' with no order id — the poll loop's
    NULL-kid recovery resolves it rather than letting the signal re-fire.

    A confirmed miss requires looking in the env the order was SENT to: after
    an env flip the current credentials query the other account, where the
    order can never appear — that's an unconfirmable lookup, not a miss. The
    row stays 'submitted' and the env-scoped poll resolves it when its env is
    active again."""
    found = None
    confirmed = False
    row_env = row.get("kalshi_env")
    if not row_env or row_env == get_env():
        try:
            found = await find_order_by_client_id(
                client_order_id, ticker=signal["ticker"], pin_env=row_env,
            )
            confirmed = True
        except Exception:
            pass
    if found:
        row["status"] = "submitted"
        row["kalshi_order_id"] = found.get("order_id")
        note = f"response lost ({err}); recovered via coid"
    elif confirmed:
        row["status"] = "error"
        row["error"] = err
        note = err
    else:
        row["status"] = "submitted"
        row["error"] = f"UNCONFIRMED: {err}"
        note = row["error"]
    logger.warning(f"[ORDER-RECOVER] {signal['ticker']}: {note}")
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, row)
        db.log_event(conn, pid, "error" if row["status"] == "error" else "placed", note=note)
        return db.fetch_position_by_id(conn, pid)




async def scan_for_trades(cfg: dict) -> list[dict]:
    global _last_scan_skip_log
    now_ts = time.time()

    def _skip_log(reason: str) -> None:
        last_cycle["skipReason"] = reason
        last_cycle["at"] = time.time()
        # Dedup on the stable prefix — reasons embed live values ("today
        # pnl=$-51.37") that change every snapshot, which defeated the dedup
        # and spammed thousands of identical lines per blocked day (rotating
        # real errors out of the log window).
        key = reason.split(" (", 1)[0]
        last = _last_scan_skip_log.get(key, 0)
        if now_ts - last < 60:
            return
        _last_scan_skip_log[key] = now_ts
        logger.info(f"[skip-cycle] {reason}")

    if not cfg.get("enable_trading"):
        _skip_log("enable_trading=false (master kill-switch off in Settings)")
        return []

    env = get_env()
    blocked, reason = _is_blocked_by_daily_risk(cfg, env)
    if blocked:
        _skip_log(reason)
        return []
    blocked, reason = _is_blocked_by_trading_hours(cfg)
    if blocked:
        _skip_log(reason)
        return []

    cents, _ = await refresh_balance(cfg)
    balance_usd = cents / 100.0
    if balance_usd < 5.0:
        _skip_log(f"balance ${balance_usd:.2f} too low")
        return []

    candidates: list[tuple[dict, str]] = []
    fetched_w = fetched_m = fetched_c = 0
    # Only pay for the per-signal market lookup (close_time) when the resolution
    # cap is actually enabled.
    gate_resolution = int(cfg.get("max_resolution_days", 0) or 0) > 0
    with db.get_db() as conn:
        if cfg.get("trade_whales"):
            seen = db.already_traded_signal_ids(conn, "whale", env)
            for sig in db.fetch_tradeable_whale_signals(
                conn,
                min_confidence=float(cfg["min_confidence_whale"]),
                max_age_sec=int(cfg["max_signal_age_sec"]),
                seen_ids=seen,
            ):
                fetched_w += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "whale"))
        if cfg.get("trade_momentum"):
            seen = db.already_traded_signal_ids(conn, "momentum", env)
            for sig in db.fetch_tradeable_momentum_signals(
                conn,
                min_confidence=float(cfg["min_confidence_momentum"]),
                max_age_sec=int(cfg["max_signal_age_sec"]),
                allowed_types=list(cfg.get("allowed_momentum_signal_types", [])),
                seen_ids=seen,
            ):
                fetched_m += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "momentum"))
        if cfg.get("trade_convergence"):
            import scanner as _scanner
            seen = db.already_traded_signal_ids(conn, "convergence", env)
            for sig in _scanner.build_convergence_signals(
                conn, max_signal_age_sec=int(cfg["max_signal_age_sec"]),
            ):
                if int(sig["id"]) in seen:
                    continue
                fetched_c += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "convergence"))

    if not candidates:
        bits = []
        if not cfg.get("trade_whales"):
            bits.append("whales OFF")
        if not cfg.get("trade_momentum"):
            bits.append("momentum OFF")
        if not bits:
            bits.append(
                f"no fresh signals match thresholds "
                f"(whale min_conf={cfg.get('min_confidence_whale')}, "
                f"momentum min_conf={cfg.get('min_confidence_momentum')}, "
                f"max_age={cfg.get('max_signal_age_sec')}s)"
            )
        _skip_log("no candidates: " + ", ".join(bits))
        return []

    candidates.sort(key=lambda c: _net_edge(c[0], c[1], cfg), reverse=True)
    inserted: list[dict] = []
    filter_counts: dict[str, int] = {}
    for sig, src in candidates:
        ok, why = should_trade(sig, src, cfg)
        if not ok:
            key = (int(sig.get("id") or 0), src)
            last = _last_filter_log.get(key, 0.0)
            if (now_ts - last) >= float(cfg.get("max_signal_age_sec", 120)):
                logger.info(f"[filter] {sig['ticker']} {src}: {why}")
                _last_filter_log[key] = now_ts
                _cap_log_dict(_last_filter_log)
            filter_counts[why] = filter_counts.get(why, 0) + 1
            continue
        try:
            row = await execute_signal(sig, src, cfg, balance_usd)
            if row:
                inserted.append(row)
                # Deduct the committed notional LOCALLY instead of forcing a
                # balance refresh per order (~0.3-0.5s each, serialized before
                # the next candidate — exactly when several signals fire
                # together and speed matters). Conservative: the real fill may
                # cost less. One forced refresh after the loop trues up.
                if row.get("status") != "error":
                    balance_usd -= (
                        int(row.get("target_contracts") or 0)
                        * int(row.get("limit_price_cents") or 0) / 100.0
                    )
        except Exception as e:
            logger.error(f"[exec-fail] {sig['ticker']} {src}: {e}", exc_info=True)
        if balance_usd < 5.0:
            logger.info("[halt-cycle] balance now below $5")
            break

    if inserted:
        await refresh_balance(cfg, force=True)

    last_cycle.update({
        "skipReason": None, "filterCounts": dict(filter_counts),
        "candidates": len(candidates), "placed": len(inserted), "at": time.time(),
    })
    if candidates:
        rejected = sum(filter_counts.values())
        logger.info(
            f"[trade-cycle] candidates={len(candidates)} "
            f"(whale={fetched_w}, momentum={fetched_m}, convergence={fetched_c}) "
            f"placed={len(inserted)} filtered={rejected}"
        )

    return inserted


_last_scan_skip_log: dict[str, float] = {}
# Last scan cycle's outcome — cycle-level skip reason (hours/daily-stop/...)
# and per-gate rejection counts. The "why isn't it trading" panel reads these;
# before this they were computed every cycle and thrown away.
last_cycle: dict = {"skipReason": None, "filterCounts": {}, "candidates": 0, "placed": 0, "at": None}

_last_filter_log: dict[tuple[int, str], float] = {}

_last_skip_import_log: dict[tuple[str, str], float] = {}


def _cap_log_dict(d: dict, cap: int = 2000, keep: int = 1500) -> None:
    """Bound the log-dedup dicts for multi-week runs: they gain a key per
    filtered signal / skipped import forever. Evict oldest-inserted first
    (dicts preserve insertion order; exact LRU isn't worth the bookkeeping
    for a dedup cache — worst case an evicted key logs once more)."""
    if len(d) > cap:
        for k in list(d)[: len(d) - keep]:
            d.pop(k, None)
# Consecutive reconciles a filled position has been absent from Kalshi's
# /portfolio/positions. Used to debounce orphan-closing against transient API
# blips and fresh-fill lag before declaring a position truly gone.
_orphan_miss_streak: dict[int, int] = {}




def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _order_fees_usd(order: dict) -> float:
    """Kalshi trading fees on an order, in dollars. The order object reports
    them as `taker_fees_dollars`/`maker_fees_dollars` (fp shape) or
    `taker_fees`/`maker_fees` (integer cents). fill_cost EXCLUDES fees, so
    P&L that ignores these overstates every round trip."""
    fees = _f(order.get("taker_fees_dollars")) + _f(order.get("maker_fees_dollars"))
    if fees:
        return fees
    return (_f(order.get("taker_fees")) + _f(order.get("maker_fees"))) / 100.0


def _parse_kalshi_order(order: dict) -> dict:
    if order.get("fill_count_fp") is not None or order.get("taker_fill_cost_dollars") is not None:
        filled = int(round(_f(order.get("fill_count_fp"))))
        cost_cents = int(round(
            (_f(order.get("taker_fill_cost_dollars")) + _f(order.get("maker_fill_cost_dollars"))) * 100
        ))
        place_count = int(round(_f(order.get("initial_count_fp"))))
        remaining = int(round(_f(order.get("remaining_count_fp"))))
    else:
        filled = int(order.get("taker_fill_count") or 0) + int(order.get("maker_fill_count") or 0)
        cost_cents = int(order.get("taker_fill_cost") or 0) + int(order.get("maker_fill_cost") or 0)
        place_count = int(order.get("place_count") or 0)
        remaining = int(order.get("remaining_count") or 0)
    status = (order.get("status") or "").lower()
    avg_cents = (cost_cents / filled) if filled else None
    return {
        "filled": filled,
        "cost_cents": cost_cents,
        "avg_cents": avg_cents,
        "status": status,
        "place_count": place_count,
        "remaining": remaining,
        "fees_usd": _order_fees_usd(order),
    }


def _parse_kalshi_fill(f: dict, default_side: str = "") -> dict:
    if f.get("count") is not None:
        count = int(_f(f.get("count")))
    else:
        count = int(round(_f(f.get("count_fp"))))
    side = str(f.get("side") or default_side).lower()
    if side == "yes":
        cents = f.get("yes_price")
        if cents in (None, "") and f.get("yes_price_dollars") is not None:
            cents = int(round(_f(f.get("yes_price_dollars")) * 100))
    else:
        cents = f.get("no_price")
        if cents in (None, "") and f.get("no_price_dollars") is not None:
            cents = int(round(_f(f.get("no_price_dollars")) * 100))
    try:
        price_cents: Optional[int] = int(cents) if cents not in (None, "") else None
    except (TypeError, ValueError):
        price_cents = None
    if price_cents is not None and not (1 <= price_cents <= 99):
        price_cents = None
    return {"count": count, "side": side, "price_cents": price_cents}


def _position_fees_usd(p: dict) -> float:
    """Trading fees a /portfolio/positions row reports for the market, in
    dollars (`fees_paid_dollars`, or legacy `fees_paid` cents)."""
    fees = _f(p.get("fees_paid_dollars"))
    if fees:
        return fees
    return _f(p.get("fees_paid")) / 100.0


def _parse_kalshi_position(p: dict) -> dict:
    try:
        qty_signed = float(p.get("position_fp") or 0)
    except (TypeError, ValueError):
        qty_signed = 0.0
    filled = int(abs(round(qty_signed)))
    side = "yes" if qty_signed > 0 else ("no" if qty_signed < 0 else "")
    try:
        exposure_usd = float(p.get("market_exposure_dollars") or 0)
    except (TypeError, ValueError):
        exposure_usd = 0.0
    try:
        fees_usd = float(p.get("fees_paid_dollars") or 0)
    except (TypeError, ValueError):
        fees_usd = 0.0
    cost_cents = int(round(exposure_usd * 100))
    avg_cents = (cost_cents / filled) if filled else None
    return {
        "filled": filled,
        "cost_cents": cost_cents,
        "avg_cents": avg_cents,
        "fees_usd": fees_usd,
        "side": side,
        "status": "executed",
        "place_count": filled,
        "remaining": 0,
    }


def _db_status_from_order(parsed: dict, target: int) -> str:
    s = parsed["status"]
    filled = parsed["filled"]
    if s == "executed" or (filled >= target and target > 0):
        return "filled"
    if s in ("canceled", "cancelled"):
        return "canceled" if filled == 0 else "partial"
    if filled > 0:
        return "partial"
    if s == "resting":
        return "submitted"
    return "submitted"


_poll_failures: dict[int, int] = {}
_POLL_FAILURE_THRESHOLD = 6

# Reentrancy guard: the UI's Refresh triggers runOnce('pollOrders') as its own
# asyncio task, concurrent with the main loop's poll. Two unserialized polls
# read the same expired 'submitted' row, both cancel it, and the loser's 404
# used to book 'gone' over the winner's carefully-confirmed outcome (a raced
# fill left terminal at 0 fills). One poll at a time; extras no-op.
_poll_orders_active = False


async def poll_open_orders(cfg: dict) -> list[dict]:
    global _poll_orders_active
    if _poll_orders_active:
        logger.debug("poll_open_orders already running; skipping concurrent run")
        return []
    _poll_orders_active = True
    try:
        return await _poll_open_orders_inner(cfg)
    finally:
        _poll_orders_active = False


async def _poll_open_orders_inner(cfg: dict) -> list[dict]:
    with db.get_db() as conn:
        # Current env only: rows from the other env would 404 against this
        # env's API and get killed as 'gone' while their orders live on.
        pending = db.get_pending_bot_positions(conn, get_env())
    if not pending:
        return []

    pos_by_key: dict[tuple[str, str], dict] = {}
    try:
        live_positions = await get_positions(limit=1000)
        for lp in live_positions:
            qty = 0.0
            for k in ("position_fp", "position"):
                v = lp.get(k)
                if v in (None, "", 0):
                    continue
                try:
                    f = float(v)
                    if f != 0:
                        qty = f
                        break
                except (TypeError, ValueError):
                    pass
            if qty == 0:
                continue
            tkr = lp.get("ticker") or ""
            side = "yes" if qty > 0 else "no"
            pos_by_key[(tkr, side)] = lp
    except Exception as e:
        logger.debug(f"poll: get_positions for rescue failed: {e}")

    updated: list[dict] = []

    def _bump_failure(pid: int) -> int:
        n = _poll_failures.get(pid, 0) + 1
        _poll_failures[pid] = n
        return n

    def _clear_failure(pid: int) -> None:
        _poll_failures.pop(pid, None)

    def _try_rescue_from_position_aggregate(pos: dict) -> dict | None:
        live_p = pos_by_key.get((pos["ticker"], pos["direction"]))
        if not live_p:
            return None
        with db.get_db() as conn:
            siblings = conn.execute(
                """SELECT id FROM bot_positions
                    WHERE ticker = ? AND direction = ?
                      AND kalshi_env = ?
                      AND resolved = 0
                      AND status NOT IN ('gone','canceled','expired','error','dry_run')""",
                (pos["ticker"], pos["direction"], pos["kalshi_env"]),
            ).fetchall()
        if len(siblings) != 1:
            return None
        qty = 0.0
        for k in ("position_fp", "position"):
            v = live_p.get(k)
            if v in (None, "", 0):
                continue
            try:
                f = float(v)
                if f != 0:
                    qty = abs(f)
                    break
            except (TypeError, ValueError):
                pass
        cost_cents = 0.0
        v = live_p.get("market_exposure")
        if v not in (None, "", 0):
            try:
                cost_cents = abs(float(v))
            except (TypeError, ValueError):
                pass
        if cost_cents == 0:
            v = live_p.get("market_exposure_dollars")
            if v not in (None, ""):
                try:
                    cost_cents = abs(float(v) * 100.0)
                except (TypeError, ValueError):
                    pass
        if qty <= 0:
            return None
        local_cost_usd = float(pos.get("cost_usd") or 0.0)
        local_avg_cents = pos.get("avg_fill_price_cents")
        new_cost_usd = (
            cost_cents / 100.0 if cost_cents > 0 else local_cost_usd
        )
        new_avg_cents = (
            (cost_cents / qty) if (qty and cost_cents > 0)
            else local_avg_cents
        )
        new_filled = int(round(qty))

        cur_filled = int(pos.get("filled_contracts") or 0)
        cur_cost_usd = float(pos.get("cost_usd") or 0.0)
        if (
            pos.get("status") == "filled"
            and cur_filled == new_filled
            and abs(cur_cost_usd - new_cost_usd) < 0.005
        ):
            return None

        fees_usd = _position_fees_usd(live_p)
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"], status="filled",
                filled_contracts=new_filled,
                cost_usd=new_cost_usd,
                avg_fill_price_cents=new_avg_cents,
                **({"fees_usd": fees_usd} if fees_usd > 0 else {}),
            )
            db.log_event(
                conn, pos["id"], "poll",
                note="rescued via /portfolio/positions",
            )
            return db.fetch_position_by_id(conn, pos["id"])

    for pos in pending:
        kid = pos.get("kalshi_order_id")
        if pos["status"] == "dry_run":
            continue

        if pos["status"] == "filled":
            row = _try_rescue_from_position_aggregate(pos)
            if row:
                _clear_failure(pos["id"])
                updated.append(row)
            else:
                _clear_failure(pos["id"])
            continue

        if not kid:
            # A lost-response entry (or odd order-response shape) left this row
            # without an order id. The order may be LIVE — look it up by our
            # client_order_id and adopt it before any give-up path runs.
            coid = pos.get("client_order_id")
            lookup_ok = False
            if coid:
                try:
                    found = await find_order_by_client_id(coid, ticker=pos.get("ticker") or "")
                    lookup_ok = True
                except Exception:
                    found = None
                if found and found.get("order_id"):
                    with db.get_db() as conn:
                        db.update_bot_position(
                            conn, pos["id"], kalshi_order_id=found.get("order_id"),
                        )
                        db.log_event(conn, pos["id"], "poll", note="adopted order via coid")
                    _clear_failure(pos["id"])
                    continue
            row = _try_rescue_from_position_aggregate(pos)
            if row:
                _clear_failure(pos["id"])
                updated.append(row)
                continue
            if coid and not lookup_ok:
                # The coid lookup FAILED (network/API) — that is not evidence
                # the order doesn't exist. Missing-kid rows exist precisely
                # because they were created during an outage; counting outage
                # cycles toward the give-up threshold 'gone'd live orders
                # after ~3 minutes of a router flap. Only a lookup that
                # SUCCEEDED and found nothing counts as a miss.
                logger.debug(
                    f"poll #{pos['id']}: coid lookup failed; not counting "
                    f"toward give-up"
                )
                continue
            n = _bump_failure(pos["id"])
            if n >= _POLL_FAILURE_THRESHOLD:
                with db.get_db() as conn:
                    db.update_bot_position(
                        conn, pos["id"], status="gone",
                        error=f"missing kalshi_order_id ({n} cycles)",
                    )
                    db.log_event(conn, pos["id"], "poll", note=f"no id × {n} -> gone")
                    row = db.fetch_position_by_id(conn, pos["id"])
                if row:
                    updated.append(row)
                _clear_failure(pos["id"])
            continue

        try:
            resp = await get_order(kid)
            order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
            parsed = _parse_kalshi_order(order)
        except KalshiAPIError as e:
            row = _try_rescue_from_position_aggregate(pos)
            if row:
                _clear_failure(pos["id"])
                updated.append(row)
                continue
            # Only a genuine 404 (Kalshi says "no such order") counts toward
            # the give-up threshold — 5xx/throttle responses are transient and
            # used to pre-charge the counter so the first real 404 fired the
            # give-up instantly instead of after 6 CONSECUTIVE 404s.
            if e.status != 404:
                logger.debug(f"order poll {kid}: HTTP {e.status} (transient; not counted)")
                continue
            n = _bump_failure(pos["id"])
            logger.debug(
                f"order poll {kid}: HTTP {e.status} (failure #{n})"
            )
            if e.status == 404 and n >= _POLL_FAILURE_THRESHOLD:
                with db.get_db() as conn:
                    db.update_bot_position(
                        conn, pos["id"], status="gone",
                        error=f"order 404 × {n}",
                    )
                    db.log_event(conn, pos["id"], "poll", note=f"404 × {n} -> gone")
                    row = db.fetch_position_by_id(conn, pos["id"])
                if row:
                    updated.append(row)
                _clear_failure(pos["id"])
            continue
        except Exception as e:
            # Transport/parse failure — not evidence of anything. Don't feed
            # the shared give-up counter (a 3-minute outage must not 'gone' a
            # live order).
            logger.warning(f"order poll {kid}: {e}")
            continue

        _clear_failure(pos["id"])

        db_status = _db_status_from_order(parsed, pos["target_contracts"])
        if db_status == "filled" and parsed["filled"] == 0:
            rescued = _try_rescue_from_position_aggregate(pos)
            if rescued:
                updated.append(rescued)
                continue
            db_status = "gone"

        with db.get_db() as conn:
            fields: dict = {
                "status": db_status,
                "filled_contracts": parsed["filled"],
            }
            # A partial fill whose remainder Kalshi CANCELED frees the unfilled
            # cash: collapse target_contracts to the filled qty so
            # current_total_exposure_usd stops valuing the dead remainder's
            # notional (target × limit) as committed capital — otherwise exposure
            # over-counts and needlessly blocks new entries until the position
            # resolves. A still-RESTING partial (kalshi status 'resting') keeps
            # its target, so its genuinely-held remainder stays counted.
            if db_status == "partial" and parsed["status"] in ("canceled", "cancelled"):
                fields["target_contracts"] = parsed["filled"]
            if parsed["avg_cents"] is not None:
                fields["avg_fill_price_cents"] = parsed["avg_cents"]
            if parsed["cost_cents"]:
                fields["cost_usd"] = parsed["cost_cents"] / 100.0
            if parsed.get("fees_usd"):
                fields["fees_usd"] = parsed["fees_usd"]
            db.update_bot_position(conn, pos["id"], **fields)
            db.log_event(
                conn, pos["id"], "poll",
                kalshi_status=parsed["status"],
                filled_contracts=parsed["filled"],
                fill_cost_cents=parsed["cost_cents"] or None,
            )
            row = db.fetch_position_by_id(conn, pos["id"])
        if row:
            updated.append(row)

        if (
            db_status == "submitted"
            and cfg.get("order_expiration_sec") is not None
            and parsed["filled"] == 0
            and kid
        ):
            with db.get_db() as conn:
                age = conn.execute(
                    "SELECT (julianday('now')-julianday(created_at))*86400 "
                    "FROM bot_positions WHERE id=?", (pos["id"],),
                ).fetchone()
            age_sec = float(age[0]) if age and age[0] is not None else 0.0
            if age_sec > float(cfg["order_expiration_sec"]):
                try:
                    await cancel_order(kid)
                except KalshiAPIError as e:
                    if e.status == 404:
                        # Cancel says the order is unknown — but a concurrent
                        # poll's cancel or a raced FILL produces the same 404.
                        # Confirm the fill state via get_order before booking a
                        # terminal status: booking 'gone' blind over a fill left
                        # a filled order terminal at 0 fills (and once the
                        # resolution pass flat-resolves it, unrecoverable).
                        final404 = None
                        for _ in range(3):
                            try:
                                resp2 = await get_order(kid)
                                final404 = _parse_kalshi_order(
                                    (resp2.get("order") if isinstance(resp2, dict) else resp2) or {}
                                )
                                break
                            except KalshiAPIError as e2:
                                if e2.status == 404:
                                    break  # order truly unknown -> 'gone' below
                                await asyncio.sleep(0.5)
                            except Exception:
                                await asyncio.sleep(0.5)
                        if final404 is not None and int(final404.get("filled") or 0) > 0:
                            # Fills exist — leave the row; the next poll's
                            # normal path books them.
                            logger.warning(
                                f"cancel {kid}: 404 but order shows "
                                f"{final404.get('filled')} fills; leaving row for next poll"
                            )
                            continue
                        with db.get_db() as conn:
                            db.update_bot_position(
                                conn, pos["id"], status="gone",
                                error=f"cancel 404: {str(e.body)[:100]}",
                            )
                            db.log_event(conn, pos["id"], "cancel", note="404 -> gone")
                            r = db.fetch_position_by_id(conn, pos["id"])
                        if r:
                            updated.append(r)
                    else:
                        # 5xx/throttle — the order may still be RESTING. Booking
                        # 'gone' here would untrack a live order; retry next poll.
                        logger.warning(f"cancel {kid}: HTTP {e.status}, will retry")
                    continue
                except Exception as e:
                    logger.warning(f"cancel exception {kid}: {e}")
                    continue
                # Cancel accepted — but a fill may have RACED it (the exact
                # shape that produced an off-book 15m settlement). Book
                # 'canceled' only from a CONFIRMED post-cancel zero-fill read;
                # if the re-read shows fills or fails, leave 'submitted' so the
                # next poll books the truth (a resolved 'canceled 0-fill' row
                # also blocks the reconcile import for this ticker, so a wrong
                # write here is unrecoverable).
                final = None
                for _ in range(3):
                    try:
                        resp2 = await get_order(kid)
                        final = _parse_kalshi_order(
                            (resp2.get("order") if isinstance(resp2, dict) else resp2) or {}
                        )
                        break
                    except Exception:
                        await asyncio.sleep(0.5)
                if final is None:
                    logger.warning(f"cancel {kid}: fill state unconfirmed, retrying next poll")
                    continue
                if int(final.get("filled") or 0) > 0:
                    continue  # next poll's normal path books the fills
                with db.get_db() as conn:
                    db.update_bot_position(
                        conn, pos["id"], status="canceled",
                        error=f"auto-canceled after {age_sec:.0f}s",
                    )
                    db.log_event(conn, pos["id"], "cancel", note=f"age {age_sec:.0f}s")
                    r = db.fetch_position_by_id(conn, pos["id"])
                if r:
                    updated.append(r)

    return updated




def _side_mark_cents(quote: dict | None, side: str) -> Optional[float]:
    """Current price of the held side, in cents, from a stored market quote.
    Mid of yes_bid/yes_ask when both are present, else last_price, else a single
    quote — clamped to [0, 100]. None when there's no usable quote yet."""
    if not quote:
        return None
    yb = float(quote.get("yes_bid") or 0)
    ya = float(quote.get("yes_ask") or 0)
    lp = float(quote.get("last_price") or 0)
    if yb > 0 and ya > 0:
        yes = (yb + ya) / 2.0
    elif lp > 0:
        yes = lp
    elif yb > 0:
        yes = yb
    elif ya > 0:
        yes = ya
    else:
        return None
    yes = max(0.0, min(1.0, yes))
    side_price = yes if side == "yes" else (1.0 - yes)
    return round(side_price * 100.0, 2)


def _market_yes_payout(market: dict | None) -> Optional[float]:
    if not market:
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return 1.0
    if result == "no":
        return 0.0
    status = (market.get("status") or "").lower()
    if status not in ("settled", "finalized", "determined"):
        return None
    sv = market.get("settlement_value_dollars")
    if sv is None:
        sv = market.get("settlement_value")
        if sv is not None:
            try:
                sv = float(sv) / 100.0
            except (TypeError, ValueError):
                sv = None
    if sv is None:
        return None
    try:
        f = float(sv)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


async def mark_resolved_positions(cfg: dict) -> list[dict]:
    with db.get_db() as conn:
        unresolved = db.get_unresolved_bot_positions(conn)
    if not unresolved:
        return []

    env = get_env()
    by_env = [p for p in unresolved if p["kalshi_env"] == env]
    if not by_env:
        return []

    updated: list[dict] = []
    needs_market: list[dict] = []
    for pos in by_env:
        filled = int(pos["filled_contracts"] or 0)
        if filled <= 0:
            # 'gone' is a GIVE-UP, not a confirmed terminal state — the order
            # may be resting live on Kalshi (poll give-ups fire during plain
            # outages). get_pending_bot_positions keeps such zero-cost rows
            # re-pollable for 24h; flat-resolving one earlier kills that
            # recovery window AND (via recent_resolved_position_exists) blocks
            # the reconcile import of a later fill for another 24h. Leave
            # 'gone' rows to the poll until the retention has elapsed.
            if (pos.get("status") or "") == "gone":
                try:
                    created = datetime.strptime(
                        str(pos.get("created_at") or ""), "%Y-%m-%d %H:%M:%S",
                    ).replace(tzinfo=timezone.utc)
                    age_sec = (datetime.now(timezone.utc) - created).total_seconds()
                except (TypeError, ValueError):
                    age_sec = float("inf")
                if age_sec < 86400:
                    continue
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pos["id"], resolved=1, outcome_correct=None,
                    pnl_usd=0.0, settlement_usd=0.0,
                )
                conn.execute(
                    "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                    (pos["id"],),
                )
                db.log_event(conn, pos["id"], "resolve", note="no fill -> closed")
                r = db.fetch_position_by_id(conn, pos["id"])
            if r:
                updated.append(r)
            continue
        needs_market.append(pos)

    if not needs_market:
        return updated

    market_cache: dict[str, dict | None] = {}
    for ticker in {p["ticker"] for p in needs_market}:
        try:
            m = await fetch_market(ticker)
        except Exception as e:
            logger.debug(f"fetch_market({ticker}) failed: {e}")
            m = None
        market_cache[ticker] = m
        if m:
            try:
                with db.get_db() as conn:
                    db.upsert_market(
                        conn,
                        {
                            "ticker": ticker,
                            "event_ticker": m.get("event_ticker", ""),
                            "title": m.get("title", ""),
                            "yes_sub_title": m.get("yes_sub_title", ""),
                            "status": m.get("status", ""),
                            "close_time": m.get("close_time", ""),
                            "volume": m.get("volume_fp", 0),
                            "volume_24h": m.get("volume_24h_fp", 0),
                            "open_interest": m.get("open_interest_fp", 0),
                            "yes_bid": m.get("yes_bid_dollars", 0),
                            "yes_ask": m.get("yes_ask_dollars", 0),
                            "last_price": m.get("last_price_dollars", 0),
                            "result": m.get("result", ""),
                            "settlement_value": m.get("settlement_value_dollars"),
                        },
                    )
            except Exception:
                pass

    for pos in needs_market:
        market = market_cache.get(pos["ticker"])
        yes_payout = _market_yes_payout(market)
        if yes_payout is None:
            continue

        direction = pos["direction"]
        filled = int(pos["filled_contracts"] or 0)
        cost_usd = float(pos["cost_usd"] or 0.0)
        fees_usd = float(pos.get("fees_usd") or 0.0)
        our_payout = yes_payout if direction == "yes" else (1.0 - yes_payout)
        settlement_usd = filled * our_payout
        # Realized P&L is NET of Kalshi trading fees — fill_cost excludes them,
        # so gross settlement−cost overstates every position by the fee paid.
        pnl_usd = settlement_usd - cost_usd - fees_usd

        max_settlement = float(filled)
        max_pnl = max_settlement - cost_usd - fees_usd
        min_pnl = -cost_usd - fees_usd
        if pnl_usd > max_pnl + 0.01 or pnl_usd < min_pnl - 0.01:
            logger.warning(
                f"[resolve-clamp] {pos['ticker']} pnl=${pnl_usd:+.2f} "
                f"outside physical bounds (filled={filled}, cost=${cost_usd:.2f}, "
                f"settlement=${settlement_usd:.2f}); clamping. "
                f"This usually means filled_contracts or cost_usd is corrupt — "
                f"run Reconcile Fills."
            )
            pnl_usd = max(min_pnl, min(max_pnl, pnl_usd))
            settlement_usd = pnl_usd + cost_usd + fees_usd

        if our_payout >= 0.99:
            correct: Optional[int] = 1
        elif our_payout <= 0.01:
            correct = 0
        elif pnl_usd > 0.05:
            correct = 1
        elif pnl_usd < -0.05:
            correct = 0
        else:
            correct = None

        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"], resolved=1, outcome_correct=correct,
                settlement_usd=settlement_usd, pnl_usd=pnl_usd,
            )
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                (pos["id"],),
            )
            label = "WIN" if correct == 1 else ("LOSS" if correct == 0 else "CLOSED")
            db.log_event(
                conn, pos["id"], "resolve",
                note=f"{label} pnl=${pnl_usd:+.2f} ({filled}c @ {our_payout:.2f})",
            )
            r = db.fetch_position_by_id(conn, pos["id"])
        if r:
            updated.append(r)
            logger.info(
                f"[resolve] {pos['ticker']} {direction}: {label} "
                f"pnl=${pnl_usd:+.2f} ({filled}c @ {our_payout:.2f}, "
                f"cost=${cost_usd:.2f})"
            )

    return updated




async def reconcile_fills_from_kalshi() -> dict:
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, kalshi_order_id, ticker, direction, status
               FROM bot_positions
               WHERE kalshi_env=? AND kalshi_order_id IS NOT NULL
                 AND status NOT IN ('dry_run','submitted')""",
            (env,),
        ).fetchall()
        targets = [dict(r) for r in rows]

    fills_reconciled = 0
    for pos in targets:
        kid = pos["kalshi_order_id"]
        try:
            fills = await get_fills_for_order(kid)
        except Exception as e:
            logger.debug(f"fills fetch failed for {kid}: {e}")
            continue

        ours = [f for f in fills if str(f.get("order_id") or "") == str(kid)]
        if not ours and fills:
            logger.warning(
                f"[reconcile] /portfolio/fills returned {len(fills)} fills "
                f"but none matched order_id={kid} — Kalshi may have rotated "
                f"old fills out. Skipping this position."
            )
            continue

        ours = [
            f for f in ours
            if str(f.get("action") or "buy").lower() == "buy"
        ]

        total_filled = 0
        total_cost_cents = 0
        for f in ours:
            fp = _parse_kalshi_fill(f, default_side=pos["direction"])
            n = fp["count"]
            if n <= 0:
                continue
            price_cents = fp["price_cents"]
            if price_cents is None:
                logger.warning(
                    f"[reconcile] unreadable price on fill order={kid} "
                    f"side={fp['side']}; skipping fill"
                )
                continue
            total_filled += n
            total_cost_cents += n * price_cents

        if total_filled == 0 and total_cost_cents == 0:
            continue

        avg_cents = (total_cost_cents / total_filled) if total_filled else None
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"],
                filled_contracts=total_filled,
                cost_usd=total_cost_cents / 100.0,
                avg_fill_price_cents=avg_cents,
            )
            db.log_event(
                conn, pos["id"], "reconcile-fills",
                filled_contracts=total_filled,
                fill_cost_cents=total_cost_cents,
            )
        fills_reconciled += 1

    with db.get_db() as conn:
        cleared = conn.execute(
            """UPDATE bot_positions
               SET resolved=0, outcome_correct=NULL,
                   pnl_usd=NULL, settlement_usd=NULL,
                   resolved_at=NULL
               WHERE kalshi_env=?
                 AND status IN ('filled','partial','expired','canceled','gone','error')
                 AND resolved=1""",
            (env,),
        ).rowcount
    resolved = await mark_resolved_positions({})
    return {
        "fills_reconciled": fills_reconciled,
        "pnl_cleared": int(cleared or 0),
        "pnl_recomputed": len(resolved),
    }


async def audit_pnl(limit: int = 200) -> dict:
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd,
                      fees_usd, pnl_usd, settlement_usd, outcome_correct
               FROM bot_positions
               WHERE kalshi_env=? AND resolved=1
               ORDER BY resolved_at DESC LIMIT ?""",
            (env, int(limit)),
        ).fetchall()
        rows = [dict(r) for r in rows]

    flagged: list[dict] = []
    sum_stored = 0.0
    sum_recompute = 0.0
    for r in rows:
        market = await fetch_market(r["ticker"])
        yes_payout = _market_yes_payout(market)
        if yes_payout is None:
            continue
        direction = r["direction"]
        filled = int(r["filled_contracts"] or 0)
        cost = float(r["cost_usd"] or 0)
        fees = float(r.get("fees_usd") or 0)
        our = yes_payout if direction == "yes" else (1.0 - yes_payout)
        settle = filled * our
        pnl_fresh = settle - cost - fees
        pnl_fresh = max(-cost - fees, min(filled - cost - fees, pnl_fresh))
        pnl_stored = float(r["pnl_usd"] or 0)
        sum_stored += pnl_stored
        sum_recompute += pnl_fresh
        if abs(pnl_stored - pnl_fresh) > 0.05:
            flagged.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "direction": direction,
                "filled": filled,
                "cost": round(cost, 2),
                "settlement_payout": round(our, 2),
                "stored_pnl": round(pnl_stored, 2),
                "fresh_pnl": round(pnl_fresh, 2),
                "delta": round(pnl_fresh - pnl_stored, 2),
            })
    return {
        "checked": len(rows),
        "flagged": len(flagged),
        "sum_stored_pnl": round(sum_stored, 2),
        "sum_recompute_pnl": round(sum_recompute, 2),
        "delta": round(sum_recompute - sum_stored, 2),
        "samples": flagged[:20],
    }


async def recompute_pnl_from_kalshi() -> dict:
    env = get_env()
    with db.get_db() as conn:
        cleared = conn.execute(
            """UPDATE bot_positions
               SET resolved=0, outcome_correct=NULL,
                   pnl_usd=NULL, settlement_usd=NULL,
                   resolved_at=NULL
               WHERE kalshi_env=?
                 AND status IN ('filled','partial','expired','canceled','gone','error')
                 AND resolved=1""",
            (env,),
        ).rowcount
    rows = await mark_resolved_positions({})
    return {"cleared": int(cleared or 0), "recomputed": len(rows)}




async def reconcile_positions_with_kalshi() -> tuple[dict, list[dict]]:
    summary = {
        "closed_orphans": 0,
        "imported_unknowns": 0,
        "rescued": 0,
        "resurrected": 0,
    }
    changed: list[dict] = []
    env = get_env()

    try:
        live = await get_positions(limit=1000)
    except Exception as e:
        logger.warning(f"reconcile: get_positions failed: {e}")
        return summary, changed

    def _signed_qty(p: dict) -> float:
        for k in ("position_fp", "position"):
            v = p.get(k)
            if v in (None, "", 0):
                continue
            try:
                f = float(v)
                if f != 0:
                    return f
            except (TypeError, ValueError):
                pass
        return 0.0

    def _cost_cents(p: dict) -> float:
        v = p.get("market_exposure")
        if v not in (None, "", 0):
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
        v = p.get("market_exposure_dollars")
        if v not in (None, ""):
            try:
                return abs(float(v) * 100.0)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _has_qty_key(p: dict) -> bool:
        return "position_fp" in p or "position" in p

    nonzero_count = sum(1 for p in live if _signed_qty(p) != 0)
    # A genuine field rename means NONE of the rows even expose a known qty key.
    # All-zero qty WITH the keys present is just a flat account (Kalshi returns a
    # zero-qty row for every market ever traded) — not a rename.
    qty_key_present = any(_has_qty_key(p) for p in live)
    if live and nonzero_count == 0 and not qty_key_present:
        sample_keys = list(live[0].keys())
        logger.warning(
            f"reconcile: Kalshi returned {len(live)} positions but NONE expose a "
            f"known qty field (position_fp/position) — likely a field-name change. "
            f"sample keys = {sample_keys}"
        )
    elif live:
        logger.debug(
            f"reconcile: Kalshi returned {len(live)} positions, "
            f"{nonzero_count} with non-zero qty"
        )

    live_by_key: dict[tuple[str, str], dict] = {}
    for p in live:
        qty = _signed_qty(p)
        if qty == 0:
            continue
        side = "yes" if qty > 0 else "no"
        live_by_key[(p.get("ticker") or "", side)] = p

    with db.get_db() as conn:
        all_local = conn.execute(
            "SELECT * FROM bot_positions WHERE kalshi_env = ?", (env,),
        ).fetchall()
    local_by_key: dict[tuple[str, str], list] = {}
    for r in all_local:
        pos = dict(r)
        if pos.get("resolved"):
            continue
        if pos.get("status") == "dry_run":
            continue
        local_by_key.setdefault(
            (pos["ticker"], pos["direction"]), [],
        ).append(pos)

    # Current per-ticker quotes (markets table is refreshed each cycle by the
    # resolution pass) → drives live mark-to-market P&L on open positions.
    with db.get_db() as conn:
        quote_by_ticker = db.get_market_quotes(
            conn, {t for (t, _s) in local_by_key},
        )

    for (ticker, side), live_p in live_by_key.items():
        rows = local_by_key.get((ticker, side), [])
        qty = abs(_signed_qty(live_p))
        cost_cents = _cost_cents(live_p)
        if qty <= 0:
            continue

        if rows:
            active = next(
                (r for r in rows if r["status"] in ("submitted", "partial", "filled")),
                None,
            )
            target = active or rows[0]
            was_terminal = target["status"] in (
                "gone", "canceled", "expired", "error",
            )
            local_cost_usd = float(target.get("cost_usd") or 0.0)
            local_avg_cents = target.get("avg_fill_price_cents")
            new_cost_usd = (
                cost_cents / 100.0 if cost_cents > 0 else local_cost_usd
            )
            new_avg_cents = (
                (cost_cents / qty) if (qty and cost_cents > 0)
                else local_avg_cents
            )
            new_filled = int(round(qty))

            # Live mark (held side's current price, in cents) → unrealized P&L.
            cur_mark = _side_mark_cents(quote_by_ticker.get(ticker), side)
            prev_mark = target.get("mark_price_cents")
            mark_moved = (
                cur_mark is not None
                and (prev_mark is None or abs(float(prev_mark) - cur_mark) >= 1.0)
            )

            cur_filled = int(target.get("filled_contracts") or 0)
            cur_cost_usd = float(target.get("cost_usd") or 0.0)

            # A partially-filled entry whose order is still WORKING (fills so
            # far < target) must NOT be flipped to 'filled': that frees the
            # resting remainder's committed notional from the exposure cap
            # (current_total_exposure_usd values filled rows at cost only),
            # drops the row from get_pending_bot_positions (so the poll stops
            # watching the live order — no fee/status updates, no
            # canceled-partial collapse), and removes it from cancel_all's
            # selector. Keep it 'partial' until the order reaches a terminal
            # state; the poll owns the order lifecycle.
            still_working = (
                not was_terminal
                and target["status"] in ("submitted", "partial")
                and new_filled < int(target.get("target_contracts") or 0)
            )
            if still_working:
                new_status = "partial" if new_filled > 0 else target["status"]
            else:
                new_status = "filled"

            # Skip a no-op write so the renderer isn't flooded with
            # position:update for unchanged rows — but DO write when the mark
            # moved ≥1c, so live P&L stays current.
            if (
                not was_terminal
                and target.get("status") == new_status
                and cur_filled == new_filled
                and abs(cur_cost_usd - new_cost_usd) < 0.005
                and not mark_moved
            ):
                continue

            live_fees_usd = _position_fees_usd(live_p)
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, target["id"], status=new_status,
                    filled_contracts=new_filled,
                    cost_usd=new_cost_usd,
                    avg_fill_price_cents=new_avg_cents,
                    error=None if was_terminal else target.get("error"),
                    **({"mark_price_cents": cur_mark}
                       if cur_mark is not None else {}),
                    **({"fees_usd": live_fees_usd}
                       if live_fees_usd > 0 else {}),
                )
                db.log_event(
                    conn, target["id"], "reconcile",
                    note=(
                        "resurrected from "
                        f"{target['status']} via /portfolio/positions"
                    ) if was_terminal else "rescued via /portfolio/positions",
                )
                row = db.fetch_position_by_id(conn, target["id"])
            if row:
                changed.append(row)
            if was_terminal:
                summary["resurrected"] += 1
                logger.info(
                    f"[reconcile] RESURRECTED #{target['id']} "
                    f"({ticker} {side}, was {target['status']}) "
                    f"qty={qty:.0f} cost=${cost_cents / 100:.2f}"
                )
            else:
                summary["rescued"] += 1
        else:
            # Skip positions owned by the 15m-crypto executor — it tracks them in
            # its own table. Importing them as "external" here would burn the main
            # engine's open-position slots and contaminate its win/loss/P&L stats.
            # Their cost is added to the account total in _build_account_snapshot.
            if ticker.split("-")[0] in _CRYPTO15M_SERIES:
                continue
            # Our OWN position wrongly resolved (flat orphan-close of an
            # active market, 0-fill give-up resolve over a raced fill) while
            # the contracts are still held? Re-link that row instead of
            # importing an 'external' duplicate: externals never count toward
            # max_open_positions, so every wrongful resolution would otherwise
            # convert into a PERMANENT cap exemption for our own money (and
            # the position would ride untracked for the 24h skip-import
            # window first). Flat-resolved (pnl $0, settlement $0, no
            # outcome) is the signature of a wrongful close — a real
            # settlement books a win's cash or a loss's negative pnl.
            with db.get_db() as conn:
                stale = db.find_flat_resolved_position(
                    conn, ticker, side, env, int(round(qty)),
                )
            if stale:
                with db.get_db() as conn:
                    db.update_bot_position(
                        conn, stale["id"], status="filled", resolved=0,
                        outcome_correct=None, pnl_usd=None,
                        settlement_usd=None, resolved_at=None, error=None,
                        filled_contracts=int(round(qty)),
                        cost_usd=(
                            cost_cents / 100.0 if cost_cents > 0
                            else float(stale.get("cost_usd") or 0.0)
                        ),
                        avg_fill_price_cents=(
                            (cost_cents / qty) if (qty and cost_cents > 0)
                            else stale.get("avg_fill_price_cents")
                        ),
                    )
                    db.log_event(
                        conn, stale["id"], "reconcile",
                        note=(
                            "re-linked wrongly-resolved row "
                            "(still held on Kalshi)"
                        ),
                    )
                    row = db.fetch_position_by_id(conn, stale["id"])
                if row:
                    changed.append(row)
                summary["resurrected"] += 1
                logger.info(
                    f"[reconcile] RE-LINKED #{stale['id']} {ticker} {side} "
                    f"(was resolved flat as {stale['status']}) "
                    f"qty={qty:.0f} cost=${cost_cents / 100:.2f}"
                )
                continue
            with db.get_db() as conn:
                if db.recent_resolved_position_exists(
                    conn, ticker, side, env,
                ):
                    key = (ticker, side)
                    last = _last_skip_import_log.get(key, 0.0)
                    if (time.time() - last) >= 600:
                        logger.info(
                            f"[reconcile] skip-import {ticker} {side}: "
                            f"already resolved within 24h "
                            f"(Kalshi cash-settlement still pending)"
                        )
                        _last_skip_import_log[key] = time.time()
                        _cap_log_dict(_last_skip_import_log)
                    continue
            now_ms = int(time.time() * 1000)
            signal_id = (
                abs(hash((ticker, side, now_ms))) % 2_000_000_000
            )
            client_id = f"ext-{ticker}-{side}-{now_ms}"
            try:
                with db.get_db() as conn:
                    new_id = db.insert_bot_position(conn, {
                        "signal_source": "external",
                        "signal_id": signal_id,
                        "ticker": ticker,
                        "event_ticker": live_p.get("event_ticker") or "",
                        "title": live_p.get("title") or ticker,
                        "category": live_p.get("category") or "",
                        "direction": side,
                        "action": "buy",
                        "target_contracts": int(round(qty)),
                        "limit_price_cents": (
                            int(round(cost_cents / qty)) if qty else 0
                        ),
                        "filled_contracts": int(round(qty)),
                        "avg_fill_price_cents": (
                            (cost_cents / qty) if qty else None
                        ),
                        "cost_usd": cost_cents / 100.0,
                        "client_order_id": client_id,
                        "kalshi_order_id": None,
                        "status": "filled",
                        "confidence": 0.0,
                        "edge_pts": 0.0,
                        "signal_price": 0.0,
                        "kalshi_env": env,
                    })
                    row = db.fetch_position_by_id(conn, new_id)
                if row:
                    changed.append(row)
                summary["imported_unknowns"] += 1
                logger.info(
                    f"[reconcile] imported external #{new_id} "
                    f"{ticker} {side} qty={qty:.0f} "
                    f"cost=${cost_cents / 100:.2f}"
                )
            except Exception as e:
                logger.warning(
                    f"[reconcile] import failed for {ticker} {side}: {e}",
                )

    # --- Orphan-closing: local "open" positions Kalshi no longer reports ---
    # A filled position absent from /portfolio/positions has settled or been
    # sold/closed outside our tracking. Without closing it, it lingers as "open"
    # forever — burning concurrency slots, inflating the open-count, and
    # double-counting its cost in the account total. (This is the 7-shown-vs-
    # 3-held drift, and why a manual Refresh appeared to do nothing.)
    # Only a real rename (no known qty key on any row) trips the fail-safe. A
    # flat account (keys present, all zero) must NOT skip orphan-closing, or
    # locally-"filled" positions sold/closed outside our tracking linger forever
    # — burning concurrency slots and skewing the daily-stop baseline.
    field_change_suspected = bool(live) and nonzero_count == 0 and not qty_key_present
    if not field_change_suspected:
        for (ticker, side), rows in local_by_key.items():
            still_held = (ticker, side) in live_by_key
            for r in rows:
                pid = r["id"]
                if still_held:
                    _orphan_miss_streak.pop(pid, None)
                    continue
                if r["status"] != "filled":
                    # resting/submitted orders aren't positions yet — leave them
                    # to poll_open_orders, which tracks the order lifecycle.
                    continue
                streak = _orphan_miss_streak.get(pid, 0) + 1
                _orphan_miss_streak[pid] = streak
                # ALWAYS require a confirming second consecutive miss. The old
                # "stale position + non-empty response → close on first miss"
                # shortcut closed real held positions on a single incomplete
                # /portfolio/positions read (a cursor page-shift skipping one
                # row while the rest looked healthy) — freeing cap slots the
                # engine refilled while the contracts were still held, with no
                # resurrection path once resolved. Systematic truncation is
                # handled at the source: get_positions raises
                # KalshiTruncatedResult at its page cap, aborting the whole
                # reconcile instead of feeding it a partial snapshot.
                if streak < 2:
                    continue

                filled = int(r["filled_contracts"] or 0)
                cost_usd = float(r["cost_usd"] or 0.0)
                fees_usd = float(r.get("fees_usd") or 0.0)
                is_c15 = ticker.split("-")[0] in _CRYPTO15M_SERIES
                # crypto15m externals carry their real P&L in their own table;
                # close them flat to avoid double-counting. Everything else
                # honors a market settlement if one has posted.
                yes_payout = None
                if not is_c15:
                    try:
                        market = await fetch_market(ticker)
                    except Exception:
                        market = None
                    yes_payout = _market_yes_payout(market)
                    if yes_payout is None and market is not None:
                        mstatus = (market.get("status") or "").lower()
                        if mstatus in ("closed", "settling", "pending", "determined"):
                            # Trading ended but the settlement value hasn't posted
                            # yet — Kalshi drops the position from the book first.
                            # Defer so the resolution pass assigns the real
                            # win/loss instead of closing a winner flat.
                            continue
                _orphan_miss_streak.pop(pid, None)

                if yes_payout is not None:
                    our_payout = yes_payout if side == "yes" else (1.0 - yes_payout)
                    settlement_usd = filled * our_payout
                    pnl_usd = settlement_usd - cost_usd - fees_usd
                    pnl_usd = max(-cost_usd - fees_usd,
                                  min(float(filled) - cost_usd - fees_usd, pnl_usd))
                    settlement_usd = pnl_usd + cost_usd + fees_usd
                    if our_payout >= 0.99:
                        correct: Optional[int] = 1
                    elif our_payout <= 0.01:
                        correct = 0
                    elif pnl_usd > 0.05:
                        correct = 1
                    elif pnl_usd < -0.05:
                        correct = 0
                    else:
                        correct = None
                    label = "WIN" if correct == 1 else ("LOSS" if correct == 0 else "FLAT")
                    note = f"orphan-resolved {label} pnl=${pnl_usd:+.2f} (gone from Kalshi)"
                else:
                    settlement_usd = 0.0
                    pnl_usd = 0.0
                    correct = None
                    note = "orphan-closed: no longer held on Kalshi"

                with db.get_db() as conn:
                    db.update_bot_position(
                        conn, pid, status="gone", resolved=1,
                        outcome_correct=correct,
                        settlement_usd=settlement_usd, pnl_usd=pnl_usd,
                    )
                    conn.execute(
                        "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                        (pid,),
                    )
                    db.log_event(conn, pid, "reconcile", note=note)
                    row = db.fetch_position_by_id(conn, pid)
                if row:
                    changed.append(row)
                summary["closed_orphans"] += 1
                logger.info(
                    f"[reconcile] CLOSED ORPHAN #{pid} {ticker} {side} "
                    f"({filled}c cost=${cost_usd:.2f}) — {note}"
                )

    return summary, changed




async def cancel_all_open() -> int:
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, kalshi_order_id, target_contracts FROM bot_positions
               WHERE status IN ('submitted','partial')
                 AND resolved=0 AND kalshi_env=?""",
            (get_env(),),
        ).fetchall()
    canceled = 0
    for r in rows:
        kid = r["kalshi_order_id"]
        if not kid:
            continue
        try:
            await cancel_order(kid)
        except Exception as e:
            logger.warning(f"cancel_all: {kid}: {e}")
            continue
        canceled += 1
        # Cancel accepted — but a fill may have RACED it. Same rule as the
        # poll's auto-cancel: book 'canceled' ONLY from a CONFIRMED post-cancel
        # zero-fill read. Writing 'canceled' blind over a partial fill drops
        # the held contracts out of the open-count cap instantly, and the
        # resolution pass would flat-resolve the 0-fill row — unrecoverable
        # (a resolved canceled 0-fill row also blocks the reconcile import
        # for this ticker for 24h, then feeds the cap-exempt external path).
        final = None
        for _ in range(3):
            try:
                resp2 = await get_order(kid)
                final = _parse_kalshi_order(
                    (resp2.get("order") if isinstance(resp2, dict) else resp2) or {}
                )
                break
            except Exception:
                await asyncio.sleep(0.5)
        if final is None:
            # Couldn't confirm the fill state — leave the row as-is
            # (submitted/partial) so the poll loop books the truth.
            logger.warning(
                f"cancel_all: {kid}: fill state unconfirmed after cancel; "
                f"leaving row for the poll loop"
            )
            continue
        with db.get_db() as conn:
            if int(final.get("filled") or 0) > 0:
                db_status = _db_status_from_order(
                    final, int(r["target_contracts"] or 0)
                )
                fields: dict = {
                    "status": db_status,
                    "filled_contracts": final["filled"],
                }
                # The canceled remainder's cash is freed — collapse the target
                # so exposure stops valuing the dead notional (same as poll).
                if db_status == "partial" and final["status"] in ("canceled", "cancelled"):
                    fields["target_contracts"] = final["filled"]
                if final["avg_cents"] is not None:
                    fields["avg_fill_price_cents"] = final["avg_cents"]
                if final["cost_cents"]:
                    fields["cost_usd"] = final["cost_cents"] / 100.0
                if final.get("fees_usd"):
                    fields["fees_usd"] = final["fees_usd"]
                db.update_bot_position(conn, r["id"], **fields)
                db.log_event(
                    conn, r["id"], "cancel",
                    kalshi_status=final["status"],
                    filled_contracts=final["filled"],
                    note="user cancel-all: raced fills kept",
                )
            else:
                db.update_bot_position(
                    conn, r["id"], status="canceled",
                    error="user cancel-all",
                )
                db.log_event(
                    conn, r["id"], "cancel", note="user cancel-all"
                )
    return canceled
