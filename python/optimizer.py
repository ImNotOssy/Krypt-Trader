from __future__ import annotations

import argparse
import itertools
import json
import random
from typing import Any, Optional

import ai_research
import main_replay
import replay
from config import merge_with_defaults


DEFAULT_C15_GRID_DIRECTIONAL = {
    "crypto15m_entry_threshold": [0.90, 0.93, 0.95],
    "crypto15m_entry_max": [0.95, 0.97],
    "crypto15m_time_delay_min": [5, 8, 12],
}

DEFAULT_C15_GRID_MODEL = {
    "crypto15m_model_min_prob": [0.95, 0.97, 0.985],
    "crypto15m_model_min_edge_cents": [1.0, 2.0, 4.0],
    "crypto15m_entry_max": [0.95, 0.97],
}

DEFAULT_C15_GRID_BTC_MA = {
    "crypto15m_min_entry_cents": [5, 10, 15],
    "crypto15m_max_entry_cents": [45, 50, 59],
    "crypto15m_min_entry_seconds_left": [60, 120, 180],
}

DEFAULT_MAIN_WHALE_GRID = {
    "trade_whales": [True],
    "trade_momentum": [False],
    "min_whale_usd": [500.0, 2500.0, 5000.0],
    "min_edge_pts_whale": [3.0, 5.0, 7.0],
    "min_confidence_whale": [50.0, 55.0, 60.0],
    "min_entry_price_cents": [10, 15],
    "max_entry_price_cents": [70, 85],
    "min_market_volume": [0.0, 100.0],
}

DEFAULT_MAIN_MOMENTUM_GRID = {
    "trade_whales": [False],
    "trade_momentum": [True],
    "min_edge_pts_momentum": [3.0, 5.0, 7.0],
    "min_confidence_momentum": [40.0, 50.0, 55.0],
    "contrarian_only": [True, False],
    "min_entry_price_cents": [10, 15],
    "max_entry_price_cents": [70, 85],
    "min_market_volume": [0.0, 100.0],
}

DEFAULT_MAIN_COMBINED_GRID = {
    "trade_whales": [True],
    "trade_momentum": [True],
    "min_edge_pts_whale": [3.0, 5.0],
    "min_edge_pts_momentum": [3.0, 5.0],
    "min_confidence_whale": [50.0, 55.0],
    "min_confidence_momentum": [40.0, 55.0],
    "contrarian_only": [True, False],
    "max_entry_price_cents": [70, 85],
    "min_market_volume": [0.0, 100.0],
}

DEFAULT_MAIN_CATEGORY_FILTERS = [
    "sports",
    "crypto",
    "politics",
    "economics",
    "entertainment",
    "world",
    "climate",
    "exotics",
]


def build_grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(param_grid.keys())
    values = [list(param_grid[k]) for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _trade_time(t: dict) -> str:
    return str(t.get("at") or "")


def split_trades(
    trades: list[dict], *, train_pct: float = 0.6, validation_pct: float = 0.2,
) -> dict[str, list[dict]]:
    rows = sorted([dict(t) for t in trades], key=_trade_time)
    n = len(rows)
    train_n = int(n * train_pct)
    val_n = int(n * validation_pct)
    if n and train_n == 0:
        train_n = 1
    if n - train_n > 1 and val_n == 0:
        val_n = 1
    return {
        "train": rows[:train_n],
        "validation": rows[train_n:train_n + val_n],
        "test": rows[train_n + val_n:],
    }


def _max_drawdown(trades: list[dict]) -> float:
    run = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=_trade_time):
        run += float(t.get("pnlUsd") or 0.0)
        peak = max(peak, run)
        max_dd = min(max_dd, run - peak)
    return round(max_dd, 4)


