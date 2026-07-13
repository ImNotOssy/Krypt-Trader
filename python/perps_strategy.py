"""User-composable perps strategies: rule engine + backtest + paper/live.

The perps sibling of the 15m rule system, built on the same contract the 15m
module earned the hard way: the LIVE gates and the BACKTEST run the exact same
code over the exact same field vocabulary, so what a backtest trades is by
construction what the live engine would have traded.

Field vocabulary (PERPS_RULE_FIELDS): every field is computable from a window
of 1-minute bars alone (+ the last FINALIZED funding rate), which is what
guarantees replay parity — the backtest walks recorded perp_candles, the
paper/live engine walks in-memory bars built from live WS quotes, both through
compute_features().

Execution honesty (the strat-hunt swarm's fill rules, hard-earned):
  * signal on bar t → fill at bar t+1 (never same-bar)
  * taker: BUY at ask_close, SELL at bid_close — the spread is always paid
  * maker: order at close, filled ONLY if the next bar trades THROUGH it
  * fees both eras (today maker 5/taker 80 bps; post-Jul-8 tier0 5/12)
  * funding applied for positions held across 04/12/20 UTC stamps
  * leverage: a position is liquidated when the adverse move exceeds ~90% of
    the margin fraction (10000/leverage bps) — a simplification of Kalshi's
    maintenance model, disclosed in the caveats.

RISK NOTE displayed to users: the strat-hunt (python/data/research/
perps-2026-07/) backtested 11 mechanisms on this venue and ALL lost money —
this panel exists so users can test their own ideas honestly before risking
anything, not because we found an edge. Leverage can lose more than margin.
"""
from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import capturetrail
import db
import kalshi_auth
import kalshi_perps_api as papi
import perps_farmer
import perps_ws as pws
import rules as rules_mod

logger = logging.getLogger("perps_strategy")

FEES = {
    "today": {"maker": 5.0, "taker": 80.0},
    "jul8": {"maker": 5.0, "taker": 12.0},
}

PERPS_RULE_FIELDS = [
    "price",
    "spreadBps",
    "ret1mBps", "ret5mBps", "ret15mBps", "ret60mBps",
    "vol15mBps", "vol60mBps",
    "fromHigh60mBps", "fromLow60mBps",
    "volume15m",
    "volumeRatio",
    "oiChange15mPct",
    "fundingRateBps",
    "minsToFunding",
    "hourUtc", "dowUtc",
]

_FUNDING_HOURS = (4, 12, 20)



