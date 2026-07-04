from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import crypto15m
import db
import kalshi_api
import kalshi_auth
import kalshi_ws
import rules as rules_engine
import trader

logger = logging.getLogger("crypto15m")

# Last tick's per-asset entry-block reasons — read by status() so the UI can
# answer "why isn't it trading" instead of the reasons dying in a local var.
_block_reasons: dict[str, str] = {}

# ───────── model-calibration drift monitor (the sniper's lifeline) ──────────
# The model edge exists ONLY while ">=97% predicted" keeps delivering >=~96%
# realized. Nothing else in the system watches that assumption — a regime
# change or degraded settlement feed would bleed quietly at high confidence.
# Rolling check over the last _CAL_WINDOW resolved windows where the model was
# sniper-confident in the entry band; if the Wilson lower bound of the realized
# hit rate drops below break-even, model-mode entries auto-pause (and resume
# with hysteresis once calibration recovers).
_CAL_CACHE: dict = {"at": 0.0, "ok": True, "n": 0, "rate": None, "lb": None}
_CAL_CHECK_SEC = 300.0
_CAL_WINDOW = 40          # rolling windows considered
_CAL_MIN_N = 20           # below this, not enough evidence to pause
# Bounds are on the WILSON LOWER BOUND, which sits well under the observed
# rate at these sample sizes: a PERFECT record's LB is n/(n+z²) — 0.886 at
# 21/21 and only 0.937 at 40/40 (the rolling-window ceiling). The old bars
# (pause 0.955 / resume 0.965) were ABOVE that ceiling, so the sniper
# auto-paused forever once n reached 20 even at a 100% hit rate (observed
# live 2026-07-03: "hit only 100% over the last 21 windows"). Correct
# framing: break-even at ~93c entries + fee is ~94% observed; 0.85 LB ≈
# observed ~95% at n=40, so a record at/under ~90% observed (a real
# money-loser) pauses while a healthy 97%+ record clears with room.
# Resume needs LB 0.90 ≈ observed ~99%+ at n=40 (hysteresis).
_CAL_PAUSE_LB = 0.85
_CAL_RESUME_LB = 0.90


def _wilson_lb(wins: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - rad) / denom)


def check_model_calibration(env: str) -> dict:
    now = time.time()
    if now - _CAL_CACHE["at"] < _CAL_CHECK_SEC:
        return dict(_CAL_CACHE)
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                """SELECT t.ticker, t.model_prob, s.up_won
                   FROM crypto15m_ticks t
                   JOIN crypto15m_signals s
                     ON s.ticker = t.ticker AND s.kalshi_env = t.kalshi_env
                   WHERE s.resolved = 1 AND s.up_won IS NOT NULL
                     AND t.kalshi_env = ? AND t.model_prob IS NOT NULL
                     AND t.mins_left <= 5 AND t.mins_left >= 0.5
                     AND (t.model_prob >= 0.97 OR t.model_prob <= 0.03)
                   ORDER BY t.observed_at""",
                (env,),
            ).fetchall()
    except Exception:
        return dict(_CAL_CACHE)
    # last qualifying tick per window = the prediction the sniper would act on
    last_by_ticker: dict = {}
    for r in rows:
        last_by_ticker[r["ticker"]] = r
    recent = list(last_by_ticker.values())[-_CAL_WINDOW:]
    n = len(recent)
    wins = sum(
        1 for r in recent
        if (float(r["model_prob"]) >= 0.5) == bool(r["up_won"])
    )
    lb = _wilson_lb(wins, n)
    prev_ok = bool(_CAL_CACHE.get("ok", True))
    if n < _CAL_MIN_N:
        ok = True  # not enough evidence to pause on
    elif prev_ok:
        ok = lb >= _CAL_PAUSE_LB
    else:
        ok = lb >= _CAL_RESUME_LB
    if prev_ok and not ok:
        logger.warning(
            f"[crypto15m] MODEL CALIBRATION DEGRADED: {wins}/{n} recent "
            f"sniper-band predictions hit (LB {lb:.3f} < {_CAL_PAUSE_LB}) — "
            f"auto-pausing model-mode entries"
        )
    elif not prev_ok and ok:
        logger.info(f"[crypto15m] model calibration recovered (LB {lb:.3f}) — resuming")
    _CAL_CACHE.update({
        "at": now, "ok": ok, "n": n,
        "rate": round(wins / n, 4) if n else None, "lb": round(lb, 4),
    })
    return dict(_CAL_CACHE)




def direction_for_favorite(favorite: str) -> str:
    return "yes" if favorite == "up" else "no"


def evaluate_rules(asset: dict, rules: list) -> tuple[bool, str]:
    """Evaluate the crypto entry rule-set against an asset snapshot (fields are
    snapshot keys like favoritePrice / macdHist / rsi / minsLeft / arbEdgeCents).
    Thin wrapper over the shared engine so crypto and the main bot evaluate
    rules identically."""
    return rules_engine.evaluate_rules(asset, rules)


def entry_limit_cents(entry_cost: float, entry_diff: float) -> int:
    cents = round((float(entry_cost) + float(entry_diff)) * 100)
    return max(1, min(99, int(cents)))


def maker_limit_cents(side: str, yes_bid, yes_ask, entry_cost: float) -> int:
    bid = None
    if side == "up":
        bid = yes_bid
    elif side == "down" and yes_ask:
        bid = 1.0 - float(yes_ask)
    if not bid or bid <= 0:
        bid = max(0.01, float(entry_cost) - 0.01)
    return max(1, min(99, int(round(float(bid) * 100))))


def side_prob_from_market(market: Optional[dict], direction: str) -> Optional[float]:
    if not market:
        return None
    yes_bid = crypto15m._price_dollars(market, "yes_bid")
    yes_ask = crypto15m._price_dollars(market, "yes_ask")
    up = crypto15m._mid_up(yes_bid, yes_ask, crypto15m._price_dollars(market, "last_price"))
    return up if direction == "yes" else (1.0 - up)


_WS_QUOTE_MAX_AGE_MS = 15_000.0


def _ws_quote_market(ticker: str) -> Optional[dict]:
    """The live WS ticker quote as a market-shaped dict (yes_bid/ask/last in
    dollars) usable by side_prob_from_market / _place_exit's price fallback —
    or None when the socket is down, the ticker isn't subscribed, or the quote
    is stale (> ~15s), in which case the caller REST-falls-back. The loop
    subscribes every HELD ticker to the WS `ticker` channel, so this replaces a
    per-position REST fetch_market on every 4s tick with a local read."""
    q = kalshi_ws.ticker_quote(ticker)
    if not q:
        return None
    ts = float(q.get("ts_ms") or 0)
    if ts <= 0 or (time.time() * 1000.0 - ts) > _WS_QUOTE_MAX_AGE_MS:
        return None
    out: dict = {}
    for src, dst in (("yes_bid_cents", "yes_bid_dollars"),
                     ("yes_ask_cents", "yes_ask_dollars"),
                     ("last_cents", "last_price_dollars")):
        v = q.get(src)
        out[dst] = (v / 100.0) if v is not None else None
    if not (out["yes_bid_dollars"] or out["yes_ask_dollars"] or out["last_price_dollars"]):
        return None
    return out


def _bought_side(asset: dict, cfg: dict) -> Optional[str]:
    """The side the executor would BUY for this asset — the favorite, its
    opposite in contrarian mode, or the settlement model's side in model
    (sniper) mode. None until the relevant signal exists."""
    mode = (cfg.get("crypto15m_direction_mode") or "favorite").lower()
    if mode == "model":
        mp = asset.get("modelProb")
        if mp is None:
            return None
        return "up" if float(mp) >= 0.5 else "down"
    fav = asset.get("favorite")
    if fav not in ("up", "down"):
        return None
    if mode == "contrarian":
        return "down" if fav == "up" else "up"
    return fav


