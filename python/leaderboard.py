"""Anonymous community leaderboard reporter.

Fire-and-forget. While the bot is running the service loop calls
``maybe_report`` roughly every 30 minutes; it posts an ANONYMOUS snapshot to
the Krypt community leaderboard Discord webhooks:

  * the current P&L (the same numbers the dashboard renders), and
  * the strategy *profile* — the live config with every secret / identifying
    field stripped out (no API key, RSA key, or the user's own webhook URLs
    ever leaves the machine).

There is NO per-install id — every report is just "a user", so reports can't be
linked across time or to an account. A report is sent ONLY when the account is
up THIS SESSION (session P&L > 0; all-time P&L is ignored); there is no
startup/always-send report. The service loop additionally reports only the
PRODUCTION environment (demo is paper money). This collection is DISCLOSED in
the in-app About → Risk & disclosure and the project Disclaimer, and is opt-out.

Design notes:
  * NEVER raises into the trading loop — every failure is swallowed silently.
  * NOTHING is written to the app Logs page or backend.log (NullHandler + no
    propagation), regardless of the root log level.
  * Four webhook URLs across distinct channels. Each report shuffles them and
    falls through on a 429, so one saturated bucket doesn't drop the sample.
    The service loop also JITTERS the 30-min cadence per client so a large user
    base de-syncs instead of hammering a webhook on the same wall-clock minute.
  * Fully opt-out with the env var ``KRYPT_LEADERBOARD=0`` (or off/false/no).
"""
from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)
# Silent by request: the leaderboard never surfaces in the app Logs page or
# backend.log. NullHandler + no propagation guarantees zero output regardless
# of the root log level — these calls stay in the code only for ad-hoc local
# debugging (temporarily set logger.propagate = True).
logger.addHandler(logging.NullHandler())
logger.propagate = False

# Community leaderboard webhooks — 4 write-only Discord URLs owned by Krypt.
# Each report shuffles the list and fails over on a 429, so aggregate load
# spreads across all of them (Discord allows ~30 msg/min PER webhook — keep
# each in its OWN channel, or webhooks in the same channel share that channel's
# limit). Rotatable/expandable server-side without touching clients. Dedicated
# Kalshi set (separate from the Polymarket bot's); the record's `app` field
# ("krypt-trader") still tags every sample.
_WEBHOOKS = (
    "https://discord.com/api/webhooks/1521395077136777217/vI1As9G4uQPjOXwY-SLmVoXoKB0C5sNK7cIsm9_O2yDVAPCLkoUBeeePJp6OULjn9-lQ",
    "https://discord.com/api/webhooks/1521395132354789436/85U-3fpXEsG1etxwVGYs-9zdHb8ou59tCFpNVMjc4Qo8xpGmAHDSvWWBUhRGIsx5VDGo",
    "https://discord.com/api/webhooks/1521449669547524116/c8Q8FMOYSIAY006gWtk19KCQ800wsFfYGDm8XgclYIWBAzEil0yrXS8R-Zi9GG9EGKKF",
    "https://discord.com/api/webhooks/1521449750489206916/JyPXK34ojHK_wlnAvzs8LwEyY7rR-VDDiOGH_lcuY6j3JGTLHp8bBTJYdX99MRmW7Qjs",
)

# Config keys whose VALUES are secret or identifying — never sent. Matched as
# case-insensitive substrings against each config key name, so this also covers
# the `*_webhook_url` keys.
_SECRET_SUBSTR = (
    "webhook", "url", "key", "secret", "token", "passphrase",
    "wallet", "funder", "address", "private", "seed", "mnemonic", "credential",
)

# Master kill-switch. Default ON; the dev (or a wary user) can silence it
# entirely without a rebuild.
DISABLED = os.environ.get("KRYPT_LEADERBOARD", "1").strip().lower() in (
    "0", "off", "false", "no",
)

# Profitability gate: is_profitable requires a POSITIVE SESSION P&L (all-time is
# ignored). There is NO per-install id and NO startup report — every report is
# anonymous ("a user") and sent only on a green session.

_HTTP_TIMEOUT = 8.0


# ───────── payload construction ───────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_profile(cfg: dict) -> dict:
    """Drop every secret / identifying key, keep the strategy settings."""
    out: dict = {}
    for k, v in (cfg or {}).items():
        if any(s in str(k).lower() for s in _SECRET_SUBSTR):
            continue
        out[k] = v
    return out


def is_profitable(snap: dict) -> bool:
    """Report only an account that's UP THIS SESSION (session P&L > 0). All-time
    P&L is intentionally NOT considered: an account that's green overall but
    losing in the current session must not post — a losing session leaking
    through on a green all-time was the bug this guards against."""
    try:
        total = float(snap.get("totalUsd") or 0)
        session = float(snap.get("sessionPnlUsd") or 0)
    except Exception:
        return False
    return total > 0 and session > 0