def compute_features(bars: list[dict], i: int, funding_rate_bps: float | None) -> dict | None:
    """Feature dict for bar index i (uses bars ≤ i only). Bars are dicts with
    keys: end_ts, bid_close, ask_close, close (trade close, may be None),
    volume, oi — all floats/None, USD units (candle-loader shape).
    Returns None when the window is too short."""
    if i < 60:
        return None
    b = bars[i]
    bid, ask = b.get("bid_close"), b.get("ask_close")
    if not bid or not ask or ask <= 0:
        return None
    mid = (bid + ask) / 2

    def close(j: int) -> float | None:
        bj = bars[j]
        c = bj.get("close")
        if c:
            return c
        bb, aa = bj.get("bid_close"), bj.get("ask_close")
        return (bb + aa) / 2 if bb and aa else None

    c_now = close(i)
    if not c_now:
        return None

    def ret_bps(minutes: int) -> float | None:
        c_then = close(i - minutes)
        if not c_then:
            return None
        return (c_now - c_then) / c_then * 10_000

    def vol_bps(minutes: int) -> float | None:
        rets = []
        prev = None
        for j in range(i - minutes, i + 1):
            c = close(j)
            if c and prev:
                rets.append((c - prev) / prev)
            prev = c if c else prev
        if len(rets) < max(5, minutes // 3):
            return None
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / len(rets)
        return math.sqrt(var) * 10_000

    highs = [close(j) for j in range(i - 60, i + 1)]
    highs = [h for h in highs if h]
    vol15 = sum((bars[j].get("volume") or 0) for j in range(i - 15, i))
    vol60 = sum((bars[j].get("volume") or 0) for j in range(i - 60, i))
    oi_now = bars[i].get("oi")
    oi_then = bars[i - 15].get("oi")
    end_ts = int(b["end_ts"])
    dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    next_funding_h = min(
        ((h - dt.hour - 1) % 24) * 60 + (60 - dt.minute) for h in _FUNDING_HOURS
    )
    return {
        "price": c_now,
        "spreadBps": (ask - bid) / mid * 10_000,
        "ret1mBps": ret_bps(1),
        "ret5mBps": ret_bps(5),
        "ret15mBps": ret_bps(15),
        "ret60mBps": ret_bps(60),
        "vol15mBps": vol_bps(15),
        "vol60mBps": vol_bps(60),
        "fromHigh60mBps": (max(highs) - c_now) / c_now * 10_000 if highs else None,
        "fromLow60mBps": (c_now - min(highs)) / c_now * 10_000 if highs else None,
        "volume15m": vol15,
        "volumeRatio": (vol15 / (vol60 / 4)) if vol60 > 0 else None,
        "oiChange15mPct": ((oi_now - oi_then) / oi_then * 100)
        if oi_now and oi_then else None,
        "fundingRateBps": funding_rate_bps,
        "minsToFunding": float(next_funding_h),
        "hourUtc": float(dt.hour),
        "dowUtc": float(dt.weekday()),
    }


def should_enter(features: dict, cfg: dict) -> tuple[bool, str]:
    """THE entry gate — identical in backtest, paper and live."""
    user_rules = cfg.get("perps_strat_rules") or []
    ok, why = rules_mod.evaluate_rules(features, user_rules)
    return ok, why


def should_exit(
    features: dict, *, side: str, entry_px: float, held_min: float, cfg: dict,
) -> tuple[bool, str]:
    """THE exit gate. Uses bar-close mark; the backtester additionally checks
    intrabar TP/SL against bid/ask extremes (conservatively, SL first)."""
    px = features.get("price")
    if not px:
        return False, ""
    move_bps = (px - entry_px) / entry_px * 10_000
    if side == "short":
        move_bps = -move_bps
    sl = float(cfg.get("perps_strat_sl_bps", 20) or 0)
    tp = float(cfg.get("perps_strat_tp_bps", 30) or 0)
    if sl > 0 and move_bps <= -sl:
        return True, "sl"
    if tp > 0 and move_bps >= tp:
        return True, "tp"
    max_hold = float(cfg.get("perps_strat_max_hold_min", 60) or 0)
    if max_hold > 0 and held_min >= max_hold:
        return True, "max_hold"
    if cfg.get("perps_strat_exit_on_rules_fail"):
        ok, _ = should_enter(features, cfg)
        if not ok:
            return True, "rules_exit"
    return False, ""



def _load_funding_windows(conn, ticker: str) -> list[tuple[int, float]]:
    """[(stamp_unix, rate)] ascending, prod rows."""
    try:
        rows = conn.execute(
            """SELECT funding_time, funding_rate FROM perp_funding
               WHERE ticker=? AND kalshi_env='production' ORDER BY funding_time""",
            (ticker,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for ft, rate in rows:
        try:
            ts = int(datetime.strptime(str(ft), "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp())
            out.append((ts, float(rate)))
        except (ValueError, TypeError):
            continue
    return out


def _last_finalized_rate_bps(fund: list[tuple[int, float]], ts: int) -> float | None:
    lo, hi = 0, len(fund)
    while lo < hi:
        mid = (lo + hi) // 2
        if fund[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return fund[lo - 1][1] * 10_000 if lo else None


def backtest(cfg: dict, *, since_days: int = 14) -> dict:
    """Walk recorded 1m candles through the LIVE gates. Returns the
    Crypto15mBacktest-shaped dict the Backtest page renders (perps semantics
    noted in caveats; netEvCentsPerContract = avg net P&L per contract, ¢)."""
    import backtest as bt

    symbol = str(cfg.get("perps_strat_symbol") or "KXBTCPERP").upper().rstrip("1")
    side_cfg = "short" if str(cfg.get("perps_strat_direction")) == "short" else "long"
    entry_style = str(cfg.get("perps_strat_entry_style") or "taker")
    fee_era = str(cfg.get("perps_strat_fee_era") or "jul8")
    fees = FEES.get(fee_era, FEES["jul8"])
    contracts = max(1, int(cfg.get("perps_strat_contracts", 1)))
    leverage = max(1.0, float(cfg.get("perps_strat_leverage", 1)))
    liq_bps = 10_000 / leverage * 0.9
    ctp = capturetrail.params_from_cfg(cfg, "perps_strat")
    use_ct = ctp.active() and ctp.override

    conn = sqlite3.connect(f"file:{db.db_path()}?mode=ro", uri=True)
    try:
        bars = bt.load_perp_candles(conn, symbol, since_days=since_days, period_min=1)
        fund = _load_funding_windows(conn, symbol)
    finally:
        conn.close()

    trades: list[dict] = []
    pos: dict | None = None
    scanned = 0

    def fill_px(bar: dict, action: str) -> float | None:
        return bar.get("ask_close") if action == "buy" else bar.get("bid_close")

    def book_exit(exit_bar: dict, exit_px: float, reason: str, exit_is_taker: bool) -> None:
        nonlocal pos
        assert pos is not None
        notional_in = pos["entry_px"] * contracts
        notional_out = exit_px * contracts
        move = (exit_px - pos["entry_px"]) * contracts
        if pos["side"] == "short":
            move = -move
        fee_in = notional_in * (fees["maker"] if pos["maker_entry"] else fees["taker"]) / 10_000
        fee_out = notional_out * (fees["maker"] if not exit_is_taker else fees["taker"]) / 10_000
        funding_usd = 0.0
        for ts, rate in fund:
            if pos["entry_ts"] < ts <= int(exit_bar["end_ts"]):
                sgn = -1 if pos["side"] == "long" else 1
                funding_usd += sgn * rate * pos["entry_px"] * contracts
        pnl = move - fee_in - fee_out + funding_usd
        trades.append({
            "ticker": symbol, "asset": symbol.replace("KX", "").replace("PERP", ""),
            "side": pos["side"], "costCents": round(pos["entry_px"] * 100, 2),
            "minsLeft": None, "won": pnl > 0, "pnlUsd": round(pnl, 4),
            "at": datetime.fromtimestamp(int(exit_bar["end_ts"]), tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
        })
        pos = None

    i = 60
    while i < len(bars) - 1:
        scanned += 1
        feats = compute_features(bars, i,
                                 _last_finalized_rate_bps(fund, int(bars[i]["end_ts"])))
        nxt = bars[i + 1]
        if pos is None:
            if feats is not None:
                ok, why = should_enter(feats, cfg)
                if ok:
                    if entry_style == "maker":
                        want = bars[i].get("bid_close") if side_cfg == "long" else bars[i].get("ask_close")
                        thru = (nxt.get("low") is not None and want is not None and nxt["low"] < want) \
                            if side_cfg == "long" else \
                            (nxt.get("high") is not None and want is not None and nxt["high"] > want)
                        if thru:
                            pos = {"side": side_cfg, "entry_px": want, "maker_entry": True,
                                   "entry_ts": int(nxt["end_ts"]), "entry_i": i + 1,
                                   "ct": capturetrail.CTState.open(want)}
                    else:
                        px = fill_px(nxt, "buy" if side_cfg == "long" else "sell")
                        if px:
                            pos = {"side": side_cfg, "entry_px": px, "maker_entry": False,
                                   "entry_ts": int(nxt["end_ts"]), "entry_i": i + 1,
                                   "ct": capturetrail.CTState.open(px)}
        else:
            held_min = (int(bars[i]["end_ts"]) - pos["entry_ts"]) / 60
            adverse = bars[i].get("bid_low") if pos["side"] == "long" else bars[i].get("ask_high")
            if adverse:
                adv_bps = (adverse - pos["entry_px"]) / pos["entry_px"] * 10_000
                if pos["side"] == "short":
                    adv_bps = -adv_bps
                if adv_bps <= -liq_bps:
                    book_exit(bars[i], adverse, "liquidated", exit_is_taker=True)
                    i += 1
                    continue
            if use_ct:
                if pos["side"] == "long":
                    fav_px, adv_px = bars[i].get("bid_high"), bars[i].get("bid_low")
                else:
                    fav_px, adv_px = bars[i].get("ask_low"), bars[i].get("ask_high")
                if fav_px:
                    capturetrail.step(pos["ct"],
                                      capturetrail.favorable_mark(pos["side"], fav_px, pos["entry_px"]), ctp)
                if adv_px:
                    done_ct, reason_ct = capturetrail.step(
                        pos["ct"], capturetrail.favorable_mark(pos["side"], adv_px, pos["entry_px"]), ctp)
                    if done_ct:
                        book_exit(bars[i], adv_px, reason_ct, exit_is_taker=True)
            else:
                sl = float(cfg.get("perps_strat_sl_bps", 20) or 0)
                tp = float(cfg.get("perps_strat_tp_bps", 30) or 0)
                if pos["side"] == "long":
                    lo = bars[i].get("bid_low")
                    hi = bars[i].get("bid_high")
                    if sl > 0 and lo and (lo - pos["entry_px"]) / pos["entry_px"] * 10_000 <= -sl:
                        book_exit(bars[i], pos["entry_px"] * (1 - sl / 10_000), "sl", True)
                    elif tp > 0 and hi and (hi - pos["entry_px"]) / pos["entry_px"] * 10_000 >= tp:
                        book_exit(bars[i], pos["entry_px"] * (1 + tp / 10_000), "tp", True)
                else:
                    hi = bars[i].get("ask_high")
                    lo = bars[i].get("ask_low")
                    if sl > 0 and hi and (pos["entry_px"] - hi) / pos["entry_px"] * 10_000 <= -sl:
                        book_exit(bars[i], pos["entry_px"] * (1 + sl / 10_000), "sl", True)
                    elif tp > 0 and lo and (pos["entry_px"] - lo) / pos["entry_px"] * 10_000 >= tp:
                        book_exit(bars[i], pos["entry_px"] * (1 - tp / 10_000), "tp", True)
            if pos is not None and feats is not None:
                done, reason = should_exit(
                    feats, side=pos["side"], entry_px=pos["entry_px"],
                    held_min=held_min, cfg=cfg,
                )
                if done and reason in ("max_hold", "rules_exit"):
                    px = fill_px(nxt, "sell" if pos["side"] == "long" else "buy")
                    if px:
                        book_exit(nxt, px, reason, exit_is_taker=True)
        i += 1

    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total = sum(t["pnlUsd"] for t in trades)
    equity, run, peak, max_dd = [], 0.0, 0.0, 0.0
    by_hour: dict[int, dict] = {h: {"hour": h, "n": 0, "wins": 0, "pnlUsd": 0.0} for h in range(24)}
    by_day: dict[str, dict] = {}
    for t in trades:
        run += t["pnlUsd"]
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)
        equity.append({"at": t["at"], "value": round(run, 4)})
        h = int(t["at"][11:13])
        by_hour[h]["n"] += 1
        by_hour[h]["wins"] += 1 if t["won"] else 0
        by_hour[h]["pnlUsd"] = round(by_hour[h]["pnlUsd"] + t["pnlUsd"], 4)
        d = t["at"][:10]
        by_day.setdefault(d, {"day": d, "n": 0, "wins": 0, "pnlUsd": 0.0})
        by_day[d]["n"] += 1
        by_day[d]["wins"] += 1 if t["won"] else 0
        by_day[d]["pnlUsd"] = round(by_day[d]["pnlUsd"] + t["pnlUsd"], 4)

    caveats = [
        f"Fee era '{fee_era}': maker {fees['maker']}bps / taker {fees['taker']}bps of notional per fill. "
        "Today's real taker fee is 80bps until Kalshi's tier schedule reaches retail (~Jul 8).",
        "Fills are honest: signal on bar t fills at bar t+1; taker crosses the spread (buy@ask/sell@bid); "
        "maker entries fill only when the next bar trades THROUGH the price.",
        "Intrabar TP/SL priced off bid/ask extremes with SL checked first (conservative).",
        f"Liquidation simulated at {liq_bps:.0f}bps adverse move ({leverage:.0f}x, ~90% of margin) — a "
        "simplification of Kalshi's maintenance model; real liquidations can be earlier.",
        "Funding applied from FINALIZED 8h rates for stamps crossed while held.",
        "1-minute bars from your own recorder: signals between bars are invisible; candles cover app "
        "uptime + REST top-up (gap-free), but quote extremes inside a minute are OHLC, not tick-true.",
        "In-sample: thresholds tuned against this window are fit to the past. Our 11-strategy audit on "
        "this venue found NO profitable configuration — treat any green result with suspicion and "
        "paper-trade before arming anything.",
    ]
    return {
        "n": n, "wins": wins, "winRate": (wins / n) if n else 0.0,
        "netEvCentsPerContract": (total / (n * contracts) * 100) if n else 0.0,
        "totalPnlUsd": round(total * 1, 4),
        "maxDrawdownUsd": round(max_dd, 4),
        "contracts": contracts,
        "windowsScanned": scanned,
        "byAsset": {symbol: {"n": n, "wins": wins, "pnlUsd": round(total, 4)}},
        "equity": equity,
        "byHourUtc": [by_hour[h] for h in range(24)],
        "byDay": [by_day[d] for d in sorted(by_day)],
        "trades": trades[-500:],
        "caveats": caveats,
    }



_BAR_SEED_MIN = 90
_QUOTE_FRESH_MS = 180_000


class _Engine:
    """One strategy slot. Builds live 1m bars from WS quotes, runs the same
    gates as the backtest, books positions to perp_positions (dry_run=1 for
    paper). Live mode places real reduce-only-exited taker IOC orders through
    kalshi_perps_api — gated hard (funded wallet, caps, daily loss, window)."""

    def __init__(self) -> None:
        self.env = "production"
        self.bars: list[dict] = []
        self._cur_minute: int = 0
        self._cur_bar: dict | None = None
        self.halted_day = ""
        self.halt_reason = ""
        self.last_reason = ""
        self.last_error = ""
        self._seeded_symbol = ""
        self._fund_cache: list[tuple[int, float]] = []
        self._fund_cache_t = 0.0

    def _symbol(self, cfg: dict) -> str:
        return str(cfg.get("perps_strat_symbol") or "KXBTCPERP").upper().rstrip("1")

    def _wire(self, cfg: dict) -> str:
        return papi.env_ticker(self._symbol(cfg), self.env)

    def _halted(self) -> bool:
        return self.halted_day == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _halt(self, reason: str) -> None:
        self.halted_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.halt_reason = reason
        logger.warning(f"perps_strategy: HALTED for the day — {reason}")


    def _seed_bars(self, symbol: str) -> None:
        import backtest as bt
        try:
            conn = sqlite3.connect(f"file:{db.db_path()}?mode=ro", uri=True)
            try:
                self.bars = bt.load_perp_candles(conn, symbol, since_days=2, period_min=1)[-_BAR_SEED_MIN * 4:]
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"perps_strategy: bar seed failed: {e}")
            self.bars = []
        self._seeded_symbol = symbol
        self._cur_bar = None

    def _update_bars(self, q: dict) -> None:
        """Fold a live quote into the current minute bar; roll closed bars in."""
        ts_ms = int(q.get("ts_ms") or 0)
        if not ts_ms:
            return
        minute = ts_ms // 60_000
        bid = papi.micro_to_usd(q.get("bid_usd_micro"))
        ask = papi.micro_to_usd(q.get("ask_usd_micro"))
        last = papi.micro_to_usd(q.get("last_usd_micro"))
        oi = (q.get("oi_cc") or 0) / 100
        if not bid or not ask:
            return
        if self._cur_bar is None or minute != self._cur_minute:
            if self._cur_bar is not None:
                self.bars.append(self._cur_bar)
                if len(self.bars) > _BAR_SEED_MIN * 8:
                    del self.bars[: _BAR_SEED_MIN * 2]
            self._cur_minute = minute
            self._cur_bar = {
                "end_ts": (minute + 1) * 60, "bid_close": bid, "ask_close": ask,
                "bid_low": bid, "bid_high": bid, "ask_low": ask, "ask_high": ask,
                "open": last, "high": last, "low": last, "close": last,
                "mean": None, "volume": 0.0, "oi": oi,
            }
        b = self._cur_bar
        b["bid_close"], b["ask_close"] = bid, ask
        b["bid_low"] = min(b["bid_low"], bid)
        b["bid_high"] = max(b["bid_high"], bid)
        b["ask_low"] = min(b["ask_low"], ask)
        b["ask_high"] = max(b["ask_high"], ask)
        if last:
            b["close"] = last
            b["high"] = max(b["high"] or last, last)
            b["low"] = min(b["low"] or last, last)
        b["oi"] = oi

    def _funding_windows(self, symbol: str) -> list[tuple[int, float]]:
        if time.monotonic() - self._fund_cache_t > 600:
            try:
                conn = sqlite3.connect(f"file:{db.db_path()}?mode=ro", uri=True)
                try:
                    self._fund_cache = _load_funding_windows(conn, symbol)
                finally:
                    conn.close()
                self._fund_cache_t = time.monotonic()
            except Exception:
                pass
        return self._fund_cache


    async def _live_fill(self, cfg: dict, action: str, count_cc: int,
                         *, reduce_only: bool) -> float | None:
        """Taker IOC at the touch; returns avg fill USD or None. Live only."""
        ticker = self._wire(cfg)
        q = pws.quote(ticker)
        if not q:
            return None
        px_micro = q.get("ask_usd_micro") if action == "buy" else q.get("bid_usd_micro")
        if not px_micro:
            return None
        side = "bid" if action == "buy" else "ask"
        try:
            resp = await papi.place_perps_limit_order(
                ticker=ticker, side=side, count_cc=count_cc,
                price_usd_micro=px_micro, time_in_force="immediate_or_cancel",
                reduce_only=reduce_only, client_order_id=str(uuid.uuid4()),
            )
        except Exception as e:
            self.last_error = f"order failed: {e}"
            logger.warning(f"perps_strategy: {action} failed: {e}")
            return None
        filled = papi.cc(resp.get("fill_count"))
        avg = papi.usd_micro(resp.get("average_fill_price"))
        if not filled or filled <= 0 or avg is None:
            return None
        return avg / 1e6


    async def tick(self, cfg: dict) -> None:
        try:
            await self._tick(cfg)
        except Exception as e:
            self.last_error = str(e)
            logger.debug(f"perps_strategy: tick error: {e}")

    async def _tick(self, cfg: dict) -> None:
        self.env = kalshi_auth.get_env()
        symbol = self._symbol(cfg)
        wire = self._wire(cfg)
        live = bool(cfg.get("perps_strat_live"))

        if self._seeded_symbol != symbol:
            self._seed_bars(symbol)

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with db.get_db() as conn:
            day_pnl = db.perp_strategy_day_pnl(conn, self.env, day)
            open_pos = db.get_open_perp_position(conn, self.env)
        max_loss = float(cfg.get("perps_strat_daily_loss_usd", 5.0))
        if day_pnl / 1e6 <= -max_loss and not self._halted():
            self._halt(f"daily loss cap hit (${day_pnl / 1e6:.2f})")

        q = pws.quote(wire)
        now_ms = int(time.time() * 1000)
        recv = q.get("recv_ms") or q.get("ts_ms") if q else None
        if not q or not recv or now_ms - int(recv) > _QUOTE_FRESH_MS:
            self.last_reason = "quote stale / stream offline"
            return
        self._update_bars(q)

        feats = compute_features(
            self.bars, len(self.bars) - 1,
            _last_finalized_rate_bps(self._funding_windows(symbol), int(time.time())),
        ) if len(self.bars) > 60 else None
        if feats is None:
            self.last_reason = f"warming up bars ({len(self.bars)}/61)"
            return

        if open_pos:
            await self._manage_position(cfg, open_pos, feats, q, live)
            return

        if self._halted():
            self.last_reason = f"halted: {self.halt_reason}"
            return
        if perps_farmer.in_maintenance_window():
            self.last_reason = "Kalshi maintenance window"
            return

        ok, why = should_enter(feats, cfg)
        self.last_reason = why
        if not ok:
            return

        side = "short" if str(cfg.get("perps_strat_direction")) == "short" else "long"
        contracts = max(1, int(cfg.get("perps_strat_contracts", 1)))
        count_cc = contracts * 100
        bid = papi.micro_to_usd(q.get("bid_usd_micro"))
        ask = papi.micro_to_usd(q.get("ask_usd_micro"))
        if not bid or not ask:
            return
        entry_px = ask if side == "long" else bid
        notional = entry_px * contracts
        max_notional = float(cfg.get("perps_strat_max_notional_usd", 100.0))
        if notional > max_notional:
            self.last_reason = f"notional ${notional:.0f} > cap ${max_notional:.0f}"
            return

        if live:
            avg = await self._live_fill(cfg, "buy" if side == "long" else "sell",
                                        count_cc, reduce_only=False)
            if avg is None:
                return
            entry_px = avg
        fee_bps = FEES["today"]["taker"]
        fee_micro = int(entry_px * contracts * fee_bps / 10_000 * 1e6)
        with db.get_db() as conn:
            db.open_perp_position(conn, {
                "ticker": wire, "side": side, "dry_run": not live,
                "count_cc": count_cc,
                "entry_usd_micro": int(entry_px * 1e6),
                "leverage": float(cfg.get("perps_strat_leverage", 1)),
                "fees_usd_micro": fee_micro,
                "entry_reason": why,
                "kalshi_env": self.env,
            })
        logger.info(
            f"perps_strategy: {'LIVE' if live else 'paper'} {side} {contracts}ct "
            f"{wire} @ ${entry_px:.4f} ({why})"
        )

    async def _manage_position(self, cfg: dict, pos: dict, feats: dict,
                               q: dict, live: bool) -> None:
        side = pos["side"]
        entry_px = pos["entry_usd_micro"] / 1e6
        contracts = pos["count_cc"] / 100
        opened = datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc)
        held_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        done, reason = should_exit(
            feats, side=side, entry_px=entry_px, held_min=held_min, cfg=cfg,
        )
        if not done:
            self.last_reason = f"holding {side} ({held_min:.0f}m)"
            return
        await self._close_position(cfg, pos, reason, live and not pos["dry_run"])

    async def _close_position(self, cfg: dict, pos: dict, reason: str,
                              live_close: bool) -> None:
        wire = pos["ticker"]
        side = pos["side"]
        contracts = pos["count_cc"] / 100
        entry_px = pos["entry_usd_micro"] / 1e6
        q = pws.quote(wire)
        bid = papi.micro_to_usd(q.get("bid_usd_micro")) if q else None
        ask = papi.micro_to_usd(q.get("ask_usd_micro")) if q else None
        exit_px = bid if side == "long" else ask
        if live_close:
            avg = await self._live_fill(
                cfg, "sell" if side == "long" else "buy",
                pos["count_cc"], reduce_only=True,
            )
            if avg is None:
                self.last_error = "live exit unfilled — retrying next tick"
                return
            exit_px = avg
        if not exit_px:
            return
        move = (exit_px - entry_px) * contracts
        if side == "short":
            move = -move
        fee_micro = int(exit_px * contracts * FEES["today"]["taker"] / 10_000 * 1e6)
        fund = self._funding_windows(self._symbol(cfg))
        opened_ts = int(datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=timezone.utc).timestamp())
        funding_usd = 0.0
        for ts, rate in fund:
            if opened_ts < ts <= int(time.time()):
                funding_usd += (-1 if side == "long" else 1) * rate * entry_px * contracts
        pnl_micro = int(move * 1e6) - pos["fees_usd_micro"] - fee_micro + int(funding_usd * 1e6)
        with db.get_db() as conn:
            db.close_perp_position(
                conn, pos["id"], exit_usd_micro=int(exit_px * 1e6),
                fees_usd_micro=fee_micro, funding_usd_micro=int(funding_usd * 1e6),
                pnl_usd_micro=pnl_micro, exit_reason=reason,
            )
        logger.info(
            f"perps_strategy: closed {side} {wire} @ ${exit_px:.4f} "
            f"({reason}) pnl ${pnl_micro / 1e6:+.4f}"
        )

    async def flatten(self, cfg: dict) -> dict:
        with db.get_db() as conn:
            pos = db.get_open_perp_position(conn, kalshi_auth.get_env())
        if not pos:
            return {"closed": 0}
        await self._close_position(cfg, pos, "flatten",
                                   live_close=not pos["dry_run"])
        return {"closed": 1}

    def status(self, cfg: dict) -> dict:
        env = kalshi_auth.get_env()
        with db.get_db() as conn:
            pos = db.get_open_perp_position(conn, env)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            day_pnl = db.perp_strategy_day_pnl(conn, env, day)
        open_out = None
        if pos:
            q = pws.quote(pos["ticker"])
            mark = None
            if q:
                bid = papi.micro_to_usd(q.get("bid_usd_micro"))
                ask = papi.micro_to_usd(q.get("ask_usd_micro"))
                mark = bid if pos["side"] == "long" else ask
            entry = pos["entry_usd_micro"] / 1e6
            upnl = None
            if mark:
                upnl = (mark - entry) * pos["count_cc"] / 100
                if pos["side"] == "short":
                    upnl = -upnl
            open_out = {
                "ticker": pos["ticker"], "side": pos["side"],
                "dryRun": bool(pos["dry_run"]),
                "contracts": pos["count_cc"] / 100,
                "entry": entry, "mark": mark,
                "unrealizedUsd": round(upnl, 4) if upnl is not None else None,
                "openedAt": pos["opened_at"],
            }
        return {
            "enabled": bool(cfg.get("perps_strat_enabled", False)),
            "live": bool(cfg.get("perps_strat_live", False)),
            "halted": self._halted(),
            "haltReason": self.halt_reason if self._halted() else "",
            "lastReason": self.last_reason,
            "lastError": self.last_error,
            "openPosition": open_out,
            "dayPnlUsd": round(day_pnl / 1e6, 4),
            "bars": len(self.bars),
        }


_engine = _Engine()


async def tick(cfg: dict) -> None:
    await _engine.tick(cfg)


async def flatten(cfg: dict) -> dict:
    return await _engine.flatten(cfg)


def status(cfg: dict) -> dict:
    return _engine.status(cfg)


def run_backtest(cfg: dict, since_days: int = 14) -> dict:
    return backtest(cfg, since_days=since_days)
