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
    # Subtract the Kalshi taker fee (~0.07·p·(1−p), in cents/contract) from a
    # signal's edge BEFORE comparing it to the min_edge gates, so "edge" means
    # net-of-fee edge. Off = legacy gross-edge gating (near 50c the fee alone
    # is ~1.75c — a "5pt" gross edge is really ~3.3).
    "fee_aware_edge": True,
    # Reject an entry when the live order book has moved more than this many
    # cents past the signal's price (limit_cross pays whatever the book asks —
    # on a thin book that silently eats the whole edge). 0 = off.
    "max_entry_slippage_cents": 5,
    # Skip signals on markets with less lifetime volume than this many contracts
    # (thin books = wide spreads + adverse fills). 0 = off. Unknown volume is
    # fail-open.
    "min_market_volume": 100.0,
    # Only count tape trades younger than this many minutes for whale signals
    # and momentum clusters. Without it, a restart "discovers" hours-old whales
    # at prices that no longer exist, and 5 trades spread over 6 quiet hours
    # count as a "cluster".
    "max_trade_age_min": 15,
    "min_entry_price_cents": 15,
    "max_entry_price_cents": 85,
    # Skip any market whose resolution (close time) is more than this many days
    # out — e.g. 30 means don't take bets that won't resolve for a month+ (long-
    # dated politics markets tie up capital for months). 0 = off (no time limit).
    # Default 30: with 0, a default user's 25 open slots silently fill with
    # multi-month markets and the bot "stops trading" with capital frozen.
    "max_resolution_days": 30,
    "allowed_momentum_signal_types": ["trade_cluster"],
    "allowed_categories": None,
    "allowed_whale_categories": None,
    "allowed_momentum_categories": None,
    "contrarian_only": True,

    # "Secret Strategy" — pure-gambling mode. Ignores every gate and gives each
    # fresh signal a flat random chance to trade. For fun only.
    "gambling_mode": False,
    "gambling_trade_probability": 0.10,

    # "percent" = edge-scaled % of balance (below). "fixed" = a flat dollar
    # amount per trade (fixed_trade_usd), ignoring the fractions.
    "sizing_mode": "percent",
    "fixed_trade_usd": 5.0,

    "base_size_fraction": 0.03,
    "min_size_fraction": 0.02,
    "max_size_fraction": 0.06,
    "sizing_base_edge": 5.0,
    # Edge at which sizing reaches max_size_fraction. The scorer caps edge at 10
    # (whale) / 8 (momentum), so anything above 10 leaves edge-scaled sizing
    # permanently stuck near the minimum (the old default 20 was a dead knob).
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
    # 0.35, not 0.75: committing 3/4 of the bankroll by default made the
    # main engine a full-account drawdown machine for small accounts.
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
    # Daily stop-loss as a FRACTION of the day-start account total (0..1;
    # 0.05 = halt new entries once down 5% on the day). Works alongside the
    # flat-dollar stop above — whichever limit is TIGHTER binds. This is what
    # protects a $100 account (where a flat -$50 is half the bankroll) and a
    # $5000 one (where -$50 is noise) with one default. 0 = off.
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
    # Strict threshold (hard floor): require the price actually PAID (the
    # favorite side's executable ask / the maker bid, floored) to be at or above
    # entry_threshold, and require a two-sided book. Off = legacy behaviour: the
    # threshold checks only the mid-derived favorite probability, so thin books
    # can fill below it (the "entered at 71c with an 85c threshold" complaint).
    "crypto15m_strict_threshold": True,
    "crypto15m_entry_max": 0.98,
    "crypto15m_exit_threshold": 0.40,
    # Cents BELOW the bid to price a stop-loss SELL so it sweeps depth and fills
    # in a fast drop instead of resting at the top of a falling book. 0 = at bid.
    "crypto15m_stop_slippage_cents": 0,
    # Per-bet take-profit: sell a winning position once the held side reaches this
    # price (cents). 0 = off / hold to settlement. Set it ABOVE the entry price or
    # it sells at a loss the moment a position fills.
    "crypto15m_take_profit_cents": 0,
    # Per-bet stop-loss as a FRACTION of the entry cost (stored 0..1; the UI shows
    # it as a %). Sell once a position is down >= this share of what it cost. 0 =
    # off. Works alongside the cents/price stop (crypto15m_exit_threshold) —
    # whichever triggers first exits.
    "crypto15m_stop_loss_pct": 0.0,
    # Session take-profit: once the 15m executor's realized P&L since the backend
    # started reaches $this, stop opening NEW 15m entries (open positions keep
    # being managed). 0 = off. Resets when the app restarts.
    "crypto15m_session_take_profit_usd": 0.0,
    "crypto15m_min_delta_pct": 0.0,
    # Direction-aware momentum confirmation layered ON TOP of the built-in
    # favorite gate (not the rule builder). The underlying MACD/RSI must agree
    # with the side being bought: an up-bet needs rsi>=min_rsi & macdHist>=min;
    # a down-bet needs the mirror (rsi<=100-min_rsi & macdHist<=-min). Each 0 =
    # off. Lets users widen the entry window yet only enter on strong momentum.
    "crypto15m_min_rsi": 0.0,
    "crypto15m_min_macd_hist": 0.0,
    "crypto15m_entry_diff": 0.02,
    "crypto15m_entry_style": "maker",
    "crypto15m_maker_cancel_min": 1.0,
    "crypto15m_hours_start_utc": 0,
    "crypto15m_hours_end_utc": 24,
    "crypto15m_enabled": False,
    "crypto15m_live": False,
    "crypto15m_sizing_mode": "fixed",
    "crypto15m_order_size": 1,
    "crypto15m_balance_pct": 0.02,
    "crypto15m_max_loss_pct": 0.0,
    # Aggregate 15m risk cap: total committed 15m cost may not exceed this
    # fraction of the bankroll (0..1; 0 = off). The 7 assets' 15m windows are
    # essentially ONE correlated bet on crypto direction — without this cap the
    # only thing between a user and "70% of bankroll on one BTC candle" is the
    # per-bet size. Applied at entry: order size is trimmed to fit the budget.
    "crypto15m_max_total_pct": 0.10,
    # Down from 7: all seven assets move together, so 7 concurrent favorites is
    # one levered position, not diversification.
    "crypto15m_max_concurrent": 3,
    # Which assets the executor may open NEW positions on. None = all enabled;
    # a list of symbols restricts to those (set from the 15m tab's Trade toggles).
    "crypto15m_assets": None,
    "crypto15m_poll_sec": 4,
    # "favorite" buys the market's favorite; "contrarian" fades it; "model"
    # is the SETTLEMENT SNIPER: buy the side the settlement model (CF index
    # vs strike, scaled by realized vol) calls near-certain while the quote
    # still lags — gated by the two crypto15m_model_* knobs below.
    "crypto15m_direction_mode": "favorite",
    # Sniper gates: minimum model probability for the bought side, and minimum
    # fee-adjusted edge (model prob − executable ask − taker fee, in cents).
    # Backtest on recorded ticks (132 markets): gate 0.97 within 5 min won
    # 97.7% at avg 93c → ~+3.9c/contract net of fees.
    "crypto15m_model_min_prob": 0.97,
    "crypto15m_model_min_edge_cents": 2.0,
    # Final-minute sniping (model mode ONLY): inside the last 60s the
    # settlement average is being realized print-by-print, so with >=30 of 60
    # prints in and the model >=3-sigma certain, stale quotes are the largest
    # measured edge in our recorded data (+22.8c/contract, 27/27 wins at the
    # edge>=2c gate). Every other mode keeps the hard final-minute block.
    "crypto15m_model_final_minute": True,
    # Auto-pause model-mode entries when rolling calibration (realized hit
    # rate of >=97%-confident predictions) drops below break-even. The edge
    # lives and dies on calibration holding; this is the alarm.
    "crypto15m_model_autopause": True,
    "crypto15m_record_signals": True,
    # Record whale/momentum signals while the app runs. Forced ON while
    # enable_trading is set — the engine cannot follow signals it never sees.
    "main_record_signals": True,

    # ── Kalshi perpetual futures: passive data recorder (no trading) ──
    # Streams the margin WS ticker/trade channels + polls the public REST API
    # (always PRODUCTION — market data is unauthenticated) into the perp_*
    # tables. Feeds the perps research program (sniper filter, consistency
    # arb, lead-lag); collection must run for ~2 weeks before those are
    # testable, which is why this exists ahead of any perps strategy. ON by
    # default like the other recorders — disk is capped by the 30-day tick
    # retention (~1GB worst case at 24/7 uptime), and the bot cannot research
    # what it never recorded.
    "perps_record_signals": True,
    "perps_symbols": ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP", "KXDOGEPERP"],
    "perps_ws_enabled": True,       # WS accelerator; REST poll is the baseline either way
    "perps_rest_poll_sec": 30,      # public markets snapshot cadence
    "perps_funding_est_sec": 60,    # live funding-estimate poll (series exists nowhere else)
    "perps_funding_poll_min": 60,   # finalized-rate top-up cadence
    "perps_candle_topup_min": 10,   # 1m candle top-up / gap repair cadence
    "perps_backfill_days": 14,      # deep candle backfill window on first enable
    # ── Perps volume farmer: maker-only two-sided quoting for the in-app
    # volume rewards ($25/$50 trade; ~2-4 bps of notional at the volume
    # tiers). A volume engine with a loss budget, not a profit strategy —
    # auto-halts for the day when measured cost/volume exceeds max_cost_bps
    # (i.e. farming costs more than the bonus pays) or the loss cap. ──
    "perps_farm_enabled": False,
    "perps_farm_symbol": "KXBTCPERP",
    "perps_farm_clip_contracts": 1,          # contracts per resting quote
    "perps_farm_max_inventory_contracts": 3,  # beyond ±this, quote reduce-side only
    "perps_farm_min_spread_ticks": 2,        # stand down when book tighter than this
    "perps_farm_requote_ticks": 1,           # re-join when touch drifts > this
    "perps_farm_daily_loss_usd": 2.0,        # hard net-loss halt per UTC day
    "perps_farm_daily_volume_usd": 0.0,      # stop after this much volume (0 = off)
    "perps_farm_max_cost_bps": 4.0,          # halt when measured cost/volume$ exceeds

    # ── Perps user strategy (rule-composed, like the 15m rule builder).
    # perps_strat_enabled = paper trading on live quotes (simulated fills,
    # dry_run rows). perps_strat_live additionally places REAL leveraged
    # orders — gated in the UI behind an explicit risk acknowledgement.
    # Same gates run in backtest/paper/live (replay parity). ──
    "perps_strat_enabled": False,
    "perps_strat_live": False,
    "perps_strat_symbol": "KXBTCPERP",
    "perps_strat_direction": "long",         # long | short
    "perps_strat_rules": [],                 # {field, op, value} ANDed (perps vocabulary)
    "perps_strat_entry_style": "taker",      # taker | maker (backtest models both; live is taker IOC)
    "perps_strat_contracts": 1,
    "perps_strat_leverage": 1.0,             # informs margin/liquidation sim + notional cap
    "perps_strat_tp_bps": 30.0,              # take-profit, bps of entry (0 = off)
    "perps_strat_sl_bps": 20.0,              # stop-loss, bps of entry (0 = off)
    "perps_strat_max_hold_min": 60.0,        # time exit (0 = off)
    "perps_strat_exit_on_rules_fail": False,
    "perps_strat_daily_loss_usd": 5.0,       # halt for the UTC day
    "perps_strat_max_notional_usd": 100.0,   # per-position notional cap
    "perps_strat_fee_era": "jul8",           # backtest fee scenario: today | jul8
    # Underlying MACD/RSI on 1-min closes (detection-only rule fields).
    "crypto15m_indicator_detect": True,
    # Coinbase WebSocket spot feed (BRTI-constituent proxy — the number Kalshi
    # actually settles against). Live prices for the listed majors + the
    # final-minute settlement-average tracker that sharpens modelProb. Off =
    # REST spot chain only. Env override: KRYPT_SPOT_WS=0.
    "crypto15m_spot_ws": True,
    # Up+Down ≠ $1 arbitrage flag (market-neutral edge, detection-only).
    # Min edge 3c: both legs pay a taker fee (~2-4c round trip at mid prices),
    # so the old 1c default flagged "arbs" that lost money after fees.
    "crypto15m_arb_detect": True,
    "crypto15m_arb_min_edge_cents": 3.0,
    # Custom entry rule builder: when on, the user's {field,op,value} conditions
    # REPLACE the built-in favorite/signal gate for the 15m executor.
    "crypto15m_use_rules": False,
    "crypto15m_rules": [],

    # Pairs — temporal complement accumulation: buy YES on its dips and NO on
    # its peaks at different times in the window; every matched pair settles at
    # exactly $1, so a blended cost below the ceiling is LOCKED profit,
    # direction-agnostic. Runs alongside the directional strategy but never on
    # the same window. No stop-loss/TP by design (selling a leg un-locks the
    # margin). Off by default.
    "crypto15m_pairs_enabled": False,
    # The directional (favorite/contrarian) engine. Off = pairs-only mode: the
    # 15m tab still monitors and manages open positions, but the favorite
    # strategy opens nothing.
    "crypto15m_directional_enabled": True,
    # Max blended YES+NO cost per pair, in cents. 95 locks ≥5c gross per pair;
    # two taker fees eat ~2-4c of that, so ~1-3c net per matched pair.
    "crypto15m_pairs_ceiling_cents": 95.0,
    # A leg only buys on a DIP: its ask this many cents below its own rolling
    # median (last ~3 min of ticks).
    "crypto15m_pairs_dip_cents": 2.0,
    # Contracts per leg (the second leg always matches what the first filled).
    "crypto15m_pairs_clip": 5,
    # FIRST-leg price band. Pairs only work where the two sides genuinely
    # seesaw — near coin-flip. Below the floor the market has a strong favorite
    # and a cheap "dip" is usually the losing side trending to zero (a knife-
    # catch, not an oscillation); above the cap the complement can't fit under
    # the ceiling.
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

