"""Unit tests for the marketing scanner (competitive/brand/growth) + composite scoring."""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.marketing import (
    DIMENSION_WEIGHTS,
    MarketingScanner,
    compute_marketing_score,
    dimension_of,
)
from src.domains.critique.services.scanners.marketing import base as mkt_base
from src.domains.critique.services.scanners.marketing.checks import (
    MARKETING_CHECKS,
)
from src.domains.critique.services.scanners.marketing.facts import (
    CHANNEL_PERFORMANCE_REASON,
    COMPETITIVE_BENCHMARK_REASON,
    build_marketing_facts,
    dimension_sources,
)
from src.domains.critique.services.scanners.marketing.scoring import (
    DIMENSION_SOURCES,
)

_BARE = "<html><body><p>we do stuff</p></body></html>"


class TestMarketingScanner:
    def test_bare_page_flags_all_three_dimensions(self):
        findings = MarketingScanner().scan(_BARE, "https://x.test")
        dims = {dimension_of(f["check_id"]) for f in findings}
        assert {"competitive", "brand", "growth"} <= dims
        assert all(f["category"] == "marketing" for f in findings)

    def test_check_ids_are_mkt_namespaced(self):
        findings = MarketingScanner().scan(_BARE, "")
        assert all(f["check_id"].startswith("mkt-") for f in findings)

    def test_rich_page_emits_fewer_findings(self):
        rich = """
        <html><head><script type="application/ld+json">{}</script>
        <meta property="og:image" content="x"><link rel="icon" href="x"></head>
        <body><header><img class="logo"></header>
        <h1>The only autonomous platform, unlike agencies</h1>
        <a href="/about">About</a><section>Merqi vs agencies</section>
        <p>Rated on G2. $99/month. Refer a friend. Subscribe to our newsletter.</p>
        </body></html>
        """
        assert len(MarketingScanner().scan(rich, "")) < len(MarketingScanner().scan(_BARE, ""))


class TestDimensionOf:
    def test_maps_all_check_id_families(self):
        assert dimension_of("mkt-competitive-no-comparison") == "competitive"
        assert dimension_of("mkt-brand-no-favicon") == "brand"
        assert dimension_of("mkt-growth-no-pricing-visibility") == "growth"
        assert dimension_of("cro-hero-no-h1") == "content"
        assert dimension_of("cro-valueprop-vague") == "content"
        assert dimension_of("cro-cta-generic") == "conversion"
        assert dimension_of("seo-title-missing") == "seo"
        assert dimension_of("unknown-thing") is None
        assert dimension_of(None) is None


class TestComputeMarketingScore:
    def test_empty_findings_scores_100(self):
        result = compute_marketing_score(
            cro_overall=100.0,
            seo_overall=100.0,
            cro_subsection_scores={"hero": 100.0, "valueprop": 100.0},
            marketing_findings=[],
        )
        assert result["overall_score"] == 100.0
        assert result["grade"] == "A+"
        assert set(result["dimension_scores"]) == set(DIMENSION_WEIGHTS)

    def test_content_is_hero_valueprop_cta_mean(self):
        # content ≈ the headline / value-prop / CTA axes = mean(hero, valueprop, cta).
        result = compute_marketing_score(
            cro_overall=80.0,
            seo_overall=90.0,
            cro_subsection_scores={"hero": 60.0, "valueprop": 90.0, "cta": 30.0},
            marketing_findings=[],
        )
        assert result["dimension_scores"]["content"] == 60.0  # (60 + 90 + 30) / 3
        assert result["dimension_scores"]["conversion"] == 80.0
        assert result["dimension_scores"]["seo"] == 90.0

    def test_content_missing_cta_subsection_defaults_to_100(self):
        # A missing sub-section score is treated as 100 (nothing wrong), so an
        # absent cta key does not drag content down.
        result = compute_marketing_score(
            cro_overall=80.0,
            seo_overall=90.0,
            cro_subsection_scores={"hero": 60.0, "valueprop": 90.0},
            marketing_findings=[],
        )
        assert result["dimension_scores"]["content"] == 83.3  # (60 + 90 + 100) / 3

    def test_scanner_findings_penalize_only_their_dimension(self):
        findings = [
            {
                "category": "marketing",
                "check_id": "mkt-growth-no-pricing-visibility",
                "severity": "medium",
            }
        ]
        result = compute_marketing_score(
            cro_overall=100.0,
            seo_overall=100.0,
            cro_subsection_scores={"hero": 100.0, "valueprop": 100.0},
            marketing_findings=findings,
        )
        assert result["dimension_scores"]["growth"] < 100.0
        assert result["dimension_scores"]["competitive"] == 100.0
        assert result["dimension_scores"]["brand"] == 100.0


