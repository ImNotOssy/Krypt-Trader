"""Main-engine CSV / JSON export.

Produces the trade-level export described in the spec — every trade row
with all 9 field groups, rejection records, category metrics, calibration
data, and a manifest. Everything exportable as both CSV and JSON.
"""
from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _s(v: Any) -> str:
    return str(v) if v is not None else ""


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

_TRADE_COLUMNS = [
    # Identity
    "timestamp", "ticker", "event_ticker", "market_title", "category", "raw_category",
    # Signal
    "source", "side", "confidence", "raw_edge", "net_edge",
    # Whale details
    "whale_dollar_size", "whale_taker_side", "whale_entry_price", "whale_count_fp",
    # Momentum details
    "signal_type", "momentum_direction", "price_change",
    "cluster_size", "cluster_duration_sec", "momentum_contrarian",
    "momentum_favorite_status",
    # Market
    "yes_bid_cents", "yes_ask_cents", "spread_cents", "entry_price_cents",
    "market_volume", "open_interest",
    # Timing
    "signal_age_sec", "days_until_resolution", "close_time",
    # Execution
    "slippage_cents", "simulated_fill_cents", "fee_per_contract",
    # Position
    "contracts", "cost_usd", "fees_usd",
    # Result
    "won", "settlement", "pnl_usd", "return_on_risk",
]

_REJECTION_COLUMNS = [
    "timestamp", "source", "ticker", "event_ticker", "market_title",
    "category", "raw_category", "confidence", "raw_edge", "net_edge",
    "entry_price_cents", "primary_rejection", "rejection_detail",
    "market_volume", "whale_dollar_size",
]

_CATEGORY_COLUMNS = [
    "category", "source_view", "n", "wins", "winRate", "avgEdge",
    "realizedEdge", "totalPnlUsd", "avgPnlUsd", "profitFactor",
    "maxDrawdownUsd", "recoveryFactor", "feesUsd",
]

_CALIBRATION_COLUMNS = [
    "label", "lo", "hi", "n", "wins", "winRate", "avgPredictedEdge",
    "avgConfidence", "predictedWinRate", "realizedEdge", "totalPnlUsd",
]


def _dict_to_row(d: dict, columns: list[str]) -> dict:
    return {c: d.get(c, "") for c in columns}


def _write_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(_dict_to_row(r, columns))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def trades_csv(result: dict) -> str:
    return _write_csv(result.get("trades") or [], _TRADE_COLUMNS)


def trades_json(result: dict) -> str:
    return json.dumps(result.get("trades") or [], indent=2, default=str)


def rejections_csv(result: dict) -> str:
    return _write_csv(result.get("rejections") or [], _REJECTION_COLUMNS)


def rejections_json(result: dict) -> str:
    return json.dumps(result.get("rejections") or [], indent=2, default=str)


def category_metrics_csv(result: dict) -> str:
    cats = result.get("categories") or {}
    rows = []
    for view_name, view_data in [("combined", cats.get("combined")),
                                  ("whaleOnly", cats.get("whaleOnly")),
                                  ("momentumOnly", cats.get("momentumOnly"))]:
        if not view_data:
            continue
        for cat, m in sorted(view_data.items()):
            rows.append({"category": cat, "source_view": view_name, **m})
    return _write_csv(rows, _CATEGORY_COLUMNS)


def source_metrics_csv(result: dict) -> str:
    sources = result.get("sources") or {}
    columns = ["source", "n", "wins", "winRate", "totalPnlUsd", "avgPnlUsd",
               "avgEdge", "realizedEdge", "profitFactor", "maxDrawdownUsd",
               "recoveryFactor", "feesUsd"]
    rows = []
    for src in ("whale", "momentum", "combined"):
        data = sources.get(src)
        if data:
            rows.append({"source": src, **data})
    return _write_csv(rows, columns)


def whale_size_buckets_csv(result: dict) -> str:
    sources = result.get("sources") or {}
    cols = ["label", "n", "wins", "winRate", "totalPnlUsd", "avgPnlUsd",
            "avgEdge", "realizedEdge", "profitFactor", "maxDrawdownUsd",
            "recoveryFactor", "avgCostCents", "feesUsd"]
    return _write_csv(sources.get("whaleSizeBuckets") or [], cols)


def momentum_buckets_csv(result: dict) -> str:
    sources = result.get("sources") or {}
    rows = []
    for kind, key in [
        ("cluster_size", "momentumClusterSizeBuckets"),
        ("cluster_duration", "momentumClusterDurationBuckets"),
        ("contrarian", "momentumContrarianBuckets"),
        ("direction", "momentumDirectionBuckets"),
    ]:
        for b in (sources.get(key) or []):
            rows.append({"type": kind, **b})
    cols = ["type", "label", "n", "wins", "winRate", "totalPnlUsd",
            "avgPnlUsd", "avgEdge", "realizedEdge", "profitFactor",
            "maxDrawdownUsd", "recoveryFactor", "avgCostCents", "feesUsd"]
    return _write_csv(rows, cols)


def edge_calibration_csv(result: dict) -> str:
    return _write_csv(result.get("edgeCalibration") or [], _CALIBRATION_COLUMNS)


def confidence_calibration_csv(result: dict) -> str:
    cols = ["label", "lo", "hi", "n", "wins", "winRate",
            "avgPredictedConfidence", "predictedWinRate", "realizedEdge", "totalPnlUsd"]
    return _write_csv(result.get("confidenceCalibration") or [], cols)


