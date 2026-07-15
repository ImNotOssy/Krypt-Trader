from __future__ import annotations
from typing import Any

import rules


DEFAULT_CONFIG: dict[str, Any] = {
    "kalshi_env": "demo",
    "enable_trading": False,

    "trade_whales": True,
    "trade_momentum": True,
    "trade_convergence": False,

    "min_edge_pts_whale": 5.0,
    "min_edge_pts_momentum": 5.0,
    "min_confidence_whale": 55.0,
    "min_confidence_momentum": 55.0,
    "fee_aware_edge": True,
    "max_entry_slippage_cents": 5,
    "min_market_volume": 100.0,
    "max_trade_age_min": 15,
    "min_entry_price_cents": 15,
    "max_entry_price_cents": 85,
    "max_resolution_days": 30,
    "allowed_momentum_signal_types": ["trade_cluster"],
    "allowed_categories": None,
    "allowed_whale_categories": None,
    "allowed_momentum_categories": None,
    "contrarian_only": True,

    "gambling_mode": False,
    "gambling_trade_probability": 0.10,

    "sizing_mode": "percent",
    "fixed_trade_usd": 5.0,

    "base_size_fraction": 0.03,
    "min_size_fraction": 0.02,
    "max_size_fraction": 0.06,
    "sizing_base_edge": 5.0,
    "sizing_max_edge": 10.0,
    "hard_max_position_usd": 50.0,
    "min_cash_reserve_fraction": 0.05,

    "order_style": "limit_cross",
    "cross_spread_fallback_offset": 2,
    "order_expiration_sec": 90,

    "max_open_positions": 25,
    "max_positions_per_event": 1,
    "max_daily_new_positions": 40,
    "unlimited_daily_new_positions": False,
    "max_total_exposure_fraction": 0.35,

    "trade_scan_interval": 20,
    "position_poll_interval": 30,
    "balance_poll_interval": 60,
    "resolution_check_interval": 300,
    "whale_scan_interval": 120,
    "momentum_scan_interval": 90,
    "market_refresh_interval": 300,
    "db_cleanup_interval": 3600,

    "max_signal_age_sec": 120,

    "start_bankroll_usd": 0.0,
    "stop_loss_on_day": -50.0,
    "stop_loss_on_day_pct": 0.05,
    "take_profit_on_day": 0.0,

    "trading_hours_enabled": False,
    "trading_hours_start": "00:00",
    "trading_hours_end": "23:59",
    "trading_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "trading_timezone_offset_min": 0,

    "min_whale_usd": 2500.0,
    "min_entry_price_frac": 0.50,

    "crypto15m_time_delay_min": 8.0,
    "crypto15m_entry_threshold": 0.70,
    "crypto15m_strict_threshold": True,
    "crypto15m_entry_max": 0.98,
    "crypto15m_exit_threshold": 0.40,
    "crypto15m_stop_slippage_cents": 0,
    "crypto15m_take_profit_cents": 0,
    "crypto15m_stop_loss_pct": 0.0,
    "crypto15m_session_take_profit_usd": 0.0,
    "crypto15m_hf_record": False,
    "crypto15m_hf_interval_ms": 200,
    "crypto15m_min_delta_pct": 0.0,
    "crypto15m_min_rsi": 0.0,
    "crypto15m_min_macd_hist": 0.0,
    "crypto15m_entry_diff": 0.02,
    "crypto15m_entry_style": "maker",
    "crypto15m_maker_cancel_min": 1.0,
    "crypto15m_hours_start_utc": 0,
    "crypto15m_hours_end_utc": 24,
    "crypto15m_hours": None,
    "crypto15m_enabled": False,
    "crypto15m_live": False,
    "crypto15m_sizing_mode": "fixed",
    "crypto15m_order_size": 1,
    "crypto15m_balance_pct": 0.02,
    "crypto15m_max_loss_pct": 0.0,
    "crypto15m_max_total_pct": 0.10,
    "crypto15m_max_concurrent": 3,
    "crypto15m_assets": None,
    "crypto15m_poll_sec": 4,
    "crypto15m_strategy_mode": "directional",
    "crypto15m_direction_mode": "favorite",
    "crypto15m_fast_ema_period": 12,
    "crypto15m_slow_sma_period": 20,
    "crypto15m_trend_sma_period": 50,
    "crypto15m_trend_timeframe_min": 5,
    "crypto15m_min_entry_cents": 5,
    "crypto15m_max_entry_cents": 59,
    "crypto15m_min_entry_seconds_left": 120,
    "crypto15m_time_stop_cents": 10,
    "crypto15m_time_stop_seconds_left": 60,
    "crypto15m_force_exit_seconds_left": 5,
    "crypto15m_model_min_prob": 0.97,
    "crypto15m_model_min_edge_cents": 2.0,
    "crypto15m_model_final_minute": True,
    "crypto15m_model_autopause": True,
    "crypto15m_record_signals": True,
    "main_record_signals": True,

    "perps_record_signals": True,
    "perps_symbols": ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP", "KXDOGEPERP"],
    "perps_ws_enabled": True,
    "perps_rest_poll_sec": 30,
    "perps_funding_est_sec": 60,
    "perps_funding_poll_min": 60,
    "perps_candle_topup_min": 10,
    "perps_backfill_days": 14,
    "perps_farm_enabled": False,
    "perps_farm_symbol": "KXBTCPERP",
    "perps_farm_clip_contracts": 1,
    "perps_farm_max_inventory_contracts": 3,
    "perps_farm_min_spread_ticks": 2,
    "perps_farm_requote_ticks": 1,
    "perps_farm_daily_loss_usd": 2.0,
    "perps_farm_daily_volume_usd": 0.0,
    "perps_farm_max_cost_bps": 4.0,
    "perps_farm_max_fee_bps": 0.0,

    "perps_strat_enabled": False,
    "perps_strat_live": False,
    "perps_strat_symbol": "KXBTCPERP",
    "perps_strat_direction": "long",
    "perps_strat_rules": [],
    "perps_strat_entry_style": "taker",
    "perps_strat_contracts": 1,
    "perps_strat_leverage": 1.0,
    "perps_strat_tp_bps": 30.0,
    "perps_strat_sl_bps": 20.0,
    "perps_strat_max_hold_min": 60.0,
    "perps_strat_exit_on_rules_fail": False,
    "perps_strat_daily_loss_usd": 5.0,
    "perps_strat_max_notional_usd": 100.0,
    "perps_strat_fee_era": "jul8",
    "crypto15m_indicator_detect": True,
    "crypto15m_spot_ws": True,
    "crypto15m_arb_detect": True,
    "crypto15m_arb_min_edge_cents": 3.0,
    "crypto15m_use_rules": False,
    "crypto15m_rules": [],

    "crypto15m_pairs_enabled": False,
    "crypto15m_directional_enabled": True,
    "crypto15m_pairs_ceiling_cents": 95.0,
    "crypto15m_pairs_dip_cents": 2.0,
    "crypto15m_pairs_clip": 5,
    "crypto15m_pairs_first_leg_min_cents": 35.0,
    "crypto15m_pairs_first_leg_max_cents": 60.0,

    "event_webhook_url": "",
    "stats_webhook_url": "",
    "whale_webhook_url": "",
    "momentum_webhook_url": "",
    "stats_push_interval": 3600,
    "stats_chart_window_hours": 168,
    "enable_discord": True,
}



