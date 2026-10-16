"""Shared HTTP layer: retries, backoff, per-host politeness, block detection (SPEC §5)."""

from __future__ import annotations

import asyncio
import random
import socket
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import redact

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
RETRY_STATUS = {429, 500, 502, 503, 504}
SINKHOLE_ADDRS = {"0.0.0.0", "::", "127.0.0.1", "::1"}


class FetchError(Exception):
    """A request failed after retries."""


class BlockedError(FetchError):
    """The site refused us (auth wall, 999, local DNS block). Retrying won't help."""


def _sinkholed(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and addrs <= SINKHOLE_ADDRS


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class Fetcher:
    """One per run. Wraps an httpx.AsyncClient with retry + politeness."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        attempts: int = 3,
        backoff_base: float = 1.0,
        max_retry_after: float = 60.0,
        host_delay: float = 1.0,
        host_delays: dict[str, float] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        timeout: float = 20.0,
    ) -> None:
        self.client = client or httpx.AsyncClient(
            headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True
        )
        self.attempts = attempts
        self.backoff_base = backoff_base
        self.max_retry_after = max_retry_after
        self.host_delay = host_delay
        self.host_delays = {"linkedin.com": 4.0, "api.telegram.org": 0.05, **(host_delays or {})}
        self._sleep = sleep
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _delay_for(self, host: str) -> float:
        for suffix, delay in self.host_delays.items():
            if host == suffix or host.endswith("." + suffix):
                return delay
        return self.host_delay

    async def _polite(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, -1e9) + self._delay_for(host) - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._host_last[host] = time.monotonic()

    async def request(self, method: str, url: str, *, attempts: int | None = None, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        attempts = attempts or self.attempts
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            await self._polite(host)
            try:
                resp = await self.client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                if isinstance(e, httpx.ConnectError) and await asyncio.to_thread(_sinkholed, host):
                    raise BlockedError(
                        f"{host} is blocked on this machine (/etc/hosts or a site blocker)"
                    ) from e
                last_exc = e
                wait = None
            else:
                if resp.status_code == 999:
                    raise BlockedError(f"{host} refused the request (HTTP 999 anti-bot)")
                if resp.status_code in RETRY_STATUS:
                    last_exc = FetchError(f"HTTP {resp.status_code} from {url}")
                    wait = _retry_after(resp)
                elif resp.status_code >= 400:
                    detail = " ".join(resp.text[:200].split())
                    raise FetchError(redact(f"HTTP {resp.status_code} from {url}" + (f": {detail}" if detail else "")))
                else:
                    return resp
            if attempt == attempts:
                break
            if wait is None:
                wait = self.backoff_base * 2 ** (attempt - 1) * (1 + random.random() * 0.25)
            await self._sleep(min(wait, self.max_retry_after))
        why = f"{type(last_exc).__name__}: {last_exc}".rstrip(": ") if last_exc else "unknown error"
        raise FetchError(redact(f"{method} {url} failed after {attempts} attempts: {why}"))

    async def pause(self, seconds: float) -> None:
        await self._sleep(seconds)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError as e:
            raise FetchError(f"invalid JSON from {url}: {e}") from e

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.get(url, **kwargs)).text

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        resp = await self.request("POST", url, json=payload, **kwargs)
        try:
            return resp.json()
        except ValueError as e:
            raise FetchError(f"invalid JSON from {url}: {e}") from e
