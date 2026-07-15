"""Pure technical-indicator math on the underlying crypto price.

Detection-only building blocks for the 15-minute crypto strategy: given a
series of 1-minute closing prices (oldest→newest), compute MACD / RSI and a
stateless "cross" flag. NOTHING here touches the network, the DB, or an
order — `crypto15m` fetches the candles and feeds them in; the recorder logs
the outputs alongside the settled outcome so `backtest.py` can measure
whether they add edge BEFORE they ever gate a trade.

Everything is plain-list arithmetic (no numpy) so it stays unit-testable and
adds nothing to the frozen PyInstaller bundle. Each function returns ``None``
when there isn't enough history to compute a stable value, so callers can
treat "not enough candles yet" the same as any other missing field.

Periods are the textbook MACD(12, 26, 9) / RSI(14). The video that prompted
this used a bespoke MACD(3, 15, 3); we deliberately do NOT expose the periods
as knobs — the user gates on the OUTPUT field (e.g. ``macdHist > 0``), and
hand-tuned periods are exactly the overfitting trap the recorder+backtest
exist to catch. Standard periods keep the field honest.
"""
from __future__ import annotations

from typing import Optional

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14
MA_FAST_EMA = 12
MA_SLOW_SMA = 20


def _floats(values) -> list[float]:
    out: list[float] = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:
            out.append(f)
    return out


def ema_series(values: list[float], period: int) -> list[float]:
    """Exponential moving average series, seeded with the SMA of the first
    `period` points (the conventional EMA seed). Returns one EMA value per
    input point from index `period-1` onward (so it's shorter than `values`
    by `period-1`). Empty if there isn't a full first window."""
    n = len(values)
    if period <= 0 or n < period:
        return []
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [seed]
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1.0 - k)
        out.append(prev)
    return out


def ema_latest(values, period: int) -> Optional[float]:
    vals = _floats(values)
    series = ema_series(vals, int(period))
    return round(series[-1], 6) if series else None


def sma(values, period: int) -> Optional[float]:
    vals = _floats(values)
    try:
        n = int(period)
    except (TypeError, ValueError):
        return None
    if n <= 0 or len(vals) < n:
        return None
    return round(sum(vals[-n:]) / n, 6)


def macd(
    closes: list[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> Optional[dict]:
    """MACD line, signal line, and histogram from a close series.

    macd = EMA(fast) − EMA(slow); signal = EMA(signal) of the macd line;
    hist = macd − signal. Also returns `cross`: +1 if the histogram flipped
    negative→positive on the latest bar (bullish cross), −1 on a bearish
    cross, else 0 — a STATELESS flag the rule engine can test directly
    without tracking prior state. None until there's enough history for a
    stable signal line (slow + signal points)."""
    vals = _floats(closes)
    if len(vals) < slow + signal:
        return None
    ema_fast = ema_series(vals, fast)
    ema_slow = ema_series(vals, slow)
    if not ema_fast or not ema_slow:
        return None
    m = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-m + i] - ema_slow[-m + i] for i in range(m)]
    signal_line = ema_series(macd_line, signal)
    if len(signal_line) < 2:
        return None
    sm = min(len(macd_line), len(signal_line))
    hist = [macd_line[-sm + i] - signal_line[-sm + i] for i in range(sm)]
    last, prev = hist[-1], hist[-2]
    cross = 1 if (prev <= 0 < last) else (-1 if (prev >= 0 > last) else 0)
    return {
        "macd": round(macd_line[-1], 6),
        "signal": round(signal_line[-1], 6),
        "hist": round(last, 6),
        "cross": cross,
    }


def rsi(closes: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    """Wilder's RSI over the close series, in [0, 100]. None until there are
    `period + 1` points. 100 when there are no down-moves in the window
    (all gains), 0 when there are no up-moves."""
    vals = _floats(closes)
    if len(vals) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        ch = vals[i] - vals[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(vals)):
        ch = vals[i] - vals[i - 1]
        gain = ch if ch > 0 else 0.0
        loss = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


SIGMA_WINDOW = 30


def sigma1m(closes: list[float], window: int = SIGMA_WINDOW) -> Optional[float]:
    """Realized 1-minute volatility: sample std-dev of the last `window`
    one-minute simple returns, as a FRACTION per √minute. This is the σ the
    spot-vs-strike settlement model scales by √(minutes left) to judge how
    far the current spot really is from the strike. None until there are
    `window + 1` closes or when the series is degenerate."""
    vals = _floats(closes)
    if len(vals) < window + 1:
        return None
    rets = []
    for i in range(len(vals) - window, len(vals)):
        prev = vals[i - 1]
        if prev <= 0:
            return None
        rets.append((vals[i] - prev) / prev)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var < 0:
        return None
    return var ** 0.5


def compute(closes) -> dict:
    """Convenience bundle for the snapshot: every indicator field at once,
    each None when there isn't enough history. Keys mirror the snapshot /
    rule-builder vocabulary (macdHist, macdCross, rsi, …)."""
    vals = _floats(closes)
    m = macd(vals)
    return {
        "macd": m["macd"] if m else None,
        "macdSignal": m["signal"] if m else None,
        "macdHist": m["hist"] if m else None,
        "macdCross": m["cross"] if m else None,
        "rsi": rsi(vals),
        "sigma1m": sigma1m(vals),
        "ema12_1m": ema_latest(vals, MA_FAST_EMA),
        "sma20_1m": sma(vals, MA_SLOW_SMA),
    }
