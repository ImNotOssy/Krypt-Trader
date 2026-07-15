from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
from config import merge_with_defaults


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "history-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def test_normalize_utc_accepts_offsets_and_epoch_ms():
    import historical_data as hd

    assert hd.normalize_utc("2026-07-03T12:00:00-05:00") == "2026-07-03 17:00:00"
    assert hd.normalize_utc("2026-07-03 12:00:00") == "2026-07-03 12:00:00"
    assert hd.normalize_utc(1_783_123_199_000) == "2026-07-03 23:59:59"


def test_import_kalshi_crypto15m_csv_is_utc_and_idempotent(fresh_db, tmp_path):
    import historical_data as hd

    path = tmp_path / "kalshi-15m.csv"
    path.write_text(
        "\n".join([
            "ticker,asset,observed_at,close_time,yes_bid,yes_ask,up_prob,spot,open_spot,delta_pct,no_ask,up_won,resolved",
            "KXBTC15M-HIST,BTC,2026-07-03T12:00:00-05:00,2026-07-03T12:04:00-05:00,0.91,0.93,0.93,61000,60900,0.0016,0.08,1,1",
        ]),
        encoding="utf-8",
    )

    first = hd.import_kalshi_crypto15m_csv(path, env="production")
    second = hd.import_kalshi_crypto15m_csv(path, env="production")

    assert first["ticksImported"] == 1
    assert first["signalsUpserted"] == 1
    assert second["ticksSkipped"] == 1
    with db.get_db() as conn:
        tick = conn.execute("SELECT * FROM crypto15m_ticks").fetchone()
        sig = conn.execute("SELECT * FROM crypto15m_signals").fetchone()
    assert tick["observed_at"] == "2026-07-03 17:00:00"
    assert tick["yes_ask"] == pytest.approx(0.93)
    assert sig["close_time"] == "2026-07-03 17:04:00"
    assert sig["up_won"] == 1


def test_import_coinbase_candles_csv_upserts_and_validation_reports_gaps(
    fresh_db, tmp_path
):
    import historical_data as hd

    path = tmp_path / "btc-1m.csv"
    path.write_text(
        "\n".join([
            "timestamp,open,high,low,close,volume",
            "2026-07-03T12:00:00-05:00,61000,61002,60999,61001,12.5",
            "2026-07-03T12:01:00-05:00,61001,61003,61000,61002,13.5",
        ]),
        encoding="utf-8",
    )

    first = hd.import_coinbase_candles_csv(path, asset="BTC", timeframe_sec=60)
    second = hd.import_coinbase_candles_csv(path, asset="BTC", timeframe_sec=60)

    assert first["candlesImported"] == 2
    assert second["candlesImported"] == 2
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM coinbase_candles ORDER BY ts"
        ).fetchall()
        report = hd.validate_dataset(conn, env="production", since_days=3650)
    assert len(rows) == 2
    assert rows[0]["ts"] == "2026-07-03 17:00:00"
    assert rows[0]["close"] == pytest.approx(61001.0)
    assert report["coinbase"]["assets"]["BTC"]["candles"] == 2


def test_download_coinbase_candles_pages_and_upserts(fresh_db):
    import historical_data as hd

    now = datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc)
    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, params: dict):
        calls.append((url, dict(params)))
        base = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        return [
            [int(base.timestamp()), 61000, 61010, 61002, 61005, 12.0],
            [int((base + timedelta(minutes=1)).timestamp()), 61005, 61012, 61006, 61010, 13.0],
        ]

    out = hd.download_coinbase_candles(
        product_id="BTC-USD",
        asset="BTC",
        days=1,
        granularity_sec=60,
        now=now,
        http_get=fake_get,
    )

    assert out["candlesImported"] == 10
    assert out["chunks"] == 5
    assert all("/products/BTC-USD/candles" in url for url, _ in calls)
    assert calls[0][1]["granularity"] == 60
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM coinbase_candles ORDER BY ts"
        ).fetchall()
    assert len(rows) == 10
    assert rows[0]["asset"] == "BTC"
    assert rows[0]["timeframe_sec"] == 60
    assert rows[0]["close"] == pytest.approx(61005.0)
    assert out["firstAt"] == rows[0]["ts"]
    assert out["lastAt"] == rows[-1]["ts"]


