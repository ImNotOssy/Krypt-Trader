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

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import backtest as bt
import capturetrail as ct
import crypto15m
import crypto15m_trader
import db as dbmod
import historical_data
import indicators

_DATASET_SCHEMA_VERSION = 1
_DATASET_DIGEST_COLUMNS = (
    "ticker", "asset", "observed_at", "mins_left",
    "yes_bid", "yes_ask", "up_prob", "spot", "open_spot", "delta_pct",
    "macd", "macd_signal", "macd_hist", "macd_cross", "rsi",
    "ema12_1m", "sma20_1m", "sma50_5m",
    "strike", "delta_signed_pct", "sigma1m", "model_prob",
    "edge_net_cents", "settle_prints", "no_ask", "spot_source",
    "kalshi_env", "up_won", "sig_close", "replay_in_sample",
)

_DERIVABLE = {
    "hasMarket", "favorite", "favoritePrice", "entryCost", "minsLeft",
    "inWindow", "signal", "modelProb", "edgeNetCents", "settlePrints",
    "upAsk", "downAsk", "yesBid", "yesAsk", "upProb", "downProb",
    "deltaPct", "deltaSignedPct", "sigma1m", "spotUsd", "strikeUsd",
    "macd", "macdSignal", "macdHist", "macdCross", "rsi",
    "ema12_1m", "sma20_1m", "sma50_5m", "hourUtc",
    "closeTime", "ticker", "asset", "series",
}


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _parse_observed_at(value) -> Optional[datetime]:
    s = str(value or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s[:26] if "%f" in fmt else s[:19], fmt)
        except ValueError:
            continue
    return None