def _check_ids(html: str) -> set[str]:
    return {f["check_id"] for f in MarketingScanner().scan(html, "https://x.test")}


class TestTrackingChecks:
    def test_bare_page_flags_no_analytics(self):
        assert "mkt-growth-no-analytics" in _check_ids(_BARE)

    def test_analytics_present_suppresses_no_analytics_but_flags_no_consent(self):
        html = (
            "<html><head><script>window.dataLayer=[];gtag('js',new Date());</script>"
            "</head><body><p>x</p></body></html>"
        )
        ids = _check_ids(html)
        assert "mkt-growth-no-analytics" not in ids
        assert "mkt-growth-no-consent" in ids

    def test_analytics_with_consent_flags_neither(self):
        html = (
            "<html><head><script src='https://www.googletagmanager.com/gtag/js'></script>"
            "</head><body><div class='cookie-consent'>Accept all cookies</div></body></html>"
        )
        ids = _check_ids(html)
        assert "mkt-growth-no-analytics" not in ids
        assert "mkt-growth-no-consent" not in ids

    def test_no_analytics_is_low_high_fp(self):
        # Static fetch can miss JS-injected trackers — keep this a soft nudge, not a tank.
        f = next(
            x
            for x in MarketingScanner().scan(_BARE, "")
            if x["check_id"] == "mkt-growth-no-analytics"
        )
        assert f["severity"] == "low"
        assert f["false_positive_risk"] == "high"


class TestGrowthDepthChecks:
    def test_email_capture_input_suppresses_finding(self):
        assert "mkt-growth-no-email-capture" in _check_ids(_BARE)
        html = "<html><body><form><input type='email' name='newsletter'></form></body></html>"
        assert "mkt-growth-no-email-capture" not in _check_ids(html)

    def test_social_profile_link_suppresses_finding(self):
        assert "mkt-growth-no-social-presence" in _check_ids(_BARE)
        html = "<html><body><a href='https://linkedin.com/company/acme'>us</a></body></html>"
        assert "mkt-growth-no-social-presence" not in _check_ids(html)


class TestBrandDepthChecks:
    def test_contact_info_suppresses_finding(self):
        assert "mkt-brand-no-contact-info" in _check_ids(_BARE)
        html = "<html><body><a href='mailto:hi@acme.com'>email us</a></body></html>"
        assert "mkt-brand-no-contact-info" not in _check_ids(html)

    def test_incomplete_social_card_flagged_when_og_image_but_missing_fields(self):
        html = (
            "<html><head><meta property='og:image' content='x'></head><body><p>x</p></body></html>"
        )
        assert "mkt-brand-incomplete-social-card" in _check_ids(html)

    def test_complete_social_card_not_flagged(self):
        html = (
            "<html><head><meta property='og:image' content='x'>"
            "<meta property='og:title' content='t'>"
            "<meta property='og:description' content='d'>"
            "<meta name='twitter:card' content='summary'></head><body><p>x</p></body></html>"
        )
        assert "mkt-brand-incomplete-social-card" not in _check_ids(html)


# ---------------------------------------------------------------------------
# Check catalog + facts (design 2026-10-17 X-G1 / MKT-G1)
# ---------------------------------------------------------------------------

_PKG = Path(mkt_base.__file__).parent
_MKT_LITERAL = re.compile(r"""["'](mkt-[a-z]+-[a-z-]+)["']""")

