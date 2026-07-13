"""Exit-latency sensitivity study over the high-frequency 15m feed.

The 25s REST study left one question open: the intra-window bid overshoot is
real (+24c/ct oracle ceiling) but every realizable exit collapsed to ~0 because
the overshoot reverts inside the 4-25s tick gap. This module answers "how much
comes back as you get faster" directly: it takes the SAME latency entry (buy
the side spot is moving, mid book, early in the window), then replays the exit
at a range of sampling intervals — from the raw HF cadence up to the old 25s —
and reports, per interval, the settle EV, the best realizable trailing-exit EV,
and the oracle-peak ceiling.

If realizable EV climbs toward the oracle as the interval shrinks, the edge is
a latency game worth an execution investment. If it stays pinned near zero even
at the raw cadence, the overshoot is unharvestable noise regardless of speed.

Reads crypto15m_ticks_hf (populated by crypto15m_hf_record); outcomes join from
crypto15m_signals. Fees are Kalshi per-order (entry + exit); one trade/window.
"""
from __future__ import annotations

from typing import Optional

import backtest as bt
import capturetrail as ct
import db as dbmod

_CONTRACTS = 20
# Same latency/momentum entry as the 25s study A: mid-priced, early, spot-side.
_MID_LO, _MID_HI = 0.30, 0.70
_MIN_MINS_LEFT = 2.0
# Realizable trailing-exit grid searched per interval (arm, unarmed-stop, rev).
_CT_GRID = [
    (0.0, 0.0, 0.05), (0.0, 0.0, 0.10), (0.10, 0.25, 0.10),
    (0.10, 0.0, 0.18), (0.25, 0.0, 0.25), (0.10, 0.45, 0.35),
]


