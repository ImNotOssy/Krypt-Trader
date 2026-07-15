from __future__ import annotations

from config import (
    CRYPTO15M_PRESETS, STRATEGY_PRESETS, crypto15m_preset_config,
    merge_with_defaults,
)


def _by_id(presets):
    return {p["id"]: p for p in presets}




def test_crypto_whale_preset_is_crypto_only_whales():
    p = _by_id(STRATEGY_PRESETS)["krypt-crypto-whale"]
    cfg = merge_with_defaults(p["config"])
    assert cfg["trade_whales"] is True
    assert cfg["trade_momentum"] is False
    assert cfg["allowed_categories"] == ["crypto"]
    assert cfg["max_entry_price_cents"] == 98


def test_sports_momentum_preset_is_sports_only_momentum():
    p = _by_id(STRATEGY_PRESETS)["krypt-sports-momentum"]
    cfg = merge_with_defaults(p["config"])
    assert cfg["trade_momentum"] is True
    assert cfg["trade_whales"] is False
    assert cfg["allowed_categories"] == ["sports"]
    assert cfg["contrarian_only"] is True
    assert cfg["min_confidence_momentum"] == 40.0
    assert cfg["max_entry_price_cents"] == 70


def test_edge_stack_preset_runs_both_sources_per_category():
    p = _by_id(STRATEGY_PRESETS)["krypt-edge"]
    cfg = merge_with_defaults(p["config"])
    assert cfg["trade_whales"] is True
    assert cfg["trade_momentum"] is True
    assert cfg["allowed_categories"] is None
    assert cfg["allowed_whale_categories"] == ["crypto", "exotics", "entertainment"]
    assert cfg["allowed_momentum_categories"] == ["sports"]
    assert cfg["max_entry_price_cents"] == 85




def test_crypto15m_presets_exist():
    ids = {p["id"] for p in CRYPTO15M_PRESETS}
    assert {"c15-favorite", "c15-contrarian", "c15-btc-ma-crossover"} <= ids


def test_contrarian_preset_sets_contrarian_mode():
    cfg = crypto15m_preset_config("c15-contrarian")
    assert cfg is not None
    assert cfg["crypto15m_direction_mode"] == "contrarian"
    assert cfg["crypto15m_exit_threshold"] == 0.0


def test_deep_favorite_preset_uses_95c_band():
    cfg = crypto15m_preset_config("c15-favorite")
    assert cfg["crypto15m_entry_threshold"] == 0.95
    assert cfg["crypto15m_entry_max"] == 0.98


def test_btc_ma_crossover_preset_matches_importable_profile_shape():
    cfg = merge_with_defaults(crypto15m_preset_config("c15-btc-ma-crossover"))
    assert cfg["crypto15m_strategy_mode"] == "btc_ma_crossover"
    assert cfg["crypto15m_assets"] == ["BTC"]
    assert cfg["crypto15m_order_size"] == 5
    assert cfg["crypto15m_max_concurrent"] == 1
    assert cfg["crypto15m_poll_sec"] == 10
    assert cfg["crypto15m_time_delay_min"] == 15
    assert cfg["crypto15m_direction_mode"] == "favorite"
    assert cfg["crypto15m_entry_threshold"] == 0.05
    assert cfg["crypto15m_strict_threshold"] is False
    assert cfg["crypto15m_entry_max"] == 0.59
    assert cfg["crypto15m_min_delta_pct"] == 0
    assert cfg["crypto15m_max_loss_pct"] == 0.05
    assert cfg["crypto15m_max_total_pct"] == 0.05
    assert cfg["crypto15m_session_take_profit_usd"] == 0
    assert cfg["crypto15m_maker_cancel_min"] == 2
    assert cfg["crypto15m_min_entry_cents"] == 5
    assert cfg["crypto15m_max_entry_cents"] == 59
    assert cfg["crypto15m_time_stop_cents"] == 10
    assert cfg["crypto15m_time_stop_seconds_left"] == 60
    assert cfg["crypto15m_force_exit_seconds_left"] == 5


def test_btc_ma_crossover_import_keys_are_clamped():
    cfg = merge_with_defaults({
        "crypto15mStrategyMode": "btc_ma_crossover",
        "crypto15mFastEmaPeriod": 0,
        "crypto15mSlowSmaPeriod": 500,
        "crypto15mTrendSmaPeriod": 0,
        "crypto15mTrendTimeframeMin": 7,
        "crypto15mMinEntryCents": 80,
        "crypto15mMaxEntryCents": 3,
        "crypto15mTimeStopCents": 500,
        "crypto15mTimeStopSecondsLeft": 0,
        "crypto15mForceExitSecondsLeft": 90,
    })
    assert cfg["crypto15m_strategy_mode"] == "btc_ma_crossover"
    assert cfg["crypto15m_fast_ema_period"] == 1
    assert cfg["crypto15m_slow_sma_period"] == 200
    assert cfg["crypto15m_trend_sma_period"] == 1
    assert cfg["crypto15m_trend_timeframe_min"] == 5
    assert cfg["crypto15m_min_entry_cents"] == 3
    assert cfg["crypto15m_max_entry_cents"] == 80
    assert cfg["crypto15m_time_stop_cents"] == 99
    assert cfg["crypto15m_time_stop_seconds_left"] == 1
    assert cfg["crypto15m_force_exit_seconds_left"] == 60


def test_unknown_preset_is_none():
    assert crypto15m_preset_config("nope") is None