def metrics(trades: list[dict], *, contracts: int = 1) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t.get("won"))
    total = sum(float(t.get("pnlUsd") or 0.0) for t in trades)
    denom = sum(int(t.get("contracts", contracts) or contracts) for t in trades)
    pnls = [float(t.get("pnlUsd") or 0.0) for t in trades]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    return {
        "n": n,
        "wins": wins,
        "winRate": round(wins / n, 4) if n else 0.0,
        "pnlUsd": round(total, 4),
        "avgPnlUsd": round(total / n, 4) if n else 0.0,
        "netEvCentsPerContract": round(total / denom * 100.0, 4) if denom else 0.0,
        "maxDrawdownUsd": _max_drawdown(trades),
        "profitFactor": (gross_win / gross_loss) if gross_loss else (None if gross_win == 0 else 999999.0),
    }


def bootstrap_metrics(
    trades: list[dict], *, contracts: int = 1, samples: int = 200, seed: int = 1337,
) -> dict:
    if not trades or samples <= 0:
        return {
            "samples": 0,
            "meanNetEvCentsPerContract": 0.0,
            "ciLow": 0.0,
            "ciHigh": 0.0,
            "probabilityPositive": 0.0,
        }
    rng = random.Random(seed)
    vals = []
    n = len(trades)
    for _ in range(samples):
        sample = [trades[rng.randrange(n)] for _ in range(n)]
        vals.append(metrics(sample, contracts=contracts)["netEvCentsPerContract"])
    vals.sort()
    lo_i = int(0.025 * (len(vals) - 1))
    hi_i = int(0.975 * (len(vals) - 1))
    return {
        "samples": samples,
        "meanNetEvCentsPerContract": round(sum(vals) / len(vals), 4),
        "ciLow": round(vals[lo_i], 4),
        "ciHigh": round(vals[hi_i], 4),
        "probabilityPositive": round(sum(1 for v in vals if v > 0) / len(vals), 4),
    }


def score_candidate(
    *, candidate_id: str, params: dict, trades: list[dict], contracts: int,
    min_trades: int = 30, bootstrap_samples: int = 200, seed: int = 1337,
    train_pct: float = 0.6, validation_pct: float = 0.2,
) -> dict:
    splits_raw = split_trades(
        trades,
        train_pct=train_pct,
        validation_pct=validation_pct,
    )
    split_metrics = {
        name: metrics(rows, contracts=contracts)
        for name, rows in splits_raw.items()
    }
    reasons = []
    for name in ("train", "validation", "test"):
        if split_metrics[name]["n"] < min_trades:
            reasons.append(f"{name} below minimum sample constraint ({min_trades})")
    if split_metrics["validation"]["netEvCentsPerContract"] <= 0:
        reasons.append("non-positive validation edge")
    if split_metrics["test"]["netEvCentsPerContract"] <= 0:
        reasons.append("non-positive test edge")
    eligible = not reasons
    score = round(
        0.20 * split_metrics["train"]["netEvCentsPerContract"]
        + 0.40 * split_metrics["validation"]["netEvCentsPerContract"]
        + 0.40 * split_metrics["test"]["netEvCentsPerContract"],
        4,
    )
    return {
        "candidateId": candidate_id,
        "params": dict(params),
        "eligible": eligible,
        "score": score,
        "reason": "" if eligible else "; ".join(reasons),
        "splits": split_metrics,
        "bootstrap": bootstrap_metrics(
            splits_raw["validation"] or trades,
            contracts=contracts,
            samples=bootstrap_samples,
            seed=seed,
        ),
        "tradeCount": len(trades),
    }


def rank_candidates(rows: list[dict], *, top_n: int = 20) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda r: (
            1 if r.get("eligible") else 0,
            float(r.get("score") or 0.0),
            float((r.get("splits") or {}).get("test", {}).get("netEvCentsPerContract") or 0.0),
            int(r.get("tradeCount") or 0),
        ),
        reverse=True,
    )
    return ranked[:max(1, int(top_n))]


def _heatmap_value(v: Any) -> Any:
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True)
    return v


