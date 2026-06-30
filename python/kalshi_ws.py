"""Kalshi WebSocket client — low-latency real-time market data + account events.

A single persistent, multiplexed connection to Kalshi's WebSocket API
(`/trade-api/ws/v2`) that the rest of the bot reads from to avoid REST polling
on the latency-critical paths. It is a STRICT ACCELERATOR: every consumer falls
back to REST when the socket is down or a given market isn't subscribed yet, so
the bot is never *more* fragile than the pure-REST version — only faster.

Channels (https://docs.kalshi.com/websockets):
  * orderbook_delta — one subscription PER held ticker (clean per-sid `seq`
    stream), maintained into a local book; powers the 15m stop-loss/TP chase
    and limit pricing. On a `seq` gap we drop the book and re-snapshot, and
    `orderbook()` returns None meanwhile so the caller transparently REST-falls-
    back rather than acting on a stale book.
  * ticker — one sub, many markets; latest yes bid/ask/last per market.
  * trade — one sub, ALL markets; a recent-trade ring buffer feeding the
    whale/momentum scanner (no 1000-row REST cap, no missed bursts).
  * fill — account fills, pushed instantly → wakes an immediate order re-poll
    (REST stays the accounting source of truth; WS only removes the latency).
  * market_lifecycle_v2 — `determined`/`settled` → wakes an immediate resolution
    check instead of waiting for the 5-min timer.
  * market_positions — account position deltas (kept for stats; cash balance has
    no WS channel and stays on REST).

Auth: the handshake reuses the REST RSA-PSS signing verbatim — sign
`timestamp + "GET" + "/trade-api/ws/v2"` via ``kalshi_auth.sign_headers`` and
send the KALSHI-ACCESS-* headers on the upgrade. The same clock-drift resync
(``now_ms``) that protects REST protects the handshake.

Wire format note: Kalshi WS payloads are DOLLAR strings (``yes_price_dollars``)
and fixed-point quantity strings (``count_fp``), not 1–99 cents. Prices are
converted to integer cents here so the book matches what REST callers expect.

Opt out entirely with ``KRYPT_KALSHI_WS=0`` (or off/false/no); override the URL
with ``KRYPT_KALSHI_WS_URL``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from typing import Awaitable, Callable, Optional

import kalshi_auth

logger = logging.getLogger("kalshi_ws")

try:
    import websockets  # type: ignore
    _WS_IMPORT_OK = True
except Exception:  # pragma: no cover - websockets missing
    websockets = None  # type: ignore
    _WS_IMPORT_OK = False

# Dedicated WS hosts (recommended by the docs); the legacy app hosts still work.
_WS_BASES = {
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
}
_WS_PATH = "/trade-api/ws/v2"  # the signed path is constant across hosts

_DISABLED = os.environ.get("KRYPT_KALSHI_WS", "1").strip().lower() in (
    "0", "off", "false", "no",
)

# Account-wide channels carry no market filter — subscribed once on connect.
_ACCOUNT_CHANNELS = ("trade", "fill", "market_positions")
# Market-scoped channels managed as one shared subscription (no per-message book
# to keep, so multi-market on a single sid is safe).
_MULTI_CHANNELS = ("ticker", "market_lifecycle_v2")

_TRADE_BUF_MAX = 8000
_RECONNECT_MAX_SEC = 60.0
_SILENT_TIMEOUT_SEC = 30.0  # no message for this long → force a reconnect

FillCb = Callable[[dict], Optional[Awaitable]]
LifecycleCb = Callable[[dict], Optional[Awaitable]]


def _cents(price_dollars) -> Optional[int]:
    try:
        return int(round(float(price_dollars) * 100))
    except (TypeError, ValueError):
        return None


def _fp(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class _Client:
    def __init__(self) -> None:
        self.env: str = "production"
        self.connected: bool = False
        self._stop: bool = False
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._gen: int = 0  # bumped to force a reconnect (e.g. env switch)
        self._id: int = 0
        self._loop_time: Callable[[], float] = lambda: 0.0
        self.last_msg_t: float = 0.0

        # desired vs live subscription state
        self.want_orderbook: set[str] = set()      # tickers needing a live book
        self.want_ticker: set[str] = set()
        self.want_lifecycle: set[str] = set()
        self._ob_sids: dict[str, int] = {}          # ticker -> sid (one sub each)
        self._multi_sids: dict[str, int] = {}       # channel -> sid
        self._multi_have: dict[str, set[str]] = {c: set() for c in _MULTI_CHANNELS}
        self._account_subbed: set[str] = set()
        self._inflight: dict[int, tuple] = {}       # cmd id -> (kind, key)

        # market-data state
        self.books: dict[str, dict[str, dict[int, float]]] = {}
        self._book_seq: dict[str, int] = {}
        self._book_valid: dict[str, bool] = {}
        self._resnap: set[str] = set()              # tickers needing a fresh snapshot
        self.quotes: dict[str, dict] = {}
        self.trades: deque = deque(maxlen=_TRADE_BUF_MAX)

        self.on_fill: Optional[FillCb] = None
        self.on_lifecycle: Optional[LifecycleCb] = None

    # ───────── lifecycle ─────────────────────────────────────────────

    def start(self, env: str, *, on_fill=None, on_lifecycle=None) -> None:
        if _DISABLED or not _WS_IMPORT_OK:
            if not _WS_IMPORT_OK and not _DISABLED:
                logger.warning("kalshi_ws: `websockets` not installed — staying on REST")
            return
        self.env = env if env in _WS_BASES else "production"
        self.on_fill = on_fill
        self.on_lifecycle = on_lifecycle
        self._stop = False
        loop = asyncio.get_event_loop()
        self._loop_time = loop.time
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="kalshi_ws")
            logger.info(f"kalshi_ws: starting ({self.env})")

    async def stop(self) -> None:
        self._stop = True
        self._gen += 1
        await self._close_ws()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.connected = False

    def set_env(self, env: str) -> None:
        env = env if env in _WS_BASES else "production"
        if env != self.env:
            self.env = env
            self._gen += 1  # force reconnect to the new host
            logger.info(f"kalshi_ws: env → {env}, reconnecting")

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ───────── connection loop ───────────────────────────────────────

    async def _run(self) -> None:
        attempt = 0
        while not self._stop:
            gen = self._gen
            try:
                await self._connect_once(gen)
                attempt = 0  # clean exit (gen bump) → reconnect immediately
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                backoff = min(2.0 ** attempt, _RECONNECT_MAX_SEC)
                logger.warning(
                    f"kalshi_ws: disconnected ({type(e).__name__}: {e}); "
                    f"reconnect in {backoff:.0f}s"
                )
                self.connected = False
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise

    async def _connect_once(self, gen: int) -> None:
        if not kalshi_auth.credentials_present(self.env):
            # No creds for this env yet — wait and let a later tick retry.
            await asyncio.sleep(5)
            return
        url = os.environ.get("KRYPT_KALSHI_WS_URL") or _WS_BASES[self.env]
        headers = kalshi_auth.sign_headers("GET", _WS_PATH)
        kwargs = dict(ping_interval=10, ping_timeout=10, close_timeout=5,
                      max_size=2 ** 23)
        # websockets >=12 uses additional_headers; older uses extra_headers.
        try:
            conn = websockets.connect(url, additional_headers=headers, **kwargs)
        except TypeError:
            conn = websockets.connect(url, extra_headers=headers, **kwargs)

        async with conn as ws:
            self._ws = ws
            self.connected = True
            self.last_msg_t = self._loop_time()
            self._reset_sub_state()
            logger.info(f"kalshi_ws: connected → {url}")
            await self._subscribe_account(ws)
            await self._reconcile(ws)
            while not self._stop and gen == self._gen:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    # idle tick: reconcile desired subs + watchdog the link
                    await self._reconcile(ws)
                    if self._loop_time() - self.last_msg_t > _SILENT_TIMEOUT_SEC:
                        logger.warning("kalshi_ws: silent link — forcing reconnect")
                        return
                    continue
                self.last_msg_t = self._loop_time()
                try:
                    self._handle(json.loads(raw))
                except Exception as e:
                    logger.debug(f"kalshi_ws: handle error: {e}")
        self.connected = False
        self._ws = None

    def _reset_sub_state(self) -> None:
        # Subscriptions never survive a reconnect — clear live state; the books
        # are invalidated so reads REST-fall-back until fresh snapshots arrive.
        self._ob_sids.clear()
        self._multi_sids.clear()
        for c in _MULTI_CHANNELS:
            self._multi_have[c] = set()
        self._account_subbed.clear()
        self._inflight.clear()
        for t in list(self._book_valid):
            self._book_valid[t] = False
        self._resnap.clear()

    # ───────── outbound commands ──────────────────────────────────────

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _send(self, ws, obj: dict) -> None:
        await ws.send(json.dumps(obj))

    async def _subscribe_account(self, ws) -> None:
        for ch in _ACCOUNT_CHANNELS:
            if ch in self._account_subbed:
                continue
            cid = self._next_id()
            self._inflight[cid] = ("account", ch)
            await self._send(ws, {"id": cid, "cmd": "subscribe",
                                  "params": {"channels": [ch]}})
            self._account_subbed.add(ch)

    async def _reconcile(self, ws) -> None:
        """Bring live subscriptions in line with the desired market sets."""
        # orderbook_delta: one subscription per ticker (clean per-sid seq).
        for t in self.want_orderbook - set(self._ob_sids):
            cid = self._next_id()
            self._inflight[cid] = ("ob", t)
            await self._send(ws, {"id": cid, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta"],
                                             "market_ticker": t}})
            self._ob_sids[t] = -1  # reserved; real sid filled on ack
        for t in set(self._ob_sids) - self.want_orderbook:
            sid = self._ob_sids.pop(t)
            self._drop_book(t)
            if sid and sid > 0:
                cid = self._next_id()
                await self._send(ws, {"id": cid, "cmd": "unsubscribe",
                                      "params": {"sids": [sid]}})
        # re-snapshot any gapped books
        if self._resnap:
            for t in list(self._resnap):
                sid = self._ob_sids.get(t)
                if sid and sid > 0:
                    cid = self._next_id()
                    await self._send(ws, {"id": cid, "cmd": "update_subscription",
                                          "params": {"sids": [sid],
                                                     "market_tickers": [t],
                                                     "action": "get_snapshot"}})
                    self._resnap.discard(t)
        # ticker / lifecycle: one shared sub each, kept in sync via add/remove.
        await self._reconcile_multi(ws, "ticker", self.want_ticker)
        await self._reconcile_multi(ws, "market_lifecycle_v2", self.want_lifecycle)

    async def _reconcile_multi(self, ws, channel: str, want: set[str]) -> None:
        have = self._multi_have[channel]
        sid = self._multi_sids.get(channel)
        if not want:
            if sid:
                cid = self._next_id()
                await self._send(ws, {"id": cid, "cmd": "unsubscribe",
                                      "params": {"sids": [sid]}})
                self._multi_sids.pop(channel, None)
                self._multi_have[channel] = set()
            return
        if sid is None:
            cid = self._next_id()
            self._inflight[cid] = ("multi", channel)
            await self._send(ws, {"id": cid, "cmd": "subscribe",
                                  "params": {"channels": [channel],
                                             "market_tickers": sorted(want)}})
            self._multi_have[channel] = set(want)
            return
        add = sorted(want - have)
        rem = sorted(have - want)
        if add:
            cid = self._next_id()
            await self._send(ws, {"id": cid, "cmd": "update_subscription",
                                  "params": {"sids": [sid], "market_tickers": add,
                                             "action": "add_markets"}})
        if rem:
            cid = self._next_id()
            await self._send(ws, {"id": cid, "cmd": "update_subscription",
                                  "params": {"sids": [sid], "market_tickers": rem,
                                             "action": "delete_markets"}})
        if add or rem:
            self._multi_have[channel] = set(want)

    # ───────── inbound dispatch ───────────────────────────────────────

    def _handle(self, m: dict) -> None:
        t = m.get("type")
        if t == "subscribed":
            self._on_subscribed(m)
        elif t == "orderbook_snapshot":
            self._on_snapshot(m)
        elif t == "orderbook_delta":
            self._on_delta(m)
        elif t == "ticker":
            self._on_ticker(m)
        elif t == "trade":
            self._on_trade(m)
        elif t in ("fill", "user_order"):
            self._on_fill(m)
        elif t == "market_lifecycle_v2":
            self._on_lifecycle(m)
        elif t == "market_position":
            pass  # position deltas — REST reconcile remains the source of truth
        elif t == "error":
            msg = m.get("msg") or {}
            logger.debug(f"kalshi_ws: server error {msg.get('code')}: {msg.get('msg')}")
        # subscribed/ok/unsubscribed acks without a type handled above are no-ops

    def _on_subscribed(self, m: dict) -> None:
        cid = m.get("id")
        sid = (m.get("msg") or {}).get("sid")
        kind_key = self._inflight.pop(cid, None) if cid is not None else None
        if not kind_key or sid is None:
            return
        kind, key = kind_key
        if kind == "ob":
            self._ob_sids[key] = sid
        elif kind == "multi":
            self._multi_sids[key] = sid

    def _drop_book(self, t: str) -> None:
        self.books.pop(t, None)
        self._book_seq.pop(t, None)
        self._book_valid.pop(t, None)

    def _on_snapshot(self, m: dict) -> None:
        msg = m.get("msg") or {}
        t = msg.get("market_ticker")
        if not t:
            return
        book = {"yes": {}, "no": {}}
        for side, key in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
            for lvl in msg.get(key) or []:
                try:
                    c = _cents(lvl[0])
                    if c is not None:
                        book[side][c] = _fp(lvl[1])
                except (TypeError, IndexError):
                    continue
        self.books[t] = book
        self._book_seq[t] = int(m.get("seq") or 0)
        self._book_valid[t] = True

    def _on_delta(self, m: dict) -> None:
        msg = m.get("msg") or {}
        t = msg.get("market_ticker")
        if not t or t not in self.books:
            return  # no baseline snapshot yet — ignore until one arrives
        seq = int(m.get("seq") or 0)
        prev = self._book_seq.get(t, 0)
        if seq <= prev:
            return  # duplicate / stale
        if seq != prev + 1:
            # gap — the book may be wrong; invalidate and request a fresh snapshot
            self._book_valid[t] = False
            self._resnap.add(t)
            logger.debug(f"kalshi_ws: seq gap on {t} ({prev}→{seq}); re-snapshotting")
            return
        side = msg.get("side")
        c = _cents(msg.get("price_dollars"))
        if side in ("yes", "no") and c is not None:
            levels = self.books[t][side]
            levels[c] = levels.get(c, 0.0) + _fp(msg.get("delta_fp"))
            if levels[c] <= 0:
                levels.pop(c, None)
        self._book_seq[t] = seq

    def _on_ticker(self, m: dict) -> None:
        msg = m.get("msg") or {}
        t = msg.get("market_ticker")
        if not t:
            return
        self.quotes[t] = {
            "yes_bid_cents": _cents(msg.get("yes_bid_dollars")),
            "yes_ask_cents": _cents(msg.get("yes_ask_dollars")),
            "last_cents": _cents(msg.get("price_dollars")),
            "volume": _fp(msg.get("volume_fp")),
            "open_interest": _fp(msg.get("open_interest_fp")),
            "ts_ms": msg.get("ts_ms") or 0,
        }

    def _on_trade(self, m: dict) -> None:
        msg = m.get("msg") or {}
        t = msg.get("market_ticker")
        if not t:
            return
        # Match the REST /markets/trades shape the scanner consumes (it already
        # reads count_fp / yes_price_dollars / taker_side). REST calls it `ticker`.
        self.trades.append({
            "trade_id": msg.get("trade_id", ""),
            "ticker": t,
            "count_fp": msg.get("count_fp", "0"),
            "yes_price_dollars": msg.get("yes_price_dollars", "0"),
            "no_price_dollars": msg.get("no_price_dollars", "0"),
            "taker_side": msg.get("taker_side", ""),
            "created_time": msg.get("ts_ms") or msg.get("ts") or "",
        })

    def _on_fill(self, m: dict) -> None:
        if self.on_fill is None:
            return
        try:
            res = self.on_fill(m.get("msg") or {})
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
        except Exception as e:
            logger.debug(f"kalshi_ws: on_fill cb error: {e}")

    def _on_lifecycle(self, m: dict) -> None:
        if self.on_lifecycle is None:
            return
        try:
            res = self.on_lifecycle(m.get("msg") or {})
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
        except Exception as e:
            logger.debug(f"kalshi_ws: on_lifecycle cb error: {e}")

    # ───────── desired-subscription setters (called from the loop) ────

    def set_orderbook_markets(self, tickers) -> None:
        self.want_orderbook = {t for t in tickers if t}

    def set_ticker_markets(self, tickers) -> None:
        self.want_ticker = {t for t in tickers if t}

    def set_lifecycle_markets(self, tickers) -> None:
        self.want_lifecycle = {t for t in tickers if t}

    # ───────── sync read APIs (consumed by kalshi_api / scanner) ──────

    def orderbook(self, ticker: str) -> Optional[dict]:
        """Live book as {"yes":[[cents,size]...],"no":[[cents,size]...]} or None
        when not connected / not subscribed / invalidated by a gap — callers
        then REST-fall-back. Best bid on a side = max price (matches REST)."""
        if not self.connected or not self._book_valid.get(ticker):
            return None
        b = self.books.get(ticker)
        if not b:
            return None
        return {
            "yes": [[c, s] for c, s in b["yes"].items()],
            "no": [[c, s] for c, s in b["no"].items()],
        }

    def best_bid_cents(self, ticker: str, side: str) -> Optional[int]:
        if not self.connected or not self._book_valid.get(ticker):
            return None
        b = self.books.get(ticker)
        if not b or side not in ("yes", "no") or not b[side]:
            return None
        return max(b[side].keys())

    def ticker_quote(self, ticker: str) -> Optional[dict]:
        return self.quotes.get(ticker) if self.connected else None

    def recent_trades(self, limit: int = 1000) -> Optional[list]:
        """Newest-first recent trades in the REST /markets/trades shape, or None
        when the buffer is cold so the scanner REST-falls-back on startup."""
        if not self.connected or not self.trades:
            return None
        out = list(self.trades)[-limit:]
        out.reverse()
        return out

    def stats(self) -> dict:
        return {
            "enabled": not _DISABLED and _WS_IMPORT_OK,
            "connected": self.connected,
            "env": self.env,
            "books": sum(1 for v in self._book_valid.values() if v),
            "orderbookSubs": len(self._ob_sids),
            "tickerCache": len(self.quotes),
            "tradeBuf": len(self.trades),
            "lastMsgAgeSec": round(max(0.0, self._loop_time() - self.last_msg_t), 1)
            if self.connected else None,
        }


_client = _Client()

# Module-level delegators (the rest of the bot imports these).
def start(env: str, *, on_fill=None, on_lifecycle=None) -> None:
    _client.start(env, on_fill=on_fill, on_lifecycle=on_lifecycle)


async def stop() -> None:
    await _client.stop()


def set_env(env: str) -> None:
    _client.set_env(env)


def is_connected() -> bool:
    return _client.connected


def set_orderbook_markets(tickers) -> None:
    _client.set_orderbook_markets(tickers)


def set_ticker_markets(tickers) -> None:
    _client.set_ticker_markets(tickers)


def set_lifecycle_markets(tickers) -> None:
    _client.set_lifecycle_markets(tickers)


def orderbook(ticker: str) -> Optional[dict]:
    return _client.orderbook(ticker)


def best_bid_cents(ticker: str, side: str) -> Optional[int]:
    return _client.best_bid_cents(ticker, side)


def ticker_quote(ticker: str) -> Optional[dict]:
    return _client.ticker_quote(ticker)


def recent_trades(limit: int = 1000) -> Optional[list]:
    return _client.recent_trades(limit)


def stats() -> dict:
    return _client.stats()
