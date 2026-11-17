"""Each scrape failure CLASS must be distinguishable in the 502 it produces.

The route used to wrap `ws_client.scrape_url` in a blanket `except Exception` and raise one
string for every cause. Marketing records only the status and that string in
`recon_discovery_job.attempt_log` — so when merqi.nl's brand leg failed on 2026-08-30, all
that survived was "502 Bad Gateway", and afterwards nobody could say whether the site was
slow, the scraper was unreachable, the scraper answered 4xx, or the client raised.

Status stays 502 for every class on purpose: marketing classifies a brand-leg failure as
non-retryable by STATUS ({400,401,402,403,404,422}), so all of these are already correctly
transient. Only the detail and the log change.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from w2v_common.clients.webscraper import ScrapeResponse

from src.domains.icp.routes import _scrape_or_raise


class _Boom:
    """A WebscraperClient stand-in whose scrape_url raises whatever it was given."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def scrape_url(self, url: str) -> ScrapeResponse:
        raise self._exc


class _Errored:
    """A client returning a REAL ScrapeResponse that carries an error string."""

    def __init__(self, error: str) -> None:
        self._error = error

    async def scrape_url(self, url: str) -> ScrapeResponse:
        return ScrapeResponse(url=url, error=self._error)


async def _detail_for(exc: Exception) -> str:
    with pytest.raises(HTTPException) as raised:
        await _scrape_or_raise(_Boom(exc), "https://merqi.nl")
    assert raised.value.status_code == 502
    return str(raised.value.detail)


async def test_a_timeout_says_timeout() -> None:
    assert "timeout" in (await _detail_for(httpx.ReadTimeout("too slow"))).lower()


async def test_an_unreachable_upstream_says_unreachable() -> None:
    assert "unreachable" in (await _detail_for(httpx.ConnectError("refused"))).lower()


async def test_an_upstream_4xx_names_its_status() -> None:
    request = httpx.Request("POST", "http://webscraper/api/scrape/url")
    response = httpx.Response(418, request=request)
    detail = await _detail_for(httpx.HTTPStatusError("teapot", request=request, response=response))
    assert "418" in detail


async def test_an_unexpected_exception_names_its_type() -> None:
    # The catch-all must still say WHICH exception — that is the whole point.
    detail = await _detail_for(ValueError("bad shape"))
    assert "ValueError" in detail


async def test_a_reported_scrape_error_is_distinct_from_a_raised_one() -> None:
    with pytest.raises(HTTPException) as raised:
        await _scrape_or_raise(_Errored("robots.txt disallows"), "https://merqi.nl")
    assert raised.value.status_code == 502
    assert "scrape error" in str(raised.value.detail).lower()


async def test_every_class_produces_a_different_detail() -> None:
    # The regression that would undo this: someone collapses the branches back into one
    # string. Distinctness is the property, not the individual wordings.
    request = httpx.Request("POST", "http://webscraper/api/scrape/url")
    details = {
        await _detail_for(httpx.ReadTimeout("t")),
        await _detail_for(httpx.ConnectError("c")),
        await _detail_for(
            httpx.HTTPStatusError(
                "s", request=request, response=httpx.Response(503, request=request)
            )
        ),
        await _detail_for(ValueError("v")),
    }
    assert len(details) == 4, f"failure classes are not distinguishable: {details}"