def build_heatmap(rows: list[dict], *, x_param: Optional[str] = None, y_param: Optional[str] = None) -> dict:
    param_names = []
    for r in rows:
        for k in (r.get("params") or {}).keys():
            if k not in param_names:
                param_names.append(k)
    if not param_names:
        return {"xParam": "", "yParam": "", "cells": []}
    x_param = x_param or param_names[0]
    y_param = y_param or (param_names[1] if len(param_names) > 1 else param_names[0])
    grouped: dict[tuple[Any, Any], list[dict]] = {}
    for r in rows:
        p = r.get("params") or {}
        grouped.setdefault((_heatmap_value(p.get(x_param)), _heatmap_value(p.get(y_param))), []).append(r)
    cells = []
    for (x, y), vals in grouped.items():
        score = sum(float(v.get("score") or 0.0) for v in vals) / len(vals)
        val_n = sum(int((v.get("splits") or {}).get("validation", {}).get("n") or 0) for v in vals)
        eligible = any(bool(v.get("eligible")) for v in vals)
        cells.append({
            "x": x,
            "y": y,
            "score": round(score, 4),
            "n": val_n,
            "eligible": eligible,
        })
    cells.sort(key=lambda c: (str(c["y"]), str(c["x"])))
    return {"xParam": x_param, "yParam": y_param, "cells": cells}


def _fold_id(at: str, boundaries: list[str]) -> int:
    if not boundaries:
        return 0
    for i, b in enumerate(boundaries):
        if at <= b:
            return i
    return len(boundaries)


def walk_forward(rows: list[dict], *, folds: int = 4, min_trades: int = 30, contracts: int = 1) -> dict:
    all_times = sorted({
        _trade_time(t)
        for r in rows
        for t in (r.get("_trades") or [])
        if _trade_time(t)
    })
    if len(all_times) < 2:
        return {"folds": [], "summary": metrics([], contracts=contracts)}
    folds = max(2, int(folds))
    boundaries = []
    for i in range(1, folds):
        idx = min(len(all_times) - 1, max(0, int(len(all_times) * i / folds) - 1))
        boundaries.append(all_times[idx])

    out_folds = []
    selected_test_trades: list[dict] = []
    for fold in range(1, folds):
        train_rows = []
        for r in rows:
            train_trades = [
                t for t in (r.get("_trades") or [])
                if _fold_id(_trade_time(t), boundaries) < fold
            ]
            train_m = metrics(train_trades, contracts=contracts)
            if train_m["n"] >= min_trades:
                train_rows.append({**r, "score": train_m["netEvCentsPerContract"], "trainMetrics": train_m})
        if not train_rows:
            out_folds.append({"fold": fold, "selectedCandidateId": None, "metrics": metrics([], contracts=contracts)})
            continue
        selected = rank_candidates(train_rows, top_n=1)[0]
        test_trades = [
            t for t in (selected.get("_trades") or [])
            if _fold_id(_trade_time(t), boundaries) == fold
        ]
        selected_test_trades.extend(test_trades)
        out_folds.append({
            "fold": fold,
            "selectedCandidateId": selected.get("candidateId"),
            "params": selected.get("params") or {},
            "train": selected.get("trainMetrics"),
            "metrics": metrics(test_trades, contracts=contracts),
        })
    return {
        "folds": out_folds,
        "summary": metrics(selected_test_trades, contracts=contracts),
    }


def _default_grid(cfg: dict) -> dict[str, list[Any]]:
    if str(cfg.get("crypto15m_strategy_mode")) == "btc_ma_crossover":
        return DEFAULT_C15_GRID_BTC_MA
    if str(cfg.get("crypto15m_direction_mode")) == "model":
        return DEFAULT_C15_GRID_MODEL
    return DEFAULT_C15_GRID_DIRECTIONAL


def _default_main_grid(source_mode: str) -> dict[str, list[Any]]:
    mode = (source_mode or "whale").lower()
    if mode == "momentum":
        return DEFAULT_MAIN_MOMENTUM_GRID
    if mode == "combined":
        return DEFAULT_MAIN_COMBINED_GRID
    return DEFAULT_MAIN_WHALE_GRID


