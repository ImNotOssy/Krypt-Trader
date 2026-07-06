from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    base = os.environ.get("KRYPT_TRADER_USERDATA")
    if base:
        d = Path(base) / "data"
    else:
        d = Path(__file__).resolve().parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return _data_dir() / "krypt-trader.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(str(db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is durable-enough under WAL (a power cut can lose the last txn but
    # never corrupts) and skips the per-commit fsync FULL forces — the trading
    # loop opens/commits dozens of these per tick on the event-loop thread,
    # each fsync stalling every coroutine 5-30ms.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()




SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT DEFAULT '',
    series_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    yes_sub_title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    close_time TEXT DEFAULT '',
    volume REAL DEFAULT 0,
    volume_24h REAL DEFAULT 0,
    open_interest REAL DEFAULT 0,
    yes_bid REAL DEFAULT 0,
    yes_ask REAL DEFAULT 0,
    last_price REAL DEFAULT 0,
    prev_yes_bid REAL DEFAULT 0,
    prev_price REAL DEFAULT 0,
    result TEXT DEFAULT '',
    settlement_value REAL DEFAULT NULL,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_ticker TEXT PRIMARY KEY,
    series_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    sub_title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    count_fp REAL DEFAULT 0,
    yes_price REAL DEFAULT 0,
    no_price REAL DEFAULT 0,
    taker_side TEXT DEFAULT '',
    dollar_value REAL DEFAULT 0,
    category TEXT DEFAULT '',
    created_time TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    signal_type TEXT DEFAULT '',
    direction TEXT DEFAULT '',
    volume_24h REAL DEFAULT 0,
    price REAL DEFAULT 0,
    price_change REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    discord_sent INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    resolved_price REAL DEFAULT NULL,
    pnl_estimate REAL DEFAULT NULL,
    resolved_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS whale_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    yes_sub_title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    taker_side TEXT DEFAULT '',
    count_fp REAL DEFAULT 0,
    price REAL DEFAULT 0,
    dollar_value REAL DEFAULT 0,
    market_volume REAL DEFAULT 0,
    open_interest REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    discord_sent INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    resolved_price REAL DEFAULT NULL,
    pnl_estimate REAL DEFAULT NULL,
    resolved_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    volume REAL DEFAULT 0,
    volume_24h REAL DEFAULT 0,
    open_interest REAL DEFAULT 0,
    yes_bid REAL DEFAULT 0,
    last_price REAL DEFAULT 0,
    snapshot_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bot_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_source TEXT NOT NULL,
    signal_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    direction TEXT NOT NULL,
    action TEXT DEFAULT 'buy',
    target_contracts INTEGER NOT NULL,
    limit_price_cents INTEGER NOT NULL,
    filled_contracts INTEGER DEFAULT 0,
    avg_fill_price_cents REAL DEFAULT NULL,
    cost_usd REAL DEFAULT 0,
    fees_usd REAL DEFAULT 0,
    client_order_id TEXT UNIQUE NOT NULL,
    kalshi_order_id TEXT DEFAULT NULL,
    status TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    edge_pts REAL DEFAULT 0,
    signal_price REAL DEFAULT 0,
    error TEXT DEFAULT NULL,
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    settlement_usd REAL DEFAULT NULL,
    pnl_usd REAL DEFAULT NULL,
    mark_price_cents REAL DEFAULT NULL,
    closed_early INTEGER DEFAULT 0,
    balance_before_usd REAL DEFAULT NULL,
    kalshi_env TEXT DEFAULT 'demo',
    created_at TEXT DEFAULT (datetime('now')),
    last_updated TEXT DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT NULL,
    UNIQUE(signal_source, signal_id, kalshi_env)
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    kalshi_status TEXT DEFAULT NULL,
    filled_contracts INTEGER DEFAULT NULL,
    fill_cost_cents INTEGER DEFAULT NULL,
    note TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT NOT NULL,
    kalshi_env TEXT NOT NULL,
    opened INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    realized_pnl_usd REAL DEFAULT 0,
    ending_balance_usd REAL DEFAULT 0,
    last_updated TEXT DEFAULT (datetime('now')),
    PRIMARY KEY(day, kalshi_env)
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT DEFAULT (datetime('now')),
    kalshi_env TEXT DEFAULT 'demo',
    cash_usd REAL NOT NULL,
    portfolio_usd REAL NOT NULL,
    total_usd REAL NOT NULL,
    realized_pnl_usd REAL DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    open_positions INTEGER DEFAULT 0
);

-- risk_state: tiny per-(env, kind) persistence for the daily-risk gate.
-- The 180s breach-persistence window used to live only in a module-global
-- dict, so a backend restart mid-breach re-enabled both engines until the
-- timer re-elapsed — restarting the app is exactly what a user does after a
-- losing streak. breach_started_at is unix seconds (wall clock); NULL/absent
-- means no active breach.
CREATE TABLE IF NOT EXISTS risk_state (
    kalshi_env TEXT NOT NULL,
    kind TEXT NOT NULL,
    breach_started_at REAL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (kalshi_env, kind)
);

CREATE TABLE IF NOT EXISTS crypto15m_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    series TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,                 -- 'up' | 'down' (display)
    direction TEXT NOT NULL,            -- 'yes' | 'no' (Kalshi side bought)
    target_contracts INTEGER NOT NULL,
    filled_contracts INTEGER DEFAULT 0,
    entry_limit_cents INTEGER NOT NULL,
    avg_entry_cents REAL DEFAULT NULL,
    cost_usd REAL DEFAULT 0,
    fees_usd REAL DEFAULT 0,            -- Kalshi trading fees on the entry order
    client_order_id TEXT UNIQUE NOT NULL,
    kalshi_order_id TEXT DEFAULT NULL,
    -- stop-loss exit leg
    exit_client_order_id TEXT DEFAULT NULL,
    exit_kalshi_order_id TEXT DEFAULT NULL,
    exit_limit_cents INTEGER DEFAULT NULL,
    exit_filled_contracts INTEGER DEFAULT 0,
    proceeds_usd REAL DEFAULT NULL,
    exit_fees_usd REAL DEFAULT 0,       -- Kalshi trading fees on the exit order
    -- lifecycle
    status TEXT NOT NULL,               -- dry_run|submitted|filled|exiting|exited|settled|canceled|error
    exit_reason TEXT DEFAULT NULL,      -- 'stop_loss' | 'settlement' | 'unfilled_expired'
    close_time TEXT DEFAULT '',
    confidence REAL DEFAULT 0,          -- favorite prob at entry (×100)
    entry_delta_usd REAL DEFAULT NULL,
    outcome_correct INTEGER DEFAULT NULL,
    settlement_usd REAL DEFAULT NULL,
    pnl_usd REAL DEFAULT NULL,
    resolved INTEGER DEFAULT 0,
    kalshi_env TEXT DEFAULT 'demo',
    dry_run INTEGER DEFAULT 0,
    error TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    last_updated TEXT DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT NULL
);

-- crypto15m_signals: a passive research log for the 15-min crypto
-- strategy. One row per market (quarter window): a decision-point
-- snapshot captured live (favorite side + price + underlying delta a few
-- minutes before close), then the settled outcome filled in afterward.
-- This is what makes the 15m strategy backtestable — independent of
-- whether the user ever enables the executor. No orders, no money.
CREATE TABLE IF NOT EXISTS crypto15m_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    series TEXT DEFAULT '',
    close_time TEXT DEFAULT '',
    observed_at TEXT DEFAULT (datetime('now')),
    mins_left REAL,                    -- minutes to close at the observation
    favorite TEXT,                     -- 'up' | 'down' at the decision point
    favorite_price REAL,               -- favorite mid probability (fraction)
    entry_cost REAL,                   -- cost to BUY the favorite (fraction)
    up_prob REAL,                      -- yes/up mid probability (fraction)
    delta_pct REAL,                    -- abs(open-live)/open underlying move
    open_spot REAL,
    obs_spot REAL,
    macd REAL,                         -- underlying MACD line (1-min closes)
    macd_signal REAL,                  -- MACD signal line
    macd_hist REAL,                    -- MACD histogram = macd - signal
    macd_cross INTEGER,                -- +1 bullish / -1 bearish / 0 no cross
    rsi REAL,                          -- Wilder RSI(14) of the underlying
    resolved INTEGER DEFAULT 0,
    up_won INTEGER DEFAULT NULL,       -- 1 if the up/yes side settled true
    settled_at TEXT DEFAULT NULL,
    kalshi_env TEXT DEFAULT 'demo'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_c15sig_ticker ON crypto15m_signals(ticker);
CREATE INDEX IF NOT EXISTS idx_c15sig_resolved ON crypto15m_signals(resolved, close_time);

-- crypto15m_ticks: high-frequency companion to crypto15m_signals. One
-- row per active 15-min market every ~25s across the WHOLE window (not
-- just the decision point), so strategies can be tested for entry
-- timing, quote staleness vs spot, and maker-fill behaviour. Outcomes
-- come from joining crypto15m_signals on ticker. Pruned by
-- cleanup_old_data after `_C15_TICKS_KEEP_DAYS`.
CREATE TABLE IF NOT EXISTS crypto15m_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    observed_at TEXT DEFAULT (datetime('now')),
    mins_left REAL,
    yes_bid REAL,                      -- fraction 0..1
    yes_ask REAL,                      -- fraction 0..1
    up_prob REAL,                      -- mid, fraction 0..1
    spot REAL,                         -- live underlying USD
    open_spot REAL,                    -- quarter-open underlying USD
    delta_pct REAL,                    -- abs(open-live)/open
    macd REAL,                         -- underlying MACD line (1-min closes)
    macd_signal REAL,                  -- MACD signal line
    macd_hist REAL,                    -- MACD histogram = macd - signal
    macd_cross INTEGER,                -- +1 bullish / -1 bearish / 0 no cross
    rsi REAL,                          -- Wilder RSI(14) of the underlying
    kalshi_env TEXT DEFAULT 'demo'
);
CREATE INDEX IF NOT EXISTS idx_c15tick_ticker ON crypto15m_ticks(ticker, observed_at);
CREATE INDEX IF NOT EXISTS idx_c15tick_time ON crypto15m_ticks(observed_at);

-- Kalshi perpetual futures (margin API) market data. Prices are INTEGER
-- micro-dollars (1 = $0.000001; perp tick is $0.0001, wire allows 6dp) and
-- counts INTEGER centi-contracts (1 = 0.01 contracts) — exact fixed-point,
-- parsed via Decimal at the wire boundary, never float (this codebase's
-- cents-vs-dollars history earned that rule). Funding rates stay REAL.
-- kalshi_env defaults 'production': the public REST recorder path always
-- reads prod (unauthenticated) regardless of the trading env.

