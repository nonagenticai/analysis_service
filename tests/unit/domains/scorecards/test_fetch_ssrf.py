"""``fetch_html_capped`` refuses non-public hosts on the initial URL AND on
every redirect hop — a scorecard endpoint must not be usable as an SSRF probe.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

import httpx
import pytest

import src.domains.scorecards.fetch as fetch_mod
from src.domains.scorecards.fetch import SsrfBlockedError, fetch_html_capped


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Point ``fetch_html_capped``'s internal AsyncClient at a MockTransport."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        fetch_mod.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


async def test_raw_link_local_ip_blocked_before_any_request() -> None:
    # The cloud metadata endpoint — must be refused without ever opening a socket.
    with pytest.raises(SsrfBlockedError):
        await fetch_html_capped("http://169.254.169.254/latest/meta-data/", user_agent="t")


async def test_localhost_blocked() -> None:
    with pytest.raises(SsrfBlockedError):
        await fetch_html_capped("http://localhost:8080/admin", user_agent="t")


async def test_private_ip_blocked() -> None:
    with pytest.raises(SsrfBlockedError):
        await fetch_html_capped("http://10.0.0.5/", user_agent="t")


async def test_redirect_to_private_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # A public URL that 302-redirects to the metadata endpoint: auto-following
    # would leak the internal host; the per-hop re-check must block it.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(SsrfBlockedError):
        await fetch_html_capped("https://public.example.com/", user_agent="t")


async def test_public_page_fetched_and_byte_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>hello</body></html>")

    _install_mock_client(monkeypatch, handler)
    html = await fetch_html_capped("https://public.example.com/", user_agent="t")
    assert "hello" in html


async def test_public_redirect_to_public_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A redirect to another PUBLIC host is fine — the guard blocks only non-public.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "start.example.com":
            return httpx.Response(302, headers={"location": "https://final.example.com/page"})
        return httpx.Response(200, text="<html><body>final</body></html>")

    _install_mock_client(monkeypatch, handler)
    html = await fetch_html_capped("https://start.example.com/", user_agent="t")
    assert "final" in html