def _with_main_source_flags(param_grid: dict[str, list[Any]], source_mode: str) -> dict[str, list[Any]]:
    out = {str(k): list(v) for k, v in (param_grid or {}).items()}
    mode = (source_mode or "whale").lower()
    if mode == "momentum":
        out["trade_whales"] = [False]
        out["trade_momentum"] = [True]
    elif mode == "combined":
        out["trade_whales"] = [True]
        out["trade_momentum"] = [True]
    else:
        out["trade_whales"] = [True]
        out["trade_momentum"] = [False]
    return out


def optimize_crypto15m(
    cfg: dict, *, env: str = "production", since_days: int = 60,
    param_grid: Optional[dict[str, list[Any]]] = None, min_trades: int = 30,
    top_n: int = 20, bootstrap_samples: int = 200, seed: int = 1337,
    base_profile_id: str = "", base_profile_name: str = "",
) -> dict:
    base_cfg = merge_with_defaults(dict(cfg))
    grid = build_grid(param_grid or _default_grid(base_cfg))
    candidates = []
    dataset = {}
    contracts = max(1, int(base_cfg.get("crypto15m_order_size") or 1))
    for idx, params in enumerate(grid, start=1):
        run_cfg = merge_with_defaults({**base_cfg, **params})
        result = replay.replay(run_cfg, env=env, since_days=since_days)
        dataset = result.get("dataset") or dataset
        trades = result.get("trades") or []
        row = score_candidate(
            candidate_id=f"grid-{idx:04d}",
            params=params,
            trades=trades,
            contracts=contracts,
            min_trades=min_trades,
            bootstrap_samples=bootstrap_samples,
            seed=seed + idx,
        )
        row["backtest"] = {
            "n": result.get("n", 0),
            "windowsScanned": result.get("windowsScanned", 0),
            "rejectionTotal": result.get("rejectionTotal", 0),
        }
        row["_trades"] = trades
        candidates.append(row)
    ranked = rank_candidates(candidates, top_n=top_n)
    public_ranked = [{k: v for k, v in r.items() if k != "_trades"} for r in ranked]
    public_candidates = [{k: v for k, v in r.items() if k != "_trades"} for r in candidates]
    out = {
        "engine": "crypto15m",
        "env": env,
        "sinceDays": int(since_days),
        "gridSize": len(grid),
        "minTrades": int(min_trades),
        "baseProfileId": str(base_profile_id or ""),
        "baseProfileName": str(base_profile_name or ""),
        "baseConfig": dict(base_cfg),
        "optimizerRunId": f"opt-{env}-{since_days}-{seed}-{len(grid)}",
        "paramGrid": param_grid or _default_grid(base_cfg),
        "dataset": dataset,
        "ranked": public_ranked,
        "candidates": public_candidates,
        "heatmap": build_heatmap(candidates),
        "walkForward": walk_forward(
            candidates,
            folds=4,
            min_trades=max(1, int(min_trades)),
            contracts=contracts,
        ),
        "caveats": [
            "Optimization is in-sample unless validation/test and walk-forward remain positive.",
            "Minimum-sample constraints rank low-trade candidates behind eligible candidates.",
            "Bootstrap samples resample observed trades; they do not model missing market data or fill slippage.",
        ],
    }
    out["research"] = ai_research.analyze_optimization(out)
    return out