STRATEGY_PRESETS: list[dict[str, Any]] = [
    {
        "id": "krypt-balanced",
        "name": "Krypt Balanced",
        "tagline": "Whales + momentum, both gates active.",
        "description": (
            "Our default everyday strategy. Trades both whale signals and "
            "trade-cluster momentum signals with edge ≥ 5pts and "
            "confidence ≥ 55%. 2-6% sizing, $50 hard cap. Best fit for "
            "most users — let it run a few weeks and check the stats."
        ),
        "riskLabel": "balanced",
        "badge": "recommended",
        "config": {},
    },
    {
        "id": "krypt-conservative",
        "name": "Krypt Conservative",
        "tagline": "Tight sizing, high-edge only, capital-preservation mode.",
        "description": (
            "Only trades signals with edge ≥ 6pts net of fees and confidence "
            "≥ 65%. Smaller sizing (1-3% of bankroll), $25 hard cap. Daily "
            "stop-loss at -$25. Designed to ride out variance with "
            "minimum drawdown. (Gate is 6, not 8: edge is fee-adjusted and "
            "capped at 10/8 by the scorer — an 8pt net gate would sit above "
            "what momentum can ever score.)"
        ),
        "riskLabel": "safe",
        "config": {
            "min_edge_pts_whale": 6.0,
            "min_edge_pts_momentum": 6.0,
            "min_confidence_whale": 65.0,
            "min_confidence_momentum": 65.0,
            "base_size_fraction": 0.015,
            "min_size_fraction": 0.01,
            "max_size_fraction": 0.03,
            "hard_max_position_usd": 25.0,
            "max_open_positions": 10,
            "max_daily_new_positions": 15,
            "stop_loss_on_day": -25.0,
            "max_total_exposure_fraction": 0.50,
        },
    },
    {
        "id": "krypt-aggressive",
        "name": "Krypt Aggressive",
        "tagline": "More signals, larger sizing, higher variance.",
        "description": (
            "Loosens edge gates to 3pts and confidence to 50%. Sizing "
            "scales 4-10% of bankroll, $100 cap. Higher max-open count. "
            "Use only with a bankroll you can stand to drop 30% on a "
            "bad day."
        ),
        "riskLabel": "aggressive",
        "config": {
            "min_edge_pts_whale": 3.0,
            "min_edge_pts_momentum": 3.0,
            "min_confidence_whale": 50.0,
            "min_confidence_momentum": 50.0,
            "base_size_fraction": 0.06,
            "min_size_fraction": 0.04,
            "max_size_fraction": 0.10,
            "hard_max_position_usd": 100.0,
            "max_open_positions": 40,
            "max_daily_new_positions": 80,
            "max_total_exposure_fraction": 0.85,
            "stop_loss_on_day": -100.0,
        },
    },
    {
        "id": "krypt-whale-only",
        "name": "Whale Hunter",
        "tagline": "Follows large taker orders. No momentum signals.",
        "description": (
            "Pure whale-following. Disables momentum entirely and only "
            "trades when a $2.5k+ taker order hits a market with a "
            "scored edge ≥ 5pts. Best when you trust 'smart money' "
            "patterns more than crowd contrarian setups."
        ),
        "riskLabel": "balanced",
        "config": {
            "trade_whales": True,
            "trade_momentum": False,
            "min_edge_pts_whale": 5.0,
            "min_confidence_whale": 55.0,
        },
    },
    {
        "id": "krypt-momentum-only",
        "name": "Crowd Contrarian",
        "tagline": "Mean-reversion on trade clusters. No whale signals.",
        "description": (
            "Only fades clusters of trades against the underdog. "
            "Empirically the highest-edge zone in the data: NO clusters "
            "when YES is heavy favourite, YES clusters when YES is deep "
            "underdog. Disables whale-following entirely."
        ),
        "riskLabel": "balanced",
        "config": {
            "trade_whales": False,
            "trade_momentum": True,
            "contrarian_only": True,
            "min_edge_pts_momentum": 7.0,
            "min_confidence_momentum": 50.0,
            "allowed_momentum_signal_types": ["trade_cluster"],
        },
    },
    {
        "id": "krypt-edge-hunter",
        "name": "Edge Hunter",
        "tagline": "Top-decile edge only. Few but high-quality trades.",
        "description": (
            "Only fires on the highest-scored signals (edge ≥ 7pts whale / "
            "6pts momentum — the scorer caps edge at 10/8, so these gates sit "
            "just under the ceiling; the old 12pt gate was above it and could "
            "NEVER trade). Sizes more aggressively on high-edge picks (4-8% "
            "scaled). Expect long quiet periods between trades."
        ),
        "riskLabel": "balanced",
        "config": {
            "min_edge_pts_whale": 7.0,
            "min_edge_pts_momentum": 6.0,
            "min_confidence_whale": 60.0,
            "min_confidence_momentum": 55.0,
            "base_size_fraction": 0.04,
            "min_size_fraction": 0.04,
            "max_size_fraction": 0.08,
            "sizing_base_edge": 7.0,
            "sizing_max_edge": 10.0,
            "max_open_positions": 15,
        },
    },
    {
        "id": "krypt-crypto-whale",
        "name": "Crypto Whale",
        "tagline": "Whale-following, crypto markets only.",
        "description": (
            "Most reliable edge (highest t-stat, 97% win). Whale signals in "
            "CRYPTO backtested strongly positive net-of-fee while sports "
            "whales lost. Entry cap raised to 98c because crypto whales follow "
            "high-price favorites (the old 85c cap threw away most of the "
            "edge). Edge gate lowered to 2pts: the scorer caps confidence at "
            "97, so above ~92c the max computable edge shrinks toward zero — "
            "the old 5pt gate silently re-capped entries at 92c, contradicting "
            "the 98c cap this preset advertises. In-sample +9.3c/contract "
            "(t=3.5, n=36). EXPERIMENTAL / in-sample on a small sample — "
            "paper-trade to confirm."
        ),
        "riskLabel": "experimental",
        "badge": "new",
        "config": {
            "trade_whales": True,
            "trade_momentum": False,
            "allowed_categories": ["crypto"],
            "min_confidence_whale": 55.0,
            "min_edge_pts_whale": 2.0,
            "min_entry_price_cents": 15,
            "max_entry_price_cents": 98,
        },
    },
    {
        "id": "krypt-sports-momentum",
        "name": "Sports Momentum",
        "tagline": "Contrarian trade-clusters, sports only.",
        "description": (
            "Highest raw edge, but noisier (single category, smaller sample). "
            "Contrarian momentum in SPORTS backtested strongly positive "
            "net-of-fee while news/world momentum lost. Fixed: confidence >= 40 "
            "(the old 50 gate cut the edge to noise — momentum scores top out "
            "near 60) and a wider 15-70c band. In-sample +18.2c/contract "
            "(t=2.2, n=37). EXPERIMENTAL / in-sample — paper-trade to confirm."
        ),
        "riskLabel": "experimental",
        "badge": "new",
        "config": {
            "trade_whales": False,
            "trade_momentum": True,
            "contrarian_only": True,
            "allowed_categories": ["sports"],
            "allowed_momentum_signal_types": ["trade_cluster"],
            "min_confidence_momentum": 40.0,
            "min_entry_price_cents": 15,
            "max_entry_price_cents": 70,
        },
    },
    {
        "id": "krypt-edge",
        "name": "Edge Stack",
        "tagline": "Both backtested edges at once — crypto whales + sports momentum.",
        "description": (
            "Our recommended pick — the best risk-adjusted edge. Runs the two "
            "signal sources that backtested net-POSITIVE after fees, each "
            "restricted to where it has an edge — whales in CRYPTO / EXOTICS / "
            "ENTERTAINMENT and contrarian momentum in SPORTS (confidence >= 40) "
            "— with an 85c cap that drops the loss-making high-price favorites. "
            "Sports Momentum has a higher raw edge, but this diversifies across "
            "two independent sources, so it's the most reliable. In-sample "
            "+15.6c/contract (t=3.2, n=81) vs the unfiltered default's "
            "net-NEGATIVE edge. EXPERIMENTAL / in-sample — test on Demo "
            "first to confirm it holds forward."
        ),
        "riskLabel": "experimental",
        "badge": "recommended",
        "config": {
            "trade_whales": True,
            "trade_momentum": True,
            "contrarian_only": True,
            "allowed_categories": None,
            "allowed_whale_categories": ["crypto", "exotics", "entertainment"],
            "allowed_momentum_categories": ["sports"],
            "allowed_momentum_signal_types": ["trade_cluster"],
            "min_confidence_whale": 55.0,
            "min_edge_pts_whale": 5.0,
            "min_confidence_momentum": 40.0,
            "min_entry_price_cents": 15,
            "max_entry_price_cents": 85,
        },
    },
    {
        "id": "krypt-experimental",
        "name": "Convergence Hunter",
        "tagline": "Trades only when 3+ whales agree on the same side.",
        "description": (
            "Experimental. Only trades when convergence is detected — "
            "3+ whales taking the same side of the same market within 2 "
            "hours. Rare but high-conviction setups. (Edge scales with the "
            "pack: 3 whales score +4, 4 score +6, 5+ score +8, so the 4pt "
            "gate needs at least a 3-whale group net of fees.)"
        ),
        "riskLabel": "experimental",
        "badge": "new",
        "config": {
            "trade_whales": False,
            "trade_momentum": False,
            "trade_convergence": True,
            "min_edge_pts_whale": 4.0,
            "min_confidence_whale": 60.0,
            "max_open_positions": 12,
        },
    },
]


