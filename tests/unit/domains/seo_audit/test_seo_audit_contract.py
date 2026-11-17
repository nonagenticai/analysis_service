"""SEO audit report contract (design 2026-10-17 §3.2, §4 SEO-G1..G7, §5.3).

Covers the SEO lane: AI status of the three SEO analyzers, LLM output as
``ai.annotations[]`` (never findings), robots/sitemap/llms.txt live checks on the
audit path (run with a URL, ``not_run`` for inline HTML or a dead origin), the
always-unmeasured ``performance`` row, the check inventory and ``facts{}``.

No network: the live probes either use the hermetic conftest stub or, in the
``TestLiveProbes`` class, the REAL probe code with ``httpx`` patched.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.domains.critique.services.report_generator import deduplicate_findings
from src.domains.critique.services.scanners.seo import SEOScanner, compute_seo_audit_score
from src.domains.critique.services.scanners.seo import facts as facts_mod
from src.domains.critique.services.scanners.seo.facts import NO_URL_REASON
from src.domains.seo_audit import service
from src.domains.seo_audit.routes import audit_seo_route
from src.domains.seo_audit.schemas import SeoAuditRequest
from src.domains.seo_audit.service import audit_seo
from tests.llm_test_doubles import DEFAULT_AUDIT_ROUTES, FakeWMClient, RoutedWMClient
from tests.unit.domains.seo_audit.test_seo_audit_routes import _STRONG, _WEAK, _make_request

_LIVE_IDS = (
    "seo-robots-txt-missing",
    "seo-sitemap-missing",
    "seo-geo-ai-crawler-blocked",
    "seo-geo-no-llms-txt",
)
_SEO_ROUTES = {
    **DEFAULT_AUDIT_ROUTES,
    "seo-intent-classify": lambda _m: {"intent": "informational", "confidence": 0.8},
    "seo-eeat-depth": lambda _m: {
        "experience": "present",
        "expertise": "strong",
        "evidence": "Cites a standards body.",
    },
}


def _wm_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://wm.test.invalid/workers/x/message")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


def _layers(result: dict) -> dict[str, dict]:
    return {layer["layer"]: layer for layer in result["ai"]["layers"]}


def _checks(result: dict) -> dict[str, dict]:
    return {row["check_id"]: row for row in result["checks"]}


# ── AI status (F8 / F9 / F11) ──────────────────────────────────────────────────


class TestSeoAiStatus:
    async def test_wm_401_marks_seo_layers_failed_and_ai_unavailable(self):
        wm = FakeWMClient(raise_after="always", error=_wm_401())
        result = await audit_seo(html=_STRONG, url=None, include_llm=True, wm_client=wm)
        layers = _layers(result)
        for layer in ("seo_intent", "seo_eeat"):
            assert layers[layer]["status"] == "failed", layers
            assert "WM 401" in (layers[layer]["reason"] or "")
        assert result["ai"]["status"] == "unavailable"
        assert result["llm_enriched"] is False
        assert result["ai"]["annotations"] == []
        # Fail-soft for the deterministic score.
        base = await audit_seo(html=_STRONG, url=None)
        assert result["overall_score"] == base["overall_score"]

    async def test_include_llm_without_wm_client_is_unavailable_not_silent(self):
        result = await audit_seo(html=_STRONG, url=None, include_llm=True, wm_client=None)
        layers = _layers(result)
        for layer in ("seo_intent", "seo_eeat"):
            assert layers[layer]["status"] == "skipped"
            assert layers[layer]["reason"] == "WM client not configured"
        assert "content_gap" not in layers  # no competitor_urls → not recorded
        assert result["ai"]["status"] == "unavailable"

    async def test_route_without_app_wm_client_reports_unavailable(self):
        request = _make_request()
        request.app.state.wm_client = None
        result = await audit_seo_route(SeoAuditRequest(html=_STRONG, include_llm=True), request)
        assert result.ai is not None
        assert result.ai.status == "unavailable"
        assert "WM client not configured" in (result.ai.reason or "")

    async def test_healthy_wm_annotates_and_never_emits_findings(self):
        wm = RoutedWMClient(routes=_SEO_ROUTES)
        result = await audit_seo(html=_STRONG, url=None, include_llm=True, wm_client=wm)
        base = await audit_seo(html=_STRONG, url=None)
        ai = result["ai"]
        assert ai["status"] == "complete", ai
        assert {k: v["status"] for k, v in _layers(result).items()} == {
            "seo_intent": "ok",
            "seo_eeat": "ok",
        }
        by_kind = {a["kind"]: a for a in ai["annotations"]}
        assert set(by_kind) == {"search_intent", "eeat_depth"}
        assert by_kind["search_intent"]["layer"] == "seo_intent"
        assert by_kind["search_intent"]["data"]["intent"] == "informational"
        assert by_kind["eeat_depth"]["data"]["expertise"] == "strong"
        # SEO-G7: no LLM id anywhere in the scored/bucketed surface.
        llm_ids = {"seo-keyword-intent", "seo-eeat-depth", "seo-content-gap"}
        for key in ("findings", "quick_wins", "strategic", "checks"):
            assert not llm_ids & {row["check_id"] for row in result[key]}, key
        assert result["overall_score"] == base["overall_score"]
        assert result["group_scores"] == base["group_scores"]
        assert result["llm_enriched"] is True

    async def test_thin_page_eeat_is_skipped_by_design(self):
        wm = RoutedWMClient(routes=_SEO_ROUTES)
        result = await audit_seo(html=_WEAK, url=None, include_llm=True, wm_client=wm)
        layers = _layers(result)
        assert layers["seo_eeat"]["status"] == "skipped"
        assert "200-word" in (layers["seo_eeat"]["reason"] or "")
        assert result["ai"]["status"] == "complete"

    async def test_content_gap_recorded_only_with_competitors(self):
        routes = {**_SEO_ROUTES, "seo-content-gap": lambda _m: {"gaps": [{"topic": "cold brew"}]}}
        wm = RoutedWMClient(routes=routes)
        result = await audit_seo(
            html=_STRONG,
            url=None,
            include_llm=True,
            wm_client=wm,
            competitor_urls=["https://competitor.test/a"],
            fetch_html=AsyncMock(return_value=_STRONG),
        )
        assert _layers(result)["content_gap"]["status"] == "ok"
        gap = next(a for a in result["ai"]["annotations"] if a["kind"] == "content_gap")
        assert gap["data"]["gaps"] == [{"topic": "cold brew"}]


# ── performance (SEO-G1 without Lighthouse) ────────────────────────────────────


class TestPerformanceNotMeasured:
    async def test_performance_row_is_unmeasured_and_score_is_untouched(self):
        result = await audit_seo(html=_STRONG, url=None)
        row = next(g for g in result["groups"] if g["group"] == "performance")
        assert row["score"] is None
        assert row["weight"] == 0.0
        assert row["measured"] is False
        assert "not measured" in row["note"]
        assert "performance" in result["unmeasured_groups"]
        assert "performance" not in result["group_scores"]
        assert result["facts"]["performance"]["measured"] is False

        # Byte-identical to the pre-change scoring of the same findings.
        raw = SEOScanner().scan_audit(_STRONG, "", include_llm=False)
        expected = compute_seo_audit_score(deduplicate_findings(raw))
        assert result["overall_score"] == expected["overall_score"]
        assert result["coverage"] == expected.get("coverage", 1.0) == 1.0
        assert result["group_scores"] == expected["group_scores"]


# ── live probes (SEO-G2) ───────────────────────────────────────────────────────


def _resp(status: int, url: str, text: str = "") -> httpx.Response:
    return httpx.Response(status, text=text, request=httpx.Request("GET", url))


class TestLiveProbes:
    @pytest.fixture
    def stub_live_probes(self) -> Iterator[None]:
        """Override the conftest stub: this class drives the REAL probe code."""
        yield

    @staticmethod
    def _patch_http(technical: dict[str, int | Exception], geo: dict[str, int | Exception]):
        async def _get(self: object, url: str, *a: object, **k: object) -> httpx.Response:
            outcome = technical[url.rsplit("/", 1)[-1]]
            if isinstance(outcome, Exception):
                raise outcome
            return _resp(outcome, url)

        async def _guarded(client: object, url: str, **k: object) -> httpx.Response:
            outcome = geo[url.rsplit("/", 1)[-1]]
            if isinstance(outcome, Exception):
                raise outcome
            body = "User-agent: GPTBot\nDisallow: /\n" if url.endswith("robots.txt") else ""
            return _resp(outcome, url, body)

        return (
            patch.object(httpx.AsyncClient, "get", _get),
            patch("src.domains.critique.services.scanners.seo.geo.guarded_get", _guarded),
        )

    async def _audit(self, technical, geo) -> dict:
        get_patch, guarded_patch = self._patch_http(technical, geo)
        with get_patch, guarded_patch:
            return await audit_seo(
                html=None,
                url="https://example.com/page",
                fetch_html=AsyncMock(return_value=_STRONG),
            )

    async def test_robots_and_sitemap_really_checked_with_url(self):
        result = await self._audit(
            {"robots.txt": 404, "sitemap.xml": 200},
            {"robots.txt": 200, "llms.txt": 404},
        )
        checks = _checks(result)
        assert checks["seo-robots-txt-missing"]["status"] == "fail"
        assert checks["seo-sitemap-missing"]["status"] == "pass"
        assert checks["seo-geo-ai-crawler-blocked"]["status"] == "fail"
        assert checks["seo-geo-no-llms-txt"]["status"] == "fail"
        ids = {f["check_id"] for f in result["findings"]}
        assert "seo-robots-txt-missing" in ids
        facts = result["facts"]
        assert facts["robots"]["measured"] is True
        assert facts["robots"]["status"] == 404
        assert facts["robots"]["fetched"] is False
        assert facts["robots"]["ai_bots_blocked"] == ["GPTBot"]
        assert facts["sitemap"] == {
            "measured": True,
            "found": True,
            "status": 200,
            "url": "https://example.com/sitemap.xml",
        }
        assert facts["llms_txt"]["found"] is False

    async def test_dead_origin_makes_live_checks_not_run_never_pass(self):
        boom = httpx.ConnectTimeout("origin timed out")
        result = await self._audit(
            {"robots.txt": boom, "sitemap.xml": boom}, {"robots.txt": boom, "llms.txt": boom}
        )
        checks = _checks(result)
        for cid in _LIVE_IDS:
            assert checks[cid]["status"] == "not_run", (cid, checks[cid])
            assert "did not complete" in checks[cid]["reason"]
        # "could not check" markers are findings, not inventory rows.
        assert "seo-robots-txt-errored" not in checks
        assert "seo-robots-txt-errored" in {f["check_id"] for f in result["findings"]}
        assert result["facts"]["robots"]["measured"] is False
        assert result["facts"]["sitemap"]["measured"] is False

    async def test_a_probe_that_raises_does_not_fail_the_audit(self, monkeypatch):
        async def _explode(url: str, *, observe: dict | None = None) -> list[dict]:
            raise RuntimeError("probe bug")

        monkeypatch.setattr(service._SCANNER, "scan_live", _explode)
        monkeypatch.setattr(service._SCANNER, "scan_audit_live", _explode)
        result = await audit_seo(
            html=None, url="https://example.com/page", fetch_html=AsyncMock(return_value=_STRONG)
        )
        checks = _checks(result)
        for cid in _LIVE_IDS:
            assert checks[cid]["status"] == "not_run"
            assert "probe bug" in checks[cid]["reason"]

    async def test_inline_html_live_checks_are_not_run(self):
        result = await audit_seo(html=_STRONG, url=None)
        checks = _checks(result)
        for cid in _LIVE_IDS:
            assert checks[cid]["status"] == "not_run"
            assert checks[cid]["reason"] == NO_URL_REASON
        assert checks["seo-url-underscores"]["status"] == "not_run"
        assert checks["seo-linking-orphan"]["status"] == "not_run"
        assert result["facts"]["robots"] == {"measured": False, "reason": NO_URL_REASON}


# ── check inventory ──────────────────────────────────────────────────────────


class TestCheckInventory:
    async def test_every_failing_finding_is_a_fail_row(self):
        result = await audit_seo(html=_WEAK, url=None)
        checks = _checks(result)
        for finding in result["findings"]:
            assert checks[finding["check_id"]]["status"] == "fail", finding["check_id"]
        assert {row["status"] for row in result["checks"]} <= {"pass", "fail", "not_run"}
        title = checks["seo-title-too-short"]
        assert title["group"] == "on_page"
        assert title["target"] == "50-60 characters"

    async def test_failed_crawl_makes_the_group_not_run(self, monkeypatch):
        async def _fail(*a: object, **k: object) -> list[dict]:
            raise TimeoutError("crawl timed out")

        monkeypatch.setattr(
            "src.domains.critique.services.scanners.seo.internal_linking_live."
            "scan_internal_linking_live",
            _fail,
        )
        result = await audit_seo(
            html=None,
            url="https://example.com/",
            fetch_html=AsyncMock(return_value=_STRONG),
            cross_page_linking=True,
        )
        checks = _checks(result)
        for cid in ("seo-linking-orphan", "seo-linking-no-internal"):
            assert checks[cid]["status"] == "not_run"
            assert "UNMEASURED" in checks[cid]["reason"]
        assert result["facts"]["crawl"]["measured"] is False


# ── facts (SEO-G3..G6) ─────────────────────────────────────────────────────────

_FACTS_PAGE = """
<html lang="en"><head>
<title>How to brew better coffee at home</title>
<meta name="description" content="Brew better coffee.">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://example.test/coffee">
<link rel="alternate" hreflang="en" href="https://example.test/coffee">
<link rel="alternate" hreflang="x-default" href="https://example.test/coffee">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","headline":"h"}</script>
</head><body><main>
<h1>Brew coffee</h1><h2>Grind</h2><h3>Fine</h3>
<img src="a.jpg" alt="a cup"><img src="b.jpg">
<p>Short text.</p>
<a href="/beans">Beans</a><a href="https://other.test/x">click here</a>
</main></body></html>
"""


class TestFacts:
    async def test_facts_for_a_known_page(self):
        facts = (await audit_seo(html=_FACTS_PAGE, url=None))["facts"]
        assert facts["title"] == {"text": "How to brew better coffee at home", "length": 33}
        assert facts["meta_description"] == {"text": "Brew better coffee.", "length": 19}
        assert facts["h1"] == ["Brew coffee"]
        assert [h["level"] for h in facts["heading_outline"]] == [1, 2, 3]
        assert facts["images"] == {"total": 2, "missing_alt": 1}
        assert facts["canonical"] == "https://example.test/coffee"
        assert facts["meta_robots"] == "index, follow"
        assert [h["lang"] for h in facts["hreflang"]] == ["en", "x-default"]
        assert facts["indexable"]["value"] is True
        assert facts["indexable"]["checked"] == ["meta_robots"]  # no URL: nothing else known
        assert facts["schema"]["types_present"] == ["Article"]
        assert facts["schema"]["incomplete"] == {"Article": ["author", "datePublished", "image"]}
        assert "Organization" in facts["schema"]["missing"]
        assert facts["primary_keyword"] is None  # too little text to pick one
        assert facts["readability"]["measured"] is False
        assert facts["readability"]["grade"] is None
        # Host taken from the canonical (inline HTML has no URL).
        assert facts["links"] == {
            "measured": True,
            "host": "example.test",
            "internal": 1,
            "external": 1,
            "generic_anchors": 1,
        }
        assert facts["crawl"]["measured"] is False
        assert facts["performance"]["measured"] is False

    async def test_noindex_page_is_not_indexable(self):
        html = _FACTS_PAGE.replace("index, follow", "noindex")
        facts = (await audit_seo(html=html, url=None))["facts"]
        assert facts["indexable"]["value"] is False
        assert facts["indexable"]["reasons"] == ["meta robots noindex"]

    async def test_missing_title_is_null_not_fabricated(self):
        facts = (await audit_seo(html="<html><body><p>x</p></body></html>", url=None))["facts"]
        assert facts["title"] is None
        assert facts["meta_description"] is None

    async def test_readability_measured_on_substantial_content(self):
        facts = (await audit_seo(html=_STRONG, url=None))["facts"]
        assert facts["readability"]["measured"] is True
        assert facts["readability"]["words"] >= 150
        assert isinstance(facts["readability"]["flesch"], float)
        assert facts["primary_keyword"] == "coffee"
        assert "coffee" in facts["top_keywords"]

    async def test_a_facts_bug_never_fails_the_audit(self, monkeypatch):
        def _boom(_soup: object) -> dict:
            raise ValueError("bug")

        monkeypatch.setattr(facts_mod, "schema_fact", _boom)
        result = await audit_seo(html=_FACTS_PAGE, url=None)
        assert result["facts"]["schema"] == {
            "measured": False,
            "reason": "fact extraction failed: ValueError",
        }
        assert result["facts"]["title"]["length"] == 33

    async def test_route_serialises_facts_and_checks(self):
        result = await audit_seo_route(SeoAuditRequest(html=_FACTS_PAGE), _make_request())
        assert result.facts["images"] == {"total": 2, "missing_alt": 1}
        assert any(c.status == "not_run" for c in result.checks)
        assert any(g.group == "performance" and g.score is None for g in result.groups)