def _pnl_block(snap: dict) -> dict:
    def f(k: str) -> float:
        try:
            return float(snap.get(k) or 0)
        except Exception:
            return 0.0

    def i(k: str) -> int:
        try:
            return int(snap.get(k) or 0)
        except Exception:
            return 0

    return {
        "totalUsd": round(f("totalUsd"), 2),
        "cashUsd": round(f("cashUsd"), 2),
        "portfolioUsd": round(f("portfolioUsd"), 2),
        "sessionPnlUsd": round(f("sessionPnlUsd"), 2),
        "sessionRoiPct": round(f("sessionRoiPct"), 2),
        "alltimePnlUsd": round(f("alltimePnlUsd"), 2),
        "roiPct": round(f("roiPct"), 2),
        "todayPnlUsd": round(f("todayPnlUsd"), 2),
        "startBankrollUsd": round(f("startBankrollUsd"), 2),
        "wins": i("wins"),
        "losses": i("losses"),
        "winRate": round(f("winRate"), 1),
        "totalOpened": i("totalOpened"),
        "openCount": i("openCount"),
    }


def _strategy_summary(cfg: dict):
    engines = []
    if cfg.get("trade_whales"):
        engines.append("whale")
    if cfg.get("trade_momentum"):
        engines.append("momentum")
    if cfg.get("trade_convergence"):
        engines.append("convergence")
    if cfg.get("crypto15m_enabled"):
        engines.append("crypto15m")
    if cfg.get("copy_enabled"):
        engines.append("copy")
    rules_on = bool(cfg.get("use_rules") or cfg.get("crypto15m_use_rules"))
    return engines, rules_on


def _fmt_usd(v) -> str:
    v = float(v or 0)
    return f"{'+' if v >= 0 else ''}${v:.2f}"


def build_embed(snap: dict, cfg: dict) -> dict:
    pnl = _pnl_block(snap)
    engines, rules_on = _strategy_summary(cfg)
    # Colour by the run's standing (session P&L, falling back to all-time):
    # green up / red down / grey flat.
    ref = pnl["sessionPnlUsd"] or pnl["alltimePnlUsd"]
    color = 0x22C55E if ref > 0 else 0xEF4444 if ref < 0 else 0x71717A
    return {
        "title": "🏆 Leaderboard update",
        "color": color,
        "timestamp": _now_iso(),
        "description": f"A user · Total **${pnl['totalUsd']:.2f}**",
        "fields": [
            {"name": "Session P&L",
             "value": f"{_fmt_usd(pnl['sessionPnlUsd'])} ({pnl['sessionRoiPct']:+.2f}%)",
             "inline": True},
            {"name": "All-time P&L",
             "value": f"{_fmt_usd(pnl['alltimePnlUsd'])} ({pnl['roiPct']:+.2f}%)",
             "inline": True},
            {"name": "Win rate",
             "value": f"{pnl['winRate']:.1f}% ({pnl['wins']}/{pnl['losses']})",
             "inline": True},
            {"name": "Engines", "value": ", ".join(engines) or "—", "inline": True},
            {"name": "Rules mode", "value": "on" if rules_on else "off", "inline": True},
            {"name": "Trades", "value": str(pnl["totalOpened"]), "inline": True},
        ],
        "footer": {"text": "Krypt Trader"},
    }


# ───────── transport (rate-limit-aware, file-attached profile) ─────────


async def _post_with_failover(payload: dict, file_bytes: bytes) -> bool:
    """POST the embed + attached profile JSON, shuffling primary/backup and
    falling through to the other webhook on a 429 (or any error). Returns True
    on the first 2xx, False if every webhook was exhausted."""
    urls = [u for u in _WEBHOOKS if u]
    random.shuffle(urls)  # spread aggregate load across both buckets
    data = {"payload_json": json.dumps(payload)}
    files = {"files[0]": ("krypt-leaderboard.json", file_bytes, "application/json")}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for url in urls:
            try:
                r = await client.post(url, data=data, files=files)
            except Exception as e:
                logger.debug(f"leaderboard post error: {e}")
                continue
            if r.status_code < 300:
                return True
            if r.status_code == 429:
                logger.debug(f"leaderboard 429 on …{url[-12:]}; trying backup")
                continue
            logger.debug(f"leaderboard HTTP {r.status_code}: {r.text[:120]}")
            continue
    return False


async def maybe_report(snap: dict, cfg: dict, env: str, authed: bool) -> bool:
    """Post the anonymous snapshot (P&L + secret-stripped profile) when the
    account is up THIS SESSION (``is_profitable``: session P&L > 0). No-op
    (returns False) when disabled, not authed, or the session isn't green.
    Anonymous — no per-install id; every report is just "a user". Never raises."""
    if DISABLED or not authed:
        return False
    try:
        if not is_profitable(snap):
            return False
        record = {
            "app": "krypt-trader",
            "env": env,
            "ts": _now_iso(),
            "pnl": _pnl_block(snap),
            "profile": sanitize_profile(cfg),
        }
        try:
            file_bytes = json.dumps(record, default=str, indent=2).encode("utf-8")
        except Exception:
            file_bytes = json.dumps({"pnl": _pnl_block(snap)}).encode("utf-8")
        payload = {"username": "Krypt Leaderboard",
                   "embeds": [build_embed(snap, cfg)]}
        ok = await _post_with_failover(payload, file_bytes)
        if ok:
            logger.debug(
                f"leaderboard reported (session {snap.get('sessionPnlUsd')})"
            )
        return ok
    except Exception as e:
        logger.debug(f"leaderboard report skipped: {e}")
        return False
