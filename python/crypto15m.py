from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

import kalshi_api
import indicators

logger = logging.getLogger(__name__)


SERIES: list[dict[str, str]] = [
    {"asset": "BTC",  "series": "KXBTC15M",  "cg": "bitcoin"},
    {"asset": "ETH",  "series": "KXETH15M",  "cg": "ethereum"},
    {"asset": "SOL",  "series": "KXSOL15M",  "cg": "solana"},
    {"asset": "XRP",  "series": "KXXRP15M",  "cg": "ripple"},
    {"asset": "DOGE", "series": "KXDOGE15M", "cg": "dogecoin"},
    {"asset": "HYPE", "series": "KXHYPE15M", "cg": "hyperliquid"},
    {"asset": "BNB",  "series": "KXBNB15M",  "cg": "binancecoin"},
]

ALL_ASSETS = [s["asset"] for s in SERIES]


def asset_enabled(cfg: dict, asset: str) -> bool:
    """True if the executor may open NEW positions on `asset`. None/missing
    `crypto15m_assets` = all enabled; a list restricts to those symbols (an
    empty list disables every asset). Monitoring/snapshots are unaffected —
    this only gates entries."""
    raw = (cfg or {}).get("crypto15m_assets")
    if isinstance(raw, list):
        return asset.upper() in {str(a).upper() for a in raw}
    return True  # None / unset → all enabled

_DEFAULTS: dict[str, float] = {
    "time_delay_min": 8.0,
    "entry_threshold": 0.95,
    "exit_threshold": 0.40,
    "entry_max": 0.98,
    "min_delta_pct": 0.0,
    "entry_diff": 0.02,
}

_CRYPTOCOMPARE_URL = "https://min-api.cryptocompare.com/data/pricemulti"
_COINBASE_URL = "https://api.coinbase.com/v2/prices/{sym}-USD/spot"
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
_SPOT_CACHE_TTL = 10.0
_QUARTER_SEC = 15 * 60

_spot_client: Optional[httpx.AsyncClient] = None

_spot_cache: dict = {"at": 0.0, "spots": {}, "source": "none"}

_window_open: dict[tuple[str, int], float] = {}


def _get_spot_client() -> httpx.AsyncClient:
    global _spot_client
    if _spot_client is None or _spot_client.is_closed:
        _spot_client = httpx.AsyncClient(
            timeout=8.0,
            headers={"Accept": "application/json", "User-Agent": "KryptTrader/1.0"},
        )
    return _spot_client


async def close_clients() -> None:
    global _spot_client
    if _spot_client and not _spot_client.is_closed:
        try:
            await _spot_client.aclose()
        except Exception:
            pass
    _spot_client = None


def _const(cfg: dict, key: str) -> float:
    try:
        return float(cfg.get(f"crypto15m_{key}", _DEFAULTS[key]))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _price_dollars(m: dict, key: str) -> float:
    d = m.get(f"{key}_dollars")
    if d is not None:
        return _to_float(d)
    return _to_float(m.get(key)) / 100.0


def _mid_up(yes_bid: float, yes_ask: float, last_price: float) -> float:
    """Up/yes probability (0..1) from quotes. Average ONLY when both sides are
    present — a one-sided book must use the single quote, never (bid+0)/2, which
    halves the probability and trips spurious stop-loss sells / wrong-side rule
    entries. Falls back to last_price when the book is empty."""
    if yes_bid and yes_ask:
        up = (yes_bid + yes_ask) / 2
    elif yes_bid:
        up = yes_bid
    elif yes_ask:
        up = yes_ask
    else:
        up = last_price
    return max(0.0, min(1.0, up))


def _parse_close_epoch(close_time: str) -> Optional[float]:
    if not close_time:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            return ct.timestamp()
        except ValueError:
            continue
    return None


