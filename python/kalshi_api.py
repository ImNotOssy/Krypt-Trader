from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional
import uuid

import httpx

import kalshi_ws
from kalshi_auth import sign_headers, get_env, sync_server_time, ENV_LOCK

logger = logging.getLogger(__name__)

PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"

_TRADE_BASES = {
    "demo": "https://demo-api.kalshi.co",
    "production": "https://api.elections.kalshi.com",
}

PATH_PREFIX = "/trade-api/v2"
REQUEST_TIMEOUT = 25.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
# Hot-path budget for latency-critical calls (order place/cancel, order status,
# orderbook): one hung socket must cost seconds, not the bulk 25s — a stop-loss
# waiting out 3 × 25s retries rides a crash for over a minute.
HOT_TIMEOUT = httpx.Timeout(connect=4.0, read=6.0, write=6.0, pool=6.0)
# Keep-alive: httpx's default expiry is 5s, and hot signed calls are often
# spaced further apart than that — so most order placements paid a fresh
# TCP+TLS handshake (~100-300ms). 60s keeps the connection warm between polls.
KEEPALIVE_SEC = 60.0

_pub_client: Optional[httpx.AsyncClient] = None
_signed_client: Optional[httpx.AsyncClient] = None
_signed_env: str = ""


async def _get_pub_client() -> httpx.AsyncClient:
    global _pub_client
    if _pub_client is None or _pub_client.is_closed:
        _pub_client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "KryptTrader/1.0",
            },
            limits=httpx.Limits(
                max_connections=20, max_keepalive_connections=10,
                keepalive_expiry=KEEPALIVE_SEC,
            ),
            follow_redirects=True,
        )
    return _pub_client


async def _get_signed_client() -> httpx.AsyncClient:
    global _signed_client, _signed_env
    env = get_env()
    if (
        _signed_client is None
        or _signed_client.is_closed
        or _signed_env != env
    ):
        if _signed_client is not None and not _signed_client.is_closed:
            try:
                await _signed_client.aclose()
            except Exception:
                pass
        _signed_client = httpx.AsyncClient(
            base_url=_TRADE_BASES[env],
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "KryptTrader/1.0",
            },
            limits=httpx.Limits(
                max_connections=10, max_keepalive_connections=5,
                keepalive_expiry=KEEPALIVE_SEC,
            ),
        )
        _signed_env = env
    return _signed_client


async def close_clients() -> None:
    global _pub_client, _signed_client
    for c in (_pub_client, _signed_client):
        if c and not c.is_closed:
            try:
                await c.aclose()
            except Exception:
                pass
    _pub_client = None
    _signed_client = None


class KalshiAPIError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")




async def _pub_get(url: str, params: dict | None = None) -> Any:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = await _get_pub_client()
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", 5))
                await asyncio.sleep(min(wait, 10))
                continue
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            try:
                return resp.json() if resp.status_code < 400 else None
            except Exception:
                return None
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            logger.warning(f"public GET failed {url}: {e}")
            return None
    return None


async def fetch_markets(
    status: str = "open",
    limit: int = 1000,
    cursor: str = "",
    series_ticker: str = "",
) -> tuple[list, str]:
    params: dict = {"limit": min(limit, 1000)}
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    if series_ticker:
        params["series_ticker"] = series_ticker
    data = await _pub_get(f"{PUBLIC_BASE}/markets", params=params)
    if not isinstance(data, dict):
        return [], ""
    return data.get("markets", []) or [], data.get("cursor", "")


async def fetch_market(ticker: str) -> dict | None:
    data = await _pub_get(f"{PUBLIC_BASE}/markets/{ticker}")
    if not isinstance(data, dict):
        return None
    return data.get("market", data)