-- perp_ticks: WS ticker-channel stream (1/sec/market coalesced) + REST
-- /margin/markets snapshot rows (src='rest', the baseline when WS is down).
CREATE TABLE IF NOT EXISTS perp_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,                       -- wire ticker (KXBTCPERP / demo KXBTCPERP1)
    observed_at TEXT DEFAULT (datetime('now')),
    ts_ms INTEGER,                              -- wire event time, epoch ms (NULL for REST rows)
    last_usd_micro INTEGER,
    bid_usd_micro INTEGER,
    ask_usd_micro INTEGER,
    bid_size_cc INTEGER,
    ask_size_cc INTEGER,
    volume_24h_cc INTEGER,
    oi_cc INTEGER,
    ref_usd_micro INTEGER,                      -- CF Benchmarks index × contract_size
    ref_ts_ms INTEGER,
    settle_mark_usd_micro INTEGER,
    liq_mark_usd_micro INTEGER,
    funding_rate REAL,                          -- running estimate (decimal per 8h window)
    next_funding_ms INTEGER,
    src TEXT DEFAULT 'ws',                      -- 'ws' | 'rest'
    kalshi_env TEXT DEFAULT 'production'
);
CREATE INDEX IF NOT EXISTS idx_perptick_ticker ON perp_ticks(ticker, observed_at);
CREATE INDEX IF NOT EXISTS idx_perptick_tsms   ON perp_ticks(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_perptick_time   ON perp_ticks(observed_at);

-- perp_trades: public tape (WS trade channel + REST backfill), deduped on trade_id.
CREATE TABLE IF NOT EXISTS perp_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    observed_at TEXT DEFAULT (datetime('now')),
    ts_ms INTEGER,
    price_usd_micro INTEGER NOT NULL,
    count_cc INTEGER NOT NULL,
    taker_side TEXT,                            -- 'bid' | 'ask' (perps have no yes/no)
    kalshi_env TEXT DEFAULT 'production',
    UNIQUE(trade_id, kalshi_env)
);
CREATE INDEX IF NOT EXISTS idx_perptrade_ticker ON perp_trades(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_perptrade_time   ON perp_trades(observed_at);

-- perp_candles: REST candlesticks (period_min ∈ 1|60|1440 — Kalshi has NO 15m).
-- Quote (bid/ask) OHLC is always present; trade OHLC/mean is NULL when no
-- trades printed in the period. Upserted, so top-up/backfill are idempotent.
CREATE TABLE IF NOT EXISTS perp_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_min INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,                    -- wire end_period_ts, unix SECONDS, inclusive
    bid_open_usd_micro INTEGER, bid_high_usd_micro INTEGER,
    bid_low_usd_micro INTEGER,  bid_close_usd_micro INTEGER,
    ask_open_usd_micro INTEGER, ask_high_usd_micro INTEGER,
    ask_low_usd_micro INTEGER,  ask_close_usd_micro INTEGER,
    trade_open_usd_micro INTEGER, trade_high_usd_micro INTEGER,
    trade_low_usd_micro INTEGER,  trade_close_usd_micro INTEGER,
    trade_mean_usd_micro INTEGER,
    volume_cc INTEGER,
    volume_notional_usd_micro INTEGER,
    oi_cc INTEGER,
    kalshi_env TEXT DEFAULT 'production',
    UNIQUE(ticker, period_min, end_ts, kalshi_env)
);
CREATE INDEX IF NOT EXISTS idx_perpcandle_lookup ON perp_candles(ticker, period_min, end_ts);

-- perp_farm_fills: the volume farmer's own fills (accounting source of truth
-- for volume/fees/realized P&L; deduped on trade_id like perp_trades).
CREATE TABLE IF NOT EXISTS perp_farm_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    order_id TEXT DEFAULT '',
    ticker TEXT NOT NULL,
    observed_at TEXT DEFAULT (datetime('now')),
    ts_ms INTEGER,
    side TEXT,                                  -- our order side: 'bid' | 'ask'
    count_cc INTEGER NOT NULL,
    price_usd_micro INTEGER NOT NULL,
    fee_usd_micro INTEGER DEFAULT 0,
    is_taker INTEGER DEFAULT 0,
    realized_pnl_usd_micro INTEGER DEFAULT 0,   -- avg-cost realized on this fill
    inventory_after_cc INTEGER,
    kalshi_env TEXT DEFAULT 'production',
    UNIQUE(trade_id, kalshi_env)
);
CREATE INDEX IF NOT EXISTS idx_perpfarm_time ON perp_farm_fills(observed_at);
CREATE INDEX IF NOT EXISTS idx_perpfarm_ticker ON perp_farm_fills(ticker, observed_at);

-- perp_positions: user-strategy positions (paper AND live — dry_run flag).
-- Prices in INTEGER micro-dollars, counts centi-contracts, like all perp tables.
CREATE TABLE IF NOT EXISTS perp_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,                         -- 'long' | 'short'
    dry_run INTEGER DEFAULT 1,                  -- 1 = paper fill, 0 = real order
    opened_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT,
    count_cc INTEGER NOT NULL,
    entry_usd_micro INTEGER NOT NULL,
    exit_usd_micro INTEGER,
    leverage REAL DEFAULT 1,
    fees_usd_micro INTEGER DEFAULT 0,           -- entry+exit fees
    funding_usd_micro INTEGER DEFAULT 0,        -- funding paid(-)/received(+) while held
    pnl_usd_micro INTEGER,                      -- realized net (px + funding - fees)
    exit_reason TEXT DEFAULT '',                -- tp|sl|max_hold|rules_exit|flatten|liquidated
    entry_reason TEXT DEFAULT '',
    kalshi_env TEXT DEFAULT 'production'
);
CREATE INDEX IF NOT EXISTS idx_perppos_open ON perp_positions(closed_at, ticker);
CREATE INDEX IF NOT EXISTS idx_perppos_time ON perp_positions(opened_at DESC);

-- perp_funding: finalized funding rates (8h windows: 04/12/20 UTC). Tiny; kept forever.
CREATE TABLE IF NOT EXISTS perp_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    funding_time TEXT NOT NULL,                 -- RFC3339 normalized to 'YYYY-MM-DD HH:MM:SS' UTC
    funding_rate REAL NOT NULL,
    mark_usd_micro INTEGER,
    kalshi_env TEXT DEFAULT 'production',
    UNIQUE(ticker, funding_time, kalshi_env)
);
CREATE INDEX IF NOT EXISTS idx_perpfund_lookup ON perp_funding(ticker, funding_time);