# The two regexes as they read before they were split into per-vendor /
# per-platform tables — the split must not change what they match.
_ORIGINAL_ANALYTICS_ALTS = (
    r"gtag\(|googletagmanager\.com|google-analytics\.com|ga\('create'|dataLayer"
    r"|fbq\(|connect\.facebook\.net|_linkedin_partner_id|snap\.licdn\.com"
    r"|static\.hotjar\.com|\bhj\(|fullstory\.com|\bFS\.identify|clarity\.ms"
    r"|clarity\(|segment\.com/analytics\.js|analytics\.track\(|mixpanel"
    r"|posthog|plausible\.io|matomo|amplitude|heap\.io|\bheap\."
)
_ORIGINAL_SOCIAL = (
    r"(twitter|x)\.com/|linkedin\.com/(company|in)/|facebook\.com/|instagram\.com/"
    r"|youtube\.com/(c/|channel/|@)|github\.com/|tiktok\.com/@|mastodon"
)


def _scanner_literals() -> set[str]:
    ids: set[str] = set()
    for path in _PKG.glob("*.py"):
        if path.name == "checks.py":
            continue
        ids.update(_MKT_LITERAL.findall(path.read_text()))
    return ids


class TestMarketingCheckCatalog:
    def test_catalog_has_17_unique_ids(self):
        ids = [spec.check_id for spec in MARKETING_CHECKS]
        assert len(ids) == 17
        assert len(set(ids)) == 17

    def test_every_scanner_literal_is_catalogued(self):
        literals = _scanner_literals()
        assert literals, "the literal grep found nothing — the regex is broken"
        assert literals <= {spec.check_id for spec in MARKETING_CHECKS}

    def test_every_catalogued_id_is_a_scanner_literal(self):
        assert {spec.check_id for spec in MARKETING_CHECKS} <= _scanner_literals()

    def test_dimension_split_brand7_competitive3_growth7(self):
        counts: dict[str, int] = {}
        for spec in MARKETING_CHECKS:
            assert spec.bucket == dimension_of(spec.check_id)
            counts[spec.bucket] = counts.get(spec.bucket, 0) + 1
        assert counts == {"brand": 7, "competitive": 3, "growth": 7}

    def test_every_check_has_title_and_target(self):
        assert all(spec.title and spec.target for spec in MARKETING_CHECKS)

    def test_bare_page_fires_only_catalogued_ids(self):
        catalogued = {spec.check_id for spec in MARKETING_CHECKS}
        assert set(_check_ids(_BARE)) <= catalogued


class TestRegexSplitIsBehaviourPreserving:
    def test_analytics_alternatives_unchanged(self):
        assert set(mkt_base.ANALYTICS_RE.pattern.split("|")) == set(
            _ORIGINAL_ANALYTICS_ALTS.split("|")
        )
        assert mkt_base.ANALYTICS_RE.flags & re.IGNORECASE

    def test_social_pattern_unchanged(self):
        assert mkt_base.SOCIAL_PROFILE_RE.pattern == _ORIGINAL_SOCIAL


_FACTS_PAGE = """
<html><head>
<meta property="og:image" content="https://acme.test/og.png">
<link rel="shortcut icon" href="/fav.ico">
<script src="https://static.hotjar.com/c/hotjar-1.js"></script>
<script src="https://cdn.cookiebot.com/uc.js"></script>
</head><body>
<header><img alt="Acme logo" src="/logo.png"></header>
<h1>Invoices for freelancers</h1><h2>Unlike spreadsheets, Acme files your taxes</h2>
<p>Trusted on Trustpilot. Refer a friend.</p>
<a href="https://github.com/acme">GitHub</a><a href="https://github.com/acme">GitHub again</a>
<a href="tel:+31201234567">Call</a>
</body></html>
"""


def _facts(html: str) -> dict:
    return dict(build_marketing_facts(BeautifulSoup(html, "lxml"), dimension_scores={}))