def optimize_main_engine(
    cfg: dict, *, env: str = "production", since_days: int = 60,
    param_grid: Optional[dict[str, list[Any]]] = None, source_mode: str = "whale",
    min_trades: int = 30, top_n: int = 20, bootstrap_samples: int = 200,
    seed: int = 1337, fixed_risk_usd: float = 1.0,
    slippage_cents: float = 1.0, execution_model: str = "recorded_ask",
    train_pct: float = 0.6, validation_pct: float = 0.2,
    walk_forward_folds: int = 4, optimizer_kind: Optional[str] = None,
    base_profile_id: str = "", base_profile_name: str = "",
) -> dict:
    """Optimize main-engine gates against the main replay pipeline.

    This intentionally does not reuse the crypto15m parameter grid. Main-engine
    optimization is source-aware so whales, momentum, and combined runs can be
    evaluated independently before promoting any profile.
    """
    base_cfg = merge_with_defaults(dict(cfg))
    source_mode = (source_mode or "whale").lower()
    param_grid = param_grid or _default_main_grid(source_mode)
    grid = build_grid(param_grid)
    candidates = []
    contracts = 1
    for idx, params in enumerate(grid, start=1):
        run_cfg = merge_with_defaults({**base_cfg, **params})
        result = main_replay.replay_main_engine(
            run_cfg,
            since_days=since_days,
            slippage_cents=slippage_cents,
            fixed_risk_usd=fixed_risk_usd,
            execution_model=execution_model,
        )
        trades = result.get("trades") or []
        row = score_candidate(
            candidate_id=f"main-grid-{idx:04d}",
            params=params,
            trades=trades,
            contracts=contracts,
            min_trades=min_trades,
            bootstrap_samples=bootstrap_samples,
            seed=seed + idx,
            train_pct=train_pct,
            validation_pct=validation_pct,
        )
        funnel = result.get("funnel") or {}
        row["backtest"] = {
            "n": result.get("n", 0),
            "signalsScanned": result.get("signalsScanned", result.get("windowsScanned", 0)),
            "rejectionTotal": funnel.get("rejected", result.get("rejectionTotal", 0)),
        }
        row["_trades"] = trades
        candidates.append(row)

    ranked = rank_candidates(candidates, top_n=top_n)
    public_ranked = [{k: v for k, v in r.items() if k != "_trades"} for r in ranked]
    public_candidates = [{k: v for k, v in r.items() if k != "_trades"} for r in candidates]
    out = {
        "engine": "main",
        "env": env,
        "sourceMode": source_mode,
        "optimizerKind": optimizer_kind or f"main_{source_mode}",
        "sinceDays": int(since_days),
        "gridSize": len(grid),
        "minTrades": int(min_trades),
        "baseProfileId": str(base_profile_id or ""),
        "baseProfileName": str(base_profile_name or ""),
        "baseConfig": dict(base_cfg),
        "optimizerRunId": f"main-opt-{source_mode}-{env}-{since_days}-{seed}-{len(grid)}",
        "paramGrid": param_grid,
        "splitPolicy": {
            "trainPct": float(train_pct),
            "validationPct": float(validation_pct),
            "testPct": round(max(0.0, 1.0 - float(train_pct) - float(validation_pct)), 4),
        },
        "walkForwardFolds": int(walk_forward_folds),
        "fixedRiskUsd": float(fixed_risk_usd),
        "slippageCents": float(slippage_cents),
        "executionModel": execution_model,
        "ranked": public_ranked,
        "candidates": public_candidates,
        "heatmap": build_heatmap(candidates),
        "walkForward": walk_forward(
            candidates,
            folds=walk_forward_folds,
            min_trades=max(1, int(min_trades)),
            contracts=contracts,
        ),
        "caveats": [
            "Main-engine optimization replays recorded whale/momentum signals only; app-off periods are absent from the sample.",
            "Default grids are staged by signal source. Run whale, momentum, and combined modes separately before trusting a merged profile.",
            "Fixed $1 risk is used by default so entry quality is evaluated separately from aggressive live position sizing.",
            "Eligibility requires positive validation/test edge and minimum samples; low-trade candidates are ranked behind eligible candidates.",
        ],
    }
    out["research"] = ai_research.analyze_optimization(out)
    return out


def optimize_main_whales(
    cfg: dict, *, param_grid: Optional[dict[str, list[Any]]] = None, **kwargs,
) -> dict:
    grid = _with_main_source_flags(param_grid or DEFAULT_MAIN_WHALE_GRID, "whale")
    return optimize_main_engine(
        cfg,
        param_grid=grid,
        source_mode="whale",
        optimizer_kind="main_whale",
        **kwargs,
    )


def optimize_main_momentum(
    cfg: dict, *, param_grid: Optional[dict[str, list[Any]]] = None, **kwargs,
) -> dict:
    grid = _with_main_source_flags(param_grid or DEFAULT_MAIN_MOMENTUM_GRID, "momentum")
    return optimize_main_engine(
        cfg,
        param_grid=grid,
        source_mode="momentum",
        optimizer_kind="main_momentum",
        **kwargs,
    )