async def fetch_markets_map(tickers, concurrency: int = 8) -> dict:
    """Fetch many markets concurrently → {ticker: market}, skipping misses.

    Replaces the serial `for t in tickers: await fetch_market(t)` chains in the
    resolution paths, where one slow endpoint could stall the whole pass
    (N tickers × the per-request timeout). A semaphore caps the in-flight burst
    so even a large unresolved backlog stays friendly to Kalshi's rate limits
    (fetch_market's own retry/backoff still applies per request)."""
    uniq = [t for t in dict.fromkeys(tickers) if t]
    if not uniq:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(tk: str):
        async with sem:
            try:
                return tk, await fetch_market(tk)
            except Exception:
                return tk, None

    pairs = await asyncio.gather(*[_one(t) for t in uniq])
    return {tk: m for tk, m in pairs if m}


async def fetch_all_open_markets(max_pages: int = 10) -> list:
    out: list = []
    cursor = ""
    for _ in range(max_pages):
        m, cursor = await fetch_markets(status="open", limit=1000, cursor=cursor)
        if not m:
            break
        out.extend(m)
        if not cursor:
            break
        await asyncio.sleep(0.25)
    return out


async def fetch_recent_trades(limit: int = 1000) -> list:
    data = await _pub_get(f"{PUBLIC_BASE}/markets/trades", params={"limit": limit})
    if not isinstance(data, dict):
        return []
    return data.get("trades", []) or []


async def fetch_events(
    status: str = "open", limit: int = 200, cursor: str = ""
) -> tuple[list, str]:
    params: dict = {"limit": min(limit, 200)}
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    data = await _pub_get(f"{PUBLIC_BASE}/events", params=params)
    if not isinstance(data, dict):
        return [], ""
    return data.get("events", []) or [], data.get("cursor", "")


_series_cache: dict[str, dict] = {}


async def fetch_series(series_ticker: str) -> dict | None:
    if series_ticker in _series_cache:
        return _series_cache[series_ticker]
    data = await _pub_get(f"{PUBLIC_BASE}/series/{series_ticker}")
    if not isinstance(data, dict):
        return None
    s = data.get("series", data)
    if len(_series_cache) < 5000:
        _series_cache[series_ticker] = s
    return s



_WEB_HOSTS = {
    "production": "kalshi.com",
    "demo": "demo.kalshi.co",
}


def _slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def _event_from_ticker(ticker: str) -> str:
    parts = (ticker or "").split("-")
    if len(parts) >= 3:
        return "-".join(parts[:-1])
    return ticker or ""


async def web_market_url(
    *, event_ticker: str = "", ticker: str = "", env: str = "production",
) -> str:
    event = (event_ticker or "").strip() or _event_from_ticker(ticker)
    base = event or (ticker or "").strip()
    if not base:
        return ""
    series_l = base.split("-")[0].lower()
    host = _WEB_HOSTS.get(env, "kalshi.com")

    slug = ""
    try:
        s = await fetch_series(base.split("-")[0])
        if s:
            slug = _slugify(str(s.get("title", "")))
    except Exception:
        slug = ""

    if slug and event:
        return f"https://{host}/markets/{series_l}/{slug}/{event.lower()}"
    if slug:
        return f"https://{host}/markets/{series_l}/{slug}"
    return f"https://{host}/markets/{series_l}"