class TestMarketingFacts:
    def test_known_page_facts(self):
        facts = _facts(_FACTS_PAGE)
        pos = facts["positioning"]
        assert pos["differentiation_claim"] == "Unlike spreadsheets, Acme files your taxes"
        assert pos["fold_copy_excerpt"].startswith("Invoices for freelancers")
        assert pos["comparison_page"] is False
        assert pos["reputation_signals"] == ["Trustpilot"]
        brand = facts["brand"]
        assert brand["logo"] == {"present": True, "url": "/logo.png"}
        assert brand["og_image_url"] == "https://acme.test/og.png"
        assert brand["favicon"] == {"present": True, "url": "/fav.ico"}
        assert brand["about_link"] == {"present": False, "url": None}
        assert brand["contact"] == {"present": True, "email": None, "phone": "+31201234567"}
        channels = facts["channels"]
        assert channels["social_profiles"] == [
            {"platform": "github", "url": "https://github.com/acme"}
        ]
        assert channels["analytics_vendors"] == ["Hotjar"]
        assert channels["consent"] is True
        assert channels["referral"] is True
        assert channels["email_capture"] is False
        assert channels["pricing_visible"] is False

    def test_bare_page_reports_absence_not_fabrication(self):
        facts = _facts(_BARE)
        assert facts["positioning"]["fold_copy_excerpt"] is None
        assert facts["positioning"]["differentiation_claim"] is None
        assert facts["brand"]["og_image_url"] is None
        assert facts["brand"]["logo"] == {"present": False, "url": None}
        assert facts["channels"]["analytics_vendors"] == []
        assert facts["channels"]["social_profiles"] == []

    def test_facts_agree_with_checks(self):
        # A fact and its check use the same detector — they can never disagree.
        for html in (_BARE, _FACTS_PAGE):
            ids = set(_check_ids(html))
            facts = _facts(html)
            assert ("mkt-growth-no-analytics" in ids) == (
                not facts["channels"]["analytics_vendors"]
            )
            assert ("mkt-growth-no-social-presence" in ids) == (
                not facts["channels"]["social_profiles"]
            )
            assert ("mkt-brand-no-logo" in ids) == (not facts["brand"]["logo"]["present"])
            assert ("mkt-growth-no-pricing-visibility" in ids) == (
                not facts["channels"]["pricing_visible"]
            )
            assert ("mkt-competitive-weak-differentiation" in ids) == (
                facts["positioning"]["differentiation_claim"] is None
            )

    def test_not_measured_facts_are_permanent(self):
        facts = _facts(_BARE)
        assert facts["competitive_benchmark"] == {
            "measured": False,
            "reason": COMPETITIVE_BENCHMARK_REASON,
        }
        assert facts["channel_performance"] == {
            "measured": False,
            "reason": CHANNEL_PERFORMANCE_REASON,
        }

    def test_no_soup_is_unknown_not_empty(self):
        facts = build_marketing_facts(None, dimension_scores={})
        assert facts["positioning"] is None
        assert facts["brand"] is None
        assert facts["channels"] is None

    def test_extractor_exception_is_contained(self, monkeypatch):
        from src.domains.critique.services.scanners.marketing import facts as facts_mod

        def boom(_soup):
            raise RuntimeError("bad markup")

        monkeypatch.setattr(facts_mod, "_brand", boom)
        facts = _facts(_FACTS_PAGE)
        assert facts["brand"] is None
        assert facts["channels"] is not None


class TestDimensionSources:
    def test_rows_follow_dimension_weights(self):
        scores = {d: 80.0 for d in DIMENSION_WEIGHTS}
        rows = dimension_sources(scores)
        assert [r["dimension"] for r in rows] == list(DIMENSION_WEIGHTS)
        for row in rows:
            assert row["weight"] == DIMENSION_WEIGHTS[row["dimension"]]
            assert row["weighted_points"] == round(80.0 * row["weight"], 2)

    def test_sources_cover_every_weighted_dimension(self):
        assert set(DIMENSION_SOURCES) == set(DIMENSION_WEIGHTS)

    def test_borrowed_dimensions_are_reference_only_with_pointer(self):
        rows = {r["dimension"]: r for r in dimension_sources({})}
        assert {d for d, r in rows.items() if r["reference_only"]} == {
            "content",
            "conversion",
            "seo",
        }
        assert rows["content"]["report_pointer"] == "cro_audit"
        assert rows["conversion"]["report_pointer"] == "cro_audit"
        assert rows["seo"]["report_pointer"] == "seo_audit"
        assert rows["seo"]["source_engine"] == "seo"
        for own in ("competitive", "brand", "growth"):
            assert rows[own]["source_engine"] == "marketing"
            assert rows[own]["report_pointer"] is None

    def test_unscored_dimension_is_none_not_zero(self):
        rows = dimension_sources({})
        assert all(r["score"] is None and r["weighted_points"] is None for r in rows)
