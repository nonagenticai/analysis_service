"""Regression tests for false positives in the cliffordtraining.nl critique report.

Every test here FAILS on the code that produced the customer report (origin/main
4d87fd2). The fixture is a trimmed live snapshot of https://www.cliffordtraining.nl/
(``tests/fixtures/critique/cliffordtraining_home.html``) plus its real response
headers; synthetic snippets pin the rule on both sides so a fix cannot pass by
simply deleting the check.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.critique.services import report_generator as rg
from src.domains.critique.services.scanners.perf import lcp, resources
from src.domains.critique.services.scanners.security_scanner import SecurityScanner
from src.domains.critique.services.scanners.seo import on_page, structural
from src.domains.critique.services.scanners.ux_reviewer import UXReviewer
from tests.llm_test_doubles import FakeWMClient, install_wm_factory

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "critique"
URL = "https://www.cliffordtraining.nl/"


@pytest.fixture(scope="module")
def html() -> str:
    return (FIXTURES / "cliffordtraining_home.html").read_text()


@pytest.fixture
def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


@pytest.fixture(scope="module")
def headers() -> httpx.Headers:
    return httpx.Headers(json.loads((FIXTURES / "cliffordtraining_home.headers.json").read_text()))


def _soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "lxml")


def _ids(findings: list[dict]) -> list[str]:
    return [f.get("check_id", "") for f in findings]


# --- 1. boolean attributes ---------------------------------------------------


def test_async_gtag_on_the_real_page_is_not_render_blocking(soup: BeautifulSoup) -> None:
    assert lcp.check_render_blocking(soup, URL) == []


@pytest.mark.parametrize(
    ("script", "blocking"),
    [
        ('<script async src="https://x.test/a.js"></script>', False),
        ('<script defer src="https://x.test/a.js"></script>', False),
        ('<script type="module" src="https://x.test/a.js"></script>', False),
        ('<script src="https://x.test/a.js"></script>', True),
    ],
)
def test_render_blocking_reads_boolean_attribute_presence(script: str, blocking: bool) -> None:
    findings = lcp.check_render_blocking(_soup(f"<html><head>{script}</head></html>"), URL)
    assert bool(findings) is blocking


def test_noscript_fallback_stylesheet_is_not_render_blocking(soup: BeautifulSoup) -> None:
    findings = resources.check_render_blocking_css(soup, URL)
    assert [f["title"] for f in findings] == ["1 render-blocking stylesheet(s)"]


# --- 2. JSON-LD @graph -------------------------------------------------------


def test_graph_container_is_not_missing_type(soup: BeautifulSoup) -> None:
    ids = _ids(structural.check_structured_data(soup, URL))
    assert "seo-structured-data-no-type" not in ids
    assert "seo-structured-data-no-name" not in ids


def test_untyped_graph_member_is_still_flagged() -> None:
    markup = (
        '<script type="application/ld+json">'
        '{"@context": "https://schema.org", "@graph": [{"@type": "Person", "name": "A"},'
        ' {"name": "no type"}]}</script>'
    )
    ids = _ids(structural.check_structured_data(_soup(markup), URL))
    assert ids.count("seo-structured-data-no-type") == 1


# --- 3. font display ---------------------------------------------------------


def test_preconnect_to_google_fonts_is_not_a_font_link(soup: BeautifulSoup) -> None:
    assert "perf-font-link-no-display" not in _ids(lcp.check_font_loading(soup, URL))


def test_font_stylesheet_without_display_is_flagged_once() -> None:
    href = "https://fonts.googleapis.com/css2?family=Inter"
    markup = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        f'<link rel="preload" as="style" href="{href}"><link rel="stylesheet" href="{href}">'
    )
    assert _ids(lcp.check_font_loading(_soup(markup), URL)) == ["perf-font-link-no-display"]


# --- 4. LLM UX findings vs the parsed page -----------------------------------


def _ux_output(*items: dict) -> dict:
    return {"dimensions": {"navigation": {"score": 50, "findings": list(items)}}}


def _item(title: str, severity: str = "high", **fields: str) -> dict:
    base = {"kind": "issue", "claim_type": "other", "scope": "page"}
    return {**base, "severity": severity, "title": title, "description": ".", **fields}


# As reported live: "The page has no contact form or way to reach the business."
_NO_CONTACT = _item("No contact form", "critical", claim_type="missing_contact_method")
_PRAISE = _item("Strong trust signals present", "info", kind="strength")
_OPINION = _item("Dense hero copy", "critical")
_NO_SEARCH = _item("No visible search functionality", "critical", claim_type="missing_search")


async def test_llm_claims_contradicted_by_the_page_are_dropped(html: str) -> None:
    wm = FakeWMClient(outputs=[_ux_output(_NO_CONTACT, _PRAISE, _OPINION, _NO_SEARCH)])
    reviewer = UXReviewer(wm_client=wm)
    findings = await reviewer.scan(html, URL)
    by_title = {f["title"]: (f["severity"], f["verified"]) for f in findings}
    # tel: and mailto: are on the page, so "no way to reach the business" is false.
    assert "No contact form" not in by_title
    # A strength is not a finding -- it is kept on the strengths list.
    assert [s["title"] for s in reviewer._last_strengths] == ["Strong trust signals present"]
    assert by_title == {
        "Dense hero copy": ("high", False),  # no evidence: critical -> high, not lower
        "No visible search functionality": ("critical", True),  # probe confirms absence
    }
    schema = wm.calls[0].output_schema or {}
    item_schema = schema["properties"]["dimensions"]["properties"]["navigation"]
    assert set(item_schema["properties"]["findings"]["items"]["required"]) >= {"kind", "scope"}
    assert "scope:" in wm.calls[0].message.split("HTML to analyze")[0]


async def test_contact_method_claim_confirmed_on_a_page_without_one() -> None:
    wm = FakeWMClient(outputs=[_ux_output(_NO_CONTACT)])
    page = "<html><body><h1>Hi</h1><p>Welcome.</p></body></html>"
    findings = await UXReviewer(wm_client=wm).scan(page, "https://x.test/about")
    assert [(f["title"], f["severity"]) for f in findings] == [("No contact form", "critical")]


async def test_llm_breadcrumb_claim_on_homepage_is_dropped(html: str) -> None:
    item = _item("Lack of breadcrumbs", "low", claim_type="missing_breadcrumbs")
    findings = await UXReviewer(wm_client=FakeWMClient(outputs=[_ux_output(item)])).scan(html, URL)
    assert findings == []


# --- 5. executive summary numbers --------------------------------------------

SCORES = {"performance": 59.7, "ux": 52.4, "security": 52.7}


def test_swapped_score_sentence_is_dropped_not_rewritten() -> None:
    text = (
        "Security headers are missing.\n\n"
        "Performance and UX score 52.4 and 59.7 respectively. "
        "UX scored 52.4 out of 100. TLS 1.3 is enabled."
    )
    assert rg.drop_swapped_score_sentences(text, SCORES) == (
        "Security headers are missing.\n\nUX scored 52.4 out of 100. TLS 1.3 is enabled."
    )


async def test_summary_leads_with_template_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    wm = FakeWMClient(
        outputs=[
            "Fix the security headers first.\n\n"
            "The security and performance scores are 59.7 and 52.7 respectively."
        ]
    )
    install_wm_factory(monkeypatch, lambda: wm)
    summary, _ = await rg.generate_summary([], URL, SCORES)
    assert summary == (
        "Overall score: 55.0/100 (Performance 59.7, Security 52.7, UX 52.4).\n\n"
        "Fix the security headers first."
    )
    # The model is given a ranking with grade words, never the numbers it could swap.
    prompt = wm.calls[0].message
    assert "1. UX: grade F (poor)\n2. Security: grade F (poor)\n3. Performance: grade F" in prompt
    assert not any(str(v) in prompt for v in (*SCORES.values(), 55.0))


# --- 6. duplicates -----------------------------------------------------------


def test_explicitly_equivalent_contact_findings_are_merged() -> None:
    findings = [
        {"category": "seo", "severity": "low", "title": "No contact information",
         "check_id": "seo-eeat-no-contact", "url": URL, "source_tool": "seo"},
        {"category": "ux", "severity": "medium", "title": "No way to reach the business",
         "claim_type": "missing_contact_method", "scope": "page", "url": URL,
         "source_tool": "ux_reviewer"},
        {"category": "ux", "severity": "medium", "title": "No phone number above the fold",
         "claim_type": "missing_contact_method", "scope": "region", "url": URL,
         "source_tool": "ux_reviewer"},
    ]  # fmt: skip
    out = rg.deduplicate_findings(findings)
    assert [f["title"] for f in out] == [
        "No way to reach the business",  # higher severity wins
        "No phone number above the fold",  # a region claim is not the page-level defect
    ]
    assert out[0]["confirmed_by"] == ["seo", "ux_reviewer"]


def test_breadcrumb_schema_not_required_on_homepage(soup: BeautifulSoup) -> None:
    assert structural.check_breadcrumb_schema(soup, URL) == []
    assert structural.check_breadcrumb_schema(soup, URL + "coaching/executive-coaching.html")


# --- 7. noise ----------------------------------------------------------------


def test_sri_not_demanded_for_dynamic_vendor_resources(soup: BeautifulSoup) -> None:
    assert SecurityScanner()._check_sri(soup, URL) == []


def test_sri_still_demanded_for_a_static_cdn_asset() -> None:
    markup = '<script src="https://cdn.jsdelivr.net/npm/x@1.0.0/x.min.js"></script>'
    assert _ids(SecurityScanner()._check_sri(_soup(markup), URL)) == ["sec-sri-missing"]


def test_github_pages_headers_get_hosting_context(headers: httpx.Headers) -> None:
    findings = {f["check_id"]: f for f in SecurityScanner()._check_security_headers(headers, URL)}
    hsts = findings["sec-missing-hsts"]
    assert hsts["severity"] == "medium"
    assert "GitHub Pages" in hsts["description"]
    assert "Cloudflare" in hsts["recommendation"]
    assert "meta http-equiv" in findings["sec-missing-csp"]["recommendation"]


def test_hsts_stays_high_on_a_host_that_can_send_headers() -> None:
    findings = SecurityScanner()._check_security_headers(httpx.Headers({"server": "nginx"}), URL)
    assert next(f for f in findings if f["check_id"] == "sec-missing-hsts")["severity"] == "high"


def test_tls_at_65_days_is_not_a_finding() -> None:
    assert SecurityScanner._tls_expiry_findings(URL, "Nov 18 11:27:20 2026 GMT", 65) == []


def test_footer_logo_is_not_a_lazy_loading_miss(soup: BeautifulSoup) -> None:
    assert lcp.check_lazy_loading(soup, URL) == []


def test_below_fold_raster_image_without_lazy_is_flagged() -> None:
    imgs = "".join(f'<img src="/p{i}.jpg">' for i in range(4))
    markup = f'<header><img src="/logo.svg"></header><main>{imgs}</main>'
    findings = lcp.check_lazy_loading(_soup(markup), URL)
    assert findings[0]["title"] == "2 below-fold images without lazy loading"


def test_hero_fetchpriority_is_read_from_the_hero_not_the_logo(soup: BeautifulSoup) -> None:
    assert lcp.hero_fetchpriority_findings(soup, URL) == []


# --- 8. keyword overlap by page language -------------------------------------


def test_dutch_function_words_are_not_keyword_overlap() -> None:
    markup = (
        '<html lang="nl"><head><title>Coaching voor de leider</title></head>'
        "<body><h1>Voor de toekomst</h1></body></html>"
    )
    assert _ids(on_page.check_keyword_alignment(_soup(markup), URL)) == ["seo-keyword-alignment"]


def test_unsupported_language_is_skipped() -> None:
    markup = '<html lang="de"><head><title>Katzen</title></head><body><h1>Hunde</h1></body></html>'
    assert on_page.check_keyword_alignment(_soup(markup), URL) == []