async def _spots_cryptocompare(client: httpx.AsyncClient) -> dict[str, float]:
    syms = ",".join(s["asset"] for s in SERIES)
    resp = await client.get(_CRYPTOCOMPARE_URL, params={"fsyms": syms, "tsyms": "USD"})
    resp.raise_for_status()
    data = resp.json() or {}
    if isinstance(data, dict) and data.get("Response") == "Error":
        raise RuntimeError(str(data.get("Message") or "cryptocompare error"))
    out: dict[str, float] = {}
    for s in SERIES:
        px = (data.get(s["asset"]) or {}).get("USD")
        if px is not None:
            out[s["asset"]] = float(px)
    return out


async def _spots_coinbase(client: httpx.AsyncClient) -> dict[str, float]:
    async def one(s: dict) -> tuple[str, Optional[float]]:
        try:
            r = await client.get(_COINBASE_URL.format(sym=s["asset"]))
            r.raise_for_status()
            amt = ((r.json() or {}).get("data") or {}).get("amount")
            return s["asset"], (float(amt) if amt is not None else None)
        except Exception:
            return s["asset"], None

    results = await asyncio.gather(*[one(s) for s in SERIES])
    return {a: px for a, px in results if px is not None}


async def _spots_coingecko(client: httpx.AsyncClient) -> dict[str, float]:
    ids = ",".join(s["cg"] for s in SERIES)
    resp = await client.get(_COINGECKO_URL, params={"ids": ids, "vs_currencies": "usd"})
    resp.raise_for_status()
    data = resp.json()
    out: dict[str, float] = {}
    for s in SERIES:
        px = (data.get(s["cg"]) or {}).get("usd")
        if px is not None:
            out[s["asset"]] = float(px)
    return out


_SPOT_SOURCES = [
    ("cryptocompare", _spots_cryptocompare),
    ("coinbase", _spots_coinbase),
    ("coingecko", _spots_coingecko),
]


async def fetch_spots() -> tuple[dict[str, float], str]:
    loop = asyncio.get_event_loop()
    now = loop.time()
    if _spot_cache["spots"] and (now - _spot_cache["at"]) < _SPOT_CACHE_TTL:
        return dict(_spot_cache["spots"]), _spot_cache["source"]

    client = _get_spot_client()
    for name, fn in _SPOT_SOURCES:
        try:
            spots = await fn(client)
        except Exception as e:
            logger.debug(f"crypto15m spot source {name} failed: {e}")
            continue
        if spots:
            _spot_cache.update(at=now, spots=dict(spots), source=name)
            return spots, name

    if _spot_cache["spots"]:
        return dict(_spot_cache["spots"]), f"{_spot_cache['source']} (stale)"
    return {}, "unavailable"


# ───────── underlying technical indicators (detection-only) ───────────
#
# MACD/RSI on the underlying's 1-minute closes, surfaced as OPTIONAL rule
# fields + logged by the recorder so backtest.py can measure whether they add
# edge before they ever gate a trade. Candles come from Hyperliquid's public
# `candleSnapshot` (keyless, US-reachable, and it lists every asset we track
# incl. HYPE/BNB). One POST per asset, cached ~60s (candles are 1-minute, so
# polling faster only spends rate limit). Fully degradable: any failure leaves
# the indicator fields None, like a missing market.
_HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
# Minutes of 1-min history to pull — enough warm-up for a stable MACD signal
# line (slow 26 + signal 9 = 35) with comfortable headroom.
_INDICATOR_LOOKBACK_MIN = 90
_INDICATOR_CACHE_TTL = 60.0
# asset -> {"at": loop_time, "data": {macdHist, macdCross, rsi, ...}}
_indicator_cache: dict[str, dict] = {}


