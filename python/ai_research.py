from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _edge(row: dict, split: str) -> float:
    try:
        return float(((row.get("splits") or {}).get(split) or {}).get("netEvCentsPerContract") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _metric(row: dict, split: str, key: str) -> float:
    try:
        return float(((row.get("splits") or {}).get(split) or {}).get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _trade_count(row: dict) -> int:
    try:
        return int(row.get("tradeCount") or 0)
    except (TypeError, ValueError):
        return 0


def _bootstrap_positive(row: dict) -> float:
    try:
        return float((row.get("bootstrap") or {}).get("probabilityPositive") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _walk_forward_edge(opt: dict) -> float:
    try:
        return float(((opt.get("walkForward") or {}).get("summary") or {}).get("netEvCentsPerContract") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _research_score(row: dict) -> float:
    return (
        _edge(row, "validation") * 0.50
        + _edge(row, "test") * 0.35
        + (_bootstrap_positive(row) - 0.5) * 4.0
        + min(_trade_count(row), 100) * 0.005
    )


def _candidate_sort_key(row: dict) -> tuple:
    return (
        1 if row.get("eligible") else 0,
        _research_score(row),
        _edge(row, "validation"),
        _edge(row, "test"),
        _trade_count(row),
    )


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _round(v: float, ndigits: int = 4) -> float:
    return round(float(v), ndigits)


def _snake_to_camel(name: str) -> str:
    parts = str(name).split("_")
    if not parts:
        return str(name)
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _config_key_to_camel(name: str) -> str:
    s = str(name)
    return _snake_to_camel(s) if "_" in s else s


def _json_value(v: Any) -> Any:
    if isinstance(v, (str, bool)) or v is None:
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return _round(v)
    if isinstance(v, list):
        return [_json_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_value(val) for k, val in v.items()}
    return str(v)


def _group_key(v: Any) -> str:
    return json.dumps(_json_value(v), sort_keys=True, separators=(",", ":"))


def _param_label(k: str) -> str:
    return str(k).replace("crypto15m_", "").replace("_", " ")


def _eligible_or_all(candidates: list[dict]) -> list[dict]:
    eligible = [c for c in candidates if c.get("eligible")]
    return eligible or list(candidates)


def feature_attribution(candidates: list[dict]) -> list[dict]:
    rows = _eligible_or_all(candidates)
    params: list[str] = []
    for row in rows:
        for k in (row.get("params") or {}).keys():
            if k not in params:
                params.append(k)

    out = []
    for param in params:
        grouped: dict[str, list[dict]] = {}
        values_by_key: dict[str, Any] = {}
        for row in rows:
            if param in (row.get("params") or {}):
                raw_value = (row.get("params") or {}).get(param)
                key = _group_key(raw_value)
                grouped.setdefault(key, []).append(row)
                values_by_key[key] = raw_value
        if len(grouped) < 2:
            continue

        values = []
        for key, vals in grouped.items():
            value = values_by_key.get(key)
            values.append({
                "value": _json_value(value),
                "candidates": len(vals),
                "validationEdgeCents": _round(_mean([_edge(v, "validation") for v in vals])),
                "testEdgeCents": _round(_mean([_edge(v, "test") for v in vals])),
                "bootstrapPositive": _round(_mean([_bootstrap_positive(v) for v in vals])),
                "score": _round(_mean([_research_score(v) for v in vals])),
            })
        values.sort(key=lambda v: (float(v["score"]), float(v["validationEdgeCents"])), reverse=True)
        best = values[0]
        worst = values[-1]
        lift = float(best["validationEdgeCents"]) - float(worst["validationEdgeCents"])
        out.append({
            "feature": param,
            "label": _param_label(param),
            "bestValue": best["value"],
            "worstValue": worst["value"],
            "liftCents": _round(lift),
            "bestValidationEdgeCents": best["validationEdgeCents"],
            "worstValidationEdgeCents": worst["validationEdgeCents"],
            "bestTestEdgeCents": best["testEdgeCents"],
            "worstTestEdgeCents": worst["testEdgeCents"],
            "values": values,
            "confidence": (
                "higher" if min(v["candidates"] for v in values) >= 3 and abs(lift) >= 1.0
                else "directional"
            ),
            "takeaway": (
                f"{_param_label(param)}={best['value']} led this grid by "
                f"{_round(lift, 2)}c validation edge versus {worst['value']}."
            ),
        })
    out.sort(key=lambda r: (abs(float(r["liftCents"])), float(r["bestValidationEdgeCents"])), reverse=True)
    return out


def winner_loser_comparisons(ranked: list[dict], candidates: list[dict]) -> list[dict]:
    rows = list(ranked or sorted(candidates, key=_candidate_sort_key, reverse=True))
    usable = _eligible_or_all(rows)
    if len(usable) < 2:
        return []

    winner = sorted(usable, key=_candidate_sort_key, reverse=True)[0]
    loser = sorted(
        [r for r in usable if r.get("candidateId") != winner.get("candidateId")],
        key=_candidate_sort_key,
    )[0]

    param_deltas = {}
    winner_params = winner.get("params") or {}
    loser_params = loser.get("params") or {}
    for k in sorted(set(winner_params.keys()) | set(loser_params.keys())):
        wv = winner_params.get(k)
        lv = loser_params.get(k)
        if wv != lv:
            param_deltas[k] = {
                "winner": _json_value(wv),
                "loser": _json_value(lv),
                "change": f"{_json_value(lv)} -> {_json_value(wv)}",
            }

    return [{
        "winnerId": winner.get("candidateId"),
        "loserId": loser.get("candidateId"),
        "metricDeltas": {
            "trainEdgeCents": _round(_edge(winner, "train") - _edge(loser, "train")),
            "validationEdgeCents": _round(_edge(winner, "validation") - _edge(loser, "validation")),
            "testEdgeCents": _round(_edge(winner, "test") - _edge(loser, "test")),
            "testPnlUsd": _round(_metric(winner, "test", "pnlUsd") - _metric(loser, "test", "pnlUsd")),
            "bootstrapPositive": _round(_bootstrap_positive(winner) - _bootstrap_positive(loser)),
        },
        "parameterDeltas": param_deltas,
        "explanation": (
            f"{winner.get('candidateId')} beats {loser.get('candidateId')} on validation "
            f"({_edge(winner, 'validation'):.2f}c vs {_edge(loser, 'validation'):.2f}c) "
            f"and test ({_edge(winner, 'test'):.2f}c vs {_edge(loser, 'test'):.2f}c)."
        ),
    }]


def _unique(vals: list[Any]) -> list[Any]:
    out = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out


def _top_param_grid(attributions: list[dict], ranked: list[dict]) -> dict[str, list[Any]]:
    grid: dict[str, list[Any]] = {}
    for attr in attributions[:3]:
        feature = str(attr.get("feature") or "")
        vals = [v.get("value") for v in attr.get("values", [])[:2]]
        if feature and vals:
            grid[feature] = _unique(vals)
    if grid:
        return grid
    top = ranked[0] if ranked else {}
    return {k: [_json_value(v)] for k, v in (top.get("params") or {}).items()}


def suggested_experiments(opt: dict, attributions: list[dict], ranked: list[dict]) -> list[dict]:
    min_trades = max(1, int(opt.get("minTrades") or 1))
    top_grid = _top_param_grid(attributions, ranked)
    out = []
    if top_grid:
        out.append({
            "id": "exp-narrow-top-features",
            "title": "Narrow strongest parameter ranges",
            "priority": "high",
            "rationale": "Retest the strongest observed values with tighter ranges before considering a profile.",
            "paramGrid": top_grid,
            "minTrades": max(min_trades, int(min_trades * 1.5)),
            "bootstrapSamples": 500,
            "requiresHumanApproval": True,
        })
    if ranked:
        top_params = ranked[0].get("params") or {}
        out.append({
            "id": "exp-sample-pressure",
            "title": "Raise the sample constraint",
            "priority": "medium",
            "rationale": "Force the apparent winner to survive a larger train and validation sample.",
            "paramGrid": {k: [_json_value(v)] for k, v in top_params.items()},
            "minTrades": max(min_trades + 5, min_trades * 2),
            "bootstrapSamples": 750,
            "requiresHumanApproval": True,
        })
    if len(ranked) >= 2:
        compare_grid: dict[str, list[Any]] = {}
        for row in ranked[:2]:
            for k, v in (row.get("params") or {}).items():
                compare_grid.setdefault(k, []).append(_json_value(v))
        compare_grid = {k: _unique(v) for k, v in compare_grid.items()}
        out.append({
            "id": "exp-winner-runnerup-ablation",
            "title": "Winner versus runner-up ablation",
            "priority": "medium",
            "rationale": "Retest only the best two neighborhoods to see whether the ranking is stable.",
            "paramGrid": compare_grid,
            "minTrades": min_trades,
            "bootstrapSamples": 500,
            "requiresHumanApproval": True,
        })
    return out


def _engine_kind(opt: dict) -> str:
    return "main" if str(opt.get("engine") or "").lower() == "main" else "crypto15m"


def _profile_patch(params: dict, *, kind: str = "crypto15m") -> dict:
    patch = {_snake_to_camel(k): _json_value(v) for k, v in params.items()}
    patch["enableTrading"] = False
    if kind == "crypto15m":
        patch["crypto15mLive"] = False
    return patch


def _python_patch(params: dict, *, kind: str = "crypto15m") -> dict:
    patch = {str(k): _json_value(v) for k, v in params.items()}
    patch["enable_trading"] = False
    if kind == "crypto15m":
        patch["crypto15m_live"] = False
    return patch


def _candidate_rejection_reason(row: dict, *, min_trades: int, walk_forward_edge: float) -> str:
    splits = row.get("splits") or {}
    train_n = int((splits.get("train") or {}).get("n") or 0)
    val_n = int((splits.get("validation") or {}).get("n") or 0)
    test_n = int((splits.get("test") or {}).get("n") or 0)
    if not row.get("eligible"):
        return str(row.get("reason") or f"below minimum sample constraint ({min_trades})")
    if train_n < min_trades or val_n < min_trades or test_n < min_trades:
        return f"below train/validation/test sample constraint ({min_trades})"
    if _edge(row, "validation") <= 0:
        return "non-positive validation edge"
    if _edge(row, "test") <= 0:
        return "negative test edge" if _edge(row, "test") < 0 else "non-positive test edge"
    if walk_forward_edge <= 0:
        return "non-positive walk-forward edge"
    return ""


def accepted_candidates(ranked: list[dict], opt: dict) -> list[dict]:
    min_trades = max(1, int(opt.get("minTrades") or 1))
    walk_forward_edge = _walk_forward_edge(opt)
    rows = []
    for row in ranked:
        reason = _candidate_rejection_reason(
            row,
            min_trades=min_trades,
            walk_forward_edge=walk_forward_edge,
        )
        if reason:
            continue
        rows.append(row)
    return sorted(rows, key=_candidate_sort_key, reverse=True)


def _full_profile_config(base_config: dict, params: dict, *, kind: str = "crypto15m") -> dict:
    full = dict(base_config or {})
    full.update(params or {})
    full["enable_trading"] = False
    if kind == "crypto15m":
        full["crypto15m_live"] = False
    return {_config_key_to_camel(k): _json_value(v) for k, v in full.items()}


def _changed_config(base_config: dict, params: dict) -> list[dict]:
    out = []
    for k, v in (params or {}).items():
        old = (base_config or {}).get(k)
        ck = _config_key_to_camel(k)
        out.append({
            "key": ck,
            "label": _param_label(k),
            "from": _json_value(old),
            "to": _json_value(v),
        })
    return out


def _preserved_summary(config: dict) -> list[str]:
    out = []
    strategy = config.get("crypto15m_strategy_mode") or config.get("crypto15mStrategyMode")
    assets = config.get("crypto15m_assets") or config.get("crypto15mAssets")
    if strategy == "btc_ma_crossover":
        out.append("BTC MA Crossover")
    if isinstance(assets, list) and assets:
        out.append(f"{', '.join(str(a) for a in assets)} only")
    fast = config.get("crypto15m_fast_ema_period") or config.get("crypto15mFastEmaPeriod")
    slow = config.get("crypto15m_slow_sma_period") or config.get("crypto15mSlowSmaPeriod")
    trend = config.get("crypto15m_trend_sma_period") or config.get("crypto15mTrendSmaPeriod")
    trend_tf = config.get("crypto15m_trend_timeframe_min") or config.get("crypto15mTrendTimeframeMin")
    if fast:
        out.append(f"EMA {fast}")
    if slow:
        out.append(f"SMA {slow}")
    if trend:
        out.append(f"SMA {trend} ({trend_tf or 5}m)")
    size = config.get("crypto15m_order_size") or config.get("crypto15mOrderSize")
    if size:
        out.append(f"{size} contracts")
    return out


def candidate_profiles(
    ranked: list[dict], opt: dict, *, top_n_profiles: int = 3,
) -> list[dict]:
    rows = accepted_candidates(ranked, opt)[:max(1, int(top_n_profiles))]
    kind = _engine_kind(opt)
    base_config = dict(opt.get("baseConfig") or {})
    base_profile_id = str(opt.get("baseProfileId") or "")
    base_profile_name = str(opt.get("baseProfileName") or "")
    optimizer_run_id = str(opt.get("optimizerRunId") or "")
    source_mode = str(opt.get("sourceMode") or "")
    out = []
    for i, row in enumerate(rows, start=1):
        params = row.get("params") or {}
        cid = str(row.get("candidateId") or f"candidate-{i}")
        profile_id = f"research-{cid}"
        name = f"AI Research {'Main' if kind == 'main' else 'Crypto15m'} Candidate {i}"
        description = (
            f"Generated from optimizer candidate {cid}: validation "
            f"{_edge(row, 'validation'):.2f}c, test {_edge(row, 'test'):.2f}c, "
            f"bootstrap positive {_bootstrap_positive(row) * 100:.0f}%."
        )
        patch = _profile_patch(params, kind=kind)
        full_config = _full_profile_config(base_config, params, kind=kind)
        profile_export = {
            "kryptTraderProfile": 1,
            "profile": {
                "id": profile_id,
                "name": name,
                "description": description,
                "kind": kind,
                "baseProfileId": base_profile_id,
                "baseProfileName": base_profile_name,
                "generatedBy": "optimizer",
                "optimizerRunId": optimizer_run_id,
                "candidateId": cid,
                "sourceMode": source_mode,
                "configMode": "full",
                "createdAt": _utc_now(),
                "updatedAt": _utc_now(),
                "config": full_config,
            },
        }
        out.append({
            "id": profile_id,
            "name": name,
            "kind": kind,
            "sourceCandidateId": cid,
            "description": description,
            "patch": patch,
            "pythonConfigPatch": _python_patch(params, kind=kind),
            "exportProfile": profile_export,
            "configMode": "full",
            "baseProfileId": base_profile_id,
            "baseProfileName": base_profile_name,
            "changedConfig": _changed_config(base_config, params),
            "preservedSummary": _preserved_summary(base_config),
            "metrics": {
                "trainEdgeCents": _round(_edge(row, "train")),
                "validationEdgeCents": _round(_edge(row, "validation")),
                "testEdgeCents": _round(_edge(row, "test")),
                "bootstrapPositive": _round(_bootstrap_positive(row)),
                "tradeCount": _trade_count(row),
            },
            "approval": {
                "required": True,
                "status": "accepted_for_research",
                "reason": "Generated from research results only; review the evidence before enabling live trading.",
            },
            "deployment": {
                "liveAllowed": False,
                "blockedUntil": "human_approval",
                "notes": [
                    "The generated patch explicitly keeps enableTrading=false.",
                    "The generated patch explicitly keeps crypto15mLive=false.",
                    "No endpoint applies this profile to live deployment automatically.",
                ],
            },
        })
    return out


def _summary(opt: dict, ranked: list[dict], profiles: list[dict], status: str) -> str:
    if not ranked:
        return "No optimizer candidates were available to analyze."
    if not profiles:
        return status
    best = ranked[0]
    eligible = sum(1 for c in opt.get("candidates", []) if c.get("eligible"))
    total = len(opt.get("candidates", []) or ranked)
    return (
        f"{best.get('candidateId')} is the current research winner with "
        f"{_edge(best, 'validation'):.2f}c validation edge and {_edge(best, 'test'):.2f}c "
        f"test edge. {eligible}/{total} candidates met the sample constraint. "
        f"{len(profiles)} research profiles were generated with live deployment blocked."
    )


def _eligibility_rules(opt: dict) -> dict:
    return {
        "minTradesPerSplit": max(1, int(opt.get("minTrades") or 1)),
        "requiresPositiveValidation": True,
        "requiresPositiveTest": True,
        "requiresPositiveWalkForward": True,
    }


def out_of_sample_rejections(ranked: list[dict], opt: dict) -> list[dict]:
    min_trades = max(1, int(opt.get("minTrades") or 1))
    walk_forward_edge = _walk_forward_edge(opt)
    out = []
    for row in ranked:
        splits = row.get("splits") or {}
        train_n = int((splits.get("train") or {}).get("n") or 0)
        val_n = int((splits.get("validation") or {}).get("n") or 0)
        test_n = int((splits.get("test") or {}).get("n") or 0)
        reasons = []
        if train_n < min_trades or val_n < min_trades or test_n < min_trades:
            reasons.append(f"below train/validation/test sample constraint ({min_trades})")
        if _edge(row, "validation") <= 0:
            reasons.append("non-positive validation edge")
        if _edge(row, "test") <= 0:
            reasons.append("negative test edge" if _edge(row, "test") < 0 else "non-positive test edge")
        if walk_forward_edge <= 0:
            reasons.append("non-positive walk-forward edge")
        if not row.get("eligible") and row.get("reason") and str(row.get("reason")) not in reasons:
            reasons.append(str(row.get("reason")))
        if not reasons:
            continue
        out.append({
            "candidateId": row.get("candidateId"),
            "reason": "; ".join(reasons),
            "reasons": reasons,
            "validationEdgeCents": _round(_edge(row, "validation")),
            "testEdgeCents": _round(_edge(row, "test")),
            "walkForwardEdgeCents": _round(walk_forward_edge),
            "splitCounts": {
                "train": int(((row.get("splits") or {}).get("train") or {}).get("n") or 0),
                "validation": int(((row.get("splits") or {}).get("validation") or {}).get("n") or 0),
                "test": int(((row.get("splits") or {}).get("test") or {}).get("n") or 0),
            },
        })
    return out


def build_research_report(opt: dict, ranked: list[dict], profiles: list[dict], status: str) -> dict:
    engine = str(opt.get("engine") or "unknown")
    optimizer_kind = str(opt.get("optimizerKind") or opt.get("sourceMode") or engine)
    lines = [
        f"# {engine} optimizer research report",
        "",
        f"- Optimizer: {optimizer_kind}",
        f"- Run ID: {opt.get('optimizerRunId') or ''}",
        f"- Candidates: {len(opt.get('candidates') or ranked)}",
        f"- Status: {status}",
    ]
    if ranked:
        best = ranked[0]
        lines.extend([
            "",
            "## Current Winner",
            f"- Candidate: {best.get('candidateId')}",
            f"- Validation edge: {_edge(best, 'validation'):.2f}c",
            f"- Test edge: {_edge(best, 'test'):.2f}c",
            f"- Bootstrap positive: {_bootstrap_positive(best) * 100:.1f}%",
        ])
    lines.extend([
        "",
        "## Deployment Guard",
        "- Generated profiles are research artifacts only.",
        "- Live trading remains disabled in every generated profile.",
        f"- Profiles generated: {len(profiles)}",
    ])
    return {
        "format": "markdown",
        "title": f"{engine} optimizer research report",
        "body": "\n".join(lines),
    }


def analyze_optimization(opt: dict, *, top_n_profiles: int = 3) -> dict:
    candidates = [dict(c) for c in (opt.get("candidates") or [])]
    ranked = [dict(c) for c in (opt.get("ranked") or [])]
    if not ranked and candidates:
        ranked = sorted(candidates, key=_candidate_sort_key, reverse=True)

    accepted = accepted_candidates(ranked, opt)
    status = "ok" if accepted else "No candidate passed out-of-sample requirements"
    attributions = feature_attribution(candidates)
    comparisons = winner_loser_comparisons(ranked, candidates)
    experiments = suggested_experiments(opt, attributions, ranked)
    profiles = candidate_profiles(ranked, opt, top_n_profiles=top_n_profiles) if ranked else []
    report = build_research_report(opt, ranked, profiles, status)

    return {
        "packageVersion": 1,
        "analysisMode": "local_research_ai",
        "generatedAt": _utc_now(),
        "status": status,
        "researchWinner": accepted[0].get("candidateId") if accepted else None,
        "summary": _summary(opt, ranked, profiles, status),
        "researchReport": report,
        "featureAttribution": attributions,
        "winnerLoserComparisons": comparisons,
        "suggestedExperiments": experiments,
        "candidateProfiles": profiles,
        "eligibilityRules": _eligibility_rules(opt),
        "outOfSampleRejections": out_of_sample_rejections(ranked, opt),
        "deploymentPolicy": {
            "requiresHumanApproval": True,
            "liveDeploymentAllowed": False,
            "blockedActions": [
                "auto_apply_profile",
                "auto_enable_crypto15m_live",
                "auto_enable_trading",
            ],
        },
        "caveats": [
            "Feature attribution is observational over this grid, not causal proof.",
            "Generated profiles are research candidates and are not applied to live trading.",
            "Human approval is required before any live deployment or config change.",
        ] + ([] if profiles else ["No generated profiles: every candidate failed sample, test-edge, or walk-forward requirements."]),
    }


def export_experiment_zip(opt: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    enriched = dict(opt)
    research = dict(enriched.get("research") or analyze_optimization(enriched))
    enriched["research"] = research
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    engine = str(enriched.get("engine") or "optimizer")
    run_id = str(enriched.get("optimizerRunId") or f"{engine}-{ts}")
    zip_path = os.path.join(out_dir, f"ai_experiment_{run_id}_{ts}.zip")
    files = [
        "manifest.json",
        "optimization.json",
        "research.json",
        "research_report.md",
        "suggested_experiments.json",
    ]
    profile_files = []
    for profile in research.get("candidateProfiles") or []:
        pid = str(profile.get("id") or profile.get("sourceCandidateId") or "candidate")
        path = f"candidate_profiles/{pid}.kryptprofile.json"
        profile_files.append((path, profile.get("exportProfile") or {}))
        files.append(path)
    manifest = {
        "generatedAt": _utc_now(),
        "engine": engine,
        "optimizerRunId": run_id,
        "optimizerKind": enriched.get("optimizerKind"),
        "sourceMode": enriched.get("sourceMode"),
        "candidateProfiles": len(profile_files),
        "files": files,
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        zf.writestr("optimization.json", json.dumps(enriched, indent=2, default=str))
        zf.writestr("research.json", json.dumps(research, indent=2, default=str))
        zf.writestr(
            "research_report.md",
            str((research.get("researchReport") or {}).get("body") or ""),
        )
        zf.writestr(
            "suggested_experiments.json",
            json.dumps(research.get("suggestedExperiments") or [], indent=2, default=str),
        )
        for path, profile_export in profile_files:
            zf.writestr(path, json.dumps(profile_export, indent=2, default=str))
    return zip_path
