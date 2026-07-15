from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
from config import merge_with_defaults


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "optimizer-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _trade(at: str, pnl: float, *, contracts: int = 5) -> dict:
    return {
        "ticker": f"KXBTC15M-{at}",
        "asset": "BTC",
        "side": "up",
        "entrySide": "YES",
        "costCents": 34.0,
        "entryPriceCents": 34.0,
        "minsLeft": 10.0,
        "won": pnl > 0,
        "pnlUsd": pnl,
        "contracts": contracts,
        "at": at,
    }


def _main_trade(at: str, pnl: float, *, source: str = "whale", category: str = "sports") -> dict:
    return {
        "ticker": f"KXMAIN-{source}-{at}",
        "event_ticker": f"KXMAIN-{category}",
        "category": category,
        "source": source,
        "side": "yes",
        "costCents": 40.0,
        "won": pnl > 0,
        "pnlUsd": pnl,
        "pnl_usd": pnl,
        "contracts": 1,
        "at": at,
    }


def test_build_grid_expands_parameters_in_stable_order():
    import optimizer

    rows = optimizer.build_grid({
        "crypto15m_min_entry_cents": [5, 10],
        "crypto15m_max_entry_cents": [50, 59],
    })

    assert rows == [
        {"crypto15m_min_entry_cents": 5, "crypto15m_max_entry_cents": 50},
        {"crypto15m_min_entry_cents": 5, "crypto15m_max_entry_cents": 59},
        {"crypto15m_min_entry_cents": 10, "crypto15m_max_entry_cents": 50},
        {"crypto15m_min_entry_cents": 10, "crypto15m_max_entry_cents": 59},
    ]


def test_split_metrics_ranking_and_bootstrap_constraints():
    import optimizer

    trades = [
        _trade("2026-07-01 00:00:00", 1.0),
        _trade("2026-07-02 00:00:00", -0.5),
        _trade("2026-07-03 00:00:00", 1.5),
        _trade("2026-07-04 00:00:00", 2.0),
        _trade("2026-07-05 00:00:00", -0.25),
        _trade("2026-07-06 00:00:00", 1.0),
    ]

    splits = optimizer.split_trades(trades, train_pct=0.5, validation_pct=0.25)
    assert [len(splits[k]) for k in ("train", "validation", "test")] == [3, 1, 2]

    row = optimizer.score_candidate(
        candidate_id="grid-0001",
        params={"crypto15m_max_entry_cents": 59},
        trades=trades,
        contracts=5,
        min_trades=1,
        bootstrap_samples=50,
        seed=7,
    )

    assert row["eligible"] is True
    assert row["splits"]["train"]["n"] == 3
    assert row["splits"]["validation"]["n"] == 1
    assert row["splits"]["test"]["n"] == 2
    assert row["bootstrap"]["samples"] == 50
    assert 0.0 <= row["bootstrap"]["probabilityPositive"] <= 1.0
    assert row["score"] == pytest.approx(round(
        0.20 * row["splits"]["train"]["netEvCentsPerContract"]
        + 0.40 * row["splits"]["validation"]["netEvCentsPerContract"]
        + 0.40 * row["splits"]["test"]["netEvCentsPerContract"],
        4,
    ))


def test_optimizer_ranks_candidates_and_builds_heatmap():
    import optimizer

    rows = [
        {
            "candidateId": "grid-0001",
            "params": {"a": 1, "b": 10},
            "eligible": True,
            "score": 2.0,
            "splits": {"validation": {"n": 3, "netEvCentsPerContract": 2.0}},
        },
        {
            "candidateId": "grid-0002",
            "params": {"a": 2, "b": 10},
            "eligible": True,
            "score": 5.0,
            "splits": {"validation": {"n": 3, "netEvCentsPerContract": 5.0}},
        },
        {
            "candidateId": "grid-0003",
            "params": {"a": 1, "b": 20},
            "eligible": False,
            "score": 99.0,
            "splits": {"validation": {"n": 0, "netEvCentsPerContract": 99.0}},
        },
    ]

    ranked = optimizer.rank_candidates(rows, top_n=3)
    heatmap = optimizer.build_heatmap(rows, x_param="a", y_param="b")

    assert [r["candidateId"] for r in ranked] == ["grid-0002", "grid-0001", "grid-0003"]
    assert heatmap["xParam"] == "a"
    assert heatmap["yParam"] == "b"
    assert {"x": 2, "y": 10, "score": 5.0, "n": 3, "eligible": True} in heatmap["cells"]