async def _fetch_closes(asset: str, client: httpx.AsyncClient, lookback_min: int) -> list[float]:
    """The last `lookback_min` one-minute closes for `asset` from Hyperliquid,
    oldest→newest. Empty list on any error (caller degrades to None fields)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": asset,
            "interval": "1m",
            "startTime": now_ms - int(lookback_min) * 60_000,
            "endTime": now_ms,
        },
    }
    resp = await client.post(_HYPERLIQUID_URL, json=body)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError("unexpected candleSnapshot shape")
    out: list[float] = []
    for r in rows:
        c = r.get("c") if isinstance(r, dict) else None
        if c:
            try:
                out.append(float(c))
            except (TypeError, ValueError):
                continue
    return out


async def asset_indicators(asset: str) -> dict:
    """MACD/RSI bundle for one asset's underlying, cached ~60s and shared by
    the monitor poll, executor tick and recorder. All-None on any failure or
    until there's enough candle history."""
    loop = asyncio.get_event_loop()
    now = loop.time()
    cached = _indicator_cache.get(asset)
    if cached and (now - cached["at"]) < cached.get("ttl", _INDICATOR_CACHE_TTL):
        return cached["data"]
    try:
        closes = await _fetch_closes(asset, _get_spot_client(), _INDICATOR_LOOKBACK_MIN)
        data = indicators.compute(closes)
        _indicator_cache[asset] = {"at": now, "data": data, "ttl": _INDICATOR_CACHE_TTL}
        return data
    except Exception as e:
        logger.debug(f"crypto15m indicators {asset} failed: {e}")
        # Don't blank MACD/RSI rule fields for a full 60s on a transient fetch
        # failure — keep the last good bundle (if any) and retry sooner.
        data = cached["data"] if cached else indicators.compute([])
        _indicator_cache[asset] = {"at": now, "data": data, "ttl": 10.0}
        return data


def _blank_asset(entry: dict, spot: Optional[float], error: Optional[str] = None) -> dict:
    return {
        "asset": entry["asset"], "series": entry["series"],
        "spotUsd": spot, "open15mUsd": None, "deltaUsd": None, "deltaPct": None,
        "hasMarket": False, "ticker": None, "closeTime": None, "minsLeft": None,
        "upProb": None, "downProb": None, "favorite": None,
        "favoritePrice": None, "entryCost": None, "yesBid": None, "yesAsk": None,
        "inWindow": False, "signal": False, "openMarketCount": 0, "error": error,
        # timing + cross-asset correlation (filled in by snapshot()); exposed as
        # optional rule-builder fields, never forced gates.
        "hourUtc": None, "peersAgree": None, "marketBias": None,
        # Up + Down ≠ $1 arbitrage (market-neutral edge), detection-only.
        "upAsk": None, "downAsk": None, "arbEdgeCents": None, "arbSignal": None,
        # underlying technical indicators (detection-only, optional rule fields).
        "macd": None, "macdSignal": None, "macdHist": None,
        "macdCross": None, "rsi": None,
    }


def hours_ok(cfg: dict, hour: Optional[int] = None) -> bool:
    try:
        start = int(cfg.get("crypto15m_hours_start_utc", 0) or 0)
        end = int(cfg.get("crypto15m_hours_end_utc", 24) or 24)
    except (TypeError, ValueError):
        return True
    start, end = start % 24, (end % 24 if end != 24 else 24)
    if start == end or (start == 0 and end == 24):
        return True
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _track_window_open(asset: str, window_start: int, spot: Optional[float]) -> Optional[float]:
    if spot is None:
        return None
    key = (asset, window_start)
    if key not in _window_open:
        _window_open[key] = spot
        if len(_window_open) > 200:
            for old in sorted(_window_open, key=lambda k: k[1])[:50]:
                _window_open.pop(old, None)
    return _window_open[key]


