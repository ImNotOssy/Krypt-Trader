"""High-frequency 15m book+spot recorder — the exit-latency study feed.

The 25s REST recorder proved the intra-window bid overshoot is real and large
(+24c/ct oracle ceiling) but unharvestable at that cadence: the bid spikes and
the book snaps back inside the 4-25s tick gap. This recorder samples the LIVE
in-memory WebSocket state instead — Kalshi ticker quotes (kalshi_ws) + the
underlying spot (spot_ws Coinbase proxy, cf_ws settlement index fallback) — at
up to ~5Hz, writing a lean row on every change. Re-running the oracle-vs-
realizable study over this data at 1s / 250ms downsampling vs the 25s baseline
answers the one open question: how much of the +24c returns as exit latency
shrinks.

STRICT ACCELERATOR, same contract as the WS clients it reads: it only records
what the sockets already hold, never places orders, is OFF by default
(`crypto15m_hf_record`), and is retention-capped hard (_C15_HF_KEEP_DAYS = 7)
so an accidentally-left-on feed can't balloon the DB. All writes go through a
dedicated connection with a short busy-timeout; a locked DB drops the batch
rather than stalling the sampler (research data, non-critical).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

import cf_ws
import crypto15m
import db
import kalshi_auth
import kalshi_ws
import spot_ws

logger = logging.getLogger("crypto15m_hf")

_META_REFRESH_SEC = 2.0    # how often to re-read active markets + resubscribe
_FLUSH_SEC = 1.0           # max time a buffered row waits before it's written
_FLUSH_ROWS = 200          # or flush early once this many rows are buffered
_HEARTBEAT_MS = 5000       # write an anchor row this often even without a change


def _parse_close_epoch(close_time) -> Optional[float]:
    if not close_time:
        return None
    return crypto15m._parse_close_epoch(str(close_time))


class _Recorder:
    def __init__(self) -> None:
        self._conn: Optional[sqlite3.Connection] = None
        self._buf: list[dict] = []
        # per-ticker dedup: last written (yes_bid, yes_ask, spot_rounded) + when
        self._last: dict[str, tuple] = {}
        self._last_write_ms: dict[str, int] = {}
        self._meta: dict[str, dict] = {}   # ticker -> {asset, close_epoch}
        self._meta_at: float = 0.0
        self._last_flush: float = 0.0
        self.samples: int = 0
        self.written: int = 0
        self.last_error: Optional[str] = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(db.db_path()), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=3000")
            self._conn = conn
        return self._conn

    async def _refresh_meta(self, cfg: dict) -> None:
        # Keep the ~3s snapshot cache warm (populates active_tickers) and make
        # sure the feeds we read are actually up — the HF recorder can be the
        # ONLY thing running (15m trading off), so it can't assume the executor
        # loop started spot_ws / the cf channel or subscribed the tickers.
        try:
            await crypto15m.snapshot(cfg)
        except Exception as e:
            logger.debug(f"hf meta snapshot failed: {e}")
        if bool(cfg.get("crypto15m_spot_ws", True)):
            kalshi_ws.set_cf_enabled(True)
            if not spot_ws.is_running():
                spot_ws.start()
        meta: dict[str, dict] = {}
        for m in crypto15m.active_market_meta():
            tk = m.get("ticker")
            if not tk:
                continue
            meta[tk] = {
                "asset": (m.get("asset") or "").upper(),
                "close_epoch": _parse_close_epoch(m.get("closeTime")),
            }
        self._meta = meta
        # Ensure our markets are subscribed WITHOUT clobbering the main loop's
        # baseline (which owns set_ticker_markets and includes held tickers).
        if meta and kalshi_ws.is_connected():
            try:
                kalshi_ws.add_ticker_markets(set(meta))
            except Exception as e:
                logger.debug(f"hf resubscribe failed: {e}")

    def _spot(self, asset: str) -> tuple[Optional[float], Optional[str]]:
        v = spot_ws.spot(asset)
        if v is not None:
            return v, "coinbase_ws"
        v = cf_ws.spot(asset)
        if v is not None:
            return v, "cf_ws"
        return None, None

    async def sample(self, cfg: dict) -> None:
        now = time.time()
        if now - self._meta_at >= _META_REFRESH_SEC:
            await self._refresh_meta(cfg)
            self._meta_at = now
        if not self._meta:
            return
        env = kalshi_auth.get_env()
        recv_ms = int(now * 1000)
        for tk, meta in self._meta.items():
            q = kalshi_ws.ticker_quote(tk)
            if not q:
                continue
            yb, ya = q.get("yes_bid_cents"), q.get("yes_ask_cents")
            spot, src = self._spot(meta["asset"])
            spot_q = round(spot, 2) if spot is not None else None
            key = (yb, ya, spot_q)
            hb = (recv_ms - self._last_write_ms.get(tk, 0)) >= _HEARTBEAT_MS
            if self._last.get(tk) == key and not hb:
                continue  # nothing moved and no heartbeat due
            self._last[tk] = key
            self._last_write_ms[tk] = recv_ms
            ce = meta.get("close_epoch")
            self._buf.append({
                "ticker": tk, "asset": meta["asset"], "recv_ms": recv_ms,
                "quote_ts_ms": q.get("ts_ms") or None,
                "mins_left": ((ce - now) / 60.0) if ce else None,
                "yes_bid": yb, "yes_ask": ya,
                "spot": spot, "spot_ms": recv_ms if spot is not None else None,
                "spot_source": src, "kalshi_env": env,
            })
        self.samples += 1
        if len(self._buf) >= _FLUSH_ROWS or (now - self._last_flush) >= _FLUSH_SEC:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            self._last_flush = time.time()
            return
        batch, self._buf = self._buf, []
        try:
            conn = self._db()
            db.insert_crypto15m_hf_ticks(conn, batch)
            conn.commit()
            self.written += len(batch)
            self.last_error = None
        except sqlite3.OperationalError as e:
            # Locked/contended — drop the batch rather than stall the sampler.
            self.last_error = str(e)
            logger.debug(f"hf flush dropped {len(batch)} rows: {e}")
        finally:
            self._last_flush = time.time()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def stats(self) -> dict:
        return {
            "markets": len(self._meta),
            "buffered": len(self._buf),
            "samples": self.samples,
            "written": self.written,
            "lastError": self.last_error,
        }


_recorder = _Recorder()


async def sample(cfg: dict) -> None:
    await _recorder.sample(cfg)


def flush() -> None:
    _recorder.flush()


def close() -> None:
    _recorder.close()


def stats() -> dict:
    return _recorder.stats()
