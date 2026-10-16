"""Retries, backoff, politeness, block detection (SPEC §5)."""

import httpx
import pytest
import respx

from eventparser import http as http_mod
from eventparser.http import BlockedError, Fetcher, FetchError
from tests.conftest import run

URL = "https://site.example/x"


def fetcher(sleeps: list[float], **kw) -> Fetcher:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    return Fetcher(sleep=fake_sleep, host_delay=0, host_delays={"linkedin.com": 0}, **kw)


@respx.mock
def test_retries_5xx_then_succeeds():
    """R-NET-2"""
    route = respx.get(URL)
    route.side_effect = [httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")]
    sleeps: list[float] = []
    assert run(fetcher(sleeps).get_text(URL)) == "ok"
    assert route.call_count == 3
    assert len(sleeps) == 2 and 1.0 <= sleeps[0] <= 1.25 and 2.0 <= sleeps[1] <= 2.5  # exponential + jitter


@respx.mock
def test_honours_retry_after_capped():
    """R-NET-2"""
    route = respx.get(URL)
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(429, headers={"Retry-After": "500"}),
                         httpx.Response(200, text="ok")]
    sleeps: list[float] = []
    run(fetcher(sleeps).get_text(URL))
    assert sleeps == [7.0, 60.0]


@respx.mock
def test_gives_up_after_three_attempts():
    route = respx.get(URL).respond(500)
    with pytest.raises(FetchError, match="3 attempts"):
        run(fetcher([]).get(URL))
    assert route.call_count == 3


@respx.mock
def test_no_retry_on_404():
    """R-NET-2: other 4xx are not retried."""
    route = respx.get(URL).respond(404)
    with pytest.raises(FetchError, match="404"):
        run(fetcher([]).get(URL))
    assert route.call_count == 1


@respx.mock
def test_retries_connect_errors():
    route = respx.get(URL)
    route.side_effect = [httpx.ConnectTimeout("t"), httpx.Response(200, text="ok")]
    assert run(fetcher([]).get_text(URL)) == "ok"


@respx.mock
def test_999_is_blocked_without_retry():
    route = respx.get("https://www.linkedin.com/company/x/").respond(999)
    with pytest.raises(BlockedError):
        run(fetcher([]).get("https://www.linkedin.com/company/x/"))
    assert route.call_count == 1


@respx.mock
def test_dns_sinkhole_is_reported_clearly(monkeypatch):
    """R-NET-5: a host blocked in /etc/hosts gives an actionable message."""
    respx.get("https://www.linkedin.com/x").mock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr(http_mod, "_sinkholed", lambda host: True)
    with pytest.raises(BlockedError, match="/etc/hosts"):
        run(fetcher([]).get("https://www.linkedin.com/x"))


def test_sinkhole_detection_with_real_resolver(monkeypatch):
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))])
    assert http_mod._sinkholed("blocked.example")
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("104.18.1.1", 443))])
    assert not http_mod._sinkholed("ok.example")


@respx.mock
def test_per_host_politeness_delay():
    """R-NET-3: consecutive requests to one host are spaced; LinkedIn gets a longer gap."""
    respx.get(url__startswith="https://a.example/").respond(200)
    respx.get(url__startswith="https://www.linkedin.com/").respond(200)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    fx = Fetcher(sleep=fake_sleep)  # real defaults: 1.0s, linkedin 4.0s

    async def go():
        await fx.get("https://a.example/1")
        await fx.get("https://a.example/2")
        await fx.get("https://www.linkedin.com/1")
        await fx.get("https://www.linkedin.com/2")

    run(go())
    assert len(sleeps) == 2
    assert 0.9 < sleeps[0] <= 1.0 and 3.9 < sleeps[1] <= 4.0


@respx.mock
def test_timeout_errors_are_never_blank():
    respx.get(URL).mock(side_effect=httpx.ReadTimeout(""))
    with pytest.raises(FetchError, match="ReadTimeout"):
        run(fetcher([]).get(URL))
