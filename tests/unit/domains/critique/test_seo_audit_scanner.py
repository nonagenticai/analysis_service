"""Unit tests for the audit-grade SEO scanner groups (content/keyword/snippet).

Pure deterministic checks over fixture HTML — no DB, no LLM, no network. Assert
specific ``check_id``s fire (or don't), which is robust regardless of the base
19-check findings that ``scan_audit`` also produces.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo import SEOScanner
from src.domains.critique.services.scanners.seo.keyword import detect_primary_keyword

_SCANNER = SEOScanner()

# ~330 words of readable prose; "coffee" is the dominant term and appears in the
# title, H1 and opening sentence. One question H2 is answered in ~45 words, and
# FAQPage schema is present.
_GOOD_BODY = """
<main>
<h1>How to brew better coffee at home</h1>
<p>Coffee is one of the most popular drinks in the world, and making great coffee
at home is simple once you learn a few basics. This guide explains how to make a
brew that tastes fresh, balanced and smooth every single morning without any
expensive equipment or special training at all. Anyone can do it with patience.</p>
<h2>Why does fresh coffee taste better?</h2>
<p>Fresh coffee tastes better because the roasted material keeps its natural oils
and aromas for only a short time after roasting. When you grind just before
brewing, the flavour stays bright and the drink feels far more lively and rich
than stale, pre-ground material poured straight from a bag that has been open.</p>
<h2>Simple brewing steps</h2>
<ul><li>Weigh the roast</li><li>Grind it fresh</li><li>Heat clean water</li>
<li>Pour it slowly</li><li>Serve and enjoy</li></ul>
<p>Good water matters as much as a good roast. Soft, clean water lets the flavour
of the coffee come through clearly without any metallic edge or bitterness. Warm
the mug first so the drink stays hot while you sip it slowly and relax at the
quiet start of the day, before the rush of everything else begins to build up.</p>
<p>Temperature and timing also change the result more than most people expect. If
the water is too hot it scorches the grind, and if it is too cool the flavour
comes out flat and thin. A gentle, steady pour helps the coffee bloom evenly and
release its best character into every single mouthful that you take and savour.</p>
<p>With a little practice, almost anyone can make coffee that rivals a good local
cafe. Keep all of your gear clean, store the roast away from light and heat, and
buy small amounts often so that every cup of coffee you make at home is as fresh
and flavourful as it can possibly be for you and the people you share it with.</p>
</main>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage"}</script>
"""

_GOOD_HTML = f"<html><head><title>How to brew better coffee at home</title></head><body>{_GOOD_BODY}</body></html>"

_THIN_HTML = "<html><head><title>Hello</title></head><body><main><h1>Hi</h1><p>Just a few words here.</p></main></body></html>"

# Long content whose dominant term ("coffee") is absent from the title.
_KW_MISSING_TITLE = (
    f"<html><head><title>Our morning ritual guide</title></head><body>{_GOOD_BODY}</body></html>"
)

# Long content, no question-format headings at all.
_NO_QUESTION = _GOOD_HTML.replace("Why does fresh coffee taste better?", "Fresh flavour notes")

# A question heading followed immediately by another heading (no answer paragraph).
_NO_ANSWER_BODY = _GOOD_BODY.replace(
    "<h2>Why does fresh coffee taste better?</h2>\n<p>Fresh coffee tastes better",
    "<h2>Why does fresh coffee taste better?</h2>\n<h2>Skip</h2>\n<p>Fresh coffee tastes better",
)
_NO_ANSWER_HTML = f"<html><head><title>How to brew better coffee at home</title></head><body>{_NO_ANSWER_BODY}</body></html>"


def _check_ids(html: str) -> set[str | None]:
    findings = _SCANNER.scan_audit(html, "https://example.test/coffee")
    return {f.get("check_id") for f in findings}


class TestPrimaryKeyword:
    def test_detects_dominant_term(self):
        soup = BeautifulSoup(_GOOD_HTML, "lxml")
        assert detect_primary_keyword(soup) == "coffee"

    def test_none_when_too_short(self):
        soup = BeautifulSoup(_THIN_HTML, "lxml")
        assert detect_primary_keyword(soup) is None


class TestContentGroup:
    def test_thin_page_flagged(self):
        assert "seo-content-very-thin" in _check_ids(_THIN_HTML)

    def test_good_page_not_thin(self):
        ids = _check_ids(_GOOD_HTML)
        assert "seo-content-thin" not in ids
        assert "seo-content-very-thin" not in ids


class TestKeywordGroup:
    def test_keyword_present_in_title_no_finding(self):
        assert "seo-keyword-missing-in-title" not in _check_ids(_GOOD_HTML)

    def test_keyword_missing_from_title_flagged(self):
        assert "seo-keyword-missing-in-title" in _check_ids(_KW_MISSING_TITLE)


class TestSnippetGroup:
    def test_answered_question_not_flagged(self):
        ids = _check_ids(_GOOD_HTML)
        assert "seo-snippet-no-question-heading" not in ids
        assert "seo-snippet-no-concise-answer" not in ids
        assert "seo-snippet-no-faq-schema" not in ids  # FAQPage schema present

    def test_no_question_heading_flagged(self):
        assert "seo-snippet-no-question-heading" in _check_ids(_NO_QUESTION)

    def test_question_without_answer_flagged(self):
        assert "seo-snippet-no-concise-answer" in _check_ids(_NO_ANSWER_HTML)


class TestBackwardCompat:
    def test_plain_scan_unchanged_no_audit_checks(self):
        # The base scan() must NOT emit any audit-group findings.
        base_ids = {f.get("check_id") for f in _SCANNER.scan(_GOOD_HTML, "https://example.test")}
        assert not any(
            cid
            and cid.startswith(
                (
                    "seo-content-",
                    "seo-keyword-missing",
                    "seo-keyword-stuffing",
                    "seo-keyword-not",
                    "seo-snippet-",
                )
            )
            for cid in base_ids
        )
