"""Design 2026-10-17 §6 "no silent passes": a check that could not run never scores clean.

Covers the two gaps left after the first contract commit:
1. CRO: a page with no footer / closing section used to score ``footer`` 100.
2. SEO: an SSRF-blocked / unreachable / empty cross-page crawl used to read as a
   clean ``internal_linking`` 100; and for all three types an SSRF-blocked page
   fetch must fail closed (no score at all), never return a clean scorecard.
Each test names the old behaviour it would have passed under (NEG).
"""

from __future__ import annotations

import functools
from unittest.mock import patch

import httpx
import pytest

from src.domains.critique.services.scanners.cro.checks import NO_FOOTER_REASON
from src.domains.critique.services.scanners.seo import internal_linking_live
from src.domains.critique.services.scanners.seo.internal_linking_live import (
    scan_internal_linking_live,
)
from src.domains.cro.schemas import CroScoreResponse
from src.domains.cro.service import score_cro
from src.domains.marketing.service import score_marketing
from src.domains.scorecards.errors import ScorecardError
from src.domains.seo_audit.schemas import SeoAuditResponse
from src.domains.seo_audit.service import audit_seo

_NO_FOOTER = (
    "<html><body><h1>Cut onboarding time by 40%</h1>"
    "<a class='btn' href='/start'>Start my free trial</a></body></html>"
)
_WITH_FOOTER = _NO_FOOTER.replace(
    "</body>",
    "<footer><a href='/start'>Start now</a> <a href='/contact'>Contact</a></footer></body>",
)
_PAGE = "<html><head><title>Acme</title></head><body><h1>Acme</h1><a href='/a'>A</a></body></html>"


class TestCroFooter:
    async def test_no_footer_is_not_measured(self):
        result = await score_cro(html=_NO_FOOTER, url=None)
        row = next(r for r in result["subsections"] if r["section"] == "footer")
        # NEG (old): footer scored 100 with no note.
        assert row["score"] is None
        assert row["measured"] is False
        assert NO_FOOTER_REASON in row["note"]
        assert result["subsection_scores"]["footer"] is None
        assert "footer" in result["unmeasured_sections"]
        footer_checks = [c for c in result["checks"] if c["section"] == "footer"]
        assert len(footer_checks) == 2
        assert {c["status"] for c in footer_checks} == {"not_run"}
        assert {c["reason"] for c in footer_checks} == {NO_FOOTER_REASON}
        CroScoreResponse(**result)

    async def test_footer_present_is_measured(self):
        result = await score_cro(html=_WITH_FOOTER, url=None)
        row = next(r for r in result["subsections"] if r["section"] == "footer")
        assert row["score"] == 100.0
        assert "footer" not in result["unmeasured_sections"]
        statuses = {c["status"] for c in result["checks"] if c["section"] == "footer"}
        assert statuses == {"pass"}


class TestCrawlObservation:
    async def test_ssrf_blocked_start_url_reports_not_ran(self):
        observe: dict = {}
        found = await scan_internal_linking_live("http://127.0.0.1/", observe=observe)
        assert found == []
        # NEG (old): an empty list with no way to tell it from a clean crawl.
        assert observe["ran"] is False
        assert "SSRF" in observe["reason"]

    async def test_no_page_reached_reports_not_ran(self):
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        # Bound to the REAL client before the patch, so the scanner's own
        # kwargs (timeout, follow_redirects, headers) pass straight through.
        _factory = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(_handler))

        from bs4 import BeautifulSoup

        home = BeautifulSoup(
            "<html><body><a href='https://example.test/a'>a</a>"
            "<a href='https://example.test/b'>b</a></body></html>",
            "lxml",
        )
        observe: dict = {}
        with patch.object(internal_linking_live.httpx, "AsyncClient", _factory):
            found = await scan_internal_linking_live(
                "https://example.test/", home_soup=home, observe=observe
            )
        assert found == []
        assert observe["ran"] is False
        assert observe["pages_crawled"] >= 1
        assert observe["pages_reached"] == 0
        assert "could be fetched" in observe["reason"]


class TestSeoBlockedCrawlIsUnmeasured:
    async def test_ssrf_blocked_crawl_and_live_checks_never_read_clean(self):
        # html supplied, url points at a private address: the page is scored from
        # the html, but every network probe (robots/sitemap/llms.txt + crawl) is
        # SSRF-blocked.
        result = await audit_seo(html=_PAGE, url="http://10.0.0.7/", cross_page_linking=True)
        row = next(r for r in result["groups"] if r["group"] == "internal_linking")
        # NEG (old): internal_linking 100, measured, no note.
        assert row["score"] is None
        assert row["measured"] is False
        assert "SSRF" in row["note"]
        assert "internal_linking" in result["unmeasured_groups"]
        assert result["coverage"] < 1.0
        assert result["facts"]["crawl"]["measured"] is False
        assert "SSRF" in result["facts"]["crawl"]["reason"]
        linking = [c for c in result["checks"] if c["group"] == "internal_linking"]
        # A real static failure (e.g. insufficient content) may still FAIL; what
        # must never happen is a PASS for a group that was not measured.
        assert linking and "pass" not in {c["status"] for c in linking}
        by_id = {c["check_id"]: c["status"] for c in linking}
        for crawl_id in ("seo-linking-broken", "seo-linking-orphan", "seo-linking-hub"):
            assert by_id[crawl_id] == "not_run"
        live = {
            c["check_id"]: c
            for c in result["checks"]
            if c["check_id"] in {"seo-robots-txt-missing", "seo-sitemap-missing"}
        }
        assert {c["status"] for c in live.values()} == {"not_run"}
        SeoAuditResponse(**result)


@pytest.mark.parametrize(
    "service",
    [score_cro, audit_seo, score_marketing],
    ids=["cro_audit", "seo_audit", "marketing_scorecard"],
)
async def test_ssrf_blocked_page_fetch_fails_closed(service):
    # NEG: a swallowed fetch error would return a clean-looking scorecard.
    with pytest.raises(ScorecardError, match=r"(?i)fetch"):
        await service(html=None, url="http://127.0.0.1:6303/")
