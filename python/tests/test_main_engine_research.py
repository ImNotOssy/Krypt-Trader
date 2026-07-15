from __future__ import annotations

import json
import zipfile

from config import merge_with_defaults


def _trade(at: str, pnl: float, *, won: bool | None = None, **overrides) -> dict:
    did_win = pnl > 0 if won is None else won
    row = {
        "timestamp": at,
        "at": at,
        "ticker": f"KXMAIN-{at[-2:]}",
        "event_ticker": "KXMAIN",
        "market_title": "Will the sample event happen?",
        "category": "sports",
        "raw_category": "Sports",
        "source": "whale",
        "side": "yes",
        "confidence": 62.0,
        "raw_edge": 7.0,
        "net_edge": 5.4,
        "yes_bid_cents": 44.0,
        "yes_ask_cents": 46.0,
        "spread_cents": 2.0,
        "entry_price_cents": 47.0,
        "market_volume": 1200,
        "open_interest": 300,
        "slippage_cents": 1.0,
        "simulated_fill_cents": 47.0,
        "fee_per_contract": 0.02,
        "contracts": 2,
        "cost_usd": 0.94,
        "fees_usd": 0.04,
        "won": did_win,
        "settlement": 1.0 if did_win else 0.0,
        "pnl_usd": pnl,
        "pnlUsd": pnl,
        "return_on_risk": pnl / 0.94,
    }
    row.update(overrides)
    return row