def _row_float(row: dict, key: str) -> Optional[float]:
    try:
        v = row.get(key)
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _floor_bucket(ts: datetime, minutes: int) -> datetime:
    minutes = max(1, int(minutes))
    return ts.replace(minute=(ts.minute // minutes) * minutes, second=0, microsecond=0)


class _ReplayMaState:
    """Replay-only completed-candle builder from recorded spot ticks."""

    def __init__(self, cfg: dict):
        self.fast_ema = max(1, _cfg_int(cfg, "crypto15m_fast_ema_period", 12))
        self.slow_sma = max(1, _cfg_int(cfg, "crypto15m_slow_sma_period", 20))
        self.trend_sma = max(1, _cfg_int(cfg, "crypto15m_trend_sma_period", 50))
        self.trend_tf = max(1, _cfg_int(cfg, "crypto15m_trend_timeframe_min", 5))
        self._minute_bucket: Optional[datetime] = None
        self._minute_last: Optional[float] = None
        self._trend_bucket: Optional[datetime] = None
        self._trend_last: Optional[float] = None
        self.closes_1m: list[float] = []
        self.closes_trend: list[float] = []

    def update(self, ts: Optional[datetime], spot: Optional[float]) -> None:
        if ts is None or spot is None:
            return
        minute_bucket = _floor_bucket(ts, 1)
        if self._minute_bucket is None:
            self._minute_bucket = minute_bucket
        elif minute_bucket != self._minute_bucket:
            if self._minute_last is not None:
                self.closes_1m.append(self._minute_last)
            self._minute_bucket = minute_bucket
        self._minute_last = spot

        trend_bucket = _floor_bucket(ts, self.trend_tf)
        if self._trend_bucket is None:
            self._trend_bucket = trend_bucket
        elif trend_bucket != self._trend_bucket:
            if self._trend_last is not None:
                self.closes_trend.append(self._trend_last)
            self._trend_bucket = trend_bucket
        self._trend_last = spot

    def prime_candles(self, candles: list[dict]) -> None:
        """Seed completed 1m and trend-timeframe closes from imported candles."""
        trend_bucket: Optional[datetime] = None
        trend_last: Optional[float] = None
        for candle in candles:
            close = _row_float(candle, "close")
            ts = _parse_observed_at(candle.get("ts"))
            if close is None or ts is None:
                continue
            self.closes_1m.append(close)
            bucket = _floor_bucket(ts, self.trend_tf)
            if trend_bucket is None:
                trend_bucket = bucket
            elif bucket != trend_bucket:
                if trend_last is not None:
                    self.closes_trend.append(trend_last)
                trend_bucket = bucket
            trend_last = close
        if trend_last is not None:
            self.closes_trend.append(trend_last)

    def snapshot(self) -> dict:
        one_n = len(self.closes_1m)
        trend_n = len(self.closes_trend)
        data = {
            "ema12_1m": indicators.ema_latest(self.closes_1m, self.fast_ema),
            "sma20_1m": indicators.sma(self.closes_1m, self.slow_sma),
            "sma50_5m": indicators.sma(self.closes_trend, self.trend_sma),
        }
        if data["ema12_1m"] is None and one_n < self.fast_ema:
            data["ema12_1m_reason"] = "insufficient 1m candles"
        if data["sma20_1m"] is None and one_n < self.slow_sma:
            data["sma20_1m_reason"] = "insufficient 1m candles"
        if data["sma50_5m"] is None and trend_n < self.trend_sma:
            data["sma50_5m_reason"] = "insufficient 5m candles"
        return data


def _apply_replay_ma(row: dict, state: _ReplayMaState) -> None:
    spot = _row_float(row, "spot")
    if spot is None:
        row["spot_reason"] = "spot unavailable"
        return
    state.update(_parse_observed_at(row.get("observed_at")), spot)
    snap = state.snapshot()
    for key in ("ema12_1m", "sma20_1m", "sma50_5m"):
        if row.get(key) is None:
            row[key] = snap.get(key)
            reason = snap.get(f"{key}_reason")
            if row.get(key) is None and reason:
                row[f"{key}_reason"] = reason


def tick_to_asset(row: dict, cfg: dict, close_iso: str) -> dict:
    """Map a crypto15m_ticks row onto the exact snapshot field names the live
    gates read. Missing-in-ticks fields (peersAgree, marketBias, arbEdgeCents)
    are deliberately absent so rule evaluation fails closed, same as live."""
    up_prob = row.get("up_prob")
    yes_bid, yes_ask = row.get("yes_bid"), row.get("yes_ask")
    no_ask = row.get("no_ask")
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - float(yes_bid), 4)
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
        "secondsLeft": float(ml) * 60.0 if ml is not None else None,
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
        "spotUsdReason": row.get("spot_reason"),
        "macd": row.get("macd"), "macdSignal": row.get("macd_signal"),
        "macdHist": row.get("macd_hist"), "macdCross": row.get("macd_cross"),
        "rsi": row.get("rsi"),
        "ema12_1m": row.get("ema12_1m"),
        "sma20_1m": row.get("sma20_1m"),
        "sma50_5m": row.get("sma50_5m"),
        "ema12_1m_reason": row.get("ema12_1m_reason"),
        "sma20_1m_reason": row.get("sma20_1m_reason"),
        "sma50_5m_reason": row.get("sma50_5m_reason"),
    }


def _missing_rule_fields(cfg: dict) -> list[str]:
    if not cfg.get("crypto15m_use_rules"):
        return []
    fields = {str(c.get("field")) for c in (cfg.get("crypto15m_rules") or [])}
    return sorted(fields - _DERIVABLE)


def _rejection_label(why: str, cfg: dict) -> str:
    text = str(why or "rejected")
    if "outside" in text and "c" in text:
        return "entry price outside range"
    if text.startswith("seconds_left"):
        min_left = int(cfg.get("crypto15m_min_entry_seconds_left", 120) or 120)
        return f"under {min_left} seconds"
    return text


def _count_rejection(rejections: dict[str, int], why: str, cfg: dict) -> None:
    label = _rejection_label(why, cfg)
    rejections[label] = rejections.get(label, 0) + 1