def price_buckets_csv(result: dict) -> str:
    cols = ["label", "lo", "hi", "n", "wins", "winRate", "avgCostCents",
            "breakevenWinRate", "calibrationMargin", "totalPnlUsd",
            "profitFactor"]
    return _write_csv(result.get("entryPriceBuckets") or [], cols)


def resolution_buckets_csv(result: dict) -> str:
    cols = ["label", "n", "wins", "winRate", "totalPnlUsd",
            "capitalEfficiency", "totalCapitalDays", "maxDrawdownUsd"]
    return _write_csv(result.get("resolutionBuckets") or [], cols)


def liquidity_buckets_csv(result: dict) -> str:
    liq = result.get("liquidity") or {}
    rows = []
    for kind, data in [("volume", liq.get("volumeBuckets")),
                        ("spread", liq.get("spreadBuckets")),
                        ("open_interest", liq.get("openInterestBuckets"))]:
        for b in (data or []):
            rows.append({"type": kind, **b})
    cols = ["type", "label", "n", "wins", "winRate", "totalPnlUsd",
            "profitFactor", "maxDrawdownUsd"]
    return _write_csv(rows, cols)


def event_concentration_csv(result: dict) -> str:
    events = (result.get("eventConcentration") or {}).get("events") or []
    cols = ["event_ticker", "trades", "pnlUsd", "wins"]
    return _write_csv(events, cols)


def daily_metrics_csv(result: dict) -> str:
    time_data = result.get("timeAnalysis") or {}
    by_day = time_data.get("byDay") or []
    cols = ["day", "n", "wins", "pnlUsd"]
    return _write_csv(by_day, cols)


def sizing_comparison_csv(result: dict) -> str:
    cols = ["model", "n", "wins", "winRate", "totalPnlUsd", "avgPnlUsd",
            "avgEdge", "realizedEdge", "profitFactor", "maxDrawdownUsd",
            "recoveryFactor", "avgCostCents", "feesUsd"]
    return _write_csv(result.get("sizingComparison") or [], cols)


def execution_models_csv(result: dict) -> str:
    cols = ["model", "costAdjustmentCents", "slippage", "n", "wins",
            "winRate", "totalPnlUsd", "avgPnlUsd", "avgEdge",
            "realizedEdge", "profitFactor", "maxDrawdownUsd",
            "recoveryFactor", "avgCostCents", "feesUsd"]
    return _write_csv(result.get("executionComparison") or [], cols)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def build_manifest(result: dict, cfg: dict) -> dict:
    trades = result.get("trades") or []
    timestamps = sorted(_s(t.get("at")) for t in trades if t.get("at"))
    return {
        "generatedAt": _utc_now(),
        "engine": "main",
        "sinceDays": (result.get("config") or {}).get("sinceDays"),
        "signalsScanned": result.get("signalsScanned"),
        "tradesAccepted": result.get("n"),
        "rejected": (result.get("funnel") or {}).get("rejected"),
        "firstTrade": timestamps[0] if timestamps else None,
        "lastTrade": timestamps[-1] if timestamps else None,
        "executionModel": (result.get("config") or {}).get("executionModel"),
        "slippageCents": (result.get("config") or {}).get("slippageCents"),
        "files": [
            "manifest.json",
            "base_profile.json",
            "trades.csv",
            "trades.json",
            "rejections.csv",
            "category_metrics.csv",
            "source_metrics.csv",
            "whale_size_buckets.csv",
            "momentum_buckets.csv",
            "edge_calibration.csv",
            "confidence_calibration.csv",
            "price_buckets.csv",
            "resolution_buckets.csv",
            "liquidity_buckets.csv",
            "event_concentration.csv",
            "sizing_comparison.csv",
            "execution_models.csv",
            "daily_metrics.csv",
        ],
    }


# ---------------------------------------------------------------------------
# ZIP export
# ---------------------------------------------------------------------------

def export_zip(result: dict, cfg: dict, out_dir: str) -> str:
    """Write all CSVs + JSON into a ZIP file, return the path."""
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_name = f"main_engine_research_{ts}.zip"
    zip_path = os.path.join(out_dir, zip_name)

    manifest = build_manifest(result, cfg)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        zf.writestr("base_profile.json", json.dumps(cfg, indent=2, default=str))
        zf.writestr("trades.csv", trades_csv(result))
        zf.writestr("trades.json", trades_json(result))
        zf.writestr("rejections.csv", rejections_csv(result))
        zf.writestr("category_metrics.csv", category_metrics_csv(result))
        zf.writestr("source_metrics.csv", source_metrics_csv(result))
        zf.writestr("whale_size_buckets.csv", whale_size_buckets_csv(result))
        zf.writestr("momentum_buckets.csv", momentum_buckets_csv(result))
        zf.writestr("edge_calibration.csv", edge_calibration_csv(result))
        zf.writestr("confidence_calibration.csv", confidence_calibration_csv(result))
        zf.writestr("price_buckets.csv", price_buckets_csv(result))
        zf.writestr("resolution_buckets.csv", resolution_buckets_csv(result))
        zf.writestr("liquidity_buckets.csv", liquidity_buckets_csv(result))
        zf.writestr("event_concentration.csv", event_concentration_csv(result))
        zf.writestr("sizing_comparison.csv", sizing_comparison_csv(result))
        zf.writestr("execution_models.csv", execution_models_csv(result))
        zf.writestr("daily_metrics.csv", daily_metrics_csv(result))

    return zip_path
