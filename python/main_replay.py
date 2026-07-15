"""Main-engine replay and analysis pipeline.

Replays recorded whale prints and momentum alerts through the LIVE
``trader.should_trade`` gates, then produces the deep analytics that
the 15-minute crypto system already has:

* Rejection funnel with structured primary+secondary reasons
* Trade-level rows with 9 field groups
* Category × signal-source breakdown
* Edge and confidence calibration
* Entry-price / resolution-horizon / liquidity buckets
* Event-concentration analysis
* Position-sizing comparison (constant vs. actual)
* Execution-model simulation
* Time anatomy (hour, day, signal age)

This module is intentionally separate from ``replay.py`` (which owns
crypto15m) — different signal shapes, different gate sets, different
optimisation grids.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

import backtest as bt
import db as dbmod
from categorize import categorize_by_keywords, KALSHI_CATEGORY_MAP_CI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FEE_COEFF = bt.DEFAULT_FEE_COEFF

_NORMALIZED_CATEGORIES = {
    "sports", "politics", "economics", "crypto",
    "climate", "entertainment", "world", "exotics", "unknown",
}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _s(v: Any) -> str:
    return str(v) if v is not None else ""


def _first_present(*values: Any) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _bool_or_none(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = _s(v).strip().lower()
    if s in {"1", "true", "yes", "y"}:
        return True
    if s in {"0", "false", "no", "n"}:
        return False
    return None


def _momentum_favorite_status(direction: str, yes_price: float) -> str:
    if yes_price <= 0 or yes_price >= 1:
        return "unknown"
    side = (_s(direction) or "yes").lower()
    side_prob = yes_price if side == "yes" else (1.0 - yes_price)
    return "favorite" if side_prob >= 0.5 else "underdog"


def _optional_float(*values: Any) -> Optional[float]:
    raw = _first_present(*values)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _normalize_category(raw: str) -> str:
    low = (raw or "").strip().lower()
    if low in _NORMALIZED_CATEGORIES:
        return low
    mapped = KALSHI_CATEGORY_MAP_CI.get(low)
    if mapped:
        return mapped
    return "unknown" if not low else low


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s[:26] if "%f" in fmt else s[:19], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _days_until_close(close_time: str) -> Optional[float]:
    dt = _parse_iso(close_time)
    if dt is None:
        return None
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0)


def _signal_age_sec(created_at: str) -> Optional[float]:
    """How old the signal was at the instant we evaluate it (now)."""
    dt = _parse_iso(created_at)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


# ---------------------------------------------------------------------------
# Rejection reasons — canonical enum strings
# ---------------------------------------------------------------------------

REJECTION_REASONS = [
    "category_disabled",
    "signal_source_disabled",
    "below_edge_threshold",
    "below_confidence_threshold",
    "entry_price_below_floor",
    "entry_price_above_ceiling",
    "signal_too_old",
    "tape_trade_too_old",
    "slippage_too_high",
    "market_volume_too_low",
    "resolution_too_far",
    "momentum_not_contrarian",
    "max_positions_reached",
    "per_event_cap_reached",
    "daily_cap_reached",
    "exposure_cap_reached",
    "cash_reserve_requirement",
    "daily_stop_reached",
    "duplicate_position",
    "quote_unavailable",
    "signal_type_not_allowed",
    "crypto15m_series",
    "other",
]


def _classify_rejection(reason: str) -> str:
    """Map a human-readable trader.should_trade reason string to a
    canonical rejection enum for the funnel."""
    r = (reason or "").lower()
    if "disabled" in r and ("whale" in r or "momentum" in r or "convergence" in r):
        return "signal_source_disabled"
    if "categor" in r and ("not in" in r or "no cat" in r or "disabled" in r):
        return "category_disabled"
    if "edge" in r or "net edge" in r:
        return "below_edge_threshold"
    if "conf" in r:
        return "below_confidence_threshold"
    if "entry" in r and ("<" in r or "below" in r or "floor" in r):
        return "entry_price_below_floor"
    if "entry" in r and (">" in r or "above" in r or "ceiling" in r):
        return "entry_price_above_ceiling"
    if "signal_type" in r or "not allowed" in r:
        return "signal_type_not_allowed"
    if "volume" in r:
        return "market_volume_too_low"
    if "resolv" in r or "days" in r:
        return "resolution_too_far"
    if "crypto15m" in r:
        return "crypto15m_series"
    if "duplicate" in r or "already open" in r:
        return "duplicate_position"
    if "max_open" in r or "max open" in r:
        return "max_positions_reached"
    if "per-event" in r or "per_event" in r:
        return "per_event_cap_reached"
    if "daily" in r and "new" in r:
        return "daily_cap_reached"
    if "slippage" in r or "book moved" in r:
        return "slippage_too_high"
    if "exposure" in r:
        return "exposure_cap_reached"
    if "reserve" in r:
        return "cash_reserve_requirement"
    if "stop" in r or "take-profit" in r:
        return "daily_stop_reached"
    if "quote" in r or "unavailable" in r or "ask" in r:
        return "quote_unavailable"
    return "other"


# ---------------------------------------------------------------------------
# Signal-cost helpers (same logic as trader.py)
# ---------------------------------------------------------------------------

def _signal_cost_cents(sig: dict, source: str) -> tuple[str, int]:
    """Direction and cost-in-cents for a signal (mirrors trader._signal_cost_cents)."""
    if source == "whale":
        direction = (_s(sig.get("taker_side")) or "yes").lower()
        price_frac = _f(sig.get("price"))
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    direction = (_s(sig.get("direction")) or "yes").lower()
    yes_frac = _f(sig.get("price"))
    yes_cents = max(1, min(99, int(round(yes_frac * 100))))
    if direction == "yes":
        return direction, yes_cents
    return direction, max(1, min(99, 100 - yes_cents))


def _compute_edge(sig: dict, source: str) -> float:
    conf = _f(sig.get("confidence"))
    if source == "whale":
        implied = _f(sig.get("price")) * 100
    else:
        direction = (_s(sig.get("direction")) or "yes").lower()
        yes = _f(sig.get("price"))
        implied = (yes if direction == "yes" else (1.0 - yes)) * 100
    return conf - implied


def _taker_fee_cents(price_cents: int) -> float:
    p = max(1, min(99, int(price_cents))) / 100.0
    return 7.0 * p * (1.0 - p)


def _net_edge(sig: dict, source: str, cfg: dict) -> float:
    edge = _compute_edge(sig, source)
    if cfg.get("fee_aware_edge", True):
        _, cost_cents = _signal_cost_cents(sig, source)
        edge -= _taker_fee_cents(cost_cents)
    return edge


# ---------------------------------------------------------------------------
# Enrichment — attach market metadata to signals
# ---------------------------------------------------------------------------

def _enrich_signal(conn, sig: dict, source: str) -> dict:
    """Return an enriched copy of the signal with market metadata for
    downstream analytics (close_time, volume, OI, bid/ask, etc.)."""
    sig = dict(sig)  # shallow copy
    ticker = sig.get("ticker") or ""

    # Get market data
    market = None
    try:
        row = conn.execute(
            "SELECT * FROM markets WHERE ticker=?", (ticker,)
        ).fetchone()
        if row:
            market = dict(row)
    except Exception:
        pass

    if market:
        if not sig.get("close_time"):
            sig["close_time"] = market.get("close_time") or ""
        if not sig.get("market_volume") and not sig.get("volume_24h"):
            sig["market_volume"] = market.get("volume") or 0
        sig["_yes_bid"] = _f(market.get("yes_bid"))
        sig["_yes_ask"] = _f(market.get("yes_ask"))
        sig["_open_interest"] = _f(market.get("open_interest"))
        sig["_market_title"] = market.get("title") or ""
        sig["_event_ticker"] = market.get("event_ticker") or sig.get("event_ticker") or ""
        sig["_settlement_value"] = market.get("settlement_value")
        sig["_result"] = market.get("result") or ""
    else:
        sig.setdefault("_yes_bid", 0.0)
        sig.setdefault("_yes_ask", 0.0)
        sig.setdefault("_open_interest", 0.0)
        sig.setdefault("_market_title", sig.get("title") or "")
        sig.setdefault("_event_ticker", sig.get("event_ticker") or "")
        sig.setdefault("_settlement_value", None)
        sig.setdefault("_result", "")

    # Normalize category
    raw_cat = sig.get("category") or ""
    if not raw_cat and sig.get("_market_title"):
        raw_cat = categorize_by_keywords(sig["_market_title"])
    sig["_raw_category"] = raw_cat
    sig["_normalized_category"] = _normalize_category(raw_cat)

    return sig


# ---------------------------------------------------------------------------
# Rejection record builder
# ---------------------------------------------------------------------------

def _build_rejection(sig: dict, source: str, reason: str, cfg: dict) -> dict:
    """Build a structured rejection record for the funnel."""
    _, cost_cents = _signal_cost_cents(sig, source)
    return {
        "timestamp": sig.get("created_at") or "",
        "source": source,
        "ticker": sig.get("ticker") or "",
        "event_ticker": sig.get("_event_ticker") or sig.get("event_ticker") or "",
        "market_title": sig.get("_market_title") or sig.get("title") or "",
        "category": sig.get("_normalized_category") or "",
        "raw_category": sig.get("_raw_category") or "",
        "confidence": _f(sig.get("confidence")),
        "raw_edge": round(_compute_edge(sig, source), 2),
        "net_edge": round(_net_edge(sig, source, cfg), 2),
        "entry_price_cents": cost_cents,
        "primary_rejection": _classify_rejection(reason),
        "rejection_detail": reason,
        "market_volume": _f(sig.get("market_volume") or sig.get("volume_24h") or 0),
        "whale_dollar_size": _f(sig.get("dollar_value")) if source == "whale" else None,
    }


# ---------------------------------------------------------------------------
# Trade row builder — 9 field groups
# ---------------------------------------------------------------------------

def _build_trade_row(
    sig: dict, source: str, cfg: dict,
    cost_cents: int, direction: str, won: bool,
    contracts: int, slippage_cents: float,
) -> dict:
    """Build a rich trade row with all 9 field groups."""
    cost = min(0.99, cost_cents / 100.0 + slippage_cents / 100.0)
    fee = bt.kalshi_fee_per_contract(cost, contracts=contracts)
    pnl_ct = (1.0 - cost - fee) if won else (-cost - fee)
    pnl_usd = round(pnl_ct * contracts, 4)
    entry_cost_usd = round(cost * contracts, 4)
    raw_edge = round(_compute_edge(sig, source), 2)
    net_edge = round(_net_edge(sig, source, cfg), 2)
    yes_bid = _f(sig.get("_yes_bid"))
    yes_ask = _f(sig.get("_yes_ask"))
    spread_cents = round((yes_ask - yes_bid) * 100, 1) if yes_ask > yes_bid else 0
    close_time = sig.get("close_time") or ""
    days_until = _days_until_close(close_time)

    # Whale details
    whale_details = {}
    if source == "whale":
        whale_details = {
            "whale_dollar_size": _f(sig.get("dollar_value")),
            "whale_taker_side": _s(sig.get("taker_side")),
            "whale_entry_price": _f(sig.get("price")),
            "whale_count_fp": _f(sig.get("count_fp")),
        }

    # Momentum details
    momentum_details = {}
    if source == "momentum":
        sig_type = _s(sig.get("signal_type"))
        cluster_size = _optional_float(
            sig.get("cluster_size"),
            sig.get("cluster_count"),
            sig.get("trade_cluster_count"),
            sig.get("alert_count"),
        )
        cluster_duration_sec = _optional_float(
            sig.get("cluster_duration_sec"),
            sig.get("cluster_duration"),
            sig.get("cluster_span_sec"),
            sig.get("time_span_sec"),
        )
        favorite_status = _momentum_favorite_status(_s(sig.get("direction")), _f(sig.get("price")))
        contrarian = _bool_or_none(_first_present(
            sig.get("contrarian"),
            sig.get("is_contrarian"),
            sig.get("momentum_contrarian"),
        ))
        if contrarian is None and favorite_status != "unknown":
            contrarian = favorite_status == "underdog"
        momentum_details = {
            "signal_type": sig_type,
            "momentum_direction": _s(sig.get("direction")),
            "price_change": _f(sig.get("price_change")),
            "cluster_size": int(cluster_size) if cluster_size is not None else None,
            "cluster_duration_sec": round(cluster_duration_sec, 2) if cluster_duration_sec is not None else None,
            "momentum_contrarian": contrarian,
            "momentum_favorite_status": favorite_status,
        }

    return {
        # Identity
        "timestamp": sig.get("created_at") or "",
        "ticker": sig.get("ticker") or "",
        "event_ticker": sig.get("_event_ticker") or sig.get("event_ticker") or "",
        "market_title": sig.get("_market_title") or sig.get("title") or "",
        "category": sig.get("_normalized_category") or "",
        "raw_category": sig.get("_raw_category") or "",
        # Signal
        "source": source,
        "side": direction,
        "confidence": _f(sig.get("confidence")),
        "raw_edge": raw_edge,
        "net_edge": net_edge,
        # Whale details
        **whale_details,
        # Momentum details
        **momentum_details,
        # Market
        "yes_bid_cents": round(yes_bid * 100, 1),
        "yes_ask_cents": round(yes_ask * 100, 1),
        "spread_cents": spread_cents,
        "entry_price_cents": round(cost * 100, 1),
        "market_volume": _f(sig.get("market_volume") or sig.get("volume_24h") or 0),
        "open_interest": _f(sig.get("_open_interest")),
        # Timing
        "signal_age_sec": None,  # historical: not available in replay
        "days_until_resolution": round(days_until, 2) if days_until is not None else None,
        "close_time": close_time,
        # Execution
        "slippage_cents": slippage_cents,
        "simulated_fill_cents": round(cost * 100, 1),
        "fee_per_contract": round(fee, 4),
        # Position
        "contracts": contracts,
        "cost_usd": entry_cost_usd,
        "fees_usd": round(fee * contracts, 4),
        # Result
        "won": won,
        "settlement": 1.0 if won else 0.0,
        "pnl_usd": pnl_usd,
        "return_on_risk": round(pnl_usd / entry_cost_usd, 4) if entry_cost_usd > 0 else 0.0,
        # Recharts-friendly aliases
        "at": sig.get("created_at") or "",
        "pnlUsd": pnl_usd,
        "costCents": round(cost * 100, 1),
        "minsLeft": None,
        "asset": sig.get("_normalized_category") or source,
    }


# ---------------------------------------------------------------------------
# Bucket analysis functions
# ---------------------------------------------------------------------------

def _group_metrics(trades: list[dict]) -> dict:
    """Compute summary metrics for a group of trades."""
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "wins": 0, "winRate": 0.0, "totalPnlUsd": 0.0,
            "avgPnlUsd": 0.0, "avgEdge": 0.0, "realizedEdge": 0.0,
            "profitFactor": None, "maxDrawdownUsd": 0.0,
            "recoveryFactor": None, "avgCostCents": 0.0,
            "feesUsd": 0.0,
        }
    wins = sum(1 for t in trades if t.get("won"))
    total = sum(_f(t.get("pnl_usd")) for t in trades)
    fees = sum(_f(t.get("fees_usd")) for t in trades)
    avg_edge = sum(_f(t.get("net_edge")) for t in trades) / n
    pnls = [_f(t.get("pnl_usd")) for t in trades]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else 999999.0)
    # Max drawdown
    run, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: _s(x.get("at"))):
        run += _f(t.get("pnl_usd"))
        peak = max(peak, run)
        max_dd = min(max_dd, run - peak)
    dd = abs(max_dd)
    rf = (total / dd) if dd > 0 else (None if total == 0 else 999999.0)
    avg_cost = sum(_f(t.get("costCents")) for t in trades) / n
    # Realized edge = average net pnl per contract in cents
    total_contracts = sum(_i(t.get("contracts") or 1) for t in trades)
    realized = (total / total_contracts * 100.0) if total_contracts > 0 else 0.0

    return {
        "n": n,
        "wins": wins,
        "winRate": round(wins / n, 4),
        "totalPnlUsd": round(total, 2),
        "avgPnlUsd": round(total / n, 4),
        "avgEdge": round(avg_edge, 2),
        "realizedEdge": round(realized, 2),
        "profitFactor": round(pf, 2) if pf is not None and pf < 999999 else pf,
        "maxDrawdownUsd": round(max_dd, 2),
        "recoveryFactor": round(rf, 2) if rf is not None and rf < 999999 else rf,
        "avgCostCents": round(avg_cost, 1),
        "feesUsd": round(fees, 4),
    }


def _category_analysis(trades: list[dict]) -> dict:
    """Category performance split by source."""
    by_cat: dict[str, list[dict]] = {}
    by_cat_src: dict[str, dict[str, list[dict]]] = {}
    for t in trades:
        cat = t.get("category") or "unknown"
        src = t.get("source") or "unknown"
        by_cat.setdefault(cat, []).append(t)
        by_cat_src.setdefault(cat, {}).setdefault(src, []).append(t)

    combined = {cat: _group_metrics(rows) for cat, rows in sorted(by_cat.items())}
    whale_only = {}
    momentum_only = {}
    for cat, sources in sorted(by_cat_src.items()):
        if "whale" in sources:
            whale_only[cat] = _group_metrics(sources["whale"])
        if "momentum" in sources:
            momentum_only[cat] = _group_metrics(sources["momentum"])

    return {
        "combined": combined,
        "whaleOnly": whale_only,
        "momentumOnly": momentum_only,
    }


def _source_analysis(trades: list[dict]) -> dict:
    """Signal-source breakdown: whale vs momentum."""
    whale = [t for t in trades if t.get("source") == "whale"]
    momentum = [t for t in trades if t.get("source") == "momentum"]

    def numeric_buckets(rows: list[dict], definitions: list[tuple[str, float, float]], key: str) -> list[dict]:
        out = []
        for label, lo, hi in definitions:
            bucket = []
            for t in rows:
                v = _optional_float(t.get(key))
                if v is not None and lo <= v < hi:
                    bucket.append(t)
            out.append({"label": label, **_group_metrics(bucket)})
        return out

    def label_buckets(rows: list[dict], labels: list[str], label_fn) -> list[dict]:
        out = []
        for label in labels:
            bucket = [t for t in rows if label_fn(t) == label]
            out.append({"label": label, **_group_metrics(bucket)})
        return out

    # Whale size buckets
    whale_size_buckets = []
    size_ranges = [
        ("$500–$999", 500, 1000),
        ("$1,000–$2,499", 1000, 2500),
        ("$2,500–$4,999", 2500, 5000),
        ("$5,000–$9,999", 5000, 10000),
        ("$10,000–$24,999", 10000, 25000),
        ("$25,000+", 25000, 1e12),
    ]
    for label, lo, hi in size_ranges:
        bucket = [t for t in whale if lo <= _f(t.get("whale_dollar_size")) < hi]
        whale_size_buckets.append({"label": label, **_group_metrics(bucket)})

    cluster_size_buckets = numeric_buckets(momentum, [
        ("1", 1, 2),
        ("2-3", 2, 4),
        ("4-6", 4, 7),
        ("7-10", 7, 11),
        ("11+", 11, 1e12),
    ], "cluster_size")

    cluster_duration_buckets = numeric_buckets(momentum, [
        ("Under 15s", 0, 15),
        ("15-60s", 15, 60),
        ("1-3m", 60, 180),
        ("3-10m", 180, 600),
        ("10m+", 600, 1e12),
    ], "cluster_duration_sec")

    def contrarian_label(t: dict) -> str:
        b = _bool_or_none(_first_present(t.get("momentum_contrarian"), t.get("contrarian")))
        if b is True:
            return "Contrarian"
        if b is False:
            return "Continuation"
        return "Unknown"

    def direction_label(t: dict) -> str:
        direction = (_s(_first_present(t.get("momentum_direction"), t.get("side"))) or "").lower()
        if direction == "yes":
            return "YES"
        if direction == "no":
            return "NO"
        return "Unknown"

    momentum_contrarian_buckets = label_buckets(
        momentum, ["Contrarian", "Continuation", "Unknown"], contrarian_label
    )
    momentum_direction_buckets = label_buckets(
        momentum, ["YES", "NO", "Unknown"], direction_label
    )

    return {
        "whale": _group_metrics(whale),
        "momentum": _group_metrics(momentum),
        "combined": _group_metrics(trades),
        "whaleSizeBuckets": whale_size_buckets,
        "momentumClusterSizeBuckets": cluster_size_buckets,
        "momentumClusterDurationBuckets": cluster_duration_buckets,
        "momentumContrarianBuckets": momentum_contrarian_buckets,
        "momentumDirectionBuckets": momentum_direction_buckets,
    }


def _entry_price_analysis(trades: list[dict]) -> list[dict]:
    """10-cent entry price buckets with calibration."""
    buckets = []
    for lo_c in range(1, 100, 10):
        hi_c = lo_c + 9
        label = f"{lo_c}–{hi_c}¢"
        rows = [t for t in trades if lo_c <= _f(t.get("costCents")) <= hi_c]
        m = _group_metrics(rows)
        n = m["n"]
        avg_cost = m["avgCostCents"]
        # Breakeven win rate after fees
        if avg_cost > 0 and avg_cost < 100:
            cost_frac = avg_cost / 100.0
            fee_frac = bt.kalshi_fee_per_contract(cost_frac)
            breakeven = (cost_frac + fee_frac)
        else:
            breakeven = 0.5
        calibration_margin = (m["winRate"] - breakeven) if n > 0 else 0.0
        buckets.append({
            "label": label,
            "lo": lo_c,
            "hi": hi_c,
            **m,
            "breakevenWinRate": round(breakeven, 4),
            "calibrationMargin": round(calibration_margin, 4),
        })
    return buckets


def _edge_calibration(trades: list[dict]) -> list[dict]:
    """Predicted vs realized edge by bucket."""
    buckets_def = [
        ("0–1.9 pts", 0, 2),
        ("2–3.9 pts", 2, 4),
        ("4–5.9 pts", 4, 6),
        ("6–7.9 pts", 6, 8),
        ("8–9.9 pts", 8, 10),
        ("10–14.9 pts", 10, 15),
        ("15+ pts", 15, 200),
    ]
    out = []
    for label, lo, hi in buckets_def:
        rows = [t for t in trades if lo <= _f(t.get("net_edge")) < hi]
        m = _group_metrics(rows)
        avg_predicted_edge = sum(_f(t.get("net_edge")) for t in rows) / len(rows) if rows else 0
        # Predicted win rate from confidence
        avg_confidence = sum(_f(t.get("confidence")) for t in rows) / len(rows) if rows else 0
        out.append({
            "label": label,
            "lo": lo, "hi": hi,
            **m,
            "avgPredictedEdge": round(avg_predicted_edge, 2),
            "avgConfidence": round(avg_confidence, 1),
            "predictedWinRate": round(avg_confidence / 100.0, 4) if avg_confidence > 0 else 0.0,
        })
    return out


def _confidence_calibration(trades: list[dict]) -> list[dict]:
    """Predicted confidence vs actual outcomes."""
    buckets_def = [
        ("40–49%", 40, 50),
        ("50–54%", 50, 55),
        ("55–59%", 55, 60),
        ("60–64%", 60, 65),
        ("65–69%", 65, 70),
        ("70–79%", 70, 80),
        ("80%+", 80, 101),
    ]
    out = []
    for label, lo, hi in buckets_def:
        rows = [t for t in trades if lo <= _f(t.get("confidence")) < hi]
        m = _group_metrics(rows)
        avg_conf = sum(_f(t.get("confidence")) for t in rows) / len(rows) if rows else 0
        out.append({
            "label": label,
            "lo": lo, "hi": hi,
            **m,
            "avgPredictedConfidence": round(avg_conf, 1),
            "predictedWinRate": round(avg_conf / 100.0, 4) if avg_conf > 0 else 0.0,
        })
    return out


def _resolution_analysis(trades: list[dict]) -> list[dict]:
    """Resolution-horizon buckets with capital efficiency."""
    buckets_def = [
        ("Under 1 hour", 0, 1/24),
        ("1–6 hours", 1/24, 6/24),
        ("6–24 hours", 6/24, 1),
        ("1–3 days", 1, 3),
        ("3–7 days", 3, 7),
        ("7–14 days", 7, 14),
        ("14–30 days", 14, 30),
        ("30+ days", 30, 9999),
    ]
    out = []
    for label, lo, hi in buckets_def:
        rows = [t for t in trades
                if t.get("days_until_resolution") is not None
                and lo <= _f(t.get("days_until_resolution")) < hi]
        m = _group_metrics(rows)
        # Capital efficiency = pnl / (dollars_risked * holding_days)
        total_capital_days = sum(
            _f(t.get("cost_usd")) * max(0.001, _f(t.get("days_until_resolution")))
            for t in rows
        )
        cap_efficiency = (m["totalPnlUsd"] / total_capital_days) if total_capital_days > 0 else 0.0
        out.append({
            "label": label,
            "lo": round(lo, 4), "hi": round(hi, 4),
            **m,
            "capitalEfficiency": round(cap_efficiency, 4),
            "totalCapitalDays": round(total_capital_days, 2),
        })
    return out


def _liquidity_analysis(trades: list[dict]) -> dict:
    """Volume, spread, and open interest buckets."""
    vol_buckets_def = [
        ("0–99", 0, 100),
        ("100–499", 100, 500),
        ("500–999", 500, 1000),
        ("1,000–4,999", 1000, 5000),
        ("5,000+", 5000, 1e12),
    ]
    spread_buckets_def = [
        ("1¢", 0, 1.5),
        ("2¢", 1.5, 2.5),
        ("3–4¢", 2.5, 4.5),
        ("5–7¢", 4.5, 7.5),
        ("8–10¢", 7.5, 10.5),
        ("Over 10¢", 10.5, 1000),
    ]
    oi_buckets_def = [
        ("0-99", 0, 100),
        ("100-499", 100, 500),
        ("500-999", 500, 1000),
        ("1,000-4,999", 1000, 5000),
        ("5,000+", 5000, 1e12),
    ]

    vol_out = []
    for label, lo, hi in vol_buckets_def:
        rows = [t for t in trades if lo <= _f(t.get("market_volume")) < hi]
        vol_out.append({"label": label, **_group_metrics(rows)})

    spread_out = []
    for label, lo, hi in spread_buckets_def:
        rows = [t for t in trades if lo <= _f(t.get("spread_cents")) < hi]
        spread_out.append({"label": label, **_group_metrics(rows)})

    oi_out = []
    for label, lo, hi in oi_buckets_def:
        rows = [t for t in trades if lo <= _f(t.get("open_interest")) < hi]
        oi_out.append({"label": label, **_group_metrics(rows)})

    return {
        "volumeBuckets": vol_out,
        "spreadBuckets": spread_out,
        "openInterestBuckets": oi_out,
    }


def _time_analysis(trades: list[dict]) -> dict:
    """P&L by UTC hour, day of week, and day."""
    by_hour = {h: {"n": 0, "wins": 0, "pnlUsd": 0.0} for h in range(24)}
    by_dow = {d: {"n": 0, "wins": 0, "pnlUsd": 0.0}
              for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}
    by_day: dict[str, dict] = {}
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for t in trades:
        ts = _s(t.get("at"))
        dt = _parse_iso(ts)
        if dt:
            h = dt.hour
            by_hour[h]["n"] += 1
            by_hour[h]["wins"] += 1 if t.get("won") else 0
            by_hour[h]["pnlUsd"] = round(by_hour[h]["pnlUsd"] + _f(t.get("pnl_usd")), 4)
            dow = dow_names[dt.weekday()]
            by_dow[dow]["n"] += 1
            by_dow[dow]["wins"] += 1 if t.get("won") else 0
            by_dow[dow]["pnlUsd"] = round(by_dow[dow]["pnlUsd"] + _f(t.get("pnl_usd")), 4)
        day = ts[:10] if len(ts) >= 10 else None
        if day:
            d = by_day.setdefault(day, {"n": 0, "wins": 0, "pnlUsd": 0.0})
            d["n"] += 1
            d["wins"] += 1 if t.get("won") else 0
            d["pnlUsd"] = round(d["pnlUsd"] + _f(t.get("pnl_usd")), 4)

    return {
        "byHourUtc": [{"hour": h, **by_hour[h]} for h in range(24)],
        "byDayOfWeek": [{"day": d, **by_dow[d]} for d in dow_names],
        "byDay": [{"day": d, **v} for d, v in sorted(by_day.items())],
    }


def _event_concentration(trades: list[dict]) -> dict:
    """Event concentration analysis."""
    by_event: dict[str, list[dict]] = {}
    for t in trades:
        ev = t.get("event_ticker") or "unknown"
        by_event.setdefault(ev, []).append(t)

    events = []
    for ev, rows in sorted(by_event.items(), key=lambda kv: -len(kv[1])):
        total = sum(_f(t.get("pnl_usd")) for t in rows)
        events.append({
            "event_ticker": ev,
            "trades": len(rows),
            "pnlUsd": round(total, 2),
            "wins": sum(1 for t in rows if t.get("won")),
        })

    # Simulated caps
    cap_results = {}
    for cap in [1, 2, 3, 5]:
        capped_trades = []
        event_counts: dict[str, int] = {}
        for t in sorted(trades, key=lambda x: _s(x.get("at"))):
            ev = t.get("event_ticker") or "unknown"
            c = event_counts.get(ev, 0)
            if c < cap:
                capped_trades.append(t)
                event_counts[ev] = c + 1
        cap_results[str(cap)] = _group_metrics(capped_trades)

    worst_event = min(events, key=lambda e: e["pnlUsd"]) if events else None
    best_event = max(events, key=lambda e: e["pnlUsd"]) if events else None

    return {
        "events": events[:50],  # top 50
        "totalEvents": len(events),
        "cappedResults": cap_results,
        "worstEvent": worst_event,
        "bestEvent": best_event,
    }


def _sizing_comparison(trades: list[dict], cfg: dict) -> list[dict]:
    """Position sizing models comparison."""
    models = [
        ("Fixed $1", 1.0),
        ("Fixed $5", 5.0),
    ]
    results = []
    for name, fixed_usd in models:
        sim_trades = []
        for t in trades:
            cost_frac = _f(t.get("costCents")) / 100.0
            if cost_frac <= 0:
                continue
            n_ct = max(1, int(round(fixed_usd / cost_frac)))
            fee = bt.kalshi_fee_per_contract(cost_frac, contracts=n_ct)
            pnl_ct = (1.0 - cost_frac - fee) if t.get("won") else (-cost_frac - fee)
            sim_trades.append({**t, "pnl_usd": round(pnl_ct * n_ct, 4), "pnlUsd": round(pnl_ct * n_ct, 4), "contracts": n_ct})
        m = _group_metrics(sim_trades)
        results.append({"model": name, **m})

    # Current profile sizing (already in trades as-is)
    results.append({"model": "Current profile", **_group_metrics(trades)})

    return results


def _execution_comparison(trades: list[dict]) -> list[dict]:
    """Simulate different execution models."""
    models = [
        ("Signal price", lambda t: 0.0),
        ("Recorded ask", lambda t: _f(t.get("slippage_cents"))),
        ("Market order", lambda t: max(_f(t.get("slippage_cents")), _f(t.get("spread_cents")), 1.0)),
        ("Limit cross", lambda t: max(_f(t.get("slippage_cents")), min(_f(t.get("spread_cents")), 2.0))),
        ("Limit mid", lambda t: max(0.0, _f(t.get("spread_cents")) / 2.0)),
        ("Maker touch", lambda t: -min(max(_f(t.get("spread_cents")) / 2.0, 0.0), 1.0)),
        ("Maker trade-through", lambda t: -min(max(_f(t.get("spread_cents")), 0.0), 2.0)),
        ("Stress +1c", lambda t: _f(t.get("slippage_cents")) + 1.0),
        ("Stress +2c", lambda t: _f(t.get("slippage_cents")) + 2.0),
        ("Delayed 5s", lambda t: _f(t.get("slippage_cents")) + 0.5),
        ("Delayed 15s", lambda t: _f(t.get("slippage_cents")) + 1.5),
    ]
    results = []
    for name, adjustment_fn in models:
        sim_trades = []
        adjustments = []
        for t in trades:
            original_cost = _f(t.get("costCents")) / 100.0
            base_cost = original_cost - _f(t.get("slippage_cents", 0)) / 100.0
            adjustment_cents = adjustment_fn(t)
            adjustments.append(adjustment_cents)
            new_cost = min(0.99, max(0.01, base_cost + adjustment_cents / 100.0))
            contracts = _i(t.get("contracts") or 1)
            fee = bt.kalshi_fee_per_contract(new_cost, contracts=contracts)
            pnl_ct = (1.0 - new_cost - fee) if t.get("won") else (-new_cost - fee)
            sim_trades.append({
                **t,
                "pnl_usd": round(pnl_ct * contracts, 4),
                "pnlUsd": round(pnl_ct * contracts, 4),
                "costCents": round(new_cost * 100, 1),
            })
        m = _group_metrics(sim_trades)
        avg_adjustment = sum(adjustments) / len(adjustments) if adjustments else 0.0
        results.append({
            "model": name,
            "slippage": round(avg_adjustment, 2),
            "costAdjustmentCents": round(avg_adjustment, 2),
            **m,
        })
    return results


# ---------------------------------------------------------------------------
# Calibration statistics
# ---------------------------------------------------------------------------

def _calibration_stats(trades: list[dict]) -> dict:
    """Brier score, log loss, expected calibration error."""
    if not trades:
        return {"brierScore": None, "logLoss": None, "ece": None}

    n = len(trades)
    brier = 0.0
    logloss = 0.0
    eps = 1e-15

    for t in trades:
        conf = max(eps, min(1.0 - eps, _f(t.get("confidence")) / 100.0))
        actual = 1.0 if t.get("won") else 0.0
        brier += (conf - actual) ** 2
        logloss += -(actual * math.log(conf + eps) + (1 - actual) * math.log(1 - conf + eps))

    brier /= n
    logloss /= n

    # Expected calibration error (10 bins)
    bins: dict[int, list[tuple[float, float]]] = {}
    for t in trades:
        conf = _f(t.get("confidence")) / 100.0
        b = min(9, int(conf * 10))
        actual = 1.0 if t.get("won") else 0.0
        bins.setdefault(b, []).append((conf, actual))

    ece = 0.0
    for b, pairs in bins.items():
        avg_conf = sum(p[0] for p in pairs) / len(pairs)
        avg_actual = sum(p[1] for p in pairs) / len(pairs)
        ece += len(pairs) / n * abs(avg_conf - avg_actual)

    return {
        "brierScore": round(brier, 6),
        "logLoss": round(logloss, 6),
        "ece": round(ece, 6),
    }


# ---------------------------------------------------------------------------
# Rejection funnel
# ---------------------------------------------------------------------------

def _build_funnel(rejections: list[dict], n_accepted: int) -> dict:
    """Build the rejection funnel structure."""
    counts: dict[str, int] = {}
    for r in rejections:
        reason = r.get("primary_rejection") or "other"
        counts[reason] = counts.get(reason, 0) + 1

    total_raw = len(rejections) + n_accepted
    sorted_reasons = sorted(counts.items(), key=lambda kv: -kv[1])

    breakdown = []
    for reason, count in sorted_reasons:
        breakdown.append({
            "reason": reason,
            "count": count,
            "pct": round(count / total_raw, 4) if total_raw > 0 else 0.0,
        })

    return {
        "rawSignals": total_raw,
        "rejected": len(rejections),
        "accepted": n_accepted,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

def _equity_curve(trades: list[dict]) -> list[dict]:
    eq = []
    run = 0.0
    for t in sorted(trades, key=lambda x: _s(x.get("at"))):
        run += _f(t.get("pnl_usd"))
        eq.append({"at": t.get("at"), "value": round(run, 4)})
    return eq[-500:]


# ---------------------------------------------------------------------------
# Main replay function
# ---------------------------------------------------------------------------

def replay_main_engine(
    cfg: dict, *,
    since_days: int = 60,
    slippage_cents: float = 1.0,
    fixed_risk_usd: Optional[float] = None,
    execution_model: str = "recorded_ask",
) -> dict:
    """Replay recorded whale + momentum signals through the live gates.

    Args:
        cfg: Merged trader config.
        since_days: How far back to look.
        slippage_cents: Default slippage added to signal price.
        fixed_risk_usd: If set, use fixed sizing instead of cfg sizing.
        execution_model: 'recorded_ask', 'signal_price', 'stress_2c', etc.
    """
    import trader as trader_mod

    effective_slip = slippage_cents
    if execution_model == "signal_price":
        effective_slip = 0.0
    elif execution_model == "stress_2c":
        effective_slip = 2.0
    elif execution_model == "stress_3c":
        effective_slip = 3.0

    risk_usd = fixed_risk_usd or _f(cfg.get("fixed_trade_usd") or 5.0) or 5.0

    trades: list[dict] = []
    rejections: list[dict] = []
    scanned = 0

    with dbmod.get_db() as conn:
        conn.row_factory = None  # we need dict via sqlite3.Row but set it below
        conn.row_factory = __import__("sqlite3").Row
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

        # Process whales
        for r in whales:
            sig = _enrich_signal(conn, dict(r), "whale")
            scanned += 1
            try:
                ok, why = trader_mod.should_trade(sig, "whale", cfg)
            except Exception as e:
                ok = False
                why = f"should_trade error: {type(e).__name__}: {str(e)[:100]}"
            if not ok:
                rejections.append(_build_rejection(sig, "whale", why, cfg))
                continue
            direction, cost_cents = _signal_cost_cents(sig, "whale")
            price = sig.get("price")
            if price is None or not (0.0 < _f(price) < 1.0):
                rejections.append(_build_rejection(sig, "whale", "quote_unavailable", cfg))
                continue
            won = bool(sig.get("outcome_correct"))
            cost_frac = min(0.99, _f(price) + effective_slip / 100.0)
            contracts = max(1, int(round(risk_usd / max(cost_frac, 0.01))))
            trades.append(_build_trade_row(
                sig, "whale", cfg, cost_cents, direction, won,
                contracts, effective_slip,
            ))

        # Process momentum
        for r in alerts:
            sig = _enrich_signal(conn, dict(r), "momentum")
            scanned += 1
            try:
                ok, why = trader_mod.should_trade(sig, "momentum", cfg)
            except Exception as e:
                ok = False
                why = f"should_trade error: {type(e).__name__}: {str(e)[:100]}"
            if not ok:
                rejections.append(_build_rejection(sig, "momentum", why, cfg))
                continue
            direction, cost_cents = _signal_cost_cents(sig, "momentum")
            price = sig.get("price")
            if price is None or not (0.0 < _f(price) < 1.0):
                rejections.append(_build_rejection(sig, "momentum", "quote_unavailable", cfg))
                continue
            if direction == "no" and price is not None:
                effective_price = 1.0 - _f(price)
            else:
                effective_price = _f(price)
            won = bool(sig.get("outcome_correct"))
            cost_frac = min(0.99, effective_price + effective_slip / 100.0)
            contracts = max(1, int(round(risk_usd / max(cost_frac, 0.01))))
            trades.append(_build_trade_row(
                sig, "momentum", cfg, cost_cents, direction, won,
                contracts, effective_slip,
            ))

    # Sort trades chronologically
    trades.sort(key=lambda t: _s(t.get("at")))

    # Build all analytics
    overall = _group_metrics(trades)
    categories = _category_analysis(trades)
    sources = _source_analysis(trades)
    price_buckets = _entry_price_analysis(trades)
    edge_cal = _edge_calibration(trades)
    conf_cal = _confidence_calibration(trades)
    resolution = _resolution_analysis(trades)
    liquidity = _liquidity_analysis(trades)
    time_data = _time_analysis(trades)
    events = _event_concentration(trades)
    sizing = _sizing_comparison(trades, cfg)
    execution = _execution_comparison(trades)
    calibration_stats = _calibration_stats(trades)
    funnel = _build_funnel(rejections, len(trades))
    equity = _equity_curve(trades)

    # Edge calibration by source
    edge_cal_whale = _edge_calibration([t for t in trades if t.get("source") == "whale"])
    edge_cal_momentum = _edge_calibration([t for t in trades if t.get("source") == "momentum"])
    conf_cal_whale = _confidence_calibration([t for t in trades if t.get("source") == "whale"])
    conf_cal_momentum = _confidence_calibration([t for t in trades if t.get("source") == "momentum"])

    # Edge calibration by category
    edge_cal_by_cat = {}
    conf_cal_by_cat = {}
    for cat in set(t.get("category") or "unknown" for t in trades):
        cat_trades = [t for t in trades if (t.get("category") or "unknown") == cat]
        if cat_trades:
            edge_cal_by_cat[cat] = _edge_calibration(cat_trades)
            conf_cal_by_cat[cat] = _confidence_calibration(cat_trades)

    caveats = [
        f"Follower economics: entry at the signal price +{effective_slip:.0f}¢ slippage — "
        f"live fills on fast markets can be worse.",
        "One simulated trade per accepted signal; live caps (max open, per-event, daily) "
        "are NOT applied — hot events stack correlated trades.",
        "Whale outcomes cluster (one game prints many whale signals) — day/hour buckets "
        "share that clustering.",
        "In-sample: signals were only recorded while the app was running.",
        "contrarianOnly and maxResolutionDays are NOT re-simulated: alerts inherit whatever "
        "filter was live when they were RECORDED.",
    ]

    if not trades:
        caveats.insert(0, "No trades matched the current filters. "
                       "Check the rejection funnel for why signals were filtered out.")
    elif len(trades) < 30:
        caveats.insert(0, f"Only {len(trades)} trades — far too few for a verdict; "
                       f"treat as anecdote.")

    return {
        # Overall metrics
        "n": overall["n"],
        "wins": overall["wins"],
        "winRate": overall["winRate"],
        "totalPnlUsd": overall["totalPnlUsd"],
        "avgPnlUsd": overall["avgPnlUsd"],
        "netEvCentsPerContract": overall["realizedEdge"],
        "maxDrawdownUsd": overall["maxDrawdownUsd"],
        "profitFactor": overall["profitFactor"],
        "recoveryFactor": overall["recoveryFactor"],
        "feesUsd": overall["feesUsd"],
        # Scanned count
        "signalsScanned": scanned,
        "windowsScanned": scanned,
        "contracts": 1,
        "byAsset": {
            cat: {
                "n": m.get("n", 0),
                "wins": m.get("wins", 0),
                "pnlUsd": m.get("totalPnlUsd", 0.0),
            }
            for cat, m in (categories.get("combined") or {}).items()
        },
        "byHourUtc": time_data["byHourUtc"],
        "byDay": time_data["byDay"],
        # Deep analytics
        "categories": categories,
        "sources": sources,
        "entryPriceBuckets": price_buckets,
        "edgeCalibration": edge_cal,
        "edgeCalibrationWhale": edge_cal_whale,
        "edgeCalibrationMomentum": edge_cal_momentum,
        "edgeCalibrationByCategory": edge_cal_by_cat,
        "confidenceCalibration": conf_cal,
        "confidenceCalibrationWhale": conf_cal_whale,
        "confidenceCalibrationMomentum": conf_cal_momentum,
        "confidenceCalibrationByCategory": conf_cal_by_cat,
        "calibrationStats": calibration_stats,
        "resolutionBuckets": resolution,
        "liquidity": liquidity,
        "timeAnalysis": time_data,
        "eventConcentration": events,
        "sizingComparison": sizing,
        "executionComparison": execution,
        # Funnel
        "funnel": funnel,
        "rejectionTotal": funnel["rejected"],
        "rejectionBreakdown": funnel["breakdown"],
        "rejections": rejections[:500],  # cap for payload size
        # Equity + trades
        "equity": equity,
        "trades": trades,
        # Metadata
        "caveats": caveats,
        "config": {
            "sinceDays": since_days,
            "slippageCents": effective_slip,
            "executionModel": execution_model,
            "fixedRiskUsd": risk_usd,
        },
    }