def test_optimize_crypto15m_runs_grid_against_replay_data(fresh_db):
    import optimizer

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
            ticker = f"KXBTC15M-OPT-{i}"
            up_won = 1 if i % 2 == 0 else 0
            conn.execute(
                """INSERT INTO crypto15m_signals (ticker, asset, series, favorite,
                   favorite_price, entry_cost, resolved, up_won, close_time, kalshi_env)
                   VALUES (?,'BTC','KXBTC15M','up',0.93,0.93,1,?,
                           ?,'production')""",
                (
                    ticker,
                    up_won,
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

    assert out["gridSize"] == 2
    assert len(out["ranked"]) == 2
    assert out["ranked"][0]["splits"]["train"]["n"] >= 1
    assert out["walkForward"]["folds"]
    assert out["dataset"]["datasetId"].startswith("crypto15m-production-")


def test_phase3_separate_main_source_optimizers_attach_research(monkeypatch):
    import optimizer

    calls: list[dict] = []

    def fake_replay(cfg: dict, **kwargs) -> dict:
        calls.append(dict(cfg))
        source = "whale" if cfg.get("trade_whales") else "momentum"
        trades = [
            _main_trade(f"2026-07-{i + 1:02d} 10:00:00", 1.0, source=source)
            for i in range(8)
        ]
        return {
            "trades": trades,
            "n": len(trades),
            "signalsScanned": 12,
            "funnel": {"rejected": 4},
        }

    monkeypatch.setattr(optimizer.main_replay, "replay_main_engine", fake_replay)

    whale = optimizer.optimize_main_whales(
        merge_with_defaults({}),
        since_days=14,
        param_grid={"min_edge_pts_whale": [3.0]},
        min_trades=1,
        bootstrap_samples=10,
        walk_forward_folds=3,
        seed=3,
    )
    momentum = optimizer.optimize_main_momentum(
        merge_with_defaults({}),
        since_days=14,
        param_grid={"min_edge_pts_momentum": [4.0]},
        min_trades=1,
        bootstrap_samples=10,
        walk_forward_folds=3,
        seed=4,
    )

    assert whale["optimizerKind"] == "main_whale"
    assert whale["sourceMode"] == "whale"
    assert whale["paramGrid"]["trade_whales"] == [True]
    assert whale["paramGrid"]["trade_momentum"] == [False]
    assert whale["ranked"][0]["bootstrap"]["samples"] == 10
    assert whale["walkForwardFolds"] == 3
    assert whale["research"]["analysisMode"] == "local_research_ai"
    assert whale["research"]["candidateProfiles"][0]["kind"] == "main"
    assert whale["research"]["candidateProfiles"][0]["exportProfile"]["profile"]["kind"] == "main"
    assert whale["research"]["candidateProfiles"][0]["exportProfile"]["profile"]["config"]["enableTrading"] is False

    assert momentum["optimizerKind"] == "main_momentum"
    assert momentum["sourceMode"] == "momentum"
    assert momentum["paramGrid"]["trade_whales"] == [False]
    assert momentum["paramGrid"]["trade_momentum"] == [True]
    assert calls[0]["trade_whales"] is True
    assert calls[0]["trade_momentum"] is False
    assert calls[1]["trade_whales"] is False
    assert calls[1]["trade_momentum"] is True


def test_phase3_category_filter_optimizer_builds_category_candidates(monkeypatch):
    import optimizer

    seen_categories: list[list[str]] = []

    def fake_replay(cfg: dict, **kwargs) -> dict:
        cats = list(cfg.get("allowed_categories") or [])
        seen_categories.append(cats)
        pnl = 1.0 if cats == ["crypto"] else -0.25
        trades = [
            _main_trade(f"2026-07-{i + 1:02d} 10:00:00", pnl, source="whale", category=cats[0])
            for i in range(8)
        ]
        return {
            "trades": trades,
            "n": len(trades),
            "signalsScanned": 8,
            "funnel": {"rejected": 0},
        }

    monkeypatch.setattr(optimizer.main_replay, "replay_main_engine", fake_replay)

    out = optimizer.optimize_main_category_filters(
        merge_with_defaults({}),
        categories=["sports", "crypto"],
        since_days=30,
        min_trades=1,
        bootstrap_samples=12,
        seed=9,
    )

    assert out["optimizerKind"] == "main_category_filter"
    assert out["sourceMode"] == "combined"
    assert out["paramGrid"]["allowed_categories"] == [["sports"], ["crypto"]]
    assert seen_categories == [["sports"], ["crypto"]]
    assert [c["params"]["allowed_categories"] for c in out["candidates"]] == [["sports"], ["crypto"]]
    assert out["ranked"][0]["params"]["allowed_categories"] == ["crypto"]
    assert out["splitPolicy"] == {"trainPct": 0.6, "validationPct": 0.2, "testPct": 0.2}
    assert out["research"]["researchReport"]["format"] == "markdown"
    assert out["research"]["eligibilityRules"]["requiresPositiveWalkForward"] is True
