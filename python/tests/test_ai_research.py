from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone

import db
from config import merge_with_defaults


def _metric(n: int, edge: float, pnl: float | None = None) -> dict:
    return {
        "n": n,
        "wins": max(0, n // 2),
        "winRate": 0.5 if n else 0.0,
        "pnlUsd": edge if pnl is None else pnl,
        "avgPnlUsd": (edge if pnl is None else pnl) / n if n else 0.0,
        "netEvCentsPerContract": edge,
        "maxDrawdownUsd": -1.0 if n else 0.0,
        "profitFactor": 1.2 if edge >= 0 else 0.8,
    }


def _candidate(cid: str, params: dict, val_edge: float, test_edge: float, *, eligible: bool = True) -> dict:
    return {
        "candidateId": cid,
        "params": params,
        "eligible": eligible,
        "score": val_edge,
        "reason": "" if eligible else "below minimum sample constraint (10)",
        "splits": {
            "train": _metric(18, val_edge - 0.5),
            "validation": _metric(12 if eligible else 2, val_edge),
            "test": _metric(10 if eligible else 1, test_edge),
        },
        "bootstrap": {
            "samples": 100,
            "meanNetEvCentsPerContract": val_edge - 0.1,
            "ciLow": val_edge - 2.0,
            "ciHigh": val_edge + 2.0,
            "probabilityPositive": 0.8 if val_edge > 0 else 0.25,
        },
        "tradeCount": 40 if eligible else 4,
    }


def test_ai_research_builds_attribution_experiments_and_guarded_profiles():
    import ai_research

    opt = {
        "engine": "crypto15m",
        "env": "production",
        "sinceDays": 30,
        "gridSize": 4,
        "minTrades": 10,
        "baseProfileId": "p_btc_ma_b",
        "baseProfileName": "BTC MA B",
        "baseConfig": {
            "crypto15m_strategy_mode": "btc_ma_crossover",
            "crypto15m_assets": ["BTC"],
            "crypto15m_fast_ema_period": 12,
            "crypto15m_slow_sma_period": 20,
            "crypto15m_trend_sma_period": 50,
            "crypto15m_trend_timeframe_min": 5,
            "crypto15m_order_size": 5,
            "crypto15m_max_concurrent": 1,
            "crypto15m_min_entry_seconds_left": 720,
            "crypto15m_live": True,
            "enable_trading": True,
        },
        "walkForward": {"summary": _metric(8, 1.4)},
        "ranked": [
            _candidate(
                "grid-0001",
                {"crypto15m_min_entry_seconds_left": 60, "crypto15m_max_entry_cents": 59},
                3.4,
                2.1,
            ),
            _candidate(
                "grid-0002",
                {"crypto15m_min_entry_seconds_left": 720, "crypto15m_max_entry_cents": 59},
                -1.2,
                -2.4,
            ),
        ],
        "candidates": [
            _candidate(
                "grid-0001",
                {"crypto15m_min_entry_seconds_left": 60, "crypto15m_max_entry_cents": 59},
                3.4,
                2.1,
            ),
            _candidate(
                "grid-0002",
                {"crypto15m_min_entry_seconds_left": 720, "crypto15m_max_entry_cents": 59},
                -1.2,
                -2.4,
            ),
            _candidate(
                "grid-0003",
                {"crypto15m_min_entry_seconds_left": 120, "crypto15m_max_entry_cents": 50},
                1.0,
                0.8,
            ),
            _candidate(
                "grid-0004",
                {"crypto15m_min_entry_seconds_left": 180, "crypto15m_max_entry_cents": 45},
                4.0,
                3.0,
                eligible=False,
            ),
        ],
    }

    out = ai_research.analyze_optimization(opt, top_n_profiles=2)

    assert out["analysisMode"] == "local_research_ai"
    assert out["deploymentPolicy"]["requiresHumanApproval"] is True
    assert out["deploymentPolicy"]["liveDeploymentAllowed"] is False

    attribution = {r["feature"]: r for r in out["featureAttribution"]}
    assert attribution["crypto15m_min_entry_seconds_left"]["bestValue"] == 60
    assert attribution["crypto15m_min_entry_seconds_left"]["liftCents"] > 0

    comparison = out["winnerLoserComparisons"][0]
    assert comparison["winnerId"] == "grid-0001"
    assert comparison["loserId"] == "grid-0002"
    assert comparison["metricDeltas"]["validationEdgeCents"] > 0
    assert "crypto15m_min_entry_seconds_left" in comparison["parameterDeltas"]

    assert out["suggestedExperiments"]
    assert out["suggestedExperiments"][0]["paramGrid"]["crypto15m_min_entry_seconds_left"]
    assert out["suggestedExperiments"][0]["requiresHumanApproval"] is True

    assert len(out["candidateProfiles"]) == 1
    assert out["candidateProfiles"][0]["patch"]["crypto15mLive"] is False
    assert out["candidateProfiles"][0]["approval"]["required"] is True
    assert out["candidateProfiles"][0]["deployment"]["liveAllowed"] is False
    export = out["candidateProfiles"][0]["exportProfile"]
    assert export["kryptTraderProfile"] == 1
    assert export["profile"]["kind"] == "crypto15m"
    assert export["profile"]["baseProfileId"] == "p_btc_ma_b"
    assert export["profile"]["generatedBy"] == "optimizer"
    assert export["profile"]["candidateId"] == "grid-0001"
    assert export["profile"]["configMode"] == "full"
    assert export["profile"]["config"]["crypto15mLive"] is False
    assert export["profile"]["config"]["enableTrading"] is False
    assert export["profile"]["config"]["crypto15mStrategyMode"] == "btc_ma_crossover"
    assert export["profile"]["config"]["crypto15mAssets"] == ["BTC"]
    assert export["profile"]["config"]["crypto15mFastEmaPeriod"] == 12
    assert export["profile"]["config"]["crypto15mSlowSmaPeriod"] == 20
    assert export["profile"]["config"]["crypto15mTrendSmaPeriod"] == 50
    assert export["profile"]["config"]["crypto15mTrendTimeframeMin"] == 5
    assert export["profile"]["config"]["crypto15mOrderSize"] == 5
    assert export["profile"]["config"]["crypto15mMinEntrySecondsLeft"] == 60
    assert out["candidateProfiles"][0]["changedConfig"]
    assert "BTC MA Crossover" in out["candidateProfiles"][0]["preservedSummary"]


def test_ai_research_does_not_generate_profiles_for_rejected_candidates():
    import ai_research

    opt = {
        "engine": "crypto15m",
        "env": "production",
        "sinceDays": 30,
        "gridSize": 2,
        "minTrades": 10,
        "baseConfig": {
            "crypto15m_strategy_mode": "btc_ma_crossover",
            "crypto15m_assets": ["BTC"],
            "crypto15m_fast_ema_period": 12,
            "crypto15m_slow_sma_period": 20,
            "crypto15m_trend_sma_period": 50,
            "crypto15m_trend_timeframe_min": 5,
        },
        "walkForward": {"summary": _metric(4, -1.0)},
        "ranked": [
            _candidate(
                "grid-0001",
                {"crypto15m_min_entry_seconds_left": 60},
                24.1,
                -8.91,
                eligible=False,
            ),
            _candidate(
                "grid-0002",
                {"crypto15m_min_entry_seconds_left": 120},
                8.0,
                -7.67,
                eligible=False,
            ),
        ],
        "candidates": [
            _candidate(
                "grid-0001",
                {"crypto15m_min_entry_seconds_left": 60},
                24.1,
                -8.91,
                eligible=False,
            ),
            _candidate(
                "grid-0002",
                {"crypto15m_min_entry_seconds_left": 120},
                8.0,
                -7.67,
                eligible=False,
            ),
        ],
    }

    out = ai_research.analyze_optimization(opt)

    assert out["status"] == "No candidate passed out-of-sample requirements"
    assert out["researchWinner"] is None
    assert out["candidateProfiles"] == []
    assert any("No generated profiles" in c for c in out["caveats"])


def test_optimizer_attaches_ai_research_layer(tmp_path, monkeypatch):
    import optimizer

    dbfile = tmp_path / "research-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    cfg = merge_with_defaults({
        "crypto15m_direction_mode": "model",
        "crypto15m_time_delay_min": 5.0,
        "crypto15m_entry_max": 0.97,
        "crypto15m_order_size": 5,
    })
    with db.get_db() as conn:
        for i in range(8):
            at = now - timedelta(days=8 - i)
            ticker = f"KXBTC15M-AI-{i}"
            conn.execute(
                """INSERT INTO crypto15m_signals (ticker, asset, series, favorite,
                   favorite_price, entry_cost, resolved, up_won, close_time, kalshi_env)
                   VALUES (?,'BTC','KXBTC15M','up',0.93,0.93,1,?,
                           ?,'production')""",
                (
                    ticker,
                    1,
                    (at + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            conn.execute(
                """INSERT INTO crypto15m_ticks (
                      ticker, asset, observed_at, mins_left, yes_bid, yes_ask,
                      up_prob, spot, open_spot, delta_pct, no_ask, model_prob,
                      edge_net_cents, kalshi_env
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticker, "BTC", at.strftime("%Y-%m-%d %H:%M:%S"), 4.0,
                    0.92, 0.93, 0.93, 61000 + i, 60900, 0.001, 0.08,
                    0.985 if i < 6 else 0.955, 4.0, "production",
                ),
            )

    out = optimizer.optimize_crypto15m(
        cfg,
        env="production",
        since_days=30,
        param_grid={"crypto15m_model_min_prob": [0.95, 0.98]},
        min_trades=1,
        bootstrap_samples=25,
        seed=11,
    )

    assert out["research"]["analysisMode"] == "local_research_ai"
    assert out["research"]["candidateProfiles"]
    export_config = out["research"]["candidateProfiles"][0]["exportProfile"]["profile"]["config"]
    assert export_config["crypto15mDirectionMode"] == "model"
    assert export_config["crypto15mOrderSize"] == 5
    assert export_config["crypto15mLive"] is False
    assert export_config["enableTrading"] is False
    assert out["research"]["candidateProfiles"][0]["approval"]["required"] is True
    assert out["research"]["candidateProfiles"][0]["deployment"]["liveAllowed"] is False


def test_phase4_ai_research_builds_main_profiles_and_rejection_report():
    import ai_research

    accepted = _candidate(
        "main-grid-0001",
        {"trade_whales": True, "trade_momentum": False, "allowed_categories": ["crypto"]},
        2.8,
        1.9,
    )
    rejected = _candidate(
        "main-grid-0002",
        {"trade_whales": False, "trade_momentum": True, "allowed_categories": ["sports"]},
        1.2,
        -0.7,
        eligible=False,
    )
    opt = {
        "engine": "main",
        "env": "production",
        "sourceMode": "combined",
        "optimizerKind": "main_category_filter",
        "sinceDays": 60,
        "gridSize": 2,
        "minTrades": 10,
        "baseProfileId": "p_main_base",
        "baseProfileName": "Main Base",
        "baseConfig": {
            "enable_trading": True,
            "trade_whales": True,
            "trade_momentum": True,
            "allowed_categories": None,
            "min_edge_pts_whale": 5.0,
            "min_edge_pts_momentum": 5.0,
        },
        "walkForward": {"summary": _metric(12, 1.1)},
        "ranked": [accepted, rejected],
        "candidates": [accepted, rejected],
    }

    out = ai_research.analyze_optimization(opt, top_n_profiles=2)

    assert out["researchReport"]["format"] == "markdown"
    assert "main_category_filter" in out["researchReport"]["body"]
    assert out["eligibilityRules"] == {
        "minTradesPerSplit": 10,
        "requiresPositiveValidation": True,
        "requiresPositiveTest": True,
        "requiresPositiveWalkForward": True,
    }
    assert out["outOfSampleRejections"][0]["candidateId"] == "main-grid-0002"
    assert "test edge" in out["outOfSampleRejections"][0]["reason"]

    profiles = out["candidateProfiles"]
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["kind"] == "main"
    assert profile["patch"]["enableTrading"] is False
    assert profile["exportProfile"]["profile"]["kind"] == "main"
    assert profile["exportProfile"]["profile"]["configMode"] == "full"
    assert profile["exportProfile"]["profile"]["baseProfileId"] == "p_main_base"
    config = profile["exportProfile"]["profile"]["config"]
    assert config["enableTrading"] is False
    assert config["tradeWhales"] is True
    assert config["tradeMomentum"] is False
    assert config["allowedCategories"] == ["crypto"]


def test_phase4_ai_experiment_zip_exports_report_profiles_and_manifest(tmp_path):
    import ai_research

    candidate = _candidate(
        "main-grid-0001",
        {"trade_whales": True, "trade_momentum": False, "allowed_categories": ["crypto"]},
        2.8,
        1.9,
    )
    opt = {
        "engine": "main",
        "env": "production",
        "sourceMode": "whale",
        "optimizerKind": "main_whale",
        "optimizerRunId": "main-opt-test",
        "sinceDays": 60,
        "gridSize": 1,
        "minTrades": 10,
        "baseConfig": {"enable_trading": True, "trade_whales": True, "trade_momentum": True},
        "walkForward": {"summary": _metric(12, 1.1)},
        "ranked": [candidate],
        "candidates": [candidate],
    }

    zip_path = ai_research.export_experiment_zip(opt, str(tmp_path))

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {
            "manifest.json",
            "optimization.json",
            "research.json",
            "research_report.md",
            "suggested_experiments.json",
            "candidate_profiles/research-main-grid-0001.kryptprofile.json",
        }.issubset(names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["engine"] == "main"
        assert manifest["optimizerRunId"] == "main-opt-test"
        assert "research_report.md" in manifest["files"]
        profile = json.loads(zf.read("candidate_profiles/research-main-grid-0001.kryptprofile.json"))
        assert profile["profile"]["kind"] == "main"
        assert profile["profile"]["config"]["enableTrading"] is False
        report = zf.read("research_report.md").decode("utf-8")
        assert "main-grid-0001" in report