CRYPTO15M_PRESETS: list[dict[str, Any]] = [
    {
        "id": "c15-favorite",
        "name": "Deep Favorite",
        "tagline": "Only the deepest favorites: buy at 95-98c.",
        "description": (
            "Buy the favorite only when it is already >=95c — the single "
            "price band that did not lose money in this app's replay of "
            "531 settled 15-minute markets (+2.8c/contract net of fees, "
            "16/16 wins). CAUTION: that sample is far too small to prove "
            "an edge; one loss at 97c wipes out ~35 wins. Paper-trade "
            "first."
        ),
        "config": {
            "crypto15m_direction_mode": "favorite",
            "crypto15m_entry_threshold": 0.95,
            "crypto15m_entry_max": 0.98,
            "crypto15m_min_delta_pct": 0.0,
            "crypto15m_exit_threshold": 0.40,
            "crypto15m_entry_style": "maker",
            "crypto15m_use_rules": False,
        },
    },
    {
        "id": "c15-contrarian",
        "name": "Contrarian Fade",
        "tagline": "Fade extreme favorites — buy the cheap underdog.",
        "description": (
            "When a side is an extreme favorite (>=90c), buy the CHEAP "
            "opposite side, betting the 15-minute move reverts before "
            "close. Low win rate, high payoff (longshot); holds to "
            "settlement, no stop. Measured roughly break-even (+1.0c/"
            "contract, t=0.3, n=64) on the recorded data — no proven "
            "edge. Paper-trade hard."
        ),
        "config": {
            "crypto15m_direction_mode": "contrarian",
            "crypto15m_entry_threshold": 0.90,
            "crypto15m_entry_max": 0.98,
            "crypto15m_min_delta_pct": 0.0,
            "crypto15m_exit_threshold": 0.0,
            "crypto15m_entry_style": "maker",
            "crypto15m_use_rules": False,
        },
    },
    {
        "id": "c15-momentum",
        "name": "Momentum (Δ-confirmed)",
        "tagline": "Buy the favorite only after the underlying has already moved.",
        "description": (
            "Enter the favorite only once the underlying has moved >=0.2% from "
            "the 15-minute open — a momentum filter. Unproven; paper-trade first."
        ),
        "config": {
            "crypto15m_direction_mode": "favorite",
            "crypto15m_entry_threshold": 0.80,
            "crypto15m_entry_max": 0.98,
            "crypto15m_min_delta_pct": 0.002,
            "crypto15m_exit_threshold": 0.40,
            "crypto15m_entry_style": "maker",
            "crypto15m_use_rules": False,
        },
    },
    {
        "id": "c15-fav-90-95",
        "name": "Favorite 90-95c",
        "tagline": "Favorites in the 90-95c pocket.",
        "description": (
            "Buy favorites priced 90-95c. Priced off the mid and unconfirmed on "
            "real fills near close — paper-trade first."
        ),
        "config": {
            "crypto15m_direction_mode": "favorite",
            "crypto15m_entry_threshold": 0.90,
            "crypto15m_entry_max": 0.95,
            "crypto15m_min_delta_pct": 0.0,
            "crypto15m_exit_threshold": 0.40,
            "crypto15m_entry_style": "maker",
            "crypto15m_use_rules": False,
        },
    },
    {
        "id": "c15-btc-ma-crossover",
        "name": "BTC MA Crossover",
        "tagline": "BTC-only EMA12/SMA20/SMA50 trend branches.",
        "description": (
            "Exact BTC 15-minute strategy: bullish branch buys YES when "
            "EMA12(1m) > SMA20(1m) and spot > SMA50(5m); bearish branch buys "
            "NO on the mirror condition. Entries are 5-59c, five contracts, "
            "one open position, no new entries in the last two minutes, a "
            "10c final-minute time stop, and a forced sell five seconds "
            "before settlement. Paper-trade first."
        ),
        "config": {
            "crypto15m_strategy_mode": "btc_ma_crossover",
            "crypto15m_assets": ["BTC"],
            "crypto15m_order_size": 5,
            "crypto15m_max_concurrent": 1,
            "crypto15m_poll_sec": 10,
            "crypto15m_time_delay_min": 15.0,
            "crypto15m_direction_mode": "favorite",
            "crypto15m_directional_enabled": True,
            "crypto15m_pairs_enabled": False,
            "crypto15m_use_rules": False,
            "crypto15m_entry_threshold": 0.05,
            "crypto15m_strict_threshold": False,
            "crypto15m_entry_max": 0.59,
            "crypto15m_min_delta_pct": 0.0,
            "crypto15m_max_loss_pct": 0.05,
            "crypto15m_max_total_pct": 0.05,
            "crypto15m_session_take_profit_usd": 0.0,
            "crypto15m_indicator_detect": True,
            "crypto15m_spot_ws": True,
            "crypto15m_entry_style": "maker",
            "crypto15m_maker_cancel_min": 2,
            "crypto15m_entry_diff": 0.0,
            "crypto15m_exit_threshold": 0.0,
            "crypto15m_take_profit_cents": 0,
            "crypto15m_stop_loss_pct": 0.0,
            "crypto15m_fast_ema_period": 12,
            "crypto15m_slow_sma_period": 20,
            "crypto15m_trend_sma_period": 50,
            "crypto15m_trend_timeframe_min": 5,
            "crypto15m_min_entry_cents": 5,
            "crypto15m_max_entry_cents": 59,
            "crypto15m_min_entry_seconds_left": 120,
            "crypto15m_time_stop_cents": 10,
            "crypto15m_time_stop_seconds_left": 60,
            "crypto15m_force_exit_seconds_left": 5,
        },
    },
    {
        "id": "c15-macd-trend",
        "name": "MACD Trend (rules)",
        "tagline": "Enter Up when Up is favored and the underlying MACD is bullish.",
        "description": (
            "Experimental rule-builder preset: enter Up when Up is favored "
            "(>=55%) and the 1-minute underlying MACD histogram is positive, in "
            "the last 6 minutes. Uses the new MACD field — recorded, not yet "
            "backtested. Paper-trade first."
        ),
        "config": {
            "crypto15m_direction_mode": "favorite",
            "crypto15m_entry_style": "maker",
            "crypto15m_exit_threshold": 0.40,
            "crypto15m_min_delta_pct": 0.0,
            "crypto15m_indicator_detect": True,
            "crypto15m_use_rules": True,
            "crypto15m_rules": [
                {"field": "upProb", "op": ">=", "value": 0.55},
                {"field": "macdHist", "op": ">", "value": 0.0},
                {"field": "minsLeft", "op": "<=", "value": 6.0},
            ],
        },
    },
]