def _held(side: str, yb: Optional[int], ya: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    """(entry_ask, exit_bid) in dollars for the held side, from cents quotes.
    up buys/sells YES; down buys/sells NO = 100-yes complement."""
    if yb is None or ya is None:
        return None, None
    yb, ya = float(yb), float(ya)
    if not (0 < yb <= ya < 100):
        return None, None
    if side == "up":
        return ya / 100.0, yb / 100.0          # pay yes_ask, sell yes_bid
    return (100 - yb) / 100.0, (100 - ya) / 100.0  # pay no_ask, sell no_bid


def _downsample(samples: list[dict], interval_ms: int) -> list[dict]:
    if interval_ms <= 0:
        return samples
    out: list[dict] = []
    last = None
    for s in samples:
        if last is None or s["recv_ms"] - last >= interval_ms:
            out.append(s)
            last = s["recv_ms"]
    return out


def _eval_interval(windows: list[dict], interval_ms: int) -> dict:
    """One trade per window at the given exit-sampling interval. Returns settle
    / best-realizable-CT / oracle EV per contract (cents) + win rates."""
    settle_tot = oracle_tot = 0.0
    ct_tot = {g: 0.0 for g in _CT_GRID}
    n = wins = 0
    for w in windows:
        samples = _downsample(w["samples"], interval_ms)
        # entry: first sample mid-priced + early enough
        entry_i = None
        for i, s in enumerate(samples):
            mid = s["mid"]
            if mid is None or s["mins_left"] is None:
                continue
            if _MID_LO <= mid <= _MID_HI and s["mins_left"] >= _MIN_MINS_LEFT:
                entry_i = i
                break
        if entry_i is None:
            continue
        e = samples[entry_i]
        # side = spot momentum vs the window's first observed spot (causal)
        if e["spot"] is None or w["first_spot"] is None:
            continue
        side = "up" if e["spot"] >= w["first_spot"] else "down"
        ask, _ = _held(side, e["yes_bid"], e["yes_ask"])
        if ask is None:
            continue
        won = w["up_won"] if side == "up" else (1 - w["up_won"])
        fin = bt.kalshi_fee_per_contract(ask, contracts=_CONTRACTS)
        n += 1
        wins += won

        # forward held-bid path (post-entry)
        path = []
        for s in samples[entry_i + 1:]:
            _, bid = _held(side, s["yes_bid"], s["yes_ask"])
            if bid is not None:
                path.append(bid)

        s_pnl = (1.0 - ask - fin) if won else (-ask - fin)
        settle_tot += s_pnl
        # oracle: sell at the exact peak bid reachable at THIS interval
        peak = max([ask] + path)
        oracle_tot += peak - ask - fin - bt.kalshi_fee_per_contract(peak, contracts=_CONTRACTS)
        # realizable trailing exits
        for (arm, stop, rev) in _CT_GRID:
            P = ct.CTParams(enabled=True, min_arm_pct=arm, unarmed_stop_pct=stop, reversal_pct=rev)
            st = ct.CTState.open(ask)
            exit_px = None
            for bid in path:
                done, _ = ct.step(st, bid, P)
                if done:
                    exit_px = bid
                    break
            if exit_px is not None:
                ct_tot[(arm, stop, rev)] += (
                    exit_px - ask - fin - bt.kalshi_fee_per_contract(exit_px, contracts=_CONTRACTS))
            else:
                ct_tot[(arm, stop, rev)] += s_pnl

    if n == 0:
        return {"intervalMs": interval_ms, "n": 0}
    best_g = max(_CT_GRID, key=lambda g: ct_tot[g])
    return {
        "intervalMs": interval_ms, "n": n, "winRate": round(wins / n, 3),
        "settleEvCents": round(settle_tot / n * 100, 2),
        "bestRealizableEvCents": round(ct_tot[best_g] / n * 100, 2),
        "bestParams": {"minArmPct": best_g[0], "unarmedStopPct": best_g[1], "reversalPct": best_g[2]},
        "oracleEvCents": round(oracle_tot / n * 100, 2),
    }


def latency_study(*, env: str = "production", since_hours: int = 72,
                  intervals_ms: Optional[list[int]] = None) -> dict:
    """Load the HF feed for resolved windows and sweep exit-sampling intervals.

    intervals_ms default: [0 (raw), 250, 1000, 5000, 25000] — 0 means the full
    recorded cadence (no downsample). Returns one row per interval plus the
    honesty caveats."""
    if intervals_ms is None:
        intervals_ms = [0, 250, 1000, 5000, 25000]
    with dbmod.get_db() as conn:
        rows = conn.execute(
            """SELECT h.ticker, h.asset, h.recv_ms, h.mins_left,
                      h.yes_bid, h.yes_ask, h.spot, s.up_won
               FROM crypto15m_ticks_hf h
               JOIN crypto15m_signals s
                 ON s.ticker = h.ticker AND s.kalshi_env = h.kalshi_env
               WHERE s.resolved = 1 AND s.up_won IS NOT NULL
                 AND h.kalshi_env = ?
                 AND h.recv_ms >= ?
               ORDER BY h.ticker, h.recv_ms""",
            (env, int((_now_ms() - since_hours * 3600 * 1000))),
        ).fetchall()

    byw: dict[str, dict] = {}
    for r in rows:
        w = byw.get(r["ticker"])
        if w is None:
            w = byw[r["ticker"]] = {
                "up_won": int(r["up_won"]), "first_spot": None, "samples": [],
            }
        mid = None
        if r["yes_bid"] is not None and r["yes_ask"] is not None:
            mid = (float(r["yes_bid"]) + float(r["yes_ask"])) / 200.0  # cents->prob
        if w["first_spot"] is None and r["spot"] is not None:
            w["first_spot"] = float(r["spot"])
        w["samples"].append({
            "recv_ms": int(r["recv_ms"]), "mins_left": r["mins_left"],
            "yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"],
            "spot": float(r["spot"]) if r["spot"] is not None else None,
            "mid": mid,
        })

    windows = list(byw.values())
    results = [_eval_interval(windows, iv) for iv in intervals_ms]
    med_gap = _median_sample_gap_ms(windows)
    caveats = [
        "Entry = spot-momentum side, mid book (0.30-0.70), >=2min left — the "
        "same latency entry as the 25s study; it has ~0 hold-to-settlement edge.",
        "Exit books at the held-side BID with an entry AND an exit fee. The "
        "oracle sells at the exact peak bid reachable at that interval (an "
        "unreachable ceiling, not a strategy).",
        "Downsampling only THINS the recorded stream — it can model slower "
        "reaction than the feed, never faster. The 0ms row is the true ceiling "
        "of this data's cadence.",
        f"Median HF sample gap: {med_gap}ms across {len(windows)} windows. "
        "Recorded during app uptime only; in-sample.",
    ]
    if len(windows) < 20:
        caveats.insert(0, f"Only {len(windows)} windows of HF data — anecdote, "
                          "not a verdict. Let the recorder run longer.")
    return {"env": env, "windows": len(windows), "byInterval": results,
            "medianSampleGapMs": med_gap, "caveats": caveats}


def _now_ms() -> int:
    # Isolated so tests can hold time still if needed; DB rows carry recv_ms.
    import time
    return int(time.time() * 1000)


def _median_sample_gap_ms(windows: list[dict]) -> Optional[int]:
    gaps = []
    for w in windows:
        ss = w["samples"]
        for a, b in zip(ss, ss[1:]):
            gaps.append(b["recv_ms"] - a["recv_ms"])
    if not gaps:
        return None
    gaps.sort()
    return int(gaps[len(gaps) // 2])