def _safe_pct(numer: Optional[float], denom: Optional[float]) -> Optional[float]:
    if numer is None or denom in (None, 0):
        return None
    try:
        return float(numer) / float(denom)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _cents(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return round(float(v) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def _row_dict(row) -> dict:
    return dict(row)


def _dataset_date_token(ts: Optional[str]) -> str:
    s = str(ts or "")
    if len(s) >= 10:
        return s[:10].replace("-", "")
    return "empty"


def _dataset_manifest(
    rows, *, env: str, since_days: int, replay_mode: str,
    validation: Optional[dict] = None,
) -> dict:
    row_dicts = [_row_dict(r) for r in rows]
    timestamps = sorted(
        str(r.get("observed_at") or "") for r in row_dicts if r.get("observed_at")
    )
    first_ts = timestamps[0] if timestamps else None
    last_ts = timestamps[-1] if timestamps else None
    in_sample = [
        r for r in row_dicts
        if int(r.get("replay_in_sample") or 0)
    ]
    windows = sorted({
        str(r.get("ticker") or "")
        for r in in_sample
        if r.get("ticker") and r.get("up_won") is not None
    })
    assets = sorted({
        str(r.get("asset") or "").upper()
        for r in row_dicts
        if r.get("asset")
    })
    canonical_rows = [
        {col: r.get(col) for col in _DATASET_DIGEST_COLUMNS}
        for r in row_dicts
    ]
    payload = {
        "schemaVersion": _DATASET_SCHEMA_VERSION,
        "source": "crypto15m_ticks",
        "env": env,
        "sinceDays": int(since_days),
        "replayMode": replay_mode,
        "columns": list(_DATASET_DIGEST_COLUMNS),
        "rows": canonical_rows,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    sha = hashlib.sha256(encoded).hexdigest()
    date_part = (
        f"{_dataset_date_token(first_ts)}-{_dataset_date_token(last_ts)}"
        if first_ts and last_ts
        else "empty"
    )
    return {
        "schemaVersion": _DATASET_SCHEMA_VERSION,
        "datasetId": f"crypto15m-{env}-{date_part}-{sha[:12]}",
        "source": "crypto15m_ticks",
        "env": env,
        "sinceDays": int(since_days),
        "firstTimestamp": first_ts,
        "lastTimestamp": last_ts,
        "rowCount": len(row_dicts),
        "inSampleRowCount": len(in_sample),
        "windows": len(windows),
        "assets": assets,
        "replayMode": replay_mode,
        "sha256": sha,
        "digestColumns": list(_DATASET_DIGEST_COLUMNS),
        "validation": validation or {},
    }


def _trade_row(
    *, ticker: str, asset: dict, side: str, cost: float, contracts: int,
    fee_per_contract: float, won: int, up_won: int, pnl_ct: float, at,
) -> dict:
    spot = _row_float(asset, "spotUsd")
    ema = _row_float(asset, "ema12_1m")
    sma20 = _row_float(asset, "sma20_1m")
    sma50 = _row_float(asset, "sma50_5m")
    entry_side = "YES" if side == "up" else "NO"
    signal_type = "bullish" if side == "up" else "bearish"
    entry_cents = round(cost * 100.0, 2)
    exit_cents = 100.0 if won else 0.0
    fees_usd = round(fee_per_contract * contracts, 4)
    seconds_left = asset.get("secondsLeft")
    try:
        seconds_left = float(seconds_left) if seconds_left is not None else None
    except (TypeError, ValueError):
        seconds_left = None
    return {
        "ticker": ticker,
        "asset": asset.get("asset"),
        "side": side,
        "entrySide": entry_side,
        "signalType": signal_type,
        "secondsLeft": seconds_left,
        "yesAskCents": _cents(asset.get("upAsk")),
        "noAskCents": _cents(asset.get("downAsk")),
        "costCents": round(cost * 100.0, 1),
        "entryPriceCents": entry_cents,
        "contracts": contracts,
        "spotUsd": spot,
        "ema12_1m": ema,
        "sma20_1m": sma20,
        "sma50_5m": sma50,
        "emaSpreadPct": _safe_pct((ema - sma20) if ema is not None and sma20 is not None else None, sma20),
        "trendDistancePct": _safe_pct((spot - sma50) if spot is not None and sma50 is not None else None, sma50),
        "exitReason": "settlement",
        "exitPriceCents": exit_cents,
        "entryFeeUsd": fees_usd,
        "exitFeeUsd": 0.0,
        "feesUsd": fees_usd,
        "minsLeft": asset["minsLeft"],
        "won": bool(won),
        "upWon": bool(up_won),
        "pnlUsd": round(pnl_ct * contracts, 4),
        "at": at,
        "timeline": [
            f"{at or ''} - {signal_type} conditions true",
            f"{at or ''} - {entry_side} ask {entry_cents:.0f}c",
            f"{at or ''} - simulated entry filled at recorded ask",
            f"Settlement - held side paid {exit_cents:.0f}c",
        ],
    }


def replay(cfg: dict, *, env: str = "production", since_days: int = 60,
           max_concurrent_ignored: bool = True) -> dict:
    """Run the live entry gate over recorded ticks, one trade max per window,
    entered at the first qualifying tick's executable ask, held to settlement.
    Returns aggregate stats + per-asset breakdown + honesty caveats."""
    cfg = dict(cfg)
    cfg["crypto15m_enabled"] = True
    cfg["crypto15m_model_autopause"] = False
    contracts = max(1, int(cfg.get("crypto15m_order_size") or 1))
    ma_mode = crypto15m_trader.is_btc_ma_crossover(cfg)
    since_mod = f"-{int(since_days)} days"
    coinbase_warmup: list[dict] = []
    validation: dict = {}
    with dbmod.get_db() as conn:
        if ma_mode:
            rows = conn.execute(
                """SELECT t.*, s.up_won, s.close_time AS sig_close,
                          CASE WHEN t.observed_at >= datetime('now', ?)
                               THEN 1 ELSE 0 END AS replay_in_sample
                   FROM crypto15m_ticks t
                   LEFT JOIN crypto15m_signals s
                     ON s.ticker = t.ticker AND s.kalshi_env = t.kalshi_env
                   WHERE t.kalshi_env = ?
                     AND UPPER(t.asset) = 'BTC'
                     AND t.observed_at >= datetime('now', ?, '-6 hours')
                     AND (
                       t.observed_at < datetime('now', ?)
                       OR (s.resolved = 1 AND s.up_won IS NOT NULL)
                     )
                   ORDER BY t.observed_at, t.ticker""",
                (since_mod, env, since_mod, since_mod),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.*, s.up_won, s.close_time AS sig_close,
                          1 AS replay_in_sample
                   FROM crypto15m_ticks t
                   JOIN crypto15m_signals s
                     ON s.ticker = t.ticker AND s.kalshi_env = t.kalshi_env
                   WHERE s.resolved = 1 AND s.up_won IS NOT NULL
                     AND t.kalshi_env = ?
                     AND t.observed_at >= datetime('now', ?)
                   ORDER BY t.ticker, t.observed_at""",
                (env, since_mod),
            ).fetchall()
        validation = historical_data.validate_dataset(
            conn, env=env, since_days=since_days,
        )
        if ma_mode:
            in_sample_times = [
                _parse_observed_at(r["observed_at"])
                for r in rows
                if int(r["replay_in_sample"] or 0)
            ]
            in_sample_times = [t for t in in_sample_times if t is not None]
            has_tick_warmup = any(not int(r["replay_in_sample"] or 0) for r in rows)
            if in_sample_times and not has_tick_warmup:
                first = min(in_sample_times)
                start = (first - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                end = first.strftime("%Y-%m-%d %H:%M:%S")
                coinbase_warmup = dbmod.load_coinbase_candle_closes(
                    conn, "BTC", start=start, end=end, timeframe_sec=60,
                )

    dataset = _dataset_manifest(
        rows,
        env=env,
        since_days=since_days,
        replay_mode="btc_ma_crossover" if ma_mode else "directional",
        validation=validation,
    )

    by_window: dict[str, list[dict]] = {}
    for r in rows:
        by_window.setdefault(r["ticker"], []).append(dict(r))

    trades: list[dict] = []
    rejections: dict[str, int] = {}
    n_windows = 0
    if ma_mode:
        state = _ReplayMaState(cfg)
        if coinbase_warmup:
            state.prime_candles(coinbase_warmup)
        scanned: set[str] = set()
        finished: set[str] = set()
        for row in rows:
            t = dict(row)
            _apply_replay_ma(t, state)
            if not int(t.get("replay_in_sample") or 0):
                continue
            if t.get("up_won") is None:
                continue
            ticker = str(t.get("ticker") or "")
            if ticker in finished:
                continue
            if not crypto15m.asset_enabled(cfg, str(t.get("asset") or "")):
                continue
            scanned.add(ticker)
            up_won = int(t.get("up_won") or 0)
            close_iso = str(t.get("sig_close") or "")
            asset = tick_to_asset(t, cfg, close_iso)
            try:
                ok, why = crypto15m_trader.should_enter(
                    asset, cfg, has_open=False, open_count=0,
                )
            except Exception as e:
                ok = False
                why = f"should_enter error: {type(e).__name__}"
            if not ok:
                _count_rejection(rejections, why, cfg)
                continue
            side = crypto15m_trader._bought_side(asset, cfg)
            if side not in ("up", "down"):
                _count_rejection(rejections, "entry side unavailable", cfg)
                finished.add(ticker)
                continue
            cost = asset["upAsk"] if side == "up" else asset["downAsk"]
            if not cost or not (0.0 < float(cost) < 1.0):
                _count_rejection(rejections, f"{side} ask unavailable", cfg)
                finished.add(ticker)
                continue
            cost = float(cost)
            fee = bt.kalshi_fee_per_contract(cost, contracts=contracts)
            won = up_won if side == "up" else (1 - up_won)
            pnl_ct = (1.0 - cost - fee) if won else (-cost - fee)
            trades.append(_trade_row(
                ticker=ticker, asset=asset, side=side, cost=cost,
                contracts=contracts, fee_per_contract=fee, won=won,
                up_won=up_won, pnl_ct=pnl_ct, at=t.get("observed_at"),
            ))
            finished.add(ticker)
        n_windows = len(scanned)
    else:
        for ticker, ticks in by_window.items():
            if not crypto15m.asset_enabled(cfg, str(ticks[0].get("asset") or "")):
                continue
            n_windows += 1
            up_won = int(ticks[0].get("up_won") or 0)
            close_iso = str(ticks[0].get("sig_close") or "")
            for t in ticks:
                asset = tick_to_asset(t, cfg, close_iso)
                try:
                    ok, why = crypto15m_trader.should_enter(
                        asset, cfg, has_open=False, open_count=0,
                    )
                except Exception as e:
                    ok = False
                    why = f"should_enter error: {type(e).__name__}"
                if not ok:
                    _count_rejection(rejections, why, cfg)
                    continue
                side = crypto15m_trader._bought_side(asset, cfg)
                if side not in ("up", "down"):
                    _count_rejection(rejections, "entry side unavailable", cfg)
                    break
                cost = asset["upAsk"] if side == "up" else asset["downAsk"]
                if not cost or not (0.0 < float(cost) < 1.0):
                    _count_rejection(rejections, f"{side} ask unavailable", cfg)
                    break
                cost = float(cost)
                fee = bt.kalshi_fee_per_contract(cost, contracts=contracts)
                won = up_won if side == "up" else (1 - up_won)
                pnl_ct = (1.0 - cost - fee) if won else (-cost - fee)
                trades.append(_trade_row(
                    ticker=ticker, asset=asset, side=side, cost=cost,
                    contracts=contracts, fee_per_contract=fee, won=won,
                    up_won=up_won, pnl_ct=pnl_ct, at=t.get("observed_at"),
                ))
                break

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
    return _summarize(
        trades, contracts, n_windows, caveats,
        rejections=rejections, dataset=dataset,
    )


def _held_bid(row: dict, side: str) -> Optional[float]:
    """Held-side taker-sell price (bid) from a tick, 0..1. Up sells YES at
    yes_bid; down sells NO at no_bid = 1 - yes_ask (single complementary book).
    None when the quote needed isn't recorded, so the caller skips that tick
    instead of marking to a fabricated price."""
    if side == "up":
        b = row.get("yes_bid")
        return float(b) if b and 0.0 < float(b) < 1.0 else None
    ya = row.get("yes_ask")
    if ya and 0.0 < float(ya) < 1.0:
        return round(1.0 - float(ya), 4)
    return None


def _simulate_capturetrail(ticks: list[dict], entry_i: int, side: str,
                           cost: float, params: ct.CTParams,
                           contracts: int) -> Optional[dict]:
    """Walk the ticks AFTER entry, marking the held side to its bid, and let
    CaptureTrail decide the exit. Returns the exit leg {price, at, reason} or
    None if it never fired (caller then holds to settlement).

    entry_mark is the ask we PAID (cost); the mark each tick is the bid we
    could SELL at — so the position is honestly down the spread from the first
    tick, the trail tracks the bid, and the exit books at the bid."""
    state = ct.CTState.open(cost)
    for row in ticks[entry_i + 1:]:
        bid = _held_bid(row, side)
        if bid is None:
            continue
        done, reason = ct.step(state, bid, params)
        if done:
            return {"price": bid, "at": row.get("observed_at"), "reason": reason}
    return None


def _max_drawdown(trades: list[dict]) -> float:
    run = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: str(x.get("at") or "")):
        run += float(t.get("pnlUsd") or 0.0)
        peak = max(peak, run)
        max_dd = min(max_dd, run - peak)
    return max_dd


def _group_summary(trades: list[dict], contracts: int) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t.get("won"))
    total = sum(float(t.get("pnlUsd") or 0.0) for t in trades)
    denom = sum(int(t.get("contracts", contracts) or contracts) for t in trades)
    return {
        "n": n,
        "wins": wins,
        "winRate": round(wins / n, 4) if n else 0.0,
        "pnlUsd": round(total, 4),
        "avgPnlUsd": round(total / n, 4) if n else 0.0,
        "edgeCentsPerContract": round(total / denom * 100.0, 2) if denom else 0.0,
        "maxDrawdownUsd": round(_max_drawdown(trades), 4),
    }


def _bucket_summary(trades: list[dict], contracts: int, buckets: list[tuple[str, float, float]], key: str) -> list[dict]:
    out = []
    for label, lo, hi in buckets:
        rows = []
        for t in trades:
            try:
                v = float(t.get(key))
            except (TypeError, ValueError):
                continue
            if lo <= v < hi:
                rows.append(t)
        out.append({"label": label, **_group_summary(rows, contracts)})
    return out


def _entry_price_buckets(trades: list[dict], contracts: int) -> list[dict]:
    return _bucket_summary(trades, contracts, [
        ("5-14c", 5, 15),
        ("15-24c", 15, 25),
        ("25-34c", 25, 35),
        ("35-44c", 35, 45),
        ("45-50c", 45, 51),
        ("51-59c", 51, 60),
    ], "entryPriceCents")


def _time_left_buckets(trades: list[dict], contracts: int) -> list[dict]:
    return _bucket_summary(trades, contracts, [
        ("12-15m", 12, 15.000001),
        ("9-12m", 9, 12),
        ("6-9m", 6, 9),
        ("4-6m", 4, 6),
        ("2-4m", 2, 4),
    ], "minsLeft")


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _streaks(trades: list[dict]) -> tuple[int, int]:
    win_run = loss_run = max_win = max_loss = 0
    for t in sorted(trades, key=lambda x: str(x.get("at") or "")):
        if t.get("won"):
            win_run += 1
            loss_run = 0
        else:
            loss_run += 1
            win_run = 0
        max_win = max(max_win, win_run)
        max_loss = max(max_loss, loss_run)
    return max_win, max_loss


def _trade_stats(trades: list[dict]) -> dict:
    pnls = [float(t.get("pnlUsd") or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    max_win_streak, max_loss_streak = _streaks(trades)
    max_dd = abs(_max_drawdown(trades))
    total = sum(pnls)
    return {
        "longestWinningStreak": max_win_streak,
        "longestLosingStreak": max_loss_streak,
        "averageWinUsd": round(gross_win / len(wins), 6) if wins else 0.0,
        "averageLossUsd": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "profitFactor": (gross_win / gross_loss) if gross_loss else (None if gross_win == 0 else 999999.0),
        "medianTradeUsd": round(_median(pnls), 6),
        "largestWinUsd": round(max(wins), 6) if wins else 0.0,
        "largestLossUsd": round(min(losses), 6) if losses else 0.0,
        "recoveryFactor": (total / max_dd) if max_dd else (None if total == 0 else 999999.0),
    }


def replay_capturetrail(cfg: dict, *, env: str = "production",
                        since_days: int = 60) -> dict:
    """Head-to-head: the SAME live entries, booked two ways.

    baseline    — held to settlement (what the live 15m trader does today).
    capturetrail — CaptureTrail's trailing exit walks the window's remaining
                   ticks; if it fires, the trade books at the held-side bid
                   with BOTH an entry and an exit fee; if not, it falls through
                   to settlement identically to the baseline.

    Returns {baseline, capturetrail, delta, params} so the UI/CLI can show the
    difference on the user's own recorded ticks. Same honesty caveats as
    replay() plus the exit-sim ones."""
    cfg = dict(cfg)
    cfg["crypto15m_enabled"] = True
    cfg["crypto15m_model_autopause"] = False
    params = ct.params_from_cfg(cfg, "crypto15m")
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

    base_trades: list[dict] = []
    ct_trades: list[dict] = []
    n_windows = 0
    n_ct_exits = 0
    reasons: dict[str, int] = {}
    for ticker, ticks in by_window.items():
        if not crypto15m.asset_enabled(cfg, str(ticks[0].get("asset") or "")):
            continue
        n_windows += 1
        up_won = int(ticks[0].get("up_won") or 0)
        close_iso = str(ticks[0].get("sig_close") or "")
        for i, t in enumerate(ticks):
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
            fee_in = bt.kalshi_fee_per_contract(cost, contracts=contracts)
            won = up_won if side == "up" else (1 - up_won)

            base_pnl = (1.0 - cost - fee_in) if won else (-cost - fee_in)
            base_trades.append({
                "ticker": ticker, "asset": asset["asset"], "side": side,
                "costCents": round(cost * 100, 1), "minsLeft": asset["minsLeft"],
                "won": bool(won), "pnlUsd": round(base_pnl * contracts, 4),
                "at": t.get("observed_at"),
            })

            exit_leg = _simulate_capturetrail(ticks, i, side, cost, params, contracts)
            if exit_leg is not None:
                px = float(exit_leg["price"])
                fee_out = bt.kalshi_fee_per_contract(px, contracts=contracts)
                ct_pnl = px - cost - fee_in - fee_out
                n_ct_exits += 1
                reasons[exit_leg["reason"]] = reasons.get(exit_leg["reason"], 0) + 1
                ct_trades.append({
                    "ticker": ticker, "asset": asset["asset"], "side": side,
                    "costCents": round(cost * 100, 1), "minsLeft": asset["minsLeft"],
                    "won": ct_pnl > 0, "pnlUsd": round(ct_pnl * contracts, 4),
                    "at": t.get("observed_at"), "reason": exit_leg["reason"],
                    "exitCents": round(px * 100, 1),
                })
            else:
                ct_trades.append({**base_trades[-1], "reason": "settled"})
            break

    base_caveats = [
        "Baseline holds every position to settlement (the live 15m default).",
        f"Ticks are 4-25s apart over {since_days} days of app uptime only.",
        "In-sample: any threshold tuned against this panel is fit to the past.",
    ]
    ct_caveats = [
        "CaptureTrail marks to the held-side BID (yes_bid / 1-yes_ask) — the "
        "position is down the spread from tick 1, and exits book at the bid.",
        "The trail only sees recorded ticks (4-25s apart); a real reversal "
        "between ticks would fill worse. Both legs pay Kalshi's per-order fee.",
        f"CaptureTrail exited {n_ct_exits} of {len(ct_trades)} trades early; "
        f"reasons: {reasons or 'none'}. The rest settled identically to baseline.",
    ] + base_caveats
    if not params.active():
        ct_caveats.insert(0, (
            "CaptureTrail is OFF or has no active trigger in this config — the "
            "two columns are identical. Set crypto15m_ct_enabled + a reversal_pct."
        ))

    base = _summarize(base_trades, contracts, n_windows, base_caveats)
    capt = _summarize(ct_trades, contracts, n_windows, ct_caveats)
    return {
        "baseline": base,
        "capturetrail": capt,
        "delta": {
            "totalPnlUsd": round(capt["totalPnlUsd"] - base["totalPnlUsd"], 2),
            "netEvCentsPerContract": round(
                capt["netEvCentsPerContract"] - base["netEvCentsPerContract"], 2),
            "winRate": round(capt["winRate"] - base["winRate"], 4),
            "maxDrawdownUsd": round(capt["maxDrawdownUsd"] - base["maxDrawdownUsd"], 2),
            "earlyExits": n_ct_exits,
            "exitReasons": reasons,
        },
        "params": {
            "enabled": params.enabled, "minArmPct": params.min_arm_pct,
            "unarmedStopPct": params.unarmed_stop_pct,
            "reversalPct": params.reversal_pct, "noisePct": params.noise_pct,
            "override": params.override,
        },
    }


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
               caveats: list[str], *, rejections: Optional[dict[str, int]] = None,
               dataset: Optional[dict] = None) -> dict:
    trades = sorted(trades, key=lambda t: str(t.get("at") or ""))
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total = sum(t["pnlUsd"] for t in trades)
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
    rejection_counts = dict(sorted(
        (rejections or {}).items(),
        key=lambda kv: (-kv[1], kv[0]),
    ))
    rejection_total = sum(rejection_counts.values())
    rejection_breakdown = [
        {
            "reason": reason,
            "count": count,
            "pct": round(count / rejection_total, 4) if rejection_total else 0.0,
        }
        for reason, count in rejection_counts.items()
    ]
    if n == 0 and rejection_counts:
        top = ", ".join(f"{reason}: {count}" for reason, count in list(rejection_counts.items())[:5])
        caveats = [f"No entries matched. Top rejection reasons: {top}."] + list(caveats)
    if n < 30:
        caveats = [f"Only {n} trades - far too few for a verdict; treat as anecdote."] + list(caveats)
    out = {
        "n": n, "wins": wins,
        "winRate": round(wins / n, 4) if n else 0.0,
        "netEvCentsPerContract": round(ev_ct, 2),
        "totalPnlUsd": round(total, 2),
        "maxDrawdownUsd": round(max_dd, 2),
        "contracts": contracts,
        "windowsScanned": n_windows,
        "byAsset": by_asset,
        "rejections": rejection_counts,
        "rejectionTotal": rejection_total,
        "rejectionBreakdown": rejection_breakdown,
        "bySide": {
            "YES": _group_summary([t for t in trades if t.get("entrySide") == "YES" or t.get("side") == "up"], contracts),
            "NO": _group_summary([t for t in trades if t.get("entrySide") == "NO" or t.get("side") == "down"], contracts),
        },
        "entryPriceBuckets": _entry_price_buckets(trades, contracts),
        "timeLeftBuckets": _time_left_buckets(trades, contracts),
        "tradeStats": _trade_stats(trades),
        "equity": equity[-400:],
        "trades": trades,
        "caveats": caveats,
        **_bucketize(trades),
    }
    if dataset is not None:
        out["dataset"] = dataset
    return out


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