def crypto15m_preset_config(preset_id: str) -> dict[str, Any] | None:
    for p in CRYPTO15M_PRESETS:
        if p["id"] == preset_id:
            return dict(p["config"])
    return None


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch.lower() if ch.isupper() else ch)
    return "".join(out)


def _clampf(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:
        return default
    return max(lo, min(hi, f))


def _clampi(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


_FRACTION_KEYS = [
    "base_size_fraction", "min_size_fraction", "max_size_fraction",
    "min_cash_reserve_fraction", "max_total_exposure_fraction",
]
_UNIT_KEYS = [
    "crypto15m_entry_threshold", "crypto15m_entry_max", "crypto15m_exit_threshold",
    "crypto15m_min_delta_pct", "crypto15m_entry_diff", "min_entry_price_frac",
    "gambling_trade_probability",
]

_CRYPTO15M_RULE_FIELDS = [
    "favoritePrice", "entryCost", "upProb", "downProb", "deltaPct",
    "deltaSignedPct", "minsLeft", "hourUtc", "peersAgree", "marketBias",
    "arbEdgeCents", "macd", "macdSignal", "macdHist", "macdCross", "rsi",
    "ema12_1m", "sma20_1m", "sma50_5m",
    "settlePrints", "upAsk", "downAsk",
    "sigma1m", "modelProb", "edgeNetCents",
]


def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    d = DEFAULT_CONFIG
    for k in _FRACTION_KEYS:
        cfg[k] = _clampf(cfg.get(k), 0.0, 1.0, d[k])
    for k in _UNIT_KEYS:
        cfg[k] = _clampf(cfg.get(k), 0.0, 1.0, d[k])
    if cfg["min_size_fraction"] > cfg["max_size_fraction"]:
        cfg["min_size_fraction"] = cfg["max_size_fraction"]

    if cfg.get("sizing_mode") not in ("percent", "fixed"):
        cfg["sizing_mode"] = d["sizing_mode"]
    cfg["fixed_trade_usd"] = _clampf(cfg.get("fixed_trade_usd"), 0.0, 1e9, d["fixed_trade_usd"])
    cfg["hard_max_position_usd"] = _clampf(cfg.get("hard_max_position_usd"), 0.0, 1e9, d["hard_max_position_usd"])
    cfg["min_entry_price_cents"] = _clampi(cfg.get("min_entry_price_cents"), 1, 99, d["min_entry_price_cents"])
    cfg["max_entry_price_cents"] = _clampi(cfg.get("max_entry_price_cents"), 1, 99, d["max_entry_price_cents"])
    if cfg["min_entry_price_cents"] > cfg["max_entry_price_cents"]:
        cfg["min_entry_price_cents"], cfg["max_entry_price_cents"] = (
            cfg["max_entry_price_cents"], cfg["min_entry_price_cents"],
        )
    cfg["max_open_positions"] = _clampi(cfg.get("max_open_positions"), 0, 100_000, d["max_open_positions"])
    cfg["max_resolution_days"] = _clampi(cfg.get("max_resolution_days"), 0, 100_000, d["max_resolution_days"])
    cfg["max_daily_new_positions"] = _clampi(cfg.get("max_daily_new_positions"), 0, 100_000, d["max_daily_new_positions"])
    cfg["max_positions_per_event"] = _clampi(cfg.get("max_positions_per_event"), 1, 100_000, d["max_positions_per_event"])
    cfg["stop_loss_on_day"] = -abs(_clampf(cfg.get("stop_loss_on_day"), -1e9, 1e9, d["stop_loss_on_day"]))
    cfg["stop_loss_on_day_pct"] = abs(_clampf(cfg.get("stop_loss_on_day_pct"), -1.0, 1.0, d["stop_loss_on_day_pct"]))
    cfg["take_profit_on_day"] = _clampf(cfg.get("take_profit_on_day"), 0.0, 1e9, d["take_profit_on_day"])
    cfg["fee_aware_edge"] = bool(cfg.get("fee_aware_edge", d["fee_aware_edge"]))
    cfg["max_entry_slippage_cents"] = _clampi(cfg.get("max_entry_slippage_cents"), 0, 99, d["max_entry_slippage_cents"])
    cfg["min_market_volume"] = _clampf(cfg.get("min_market_volume"), 0.0, 1e12, d["min_market_volume"])
    cfg["max_trade_age_min"] = _clampi(cfg.get("max_trade_age_min"), 1, 1440, d["max_trade_age_min"])
    cfg["gambling_mode"] = bool(cfg.get("gambling_mode", False))
    cfg["crypto15m_live"] = bool(cfg.get("crypto15m_live", False))
    cfg["crypto15m_order_size"] = _clampi(cfg.get("crypto15m_order_size"), 1, 10_000, d["crypto15m_order_size"])
    cfg["crypto15m_max_concurrent"] = _clampi(cfg.get("crypto15m_max_concurrent"), 1, 50, d["crypto15m_max_concurrent"])
    aw = cfg.get("crypto15m_assets")
    if isinstance(aw, list):
        try:
            from crypto15m import ALL_ASSETS as _C15_ALL
            valid = {a.upper() for a in _C15_ALL}
        except Exception:
            valid = {"BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"}
        cfg["crypto15m_assets"] = [
            a.upper() for a in aw if isinstance(a, str) and a.upper() in valid
        ]
    elif aw is not None:
        cfg["crypto15m_assets"] = None
    if cfg.get("crypto15m_sizing_mode") not in ("fixed", "balance_pct"):
        cfg["crypto15m_sizing_mode"] = d["crypto15m_sizing_mode"]
    cfg["crypto15m_balance_pct"] = _clampf(cfg.get("crypto15m_balance_pct"), 0.0, 1.0, d["crypto15m_balance_pct"])
    cfg["crypto15m_max_loss_pct"] = _clampf(cfg.get("crypto15m_max_loss_pct"), 0.0, 1.0, d["crypto15m_max_loss_pct"])
    cfg["crypto15m_max_total_pct"] = _clampf(cfg.get("crypto15m_max_total_pct"), 0.0, 1.0, d["crypto15m_max_total_pct"])
    cfg["crypto15m_time_delay_min"] = _clampf(cfg.get("crypto15m_time_delay_min"), 0.0, 15.0, d["crypto15m_time_delay_min"])
    if cfg.get("crypto15m_strategy_mode") not in ("directional", "btc_ma_crossover"):
        cfg["crypto15m_strategy_mode"] = d["crypto15m_strategy_mode"]
    if cfg.get("crypto15m_direction_mode") not in ("favorite", "contrarian", "model"):
        cfg["crypto15m_direction_mode"] = d["crypto15m_direction_mode"]
    cfg["crypto15m_fast_ema_period"] = _clampi(cfg.get("crypto15m_fast_ema_period"), 1, 200, d["crypto15m_fast_ema_period"])
    cfg["crypto15m_slow_sma_period"] = _clampi(cfg.get("crypto15m_slow_sma_period"), 1, 200, d["crypto15m_slow_sma_period"])
    cfg["crypto15m_trend_sma_period"] = _clampi(cfg.get("crypto15m_trend_sma_period"), 1, 300, d["crypto15m_trend_sma_period"])
    cfg["crypto15m_trend_timeframe_min"] = _clampi(cfg.get("crypto15m_trend_timeframe_min"), 1, 15, d["crypto15m_trend_timeframe_min"])
    if cfg["crypto15m_trend_timeframe_min"] not in (1, 5, 15):
        cfg["crypto15m_trend_timeframe_min"] = d["crypto15m_trend_timeframe_min"]
    cfg["crypto15m_min_entry_cents"] = _clampi(cfg.get("crypto15m_min_entry_cents"), 1, 99, d["crypto15m_min_entry_cents"])
    cfg["crypto15m_max_entry_cents"] = _clampi(cfg.get("crypto15m_max_entry_cents"), 1, 99, d["crypto15m_max_entry_cents"])
    if cfg["crypto15m_min_entry_cents"] > cfg["crypto15m_max_entry_cents"]:
        cfg["crypto15m_min_entry_cents"], cfg["crypto15m_max_entry_cents"] = (
            cfg["crypto15m_max_entry_cents"], cfg["crypto15m_min_entry_cents"],
        )
    cfg["crypto15m_min_entry_seconds_left"] = _clampi(cfg.get("crypto15m_min_entry_seconds_left"), 0, 900, d["crypto15m_min_entry_seconds_left"])
    cfg["crypto15m_time_stop_cents"] = _clampi(cfg.get("crypto15m_time_stop_cents"), 1, 99, d["crypto15m_time_stop_cents"])
    cfg["crypto15m_time_stop_seconds_left"] = _clampi(cfg.get("crypto15m_time_stop_seconds_left"), 1, 300, d["crypto15m_time_stop_seconds_left"])
    cfg["crypto15m_force_exit_seconds_left"] = _clampi(cfg.get("crypto15m_force_exit_seconds_left"), 1, 60, d["crypto15m_force_exit_seconds_left"])
    if cfg["crypto15m_strategy_mode"] == "btc_ma_crossover":
        cfg["crypto15m_indicator_detect"] = True
    cfg["crypto15m_model_min_prob"] = _clampf(cfg.get("crypto15m_model_min_prob"), 0.50, 1.0, d["crypto15m_model_min_prob"])
    cfg["crypto15m_model_min_edge_cents"] = _clampf(cfg.get("crypto15m_model_min_edge_cents"), 0.0, 50.0, d["crypto15m_model_min_edge_cents"])
    cfg["crypto15m_model_final_minute"] = bool(cfg.get("crypto15m_model_final_minute", d["crypto15m_model_final_minute"]))
    cfg["crypto15m_model_autopause"] = bool(cfg.get("crypto15m_model_autopause", d["crypto15m_model_autopause"]))
    cfg["main_record_signals"] = bool(cfg.get("main_record_signals", d["main_record_signals"]))
    if cfg.get("crypto15m_entry_style") not in ("maker", "taker"):
        cfg["crypto15m_entry_style"] = d["crypto15m_entry_style"]
    cfg["crypto15m_maker_cancel_min"] = _clampf(cfg.get("crypto15m_maker_cancel_min"), 0.0, 15.0, d["crypto15m_maker_cancel_min"])
    cfg["crypto15m_stop_slippage_cents"] = _clampi(cfg.get("crypto15m_stop_slippage_cents"), 0, 50, d["crypto15m_stop_slippage_cents"])
    cfg["crypto15m_take_profit_cents"] = _clampi(cfg.get("crypto15m_take_profit_cents"), 0, 99, d["crypto15m_take_profit_cents"])
    cfg["crypto15m_stop_loss_pct"] = _clampf(cfg.get("crypto15m_stop_loss_pct"), 0.0, 1.0, d["crypto15m_stop_loss_pct"])
    cfg["crypto15m_session_take_profit_usd"] = _clampf(cfg.get("crypto15m_session_take_profit_usd"), 0.0, 1e9, d["crypto15m_session_take_profit_usd"])
    cfg["crypto15m_hf_record"] = bool(cfg.get("crypto15m_hf_record", d["crypto15m_hf_record"]))
    cfg["crypto15m_hf_interval_ms"] = _clampi(cfg.get("crypto15m_hf_interval_ms"), 50, 5000, d["crypto15m_hf_interval_ms"])
    cfg["crypto15m_min_rsi"] = _clampf(cfg.get("crypto15m_min_rsi"), 0.0, 100.0, d["crypto15m_min_rsi"])
    cfg["crypto15m_min_macd_hist"] = _clampf(cfg.get("crypto15m_min_macd_hist"), 0.0, 1e9, d["crypto15m_min_macd_hist"])
    cfg["crypto15m_hours_start_utc"] = _clampi(cfg.get("crypto15m_hours_start_utc"), 0, 24, d["crypto15m_hours_start_utc"])
    cfg["crypto15m_hours_end_utc"] = _clampi(cfg.get("crypto15m_hours_end_utc"), 0, 24, d["crypto15m_hours_end_utc"])
    hh = cfg.get("crypto15m_hours")
    if isinstance(hh, list):
        cfg["crypto15m_hours"] = sorted({
            int(h) for h in hh if isinstance(h, (int, float)) and 0 <= int(h) <= 23
        })
    elif hh is not None:
        cfg["crypto15m_hours"] = None
    cfg["crypto15m_indicator_detect"] = bool(cfg.get("crypto15m_indicator_detect", True))
    cfg["crypto15m_spot_ws"] = bool(cfg.get("crypto15m_spot_ws", d["crypto15m_spot_ws"]))
    cfg["crypto15m_strict_threshold"] = bool(cfg.get("crypto15m_strict_threshold", d["crypto15m_strict_threshold"]))
    cfg["crypto15m_arb_detect"] = bool(cfg.get("crypto15m_arb_detect", True))
    cfg["crypto15m_arb_min_edge_cents"] = _clampf(cfg.get("crypto15m_arb_min_edge_cents"), 0.0, 100.0, d["crypto15m_arb_min_edge_cents"])
    cfg["crypto15m_use_rules"] = bool(cfg.get("crypto15m_use_rules", False))
    cfg["crypto15m_rules"] = rules.sanitize_rules(cfg.get("crypto15m_rules"), _CRYPTO15M_RULE_FIELDS)
    cfg["crypto15m_pairs_enabled"] = False
    cfg["crypto15m_directional_enabled"] = True
    cfg["crypto15m_pairs_ceiling_cents"] = _clampf(cfg.get("crypto15m_pairs_ceiling_cents"), 50.0, 99.0, d["crypto15m_pairs_ceiling_cents"])
    cfg["crypto15m_pairs_dip_cents"] = _clampf(cfg.get("crypto15m_pairs_dip_cents"), 0.5, 30.0, d["crypto15m_pairs_dip_cents"])
    cfg["crypto15m_pairs_clip"] = _clampi(cfg.get("crypto15m_pairs_clip"), 1, 1000, d["crypto15m_pairs_clip"])
    cfg["crypto15m_pairs_first_leg_min_cents"] = _clampf(cfg.get("crypto15m_pairs_first_leg_min_cents"), 1.0, 90.0, d["crypto15m_pairs_first_leg_min_cents"])
    cfg["crypto15m_pairs_first_leg_max_cents"] = _clampf(cfg.get("crypto15m_pairs_first_leg_max_cents"), 5.0, 95.0, d["crypto15m_pairs_first_leg_max_cents"])
    if cfg["crypto15m_pairs_first_leg_min_cents"] > cfg["crypto15m_pairs_first_leg_max_cents"]:
        cfg["crypto15m_pairs_first_leg_min_cents"], cfg["crypto15m_pairs_first_leg_max_cents"] = (
            cfg["crypto15m_pairs_first_leg_max_cents"], cfg["crypto15m_pairs_first_leg_min_cents"],
        )
    cfg["perps_record_signals"] = bool(cfg.get("perps_record_signals", d["perps_record_signals"]))
    cfg["perps_ws_enabled"] = bool(cfg.get("perps_ws_enabled", d["perps_ws_enabled"]))
    ps = cfg.get("perps_symbols")
    if isinstance(ps, list):
        cleaned = []
        for s in ps:
            if not isinstance(s, str):
                continue
            s = s.strip().upper().rstrip("1")
            if s.startswith("KX") and s.endswith("PERP"):
                cleaned.append(s)
        cfg["perps_symbols"] = list(dict.fromkeys(cleaned))[:16] or list(d["perps_symbols"])
    else:
        cfg["perps_symbols"] = list(d["perps_symbols"])
    cfg["perps_farm_enabled"] = bool(cfg.get("perps_farm_enabled", d["perps_farm_enabled"]))
    sym = str(cfg.get("perps_farm_symbol") or d["perps_farm_symbol"]).strip().upper().rstrip("1")
    cfg["perps_farm_symbol"] = sym if (sym.startswith("KX") and sym.endswith("PERP")) else d["perps_farm_symbol"]
    cfg["perps_farm_clip_contracts"] = _clampi(cfg.get("perps_farm_clip_contracts"), 1, 100, d["perps_farm_clip_contracts"])
    cfg["perps_farm_max_inventory_contracts"] = _clampi(cfg.get("perps_farm_max_inventory_contracts"), 1, 1000, d["perps_farm_max_inventory_contracts"])
    cfg["perps_farm_min_spread_ticks"] = _clampi(cfg.get("perps_farm_min_spread_ticks"), 1, 100, d["perps_farm_min_spread_ticks"])
    cfg["perps_farm_requote_ticks"] = _clampi(cfg.get("perps_farm_requote_ticks"), 1, 100, d["perps_farm_requote_ticks"])
    cfg["perps_farm_daily_loss_usd"] = _clampf(cfg.get("perps_farm_daily_loss_usd"), 0.1, 10000.0, d["perps_farm_daily_loss_usd"])
    cfg["perps_farm_daily_volume_usd"] = _clampf(cfg.get("perps_farm_daily_volume_usd"), 0.0, 1e9, d["perps_farm_daily_volume_usd"])
    cfg["perps_farm_max_cost_bps"] = _clampf(cfg.get("perps_farm_max_cost_bps"), 0.1, 100.0, d["perps_farm_max_cost_bps"])
    cfg["perps_farm_max_fee_bps"] = _clampf(cfg.get("perps_farm_max_fee_bps"), 0.0, 100.0, d["perps_farm_max_fee_bps"])
    cfg["perps_strat_enabled"] = bool(cfg.get("perps_strat_enabled", d["perps_strat_enabled"]))
    cfg["perps_strat_live"] = bool(cfg.get("perps_strat_live", d["perps_strat_live"]))
    ssym = str(cfg.get("perps_strat_symbol") or d["perps_strat_symbol"]).strip().upper().rstrip("1")
    cfg["perps_strat_symbol"] = ssym if (ssym.startswith("KX") and ssym.endswith("PERP")) else d["perps_strat_symbol"]
    if cfg.get("perps_strat_direction") not in ("long", "short"):
        cfg["perps_strat_direction"] = d["perps_strat_direction"]
    if cfg.get("perps_strat_entry_style") not in ("taker", "maker"):
        cfg["perps_strat_entry_style"] = d["perps_strat_entry_style"]
    if cfg.get("perps_strat_fee_era") not in ("today", "jul8"):
        cfg["perps_strat_fee_era"] = d["perps_strat_fee_era"]
    try:
        from perps_strategy import PERPS_RULE_FIELDS as _PERPS_FIELDS
    except Exception:
        _PERPS_FIELDS = []
    cfg["perps_strat_rules"] = rules.sanitize_rules(cfg.get("perps_strat_rules"), _PERPS_FIELDS)
    cfg["perps_strat_contracts"] = _clampi(cfg.get("perps_strat_contracts"), 1, 500, d["perps_strat_contracts"])
    cfg["perps_strat_leverage"] = _clampf(cfg.get("perps_strat_leverage"), 1.0, 5.0, d["perps_strat_leverage"])
    cfg["perps_strat_tp_bps"] = _clampf(cfg.get("perps_strat_tp_bps"), 0.0, 5000.0, d["perps_strat_tp_bps"])
    cfg["perps_strat_sl_bps"] = _clampf(cfg.get("perps_strat_sl_bps"), 0.0, 5000.0, d["perps_strat_sl_bps"])
    cfg["perps_strat_max_hold_min"] = _clampf(cfg.get("perps_strat_max_hold_min"), 0.0, 10080.0, d["perps_strat_max_hold_min"])
    cfg["perps_strat_exit_on_rules_fail"] = bool(cfg.get("perps_strat_exit_on_rules_fail", d["perps_strat_exit_on_rules_fail"]))
    cfg["perps_strat_daily_loss_usd"] = _clampf(cfg.get("perps_strat_daily_loss_usd"), 0.5, 10000.0, d["perps_strat_daily_loss_usd"])
    cfg["perps_strat_max_notional_usd"] = _clampf(cfg.get("perps_strat_max_notional_usd"), 5.0, 100000.0, d["perps_strat_max_notional_usd"])
    cfg["perps_rest_poll_sec"] = _clampi(cfg.get("perps_rest_poll_sec"), 10, 600, d["perps_rest_poll_sec"])
    cfg["perps_funding_est_sec"] = _clampi(cfg.get("perps_funding_est_sec"), 30, 3600, d["perps_funding_est_sec"])
    cfg["perps_funding_poll_min"] = _clampi(cfg.get("perps_funding_poll_min"), 15, 1440, d["perps_funding_poll_min"])
    cfg["perps_candle_topup_min"] = _clampi(cfg.get("perps_candle_topup_min"), 5, 120, d["perps_candle_topup_min"])
    cfg["perps_backfill_days"] = _clampi(cfg.get("perps_backfill_days"), 1, 90, d["perps_backfill_days"])
    cfg["trade_scan_interval"] = _clampi(cfg.get("trade_scan_interval"), 5, 3600, d["trade_scan_interval"])
    cfg["position_poll_interval"] = _clampi(cfg.get("position_poll_interval"), 5, 3600, d["position_poll_interval"])
    cfg["balance_poll_interval"] = _clampi(cfg.get("balance_poll_interval"), 10, 3600, d["balance_poll_interval"])
    cfg["resolution_check_interval"] = _clampi(cfg.get("resolution_check_interval"), 30, 86400, d["resolution_check_interval"])
    cfg["whale_scan_interval"] = _clampi(cfg.get("whale_scan_interval"), 30, 3600, d["whale_scan_interval"])
    cfg["momentum_scan_interval"] = _clampi(cfg.get("momentum_scan_interval"), 30, 3600, d["momentum_scan_interval"])
    cfg["market_refresh_interval"] = _clampi(cfg.get("market_refresh_interval"), 30, 86400, d["market_refresh_interval"])
    cfg["crypto15m_poll_sec"] = _clampi(cfg.get("crypto15m_poll_sec"), 2, 60, d["crypto15m_poll_sec"])
    _oe = cfg.get("order_expiration_sec")
    if _oe in (None, 0, "0"):
        cfg["order_expiration_sec"] = None
    else:
        cfg["order_expiration_sec"] = _clampi(_oe, 10, 3600, d["order_expiration_sec"])
    return cfg


def merge_with_defaults(user: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    for k, v in (user or {}).items():
        if k in out:
            out[k] = v
            continue
        sk = _camel_to_snake(k)
        out[sk] = v
    return _validate_config(out)


def strategy_full_config(strategy_id: str) -> dict[str, Any] | None:
    for s in STRATEGY_PRESETS:
        if s["id"] == strategy_id:
            return merge_with_defaults(s["config"])
    return None
