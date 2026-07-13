"""Unit tests for the CaptureTrail trailing-exit engine.

The engine reasons in "favorable mark" space (higher = better). Binary callers
pass the held-side probability; perps callers pass price (long) or its mirror
(short). These tests exercise the mechanics on raw marks — the domain mapping
is tested where it's applied (favorable_mark + the backtests).
"""
from __future__ import annotations

import capturetrail as ct


def _run(marks, p, entry=None):
    """Feed a mark series through a fresh state; return (exit_index, reason,
    state) where exit_index is the first tick that fired (or None)."""
    st = ct.CTState.open(entry if entry is not None else marks[0])
    for i, m in enumerate(marks):
        done, reason = ct.step(st, m, p)
        if done:
            return i, reason, st
    return None, "", st


def test_disabled_never_fires():
    p = ct.CTParams(enabled=False, reversal_pct=0.05)
    i, _, _ = _run([1.0, 2.0, 1.0, 0.5], p)
    assert i is None


def test_all_zero_triggers_is_inactive():
    p = ct.CTParams(enabled=True)
    assert not p.active()
    i, _, _ = _run([1.0, 2.0, 0.1], p)
    assert i is None


def test_peak_tracking_and_trailing_exit():
    # Arm immediately (no min_arm), trail 10% off the peak. Peak reaches 2.0 at
    # index 2; a 10% give-back means exit at/below 1.80.
    p = ct.CTParams(enabled=True, reversal_pct=0.10)
    marks = [1.00, 1.50, 2.00, 1.95, 1.85, 1.79]
    i, reason, st = _run(marks, p, entry=1.00)
    assert reason == "ct_trail"
    assert i == 5  # 1.79 is 10.5% below the 2.00 peak
    assert st.peak_mark == 2.00
    assert st.closed is True


def test_no_exit_while_within_reversal_band():
    p = ct.CTParams(enabled=True, reversal_pct=0.10)
    # Never gives back more than ~5% from the running peak.
    i, _, _ = _run([1.0, 1.1, 1.2, 1.15, 1.25, 1.20], p, entry=1.0)
    assert i is None


def test_arm_latch_requires_min_profit():
    # Needs peak +5% to arm. Peak only reaches +3% -> never armed -> trail can't
    # fire, and with no unarmed stop it holds.
    p = ct.CTParams(enabled=True, min_arm_pct=0.05, reversal_pct=0.02)
    i, _, st = _run([1.00, 1.02, 1.03, 1.00, 0.98], p, entry=1.00)
    assert i is None
    assert st.armed is False


def test_arm_then_trail():
    # Peak +6% arms it; then a 2% give-back off peak fires the trail.
    p = ct.CTParams(enabled=True, min_arm_pct=0.05, reversal_pct=0.02)
    marks = [1.00, 1.06, 1.05, 1.038]  # 1.038 is ~2.1% below the 1.06 peak
    i, reason, st = _run(marks, p, entry=1.00)
    assert st.armed is True
    assert reason == "ct_trail"
    assert i == 3


def test_unarmed_stop_caps_early_loss():
    # Before arming, a 4% drop from entry trips the protective stop.
    p = ct.CTParams(enabled=True, min_arm_pct=0.10, unarmed_stop_pct=0.04,
                    reversal_pct=0.02)
    i, reason, _ = _run([1.00, 0.99, 0.955], p, entry=1.00)
    assert reason == "ct_unarmed_stop"
    assert i == 2


def test_armed_position_ignores_unarmed_stop():
    # Once armed, the trail governs — the unarmed stop no longer applies, so a
    # dip below entry only exits if it's a big enough give-back from the peak.
    p = ct.CTParams(enabled=True, min_arm_pct=0.03, unarmed_stop_pct=0.01,
                    reversal_pct=0.50)
    # Arm at +4%, then fall to 0.98 (below entry) — that's only ~5.8% below the
    # 1.04 peak, under the 50% reversal band, so it holds despite the tiny
    # unarmed stop that would have fired pre-arm.
    i, _, st = _run([1.00, 1.04, 0.98], p, entry=1.00)
    assert st.armed is True
    assert i is None


def test_noise_band_suppresses_tiny_giveback():
    # A 3% reversal would normally fire, but the noise band (5% of entry)
    # swallows give-backs under 0.05 in mark units.
    p = ct.CTParams(enabled=True, reversal_pct=0.03, noise_pct=0.05)
    # Peak 1.02, dip to 0.985: give-back 0.035 < noise floor 0.05 -> hold, even
    # though 0.035/1.02 = 3.4% exceeds the reversal band.
    i, _, _ = _run([1.00, 1.02, 0.985], p, entry=1.00)
    assert i is None


def test_sticky_close_is_idempotent():
    p = ct.CTParams(enabled=True, reversal_pct=0.10)
    st = ct.CTState.open(1.00)
    # Drive it to an exit.
    ct.step(st, 2.00, p)
    done, _ = ct.step(st, 1.70, p)
    assert done and st.closed
    # Any further steps — even a recovery back above peak — stay closed.
    for m in (2.50, 3.00, 0.10):
        again, reason = ct.step(st, m, p)
        assert again is False and reason == ""


def test_short_mirror_via_favorable_mark():
    # Perps short: gains as price falls. favorable_mark mirrors about entry.
    entry = 100.0
    prices = [100.0, 98.0, 96.0, 97.0, 97.5]  # falls to 96 (peak gain), rebounds
    marks = [ct.favorable_mark("short", px, entry) for px in prices]
    assert marks[2] > marks[0]  # 96 is favorable for a short
    p = ct.CTParams(enabled=True, reversal_pct=0.01)  # 1% trail
    i, reason, _ = _run(marks, p, entry=ct.favorable_mark("short", entry, entry))
    # Peak favorable mark at price 96 (mark 104); a rebound to 97.5 (mark 102.5)
    # is a 1.44% give-back from the 104 peak -> trail fires.
    assert reason == "ct_trail"
    assert i == 4


def test_params_from_cfg_reads_prefixed_block():
    cfg = {
        "crypto15m_ct_enabled": True,
        "crypto15m_ct_min_arm_pct": 0.03,
        "crypto15m_ct_unarmed_stop_pct": 0.15,
        "crypto15m_ct_reversal_pct": 0.055,
        "crypto15m_ct_noise_pct": 0.01,
        "crypto15m_ct_override": True,
    }
    p = ct.params_from_cfg(cfg, "crypto15m")
    assert p.enabled and p.override
    assert p.min_arm_pct == 0.03
    assert p.unarmed_stop_pct == 0.15
    assert p.reversal_pct == 0.055
    assert p.noise_pct == 0.01
    assert p.active()


def test_params_from_cfg_defaults_disabled():
    p = ct.params_from_cfg({}, "perps_strat")
    assert not p.enabled
    assert not p.active()