def momentum_filters_ok(asset: dict, cfg: dict) -> tuple[bool, str]:
    """Direction-aware RSI/MACD confirmation layered on the built-in favorite
    gate: the underlying momentum must AGREE with the side being bought. An
    up-bet needs rsi>=min_rsi and macdHist>=min_macd; a down-bet needs the
    mirror (rsi<=100-min_rsi and macdHist<=-min_macd). Each threshold 0 = off.
    A filter that's on but whose indicator isn't populated (detector off or too
    few candles yet) rejects the entry, matching the rule builder's
    missing-field behaviour."""
    try:
        min_rsi = float(cfg.get("crypto15m_min_rsi", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_rsi = 0.0
    try:
        min_macd = float(cfg.get("crypto15m_min_macd_hist", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_macd = 0.0
    if min_rsi <= 0 and min_macd <= 0:
        return True, "ok"
    side = _bought_side(asset, cfg)
    if side is None:
        return False, "no favorite"
    up = side == "up"
    if min_rsi > 0:
        rsi = asset.get("rsi")
        if rsi is None:
            return False, "rsi unavailable (turn on Detect MACD/RSI)"
        rsi = float(rsi)
        if up and rsi < min_rsi:
            return False, f"rsi {rsi:.0f} < {min_rsi:.0f}"
        if (not up) and rsi > (100.0 - min_rsi):
            return False, f"rsi {rsi:.0f} > {100.0 - min_rsi:.0f}"
    if min_macd > 0:
        mh = asset.get("macdHist")
        if mh is None:
            return False, "macd unavailable (turn on Detect MACD/RSI)"
        mh = float(mh)
        if up and mh < min_macd:
            return False, f"macdHist {mh:.3f} < {min_macd:.3f}"
        if (not up) and mh > -min_macd:
            return False, f"macdHist {mh:.3f} > {-min_macd:.3f}"
    return True, "ok"


# Final-minute sniper gates (model mode only). 30+ of 60 prints locked means
# remaining settlement variance has collapsed ~87%; 0.9985 ≈ a 3-sigma
# distance between the projected average and the strike; 54 prints max leaves
# ~6s for the order round-trip before the close tick.
_FM_MIN_PRINTS = 30
_FM_MAX_PRINTS = 54
_FM_MIN_PROB = 0.9985


def should_enter(asset: dict, cfg: dict, *, has_open: bool, open_count: int) -> tuple[bool, str]:
    if not cfg.get("crypto15m_enabled"):
        return False, "disabled"
    if has_open:
        return False, "already open"
    max_conc = int(cfg.get("crypto15m_max_concurrent", len(crypto15m.SERIES)))
    if open_count >= max_conc:
        return False, "max concurrent"
    if not asset.get("hasMarket"):
        return False, "no market"
    if asset.get("favorite") not in ("up", "down"):
        return False, "no favorite"
    # The trading-hours gate is a HARD time control and always applies — even
    # with custom rules, which replace only the favorite/signal EDGE (the rule
    # vocabulary can't express a wrapping overnight window). Without this,
    # use_rules would silently trade 24h.
    if not crypto15m.hours_ok(cfg):
        return False, "outside trading hours"
    # Custom rule-set (rule builder): the user's composed conditions REPLACE the
    # built-in favorite/signal gate. The side bought still comes from
    # direction_mode; the rules decide WHEN to enter (they can gate on minsLeft
    # themselves, so the entry window is theirs to control).
    if cfg.get("crypto15m_use_rules"):
        # User rules replace the built-in gate but NOT the safety rails: a
        # naive rule-set could otherwise buy a 99c contract 30 seconds before
        # close (max-gamma, the -$7.12 lesson). Final minute stays model-mode-
        # only, and the executable ask must respect the entry cap.
        ml = asset.get("minsLeft")
        if ml is not None and float(ml) < 1.0:
            return False, "final minute (custom rules are blocked here — model mode only)"
        ok, why = evaluate_rules(asset, cfg.get("crypto15m_rules") or [])
        if not ok:
            return ok, why
        rside = _bought_side(asset, cfg)
        rask = asset.get("upAsk") if rside == "up" else (
            asset.get("downAsk") if rside == "down" else None
        )
        if rask and float(rask) > crypto15m._const(cfg, "entry_max"):
            return False, f"ask {float(rask)*100:.0f}c above the entry cap"
        return True, "ok"
    # Settlement sniper (model mode): its own gate, independent of the
    # favorite/signal machinery — enter only when the settlement model calls
    # the outcome near-certain AND the quote still leaves fee-adjusted edge.
    # Backtest on recorded ticks: 0.97 gate ≤5min → 97.7% win, +3.9c/ct net.
    if (cfg.get("crypto15m_direction_mode") or "favorite").lower() == "model":
        mp = asset.get("modelProb")
        if mp is None:
            return False, "model unavailable (needs indicators + spot feed)"
        mp = float(mp)
        p_side = mp if mp >= 0.5 else 1.0 - mp
        if cfg.get("crypto15m_model_autopause", True):
            cal = check_model_calibration(trader.get_env())
            if not cal.get("ok", True):
                return False, (
                    f"model calibration degraded ({cal.get('rate', 0):.0%} hit over "
                    f"last {cal.get('n', 0)} windows) — auto-paused"
                )
        ml = asset.get("minsLeft")
        final_minute = ml is not None and 0.0 < float(ml) < 1.0
        if final_minute:
            # The largest measured edge in our data (+22.8c/ct, 27/27 wins):
            # inside the last 60s the settlement average is being REALIZED
            # print-by-print while stale quotes linger — but only trade it
            # with real prints in hand, ~3-sigma certainty, and enough runway
            # for the order round-trip. Model mode only; other modes keep the
            # hard final-minute block (that block exists because BLIND
            # favorite-buying here is max-gamma — trade #250's −$7.12).
            if not cfg.get("crypto15m_model_final_minute", True):
                return False, "final minute (disabled)"
            prints = int(asset.get("settlePrints") or 0)
            if prints < _FM_MIN_PRINTS:
                return False, f"only {prints}/{_FM_MIN_PRINTS} settlement prints in"
            if prints > _FM_MAX_PRINTS:
                return False, "too close to the close for an order round-trip"
            if p_side < _FM_MIN_PROB:
                return False, f"model {p_side:.4f} < {_FM_MIN_PROB} (3-sigma gate)"
        elif not asset.get("inWindow"):
            return False, "outside entry window"
        else:
            min_p = float(cfg.get("crypto15m_model_min_prob", 0.97) or 0.97)
            if p_side < min_p:
                return False, f"model {p_side:.3f} < {min_p:.2f}"
        edge = asset.get("edgeNetCents")
        min_e = float(cfg.get("crypto15m_model_min_edge_cents", 2.0) or 0.0)
        if edge is None or float(edge) < min_e:
            return False, f"net edge {edge}c < {min_e:.1f}c"
        ask = asset.get("upAsk") if mp >= 0.5 else asset.get("downAsk")
        if not ask or not (0.0 < float(ask) <= crypto15m._const(cfg, "entry_max")):
            return False, "no executable ask under the entry cap"
        return True, "ok"
    if not asset.get("signal"):
        return False, "no signal"
    # Optional direction-aware RSI/MACD confirmation on top of the favorite gate.
    return momentum_filters_ok(asset, cfg)


def stop_loss_pct(cfg: dict) -> float:
    """Per-bet stop-loss as a FRACTION of the entry cost (`crypto15m_stop_loss_pct`,
    stored 0..1). 0 = off. E.g. 0.20 stops out once a position is down 20% from
    what it cost. Independent of the cents/price stop (`exit_threshold`)."""
    try:
        return max(0.0, min(1.0, float(cfg.get("crypto15m_stop_loss_pct", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def should_stop_loss(position: dict, side_prob: Optional[float], cfg: dict) -> bool:
    if side_prob is None:
        return False
    if position.get("status") != "filled":
        return False
    if int(position.get("filled_contracts") or 0) <= 0:
        return False
    # Cents/price stop: the held side has fallen to (or below) the exit price.
    if side_prob < crypto15m._const(cfg, "exit_threshold"):
        return True
    # Percent-of-entry stop: the position is down >= X% from its entry cost. 0=off.
    # A binary contract's current mark ≈ side_prob dollars, so current value is
    # filled*side_prob vs the cost_usd we paid.
    slp = stop_loss_pct(cfg)
    if slp > 0:
        filled = int(position.get("filled_contracts") or 0)
        cost = float(position.get("cost_usd") or 0.0)
        if cost > 0 and filled > 0:
            cur_value = filled * float(side_prob)
            if (cost - cur_value) / cost >= slp:
                return True
    return False


def take_profit_cents(cfg: dict) -> int:
    """Per-bet take-profit price in cents (`crypto15m_take_profit_cents`). 0 =
    off. Clamped to a valid sell price."""
    try:
        return max(0, min(99, int(cfg.get("crypto15m_take_profit_cents", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def should_take_profit(position: dict, side_prob: Optional[float], cfg: dict) -> bool:
    """Lock in a winner: sell once the held side's market probability reaches the
    per-bet take-profit price. 0 = off. (Set it ABOVE the entry price, or it
    would sell the instant a position fills.)"""
    if side_prob is None:
        return False
    if position.get("status") != "filled":
        return False
    if int(position.get("filled_contracts") or 0) <= 0:
        return False
    tp = take_profit_cents(cfg)
    if tp <= 0:
        return False
    return side_prob * 100.0 >= tp


def _stop_slippage(cfg: dict) -> int:
    """Cents below the bid to price a stop-loss SELL (user setting
    `crypto15m_stop_slippage_cents`), so it sweeps book depth and fills in a fast
    drop instead of resting at the top of a falling book. 0 = sell at the bid.
    Clamped 0..50."""
    try:
        return max(0, min(50, int(cfg.get("crypto15m_stop_slippage_cents", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def _clamp01(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:
        return 0.0
    return max(0.0, min(1.0, f))


def compute_entry_contracts(
    cfg: dict, *, entry_limit_cents: int, balance_usd: float, order_size: int
) -> int:
    price = max(0.01, int(entry_limit_cents) / 100.0)
    bal = max(0.0, float(balance_usd or 0.0))
    mode = (cfg.get("crypto15m_sizing_mode") or "fixed").lower()

    if mode == "balance_pct" and bal > 0:
        pct = _clamp01(cfg.get("crypto15m_balance_pct", 0.02))
        contracts = int((bal * pct) // price)
    else:
        contracts = max(1, int(order_size))

    max_loss = _clamp01(cfg.get("crypto15m_max_loss_pct", 0.0))
    if max_loss > 0 and bal > 0:
        contracts = min(contracts, int((bal * max_loss) // price))

    return max(0, contracts)


async def _bankroll_usd(cfg: dict, authed: bool) -> float:
    if authed:
        try:
            cents, _port = await trader.refresh_balance(cfg, force=False)
            if cents > 0:
                return cents / 100.0
        except Exception:
            pass
    return max(0.0, float(cfg.get("start_bankroll_usd", 0.0) or 0.0))




def _iso(s):
    if not s or not isinstance(s, str):
        return s
    s = s.strip()
    if not s:
        return s
    if s.endswith("Z") or "+" in s[10:]:
        return s.replace(" ", "T")
    return s.replace(" ", "T") + "Z"


def _pos_to_js(r: dict) -> dict:
    def _f(k):
        return float(r[k]) if r.get(k) is not None else None
    return {
        "id": int(r["id"]),
        "asset": r["asset"], "series": r.get("series") or "",
        "ticker": r.get("ticker") or "",
        "side": r.get("side") or "", "direction": r.get("direction") or "",
        "targetContracts": int(r.get("target_contracts") or 0),
        "filledContracts": int(r.get("filled_contracts") or 0),
        "entryLimitCents": int(r.get("entry_limit_cents") or 0),
        "avgEntryCents": _f("avg_entry_cents"),
        "costUsd": float(r.get("cost_usd") or 0.0),
        "status": r.get("status") or "",
        "exitReason": r.get("exit_reason"),
        "exitLimitCents": int(r["exit_limit_cents"]) if r.get("exit_limit_cents") is not None else None,
        "proceedsUsd": _f("proceeds_usd"),
        "feesUsd": float(r.get("fees_usd") or 0.0) + float(r.get("exit_fees_usd") or 0.0),
        "confidence": float(r.get("confidence") or 0.0),
        "entryDeltaUsd": _f("entry_delta_usd"),
        "outcomeCorrect": int(r["outcome_correct"]) if r.get("outcome_correct") is not None else None,
        "settlementUsd": _f("settlement_usd"),
        "pnlUsd": _f("pnl_usd"),
        "resolved": bool(r.get("resolved") or 0),
        "dryRun": bool(r.get("dry_run") or 0),
        "closeTime": _iso(r.get("close_time")) or "",
        "strategy": r.get("strategy") or "",
        "kalshiEnv": r.get("kalshi_env") or "demo",
        "createdAt": _iso(r.get("created_at")) or "",
        "resolvedAt": _iso(r.get("resolved_at")),
        "error": r.get("error"),
    }




async def _open_entry(a: dict, cfg: dict, env: str, balance_usd: float) -> Optional[dict]:
    mode = (cfg.get("crypto15m_direction_mode") or "favorite").lower()
    favorite = a.get("favorite")
    fav_price = float(a.get("favoritePrice") or 0.0)
    if mode == "contrarian":
        side = "down" if favorite == "up" else "up"
        entry_cost = max(0.01, 1.0 - fav_price)
        conf = entry_cost * 100.0
    elif mode == "model":
        # NOT `or 0.5`: modelProb of EXACTLY 0.0 (down near-certain — the
        # strongest snipe there is) is falsy, and the fallback flipped the
        # side to UP at 50/50 (real trade #346 bought UP with the model at
        # 0.0000). None can't reach here (should_enter gates it), but guard.
        mp_raw = a.get("modelProb")
        mp = float(mp_raw) if mp_raw is not None else 0.5
        side = "up" if mp >= 0.5 else "down"
        ask = a.get("upAsk") if side == "up" else a.get("downAsk")
        entry_cost = float(ask or a.get("entryCost") or fav_price)
        conf = (mp if side == "up" else 1.0 - mp) * 100.0
    else:
        side = favorite
        entry_cost = float(a.get("entryCost") or fav_price)
        conf = fav_price * 100.0
    direction = direction_for_favorite(side)
    style = (cfg.get("crypto15m_entry_style") or "maker").lower()
    if mode == "model":
        # The sniper's edge is minutes from expiry — a resting maker bid would
        # miss the window. Always take the ask.
        style = "taker"
    if style == "maker":
        limit_cents = maker_limit_cents(side, a.get("yesBid"), a.get("yesAsk"), entry_cost)
    else:
        limit_cents = entry_limit_cents(entry_cost, crypto15m._const(cfg, "entry_diff"))
    # HARD threshold floor (strict mode, favorite strategy only): the user's
    # entry_threshold applies to the price actually PAID, not just the
    # mid-derived favorite probability. On a thin/one-sided book the mid can say
    # "85c favorite" while the executable ask is far lower (the 71c NO case) —
    # a favorite that cheap isn't really that strong, so skip. A maker bid is
    # floored AT the threshold so a fill can never land below it either.
    # (Model mode has its own gates; flooring its limit at the favorite
    # threshold would be interference.)
    if (
        mode == "favorite"
        and not cfg.get("crypto15m_use_rules")
        and bool(cfg.get("crypto15m_strict_threshold", True))
    ):
        thr_cents = int(round(crypto15m._const(cfg, "entry_threshold") * 100))
        if int(round(entry_cost * 100)) < thr_cents:
            logger.info(
                f"[crypto15m] skip {a.get('asset')}: strict threshold — buy price "
                f"{entry_cost * 100:.0f}c below the {thr_cents}c floor "
                f"(thin book: mid says favorite, ask disagrees)"
            )
            return None
        limit_cents = max(limit_cents, min(99, thr_cents))
    order_size = compute_entry_contracts(
        cfg,
        entry_limit_cents=limit_cents,
        balance_usd=balance_usd,
        order_size=max(1, int(cfg.get("crypto15m_order_size", 1))),
    )
    # Aggregate 15m exposure cap: the assets' windows are one correlated crypto
    # bet, so total committed 15m cost is capped at a fraction of the bankroll
    # (cash + already-committed 15m cost). Trim the order to the remaining
    # budget rather than skipping outright.
    cap_pct = _clamp01(cfg.get("crypto15m_max_total_pct", 0.0))
    if cap_pct > 0 and balance_usd > 0:
        with db.get_db() as conn:
            committed = db.open_crypto15m_committed_usd(conn, env)
        budget = (balance_usd + committed) * cap_pct - committed
        price = max(0.01, limit_cents / 100.0)
        order_size = min(order_size, int(max(0.0, budget) // price))
        if order_size < 1:
            logger.info(
                f"[crypto15m] skip {a.get('asset')}: aggregate 15m exposure cap "
                f"(${committed:.2f} committed >= {cap_pct:.0%} of bankroll)"
            )
            return None
    if order_size < 1:
        logger.info(
            f"[crypto15m] skip {a.get('asset')}: sizing yielded 0 contracts "
            f"(risk budget too small at {limit_cents}c)"
        )
        return None
    # HARD wall-clock guard at ORDER time (not snapshot time — the snapshot
    # can be ~3-7s stale): never place an entry within 10s of the close. Real
    # trade #402 went out at T−2s, its cancel raced a maker fill, and 8
    # untracked contracts rode into settlement (−$6.56 off the books). Nothing
    # good happens ordering into the last seconds of the settlement window.
    close_epoch = crypto15m._parse_close_epoch(a.get("closeTime") or "")
    if close_epoch is not None and close_epoch - kalshi_auth.server_now() < 10.0:
        logger.info(f"[crypto15m] skip {a.get('asset')}: <10s to close at order time")
        return None
    ticker = a.get("ticker")
    coid = f"krypt-c15-{a['asset']}-{uuid.uuid4().hex[:8]}"
    # Stamp WHICH strategy opened this row so per-strategy P&L is computable
    # (before this, sniper results were indistinguishable from favorite-follow).
    ml = a.get("minsLeft")
    strategy = "rules" if cfg.get("crypto15m_use_rules") else mode
    if mode == "model" and ml is not None and float(ml) < 1.0:
        strategy = "model_fm"  # final-minute strike — its own risk profile
    row = {
        "asset": a["asset"], "series": a["series"], "ticker": ticker,
        "side": side, "direction": direction,
        "target_contracts": order_size, "entry_limit_cents": limit_cents,
        "client_order_id": coid, "close_time": a.get("closeTime") or "",
        "confidence": conf, "strategy": strategy,
        "entry_delta_usd": a.get("deltaUsd"), "kalshi_env": env,
    }

    # Insert the row BEFORE the POST. If the process dies between the POST and
    # the insert (Electron watchdog SIGKILL, crash, locked DB), the live order
    # would have NO row and — because this series is excluded from the main
    # reconcile — zero recovery path: real fills settling completely off-book.
    # A 'placing' row costs nothing and lets _poll_entry resolve the truth via
    # the coid on the next tick no matter where we died.
    row.update({"status": "placing", "dry_run": False})
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, row)

    try:
        resp = await kalshi_api.place_limit_order(
            ticker=ticker, side=direction, action="buy",
            count=order_size, price_cents=limit_cents, client_order_id=coid,
        )
    except Exception as e:
        # The POST may have reached Kalshi with the response lost (timeout, or a
        # retry rejected as a duplicate client_order_id). Look the order up by
        # our coid before booking 'error' — an unacknowledged LIVE order would
        # otherwise trade real money with no stop-loss, no take-profit and no
        # settlement booking.
        recovered, lookup_ok = await _lookup_lost_order(coid, ticker or "")
        with db.get_db() as conn:
            if isinstance(recovered, dict) and recovered.get("order_id"):
                db.update_crypto15m_position(
                    conn, pid, status="submitted",
                    kalshi_order_id=recovered.get("order_id"),
                )
                logger.warning(
                    f"[crypto15m] entry {a['asset']} recovered via client_order_id "
                    f"after order error: {e}"
                )
            elif lookup_ok:
                # Lookup succeeded and found nothing — the POST never landed.
                db.update_crypto15m_position(conn, pid, error=str(e)[:200])
                _mark_resolved(conn, pid, status="error")
                logger.error(f"[crypto15m] entry order failed {a['asset']}: {e}")
            else:
                # Couldn't check (network down) — leave 'placing'; _poll_entry's
                # placing branch resolves the truth on the next tick.
                db.update_crypto15m_position(conn, pid, error=f"UNCONFIRMED: {str(e)[:160]}")
            return db.fetch_crypto15m_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    with db.get_db() as conn:
        db.update_crypto15m_position(
            conn, pid, status="submitted",
            kalshi_order_id=order.get("order_id") if isinstance(order, dict) else None,
        )
        logger.info(f"[live] entry {a['asset']} {direction} x{order_size} @ {limit_cents}c")
        return db.fetch_crypto15m_by_id(conn, pid)




def _mark_resolved(conn, pid: int, **fields) -> None:
    db.update_crypto15m_position(conn, pid, resolved=1, **fields)
    conn.execute(
        "UPDATE crypto15m_positions SET resolved_at=datetime('now') WHERE id=?", (pid,)
    )


async def _poll_entry(pos: dict, cfg: dict) -> Optional[dict]:
    pid, kid = pos["id"], pos.get("kalshi_order_id")
    if not kid:
        # 'placing' (crash between insert and POST resolution) or a lost
        # response with an unconfirmable lookup: resolve the truth by coid.
        coid = pos.get("client_order_id")
        if not coid:
            return None
        found, confirmed = await _lookup_lost_order(coid, pos.get("ticker") or "")
        with db.get_db() as conn:
            if found and found.get("order_id"):
                db.update_crypto15m_position(
                    conn, pid, status="submitted",
                    kalshi_order_id=found.get("order_id"),
                )
                logger.warning(f"[crypto15m] adopted orphan entry via coid ({pos.get('asset')})")
            elif confirmed:
                # Confirmed absent — the order never existed; free the slot.
                _mark_resolved(conn, pid, status="canceled", exit_reason="never_placed")
            return db.fetch_crypto15m_by_id(conn, pid)
    parsed = None
    try:
        resp = await kalshi_api.get_order(kid)
        order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
        parsed = trader._parse_kalshi_order(order)
    except Exception as e:
        logger.debug(f"[crypto15m] entry poll {kid}: {e}")

    filled = int(parsed.get("filled") or 0) if parsed else 0
    remaining = int(parsed.get("remaining") or 0) if parsed else 0

    if filled > 0 and remaining <= 0:
        with db.get_db() as conn:
            db.update_crypto15m_position(
                conn, pid, status="filled",
                filled_contracts=filled,
                cost_usd=parsed["cost_cents"] / 100.0,
                avg_entry_cents=parsed["avg_cents"],
                fees_usd=float(parsed.get("fees_usd") or 0.0),
            )
            return db.fetch_crypto15m_by_id(conn, pid)

    if filled > 0 and filled != int(pos.get("filled_contracts") or 0):
        with db.get_db() as conn:
            db.update_crypto15m_position(
                conn, pid,
                filled_contracts=filled,
                cost_usd=parsed["cost_cents"] / 100.0,
                avg_entry_cents=parsed["avg_cents"],
                fees_usd=float(parsed.get("fees_usd") or 0.0),
            )

    if _entry_expired(pos, cfg):
        try:
            await kalshi_api.cancel_order(kid)
        except Exception:
            pass
        # Read the FINAL fill state with retries — the cancel can race a fill,
        # and a single stale/failed read here booked a maker-FILLED order as
        # "canceled 0/8" (real order a18e5ca1: 8 untracked contracts rode into
        # settlement, −$6.56 off the books). Never book canceled without a
        # CONFIRMED zero-fill read; on total read failure leave the row
        # 'submitted' so the next tick retries (a second cancel just 404s).
        final_filled, final_cost, final_avg, final_fees = filled, None, None, None
        read_ok = False
        for attempt in range(3):
            try:
                resp2 = await kalshi_api.get_order(kid)
                order2 = (resp2.get("order") if isinstance(resp2, dict) else resp2) or {}
                p2 = trader._parse_kalshi_order(order2)
                read_ok = True
                if int(p2.get("filled") or 0) >= final_filled:
                    final_filled = int(p2["filled"])
                    final_cost = p2["cost_cents"] / 100.0
                    final_avg = p2["avg_cents"]
                    final_fees = float(p2.get("fees_usd") or 0.0)
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5)
        with db.get_db() as conn:
            if final_filled > 0:
                upd = {"status": "filled", "filled_contracts": final_filled}
                if final_cost is not None:
                    upd["cost_usd"] = final_cost
                    upd["avg_entry_cents"] = final_avg
                    upd["fees_usd"] = final_fees
                db.update_crypto15m_position(conn, pid, **upd)
            elif read_ok:
                _mark_resolved(conn, pid, status="canceled", exit_reason="unfilled_expired")
            else:
                logger.warning(
                    f"[crypto15m] {pos.get('asset')} entry {kid}: cancel sent but "
                    f"final fill state UNCONFIRMED — keeping row open to retry"
                )
            return db.fetch_crypto15m_by_id(conn, pid)
    return None


def _entry_expired(pos: dict, cfg: dict) -> bool:
    if _pair_entry_stale(pos):
        return True
    close_epoch = crypto15m._parse_close_epoch(pos.get("close_time") or "")
    if close_epoch is None:
        return False
    lead = max(0.0, float(cfg.get("crypto15m_maker_cancel_min", 0.0) or 0.0)) * 60.0
    return kalshi_auth.server_now() >= close_epoch - lead


# A pair entry is a MARKETABLE limit (ask + 1c): if it hasn't filled within
# this long, the book moved away and it's now a resting bid that only fills
# when price comes DOWN through it — adverse by construction. Cancel and let
# a fresh gate pass re-enter on a genuine dip (partials keep their fills via
# the normal expiry path).
_PAIR_ENTRY_TTL_SEC = 30.0


def _pair_entry_stale(pos: dict) -> bool:
    if (pos.get("strategy") or "") != "pair":
        return False
    ca = str(pos.get("created_at") or "")
    try:
        dt = datetime.strptime(ca[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() > _PAIR_ENTRY_TTL_SEC


async def _place_exit(
    pos: dict, market: Optional[dict], cfg: dict, *, reason: str = "stop_loss"
) -> Optional[dict]:
    """Sell the held position at the best bid. Shared by the stop-loss and the
    per-bet take-profit; `reason` ('stop_loss' | 'take_profit') tags the exit leg
    so _chase_exit/_settle_if_closed preserve it and the UI can label it. A
    stop-loss prices `crypto15m_stop_slippage_cents` THROUGH the bid to sweep a
    falling book; a take-profit sells AT the bid (it's already a winner — no need
    to give up extra cents)."""
    pid, ticker, direction = pos["id"], pos["ticker"], pos["direction"]
    filled = int(pos.get("filled_contracts") or 0)
    slippage = _stop_slippage(cfg) if reason == "stop_loss" else 0
    exit_cents: Optional[int] = None
    try:
        book = await kalshi_api.get_orderbook(ticker)
        bids = book.get(direction) or []
        if bids:
            exit_cents = max(int(b[0]) for b in bids if b and b[0] is not None)
    except Exception:
        exit_cents = None
    if exit_cents is None:
        sp = side_prob_from_market(market, direction) or 0.0
        exit_cents = int(round(sp * 100)) - 2
    # Price `slippage` cents THROUGH the bid so a stop-loss sweeps depth and fills
    # instead of resting at the top of a falling book (0 for a take-profit).
    exit_cents = max(1, min(99, exit_cents - slippage))

    coid = f"krypt-c15x-{pos['asset']}-{uuid.uuid4().hex[:8]}"
    try:
        resp = await kalshi_api.place_limit_order(
            ticker=ticker, side=direction, action="sell",
            count=filled, price_cents=exit_cents, client_order_id=coid,
        )
    except Exception as e:
        # The POST may have been DELIVERED despite the exception (timeout after
        # delivery; the transport retry re-sends the same coid and Kalshi
        # rejects it as a duplicate). If we book only an error note, the next
        # tick re-fires the stop with a NEW coid alongside the live sell —
        # selling more than held on Kalshi's single book BUYS the opposite
        # side with real cash. Adopt-or-confirm before giving up.
        found, confirmed = await _lookup_lost_order(coid, ticker)
        with db.get_db() as conn:
            if found:
                db.update_crypto15m_position(
                    conn, pid, status="exiting", exit_reason=reason,
                    exit_client_order_id=coid,
                    exit_kalshi_order_id=found.get("order_id"),
                    exit_limit_cents=exit_cents,
                )
                logger.warning(f"[live] {reason} sell {pos['asset']}: response lost, recovered via coid")
            elif confirmed:
                # Lookup succeeded and no order exists — the POST really never
                # landed. Safe to leave 'filled' so the exit re-fires fresh.
                db.update_crypto15m_position(conn, pid, error=f"{reason} sell failed: {str(e)[:160]}")
            else:
                # Couldn't even check (network down). Park as 'exiting' with no
                # kid — _poll_exit's no-kid branch resolves the truth before
                # any second sell can be placed.
                db.update_crypto15m_position(
                    conn, pid, status="exiting", exit_reason=reason,
                    exit_client_order_id=coid,
                    error=f"{reason} sell UNCONFIRMED: {str(e)[:120]}",
                )
            return db.fetch_crypto15m_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    label = reason.upper().replace("_", "-")
    with db.get_db() as conn:
        db.update_crypto15m_position(
            conn, pid, status="exiting", exit_reason=reason,
            exit_client_order_id=coid,
            exit_kalshi_order_id=order.get("order_id") if isinstance(order, dict) else None,
            exit_limit_cents=exit_cents,
        )
        logger.info(f"[live] {label} sell {pos['asset']} x{filled} @ {exit_cents}c")
        return db.fetch_crypto15m_by_id(conn, pid)


async def _lookup_lost_order(coid: str, ticker: str) -> tuple[Optional[dict], bool]:
    """After a place-order exception, find out whether the POST actually
    landed. Returns (order_or_None, confirmed): confirmed=True means the
    lookup itself succeeded, so a None order is a REAL absence — only then is
    it safe to re-place without risking a double order."""
    try:
        found = await kalshi_api.find_order_by_client_id(coid, ticker=ticker)
        return found, True
    except Exception:
        return None, False


async def _poll_exit(pos: dict) -> Optional[dict]:
    pid, kid = pos["id"], pos.get("exit_kalshi_order_id")
    if not kid:
        # An exit whose POST response was lost parks here with the coid but no
        # order id (see _place_exit / _chase_exit). Resolve the truth before
        # anything else may sell again.
        coid = pos.get("exit_client_order_id")
        if not coid:
            return None
        found, confirmed = await _lookup_lost_order(coid, pos.get("ticker") or "")
        with db.get_db() as conn:
            if found:
                db.update_crypto15m_position(
                    conn, pid, exit_kalshi_order_id=found.get("order_id")
                )
            elif confirmed:
                # Confirmed absent — the sell never landed; go back to 'filled'
                # so the stop/TP logic re-fires a fresh exit next tick.
                db.update_crypto15m_position(conn, pid, status="filled")
        return None
    try:
        resp = await kalshi_api.get_order(kid)
        order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
        parsed = trader._parse_kalshi_order(order)
    except Exception:
        return None
    sold = int(parsed.get("filled") or 0)
    remaining = int(parsed.get("remaining") or 0)
    exit_fees = float(parsed.get("fees_usd") or 0.0)
    # Kalshi reports a SELL's taker/maker_fill_cost as the OFFSETTING-leg cost
    # basis (sold*(100-sell_price)), NOT the cash received. Actual cash proceeds =
    # face value (sold contracts * $1) minus that complement. (A BUY's cost_cents
    # IS the cost paid, so the entry path is unaffected — this is exit-only.)
    proceeds = sold - parsed["cost_cents"] / 100.0

    held = int(pos.get("filled_contracts") or 0)
    # A position is fully exited ONLY when every held contract is confirmed
    # sold. `remaining <= 0` alone is NOT that: Kalshi zeroes remaining_count on
    # a CANCELED order, so a partially-filled exit whose remainder was canceled
    # (by the chase, by market close, or manually) would be booked as a full
    # exit while the residual contracts ride on Kalshi unbooked — their
    # settlement cash would never be recorded. A canceled partial stays
    # 'exiting' so _settle_if_closed books partial proceeds + residual payout.
    if sold > 0 and remaining <= 0 and sold >= held:
        pnl = (
            proceeds - float(pos.get("cost_usd") or 0.0)
            - float(pos.get("fees_usd") or 0.0) - exit_fees
        )
        with db.get_db() as conn:
            _mark_resolved(
                conn, pid, status="exited",
                exit_filled_contracts=sold, proceeds_usd=proceeds,
                exit_fees_usd=exit_fees,
                pnl_usd=pnl, outcome_correct=1 if pnl > 0 else 0,
            )
            return db.fetch_crypto15m_by_id(conn, pid)

    if sold > 0 and sold != int(pos.get("exit_filled_contracts") or 0):
        with db.get_db() as conn:
            db.update_crypto15m_position(
                conn, pid, exit_filled_contracts=sold, proceeds_usd=proceeds,
                exit_fees_usd=exit_fees,
            )
    return None


async def _settle_if_closed(pos: dict) -> Optional[dict]:
    try:
        market = await kalshi_api.fetch_market(pos["ticker"])
    except Exception:
        return None
    payout = trader._market_yes_payout(market) if market else None
    if payout is None:
        return None

    kid = pos.get("exit_kalshi_order_id")
    if not kid and pos.get("exit_client_order_id") and pos.get("status") == "exiting":
        # A lost-response exit is still unresolved — settling now would use
        # numbers that may be missing its fills. _poll_exit adopts/reverts it
        # first; settle on a later tick.
        return None
    if kid:
        try:
            await kalshi_api.cancel_order(kid)
        except Exception:
            pass
        # The exit may have (partially) filled between the last poll and this
        # cancel — re-read its FINAL fills so the settlement books the real
        # partial proceeds instead of a stale zero (a same-tick partial fill
        # would otherwise value already-sold contracts at the settlement payout,
        # booking a losing trade as a win). If the re-read fails outright,
        # DO NOT settle from the stale row — a wrong settlement is permanent
        # (resolved rows are never revisited); retry next tick instead.
        try:
            resp = await kalshi_api.get_order(kid)
            parsed = trader._parse_kalshi_order(
                (resp.get("order") if isinstance(resp, dict) else resp) or {}
            )
            sold_final = int(parsed.get("filled") or 0)
            if sold_final > int(pos.get("exit_filled_contracts") or 0):
                with db.get_db() as conn:
                    db.update_crypto15m_position(
                        conn, pos["id"],
                        exit_filled_contracts=sold_final,
                        proceeds_usd=sold_final - parsed["cost_cents"] / 100.0,
                        exit_fees_usd=float(parsed.get("fees_usd") or 0.0),
                    )
                    pos = db.fetch_crypto15m_by_id(conn, pos["id"]) or pos
        except Exception:
            return None

    filled = int(pos.get("filled_contracts") or 0)
    cost_usd = float(pos.get("cost_usd") or 0.0)
    sold = int(pos.get("exit_filled_contracts") or 0)
    partial_proceeds = float(pos.get("proceeds_usd") or 0.0)
    residual = max(0, filled - sold)
    our = payout if pos["direction"] == "yes" else (1.0 - payout)
    settlement = residual * our
    pnl = (
        partial_proceeds + settlement - cost_usd
        - float(pos.get("fees_usd") or 0.0)
        - float(pos.get("exit_fees_usd") or 0.0)
    )
    correct = 1 if our >= 0.99 else (0 if our <= 0.01 else (1 if pnl > 0.01 else 0))
    with db.get_db() as conn:
        _mark_resolved(
            conn, pos["id"], status="settled",
            exit_reason=pos.get("exit_reason") or "settlement",
            outcome_correct=correct, settlement_usd=settlement, pnl_usd=pnl,
        )
        logger.info(
            f"[crypto15m] exiting->settled {pos['asset']} pnl=${pnl:+.2f} "
            f"(exit sold {sold}/{filled} before close)"
        )
        return db.fetch_crypto15m_by_id(conn, pos["id"])


async def _chase_exit(pos: dict, cfg: dict) -> Optional[dict]:
    """Re-price a resting stop-loss SELL that has gone stale so it actually fills.

    A stop-loss fires precisely when the held side is dropping, so a limit sell
    placed at the best bid is, within seconds, left ABOVE the now-lower bid and
    rests UNFILLED — the position then rides to settlement at the full loss (the
    #1 'stop-loss didn't work' complaint). While the market is still open and the
    resting exit has NOT partially filled, cancel it and re-place the full size
    at the current best bid, walking the order down with the market until it
    clears (a marketable sell AT the bid is a taker and matches immediately).

    Only a zero-fill order is chased (safe to fully re-place). A partially-filled
    exit is left to ride to settlement, where _settle_if_closed credits the
    partial and settles only the residual — so we can never double-sell. A
    race-guard re-reads the cancelled order's FINAL fill in case it filled
    between the last poll and the cancel."""
    if int(pos.get("exit_filled_contracts") or 0) > 0:
        return None
    filled = int(pos.get("filled_contracts") or 0)
    if filled <= 0:
        return None
    direction = pos["direction"]
    reason = pos.get("exit_reason") or "stop_loss"
    cur_limit = int(pos.get("exit_limit_cents") or 0)

    try:
        book = await kalshi_api.get_orderbook(pos["ticker"])
        bids = book.get(direction) or []
        best_bid = max((int(b[0]) for b in bids if b and b[0] is not None), default=0)
    except Exception:
        best_bid = 0
    if best_bid <= 0:
        return None  # no liquidity to sell into — settlement will flatten it
    if cur_limit and cur_limit <= best_bid:
        return None  # our sell is at/under the top bid → still marketable, hold

    # Re-price `slippage` cents through the bid as well, matching the initial stop.
    new_cents = max(1, min(99, best_bid - _stop_slippage(cfg)))
    kid = pos.get("exit_kalshi_order_id")
    if not kid:
        # 'exiting' with no order id = a lost-response sell parked for
        # resolution (_poll_exit adopts or reverts it). Placing a fresh sell
        # here could double-sell against the unconfirmed one.
        return None
    if kid:
        try:
            await kalshi_api.cancel_order(kid)
        except Exception:
            pass
        # CONFIRM the old order is dead before placing a replacement. If its
        # state can't be read, or it's somehow still live (cancel timed out or
        # 5xx'd while the order kept resting), skip this tick and retry on the
        # next one — placing a second full-size sell alongside a live one can
        # OVERSELL and flip the account into an unintended opposite position.
        try:
            resp = await kalshi_api.get_order(kid)
            parsed = trader._parse_kalshi_order(
                (resp.get("order") if isinstance(resp, dict) else resp) or {}
            )
        except Exception:
            return None
        sold = int(parsed.get("filled") or 0)
        if sold > 0:
            # The cancelled order filled in the race — record/resolve instead of
            # placing a second sell for the same contracts. SELL fill_cost is the
            # offsetting-leg cost; cash proceeds = face value (sold * $1) minus
            # that complement (see _poll_exit).
            proceeds = sold - parsed["cost_cents"] / 100.0
            exit_fees = float(parsed.get("fees_usd") or 0.0)
            with db.get_db() as conn:
                if sold >= filled:
                    pnl = (
                        proceeds - float(pos.get("cost_usd") or 0.0)
                        - float(pos.get("fees_usd") or 0.0) - exit_fees
                    )
                    _mark_resolved(
                        conn, pos["id"], status="exited",
                        exit_filled_contracts=sold, proceeds_usd=proceeds,
                        exit_fees_usd=exit_fees,
                        pnl_usd=pnl, outcome_correct=1 if pnl > 0 else 0,
                    )
                else:
                    db.update_crypto15m_position(
                        conn, pos["id"],
                        exit_filled_contracts=sold, proceeds_usd=proceeds,
                        exit_fees_usd=exit_fees,
                    )
                return db.fetch_crypto15m_by_id(conn, pos["id"])
        # Re-place ONLY on a CONFIRMED dead order with zero fills. 'executed'
        # with a zero parsed fill would mean the fill fields changed shape —
        # re-placing against an executed exit oversells; let the next tick's
        # re-read sort it out instead.
        if parsed.get("status") not in ("canceled", "cancelled") or sold > 0:
            return None

    coid = f"krypt-c15x-{pos['asset']}-{uuid.uuid4().hex[:8]}"
    try:
        resp = await kalshi_api.place_limit_order(
            ticker=pos["ticker"], side=direction, action="sell",
            count=filled, price_cents=new_cents, client_order_id=coid,
        )
    except Exception as e:
        # Same lost-response hazard as _place_exit: the old order is confirmed
        # dead, but this POST may have landed. Adopt-or-confirm; never leave a
        # state where the next tick re-places blind.
        found, confirmed = await _lookup_lost_order(coid, pos["ticker"])
        with db.get_db() as conn:
            if found:
                db.update_crypto15m_position(
                    conn, pos["id"], status="exiting", exit_reason=reason,
                    exit_client_order_id=coid,
                    exit_kalshi_order_id=found.get("order_id"),
                    exit_limit_cents=new_cents,
                )
                logger.warning(f"[live] {reason} re-price {pos['asset']}: response lost, recovered via coid")
            elif confirmed:
                db.update_crypto15m_position(
                    conn, pos["id"], error=f"{reason} re-price failed: {str(e)[:140]}"
                )
            else:
                db.update_crypto15m_position(
                    conn, pos["id"], status="exiting", exit_reason=reason,
                    exit_client_order_id=coid, exit_kalshi_order_id=None,
                    error=f"{reason} re-price UNCONFIRMED: {str(e)[:120]}",
                )
            return db.fetch_crypto15m_by_id(conn, pos["id"])
    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    with db.get_db() as conn:
        db.update_crypto15m_position(
            conn, pos["id"], status="exiting", exit_reason=reason,
            exit_client_order_id=coid,
            exit_kalshi_order_id=order.get("order_id") if isinstance(order, dict) else None,
            exit_limit_cents=new_cents,
        )
        logger.info(
            f"[live] {reason.upper().replace('_', '-')} re-price {pos['asset']} "
            f"x{filled} @ {new_cents}c (was {cur_limit}c — chasing the bid down)"
        )
        return db.fetch_crypto15m_by_id(conn, pos["id"])


async def _manage_position(pos: dict, cfg: dict, env: str) -> Optional[dict]:
    status = pos.get("status")

    if pos.get("dry_run"):
        # Legacy simulated position from the removed paper engine — retire it
        # so it stops showing as open. No real order ever backed it.
        with db.get_db() as conn:
            _mark_resolved(
                conn, pos["id"], status="canceled", exit_reason="unfilled_expired"
            )
            return db.fetch_crypto15m_by_id(conn, pos["id"])

    if status in ("submitted", "placing"):
        return await _poll_entry(pos, cfg)
    if status == "exiting":
        row = await _poll_exit(pos)
        if row:
            return row
        # _poll_exit may have written a partial fill to the DB and returned None
        # — re-fetch before chasing/settling. Handing the STALE dict onward made
        # _chase_exit see exit_filled_contracts=0, cancel the partially-filled
        # live stop-loss without re-placing it (the residual then rode to
        # settlement unprotected), and let _settle_if_closed book settlement
        # from zeroed fill/proceeds numbers.
        with db.get_db() as conn:
            pos = db.fetch_crypto15m_by_id(conn, pos["id"]) or pos
        # Chase the bid down if the resting stop-loss has gone stale, so it fills
        # instead of riding to settlement at the full loss.
        row = await _chase_exit(pos, cfg)
        if row:
            return row
        return await _settle_if_closed(pos)
    if status == "error" and int(pos.get("filled_contracts") or 0) <= 0:
        with db.get_db() as conn:
            _mark_resolved(conn, pos["id"], status="error")
            return db.fetch_crypto15m_by_id(conn, pos["id"])
    if status != "filled":
        return None

    # Pair legs: settlement only — no stop-loss, no take-profit, no WS
    # quote management (see _manage_pair).
    if (pos.get("strategy") or "") == "pair":
        return await _manage_pair(pos)

    pid = pos["id"]
    direction = pos["direction"]
    filled = int(pos.get("filled_contracts") or 0)
    cost_usd = float(pos.get("cost_usd") or 0.0)

    # While the window is still OPEN, settlement is impossible — so stop-loss/
    # take-profit detection can run off the live WS ticker quote instead of a
    # REST fetch_market per position per tick (0.2-0.5s each, and the reason
    # exit reaction was capped at REST cadence). Once close_time passes (or no
    # fresh quote exists) fall back to REST, which also detects settlement.
    market = None
    ws_market = False
    close_epoch = crypto15m._parse_close_epoch(pos.get("close_time") or "")
    if close_epoch is not None and kalshi_auth.server_now() < close_epoch - 2:
        market = _ws_quote_market(pos["ticker"])
        ws_market = market is not None
    if market is None:
        try:
            market = await kalshi_api.fetch_market(pos["ticker"])
        except Exception:
            market = None

    payout = trader._market_yes_payout(market) if (market and not ws_market) else None
    if payout is not None:
        our = payout if direction == "yes" else (1.0 - payout)
        settlement = filled * our
        pnl = settlement - cost_usd - float(pos.get("fees_usd") or 0.0)
        correct = 1 if our >= 0.99 else (0 if our <= 0.01 else (1 if pnl > 0.01 else 0))
        with db.get_db() as conn:
            _mark_resolved(
                conn, pid, status="settled",
                exit_reason=(pos.get("exit_reason") or "settlement"),
                outcome_correct=correct, settlement_usd=settlement, pnl_usd=pnl,
            )
            return db.fetch_crypto15m_by_id(conn, pid)

    side_prob = side_prob_from_market(market, direction)
    if should_take_profit(pos, side_prob, cfg):
        return await _place_exit(pos, market, cfg, reason="take_profit")
    if should_stop_loss(pos, side_prob, cfg):
        return await _place_exit(pos, market, cfg, reason="stop_loss")

    return None




# ───────── pairs: temporal complement accumulation ──────────────────────────
#
# Both sides of one 15m binary seesaw: when UP dips, DOWN peaks, and vice
# versa. Buy each side on ITS OWN cheap moment at different times in the
# window; once both legs are held, each matched YES+NO pair settles at exactly
# $1 regardless of direction — so a blended pair cost below $1 − fees is
# LOCKED profit. Instantaneous ask_up + ask_down < $1 essentially never
# happens; the temporal version has opportunities every window.
#
# Rules (adapted from the spread-capture playbook):
#   * a leg buys only on a DIP: its ask ≥ `pairs_dip_cents` below its rolling
#     median over the last ~3 minutes of ticks
#   * second leg only if ask ≤ ceiling − (what the first leg actually cost) —
#     the hard constraint that keeps the blended pair below the ceiling
#   * first leg only early in the window (≥5 min left — there must be time to
#     catch the complement), below `pairs_first_leg_max_cents`, and only when
#     the OTHER side's recent prices make completion plausible
#   * NO mid-window exits, no stop-loss, no take-profit: selling a leg
#     converts locked margin into a naked directional bet. Both legs ride to
#     settlement (the existing per-row settlement booking nets them correctly).
#   * an unmatched window (second leg never came cheap) is the accepted
#     failure mode: one clip rides to settlement like any directional bet.

_PAIR_FIRST_LEG_MIN_MINS = 5.0   # first leg needs time to catch the complement
_PAIR_LEG_MIN_MINS = 1.0         # final minute = settlement sampling, stay out
_PAIR_HIST_MIN = 8               # ~30s of ticks before a "dip" means anything
# First legs need a LONGER baseline (~80s of ticks): in a window's first
# minute the book is still finding its level, so early "dips" are price
# discovery / the opening drift — the 04:45:54-entry knife-catches — not the
# seesaw. Second legs keep the short warm-up (they reduce risk).
_PAIR_FIRST_HIST_MIN = 20
# Max windows allowed to sit with an UNMATCHED first leg at once. Until its
# complement fills, a first leg is a small directional bet — and cheap "dips"
# cluster in trending regimes, so unmatched legs across assets are one
# correlated reversal bet. Second legs are always allowed (they REDUCE risk).
_PAIR_MAX_UNMATCHED = 2

# ticker -> {"yes": deque[ask_cents], "no": deque[ask_cents]} — rolling ask
# history fed once per executor tick (~4s), so maxlen 45 ≈ the last 3 minutes.
_pair_ask_hist: dict[str, dict[str, deque]] = {}


def _update_pair_hist(assets: dict) -> None:
    live_tickers = set()
    for a in assets.values():
        t = a.get("ticker")
        if not a.get("hasMarket") or not t:
            continue
        live_tickers.add(t)
        hist = _pair_ask_hist.setdefault(
            t, {"yes": deque(maxlen=45), "no": deque(maxlen=45)}
        )
        ya, na = a.get("upAsk"), a.get("downAsk")
        if ya:
            hist["yes"].append(round(float(ya) * 100.0, 1))
        if na:
            hist["no"].append(round(float(na) * 100.0, 1))
    # Windows roll every 15 minutes — drop histories for dead tickers.
    for t in [t for t in _pair_ask_hist if t not in live_tickers]:
        _pair_ask_hist.pop(t, None)


def _pair_leg_ok(
    ask_cents: float,
    own_hist,
    other_hist,
    *,
    other_cost_cents: Optional[float],
    ceiling_cents: float,
    dip_cents: float,
    first_leg_max_cents: float,
    mins_left: float,
    first_leg_min_cents: float = 35.0,
) -> tuple[bool, str]:
    """Decide one pair leg. `other_cost_cents` is what the other side has
    (or will, for a resting order) cost — None means this is the FIRST leg."""
    if mins_left < _PAIR_LEG_MIN_MINS:
        return False, "final minute"
    if len(own_hist) < _PAIR_HIST_MIN:
        return False, "history warming up"
    med = statistics.median(own_hist)
    if med - ask_cents < dip_cents:
        return False, f"no dip (ask {ask_cents:.0f}c vs median {med:.0f}c)"
    if other_cost_cents is not None:
        # Gate on the MARKETABLE LIMIT we actually send (ask + 1c), not the
        # raw ask — a leg gated at ask=64 but filled at its 65c limit produced
        # a 96c blended pair, 1c through the ceiling (real trade #285).
        if ask_cents + 1 > ceiling_cents - other_cost_cents:
            return False, (
                f"pair would cost up to {ask_cents + 1 + other_cost_cents:.0f}c "
                f"> {ceiling_cents:.0f}c ceiling"
            )
        return True, "ok"
    # First leg: there must be time and a plausible path to completion.
    if mins_left < _PAIR_FIRST_LEG_MIN_MINS:
        return False, "too late to start a pair"
    if len(own_hist) < _PAIR_FIRST_HIST_MIN or len(other_hist) < _PAIR_FIRST_HIST_MIN:
        return False, "window too young for a first leg (~80s of ticks needed)"
    # Price BAND, not just a cap. Pairs only pay where the two sides genuinely
    # oscillate — near coin-flip. A "dip" below the floor usually means a
    # strong favorite is trending and this is its LOSING side making new lows:
    # buying it is a knife-catch whose complement only arrives if the whole
    # trend reverses (the 20-33c first legs that motivated this band).
    if ask_cents < first_leg_min_cents:
        return False, (
            f"first leg {ask_cents:.0f}c < {first_leg_min_cents:.0f}c floor "
            f"(strong favorite against — trend, not seesaw)"
        )
    if ask_cents > first_leg_max_cents:
        return False, f"first leg {ask_cents:.0f}c > {first_leg_max_cents:.0f}c max"
    if len(other_hist) < _PAIR_HIST_MIN:
        return False, "other side history warming up"
    # Plausibility uses the other side's recent LOW, not its median — median
    # asks sum to >100c (the book's overround), so a median-based check would
    # never pass. The seesaw is the whole edge: when this side is on a dip the
    # other is peaking, and its recent low marks where its own next dip
    # plausibly lands. Without that path this is just a naked directional buy.
    expected_other = min(other_hist)
    if ask_cents > ceiling_cents - expected_other:
        return False, (
            f"complement unlikely to fit (other side's recent low "
            f"{expected_other:.0f}c, needs ≤ {ceiling_cents - ask_cents:.0f}c)"
        )
    return True, "ok"


async def _open_pair_leg(
    a: dict, cfg: dict, env: str, *, direction: str, count: int, limit_cents: int,
) -> Optional[dict]:
    ticker = a["ticker"]
    coid = f"krypt-c15p-{a['asset']}-{direction}-{uuid.uuid4().hex[:8]}"
    row = {
        "asset": a["asset"], "series": a["series"], "ticker": ticker,
        "side": "up" if direction == "yes" else "down", "direction": direction,
        "target_contracts": count, "entry_limit_cents": limit_cents,
        "client_order_id": coid, "close_time": a.get("closeTime") or "",
        "confidence": float(limit_cents), "kalshi_env": env,
        "strategy": "pair",
    }
    try:
        resp = await kalshi_api.place_limit_order(
            ticker=ticker, side=direction, action="buy",
            count=count, price_cents=limit_cents, client_order_id=coid,
        )
    except Exception as e:
        # Same unacked-order recovery as directional entries: the POST may have
        # reached Kalshi with the response lost.
        recovered = None
        try:
            recovered = await kalshi_api.find_order_by_client_id(coid, ticker=ticker)
        except Exception:
            recovered = None
        if isinstance(recovered, dict) and recovered.get("order_id"):
            row.update({"status": "submitted",
                        "kalshi_order_id": recovered.get("order_id"),
                        "dry_run": False})
            with db.get_db() as conn:
                pid = db.insert_crypto15m_position(conn, row)
                logger.warning(
                    f"[pairs] {a['asset']} {direction} leg recovered via "
                    f"client_order_id after order error: {e}"
                )
                return db.fetch_crypto15m_by_id(conn, pid)
        row.update({"status": "error", "error": str(e)[:200], "dry_run": False})
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, row)
            _mark_resolved(conn, pid, status="error")
            logger.error(f"[pairs] {a['asset']} {direction} leg failed: {e}")
            return db.fetch_crypto15m_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    row.update({
        "status": "submitted",
        "kalshi_order_id": order.get("order_id") if isinstance(order, dict) else None,
        "dry_run": False,
    })
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, row)
        logger.info(
            f"[live] PAIR {a['asset']} {direction} x{count} @ {limit_cents}c"
        )
        return db.fetch_crypto15m_by_id(conn, pid)


async def _run_pairs(
    cfg: dict, env: str, assets: dict, open_positions: list[dict],
    balance_usd: float,
) -> list[dict]:
    """One pass of the complement-accumulation engine (already gated on live +
    daily-risk + session-TP by run_tick). At most one clip per side per window."""
    # The trading-hours window gates pairs exactly like directional entries.
    if not crypto15m.hours_ok(cfg):
        return []
    ceiling = float(cfg.get("crypto15m_pairs_ceiling_cents", 95.0) or 95.0)
    dip = float(cfg.get("crypto15m_pairs_dip_cents", 2.0) or 2.0)
    clip = max(1, int(cfg.get("crypto15m_pairs_clip", 5) or 5))
    first_min = float(cfg.get("crypto15m_pairs_first_leg_min_cents", 35.0) or 35.0)
    first_max = float(cfg.get("crypto15m_pairs_first_leg_max_cents", 60.0) or 60.0)
    cap_pct = _clamp01(cfg.get("crypto15m_max_total_pct", 0.0))

    # Pair rows by (ticker, direction); directional rows block the whole asset.
    pair_rows: dict[tuple[str, str], dict] = {}
    directional_assets: set[str] = set()
    for p in open_positions:
        if (p.get("strategy") or "") == "pair":
            pair_rows[(p.get("ticker") or "", p.get("direction") or "")] = p
        else:
            directional_assets.add(p.get("asset") or "")

    def _unmatched_count() -> int:
        tickers: dict[str, int] = {}
        for (tk, _d) in pair_rows:
            tickers[tk] = tickers.get(tk, 0) + 1
        return sum(1 for n in tickers.values() if n == 1)

    with db.get_db() as conn:
        errored = db.crypto15m_errored_tickers(conn, env)

    updated: list[dict] = []
    for sym, a in assets.items():
        t = a.get("ticker")
        if not a.get("hasMarket") or not t or t in errored:
            continue
        if not crypto15m.asset_enabled(cfg, sym):
            continue
        if sym in directional_assets:
            continue  # the directional strategy owns this asset's window
        mins_left = a.get("minsLeft")
        if mins_left is None:
            continue
        hist = _pair_ask_hist.get(t) or {"yes": deque(), "no": deque()}

        for direction, ask_key in (("yes", "upAsk"), ("no", "downAsk")):
            if (t, direction) in pair_rows:
                continue  # this leg already placed for this window
            ask = a.get(ask_key)
            if not ask or not (0.0 < float(ask) < 1.0):
                continue
            ask_c = round(float(ask) * 100.0, 1)
            other_dir = "no" if direction == "yes" else "yes"
            other = pair_rows.get((t, other_dir))
            other_cost: Optional[float] = None
            count = clip
            if other is not None:
                filled = int(other.get("filled_contracts") or 0)
                if filled <= 0:
                    # NEVER hedge an unfilled first leg. Second legs skip the
                    # band/floor protections because they offset held
                    # inventory — but if the resting first leg dies unfilled
                    # (book moved away), the "hedge" becomes a naked knife
                    # bought with no protections at all (real trade #290:
                    # down@48 never filled, up@23 rode to zero). Wait for the
                    # fill; the 30s entry TTL recycles dead first legs fast.
                    continue
                other_cost = float(other.get("avg_entry_cents") or
                                   other.get("entry_limit_cents") or 0)
                count = filled  # match the inventory that actually exists
            elif _unmatched_count() >= _PAIR_MAX_UNMATCHED:
                # Enough windows already waiting on their complement — every
                # unmatched first leg is directional risk, and cheap dips
                # cluster in trends, so they'd all be the SAME bet.
                continue
            else:
                # Model conviction gate on FIRST legs (fail-open when the
                # settlement model is unavailable): never open the side the
                # spot-vs-strike model says is being run over — that "dip" is
                # a trend leg making new lows, not a seesaw (the down@59 /
                # down@53 strands). This is the PBOT-6/Bonereaper lesson:
                # no-edge balanced entries are the drag.
                mp = a.get("modelProb")
                if mp is not None and (
                    (direction == "yes" and mp < 0.35)
                    or (direction == "no" and mp > 0.65)
                ):
                    continue
            ok, why = _pair_leg_ok(
                ask_c, hist.get(direction) or [], hist.get(other_dir) or [],
                other_cost_cents=other_cost, ceiling_cents=ceiling,
                dip_cents=dip, first_leg_max_cents=first_max,
                mins_left=float(mins_left),
                first_leg_min_cents=first_min,
            )
            if not ok:
                continue
            limit_cents = max(1, min(99, int(round(ask_c)) + 1))  # marketable taker
            # Aggregate 15m budget applies to pair legs too.
            if cap_pct > 0 and balance_usd > 0:
                with db.get_db() as conn:
                    committed = db.open_crypto15m_committed_usd(conn, env)
                budget = (balance_usd + committed) * cap_pct - committed
                if count * limit_cents / 100.0 > budget:
                    logger.info(
                        f"[pairs] skip {sym} {direction}: aggregate 15m cap "
                        f"(${committed:.2f} committed)"
                    )
                    continue
            row = await _open_pair_leg(
                a, cfg, env, direction=direction, count=count,
                limit_cents=limit_cents,
            )
            if row:
                updated.append(row)
                pair_rows[(t, direction)] = row
                if other is not None and (row.get("status") or "") != "error":
                    oc = other_cost or 0.0
                    logger.info(
                        f"[pairs] {sym} pair complete: blended ≤ "
                        f"{limit_cents + oc:.0f}c vs {ceiling:.0f}c ceiling "
                        f"({count} matched)"
                    )
    return updated


async def _manage_pair(pos: dict) -> Optional[dict]:
    """Pair legs have NO stop-loss and NO take-profit — a matched pair pays $1
    at settlement, and selling one leg would turn locked margin into a naked
    directional bet. Only settlement is checked (REST, after the close)."""
    close_epoch = crypto15m._parse_close_epoch(pos.get("close_time") or "")
    if close_epoch is not None and kalshi_auth.server_now() < close_epoch - 2:
        return None
    try:
        market = await kalshi_api.fetch_market(pos["ticker"])
    except Exception:
        return None
    payout = trader._market_yes_payout(market) if market else None
    if payout is None:
        return None
    filled = int(pos.get("filled_contracts") or 0)
    cost_usd = float(pos.get("cost_usd") or 0.0)
    our = payout if pos["direction"] == "yes" else (1.0 - payout)
    settlement = filled * our
    pnl = settlement - cost_usd - float(pos.get("fees_usd") or 0.0)
    correct = 1 if our >= 0.99 else (0 if our <= 0.01 else (1 if pnl > 0.01 else 0))
    with db.get_db() as conn:
        _mark_resolved(
            conn, pos["id"], status="settled", exit_reason="settlement",
            outcome_correct=correct, settlement_usd=settlement, pnl_usd=pnl,
        )
        return db.fetch_crypto15m_by_id(conn, pos["id"])


def session_take_profit_target(cfg: dict) -> float:
    """Dollar target for the session take-profit (`crypto15m_session_take_profit_usd`).
    0 = off."""
    try:
        return max(0.0, float(cfg.get("crypto15m_session_take_profit_usd", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def session_realized_pnl(env: str, session_start: Optional[str]) -> float:
    """Realized 15m P&L for `env` since the backend started (`session_start`)."""
    with db.get_db() as conn:
        return db.crypto15m_session_realized_pnl(conn, env, session_start)


def is_blocked_by_session_take_profit(
    cfg: dict, env: str, session_start: Optional[str]
) -> tuple[bool, str, float]:
    """Halt NEW 15m entries once realized session P&L reaches the take-profit
    target (open positions keep being managed). Returns (blocked, reason, pnl)."""
    target = session_take_profit_target(cfg)
    pnl = session_realized_pnl(env, session_start)
    if target > 0 and pnl >= target:
        return True, (
            f"15m session take-profit hit (session pnl=${pnl:+.2f}, "
            f"target=${target:+.2f})"
        ), pnl
    return False, "", pnl


async def run_tick(
    cfg: dict, *, authed: bool, session_start: Optional[str] = None
) -> list[dict]:
    env = trader.get_env()
    if not cfg.get("crypto15m_enabled"):
        # The feature toggle gates ENTRIES, never management: turning it off
        # with live positions open must not abandon them (no stop-loss, no
        # settlement booking, P&L never recorded). Manage what exists, open
        # nothing.
        with db.get_db() as conn:
            leftovers = db.get_open_crypto15m(conn, env)
        if not leftovers:
            return []
        updated: list[dict] = []
        for pos in leftovers:
            try:
                row = await _manage_position(pos, cfg, env)
                if row:
                    updated.append(row)
            except Exception as e:
                logger.warning(f"[crypto15m] manage {pos.get('asset')} failed: {e}")
        return updated
    live = bool(authed) and bool(cfg.get("crypto15m_live")) and env == "production"

    snap = await crypto15m.snapshot(cfg)
    assets = {a["asset"]: a for a in snap.get("assets", [])}

    # Keep the pairs engine's rolling ask history warm even in monitor-only
    # mode, so enabling it (or arming live) doesn't start from a cold baseline.
    _update_pair_hist(assets)

    with db.get_db() as conn:
        open_positions = db.get_open_crypto15m(conn, env)
        errored_tickers = db.crypto15m_errored_tickers(conn, env)
        stopped_tickers = db.crypto15m_stopped_tickers(conn, env)
    open_by_asset = {p["asset"]: p for p in open_positions}
    # Pair legs are market-neutral once matched — they must not consume the
    # directional correlation cap (max_concurrent), only the $ budget.
    open_count = len([
        p for p in open_positions if (p.get("strategy") or "") != "pair"
    ])

    updated: list[dict] = []

    for pos in open_positions:
        try:
            row = await _manage_position(pos, cfg, env)
            if row:
                updated.append(row)
        except Exception as e:
            logger.warning(f"[crypto15m] manage {pos.get('asset')} failed: {e}")

    # Entries only happen on a live (production) account — paper simulation was
    # removed, so demo/unarmed runs monitor existing positions but open nothing.
    if not live:
        return updated

    # The daily stop-loss / take-profit is an account-wide loss limit, so it must
    # cover the 15m executor too — otherwise it keeps opening live positions after
    # the main bot has halted for the day. Exits above already ran; only block
    # NEW entries here.
    blocked, why = trader._is_blocked_by_daily_risk(cfg, env)
    if blocked:
        logger.info(f"[crypto15m] skip entries: {why}")
        return updated

    # 15m-specific session take-profit: once this run's realized 15m P&L reaches
    # the target, stop opening NEW entries (exits above still run). Independent of
    # the account-wide daily take-profit checked just above.
    _block_reasons.clear()
    tp_blocked, tp_why, _tp_pnl = is_blocked_by_session_take_profit(cfg, env, session_start)
    if tp_blocked:
        logger.info(f"[crypto15m] skip entries: {tp_why}")
        return updated

    need_balance = (
        (cfg.get("crypto15m_sizing_mode") or "fixed").lower() == "balance_pct"
        or float(cfg.get("crypto15m_max_loss_pct") or 0.0) > 0.0
        or _clamp01(cfg.get("crypto15m_max_total_pct", 0.0)) > 0.0
        or bool(cfg.get("crypto15m_pairs_enabled"))
    )
    balance_usd = await _bankroll_usd(cfg, bool(authed)) if need_balance else 0.0

    # Directional (favorite/contrarian) entries — skippable entirely for
    # pairs-only mode (Direction = "Off" in the UI).
    if cfg.get("crypto15m_directional_enabled", True):
        for sym, a in assets.items():
            if sym in open_by_asset:
                continue
            if not crypto15m.asset_enabled(cfg, sym):
                continue  # asset toggled off in the 15m tab — monitor only
            if a.get("ticker") in errored_tickers:
                continue
            if a.get("ticker") in stopped_tickers:
                # Already stopped out of this window once — re-entering the
                # same 15-minute market after a stop just churns fees in chop.
                # The exclusion dies with the window (each ticker IS one window).
                continue
            ok, _why = should_enter(a, cfg, has_open=False, open_count=open_count)
            if not ok:
                # Surface the block reason instead of discarding it — this is
                # what the "why isn't it trading" panel reads per asset.
                _block_reasons[a.get("asset") or "?"] = _why
                continue
            try:
                row = await _open_entry(a, cfg, env, balance_usd)
                if row:
                    updated.append(row)
                    open_count += 1
            except Exception as e:
                logger.warning(f"[crypto15m] entry {sym} failed: {e}")

    if cfg.get("crypto15m_pairs_enabled"):
        try:
            # Include rows opened THIS tick so pairs never double-book an
            # asset the directional loop just entered (and vice versa).
            live_rows = open_positions + [
                r for r in updated if r and not r.get("resolved")
            ]
            updated.extend(await _run_pairs(cfg, env, assets, live_rows, balance_usd))
        except Exception as e:
            logger.warning(f"[pairs] tick failed: {e}")

    return updated


async def _sizing_preview(cfg: dict, authed: bool) -> dict:
    mode = (cfg.get("crypto15m_sizing_mode") or "fixed").lower()
    balance_pct = _clamp01(cfg.get("crypto15m_balance_pct", 0.02))
    max_loss_pct = _clamp01(cfg.get("crypto15m_max_loss_pct", 0.0))
    order_size = max(1, int(cfg.get("crypto15m_order_size", 1)))
    bal = await _bankroll_usd(cfg, bool(authed))

    thr = crypto15m._const(cfg, "entry_threshold")
    mode_now = (cfg.get("crypto15m_direction_mode") or "favorite").lower()
    if mode_now == "contrarian":
        base = 1.0 - thr
    elif mode_now == "model":
        base = 0.93  # sniper's observed average entry cost
    else:
        base = thr
    if (cfg.get("crypto15m_entry_style") or "maker") == "maker":
        est_price_cents = max(1, min(99, int(round(base * 100)) - 1))
    else:
        est_price_cents = entry_limit_cents(base, crypto15m._const(cfg, "entry_diff"))
    est_contracts = compute_entry_contracts(
        cfg, entry_limit_cents=est_price_cents, balance_usd=bal, order_size=order_size,
    )
    est_cost = est_contracts * est_price_cents / 100.0

    note = ""
    if mode == "balance_pct" and bal <= 0:
        note = "No balance yet — using fixed order size. Connect Kalshi or set a start bankroll to size by %."
    elif max_loss_pct > 0 and bal > 0 and est_contracts < 1:
        note = f"Max-loss budget too small to fund a contract at ~{est_price_cents}c."

    return {
        "mode": mode,
        "balancePct": balance_pct,
        "maxLossPct": max_loss_pct,
        "balanceUsd": bal,
        "estPriceCents": est_price_cents,
        "estContracts": est_contracts,
        "estCostUsd": est_cost,
        "note": note,
    }


async def status(
    cfg: dict, *, authed: bool = False, session_start: Optional[str] = None
) -> dict:
    env = trader.get_env()
    with db.get_db() as conn:
        open_rows = db.get_open_crypto15m(conn, env)
        recent = db.recent_crypto15m(conn, env, limit=40)
        by_strategy = db.crypto15m_strategy_stats(conn, env)
        stats = db.crypto15m_stats(conn, env)
    live_armed = bool(cfg.get("crypto15m_live"))
    live_supported = env == "production"
    tp_target = session_take_profit_target(cfg)
    session_pnl = session_realized_pnl(env, session_start)
    return {
        "enabled": bool(cfg.get("crypto15m_enabled")),
        "blockReasons": dict(_block_reasons),
        "modelCalibration": dict(_CAL_CACHE),
        "live": bool(cfg.get("crypto15m_enabled")) and live_armed and bool(authed) and live_supported,
        "liveArmed": live_armed,
        "liveSupported": live_supported,
        "authed": bool(authed),
        "orderSize": int(cfg.get("crypto15m_order_size", 1)),
        "maxConcurrent": int(cfg.get("crypto15m_max_concurrent", len(crypto15m.SERIES))),
        "takeProfitCents": take_profit_cents(cfg),
        "sessionTakeProfitUsd": tp_target,
        "sessionPnlUsd": session_pnl,
        "takeProfitHalted": bool(tp_target > 0 and session_pnl >= tp_target),
        "sizing": await _sizing_preview(cfg, authed),
        "env": env,
        "stats": stats,
        "byStrategy": by_strategy,
        "open": [_pos_to_js(r) for r in open_rows],
        "recent": [_pos_to_js(r) for r in recent],
    }
