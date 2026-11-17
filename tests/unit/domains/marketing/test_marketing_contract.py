"""Marketing scorecard audit contract (design 2026-10-17 §2.3 / §5.3, lane D).

findings[] / checks[] carry only the scorecard's own mkt-* dimensions, facts{}
is populated from the page, the permanent not-measured facts are present, the
deterministic score is byte-identical to origin/main, and the AI status reports
a WM outage instead of a silent green.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.domains.marketing.schemas import MarketingScorecardResponse
from src.domains.marketing.service import OWN_DIMENSIONS, score_marketing
from tests.llm_test_doubles import FakeWMClient, RoutedWMClient

_KNOWN_PAGE = """
<html lang="en"><head>
<title>Acme Ledger - the only bookkeeping app built for freelancers</title>
<meta name="description" content="Acme Ledger is the only bookkeeping app purpose-built for freelancers. Start free today.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:image" content="https://acme.test/og-card.png">
<meta property="og:title" content="Acme Ledger">
<link rel="icon" href="/favicon-32.png">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TEST"></script>
<script>window.dataLayer = window.dataLayer || []; function gtag(){dataLayer.push(arguments);} gtag('js', new Date());</script>
<script src="https://cdn.segment.com/analytics.js/v1/abc/analytics.min.js"></script>
</head>
<body>
<header><img class="site-logo" alt="Acme Ledger logo" src="/img/logo.svg"><nav><a href="/about-us">About us</a><a href="/pricing">Pricing</a></nav></header>
<h1>The only bookkeeping app built for freelancers</h1>
<h2>Send invoices, track expenses, file taxes</h2>
<p>Rated 4.9 on Capterra and featured in TechCrunch.</p>
<section><h2>Acme Ledger vs spreadsheets</h2></section>
<form><input type="email" name="email" placeholder="you@example.com"><button>Get the newsletter</button></form>
<p>Plans from $12 / month.</p>
<footer>
<a href="https://twitter.com/acmeledger">Twitter</a>
<a href="https://www.linkedin.com/company/acme-ledger/">LinkedIn</a>
<a href="https://www.youtube.com/@acmeledger">YouTube</a>
<a href="mailto:hello@acme.test">Email us</a>
</footer>
</body></html>
"""

_WEAK_PAGE = "<html><body><p>we do stuff</p></body></html>"

# Captured from origin/main f641fec (before lane D) on these exact pages. MKT-G2
# de-duplication and any weight change are owner decisions (design §7): until one
# is made, the composite must not move.
_BASELINE = {
    "known": {
        "overall_score": 90.6,
        "grade": "A",
        "dimension_scores": {
            "brand": 83.5,
            "competitive": 100.0,
            "content": 94.5,
            "conversion": 86.1,
            "growth": 89.3,
            "seo": 87.6,
        },
    },
    "weak": {
        "overall_score": 79.9,
        "grade": "C",
        "dimension_scores": {
            "brand": 76.4,
            "competitive": 79.7,
            "content": 82.8,
            "conversion": 85.9,
            "growth": 70.7,
            "seo": 76.9,
        },
    },
}
_PAGES = {"known": _KNOWN_PAGE, "weak": _WEAK_PAGE}

_ALL_AI: dict[str, Any] = {
    "include_llm": True,
    "include_recommendations": True,
    "include_grounding": True,
    "include_exec_summary": True,
    "include_critic": True,
}


def _wm_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://wm.test.invalid/workers/x/message")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["check_id"]: r for r in rows}


class TestScoreIsByteIdentical:
    @pytest.mark.parametrize("name", sorted(_PAGES))
    async def test_score_matches_pre_change_baseline(self, name):
        result = await score_marketing(html=_PAGES[name], url=None)
        got = {k: result[k] for k in ("overall_score", "grade", "dimension_scores")}
        assert got == _BASELINE[name]

    async def test_ai_on_does_not_move_the_score(self):
        result = await score_marketing(
            html=_KNOWN_PAGE, url=None, wm_client=RoutedWMClient(), **_ALL_AI
        )
        assert result["overall_score"] == _BASELINE["known"]["overall_score"]
        assert result["dimension_scores"] == _BASELINE["known"]["dimension_scores"]


class TestOwnDimensionsOnly:
    @pytest.mark.parametrize("name", sorted(_PAGES))
    async def test_findings_and_checks_have_no_cro_or_seo_ids(self, name):
        result = await score_marketing(html=_PAGES[name], url=None, include_unbucketed=True)
        rows = [*result["findings"], *result["checks"]]
        assert rows
        assert all(r["check_id"].startswith("mkt-") for r in rows), [
            r["check_id"] for r in rows if not r["check_id"].startswith("mkt-")
        ]
        assert {r["dimension"] for r in rows} <= OWN_DIMENSIONS

    async def test_checks_list_the_full_catalog(self):
        result = await score_marketing(html=_KNOWN_PAGE, url=None)
        assert len(result["checks"]) == 17


class TestCheckInventory:
    async def test_known_page_statuses_and_measurements(self):
        checks = _by_id((await score_marketing(html=_KNOWN_PAGE, url=None))["checks"])
        assert checks["mkt-brand-no-structured-data"]["status"] == "fail"
        assert checks["mkt-growth-no-consent"]["status"] == "fail"
        assert checks["mkt-growth-no-growth-loop"]["status"] == "fail"
        assert checks["mkt-brand-incomplete-social-card"]["status"] == "fail"
        assert checks["mkt-brand-no-og-image"]["status"] == "pass"
        assert checks["mkt-brand-no-og-image"]["measured"] == "https://acme.test/og-card.png"
        assert checks["mkt-growth-no-analytics"]["measured"] == (
            "Google Analytics, Google Tag Manager, Segment"
        )
        assert checks["mkt-growth-no-social-presence"]["measured"] == "x, linkedin, youtube"

    async def test_preconditioned_checks_are_not_run_never_pass(self):
        checks = _by_id((await score_marketing(html=_WEAK_PAGE, url=None))["checks"])
        consent = checks["mkt-growth-no-consent"]
        assert consent["status"] == "not_run"
        assert "analytics" in consent["reason"].lower()
        card = checks["mkt-brand-incomplete-social-card"]
        assert card["status"] == "not_run"
        assert "og:image" in card["reason"]

    async def test_topic_merged_contact_finding_still_fails_the_check(self):
        # deduplicate_findings merges mkt-brand-no-contact-info into
        # seo-eeat-no-contact; the inventory must still say the page has none.
        result = await score_marketing(html=_WEAK_PAGE, url=None)
        assert _by_id(result["checks"])["mkt-brand-no-contact-info"]["status"] == "fail"


class TestFacts:
    async def test_known_page_facts(self):
        facts = (await score_marketing(html=_KNOWN_PAGE, url=None))["facts"]
        assert facts["positioning"]["differentiation_claim"] == (
            "The only bookkeeping app built for freelancers"
        )
        assert facts["positioning"]["comparison_page"] is True
        assert facts["positioning"]["reputation_signals"] == [
            "Capterra",
            "featured in",
            "TechCrunch",
        ]
        brand = facts["brand"]
        assert brand["og_image_url"] == "https://acme.test/og-card.png"
        assert brand["logo"] == {"present": True, "url": "/img/logo.svg"}
        assert brand["about_link"] == {"present": True, "url": "/about-us"}
        assert brand["contact"]["email"] == "hello@acme.test"
        assert brand["social_card_fields"] == {
            "og_title": True,
            "og_description": False,
            "og_image": True,
            "twitter_card": False,
        }
        channels = facts["channels"]
        assert [p["platform"] for p in channels["social_profiles"]] == [
            "x",
            "linkedin",
            "youtube",
        ]
        assert channels["consent"] is False
        assert channels["pricing_visible"] is True
        assert channels["email_capture"] is True
        assert channels["retention_hook"] is True
        assert channels["referral"] is False

    async def test_not_measured_facts_present(self):
        facts = (await score_marketing(html=_WEAK_PAGE, url=None))["facts"]
        assert facts["competitive_benchmark"]["measured"] is False
        assert "competitor" in facts["competitive_benchmark"]["reason"]
        assert facts["channel_performance"]["measured"] is False
        assert "analytics/ad-account" in facts["channel_performance"]["reason"]

    async def test_dimension_sources_match_the_scored_dimensions(self):
        result = await score_marketing(html=_KNOWN_PAGE, url=None)
        rows = {r["dimension"]: r for r in result["facts"]["dimension_sources"]}
        dims = {d["dimension"]: d for d in result["dimensions"]}
        assert set(rows) == set(dims)
        for name, row in rows.items():
            assert row["score"] == result["dimension_scores"][name]
            assert row["weight"] == dims[name]["weight"]
        assert sum(r["weighted_points"] for r in rows.values()) == pytest.approx(
            result["overall_score"], abs=0.1
        )

    async def test_response_model_validates_typed_facts(self):
        result = await score_marketing(html=_KNOWN_PAGE, url=None)
        model = MarketingScorecardResponse(**result)
        assert model.facts is not None
        assert model.facts.brand is not None
        assert model.facts.brand.og_image_url == "https://acme.test/og-card.png"
        assert model.facts.dimension_sources[0].report_pointer == "cro_audit"
        assert len(model.checks) == 17


class TestAiStatus:
    async def test_wm_401_is_unavailable_and_still_scores(self):
        wm = FakeWMClient(raise_after="always", error=_wm_401())
        result = await score_marketing(html=_KNOWN_PAGE, url=None, wm_client=wm, **_ALL_AI)
        assert result["overall_score"] == _BASELINE["known"]["overall_score"]
        ai = result["ai"]
        assert ai["requested"] is True
        assert ai["status"] == "unavailable", ai
        layers = {layer["layer"]: layer for layer in ai["layers"]}
        assert layers["micro_judge"]["status"] == "failed"
        assert "WM 401" in (layers["micro_judge"]["reason"] or "")
        assert result["llm_enriched"] is False
        # The deterministic contract survives the outage.
        assert len(result["checks"]) == 17
        assert result["facts"]["brand"] is not None

    async def test_healthy_wm_is_complete(self):
        result = await score_marketing(
            html=_KNOWN_PAGE, url=None, wm_client=RoutedWMClient(), **_ALL_AI
        )
        ai = result["ai"]
        assert ai["status"] == "complete", ai
        assert {layer["status"] for layer in ai["layers"]} <= {"ok", "skipped"}
        assert result["llm_enriched"] is True
        assert result["exec_summary"]["summary"]

    async def test_ai_off_is_not_requested(self):
        result = await score_marketing(html=_KNOWN_PAGE, url=None)
        assert result["ai"]["status"] == "not_requested"
        assert result["ai"]["layers"] == []
