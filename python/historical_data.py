from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import db


SQLITE_UTC_FMT = "%Y-%m-%d %H:%M:%S"
_MISSING_TICK_FIELDS = ("yes_bid", "yes_ask", "up_prob", "spot")
_COINBASE_EXCHANGE_BASE = "https://api.exchange.coinbase.com"
_COINBASE_GRANULARITIES = {60, 300, 900, 3600, 21600, 86400}
_KALSHI_BASES = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}
HttpGet = Callable[[str, dict[str, Any]], Any]


def normalize_utc(value: Any) -> str:
    """Normalize common historical timestamp shapes into SQLite UTC text."""
    if value is None:
        raise ValueError("timestamp is required")
    if isinstance(value, (int, float)):
        n = float(value)
        if abs(n) >= 100_000_000_000:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc).strftime(SQLITE_UTC_FMT)

    s = str(value).strip()
    if not s:
        raise ValueError("timestamp is required")
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        if abs(n) >= 100_000_000_000:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc).strftime(SQLITE_UTC_FMT)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "T" not in s and "+" not in s and "-" in s[10:]:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(str(value).strip()[:19], SQLITE_UTC_FMT)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(SQLITE_UTC_FMT)


def _utc_now_or(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sqlite_utc(dt: datetime) -> str:
    return _utc_now_or(dt).strftime(SQLITE_UTC_FMT)


def _iso_z(dt: datetime) -> str:
    return _utc_now_or(dt).isoformat().replace("+00:00", "Z")


def _http_get_json(url: str, params: dict[str, Any]) -> Any:
    clean = {
        k: v for k, v in params.items()
        if v is not None and v != ""
    }
    qs = urllib.parse.urlencode(clean)
    full_url = f"{url}?{qs}" if qs else url
    req = urllib.request.Request(
        full_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Krypt-Trader historical importer",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _first(row: dict, *names: str) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        if name in row and row[name] not in ("", None):
            return row[name]
        key = name.lower()
        if key in lowered and lowered[key] not in ("", None):
            return lowered[key]
    return None


def _float_or_none(value: Any) -> Optional[float]:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    if value in ("", None):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bool_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return 1 if value else 0
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "up", "yes_won"):
        return 1
    if s in ("0", "false", "no", "n", "down", "no_won"):
        return 0
    return _int_or_none(value)


def _rows(path: str | os.PathLike) -> Iterable[dict]:
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def _tick_exists(conn: sqlite3.Connection, ticker: str, observed_at: str, env: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM crypto15m_ticks
           WHERE ticker=? AND observed_at=? AND kalshi_env=? LIMIT 1""",
        (ticker, observed_at, env),
    ).fetchone() is not None


def _upsert_signal(conn: sqlite3.Connection, row: dict, *, env: str) -> None:
    ticker = str(_first(row, "ticker", "market_ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    asset = str(_first(row, "asset", "underlying") or "BTC").strip().upper()
    series = str(_first(row, "series", "series_ticker") or f"KX{asset}15M").strip().upper()
    up_prob = _float_or_none(_first(row, "up_prob", "upProb", "prob_up"))
    yes_ask = _float_or_none(_first(row, "yes_ask", "yesAsk", "up_ask"))
    yes_bid = _float_or_none(_first(row, "yes_bid", "yesBid"))
    no_ask = _float_or_none(_first(row, "no_ask", "noAsk", "down_ask"))
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - yes_bid, 4)
    favorite = str(_first(row, "favorite") or "").lower()
    if favorite not in ("up", "down") and up_prob is not None:
        favorite = "up" if up_prob >= 0.5 else "down"
    favorite_price = _float_or_none(_first(row, "favorite_price", "favoritePrice"))
    if favorite_price is None and up_prob is not None:
        favorite_price = up_prob if favorite == "up" else 1.0 - up_prob
    entry_cost = _float_or_none(_first(row, "entry_cost", "entryCost"))
    if entry_cost is None:
        entry_cost = yes_ask if favorite == "up" else no_ask
    close_time = normalize_utc(_first(row, "close_time", "closeTime", "settlement_time"))
    resolved = _bool_int(_first(row, "resolved"), default=1 if _first(row, "up_won", "upWon") is not None else 0)
    up_won = _bool_int(_first(row, "up_won", "upWon", "result", "winner"))
    conn.execute(
        """INSERT INTO crypto15m_signals (
              ticker, asset, series, close_time, mins_left, favorite,
              favorite_price, entry_cost, up_prob, delta_pct, open_spot,
              obs_spot, macd, macd_signal, macd_hist, macd_cross, rsi,
              ema12_1m, sma20_1m, sma50_5m, strike, model_prob,
              edge_net_cents, resolved, up_won, kalshi_env
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
              asset=excluded.asset,
              series=excluded.series,
              close_time=excluded.close_time,
              mins_left=excluded.mins_left,
              favorite=excluded.favorite,
              favorite_price=excluded.favorite_price,
              entry_cost=excluded.entry_cost,
              up_prob=excluded.up_prob,
              delta_pct=excluded.delta_pct,
              open_spot=excluded.open_spot,
              obs_spot=excluded.obs_spot,
              macd=excluded.macd,
              macd_signal=excluded.macd_signal,
              macd_hist=excluded.macd_hist,
              macd_cross=excluded.macd_cross,
              rsi=excluded.rsi,
              ema12_1m=excluded.ema12_1m,
              sma20_1m=excluded.sma20_1m,
              sma50_5m=excluded.sma50_5m,
              strike=excluded.strike,
              model_prob=excluded.model_prob,
              edge_net_cents=excluded.edge_net_cents,
              resolved=excluded.resolved,
              up_won=excluded.up_won,
              kalshi_env=excluded.kalshi_env""",
        (
            ticker, asset, series, close_time,
            _float_or_none(_first(row, "mins_left", "minsLeft")),
            favorite or None, favorite_price, entry_cost, up_prob,
            _float_or_none(_first(row, "delta_pct", "deltaPct")),
            _float_or_none(_first(row, "open_spot", "openSpot")),
            _float_or_none(_first(row, "spot", "obs_spot", "obsSpot")),
            _float_or_none(_first(row, "macd")),
            _float_or_none(_first(row, "macd_signal", "macdSignal")),
            _float_or_none(_first(row, "macd_hist", "macdHist")),
            _int_or_none(_first(row, "macd_cross", "macdCross")),
            _float_or_none(_first(row, "rsi")),
            _float_or_none(_first(row, "ema12_1m")),
            _float_or_none(_first(row, "sma20_1m")),
            _float_or_none(_first(row, "sma50_5m")),
            _float_or_none(_first(row, "strike", "strike_usd")),
            _float_or_none(_first(row, "model_prob", "modelProb")),
            _float_or_none(_first(row, "edge_net_cents", "edgeNetCents")),
            resolved, up_won, env,
        ),
    )


def _insert_tick(conn: sqlite3.Connection, row: dict, *, env: str) -> bool:
    ticker = str(_first(row, "ticker", "market_ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    asset = str(_first(row, "asset", "underlying") or "BTC").strip().upper()
    observed_at = normalize_utc(_first(row, "observed_at", "timestamp", "ts", "time"))
    if _tick_exists(conn, ticker, observed_at, env):
        return False
    yes_bid = _float_or_none(_first(row, "yes_bid", "yesBid"))
    no_ask = _float_or_none(_first(row, "no_ask", "noAsk", "down_ask"))
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - yes_bid, 4)
    conn.execute(
        """INSERT INTO crypto15m_ticks (
              ticker, asset, observed_at, mins_left, yes_bid, yes_ask, up_prob,
              spot, open_spot, delta_pct, macd, macd_signal, macd_hist,
              macd_cross, rsi, ema12_1m, sma20_1m, sma50_5m, strike,
              delta_signed_pct, sigma1m, model_prob, edge_net_cents,
              settle_prints, no_ask, spot_source, kalshi_env
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ticker, asset, observed_at,
            _float_or_none(_first(row, "mins_left", "minsLeft")),
            yes_bid,
            _float_or_none(_first(row, "yes_ask", "yesAsk", "up_ask")),
            _float_or_none(_first(row, "up_prob", "upProb", "prob_up")),
            _float_or_none(_first(row, "spot", "spotUsd")),
            _float_or_none(_first(row, "open_spot", "openSpot")),
            _float_or_none(_first(row, "delta_pct", "deltaPct")),
            _float_or_none(_first(row, "macd")),
            _float_or_none(_first(row, "macd_signal", "macdSignal")),
            _float_or_none(_first(row, "macd_hist", "macdHist")),
            _int_or_none(_first(row, "macd_cross", "macdCross")),
            _float_or_none(_first(row, "rsi")),
            _float_or_none(_first(row, "ema12_1m")),
            _float_or_none(_first(row, "sma20_1m")),
            _float_or_none(_first(row, "sma50_5m")),
            _float_or_none(_first(row, "strike", "strike_usd")),
            _float_or_none(_first(row, "delta_signed_pct", "deltaSignedPct")),
            _float_or_none(_first(row, "sigma1m")),
            _float_or_none(_first(row, "model_prob", "modelProb")),
            _float_or_none(_first(row, "edge_net_cents", "edgeNetCents")),
            _int_or_none(_first(row, "settle_prints", "settlePrints")),
            no_ask,
            _first(row, "spot_source", "spotSource") or "historical_import",
            env,
        ),
    )
    return True


def import_kalshi_crypto15m_csv(path: str | os.PathLike, *, env: str = "production") -> dict:
    db.init_db()
    seen_signals: set[str] = set()
    ticks_imported = 0
    ticks_skipped = 0
    with db.get_db() as conn:
        for row in _rows(path):
            ticker = str(_first(row, "ticker", "market_ticker") or "").strip().upper()
            if not ticker:
                continue
            if ticker not in seen_signals:
                _upsert_signal(conn, row, env=env)
                seen_signals.add(ticker)
            if _insert_tick(conn, row, env=env):
                ticks_imported += 1
            else:
                ticks_skipped += 1
    return {
        "source": str(path),
        "env": env,
        "signalsUpserted": len(seen_signals),
        "ticksImported": ticks_imported,
        "ticksSkipped": ticks_skipped,
    }


def import_coinbase_candles_csv(
    path: str | os.PathLike, *, asset: str = "BTC", timeframe_sec: int = 60,
    source: str = "coinbase",
) -> dict:
    db.init_db()
    rows = []
    asset = str(asset or "").upper()
    for row in _rows(path):
        ts = normalize_utc(_first(row, "timestamp", "time", "ts", "start"))
        close = _float_or_none(_first(row, "close", "price"))
        if close is None:
            continue
        rows.append({
            "asset": asset,
            "timeframe_sec": int(timeframe_sec),
            "ts": ts,
            "open": _float_or_none(_first(row, "open")) or close,
            "high": _float_or_none(_first(row, "high")) or close,
            "low": _float_or_none(_first(row, "low")) or close,
            "close": close,
            "volume": _float_or_none(_first(row, "volume")),
            "source": source,
        })
    with db.get_db() as conn:
        imported = db.upsert_coinbase_candles(conn, rows)
    return {
        "source": str(path),
        "asset": asset,
        "timeframeSec": int(timeframe_sec),
        "candlesImported": imported,
    }


def download_coinbase_candles(
    *,
    product_id: str = "BTC-USD",
    asset: str = "BTC",
    days: int = 7,
    granularity_sec: int = 60,
    now: Optional[datetime] = None,
    http_get: Optional[HttpGet] = None,
) -> dict:
    """Download Coinbase Exchange product candles into coinbase_candles.

    Coinbase caps each request at 300 candles, so longer ranges are paged by
    time chunk. The optional http_get hook keeps tests deterministic.
    """
    granularity = int(granularity_sec)
    if granularity not in _COINBASE_GRANULARITIES:
        raise ValueError(
            "granularity_sec must be one of "
            + ", ".join(str(x) for x in sorted(_COINBASE_GRANULARITIES))
        )
    product = str(product_id or "").strip().upper()
    if not product:
        raise ValueError("product_id is required")
    asset = str(asset or "").strip().upper()
    if not asset:
        raise ValueError("asset is required")
    end_dt = _utc_now_or(now).replace(microsecond=0)
    start_dt = end_dt - timedelta(days=max(1, int(days)))
    max_span = timedelta(seconds=granularity * 300)
    getter = http_get or _http_get_json
    url = f"{_COINBASE_EXCHANGE_BASE}/products/{urllib.parse.quote(product)}/candles"

    db.init_db()
    candles_imported = 0
    chunks = 0
    first_at: Optional[str] = None
    last_at: Optional[str] = None
    cursor = start_dt
    with db.get_db() as conn:
        while cursor < end_dt:
            chunk_end = min(cursor + max_span, end_dt)
            data = getter(url, {
                "start": _iso_z(cursor),
                "end": _iso_z(chunk_end),
                "granularity": granularity,
            })
            chunks += 1
            if not isinstance(data, list):
                raise ValueError("Coinbase candles response must be a list")
            rows = []
            for item in data:
                if not isinstance(item, (list, tuple)) or len(item) < 6:
                    continue
                ts, low, high, open_, close, volume = item[:6]
                close_f = _float_or_none(close)
                if close_f is None:
                    continue
                ts_text = normalize_utc(ts)
                first_at = ts_text if first_at is None or ts_text < first_at else first_at
                last_at = ts_text if last_at is None or ts_text > last_at else last_at
                rows.append({
                    "asset": asset,
                    "timeframe_sec": granularity,
                    "ts": ts_text,
                    "open": _float_or_none(open_) or close_f,
                    "high": _float_or_none(high) or close_f,
                    "low": _float_or_none(low) or close_f,
                    "close": close_f,
                    "volume": _float_or_none(volume),
                    "source": "coinbase",
                })
            candles_imported += db.upsert_coinbase_candles(conn, rows)
            cursor = chunk_end
    return {
        "source": "coinbase",
        "productId": product,
        "asset": asset,
        "granularitySec": granularity,
        "days": max(1, int(days)),
        "chunks": chunks,
        "candlesImported": candles_imported,
        "firstAt": first_at,
        "lastAt": last_at,
    }


def _kalshi_price(value: Any) -> Optional[float]:
    n = _float_or_none(value)
    if n is None:
        return None
    # Kalshi candlesticks may expose raw cents (62) or dollars/probability (0.62).
    if abs(n) > 1.0 and abs(n) <= 100.0:
        n /= 100.0
    return n


def _kalshi_close(candle: dict, field: str) -> Optional[float]:
    obj = candle.get(field)
    if not isinstance(obj, dict):
        return _kalshi_price(obj)
    for key in (
        "close_dollars", "mean_dollars", "previous_dollars",
        "close", "mean", "previous",
        "open_dollars", "open",
    ):
        v = _kalshi_price(obj.get(key))
        if v is not None:
            return v
    return None


def download_kalshi_market_candles(
    *,
    ticker: str,
    series_ticker: str = "",
    asset: str = "BTC",
    env: str = "production",
    days: int = 7,
    period_interval: int = 1,
    historical: bool = False,
    now: Optional[datetime] = None,
    http_get: Optional[HttpGet] = None,
) -> dict:
    """Download Kalshi market candlesticks into crypto15m quote history."""
    market = str(ticker or "").strip().upper()
    if not market:
        raise ValueError("ticker is required")
    series = str(series_ticker or "").strip().upper()
    if not historical and not series:
        raise ValueError("series_ticker is required for the live Kalshi route")
    asset = str(asset or "").strip().upper() or "BTC"
    env = str(env or "production").strip().lower()
    if env not in _KALSHI_BASES:
        raise ValueError("env must be production or demo")
    interval = int(period_interval)
    if interval not in (1, 60, 1440):
        raise ValueError("period_interval must be 1, 60, or 1440")
    end_dt = _utc_now_or(now).replace(microsecond=0)
    start_dt = end_dt - timedelta(days=max(1, int(days)))
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    base = _KALSHI_BASES[env]
    if historical:
        url = f"{base}/historical/markets/{urllib.parse.quote(market)}/candlesticks"
    else:
        url = (
            f"{base}/series/{urllib.parse.quote(series)}/markets/"
            f"{urllib.parse.quote(market)}/candlesticks"
        )

    getter = http_get or _http_get_json
    data = getter(url, {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": interval,
    })
    if not isinstance(data, dict):
        raise ValueError("Kalshi candlesticks response must be an object")
    candles = data.get("candlesticks")
    if not isinstance(candles, list):
        raise ValueError("Kalshi candlesticks response is missing candlesticks")

    db.init_db()
    ticks_imported = 0
    ticks_skipped = 0
    first_at: Optional[str] = None
    last_at: Optional[str] = None
    signal_row = {
        "ticker": market,
        "asset": asset,
        "series": series or str(data.get("series_ticker") or "").upper() or f"KX{asset}15M",
        "close_time": _sqlite_utc(end_dt),
        "resolved": 0,
    }
    with db.get_db() as conn:
        _upsert_signal(conn, signal_row, env=env)
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            ts = candle.get("end_period_ts") or candle.get("ts") or candle.get("time")
            if ts in ("", None):
                continue
            observed_at = normalize_utc(ts)
            first_at = observed_at if first_at is None or observed_at < first_at else first_at
            last_at = observed_at if last_at is None or observed_at > last_at else last_at
            yes_bid = _kalshi_close(candle, "yes_bid")
            yes_ask = _kalshi_close(candle, "yes_ask")
            up_prob = _kalshi_close(candle, "price")
            if up_prob is None:
                up_prob = yes_ask if yes_ask is not None else yes_bid
            tick_row = {
                "ticker": market,
                "asset": asset,
                "observed_at": observed_at,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "up_prob": up_prob,
                "spot_source": "kalshi_candlesticks",
            }
            if _insert_tick(conn, tick_row, env=env):
                ticks_imported += 1
            else:
                ticks_skipped += 1
    return {
        "source": "kalshi",
        "ticker": market,
        "seriesTicker": series,
        "asset": asset,
        "env": env,
        "historical": bool(historical),
        "periodInterval": interval,
        "days": max(1, int(days)),
        "signalsUpserted": 1,
        "candlesImported": ticks_imported,
        "candlesSkipped": ticks_skipped,
        "firstAt": first_at,
        "lastAt": last_at,
    }


def _parse_sqlite_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value)[:19], SQLITE_UTC_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _gap_report(rows: list[dict], *, key: str, max_gap_seconds: int) -> tuple[int, list[dict]]:
    gaps: list[dict] = []
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(str(r.get(key) or ""), []).append(r)
    for group, items in by_key.items():
        last_ts: Optional[datetime] = None
        last_raw: Optional[str] = None
        for r in sorted(items, key=lambda x: str(x.get("observed_at") or x.get("ts") or "")):
            raw = str(r.get("observed_at") or r.get("ts") or "")
            ts = _parse_sqlite_ts(raw)
            if ts and last_ts:
                gap = int((ts - last_ts).total_seconds())
                if gap > max_gap_seconds:
                    gaps.append({
                        key: group,
                        "from": last_raw,
                        "to": raw,
                        "gapSeconds": gap,
                    })
            if ts:
                last_ts = ts
                last_raw = raw
    gaps.sort(key=lambda g: int(g["gapSeconds"]), reverse=True)
    return len(gaps), gaps[:10]


def validate_dataset(
    conn: sqlite3.Connection, *, env: str = "production", since_days: int = 60,
    max_gap_seconds: int = 90,
) -> dict:
    cutoff_expr = f"datetime('now', '-{max(1, int(since_days))} days')"
    c15_rows = conn.execute(
        f"""SELECT t.*, s.resolved, s.up_won
            FROM crypto15m_ticks t
            LEFT JOIN crypto15m_signals s
              ON s.ticker=t.ticker AND s.kalshi_env=t.kalshi_env
            WHERE t.kalshi_env=? AND t.observed_at >= {cutoff_expr}
            ORDER BY t.ticker, t.observed_at""",
        (env,),
    ).fetchall()
    ticks = [dict(r) for r in c15_rows]
    missing = {
        field: sum(1 for r in ticks if r.get(field) is None)
        for field in _MISSING_TICK_FIELDS
    }
    bad_ts = sum(1 for r in ticks if _parse_sqlite_ts(r.get("observed_at")) is None)
    gap_count, largest_gaps = _gap_report(ticks, key="ticker", max_gap_seconds=max_gap_seconds)
    windows = {str(r.get("ticker") or "") for r in ticks if r.get("ticker")}
    resolved = {str(r.get("ticker") or "") for r in ticks if r.get("up_won") is not None}

    candle_rows = conn.execute(
        """SELECT asset, timeframe_sec, ts, close FROM coinbase_candles
           ORDER BY asset, timeframe_sec, ts"""
    ).fetchall()
    by_asset: dict[str, list[dict]] = {}
    for row in candle_rows:
        d = dict(row)
        by_asset.setdefault(str(d.get("asset") or ""), []).append(d)
    coinbase_assets = {}
    for asset, rows in by_asset.items():
        c_gaps, c_largest = _gap_report(
            rows, key="asset", max_gap_seconds=max(90, max_gap_seconds),
        )
        coinbase_assets[asset] = {
            "candles": len(rows),
            "firstAt": rows[0]["ts"] if rows else None,
            "lastAt": rows[-1]["ts"] if rows else None,
            "gapCount": c_gaps,
            "largestGaps": c_largest,
        }

    return {
        "env": env,
        "sinceDays": int(since_days),
        "crypto15m": {
            "ticks": len(ticks),
            "windows": len(windows),
            "resolvedWindows": len(resolved),
            "unresolvedWindows": max(0, len(windows) - len(resolved)),
            "firstAt": min((r["observed_at"] for r in ticks if r.get("observed_at")), default=None),
            "lastAt": max((r["observed_at"] for r in ticks if r.get("observed_at")), default=None),
            "missingFields": missing,
            "badTimestamps": bad_ts,
            "gapCount": gap_count,
            "largestGaps": largest_gaps,
        },
        "coinbase": {
            "assets": coinbase_assets,
        },
    }


def export_tables_parquet(
    out_dir: str | os.PathLike, *, tables: Optional[list[str]] = None,
) -> dict:
    """Write large-history tables to Parquet when pyarrow is installed."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except Exception as e:
        return {
            "ok": False,
            "reason": f"pyarrow unavailable: {e}",
            "files": [],
        }
    db.init_db()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tables = tables or [
        "crypto15m_signals", "crypto15m_ticks", "coinbase_candles",
        "perp_ticks", "perp_trades", "perp_candles", "perp_funding",
    ]
    files = []
    with db.get_db() as conn:
        for table in tables:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
            path = out / f"{table}.parquet"
            pq.write_table(pa.Table.from_pylist(rows), path)
            files.append(str(path))
    return {"ok": True, "dir": str(out), "files": files}


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Import and validate Krypt historical research data")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("kalshi-c15", help="import historical 15m Kalshi CSV rows")
    k.add_argument("csv")
    k.add_argument("env_pos", nargs="?")
    k.add_argument("--env", default="production")

    c = sub.add_parser("coinbase-candles", help="import Coinbase spot candle CSV rows")
    c.add_argument("csv")
    c.add_argument("asset_pos", nargs="?")
    c.add_argument("timeframe_pos", nargs="?")
    c.add_argument("--asset", default="BTC")
    c.add_argument("--timeframe-sec", type=int, default=60)

    v = sub.add_parser("validate", help="print dataset quality report")
    v.add_argument("since_days_pos", nargs="?")
    v.add_argument("--env", default="production")
    v.add_argument("--since-days", type=int, default=60)
    v.add_argument("--max-gap-seconds", type=int, default=90)

    p = sub.add_parser("export-parquet", help="export large research tables as Parquet")
    p.add_argument("out_dir")
    p.add_argument("--tables", nargs="*")

    args = ap.parse_args(argv)
    if args.cmd == "kalshi-c15":
        _print_json(import_kalshi_crypto15m_csv(args.csv, env=args.env_pos or args.env))
    elif args.cmd == "coinbase-candles":
        timeframe = int(args.timeframe_pos or args.timeframe_sec)
        _print_json(import_coinbase_candles_csv(
            args.csv, asset=args.asset_pos or args.asset, timeframe_sec=timeframe,
        ))
    elif args.cmd == "validate":
        db.init_db()
        since_days = int(args.since_days_pos or args.since_days)
        with db.get_db() as conn:
            _print_json(validate_dataset(
                conn, env=args.env, since_days=since_days,
                max_gap_seconds=args.max_gap_seconds,
            ))
    elif args.cmd == "export-parquet":
        _print_json(export_tables_parquet(args.out_dir, tables=args.tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