async def _asset_snapshot(entry: dict, spot: Optional[float], cfg: dict, now_epoch: float) -> dict:
    asset, series = entry["asset"], entry["series"]
    try:
        markets, _ = await kalshi_api.fetch_markets(
            status="open", series_ticker=series, limit=200,
        )
    except Exception as e:
        return _blank_asset(entry, spot, f"market fetch failed: {e}")

    candidates: list[tuple[float, dict]] = []
    for m in markets:
        ce = _parse_close_epoch(m.get("close_time", ""))
        if ce is None or ce <= now_epoch:
            continue
        candidates.append((ce, m))

    out = _blank_asset(entry, spot)
    out["openMarketCount"] = len(candidates)
    if not candidates:
        return out

    candidates.sort(key=lambda x: x[0])
    close_epoch, m = candidates[0]

    window_start = int(close_epoch - _QUARTER_SEC)
    open15m = _track_window_open(asset, window_start, spot)
    delta = abs(open15m - spot) if (open15m is not None and spot is not None) else None
    delta_pct = (delta / open15m) if (delta is not None and open15m) else None

    yes_bid = _price_dollars(m, "yes_bid")
    yes_ask = _price_dollars(m, "yes_ask")
    up = _mid_up(yes_bid, yes_ask, _price_dollars(m, "last_price"))
    down = 1.0 - up
    favorite = "up" if up >= down else "down"
    fav_price = up if favorite == "up" else down

    no_ask = _price_dollars(m, "no_ask")
    entry_cost = yes_ask if favorite == "up" else (no_ask if no_ask else 1.0 - yes_bid)
    entry_cost = max(0.0, min(1.0, entry_cost))

    mins_left = (close_epoch - now_epoch) / 60.0
    hour_utc = datetime.fromtimestamp(now_epoch, timezone.utc).hour
    in_window = mins_left <= _const(cfg, "time_delay_min")
    signal = (
        in_window
        and hours_ok(cfg)
        and fav_price >= _const(cfg, "entry_threshold")
        and entry_cost <= _const(cfg, "entry_max")
        and (delta_pct is None or delta_pct >= _const(cfg, "min_delta_pct"))
    )

    out.update({
        "open15mUsd": open15m, "deltaUsd": delta, "deltaPct": delta_pct,
        "hasMarket": True, "ticker": m.get("ticker"),
        "closeTime": m.get("close_time"), "minsLeft": round(mins_left, 2),
        "upProb": round(up, 4), "downProb": round(down, 4),
        "favorite": favorite, "favoritePrice": round(fav_price, 4),
        "entryCost": round(entry_cost, 4),
        "yesBid": round(yes_bid, 4) if yes_bid else None,
        "yesAsk": round(yes_ask, 4) if yes_ask else None,
        "inWindow": in_window, "signal": signal, "hourUtc": hour_utc,
    })

    # Up + Down ≠ $1 arbitrage detection (market-neutral edge). Detection only.
    # On Kalshi a single binary market carries both sides, so if yes_ask + no_ask
    # sums below $1 you could buy both for a locked profit — computed for free
    # from the quotes we already have (no extra API call).
    if cfg.get("crypto15m_arb_detect", True) and yes_ask and no_ask:
        thresh = float(cfg.get("crypto15m_arb_min_edge_cents", 1.0) or 0.0)
        up_ask_c = round(yes_ask * 100.0, 1)
        dn_ask_c = round(no_ask * 100.0, 1)
        edge_c = round(100.0 - (up_ask_c + dn_ask_c), 1)
        out["upAsk"] = round(yes_ask, 4)
        out["downAsk"] = round(no_ask, 4)
        out["arbEdgeCents"] = edge_c
        out["arbSignal"] = edge_c >= thresh

    # Underlying MACD/RSI (record → rule field; does NOT drive entries unless a
    # user composes a rule on macdHist/macdCross/rsi). Cached ~60s per asset.
    # Hard-cap the (non-critical) fetch so a slow Hyperliquid call can't blow the
    # whole asset-snapshot budget; degrades to all-None and retries next pass.
    if cfg.get("crypto15m_indicator_detect", True):
        try:
            ind = await asyncio.wait_for(asset_indicators(asset), 4.0)
        except Exception:
            ind = {}
        out["macd"] = ind.get("macd")
        out["macdSignal"] = ind.get("macdSignal")
        out["macdHist"] = ind.get("macdHist")
        out["macdCross"] = ind.get("macdCross")
        out["rsi"] = ind.get("rsi")
    return out