-- bot_runs: each row is a single launch of the bot (start → stop).
-- This is what powers the user-facing "session P&L" model — every
-- restart starts a fresh run, and `start_balance` is what we benchmark
-- against. Per-run aggregates (P&L, trades opened/won/lost) get
-- updated periodically and finalised on shutdown.
CREATE TABLE IF NOT EXISTS bot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kalshi_env TEXT NOT NULL DEFAULT 'demo',
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at TEXT,
    start_cash_usd REAL NOT NULL DEFAULT 0,
    start_portfolio_usd REAL NOT NULL DEFAULT 0,
    start_total_usd REAL NOT NULL DEFAULT 0,
    end_cash_usd REAL,
    end_portfolio_usd REAL,
    end_total_usd REAL,
    pnl_usd REAL DEFAULT 0,
    trades_opened INTEGER DEFAULT 0,
    trades_won INTEGER DEFAULT 0,
    trades_lost INTEGER DEFAULT 0,
    -- Lifetime counters captured at run start. The run's per-session
    -- trade/W/L counts are computed as `current_lifetime - start_lifetime`
    -- on every heartbeat. Without this baseline the heartbeat just
    -- stored lifetime totals into every run, which is why every row in
    -- the History → Run history table looked the same.
    start_trades_opened INTEGER DEFAULT 0,
    start_trades_won INTEGER DEFAULT 0,
    start_trades_lost INTEGER DEFAULT 0,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_env ON bot_runs(kalshi_env, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_open ON bot_runs(ended_at, kalshi_env);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(created_time DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker, direction, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_whale_trade_id ON whale_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_whale_cat ON whale_trades(category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_whale_convergence ON whale_trades(ticker, taker_side, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots ON market_snapshots(ticker, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_markets_vol ON markets(volume DESC);
CREATE INDEX IF NOT EXISTS idx_bp_status ON bot_positions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bp_ticker ON bot_positions(ticker, direction, status);
CREATE INDEX IF NOT EXISTS idx_bp_resolved ON bot_positions(resolved, status);
CREATE INDEX IF NOT EXISTS idx_bp_created ON bot_positions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oe_pos ON order_events(position_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pnl_at ON pnl_snapshots(at DESC);
CREATE INDEX IF NOT EXISTS idx_pnl_env_at ON pnl_snapshots(kalshi_env, at);
CREATE INDEX IF NOT EXISTS idx_c15_open ON crypto15m_positions(resolved, status);
CREATE INDEX IF NOT EXISTS idx_c15_asset ON crypto15m_positions(asset, kalshi_env, resolved);
-- the executor tick reads errored/stopped ticker sets every ~4s; without this
-- both are full-table DISTINCT scans that grow with history
CREATE INDEX IF NOT EXISTS idx_c15_env_status ON crypto15m_positions(kalshi_env, status);
CREATE INDEX IF NOT EXISTS idx_c15_env_exit ON crypto15m_positions(kalshi_env, exit_reason);
"""


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
        for migration in [
            "ALTER TABLE bot_positions ADD COLUMN closed_early INTEGER DEFAULT 0",
            "ALTER TABLE bot_positions ADD COLUMN mark_price_cents REAL DEFAULT NULL",
            "ALTER TABLE alerts ADD COLUMN yes_sub_title TEXT DEFAULT ''",
            "ALTER TABLE bot_runs ADD COLUMN start_trades_opened INTEGER DEFAULT 0",
            "ALTER TABLE bot_runs ADD COLUMN start_trades_won INTEGER DEFAULT 0",
            "ALTER TABLE bot_runs ADD COLUMN start_trades_lost INTEGER DEFAULT 0",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd_signal REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd_hist REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd_cross INTEGER",
            "ALTER TABLE crypto15m_signals ADD COLUMN rsi REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd_signal REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd_hist REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd_cross INTEGER",
            "ALTER TABLE crypto15m_ticks ADD COLUMN rsi REAL",
            "ALTER TABLE crypto15m_positions ADD COLUMN fees_usd REAL DEFAULT 0",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_fees_usd REAL DEFAULT 0",
            # spot-vs-strike settlement model (detection-only recorded fields)
            "ALTER TABLE crypto15m_ticks ADD COLUMN strike REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN delta_signed_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN sigma1m REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN model_prob REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN edge_net_cents REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN strike REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN model_prob REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN edge_net_cents REAL",
            # Final-minute settlement context — settle_prints gates the
            # largest validated edge; spot_source says whether model_prob was
            # computed off the exact CF settlement feed or a REST fallback.
            # Without these, final-minute strategies can't be backtested and
            # recorded model_prob can't be stratified by input quality.
            "ALTER TABLE crypto15m_ticks ADD COLUMN settle_prints INTEGER",
            "ALTER TABLE crypto15m_ticks ADD COLUMN no_ask REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN spot_source TEXT",
            # '' = directional (favorite/contrarian); 'pair' = complement-
            # accumulation legs (no stop-loss/TP; hold to settlement).
            "ALTER TABLE crypto15m_positions ADD COLUMN strategy TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass


def factory_reset(*, wipe_markets: bool = False) -> dict:
    # The research tables are irreplaceable evidence — snapshot them before
    # the wipe so "reset" is never "destroy the dataset".
    backup_research()
    targets = [
        "bot_positions",
        "bot_runs",
        "pnl_snapshots",
        "daily_stats",
        "order_events",
        "alerts",
        "whale_trades",
        "crypto15m_positions",
        "crypto15m_signals",
        "crypto15m_ticks",
        "perp_ticks",
        "perp_trades",
        "perp_candles",
        "perp_funding",
        "perp_farm_fills",
        "perp_positions",
    ]
    if wipe_markets:
        targets.extend(["markets", "events", "trades", "market_snapshots"])

    summary: dict[str, int] = {}
    errors: dict[str, str] = {}

    for t in targets:
        try:
            with get_db() as conn:
                cur = conn.execute(f"SELECT COUNT(*) FROM {t}")
                count = int(cur.fetchone()[0])
                conn.execute(f"DELETE FROM {t}")
                summary[t] = count
        except sqlite3.OperationalError as e:
            errors[t] = str(e)
            summary[t] = -1

    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('bot_positions','bot_runs','pnl_snapshots',"
                "'daily_stats','order_events','alerts','whale_trades',"
                "'crypto15m_positions','crypto15m_signals','crypto15m_ticks',"
                "'perp_ticks','perp_trades','perp_candles','perp_funding',"
                "'perp_farm_fills','perp_positions',"
                "'markets','events','trades','market_snapshots')",
            )
    except sqlite3.OperationalError:
        pass

    try:
        conn = sqlite3.connect(str(db_path()), timeout=30)
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        conn.close()
    except sqlite3.OperationalError:
        pass

    if errors:
        summary["_errors"] = errors  # type: ignore[assignment]
    return summary




def upsert_market(conn, market: dict) -> None:
    conn.execute(
        """
        INSERT INTO markets (ticker, event_ticker, series_ticker, title, yes_sub_title,
            category, status, close_time, volume, volume_24h, open_interest,
            yes_bid, yes_ask, last_price, prev_yes_bid, prev_price,
            result, settlement_value, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            event_ticker=excluded.event_ticker,
            series_ticker=excluded.series_ticker,
            title=COALESCE(excluded.title, title),
            yes_sub_title=COALESCE(excluded.yes_sub_title, yes_sub_title),
            category=COALESCE(NULLIF(excluded.category,''), category),
            status=excluded.status,
            close_time=excluded.close_time,
            prev_yes_bid=markets.yes_bid,
            prev_price=markets.last_price,
            volume=excluded.volume,
            volume_24h=excluded.volume_24h,
            open_interest=excluded.open_interest,
            yes_bid=excluded.yes_bid,
            yes_ask=excluded.yes_ask,
            last_price=excluded.last_price,
            result=excluded.result,
            settlement_value=excluded.settlement_value,
            last_updated=excluded.last_updated
        """,
        (
            market.get("ticker", ""),
            market.get("event_ticker", ""),
            market.get("series_ticker", ""),
            market.get("title", ""),
            market.get("yes_sub_title", ""),
            market.get("category", ""),
            market.get("status", "open"),
            market.get("close_time", ""),
            _to_float(market.get("volume", 0)),
            _to_float(market.get("volume_24h", 0)),
            _to_float(market.get("open_interest", 0)),
            _to_float(market.get("yes_bid", 0)),
            _to_float(market.get("yes_ask", 0)),
            _to_float(market.get("last_price", 0)),
            0,
            0,
            market.get("result", ""),
            _to_float(market.get("settlement_value")) if market.get("settlement_value") is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_market(conn, ticker: str) -> dict | None:
    row = conn.execute("SELECT * FROM markets WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row else None


def get_active_markets(conn, min_volume: float = 0, limit: int = 500) -> list:
    rows = conn.execute(
        "SELECT * FROM markets WHERE status IN ('active','open') AND volume >= ? "
        "ORDER BY volume DESC LIMIT ?",
        (min_volume, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_event(conn, event: dict) -> None:
    conn.execute(
        """
        INSERT INTO events (event_ticker, series_ticker, title, sub_title, category, status, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_ticker) DO UPDATE SET
            series_ticker=excluded.series_ticker,
            title=COALESCE(excluded.title, title),
            sub_title=COALESCE(excluded.sub_title, sub_title),
            category=COALESCE(NULLIF(excluded.category,''), category),
            status=excluded.status,
            last_updated=excluded.last_updated
        """,
        (
            event.get("event_ticker", ""),
            event.get("series_ticker", ""),
            event.get("title", ""),
            event.get("sub_title", ""),
            event.get("category", ""),
            event.get("status", "open"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def trade_exists(conn, trade_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        is not None
    )


def insert_trade(conn, trade: dict) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO trades (trade_id, ticker, event_ticker, count_fp, yes_price,
                no_price, taker_side, dollar_value, category, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.get("trade_id", ""),
                trade.get("ticker", ""),
                trade.get("event_ticker", ""),
                _to_float(trade.get("count_fp", 0)),
                _to_float(trade.get("yes_price", 0)),
                _to_float(trade.get("no_price", 0)),
                trade.get("taker_side", ""),
                _to_float(trade.get("dollar_value", 0)),
                trade.get("category", ""),
                trade.get("created_time", ""),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False




def insert_alert(conn, alert: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO alerts (ticker, event_ticker, title, yes_sub_title, category, signal_type,
            direction, volume_24h, price, price_change, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert.get("ticker", ""),
            alert.get("event_ticker", ""),
            alert.get("title", ""),
            alert.get("yes_sub_title", ""),
            alert.get("category", ""),
            alert.get("signal_type", ""),
            alert.get("direction", ""),
            alert.get("volume_24h", 0),
            alert.get("price", 0),
            alert.get("price_change", 0),
            alert.get("confidence", 0),
        ),
    )
    return cur.lastrowid


def mark_alert_discord_sent(conn, alert_id: int) -> None:
    conn.execute("UPDATE alerts SET discord_sent = 1 WHERE id = ?", (alert_id,))


def recent_alert_exists(
    conn, ticker: str, signal_type: str, direction: str, cooldown_minutes: int = 30
) -> bool:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    ).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        """
        SELECT 1 FROM alerts
        WHERE ticker = ? AND signal_type = ? AND direction = ? AND created_at >= ?
        """,
        (ticker, signal_type, direction, cutoff),
    ).fetchone()
    return row is not None


def fetch_tradeable_momentum_signals(
    conn,
    *,
    min_confidence: float,
    max_age_sec: int,
    allowed_types: list[str],
    seen_ids: set[int],
    limit: int = 50,
) -> list[dict]:
    if not allowed_types:
        return []
    placeholders = ",".join("?" for _ in allowed_types)
    rows = conn.execute(
        f"""SELECT a.* FROM alerts a
            WHERE a.confidence >= ?
              AND a.resolved = 0
              AND (julianday('now') - julianday(a.created_at)) * 86400 <= ?
              AND a.signal_type IN ({placeholders})
            ORDER BY a.created_at DESC
            LIMIT ?""",
        [min_confidence, max_age_sec, *allowed_types, limit * 2],
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if int(d["id"]) in seen_ids:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out




def whale_trade_exists(conn, trade_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM whale_trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        is not None
    )


def insert_whale_trade(conn, trade: dict) -> int:
    try:
        cur = conn.execute(
            """
            INSERT INTO whale_trades
                (trade_id, ticker, event_ticker, title, yes_sub_title, category, taker_side,
                 count_fp, price, dollar_value, market_volume, open_interest, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.get("trade_id", ""),
                trade.get("ticker", ""),
                trade.get("event_ticker", ""),
                trade.get("title", ""),
                trade.get("yes_sub_title", ""),
                trade.get("category", ""),
                trade.get("taker_side", ""),
                _to_float(trade.get("count_fp", 0)),
                _to_float(trade.get("price", 0)),
                _to_float(trade.get("dollar_value", 0)),
                _to_float(trade.get("market_volume", 0)),
                _to_float(trade.get("open_interest", 0)),
                trade.get("confidence", 0),
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return 0


def mark_whale_discord_sent(conn, whale_id: int) -> None:
    conn.execute("UPDATE whale_trades SET discord_sent = 1 WHERE id = ?", (whale_id,))


def fetch_tradeable_whale_signals(
    conn,
    *,
    min_confidence: float,
    max_age_sec: int,
    seen_ids: set[int],
    limit: int = 50,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM whale_trades
           WHERE confidence >= ?
             AND resolved = 0
             AND (julianday('now') - julianday(created_at)) * 86400 <= ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (min_confidence, max_age_sec, limit * 2),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if int(d["id"]) in seen_ids:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def get_recent_whales_for_convergence(conn, hours: int = 2) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """
        SELECT ticker, taker_side, dollar_value, confidence, price,
               count_fp, title, category, created_at,
               id, event_ticker, market_volume
        FROM whale_trades WHERE created_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r[0], r[1])
        groups.setdefault(key, []).append(
            {
                "dollar_value": r[2],
                "confidence": r[3],
                "price": r[4],
                "count_fp": r[5],
                "title": r[6],
                "category": r[7],
                "created_at": r[8],
                "id": r[9],
                "event_ticker": r[10],
                "market_volume": r[11],
            }
        )
    return groups




def save_snapshot(conn, ticker: str, market: dict) -> None:
    conn.execute(
        """INSERT INTO market_snapshots (ticker, volume, volume_24h, open_interest, yes_bid, last_price)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            ticker,
            _to_float(market.get("volume", 0)),
            _to_float(market.get("volume_24h", 0)),
            _to_float(market.get("open_interest", 0)),
            _to_float(market.get("yes_bid", 0)),
            _to_float(market.get("last_price", 0)),
        ),
    )


def get_previous_snapshot(conn, ticker: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM market_snapshots WHERE ticker = ?
           ORDER BY snapshot_at DESC LIMIT 1 OFFSET 1""",
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def get_previous_snapshots_bulk(conn, tickers) -> dict:
    """Second-most-recent snapshot per ticker (same as get_previous_snapshot's
    LIMIT 1 OFFSET 1, i.e. rn=2) for many tickers in ONE query — replaces a
    ~500× N+1 in the momentum scan. Returns {ticker: {volume_24h, yes_bid,
    last_price}}. Chunked under SQLite's 999-variable limit."""
    out: dict[str, dict] = {}
    uniq = [t for t in dict.fromkeys(tickers) if t]
    CHUNK = 400
    for i in range(0, len(uniq), CHUNK):
        chunk = uniq[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT ticker, volume_24h, yes_bid, last_price FROM (
                   SELECT ticker, volume_24h, yes_bid, last_price,
                          ROW_NUMBER() OVER (
                              PARTITION BY ticker ORDER BY snapshot_at DESC, id DESC
                          ) AS rn
                   FROM market_snapshots
                   WHERE ticker IN ({placeholders})
               ) WHERE rn = 2""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["ticker"]] = dict(r)
    return out




def get_unresolved_alerts(conn, days: int = 30) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """SELECT id, ticker, direction, price, created_at
           FROM alerts WHERE resolved = 0 AND created_at >= ?""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unresolved_whale_trades(conn, days: int = 30) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """SELECT id, ticker, taker_side, price, dollar_value, created_at
           FROM whale_trades WHERE resolved = 0 AND created_at >= ?""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_resolved(
    conn, alert_id: int, correct: bool, resolved_price: float, pnl_est: float
) -> None:
    conn.execute(
        """UPDATE alerts SET resolved = 1, outcome_correct = ?, resolved_price = ?,
              pnl_estimate = ?, resolved_at = ? WHERE id = ?""",
        (
            1 if correct else 0,
            resolved_price,
            pnl_est,
            datetime.now(timezone.utc).isoformat(),
            alert_id,
        ),
    )


def mark_whale_resolved(
    conn, trade_id: int, correct: bool, resolved_price: float, pnl_est: float
) -> None:
    conn.execute(
        """UPDATE whale_trades SET resolved = 1, outcome_correct = ?, resolved_price = ?,
              pnl_estimate = ?, resolved_at = ? WHERE id = ?""",
        (
            1 if correct else 0,
            resolved_price,
            pnl_est,
            datetime.now(timezone.utc).isoformat(),
            trade_id,
        ),
    )




def insert_bot_position(conn, row: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO bot_positions (
            signal_source, signal_id, ticker, event_ticker, title, category,
            direction, action, target_contracts, limit_price_cents,
            filled_contracts, avg_fill_price_cents, cost_usd,
            client_order_id, kalshi_order_id, status,
            confidence, edge_pts, signal_price, error,
            balance_before_usd, kalshi_env
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["signal_source"],
            row["signal_id"],
            row["ticker"],
            row.get("event_ticker", ""),
            row.get("title", ""),
            row.get("category", ""),
            row["direction"],
            row.get("action", "buy"),
            int(row["target_contracts"]),
            int(row["limit_price_cents"]),
            int(row.get("filled_contracts", 0)),
            row.get("avg_fill_price_cents"),
            float(row.get("cost_usd", 0.0)),
            row["client_order_id"],
            row.get("kalshi_order_id"),
            row["status"],
            float(row.get("confidence", 0.0)),
            float(row.get("edge_pts", 0.0)),
            float(row.get("signal_price", 0.0)),
            row.get("error"),
            row.get("balance_before_usd"),
            row.get("kalshi_env", "demo"),
        ),
    )
    return cur.lastrowid


def update_bot_position(conn, bot_id: int, **fields) -> None:
    if not fields:
        return
    stamp_last = fields.pop("_stamp_last_updated", True)
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    if stamp_last:
        cols.append("last_updated=datetime('now')")
    vals.append(bot_id)
    conn.execute(
        f"UPDATE bot_positions SET {', '.join(cols)} WHERE id=?", vals
    )


def log_event(
    conn,
    position_id: int,
    kind: str,
    *,
    kalshi_status: str | None = None,
    filled_contracts: int | None = None,
    fill_cost_cents: int | None = None,
    note: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO order_events
              (position_id, kind, kalshi_status, filled_contracts,
               fill_cost_cents, note)
           VALUES (?,?,?,?,?,?)""",
        (position_id, kind, kalshi_status, filled_contracts, fill_cost_cents, note),
    )


def count_open_bot_positions(conn, env: str | None = None) -> int:
    # Excludes signal_source='external' (positions imported from Kalshi, incl.
    # 15m-crypto fills and manual trades) — the main engine's max_open_positions
    # cap governs how many positions IT manages, so externals must not eat its
    # slots. Dollar exposure is still capped separately by current exposure.
    sql = (
        "SELECT COUNT(*) FROM bot_positions "
        "WHERE status IN ('submitted','partial','filled') AND resolved=0 "
        "AND COALESCE(signal_source,'') != 'external'"
    )
    args: tuple = ()
    if env:
        sql += " AND kalshi_env = ?"
        args = (env,)
    return conn.execute(sql, args).fetchone()[0]


def get_market_quotes(conn, tickers) -> dict[str, dict]:
    """Latest stored quote (yes_bid/yes_ask/last_price, in dollars 0..1) per
    ticker, from the markets table. Used to mark open positions to market."""
    out: dict[str, dict] = {}
    for t in {x for x in tickers if x}:
        row = conn.execute(
            "SELECT yes_bid, yes_ask, last_price FROM markets WHERE ticker=?",
            (t,),
        ).fetchone()
        if row:
            out[t] = {
                "yes_bid": float(row["yes_bid"] or 0),
                "yes_ask": float(row["yes_ask"] or 0),
                "last_price": float(row["last_price"] or 0),
            }
    return out


def count_new_positions_today(conn, env: str | None = None, offset_min: int = 0) -> int:
    # offset_min shifts the day boundary to the user's local day (matches the
    # trading-hours gate); default 0 = UTC, unchanged.
    #
    # Count ONLY rows that actually became (or are still working toward) a real
    # position — 'submitted'/'partial'/'filled', matching count_open_bot_positions.
    # Terminal non-position rows ('canceled','error','gone','expired','dry_run')
    # must NOT burn a daily slot: a run of unfilled maker auto-cancels or Kalshi
    # order rejections would otherwise saturate max_daily_new_positions while zero
    # contracts are actually held, silently halting all new entries until the day
    # boundary ("runs for hours then stops trading, empty portfolio, full balance,
    # works next day"). No resolved=0 filter here on purpose: a position opened
    # AND resolved today keeps status='filled', so it still counts as taken-today,
    # which is the cap's intent.
    mod = f"{int(offset_min):+d} minutes"
    sql = (
        "SELECT COUNT(*) FROM bot_positions "
        "WHERE date(created_at, ?)=date('now', ?) "
        "AND status IN ('submitted','partial','filled') "
        "AND COALESCE(signal_source,'') != 'external'"
    )
    args: tuple = (mod, mod)
    if env:
        sql += " AND kalshi_env = ?"
        args = (mod, mod, env)
    return conn.execute(sql, args).fetchone()[0]


def recent_resolved_position_exists(
    conn,
    ticker: str,
    direction: str,
    env: str,
    within_hours: int = 24,
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM bot_positions
           WHERE ticker = ? AND direction = ? AND kalshi_env = ?
             AND resolved = 1
             AND resolved_at IS NOT NULL
             AND resolved_at >= datetime('now', ?)
           LIMIT 1""",
        (ticker, direction, env, f"-{int(within_hours)} hours"),
    ).fetchone()
    return row is not None


def find_flat_resolved_position(
    conn, ticker: str, direction: str, env: str, qty: int
) -> dict | None:
    """A resolved row for (ticker, direction, env) that was closed FLAT —
    pnl $0, settlement $0, no outcome — i.e. the signature of a wrongful
    orphan-close / give-up resolve, not of a real settlement (a genuine loss
    books pnl<0; a genuine win books settlement>0). Used by reconcile: when
    Kalshi still holds this (ticker, side), such a row is our OWN position
    that got mis-resolved and must be re-linked instead of re-imported as a
    cap-exempt 'external' duplicate. Prefers a row whose recorded fill count
    matches the held quantity."""
    row = conn.execute(
        """SELECT * FROM bot_positions
           WHERE ticker=? AND direction=? AND kalshi_env=?
             AND resolved=1
             AND status != 'dry_run'
             AND COALESCE(pnl_usd, 0)=0
             AND COALESCE(settlement_usd, 0)=0
             AND outcome_correct IS NULL
           ORDER BY (filled_contracts = ?) DESC, resolved_at DESC, id DESC
           LIMIT 1""",
        (ticker, direction, env, int(qty)),
    ).fetchone()
    return dict(row) if row else None


def exists_position_in_event(conn, event_ticker: str, env: str) -> bool:
    if not event_ticker:
        return False
    row = conn.execute(
        """SELECT 1 FROM bot_positions
           WHERE event_ticker=? AND resolved=0 AND kalshi_env=?
             AND status IN ('submitted','partial','filled')
           LIMIT 1""",
        (event_ticker, env),
    ).fetchone()
    return row is not None


def count_positions_in_event(conn, event_ticker: str, env: str) -> int:
    if not event_ticker:
        return 0
    row = conn.execute(
        """SELECT COUNT(*) AS c FROM bot_positions
           WHERE event_ticker=? AND resolved=0 AND kalshi_env=?
             AND status IN ('submitted','partial','filled')""",
        (event_ticker, env),
    ).fetchone()
    return int(row["c"]) if row and row["c"] is not None else 0


def exists_position_in_market(
    conn, ticker: str, direction: str, env: str
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM bot_positions
           WHERE ticker=? AND direction=? AND resolved=0 AND kalshi_env=?
             AND status IN ('submitted','partial','filled')
           LIMIT 1""",
        (ticker, direction, env),
    ).fetchone()
    return row is not None


def current_total_exposure_usd(conn, env: str) -> float:
    # Count COMMITTED capital, not just settled cost. A resting/in-flight order
    # (status submitted/partial) is inserted with cost_usd=0 and only gets a real
    # cost on a later reconcile, so summing cost_usd alone undercounts exposure
    # and lets a single scan cycle over-deploy past max_total_exposure_fraction.
    # Value non-filled rows at their committed notional (target_contracts ×
    # limit_price_cents); Kalshi holds the cash for these, so this is the real
    # capital tied up in open positions.
    row = conn.execute(
        """SELECT COALESCE(SUM(
              CASE WHEN status='filled' THEN COALESCE(cost_usd, 0)
                   ELSE MAX(COALESCE(cost_usd, 0),
                            COALESCE(target_contracts, 0) * COALESCE(limit_price_cents, 0) / 100.0)
              END
           ), 0) FROM bot_positions
           WHERE resolved=0 AND status IN ('submitted','partial','filled') AND kalshi_env=?""",
        (env,),
    ).fetchone()
    return float(row[0] or 0.0)


def open_filled_cost_usd(conn, env: str) -> float:
    """Cost basis of currently-open FILLED contracts only. Unlike
    current_total_exposure_usd (which counts committed notional of resting orders
    for risk capping), this EXCLUDES unfilled/resting orders, whose cash is still
    sitting in Kalshi's `balance` (it isn't held). This is the correct value of
    open positions for the account total / P&L: cash + filled-cost reconstructs
    the account with no double-count, and opening a position is P&L-neutral."""
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0) FROM bot_positions
           WHERE resolved=0 AND status IN ('filled','partial') AND kalshi_env=?""",
        (env,),
    ).fetchone()
    return float(row[0] or 0.0)


def open_unrealized_pnl_usd(conn, env: str) -> float:
    """Mark-to-market P&L of open FILLED positions with a live mark (written by
    the 30s reconcile). Feeds the daily stop-loss so a day of positions bleeding
    toward zero can trigger it BEFORE settlement — the balance-delta measure
    alone values open positions at cost and sees nothing until cash moves."""
    row = conn.execute(
        """SELECT COALESCE(SUM(
              filled_contracts * mark_price_cents / 100.0 - COALESCE(cost_usd, 0)
           ), 0) FROM bot_positions
           WHERE resolved=0 AND status='filled' AND kalshi_env=?
             AND mark_price_cents IS NOT NULL AND filled_contracts > 0""",
        (env,),
    ).fetchone()
    return float(row[0] or 0.0)


def _c15_matched_adjusted_cost(conn, env: str) -> float:
    """Open 15m cost basis with MATCHED pairs excluded. Kalshi nets opposing
    positions: the moment both sides of one market are held, the matched
    contracts are redeemed for $1×matched CASH on the spot — the position is
    flat and its basis is no longer held value. Counting it anyway double-
    counts against the cash credit, inflating the account total while a pair
    is open and then 'crashing' it at settlement when the rows resolve.

    Sold-but-unsettled contracts are excluded too: a partially-filled exit
    (stop-loss/TP) leaves the row status='exiting', resolved=0 until
    settlement, but the sold contracts' cash proceeds have ALREADY landed in
    the Kalshi balance — still counting their entry cost as held value
    overstates the account total by sold × avg entry cost, which reads as
    phantom profit to the balance-delta daily stop-loss at exactly the moment
    a stop just half-failed. Each row is valued at its RESIDUAL cost:
    cost × (filled − exit_filled) / filled."""
    rows = conn.execute(
        """SELECT ticker, direction,
                  SUM(MAX(0, filled_contracts - COALESCE(exit_filled_contracts, 0))) f,
                  SUM(COALESCE(cost_usd, 0)
                      * MAX(0, filled_contracts - COALESCE(exit_filled_contracts, 0))
                      / filled_contracts) c
           FROM crypto15m_positions
           WHERE resolved=0 AND filled_contracts > 0 AND kalshi_env=?
           GROUP BY ticker, direction""",
        (env,),
    ).fetchall()
    by_ticker: dict[str, dict[str, tuple[int, float]]] = {}
    for r in rows:
        d = by_ticker.setdefault(r["ticker"], {})
        d[r["direction"]] = (int(r["f"] or 0), float(r["c"] or 0.0))
    total = 0.0
    for sides in by_ticker.values():
        fy, cy = sides.get("yes", (0, 0.0))
        fn, cn = sides.get("no", (0, 0.0))
        matched = min(fy, fn)
        # Residual (unmatched) contracts stay valued at their average cost.
        total += (cy * (fy - matched) / fy if fy else 0.0)
        total += (cn * (fn - matched) / fn if fn else 0.0)
    return total


def open_crypto15m_filled_cost_usd(conn, env: str) -> float:
    """Held value of currently-open 15m-crypto positions (its own table),
    matched-pair aware (see _c15_matched_adjusted_cost). Added to the account
    total because those positions are excluded from the main bot_positions
    reconcile import — without this the total would under-count by the crypto
    cash that's already been spent."""
    return _c15_matched_adjusted_cost(conn, env)


def get_pending_bot_positions(conn, env: str | None = None) -> list[dict]:
    # env filter: the poll loop signs for the CURRENT env only — feeding it the
    # other env's rows made every one of their live orders 404 six polls in a
    # row and get killed as 'gone' the moment the user switched environments.
    # Every other consumer (count/exposure/reconcile/resolve) is env-filtered;
    # rows from the inactive env must simply freeze until that env is active.
    sql = """SELECT * FROM bot_positions
           WHERE resolved=0
             AND signal_source != 'external'
             AND (
               status IN ('submitted','partial')
               OR (status='filled' AND (cost_usd IS NULL OR cost_usd=0))
               OR (status IN ('canceled','expired','gone','error')
                   AND (cost_usd IS NULL OR cost_usd=0)
                   AND (julianday('now')-julianday(created_at))*86400 < 86400)
             )"""
    args: tuple = ()
    if env:
        sql += " AND kalshi_env = ?"
        args = (env,)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_open_bot_positions(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM bot_positions
           WHERE status IN ('submitted','partial','filled') AND resolved=0
           ORDER BY created_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_unresolved_bot_positions(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM bot_positions
           WHERE resolved=0 AND status IN ('filled','partial','expired','canceled','gone')
           ORDER BY created_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def already_traded_signal_ids(conn, source: str, env: str) -> set[int]:
    rows = conn.execute(
        "SELECT signal_id FROM bot_positions WHERE signal_source=? AND kalshi_env=?",
        (source, env),
    ).fetchall()
    return {int(r["signal_id"]) for r in rows}


def fetch_position_by_id(conn, pos_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bot_positions WHERE id=?", (pos_id,)
    ).fetchone()
    return dict(row) if row else None




def insert_crypto15m_position(conn, row: dict) -> int:
    cur = conn.execute(
        """INSERT INTO crypto15m_positions (
              asset, series, ticker, side, direction, target_contracts,
              filled_contracts, entry_limit_cents, avg_entry_cents, cost_usd,
              client_order_id, kalshi_order_id, status, exit_reason, close_time,
              confidence, entry_delta_usd, kalshi_env, dry_run, error, strategy
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["asset"], row["series"], row["ticker"], row["side"],
            row["direction"], int(row["target_contracts"]),
            int(row.get("filled_contracts", 0)),
            int(row["entry_limit_cents"]),
            row.get("avg_entry_cents"),
            float(row.get("cost_usd", 0.0)),
            row["client_order_id"], row.get("kalshi_order_id"),
            row["status"], row.get("exit_reason"), row.get("close_time", ""),
            float(row.get("confidence", 0.0)),
            row.get("entry_delta_usd"),
            row.get("kalshi_env", "demo"),
            1 if row.get("dry_run") else 0,
            row.get("error"),
            row.get("strategy", "") or "",
        ),
    )
    return cur.lastrowid


def update_crypto15m_position(conn, pid: int, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("last_updated=datetime('now')")
    vals.append(pid)
    conn.execute(
        f"UPDATE crypto15m_positions SET {', '.join(cols)} WHERE id=?", vals
    )


def fetch_crypto15m_by_id(conn, pid: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM crypto15m_positions WHERE id=?", (pid,)
    ).fetchone()
    return dict(row) if row else None


def get_open_crypto15m(conn, env: str) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE resolved=0 AND kalshi_env=?
           ORDER BY created_at DESC""",
        (env,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_crypto15m_by_asset(conn, asset: str, env: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE resolved=0 AND asset=? AND kalshi_env=?
           ORDER BY created_at DESC LIMIT 1""",
        (asset, env),
    ).fetchone()
    return dict(row) if row else None


def count_open_crypto15m(conn, env: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM crypto15m_positions WHERE resolved=0 AND kalshi_env=?",
        (env,),
    ).fetchone()[0]


def crypto15m_errored_tickers(conn, env: str) -> set:
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM crypto15m_positions
           WHERE kalshi_env=? AND status='error' AND ticker IS NOT NULL""",
        (env,),
    ).fetchall()
    return {r["ticker"] for r in rows}


def crypto15m_stopped_tickers(conn, env: str) -> set:
    """Tickers whose position exited via stop-loss — excluded from re-entry so
    chop can't churn stop → re-enter → stop within one window (each ticker IS
    one 15-minute window, so the exclusion expires naturally with it)."""
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM crypto15m_positions
           WHERE kalshi_env=? AND exit_reason='stop_loss' AND ticker IS NOT NULL""",
        (env,),
    ).fetchall()
    return {r["ticker"] for r in rows}


def open_crypto15m_committed_usd(conn, env: str) -> float:
    """Capital committed to open 15m positions: matched-pair-adjusted cost of
    filled contracts (matched pairs already redeemed to cash — see
    _c15_matched_adjusted_cost) plus the committed notional of still-resting
    entry remainders. Drives the aggregate 15m cap (crypto15m_max_total_pct);
    without the adjustment, completed pairs kept 'using' budget they had
    already returned."""
    filled_cost = _c15_matched_adjusted_cost(conn, env)
    row = conn.execute(
        """SELECT COALESCE(SUM(
              MAX(0, COALESCE(target_contracts, 0) - COALESCE(filled_contracts, 0))
              * COALESCE(entry_limit_cents, 0) / 100.0
           ), 0) FROM crypto15m_positions
           WHERE resolved=0 AND status='submitted' AND kalshi_env=?""",
        (env,),
    ).fetchone()
    return filled_cost + float(row[0] or 0.0)


def recent_crypto15m(conn, env: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE kalshi_env=?
           ORDER BY created_at DESC LIMIT ?""",
        (env, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_stats(conn, env: str) -> dict:
    row = conn.execute(
        """SELECT
              SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS open_count,
              SUM(CASE WHEN resolved=1 AND outcome_correct=1 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN resolved=1 AND outcome_correct=0 THEN 1 ELSE 0 END) AS losses,
              COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usd END),0) AS realized_pnl,
              COUNT(*) AS total
           FROM crypto15m_positions WHERE kalshi_env=?""",
        (env,),
    ).fetchone()
    return {
        "openCount": int(row["open_count"] or 0),
        "wins": int(row["wins"] or 0),
        "losses": int(row["losses"] or 0),
        "realizedPnlUsd": float(row["realized_pnl"] or 0.0),
        "total": int(row["total"] or 0),
    }


def crypto15m_session_realized_pnl(conn, env: str, since: str | None) -> float:
    """Realized P&L of resolved 15m positions settled at/after `since` — an
    ISO/SQLite UTC datetime, typically the backend start time. Powers the 15m
    session take-profit. `since` falsy = the whole history for the env.
    `datetime(?)` normalises the ISO start to the 'YYYY-MM-DD HH:MM:SS' form
    that resolved_at is stored in, so the string comparison is valid."""
    if since:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) AS pnl
                 FROM crypto15m_positions
                WHERE kalshi_env=? AND resolved=1 AND pnl_usd IS NOT NULL
                  AND resolved_at IS NOT NULL AND resolved_at >= datetime(?)""",
            (env, since),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) AS pnl
                 FROM crypto15m_positions
                WHERE kalshi_env=? AND resolved=1 AND pnl_usd IS NOT NULL""",
            (env,),
        ).fetchone()
    return float(row["pnl"] or 0.0)




def insert_crypto15m_signal(conn, row: dict) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO crypto15m_signals (
              ticker, asset, series, close_time, mins_left, favorite,
              favorite_price, entry_cost, up_prob, delta_pct, open_spot,
              obs_spot, macd, macd_signal, macd_hist, macd_cross, rsi,
              strike, model_prob, edge_net_cents, kalshi_env
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ticker"], row["asset"], row.get("series", ""),
            row.get("close_time", ""), row.get("mins_left"),
            row.get("favorite"), row.get("favorite_price"),
            row.get("entry_cost"), row.get("up_prob"), row.get("delta_pct"),
            row.get("open_spot"), row.get("obs_spot"),
            row.get("macd"), row.get("macd_signal"), row.get("macd_hist"),
            row.get("macd_cross"), row.get("rsi"),
            row.get("strike"), row.get("model_prob"), row.get("edge_net_cents"),
            row.get("kalshi_env", "demo"),
        ),
    )
    return (cur.rowcount or 0) > 0


def unresolved_crypto15m_signals(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_signals
           WHERE resolved=0
           ORDER BY close_time ASC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_crypto15m_signal(conn, ticker: str, up_won: int) -> None:
    conn.execute(
        """UPDATE crypto15m_signals
              SET resolved=1, up_won=?, settled_at=datetime('now')
            WHERE ticker=?""",
        (int(up_won), ticker),
    )


def fetch_resolved_crypto15m_signals(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_signals
           WHERE resolved=1 AND up_won IS NOT NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_signal_counts(conn) -> dict:
    row = conn.execute(
        """SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
              SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS pending
           FROM crypto15m_signals"""
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "resolved": int(row["resolved"] or 0),
        "pending": int(row["pending"] or 0),
    }


# 60, not 14: ticks+outcomes are the research dataset every strategy verdict
# came from (sniper validated, pairs killed) — deleting them at 14 days
# starves user backtests of sample.
_C15_TICKS_KEEP_DAYS = 60
_PERP_TICKS_KEEP_DAYS = 30
_PERP_CANDLES_KEEP_DAYS = 365


def insert_crypto15m_tick(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO crypto15m_ticks (
              ticker, asset, mins_left, yes_bid, yes_ask, up_prob,
              spot, open_spot, delta_pct, macd, macd_signal, macd_hist,
              macd_cross, rsi, strike, delta_signed_pct, sigma1m,
              model_prob, edge_net_cents, settle_prints, no_ask, spot_source,
              kalshi_env
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ticker"], row["asset"], row.get("mins_left"),
            row.get("yes_bid"), row.get("yes_ask"), row.get("up_prob"),
            row.get("spot"), row.get("open_spot"), row.get("delta_pct"),
            row.get("macd"), row.get("macd_signal"), row.get("macd_hist"),
            row.get("macd_cross"), row.get("rsi"),
            row.get("strike"), row.get("delta_signed_pct"), row.get("sigma1m"),
            row.get("model_prob"), row.get("edge_net_cents"),
            row.get("settle_prints"), row.get("no_ask"), row.get("spot_source"),
            row.get("kalshi_env", "demo"),
        ),
    )


def crypto15m_strategy_stats(conn, env: str) -> list[dict]:
    """Fee-true P&L per strategy (favorite/contrarian/model/model_fm/rules/
    pair). Only real fills; the attribution column is stamped at entry."""
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(strategy, ''), 'directional') strategy,
                  COUNT(*) n,
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
                  SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) losses,
                  ROUND(SUM(pnl_usd), 4) pnl_usd,
                  ROUND(SUM(COALESCE(fees_usd,0) + COALESCE(exit_fees_usd,0)), 4) fees_usd
           FROM crypto15m_positions
           WHERE kalshi_env=? AND resolved=1 AND filled_contracts>0
             AND dry_run=0 AND pnl_usd IS NOT NULL
           GROUP BY 1 ORDER BY SUM(pnl_usd) DESC""",
        (env,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_crypto15m_resolved(conn, env: str, limit: int = 200) -> list[dict]:
    """Resolved 15m trades for the History page (newest first)."""
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE kalshi_env=? AND resolved=1 AND filled_contracts>0 AND dry_run=0
           ORDER BY resolved_at DESC, id DESC LIMIT ?""",
        (env, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_tick_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM crypto15m_ticks").fetchone()[0])


# ───────── perps (margin) market-data recording ─────────────────────────────
# Rows arrive pre-converted to integer micro-dollars / centi-contracts
# (kalshi_perps_api.usd_micro / cc at the wire boundary). Helpers take `conn`;
# callers own the transaction — the recorder batches one txn per flush.

_PERP_TICK_COLS = (
    "ticker", "ts_ms", "last_usd_micro", "bid_usd_micro", "ask_usd_micro",
    "bid_size_cc", "ask_size_cc", "volume_24h_cc", "oi_cc",
    "ref_usd_micro", "ref_ts_ms", "settle_mark_usd_micro", "liq_mark_usd_micro",
    "funding_rate", "next_funding_ms", "src", "kalshi_env",
)


def insert_perp_ticks(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = (
        f"INSERT INTO perp_ticks ({','.join(_PERP_TICK_COLS)}) "
        f"VALUES ({','.join('?' * len(_PERP_TICK_COLS))})"
    )
    # kalshi_env is written explicitly on every row — never rely on the DDL
    # default (c15 tables default 'demo', perps 'production'; a missed field
    # would silently mislabel the env).
    conn.executemany(sql, [
        tuple(r.get(c) for c in _PERP_TICK_COLS) for r in rows
    ])
    return len(rows)


def insert_perp_trades(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur = conn.executemany(
        """INSERT OR IGNORE INTO perp_trades
              (trade_id, ticker, ts_ms, price_usd_micro, count_cc,
               taker_side, kalshi_env)
           VALUES (?,?,?,?,?,?,?)""",
        [
            (r.get("trade_id"), r.get("ticker"), r.get("ts_ms"),
             r.get("price_usd_micro"), r.get("count_cc"),
             r.get("taker_side"), r.get("kalshi_env"))
            for r in rows
        ],
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


_PERP_CANDLE_COLS = (
    "ticker", "period_min", "end_ts",
    "bid_open_usd_micro", "bid_high_usd_micro", "bid_low_usd_micro", "bid_close_usd_micro",
    "ask_open_usd_micro", "ask_high_usd_micro", "ask_low_usd_micro", "ask_close_usd_micro",
    "trade_open_usd_micro", "trade_high_usd_micro", "trade_low_usd_micro",
    "trade_close_usd_micro", "trade_mean_usd_micro",
    "volume_cc", "volume_notional_usd_micro", "oi_cc", "kalshi_env",
)


def upsert_perp_candles(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    update_cols = [c for c in _PERP_CANDLE_COLS
                   if c not in ("ticker", "period_min", "end_ts", "kalshi_env")]
    sql = (
        f"INSERT INTO perp_candles ({','.join(_PERP_CANDLE_COLS)}) "
        f"VALUES ({','.join('?' * len(_PERP_CANDLE_COLS))}) "
        f"ON CONFLICT(ticker, period_min, end_ts, kalshi_env) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in update_cols)
    )
    conn.executemany(sql, [
        tuple(r.get(c) for c in _PERP_CANDLE_COLS) for r in rows
    ])
    return len(rows)


def upsert_perp_funding(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    # Finalized rates never change → OR IGNORE keeps re-fetch idempotent.
    cur = conn.executemany(
        """INSERT OR IGNORE INTO perp_funding
              (ticker, funding_time, funding_rate, mark_usd_micro, kalshi_env)
           VALUES (?,?,?,?,?)""",
        [
            (r.get("ticker"), r.get("funding_time"), r.get("funding_rate"),
             r.get("mark_usd_micro"), r.get("kalshi_env"))
            for r in rows
        ],
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def perp_last_candle_end_ts(
    conn, ticker: str, period_min: int, env: str = "production",
) -> int | None:
    row = conn.execute(
        """SELECT MAX(end_ts) FROM perp_candles
           WHERE ticker=? AND period_min=? AND kalshi_env=?""",
        (ticker, int(period_min), env),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def perp_last_funding_time(conn, ticker: str = "", env: str = "production") -> str | None:
    if ticker:
        row = conn.execute(
            "SELECT MAX(funding_time) FROM perp_funding WHERE ticker=? AND kalshi_env=?",
            (ticker, env),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(funding_time) FROM perp_funding WHERE kalshi_env=?", (env,),
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def open_perp_position(conn, row: dict) -> int:
    cur = conn.execute(
        """INSERT INTO perp_positions
              (ticker, side, dry_run, count_cc, entry_usd_micro, leverage,
               fees_usd_micro, entry_reason, kalshi_env)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            row["ticker"], row["side"], 1 if row.get("dry_run", True) else 0,
            row["count_cc"], row["entry_usd_micro"], row.get("leverage", 1),
            row.get("fees_usd_micro", 0), row.get("entry_reason", ""),
            row.get("kalshi_env", "production"),
        ),
    )
    return int(cur.lastrowid)


def close_perp_position(
    conn, pos_id: int, *, exit_usd_micro: int, fees_usd_micro: int,
    funding_usd_micro: int, pnl_usd_micro: int, exit_reason: str,
) -> None:
    conn.execute(
        """UPDATE perp_positions
           SET closed_at=datetime('now'), exit_usd_micro=?,
               fees_usd_micro=fees_usd_micro+?, funding_usd_micro=?,
               pnl_usd_micro=?, exit_reason=?
           WHERE id=?""",
        (exit_usd_micro, fees_usd_micro, funding_usd_micro,
         pnl_usd_micro, exit_reason, int(pos_id)),
    )


def get_open_perp_position(conn, env: str, *, dry_run: bool | None = None) -> dict | None:
    sql = "SELECT * FROM perp_positions WHERE closed_at IS NULL AND kalshi_env=?"
    params: list = [env]
    if dry_run is not None:
        sql += " AND dry_run=?"
        params.append(1 if dry_run else 0)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return dict(row) if row else None


def recent_perp_positions(conn, env: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM perp_positions WHERE kalshi_env=?
           ORDER BY id DESC LIMIT ?""",
        (env, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def perp_strategy_day_pnl(conn, env: str, day_utc: str) -> int:
    """Realized strategy P&L (micro-dollars) closed on the given UTC day."""
    row = conn.execute(
        """SELECT COALESCE(SUM(pnl_usd_micro), 0) FROM perp_positions
           WHERE kalshi_env=? AND closed_at >= ? AND closed_at < datetime(?, '+1 day')""",
        (env, f"{day_utc} 00:00:00", f"{day_utc} 00:00:00"),
    ).fetchone()
    return int(row[0] or 0)


def insert_perp_farm_fill(conn, row: dict) -> bool:
    """True when the fill is NEW (dedup on trade_id per env) — callers only
    apply inventory/P&L updates for new rows."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO perp_farm_fills
              (trade_id, order_id, ticker, ts_ms, side, count_cc,
               price_usd_micro, fee_usd_micro, is_taker,
               realized_pnl_usd_micro, inventory_after_cc, kalshi_env)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row.get("trade_id"), row.get("order_id", ""), row.get("ticker"),
            row.get("ts_ms"), row.get("side"), row.get("count_cc"),
            row.get("price_usd_micro"), row.get("fee_usd_micro", 0),
            1 if row.get("is_taker") else 0,
            row.get("realized_pnl_usd_micro", 0),
            row.get("inventory_after_cc"), row.get("kalshi_env"),
        ),
    )
    return bool(cur.rowcount and cur.rowcount > 0)


def perp_farm_fill_seen(conn, trade_id: str, env: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM perp_farm_fills WHERE trade_id=? AND kalshi_env=? LIMIT 1",
        (trade_id, env),
    ).fetchone()
    return row is not None


def perp_farm_stats(conn, env: str, *, day_utc: str | None = None) -> dict:
    """Volume/fees/realized aggregates in micro-dollars. day_utc 'YYYY-MM-DD'
    filters to that UTC day; None = lifetime."""
    where = "kalshi_env=?"
    params: list = [env]
    if day_utc:
        where += " AND observed_at >= ? AND observed_at < datetime(?, '+1 day')"
        params += [f"{day_utc} 00:00:00", f"{day_utc} 00:00:00"]
    row = conn.execute(
        f"""SELECT COUNT(*),
                   COALESCE(SUM(CAST(price_usd_micro AS REAL) * count_cc / 100.0), 0),
                   COALESCE(SUM(fee_usd_micro), 0),
                   COALESCE(SUM(realized_pnl_usd_micro), 0),
                   MAX(observed_at)
            FROM perp_farm_fills WHERE {where}""",
        params,
    ).fetchone()
    return {
        "fills": int(row[0] or 0),
        "volume_usd_micro": int(row[1] or 0),
        "fees_usd_micro": int(row[2] or 0),
        "realized_usd_micro": int(row[3] or 0),
        "lastAt": row[4],
    }


def perp_collection_counts(conn, env: str = "production") -> dict:
    out: dict = {"ticks": 0, "trades": 0, "candles": 0, "funding": 0,
                 "firstAt": None, "lastAt": None, "byTicker": []}
    try:
        for key, table in (("ticks", "perp_ticks"), ("trades", "perp_trades"),
                           ("candles", "perp_candles"), ("funding", "perp_funding")):
            out[key] = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE kalshi_env=?", (env,),
            ).fetchone()[0])
        row = conn.execute(
            "SELECT MIN(observed_at), MAX(observed_at) FROM perp_ticks WHERE kalshi_env=?",
            (env,),
        ).fetchone()
        if row:
            out["firstAt"], out["lastAt"] = row[0], row[1]
        rows = conn.execute(
            """SELECT ticker, COUNT(*) n, MAX(observed_at) last_at
               FROM perp_ticks WHERE kalshi_env=? GROUP BY ticker ORDER BY ticker""",
            (env,),
        ).fetchall()
        out["byTicker"] = [
            {"ticker": r[0], "ticks": int(r[1]), "lastAt": r[2]} for r in rows
        ]
    except sqlite3.OperationalError:
        pass
    return out




def insert_pnl_snapshot(
    conn,
    *,
    cash_usd: float,
    portfolio_usd: float,
    realized_pnl_usd: float,
    wins: int,
    losses: int,
    open_positions: int,
    env: str,
) -> None:
    conn.execute(
        """INSERT INTO pnl_snapshots
              (kalshi_env, cash_usd, portfolio_usd, total_usd, realized_pnl_usd,
               wins, losses, open_positions)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            env,
            float(cash_usd),
            float(portfolio_usd),
            float(cash_usd) + float(portfolio_usd),
            float(realized_pnl_usd),
            int(wins),
            int(losses),
            int(open_positions),
        ),
    )


def get_risk_breach_start(conn, env: str, kind: str) -> float | None:
    """Persisted first-breach timestamp (unix seconds) for the daily-risk
    gate, or None when no breach is latched. Survives restarts so a reboot
    mid-breach can't reopen the trading gate for another persistence window."""
    row = conn.execute(
        "SELECT breach_started_at FROM risk_state WHERE kalshi_env=? AND kind=?",
        (env, kind),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def set_risk_breach_start(
    conn, env: str, kind: str, started_at: float | None
) -> None:
    conn.execute(
        """INSERT INTO risk_state (kalshi_env, kind, breach_started_at, updated_at)
           VALUES (?,?,?,datetime('now'))
           ON CONFLICT(kalshi_env, kind) DO UPDATE SET
             breach_started_at=excluded.breach_started_at,
             updated_at=excluded.updated_at""",
        (env, kind, started_at),
    )


def earliest_pnl_total(conn, env: str) -> float | None:
    # Ignore any $0/unknown-balance rows so they can never become a baseline.
    row = conn.execute(
        """SELECT total_usd FROM pnl_snapshots
           WHERE kalshi_env = ? AND total_usd > 0
           ORDER BY at ASC LIMIT 1""",
        (env,),
    ).fetchone()
    if row is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def first_snapshot_of_today(conn, env: str, offset_min: int = 0) -> dict | None:
    # Ignore any $0/unknown-balance rows so they can never become today's baseline.
    # offset_min shifts the day boundary to the user's local day (default 0 = UTC).
    # The day-start boundary is computed in Python so the predicate is a
    # SARGABLE range (`at >= ?`) served by idx_pnl_env_at — the old
    # `date(at, ?) = date('now', ?)` form full-scanned + temp-sorted the
    # whole snapshot history on every daily-risk check and account build.
    now_local = datetime.utcnow() + timedelta(minutes=int(offset_min))
    day_start_utc = (
        datetime(now_local.year, now_local.month, now_local.day)
        - timedelta(minutes=int(offset_min))
    )
    row = conn.execute(
        """SELECT * FROM pnl_snapshots
           WHERE kalshi_env = ? AND at >= ? AND total_usd > 0
           ORDER BY at ASC LIMIT 1""",
        (env, day_start_utc.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return dict(row) if row else None


def latest_snapshot(conn, env: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM pnl_snapshots
           WHERE kalshi_env = ?
           ORDER BY at DESC LIMIT 1""",
        (env,),
    ).fetchone()
    return dict(row) if row else None




def start_bot_run(
    conn, *, env: str, cash_usd: float, portfolio_usd: float,
    lifetime_trades: int = 0, lifetime_wins: int = 0, lifetime_losses: int = 0,
) -> int:
    conn.execute(
        """UPDATE bot_runs
           SET ended_at = COALESCE(ended_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
               notes    = COALESCE(notes, '') || ' [auto-closed on next start]'
           WHERE kalshi_env = ? AND ended_at IS NULL""",
        (env,),
    )
    total = float(cash_usd) + float(portfolio_usd)
    cur = conn.execute(
        """INSERT INTO bot_runs (
              kalshi_env, start_cash_usd, start_portfolio_usd,
              start_total_usd, end_cash_usd, end_portfolio_usd,
              end_total_usd, pnl_usd,
              start_trades_opened, start_trades_won, start_trades_lost,
              trades_opened, trades_won, trades_lost
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, 0, 0)""",
        (
            env, float(cash_usd), float(portfolio_usd), total,
            float(cash_usd), float(portfolio_usd), total,
            int(lifetime_trades), int(lifetime_wins), int(lifetime_losses),
        ),
    )
    return int(cur.lastrowid or 0)


def heartbeat_bot_run(
    conn, run_id: int, *, cash_usd: float, portfolio_usd: float,
    lifetime_trades: int = 0, lifetime_wins: int = 0, lifetime_losses: int = 0,
) -> None:
    if not run_id:
        return
    total = float(cash_usd) + float(portfolio_usd)
    conn.execute(
        """UPDATE bot_runs
              SET end_cash_usd = ?,
                  end_portfolio_usd = ?,
                  end_total_usd = ?,
                  pnl_usd = ? - start_total_usd,
                  trades_opened = MAX(0, ? - start_trades_opened),
                  trades_won    = MAX(0, ? - start_trades_won),
                  trades_lost   = MAX(0, ? - start_trades_lost)
            WHERE id = ?""",
        (
            float(cash_usd), float(portfolio_usd), total, total,
            int(lifetime_trades), int(lifetime_wins), int(lifetime_losses),
            int(run_id),
        ),
    )


def end_bot_run(conn, run_id: int) -> None:
    if not run_id:
        return
    conn.execute(
        "UPDATE bot_runs SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND ended_at IS NULL",
        (int(run_id),),
    )


def get_active_run(conn, env: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM bot_runs
            WHERE kalshi_env = ? AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1""",
        (env,),
    ).fetchone()
    return dict(row) if row else None


def get_recent_runs(conn, env: str | None = None, limit: int = 50) -> list[dict]:
    if env:
        rows = conn.execute(
            """SELECT * FROM bot_runs
                WHERE kalshi_env = ?
                ORDER BY started_at DESC LIMIT ?""",
            (env, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bot_runs ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pnl_snapshots(
    conn, *, since_hours: int = 168, env: str | None = None,
    max_points: int = 2000,
) -> list[dict]:
    # Sargable predicate: the old per-row julianday() arithmetic could never
    # use idx_pnl_at — a full table scan (hundreds of thousands of rows after
    # weeks of 24/7 snapshots) on the event-loop thread, every 30s, for every
    # dashboard pull.
    since_hours = max(1, min(int(since_hours), 24 * 365))
    sql = "SELECT * FROM pnl_snapshots WHERE at >= datetime('now', ?)"
    args: list = [f"-{since_hours} hours"]
    if env:
        sql += " AND kalshi_env = ?"
        args.append(env)
    sql += " ORDER BY at ASC"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    # Downsample by stride to a renderable series — a 7-day window of 15s
    # snapshots is ~40k points, i.e. ~5MB of JSON over stdio into a chart that
    # can't show more than a few thousand anyway. Keep the newest point exact.
    if max_points and len(rows) > max_points:
        stride = -(-len(rows) // max_points)  # ceil
        sampled = rows[::stride]
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        rows = sampled
    return rows


def recent_balance_transition(conn, env: str, within_sec: int = 180) -> bool:
    """True when a bot position was OPENED or RESOLVED within the last
    `within_sec` seconds — the window where the exchange's cash ledger and
    our position ledger can disagree (an entry's debit / a settlement's
    payout is in flight), so account totals built from cash + open-cost
    transiently dip by ~one position (the "-$3.00 balance" reports) or lag
    a win. Consumers show a 'syncing' hint instead of the scary number.
    Keys on created_at / resolved_at, NOT last_updated — the 30s mark
    reconcile touches last_updated constantly on open rows. Default window
    matches the measured worst settlement-payout gap (~130s observed) and
    the daily-risk persistence window (180s)."""
    args = (env, f"-{int(within_sec)} seconds", f"-{int(within_sec)} seconds")
    # bot_positions has no dry_run column; crypto15m_positions does (its
    # dry-run rows never move real money, so they must not flag syncing).
    for table, extra in (("bot_positions", ""),
                         ("crypto15m_positions", "AND dry_run = 0")):
        row = conn.execute(
            f"""SELECT 1 FROM {table}
                WHERE kalshi_env = ? {extra}
                  AND ((resolved_at IS NOT NULL AND resolved_at >= datetime('now', ?))
                    OR (created_at >= datetime('now', ?)
                        AND status IN ('submitted', 'partial', 'filled')))
                LIMIT 1""",
            args,
        ).fetchone()
        if row:
            return True
    return False


def aggregate_stats(conn, env: str | None = None) -> dict:
    cond = ""
    args: list = []
    if env:
        cond = "AND kalshi_env=?"
        args = [env]
    row = conn.execute(
        f"""SELECT
              SUM(CASE WHEN status IN ('submitted','partial') AND resolved=0 THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='filled' AND resolved=0 THEN 1 ELSE 0 END) AS open_filled,
              SUM(CASE WHEN resolved=1 AND outcome_correct=1 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN resolved=1 AND outcome_correct=0 THEN 1 ELSE 0 END) AS losses,
              COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usd END),0) AS realized_pnl,
              COALESCE(SUM(CASE WHEN resolved=1 AND date(resolved_at)=date('now') THEN pnl_usd END),0) AS today_pnl,
              SUM(CASE WHEN resolved=1 AND outcome_correct=1 AND date(resolved_at)=date('now') THEN 1 ELSE 0 END) AS today_wins,
              SUM(CASE WHEN resolved=1 AND outcome_correct=0 AND date(resolved_at)=date('now') THEN 1 ELSE 0 END) AS today_losses,
              COALESCE(SUM(CASE WHEN resolved=0 AND status IN ('filled','partial') THEN cost_usd END),0) AS open_cost,
              COALESCE(SUM(fees_usd),0) AS fees,
              SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved_count,
              COUNT(*) AS total_opened
           FROM bot_positions
           WHERE status!='dry_run' {cond}""",
        args,
    ).fetchone()
    return {
        "pending": int(row["pending"] or 0),
        "open_filled": int(row["open_filled"] or 0),
        "wins": int(row["wins"] or 0),
        "losses": int(row["losses"] or 0),
        "realized_pnl": float(row["realized_pnl"] or 0.0),
        "today_pnl": float(row["today_pnl"] or 0.0),
        "today_wins": int(row["today_wins"] or 0),
        "today_losses": int(row["today_losses"] or 0),
        "open_cost": float(row["open_cost"] or 0.0),
        "fees": float(row["fees"] or 0.0),
        "resolved_count": int(row["resolved_count"] or 0),
        "total_opened": int(row["total_opened"] or 0),
    }



_DELETE_BATCH = 50_000


def _delete_batched(
    where_sql: str, params: tuple, table: str, *, batch: int = _DELETE_BATCH
) -> int:
    sql = (
        f"DELETE FROM {table} WHERE rowid IN "
        f"(SELECT rowid FROM {table} WHERE {where_sql} LIMIT ?)"
    )
    total = 0
    while True:
        with get_db() as conn:
            n = conn.execute(sql, (*params, batch)).rowcount or 0
        total += n
        if n < batch:
            return total


def cleanup_old_data(
    conn=None, *, trade_hours: int = 48, alert_days: int = 45,
    snapshot_hours: int = 6, pnl_days: int = 45,
    event_days: int = 30, c15_signal_days: int = 60,
) -> int:
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    trade_cutoff = (now - timedelta(hours=trade_hours)).strftime("%Y-%m-%d %H:%M:%S")
    alert_cutoff = (now - timedelta(days=alert_days)).strftime("%Y-%m-%d %H:%M:%S")
    snap_cutoff = (now - timedelta(hours=snapshot_hours)).strftime("%Y-%m-%d %H:%M:%S")
    pnl_cutoff = (now - timedelta(days=pnl_days)).strftime("%Y-%m-%d %H:%M:%S")
    settled_cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    event_cutoff = (now - timedelta(days=event_days)).strftime("%Y-%m-%d %H:%M:%S")
    c15sig_cutoff = (now - timedelta(days=c15_signal_days)).strftime("%Y-%m-%d %H:%M:%S")

    ticks_cutoff = (now - timedelta(days=_C15_TICKS_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    perp_tick_cutoff = (now - timedelta(days=_PERP_TICKS_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    perp_candle_cutoff_epoch = int((now - timedelta(days=_PERP_CANDLES_KEEP_DAYS)).timestamp())

    deleted = 0
    deleted += _delete_batched("created_time < ?", (trade_cutoff,), "trades")
    deleted += _delete_batched("snapshot_at < ?", (snap_cutoff,), "market_snapshots")
    deleted += _delete_batched("observed_at < ?", (ticks_cutoff,), "crypto15m_ticks")
    # Perps recording: ticks/trades are the bulky raw series (5 symbols @1Hz ≈
    # 430k rows/day); candles+funding are the compact long-horizon record —
    # candles kept a year, funding never pruned (13 tickers × 3 rows/day).
    deleted += _delete_batched("observed_at < ?", (perp_tick_cutoff,), "perp_ticks")
    deleted += _delete_batched("observed_at < ?", (perp_tick_cutoff,), "perp_trades")
    deleted += _delete_batched("end_ts < ?", (perp_candle_cutoff_epoch,), "perp_candles")
    # order_events + resolved 15m signals were never swept → unbounded growth.
    deleted += _delete_batched("created_at < ?", (event_cutoff,), "order_events")
    deleted += _delete_batched(
        "resolved = 1 AND observed_at < ?", (c15sig_cutoff,), "crypto15m_signals")
    deleted += _delete_batched(
        "status NOT IN ('active','open') AND last_updated < ?", (settled_cutoff,), "markets")
    deleted += _delete_batched(
        "close_time != '' AND close_time < ? AND last_updated < ?",
        (now_iso, settled_cutoff), "markets")
    deleted += _delete_batched("volume < 10 AND volume_24h < 5", (), "markets")
    with get_db() as c:
        deleted += c.execute(
            "DELETE FROM alerts WHERE resolved = 1 AND created_at < ?", (alert_cutoff,)
        ).rowcount or 0
        deleted += c.execute(
            "DELETE FROM whale_trades WHERE resolved = 1 AND created_at < ?", (alert_cutoff,)
        ).rowcount or 0
        # UNRESOLVED alerts/whales must age out too: the resolvers only look
        # back 30 days, so a signal on a delisted/never-settling market stays
        # resolved=0 FOREVER — permanent growth the resolved-only sweep missed.
        deleted += c.execute(
            "DELETE FROM alerts WHERE resolved = 0 AND created_at < ?", (alert_cutoff,)
        ).rowcount or 0
        deleted += c.execute(
            "DELETE FROM whale_trades WHERE resolved = 0 AND created_at < ?", (alert_cutoff,)
        ).rowcount or 0
        # Keep one ANCHOR row per env — the first-ever positive-total
        # snapshot. earliest_pnl_total() is the all-time P&L/ROI baseline;
        # pruning it silently re-baselined "all-time" to a trailing
        # 45-day window every maintenance pass.
        deleted += c.execute(
            """DELETE FROM pnl_snapshots WHERE at < ?
               AND id NOT IN (SELECT MIN(id) FROM pnl_snapshots
                              WHERE total_usd > 0 GROUP BY kalshi_env)""",
            (pnl_cutoff,),
        ).rowcount or 0
        # events had NO pruning rule at all (sync_events upserts every 10 min
        # forever); anything not touched in `event_days` is long closed.
        deleted += c.execute(
            "DELETE FROM events WHERE last_updated < ?", (event_cutoff,)
        ).rowcount or 0
    return deleted


def _reclaimable_mb() -> float:
    with get_db() as conn:
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return fl * ps / 1e6


def vacuum() -> None:
    conn = sqlite3.connect(str(db_path()), timeout=120)
    conn.isolation_level = None
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        conn.execute("VACUUM")
    finally:
        conn.close()


def backup_research(keep: int = 7) -> str | None:
    """Nightly copy of the whole DB into data/backups (rotated). The research
    tables (ticks/signals/positions) are the irreplaceable evidence every
    strategy verdict came from — before this there was NO backup anywhere and
    factory_reset wiped them permanently. sqlite3's online backup API is safe
    against concurrent writers."""
    try:
        src = str(db_path())
        bdir = os.path.join(os.path.dirname(src), "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d")
        dest = os.path.join(bdir, f"research-{stamp}.db")
        if os.path.exists(dest):
            return dest  # already taken today
        with get_db() as conn:
            out = sqlite3.connect(dest)
            try:
                conn.backup(out)
            finally:
                out.close()
        # rotate: keep the newest `keep`
        snaps = sorted(
            f for f in os.listdir(bdir)
            if f.startswith("research-") and f.endswith(".db")
        )
        for old_f in snaps[:-keep]:
            try:
                os.remove(os.path.join(bdir, old_f))
            except OSError:
                pass
        return dest
    except Exception:
        return None


def run_maintenance(*, vacuum_min_free_mb: float = 50.0, force_vacuum: bool = False) -> dict:
    deleted = cleanup_old_data()
    backed_up = backup_research()
    free_mb = _reclaimable_mb()
    vacuumed = False
    if force_vacuum or free_mb >= vacuum_min_free_mb:
        vacuum()
        vacuumed = True
    return {"deleted": deleted, "reclaimable_mb": round(free_mb, 1), "vacuumed": vacuumed,
            "backup": backed_up}