def test_main_export_zip_writes_manifest_and_research_files(tmp_path):
    import main_export

    result = {
        "config": {"sinceDays": 14, "executionModel": "recorded_ask", "slippageCents": 1.0},
        "signalsScanned": 3,
        "n": 2,
        "funnel": {"rejected": 1},
        "trades": [
            _trade("2026-07-01 10:00:00", 0.98),
            _trade("2026-07-02 11:00:00", -0.96, won=False),
        ],
        "rejections": [{
            "timestamp": "2026-07-03 12:00:00",
            "source": "momentum",
            "ticker": "KXMAIN-R",
            "event_ticker": "KXMAIN",
            "market_title": "Will the sample event happen?",
            "category": "sports",
            "raw_category": "Sports",
            "confidence": 44.0,
            "raw_edge": -2.0,
            "net_edge": -3.0,
            "entry_price_cents": 55,
            "primary_rejection": "below_confidence_threshold",
            "rejection_detail": "conf 44.0 < 55.0",
            "market_volume": 1200,
            "whale_dollar_size": None,
        }],
        "categories": {"combined": {"sports": {"n": 2, "wins": 1}}},
        "sources": {
            "whale": {"n": 2, "wins": 1},
            "whaleSizeBuckets": [{"label": "$1,000-$2,499", "n": 2, "wins": 1}],
            "momentumClusterSizeBuckets": [{"label": "2-3", "n": 1, "wins": 1}],
            "momentumClusterDurationBuckets": [{"label": "15-60s", "n": 1, "wins": 1}],
            "momentumContrarianBuckets": [{"label": "Contrarian", "n": 1, "wins": 1}],
        },
        "edgeCalibration": [],
        "confidenceCalibration": [],
        "entryPriceBuckets": [],
        "resolutionBuckets": [],
        "liquidity": {
            "openInterestBuckets": [{"label": "100-499", "n": 2, "wins": 1}],
        },
        "eventConcentration": {"events": []},
        "sizingComparison": [{"model": "Current profile", "n": 2, "wins": 1}],
        "executionComparison": [{"model": "Signal price", "costAdjustmentCents": 0.0, "n": 2, "wins": 1}],
        "timeAnalysis": {"byDay": []},
    }

    zip_path = main_export.export_zip(result, merge_with_defaults({}), str(tmp_path))

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {
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
            "sizing_comparison.csv",
            "execution_models.csv",
        }.issubset(names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["engine"] == "main"
        assert manifest["firstTrade"] == "2026-07-01 10:00:00"
        assert manifest["lastTrade"] == "2026-07-02 11:00:00"
        assert manifest["signalsScanned"] == 3
        assert "momentum_buckets.csv" in manifest["files"]
        assert "execution_models.csv" in manifest["files"]
        assert "KXMAIN-" in zf.read("trades.csv").decode("utf-8")
        assert "Current profile" in zf.read("sizing_comparison.csv").decode("utf-8")


def test_phase2_trade_row_preserves_momentum_cluster_fields():
    import main_replay

    row = main_replay._build_trade_row(
        {
            "created_at": "2026-07-01 10:00:00",
            "ticker": "KXMOM",
            "event_ticker": "KXMOM-EVENT",
            "title": "Will momentum persist?",
            "_normalized_category": "crypto",
            "_raw_category": "Crypto",
            "confidence": 65.0,
            "price": 0.42,
            "direction": "yes",
            "signal_type": "trade_cluster",
            "price_change": 3.5,
            "cluster_size": 5,
            "cluster_duration_sec": 32,
            "contrarian": True,
            "_yes_bid": 0.41,
            "_yes_ask": 0.43,
            "_open_interest": 750,
            "market_volume": 4000,
        },
        "momentum",
        merge_with_defaults({}),
        42,
        "yes",
        True,
        2,
        1.0,
    )

    assert row["cluster_size"] == 5
    assert row["cluster_duration_sec"] == 32.0
    assert row["momentum_contrarian"] is True
    assert row["momentum_favorite_status"] == "underdog"


def test_phase2_source_analysis_adds_momentum_cluster_breakdowns():
    import main_replay

    out = main_replay._source_analysis([
        _trade(
            "2026-07-01 10:00:00",
            0.50,
            source="momentum",
            cluster_size=2,
            cluster_duration_sec=12,
            momentum_contrarian=True,
            momentum_direction="yes",
            whale_dollar_size=None,
        ),
        _trade(
            "2026-07-01 10:01:00",
            -0.47,
            won=False,
            source="momentum",
            cluster_size=8,
            cluster_duration_sec=140,
            momentum_contrarian=False,
            momentum_direction="no",
            whale_dollar_size=None,
        ),
        _trade("2026-07-01 10:02:00", 0.25, whale_dollar_size=1500),
    ])

    cluster_sizes = {b["label"]: b["n"] for b in out["momentumClusterSizeBuckets"]}
    durations = {b["label"]: b["n"] for b in out["momentumClusterDurationBuckets"]}
    contrarian = {b["label"]: b["n"] for b in out["momentumContrarianBuckets"]}
    directions = {b["label"]: b["n"] for b in out["momentumDirectionBuckets"]}

    assert cluster_sizes["2-3"] == 1
    assert cluster_sizes["7-10"] == 1
    assert durations["Under 15s"] == 1
    assert durations["1-3m"] == 1
    assert contrarian["Contrarian"] == 1
    assert contrarian["Continuation"] == 1
    assert directions["YES"] == 1
    assert directions["NO"] == 1


def test_phase2_liquidity_analysis_adds_open_interest_buckets():
    import main_replay

    out = main_replay._liquidity_analysis([
        _trade("2026-07-01 10:00:00", 0.50, market_volume=50, open_interest=50, spread_cents=1.0),
        _trade("2026-07-01 10:01:00", 0.25, market_volume=750, open_interest=750, spread_cents=3.0),
        _trade("2026-07-01 10:02:00", -0.47, won=False, market_volume=8000, open_interest=8000, spread_cents=12.0),
    ])

    oi = {b["label"]: b["n"] for b in out["openInterestBuckets"]}
    volume_labels = [b["label"] for b in out["volumeBuckets"]]

    assert oi["0-99"] == 1
    assert oi["500-999"] == 1
    assert oi["5,000+"] == 1
    assert len(volume_labels) == len(set(volume_labels))


def test_phase2_execution_comparison_adds_stress_and_execution_models():
    import main_replay

    out = main_replay._execution_comparison([
        _trade(
            "2026-07-01 10:00:00",
            0.50,
            costCents=47.0,
            slippage_cents=1.0,
            spread_cents=4.0,
            contracts=2,
        ),
    ])

    names = {r["model"] for r in out}
    expected = {
        "Signal price",
        "Recorded ask",
        "Market order",
        "Limit cross",
        "Limit mid",
        "Maker touch",
        "Maker trade-through",
        "Stress +1c",
        "Stress +2c",
        "Delayed 5s",
        "Delayed 15s",
    }
    assert expected.issubset(names)
    assert all("costAdjustmentCents" in row for row in out)


def test_optimize_main_engine_uses_main_replay_and_main_grid(monkeypatch):
    import optimizer

    calls: list[tuple[dict, dict]] = []

    def fake_replay(cfg: dict, **kwargs) -> dict:
        calls.append((dict(cfg), dict(kwargs)))
        whale_edge = float(cfg.get("min_edge_pts_whale") or 0.0)
        pnl = 1.0 if whale_edge <= 3.0 else -1.0
        trades = [
            _trade(f"2026-07-0{i + 1} 10:00:00", pnl, won=pnl > 0)
            for i in range(6)
        ]
        return {
            "trades": trades,
            "n": len(trades),
            "signalsScanned": 9,
            "funnel": {"rawSignals": 9, "rejected": 3, "accepted": len(trades)},
        }

    monkeypatch.setattr(optimizer.main_replay, "replay_main_engine", fake_replay)

    out = optimizer.optimize_main_engine(
        merge_with_defaults({}),
        since_days=14,
        param_grid={"min_edge_pts_whale": [3.0, 7.0]},
        min_trades=1,
        bootstrap_samples=20,
        seed=5,
    )

    assert out["engine"] == "main"
    assert out["gridSize"] == 2
    assert out["ranked"][0]["params"] == {"min_edge_pts_whale": 3.0}
    assert out["ranked"][0]["backtest"] == {
        "n": 6,
        "signalsScanned": 9,
        "rejectionTotal": 3,
    }
    assert out["paramGrid"] == {"min_edge_pts_whale": [3.0, 7.0]}
    assert out["optimizerRunId"].startswith("main-opt-")
    assert len(calls) == 2
    assert calls[0][1]["since_days"] == 14
    assert calls[0][1]["fixed_risk_usd"] == 1.0
    assert "crypto15m_model_min_prob" not in out["paramGrid"]