def _apply_cross_asset(assets: list[dict], now_epoch: float) -> None:
    """Compute timing + cross-asset correlation fields across the whole snapshot
    and inject them per-asset (optional rule-builder fields, never forced gates):

      hourUtc     current UTC hour 0-23 (also stamped on blank/errored assets)
      marketBias  market-wide directional lean = (#up - #down) / #withFavorite,
                  range -1..1; same for every asset (a breadth gauge)
      peersAgree  fraction of the OTHER favorited assets whose favorite matches
                  this asset's, range 0..1 — high = the pack agrees."""
    hour_utc = datetime.fromtimestamp(now_epoch, timezone.utc).hour
    favs = [a.get("favorite") for a in assets
            if a.get("hasMarket") and a.get("favorite") in ("up", "down")]
    n_up = favs.count("up")
    n_down = favs.count("down")
    total = n_up + n_down
    market_bias = round((n_up - n_down) / total, 4) if total else None

    for a in assets:
        a["hourUtc"] = hour_utc
        a["marketBias"] = market_bias
        a["peersAgree"] = None
        fav = a.get("favorite")
        if not a.get("hasMarket") or fav not in ("up", "down") or total <= 1:
            continue
        peers = total - 1
        same = (n_up - 1) if fav == "up" else (n_down - 1)
        a["peersAgree"] = round(same / peers, 4) if peers else None


_snapshot_cache: dict = {"at": 0.0, "data": None}
_SNAPSHOT_TTL = 3.0


async def snapshot(cfg: dict) -> dict:
    """Build the full monitor snapshot for all seven series, then inject the
    cross-asset fields. Cached ~3s so the executor poll, the recorder and the
    open Crypto tab SHARE one snapshot instead of each firing the full fan-out
    (incl. the new Hyperliquid indicator calls) — well inside the poll interval
    and a 15-min window, so the slight staleness is immaterial."""
    now_epoch = datetime.now(timezone.utc).timestamp()
    cached = _snapshot_cache.get("data")
    if cached is not None and (now_epoch - _snapshot_cache.get("at", 0.0)) < _SNAPSHOT_TTL:
        return cached
    try:
        spots, spot_source = await fetch_spots()
    except Exception as e:
        logger.debug(f"crypto15m spot fetch failed: {e}")
        spots, spot_source = {}, "unavailable"
    spot_ok = bool(spots)

    results = await asyncio.gather(
        *[_asset_snapshot(s, spots.get(s["asset"]), cfg, now_epoch) for s in SERIES],
        return_exceptions=True,
    )
    assets: list[dict] = []
    for s, r in zip(SERIES, results):
        assets.append(
            r if not isinstance(r, Exception)
            else _blank_asset(s, spots.get(s["asset"]), str(r))
        )

    _apply_cross_asset(assets, now_epoch)

    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "spotOk": spot_ok,
        "spotSource": spot_source,
        "hoursOk": hours_ok(cfg),
        "constants": {
            "timeDelayMin": _const(cfg, "time_delay_min"),
            "entryThreshold": _const(cfg, "entry_threshold"),
            "exitThreshold": _const(cfg, "exit_threshold"),
            "entryMax": _const(cfg, "entry_max"),
            "minDeltaPct": _const(cfg, "min_delta_pct"),
            "entryDiff": _const(cfg, "entry_diff"),
            "directionMode": str(cfg.get("crypto15m_direction_mode", "favorite")),
            "entryStyle": str(cfg.get("crypto15m_entry_style", "maker")),
            "hoursStartUtc": int(cfg.get("crypto15m_hours_start_utc", 0) or 0),
            "hoursEndUtc": int(cfg.get("crypto15m_hours_end_utc", 24) or 24),
            "indicatorDetect": bool(cfg.get("crypto15m_indicator_detect", True)),
            "arbDetect": bool(cfg.get("crypto15m_arb_detect", True)),
            "useRules": bool(cfg.get("crypto15m_use_rules", False)),
        },
        "assets": assets,
    }
    _snapshot_cache["at"] = now_epoch
    _snapshot_cache["data"] = result
    return result