def test_download_kalshi_market_candles_imports_ticks_from_historical_shape(fresh_db):
    import historical_data as hd

    now = datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc)
    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, params: dict):
        calls.append((url, dict(params)))
        start = int(params["start_ts"])
        return {
            "ticker": "KXBTC-TEST",
            "candlesticks": [
                {
                    "end_period_ts": start + 60,
                    "yes_bid": {"close": 62},
                    "yes_ask": {"close_dollars": 0.64},
                    "price": {"mean": 63},
                    "volume": 125,
                },
                {
                    "end_period_ts": start + 120,
                    "yes_bid": {"close_dollars": 0.65},
                    "yes_ask": {"close": 66},
                    "price": {"close_dollars": 0.655},
                    "open_interest": 42,
                },
            ],
        }

    out = hd.download_kalshi_market_candles(
        ticker="KXBTC-TEST",
        series_ticker="KXBTC",
        asset="BTC",
        env="production",
        days=1,
        period_interval=1,
        historical=True,
        now=now,
        http_get=fake_get,
    )

    assert out["candlesImported"] == 2
    assert out["signalsUpserted"] == 1
    assert calls[0][0].endswith("/historical/markets/KXBTC-TEST/candlesticks")
    assert calls[0][1]["period_interval"] == 1
    with db.get_db() as conn:
        signal = conn.execute("SELECT * FROM crypto15m_signals").fetchone()
        ticks = conn.execute(
            "SELECT * FROM crypto15m_ticks ORDER BY observed_at"
        ).fetchall()
    assert signal["ticker"] == "KXBTC-TEST"
    assert signal["resolved"] == 0
    assert ticks[0]["yes_bid"] == pytest.approx(0.62)
    assert ticks[0]["yes_ask"] == pytest.approx(0.64)
    assert ticks[0]["up_prob"] == pytest.approx(0.63)
    assert ticks[0]["no_ask"] == pytest.approx(0.38)


def test_validate_dataset_reports_missing_fields_and_large_tick_gaps(fresh_db):
    import historical_data as hd

    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO crypto15m_signals (ticker, asset, series, favorite,
               favorite_price, entry_cost, resolved, up_won, close_time, kalshi_env)
               VALUES ('KXBTC15M-GAP','BTC','KXBTC15M','up',0.93,0.93,1,1,
                       '2026-07-03T12:05:00Z','production')"""
        )
        conn.execute(
            """INSERT INTO crypto15m_ticks (
                  ticker, asset, observed_at, mins_left, yes_bid, yes_ask,
                  up_prob, spot, kalshi_env
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("KXBTC15M-GAP", "BTC", "2026-07-03 12:00:00", 5, 0.91, 0.93, 0.93, 61000, "production"),
        )
        conn.execute(
            """INSERT INTO crypto15m_ticks (
                  ticker, asset, observed_at, mins_left, yes_bid, yes_ask,
                  up_prob, spot, kalshi_env
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("KXBTC15M-GAP", "BTC", "2026-07-03 12:04:00", 1, 0.90, None, 0.92, None, "production"),
        )
        report = hd.validate_dataset(conn, env="production", since_days=3650, max_gap_seconds=90)

    assert report["crypto15m"]["ticks"] == 2
    assert report["crypto15m"]["missingFields"]["yes_ask"] == 1
    assert report["crypto15m"]["missingFields"]["spot"] == 1
    assert report["crypto15m"]["gapCount"] == 1
    assert report["crypto15m"]["largestGaps"][0]["gapSeconds"] == 240


def test_replay_uses_coinbase_candles_as_ma_warmup_when_tick_warmup_missing(
    fresh_db, tmp_path
):
    import historical_data as hd
    import replay

    now = datetime.now(timezone.utc).replace(microsecond=0)
    trade_at = now - timedelta(hours=1)
    warmup_start = trade_at - timedelta(minutes=310)
    path = tmp_path / "btc-warmup.csv"
    lines = ["timestamp,open,high,low,close,volume"]
    for i in range(310):
        ts = warmup_start + timedelta(minutes=i)
        close = 50000.0 + i
        lines.append(f"{ts.isoformat()},{close},{close},{close},{close},1")
    path.write_text("\n".join(lines), encoding="utf-8")
    hd.import_coinbase_candles_csv(path, asset="BTC", timeframe_sec=60)

    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO crypto15m_signals (ticker, asset, series, favorite,
               favorite_price, entry_cost, resolved, up_won, close_time, kalshi_env)
               VALUES ('KXBTC15M-CBWARM','BTC','KXBTC15M','up',0.50,0.34,1,1,
                       ?,'production')""",
            ((trade_at + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.execute(
            """INSERT INTO crypto15m_ticks (
                  ticker, asset, observed_at, mins_left, yes_bid, yes_ask,
                  up_prob, spot, open_spot, delta_pct, no_ask, kalshi_env
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "KXBTC15M-CBWARM", "BTC", trade_at.strftime("%Y-%m-%d %H:%M:%S"),
                10.0, 0.30, 0.34, 0.50, 50350.0, 50300.0, 0.001, 0.70,
                "production",
            ),
        )

    cfg = merge_with_defaults({
        "crypto15m_strategy_mode": "btc_ma_crossover",
        "crypto15m_assets": ["BTC"],
        "crypto15m_order_size": 5,
        "crypto15m_time_delay_min": 15,
        "crypto15m_min_entry_cents": 5,
        "crypto15m_max_entry_cents": 59,
        "crypto15m_min_entry_seconds_left": 120,
        "crypto15m_entry_diff": 0.0,
    })

    out = replay.replay(cfg, env="production", since_days=1)

    assert out["n"] == 1
    assert out["trades"][0]["ema12_1m"] is not None
    assert out["trades"][0]["sma50_5m"] is not None
    assert out["dataset"]["validation"]["coinbase"]["assets"]["BTC"]["candles"] >= 310