async def _signed_request(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: httpx.Timeout | float | None = None,
    pin_env: str | None = None,
) -> Any:
    """Signed request with retries.

    Env safety: the env is PINNED (to `pin_env` when given — read under
    ENV_LOCK by order placement — else to the env seen on the first attempt).
    If a credential test / env switch flips the global env at any point,
    attempts ABORT instead of silently signing for (and sending real money to)
    the other account. Signing + client fetch are back-to-back with no lock held
    across the HTTP call itself, so a slow request or its 429/backoff ladder can
    never wedge other callers (the old design held ENV_LOCK around whole calls —
    one hung balance poll blocked every order placement behind it).
    """
    assert path.startswith("/")
    signed_path = f"{PATH_PREFIX}{path}"
    method = method.upper()
    last_exc: Optional[Exception] = None
    env0: Optional[str] = pin_env

    for attempt in range(1, MAX_RETRIES + 1):
        cur_env = get_env()
        if env0 is None:
            env0 = cur_env
        elif cur_env != env0:
            raise KalshiAPIError(
                409, {"error": {"code": "env_changed",
                                "message": "environment switched mid-request; aborted"}},
            )
        headers = sign_headers(method, signed_path)
        client = await _get_signed_client()
        try:
            resp = await client.request(
                method, signed_path, headers=headers, json=json, params=params,
                **({"timeout": timeout} if timeout is not None else {}),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            raise

        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", 2.0))
            await asyncio.sleep(min(wait, 10))
            continue

        if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue

        try:
            body = resp.json()
        except Exception:
            body = resp.text

        if resp.status_code == 401 and attempt < MAX_RETRIES:
            code = ((body.get("error") or {}).get("code") or "") if isinstance(body, dict) else ""
            if "timestamp" in code:
                # Blocking 5s HTTP HEAD — run it off the event loop so a clock
                # resync can't freeze every other coroutine (incl. exits).
                await asyncio.to_thread(sync_server_time, True)
                await asyncio.sleep(0.2)
                continue

        if resp.status_code >= 400:
            raise KalshiAPIError(resp.status_code, body)
        return body

    if last_exc:
        raise last_exc
    raise RuntimeError("exhausted retries without response")


async def get_balance(pin_env: str | None = None) -> dict:
    return await _signed_request("GET", "/portfolio/balance", pin_env=pin_env)


async def get_positions(
    limit: int = 200, *, settlement_status: str | None = None,
    paginate: bool = True,
) -> list[dict]:
    out: list[dict] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        params: dict = {"limit": int(limit)}
        if settlement_status:
            params["settlement_status"] = settlement_status
        if cursor:
            params["cursor"] = cursor
        data = await _signed_request(
            "GET", "/portfolio/positions", params=params,
        )
        if isinstance(data, dict):
            out.extend(data.get("market_positions", []) or [])
            cursor = data.get("cursor") or None
        else:
            out.extend(data or [])
            cursor = None
        pages += 1
        if not paginate or not cursor or pages >= 25:
            break
    return out


async def get_settled_positions(limit: int = 200) -> list[dict]:
    try:
        data = await _signed_request(
            "GET",
            "/portfolio/positions",
            params={"limit": limit, "settlement_status": "settled"},
        )
    except KalshiAPIError:
        return []
    if isinstance(data, dict):
        return data.get("market_positions", []) or []
    return data or []


async def get_order(order_id: str) -> dict:
    return await _signed_request(
        "GET", f"/portfolio/orders/{order_id}", timeout=HOT_TIMEOUT,
    )


async def find_order_by_client_id(
    client_order_id: str, *, ticker: str = "", limit: int = 200,
) -> dict | None:
    """Look an order up by our client_order_id. Used after a POST whose
    response was lost (timeout / dropped connection): the order may be live on
    Kalshi even though place_limit_order raised, and booking it as 'error'
    would leave an untracked real-money position. Returns the order dict or
    None when no order with that client id exists."""
    params: dict = {"limit": int(limit)}
    if ticker:
        params["ticker"] = ticker
    data = await _signed_request("GET", "/portfolio/orders", params=params)
    orders = (data.get("orders") if isinstance(data, dict) else data) or []
    for o in orders:
        if isinstance(o, dict) and str(o.get("client_order_id") or "") == str(client_order_id):
            return o
    return None


async def get_fills_for_order(order_id: str, limit: int = 200) -> list[dict]:
    out: list[dict] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        params: dict = {"order_id": order_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            data = await _signed_request("GET", "/portfolio/fills", params=params)
        except KalshiAPIError as e:
            if e.status == 404:
                return []
            raise
        chunk = (data.get("fills") if isinstance(data, dict) else data) or []
        out.extend(chunk)
        cursor = data.get("cursor") if isinstance(data, dict) else None
        pages += 1
        if not cursor or pages >= 5:
            break
    return out


async def get_fills_since(after_ts_unix: int, limit: int = 200) -> list[dict]:
    out: list[dict] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        params: dict = {
            "min_ts": int(after_ts_unix),
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = await _signed_request("GET", "/portfolio/fills", params=params)
        except KalshiAPIError:
            return out
        chunk = (data.get("fills") if isinstance(data, dict) else data) or []
        out.extend(chunk)
        cursor = data.get("cursor") if isinstance(data, dict) else None
        pages += 1
        if not cursor or pages >= 5:
            break
    return out


def _normalize_orderbook(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"yes": [], "no": []}
    book = raw.get("orderbook") or raw.get("orderbook_fp") or raw
    out: dict = {"yes": [], "no": []}
    for side in ("yes", "no"):
        levels = book.get(side)
        is_dollars = levels is None
        if is_dollars:
            levels = book.get(f"{side}_dollars") or []
        for lvl in levels or []:
            try:
                price = int(round(float(lvl[0]) * 100)) if is_dollars else int(lvl[0])
                size = float(lvl[1]) if len(lvl) > 1 else 0.0
                out[side].append([price, size])
            except (TypeError, ValueError, IndexError):
                continue
    return out


async def get_orderbook(ticker: str) -> dict:
    # Prefer the real-time WebSocket book when it's live + validated for this
    # market — it's effectively current vs the ~seconds-stale REST snapshot, and
    # is what the 15m stop-loss/TP chase reads. Returns None (→ REST) when the
    # socket is down, the market isn't subscribed, or a seq gap invalidated it.
    wb = kalshi_ws.orderbook(ticker)
    if wb is not None and (wb["yes"] or wb["no"]):
        return wb
    try:
        book = _normalize_orderbook(await _signed_request(
            "GET", f"/markets/{ticker}/orderbook", timeout=HOT_TIMEOUT,
        ))
        if book["yes"] or book["no"]:
            return book
    except Exception:
        pass
    return _normalize_orderbook(await _pub_get(f"{PUBLIC_BASE}/markets/{ticker}/orderbook"))


ORDERS_V2_PATH = "/portfolio/events/orders"


def _v2_order_fields(side: str, action: str, price_cents: int) -> tuple[str, str]:
    if side == "yes":
        book_side = "bid" if action == "buy" else "ask"
        yes_cents = price_cents
    else:
        book_side = "ask" if action == "buy" else "bid"
        yes_cents = 100 - price_cents
    return book_side, f"{yes_cents / 100:.4f}"


async def place_limit_order(
    *,
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: int,
    client_order_id: Optional[str] = None,
) -> dict:
    side = side.lower()
    action = action.lower()
    if side not in ("yes", "no"):
        raise ValueError(f"side must be yes|no, got {side}")
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be buy|sell, got {action}")
    if not (1 <= price_cents <= 99):
        raise ValueError(f"price_cents must be 1..99, got {price_cents}")
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")

    book_side, price = _v2_order_fields(side, action, int(price_cents))
    body: dict = {
        "ticker": ticker,
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "side": book_side,
        "count": f"{int(count)}.00",
        "price": price,
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }
    # Serialize against an in-progress credential test / env switch (which
    # briefly flips the global signing env) WITHOUT holding the lock across the
    # HTTP call: the env is read under ENV_LOCK (so a flip-in-progress is waited
    # out) and pinned across every attempt inside _signed_request — a flip after
    # the read makes the request ABORT rather than sign for the other account.
    # A slow send / 429 ladder no longer wedges every other caller behind the
    # lock (the old design held it across the whole call, worst case ~80s).
    async with ENV_LOCK:
        env0 = get_env()
    return await _signed_request(
        "POST", ORDERS_V2_PATH, json=body, timeout=HOT_TIMEOUT, pin_env=env0,
    )


async def cancel_order(order_id: str) -> dict:
    async with ENV_LOCK:
        env0 = get_env()  # wait out any env flip, pin; see place_limit_order
    return await _signed_request(
        "DELETE", f"{ORDERS_V2_PATH}/{order_id}", timeout=HOT_TIMEOUT, pin_env=env0,
    )