# Snapshot fields a crypto15m entry rule may gate on (the rule-builder vocabulary).
# Anything not in this set is dropped by sanitize_rules so the running executor
# only ever sees rules over fields it actually computes.
_CRYPTO15M_RULE_FIELDS = [
    "favoritePrice", "entryCost", "upProb", "downProb", "deltaPct",
    "deltaSignedPct", "minsLeft", "hourUtc", "peersAgree", "marketBias",
    "arbEdgeCents", "macd", "macdSignal", "macdHist", "macdCross", "rsi",
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
    # Sign-normalize instead of clamping to 0: a user typing "+50" means "stop at
    # a $50 loss" — the old clamp silently turned it into 0 = OFF while the UI
    # kept displaying 50, i.e. a safety rail that looked set but wasn't.
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
    # crypto15m_assets: None = all enabled; a list restricts to valid symbols
    # (uppercased, unknowns dropped). Any non-list/non-None falls back to all.
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
    if cfg.get("crypto15m_direction_mode") not in ("favorite", "contrarian", "model"):
        cfg["crypto15m_direction_mode"] = d["crypto15m_direction_mode"]
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
    cfg["crypto15m_min_rsi"] = _clampf(cfg.get("crypto15m_min_rsi"), 0.0, 100.0, d["crypto15m_min_rsi"])
    cfg["crypto15m_min_macd_hist"] = _clampf(cfg.get("crypto15m_min_macd_hist"), 0.0, 1e9, d["crypto15m_min_macd_hist"])
    cfg["crypto15m_hours_start_utc"] = _clampi(cfg.get("crypto15m_hours_start_utc"), 0, 24, d["crypto15m_hours_start_utc"])
    cfg["crypto15m_hours_end_utc"] = _clampi(cfg.get("crypto15m_hours_end_utc"), 0, 24, d["crypto15m_hours_end_utc"])
    cfg["crypto15m_indicator_detect"] = bool(cfg.get("crypto15m_indicator_detect", True))
    cfg["crypto15m_spot_ws"] = bool(cfg.get("crypto15m_spot_ws", d["crypto15m_spot_ws"]))
    cfg["crypto15m_strict_threshold"] = bool(cfg.get("crypto15m_strict_threshold", d["crypto15m_strict_threshold"]))
    cfg["crypto15m_arb_detect"] = bool(cfg.get("crypto15m_arb_detect", True))
    cfg["crypto15m_arb_min_edge_cents"] = _clampf(cfg.get("crypto15m_arb_min_edge_cents"), 0.0, 100.0, d["crypto15m_arb_min_edge_cents"])
    cfg["crypto15m_use_rules"] = bool(cfg.get("crypto15m_use_rules", False))
    cfg["crypto15m_rules"] = rules.sanitize_rules(cfg.get("crypto15m_rules"), _CRYPTO15M_RULE_FIELDS)
    # Pairs is HARD-DISABLED: live testing settled it at −$11.69 — on Kalshi's
    # single complementary book "buying the other side" is just selling the
    # first (a taker-taker scalp in disguise), and stranded first legs lose
    # nearly always. Engine code retained for research; no config can enable it.
    cfg["crypto15m_pairs_enabled"] = False
    # With pairs gone, "directional off" would mean the 15m tab does nothing —
    # the master 15m toggle is the way to turn it off.
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
    # Perps recorder — same rate-limit-floor rationale as the intervals below.
    cfg["perps_record_signals"] = bool(cfg.get("perps_record_signals", d["perps_record_signals"]))
    cfg["perps_ws_enabled"] = bool(cfg.get("perps_ws_enabled", d["perps_ws_enabled"]))
    ps = cfg.get("perps_symbols")
    if isinstance(ps, list):
        cleaned = []
        for s in ps:
            if not isinstance(s, str):
                continue
            s = s.strip().upper().rstrip("1")  # config always stores prod symbols
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
    # Leverage hard-capped at 5x regardless of what the venue allows — this is
    # a retail research bot, not a liquidation speedrun.
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
    # Floor the scan/poll intervals so a user can't drive them toward ~1s and get
    # rate-limited / banned by Kalshi (the UI had no minimum).
    cfg["trade_scan_interval"] = _clampi(cfg.get("trade_scan_interval"), 5, 3600, d["trade_scan_interval"])
    cfg["position_poll_interval"] = _clampi(cfg.get("position_poll_interval"), 5, 3600, d["position_poll_interval"])
    cfg["balance_poll_interval"] = _clampi(cfg.get("balance_poll_interval"), 10, 3600, d["balance_poll_interval"])
    cfg["resolution_check_interval"] = _clampi(cfg.get("resolution_check_interval"), 30, 86400, d["resolution_check_interval"])
    cfg["whale_scan_interval"] = _clampi(cfg.get("whale_scan_interval"), 30, 3600, d["whale_scan_interval"])
    cfg["momentum_scan_interval"] = _clampi(cfg.get("momentum_scan_interval"), 30, 3600, d["momentum_scan_interval"])
    cfg["market_refresh_interval"] = _clampi(cfg.get("market_refresh_interval"), 30, 86400, d["market_refresh_interval"])
    cfg["crypto15m_poll_sec"] = _clampi(cfg.get("crypto15m_poll_sec"), 2, 60, d["crypto15m_poll_sec"])
    # 0/None = never expire (the UI's "0 = never" affordance was previously a
    # lie: None fell through the clamp to 90s and orders silently canceled).
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