def optimize_main_category_filters(
    cfg: dict, *, categories: Optional[list[str]] = None,
    source_mode: str = "combined", param_grid: Optional[dict[str, list[Any]]] = None,
    **kwargs,
) -> dict:
    cats = [str(c) for c in (categories or DEFAULT_MAIN_CATEGORY_FILTERS) if str(c)]
    base_grid = {
        "allowed_categories": [[c] for c in cats],
    }
    if param_grid:
        base_grid.update({str(k): list(v) for k, v in param_grid.items()})
    grid = _with_main_source_flags(base_grid, source_mode)
    return optimize_main_engine(
        cfg,
        param_grid=grid,
        source_mode=source_mode,
        optimizer_kind="main_category_filter",
        **kwargs,
    )


def export_ai_experiment_zip(opt: dict, out_dir: str) -> str:
    return ai_research.export_experiment_zip(opt, out_dir)


def _parse_grid(raw: Optional[str]) -> Optional[dict[str, list[Any]]]:
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--grid must be a JSON object")
    return {str(k): list(v if isinstance(v, list) else [v]) for k, v in data.items()}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Optimize Krypt backtest parameters")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c15 = sub.add_parser("c15", help="optimize 15m crypto strategy")
    c15.add_argument("since_days", nargs="?", type=int, default=60)
    c15.add_argument("min_trades_pos", nargs="?", type=int)
    c15.add_argument("bootstrap_samples_pos", nargs="?", type=int)
    c15.add_argument("top_n_pos", nargs="?", type=int)
    c15.add_argument("--env", default="production")
    c15.add_argument("--grid", default="")
    c15.add_argument("--min-trades", type=int, default=30)
    c15.add_argument("--top-n", type=int, default=20)
    c15.add_argument("--bootstrap-samples", type=int, default=200)
    mainp = sub.add_parser("main", help="optimize main-engine whale/momentum gates")
    mainp.add_argument("since_days", nargs="?", type=int, default=60)
    mainp.add_argument("min_trades_pos", nargs="?", type=int)
    mainp.add_argument("bootstrap_samples_pos", nargs="?", type=int)
    mainp.add_argument("top_n_pos", nargs="?", type=int)
    mainp.add_argument("--env", default="production")
    mainp.add_argument("--source-mode", choices=["whale", "momentum", "combined"], default="whale")
    mainp.add_argument("--grid", default="")
    mainp.add_argument("--min-trades", type=int, default=30)
    mainp.add_argument("--top-n", type=int, default=20)
    mainp.add_argument("--bootstrap-samples", type=int, default=200)
    mainp.add_argument("--fixed-risk-usd", type=float, default=1.0)
    mainp.add_argument("--slippage-cents", type=float, default=1.0)
    mainp.add_argument("--execution-model", default="recorded_ask")
    args = ap.parse_args(argv)
    if args.cmd == "c15":
        out = optimize_crypto15m(
            merge_with_defaults({}),
            env=args.env,
            since_days=args.since_days,
            param_grid=_parse_grid(args.grid),
            min_trades=args.min_trades_pos or args.min_trades,
            top_n=args.top_n_pos or args.top_n,
            bootstrap_samples=args.bootstrap_samples_pos or args.bootstrap_samples,
        )
        print(json.dumps(out, indent=2, sort_keys=True))
    elif args.cmd == "main":
        out = optimize_main_engine(
            merge_with_defaults({}),
            env=args.env,
            since_days=args.since_days,
            param_grid=_parse_grid(args.grid),
            source_mode=args.source_mode,
            min_trades=args.min_trades_pos or args.min_trades,
            top_n=args.top_n_pos or args.top_n,
            bootstrap_samples=args.bootstrap_samples_pos or args.bootstrap_samples,
            fixed_risk_usd=args.fixed_risk_usd,
            slippage_cents=args.slippage_cents,
            execution_model=args.execution_model,
        )
        print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
