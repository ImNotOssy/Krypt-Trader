"""Replay recorded ticks through the LIVE entry gates.

The old backtest harness reimplemented the gates and drifted (it could not
even express the shipped sniper). This module builds a snapshot-shaped asset
dict from each recorded tick and calls the real ``crypto15m_trader.
should_enter`` — so what the backtest trades is, by construction, what the
live engine would have traded under the same config.

Honesty is part of the contract: fills are assumed at the recorded ask
(taker), ticks are ~4-25s apart (the gate could have seen better/worse quotes
between them), maker fills are not modeled, and everything is in-sample.
Those caveats ship in the result payload, not a docstring.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

import backtest as bt
import crypto15m
import crypto15m_trader
import db as dbmod

# Every field the live gates (built-in modes + rule vocabulary) can read,
# derivable from a recorded tick. Rules referencing anything else fail closed
# (0 trades) — replay detects that and says so instead of returning a silent
# zero.
_DERIVABLE = {
    "hasMarket", "favorite", "favoritePrice", "entryCost", "minsLeft",
    "inWindow", "signal", "modelProb", "edgeNetCents", "settlePrints",
    "upAsk", "downAsk", "yesBid", "yesAsk", "upProb", "downProb",
    "deltaPct", "deltaSignedPct", "sigma1m", "spotUsd", "strikeUsd",
    "macd", "macdSignal", "macdHist", "macdCross", "rsi", "hourUtc",
    "closeTime", "ticker", "asset", "series",
}


def tick_to_asset(row: dict, cfg: dict, close_iso: str) -> dict:
    """Map a crypto15m_ticks row onto the exact snapshot field names the live
    gates read. Missing-in-ticks fields (peersAgree, marketBias, arbEdgeCents)
    are deliberately absent so rule evaluation fails closed, same as live."""
    up_prob = row.get("up_prob")
    yes_bid, yes_ask = row.get("yes_bid"), row.get("yes_ask")
    no_ask = row.get("no_ask")
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - float(yes_bid), 4)  # single complementary book
    fav = None
    fav_price = None
    if up_prob is not None:
        fav = "up" if float(up_prob) >= 0.5 else "down"
        fav_price = float(up_prob) if fav == "up" else 1.0 - float(up_prob)
    ml = row.get("mins_left")
    in_window = (
        ml is not None
        and 1.0 <= float(ml) <= float(crypto15m._const(cfg, "time_delay_min"))
    )
    hour = None
    ts = row.get("observed_at") or ""
    try:
        hour = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").hour
    except Exception:
        pass
    entry_cost = None
    if fav == "up":
        entry_cost = yes_ask
    elif fav == "down":
        entry_cost = no_ask
    # Rebuild the built-in favorite signal EXACTLY as the live snapshot does
    # (crypto15m.py signal expression): window + hours + threshold + the
    # entry_max cap + direction-aware min-delta + strict-threshold (the
    # executable ask must itself clear the threshold on a two-sided book).
    # Omitting the last three made replay trade entries live would reject.
    strict = bool(cfg.get("crypto15m_strict_threshold", True))
    two_sided = bool(yes_bid and yes_ask)
    min_dp = crypto15m._const(cfg, "min_delta_pct")
    ds = row.get("delta_signed_pct")
    if ds is None or min_dp <= 0:
        delta_ok = True
    elif fav == "up":
        delta_ok = float(ds) >= min_dp
    else:
        delta_ok = float(ds) <= -min_dp
    signal = bool(
        in_window
        and fav_price is not None
        and fav_price >= crypto15m._const(cfg, "entry_threshold")
        and entry_cost is not None
        and float(entry_cost) <= crypto15m._const(cfg, "entry_max")
        and delta_ok
        and (hour is None or crypto15m.hours_ok(cfg, hour=hour))
        and (not strict or (two_sided and float(entry_cost) >= crypto15m._const(cfg, "entry_threshold")))
    )
    return {
        "asset": row.get("asset"), "ticker": row.get("ticker"),
        "series": f"KX{row.get('asset')}15M", "hasMarket": True,
        "closeTime": close_iso,
        "favorite": fav, "favoritePrice": fav_price, "entryCost": entry_cost,
        "minsLeft": float(ml) if ml is not None else None,
        "inWindow": in_window, "signal": signal, "hourUtc": hour,
        "modelProb": row.get("model_prob"),
        "edgeNetCents": row.get("edge_net_cents"),
        "settlePrints": int(row.get("settle_prints") or 0),
        "upAsk": yes_ask, "downAsk": no_ask,
        "yesBid": yes_bid, "yesAsk": yes_ask,
        "upProb": up_prob,
        "downProb": (1.0 - float(up_prob)) if up_prob is not None else None,
        "deltaPct": row.get("delta_pct"),
        "deltaSignedPct": row.get("delta_signed_pct"),
        "sigma1m": row.get("sigma1m"),
        "spotUsd": row.get("spot"), "strikeUsd": row.get("strike"),
        "macd": row.get("macd"), "macdSignal": row.get("macd_signal"),
        "macdHist": row.get("macd_hist"), "macdCross": row.get("macd_cross"),
        "rsi": row.get("rsi"),
    }


def _missing_rule_fields(cfg: dict) -> list[str]:
    if not cfg.get("crypto15m_use_rules"):
        return []
    fields = {str(c.get("field")) for c in (cfg.get("crypto15m_rules") or [])}
    return sorted(fields - _DERIVABLE)


def replay(cfg: dict, *, env: str = "production", since_days: int = 60,
           max_concurrent_ignored: bool = True) -> dict:
    """Run the live entry gate over recorded ticks, one trade max per window,
    entered at the first qualifying tick's executable ask, held to settlement.
    Returns aggregate stats + per-asset breakdown + honesty caveats."""
    cfg = dict(cfg)
    # The replayed strategy is hypothetical — the live enable/arm switches
    # must not zero the backtest. Same for the calibration autopause:
    # historical pause state can't be reconstructed, so applying TODAY'S
    # pause/resume to the whole window is wrong in both directions.
    cfg["crypto15m_enabled"] = True
    cfg["crypto15m_model_autopause"] = False
    contracts = max(1, int(cfg.get("crypto15m_order_size") or 1))
    with dbmod.get_db() as conn:
        rows = conn.execute(
            """SELECT t.*, s.up_won, s.close_time AS sig_close
               FROM crypto15m_ticks t
               JOIN crypto15m_signals s
                 ON s.ticker = t.ticker AND s.kalshi_env = t.kalshi_env
               WHERE s.resolved = 1 AND s.up_won IS NOT NULL
                 AND t.kalshi_env = ?
                 AND t.observed_at >= datetime('now', ?)
               ORDER BY t.ticker, t.observed_at""",
            (env, f"-{int(since_days)} days"),
        ).fetchall()

    by_window: dict[str, list[dict]] = {}
    for r in rows:
        by_window.setdefault(r["ticker"], []).append(dict(r))

    trades: list[dict] = []
    n_windows = 0
    for ticker, ticks in by_window.items():
        # Honor the per-asset Trade toggles: live entries are filtered by
        # asset_enabled in run_tick, so a "My current settings" replay must
        # skip disabled assets too (they don't count as scanned either).
        if not crypto15m.asset_enabled(cfg, str(ticks[0].get("asset") or "")):
            continue
        n_windows += 1
        up_won = int(ticks[0].get("up_won") or 0)
        close_iso = str(ticks[0].get("sig_close") or "")
        for t in ticks:
            asset = tick_to_asset(t, cfg, close_iso)
            try:
                ok, _why = crypto15m_trader.should_enter(
                    asset, cfg, has_open=False, open_count=0,
                )
            except Exception:
                ok = False
            if not ok:
                continue
            side = crypto15m_trader._bought_side(asset, cfg)
            if side not in ("up", "down"):
                break
            cost = asset["upAsk"] if side == "up" else asset["downAsk"]
            if not cost or not (0.0 < float(cost) < 1.0):
                break
            cost = float(cost)
            fee = bt.kalshi_fee_per_contract(cost, contracts=contracts)
            won = up_won if side == "up" else (1 - up_won)
            pnl_ct = (1.0 - cost - fee) if won else (-cost - fee)
            trades.append({
                "ticker": ticker, "asset": asset["asset"], "side": side,
                "costCents": round(cost * 100, 1),
                "minsLeft": asset["minsLeft"], "won": bool(won),
                "pnlUsd": round(pnl_ct * contracts, 4),
                "at": t.get("observed_at"),
            })
            break  # one trade per window

    caveats = [
        "Entries fill at the recorded ask (taker); real fills can be worse and marketable orders sometimes miss entirely.",
        "Positions are held to settlement — stop-loss/take-profit exits are NOT simulated.",
        f"Ticks are 4-25s apart over {since_days} days of app uptime only; the gate could have fired between ticks.",
        "In-sample: any threshold tuned against this panel is fit to the past. Paper-trade before arming.",
        "Live model-calibration auto-pause is NOT simulated — live trading can pause where this replay keeps trading.",
    ]
    missing = _missing_rule_fields(cfg)
    if missing:
        caveats.insert(0, (
            "Rules reference fields not recorded in ticks — those conditions "
            f"never match in replay (0 trades is expected): {', '.join(missing)}"
        ))
    return _summarize(trades, contracts, n_windows, caveats)


def _bucketize(trades: list[dict]) -> dict:
    """Time anatomy: WHERE the profit lives. A strategy that prints in the
    first 12 UTC hours and bleeds in the last 12, or made all its money on
    one lucky day, looks identical in a single total - these buckets are how
    users catch that before arming."""
    by_hour = {h: {"n": 0, "wins": 0, "pnlUsd": 0.0} for h in range(24)}
    by_day: dict[str, dict] = {}
    for t in trades:
        ts = str(t.get("at") or "")
        try:
            hour = int(ts[11:13])
        except (ValueError, IndexError):
            hour = None
        day = ts[:10] if len(ts) >= 10 else None
        if hour is not None:
            b = by_hour[hour]
            b["n"] += 1
            b["wins"] += 1 if t["won"] else 0
            b["pnlUsd"] = round(b["pnlUsd"] + t["pnlUsd"], 4)
        if day:
            d = by_day.setdefault(day, {"n": 0, "wins": 0, "pnlUsd": 0.0})
            d["n"] += 1
            d["wins"] += 1 if t["won"] else 0
            d["pnlUsd"] = round(d["pnlUsd"] + t["pnlUsd"], 4)
    return {
        "byHourUtc": [{"hour": h, **by_hour[h]} for h in range(24)],
        "byDay": [{"day": d, **v} for d, v in sorted(by_day.items())],
    }


def _summarize(trades: list[dict], contracts: int, n_windows: int,
               caveats: list[str]) -> dict:
    trades = sorted(trades, key=lambda t: str(t.get("at") or ""))
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total = sum(t["pnlUsd"] for t in trades)
    # Per-contract EV must divide by the contracts each trade ACTUALLY sized
    # (replay_main sizes n_ct = fixed_usd/cost per signal, ~2x the flat
    # default at mid prices — the hardcoded denominator inflated the FE
    # "Edge / contract" stat accordingly). Trades without a size fall back
    # to the flat `contracts` (the crypto15m path, which sizes uniformly).
    denom = sum(int(t.get("contracts", contracts) or contracts) for t in trades)
    ev_ct = (total / denom * 100.0) if denom else 0.0
    by_asset: dict[str, dict] = {}
    for t in trades:
        a = by_asset.setdefault(t.get("asset") or "?", {"n": 0, "wins": 0, "pnlUsd": 0.0})
        a["n"] += 1
        a["wins"] += 1 if t["won"] else 0
        a["pnlUsd"] = round(a["pnlUsd"] + t["pnlUsd"], 4)
    equity, run = [], 0.0
    for t in trades:
        run += t["pnlUsd"]
        equity.append({"at": t.get("at"), "value": round(run, 4)})
    max_dd, peak = 0.0, 0.0
    for e in equity:
        peak = max(peak, e["value"])
        max_dd = min(max_dd, e["value"] - peak)
    if n < 30:
        caveats = [f"Only {n} trades - far too few for a verdict; treat as anecdote."] + list(caveats)
    return {
        "n": n, "wins": wins,
        "winRate": round(wins / n, 4) if n else 0.0,
        "netEvCentsPerContract": round(ev_ct, 2),
        "totalPnlUsd": round(total, 2),
        "maxDrawdownUsd": round(max_dd, 2),
        "contracts": contracts,
        "windowsScanned": n_windows,
        "byAsset": by_asset,
        "equity": equity[-400:],
        "trades": trades[-50:],
        "caveats": caveats,
        **_bucketize(trades),
    }


def replay_main(cfg: dict, *, since_days: int = 60,
                slippage_cents: float = 1.0) -> dict:
    """Replay recorded whale prints + momentum alerts through the LIVE
    trader.should_trade gates with follower economics: entry at the signal
    price + slippage, per-order rounded Kalshi fee, outcome from the recorded
    resolution. One simulated trade per accepted signal."""
    import trader as trader_mod
    contracts = 5
    fixed_usd = float(cfg.get("fixed_trade_usd") or 5.0)
    trades: list[dict] = []
    scanned = 0
    with dbmod.get_db() as conn:
        whales = conn.execute(
            """SELECT * FROM whale_trades WHERE resolved=1
               AND outcome_correct IS NOT NULL
               AND created_at >= datetime('now', ?)""",
            (f"-{int(since_days)} days",),
        ).fetchall()
        alerts = conn.execute(
            """SELECT * FROM alerts WHERE resolved=1
               AND outcome_correct IS NOT NULL
               AND created_at >= datetime('now', ?)""",
            (f"-{int(since_days)} days",),
        ).fetchall()

    def _sim(sig: dict, source: str, price, won: bool) -> None:
        if price is None or not (0.0 < float(price) < 1.0):
            return
        cost = min(0.99, float(price) + slippage_cents / 100.0)
        n_ct = max(1, int(round(fixed_usd / max(cost, 0.01))))
        fee = bt.kalshi_fee_per_contract(cost, contracts=n_ct)
        pnl_ct = (1.0 - cost - fee) if won else (-cost - fee)
        trades.append({
            "ticker": sig.get("ticker"), "asset": sig.get("category") or source,
            "side": sig.get("taker_side") or sig.get("direction") or "?",
            "costCents": round(cost * 100, 1), "minsLeft": None,
            "won": bool(won), "pnlUsd": round(pnl_ct * n_ct, 4),
            "contracts": n_ct,
            "at": sig.get("created_at"),
        })

    for r in whales:
        sig = dict(r)
        scanned += 1
        try:
            ok, _why = trader_mod.should_trade(sig, "whale", cfg)
        except Exception:
            ok = False
        if not ok:
            continue
        _sim(sig, "whale", sig.get("price"), bool(sig.get("outcome_correct")))
    for r in alerts:
        sig = dict(r)
        scanned += 1
        try:
            ok, _why = trader_mod.should_trade(sig, "momentum", cfg)
        except Exception:
            ok = False
        if not ok:
            continue
        price = sig.get("price")
        if (sig.get("direction") or "yes").lower() == "no" and price is not None:
            price = 1.0 - float(price)
        _sim(sig, "momentum", price, bool(sig.get("outcome_correct")))

    caveats = [
        f"Follower economics: entry at the signal price +{slippage_cents:.0f}c slippage - live fills on fast markets can be worse.",
        "One simulated trade per accepted signal; live caps (max open, per-event, daily) are NOT applied, so hot events stack correlated trades.",
        "Whale outcomes cluster (one game prints many whale signals) - day/hour buckets share that clustering.",
        "In-sample: signals were only recorded while the app was running.",
        "contrarianOnly and maxResolutionDays are NOT re-simulated: alerts inherit whatever filter was live when they were RECORDED (contrarianOnly gates at record time), and signal rows carry no close_time for the resolution-days gate to read.",
    ]
    return _summarize(trades, contracts, scanned, caveats)


def perp_series(cfg: dict, ticker: str, *, since_days: int = 30,
                env: str = "production") -> dict:
    """Joined perps dataset for one market — the consumable a future
    replay_perps() (and today's research notebooks) reads. Same honesty
    contract as replay(): the caveats state the sampling reality instead of
    letting a 1Hz-coalesced stream masquerade as tick data.

    P&L note: do NOT push perps trades through backtest.summarize — perps fees
    are bps-of-notional (tier 0: 12 taker / 5 maker) plus 8h funding cash
    flows, not the binary settle-to-$1 + 0.07·p·(1−p) model. A perps sim owns
    its own fee/funding math and can then emit {pnlUsd, at, won} dicts into
    _summarize/_bucketize unchanged.
    """
    conn = sqlite3.connect(f"file:{dbmod.db_path()}?mode=ro", uri=True)
    try:
        ticks = bt.load_perp_ticks(conn, ticker, since_days=since_days, env=env)
        trades = bt.load_perp_trades(conn, ticker, since_days=since_days, env=env)
        candles = bt.load_perp_candles(conn, ticker, since_days=since_days, env=env)
        funding = bt.load_perp_funding(conn, ticker, env=env)
        summary = bt.perp_dataset_summary(conn, env)
    finally:
        conn.close()

    med_gap = None
    for t in summary.get("tickers", []):
        if t.get("ticker") == ticker:
            med_gap = t.get("medianTickGapMs")
            break
    caveats = [
        "Ticker stream is WS 1Hz-coalesced per market (latest-wins within the "
        "second) — intra-second quote changes are invisible.",
        "REST snapshot rows (src='rest') land every ~30s and carry no sizes; "
        "they are the baseline when the WS was down.",
        "Recorded during app uptime only — gaps are app-closed periods; "
        "candles (REST top-up) are the only gap-free series.",
        "Demo rows (env='demo', tickers suffixed '1') are order-mechanics "
        "data, not edge data — this loader defaults to production.",
    ]
    if med_gap is not None:
        caveats.append(f"Median WS tick spacing for {ticker}: {med_gap}ms.")
    return {
        "ticker": ticker,
        "env": env,
        "ticks": ticks,
        "trades": trades,
        "candles": candles,
        "funding": funding,
        "caveats": caveats,
    }
