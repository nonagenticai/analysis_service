"""Regression tests for the SEO-audit hardening pass (H1-H7).

Each test pins a specific defect the audit found so it cannot silently return:

- H1 robots.txt RFC-9309 group semantics (stacked UA, specific-over-wildcard, Allow).
- H2 keyword presence is word-boundary, not substring.
- H3 snippet answer is found across wrapper elements, not just same-level siblings.
- H4 thin pages emit a transparent "insufficient content" finding per word-gated group.
- H6 readability is gated at the very-thin floor (no double-report).
- H7 schema-coverage industry detection uses the page's own JSON-LD @types.

Pure deterministic checks over fixture HTML — no DB, no LLM, no network.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo import keyword as kw
from src.domains.critique.services.scanners.seo import readability as rd
from src.domains.critique.services.scanners.seo.content_utils import phrase_in_tokens
from src.domains.critique.services.scanners.seo.eeat import check_eeat
from src.domains.critique.services.scanners.seo.geo import _ai_bots_blocked, check_geo
from src.domains.critique.services.scanners.seo.internal_linking import check_internal_linking
from src.domains.critique.services.scanners.seo.schema_coverage import check_schema_coverage
from src.domains.critique.services.scanners.seo.snippet import check_snippet

_URL = "https://example.test/page"


def _ids(findings: list[dict]) -> set[str | None]:
    return {f.get("check_id") for f in findings}


def _page(inner: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><head></head><body>{inner}</body></html>", "lxml")


# --------------------------------------------------------------------------- H1
class TestRobotsRfcGrouping:
    def test_stacked_user_agents_share_the_following_rule(self):
        # RFC 9309 §2.2.1: adjacent User-agent lines share the rules that follow.
        robots = "User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n"
        blocked = _ai_bots_blocked(robots)
        assert "GPTBot" in blocked and "CCBot" in blocked

    def test_specific_group_overrides_wildcard(self):
        # A bot with its own group ignores "*" entirely (most-specific wins).
        robots = "User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow:\n"
        blocked = _ai_bots_blocked(robots)
        assert "GPTBot" not in blocked  # own group allows it
        assert "ClaudeBot" in blocked  # no own group -> falls back to "*"

    def test_allow_root_overrides_disallow(self):
        robots = "User-agent: GPTBot\nDisallow: /\nAllow: /\n"
        assert "GPTBot" not in _ai_bots_blocked(robots)


# --------------------------------------------------------------------------- H2
class TestKeywordWordBoundary:
    def test_phrase_in_tokens_rejects_substring(self):
        assert phrase_in_tokens("art", "smart marketing") is False
        assert phrase_in_tokens("art", "modern art gallery") is True

    def test_phrase_in_tokens_matches_multiword_run(self):
        assert phrase_in_tokens("cloud backup", "best cloud backup tools") is True
        assert phrase_in_tokens("cloud backup", "backup in the cloud") is False

    def test_phrase_in_tokens_empty_or_too_long(self):
        assert phrase_in_tokens("", "anything") is False
        assert phrase_in_tokens("a b c", "a b") is False

    def test_keyword_as_substring_of_title_word_still_flagged(self, monkeypatch):
        # primary "art" is NOT present as a word in "Smart marketing guide".
        monkeypatch.setattr(kw, "detect_primary_keyword", lambda _soup: "art")
        body = "<main><h1>Smart marketing</h1><p>" + ("word " * 130) + "</p></main>"
        soup = BeautifulSoup(
            f"<html><head><title>Smart marketing guide</title></head><body>{body}</body></html>",
            "lxml",
        )
        ids = _ids(kw.check_keyword(soup, _URL))
        assert "seo-keyword-missing-in-title" in ids


# --------------------------------------------------------------------------- H3
class TestSnippetAnswerAcrossWrappers:
    def test_answer_in_separate_wrapper_is_detected(self):
        answer = (
            "Fresh coffee tastes better because the beans keep their oils and aromas "
            "only briefly after roasting so grinding right before you brew keeps the "
            "flavour bright and lively instead of flat and stale the way pre ground "
            "material always tastes after sitting around in a warm open bag."
        )  # ~50 words -> inside the 40-60 snippet window
        filler = "<p>" + ("padding " * 180) + "</p>"
        inner = (
            f"<main><h1>Coffee brewing guide</h1>{filler}"
            f"<div><h2>Why does fresh coffee taste better?</h2></div>"
            f"<div><p>{answer}</p></div></main>"
        )
        ids = _ids(check_snippet(_page(inner), _URL))
        # The old same-level-sibling scan missed the wrapped answer and false-flagged.
        assert "seo-snippet-no-concise-answer" not in ids
        assert "seo-snippet-no-question-heading" not in ids


# --------------------------------------------------------------------------- H4
class TestThinContentTransparentPenalty:
    _THIN = "<main><h1>Hi</h1><p>Just a few short words on this stub page.</p></main>"

    def test_geo_flags_insufficient_content(self):
        assert "seo-geo-insufficient-content" in _ids(check_geo(_page(self._THIN), _URL))

    def test_eeat_flags_insufficient_content(self):
        assert "seo-eeat-insufficient-content" in _ids(check_eeat(_page(self._THIN), _URL))

    def test_internal_linking_flags_insufficient_content(self):
        assert "seo-linking-insufficient-content" in _ids(
            check_internal_linking(_page(self._THIN), _URL)
        )

    def test_rich_page_has_no_insufficient_content_findings(self):
        rich = "<main><h1>Guide</h1><p>" + ("content " * 260) + "</p></main>"
        ids = _ids(check_geo(_page(rich), _URL)) | _ids(check_eeat(_page(rich), _URL))
        assert not any(cid and cid.endswith("insufficient-content") for cid in ids)


# --------------------------------------------------------------------------- H6
class TestReadabilityGate:
    def test_readability_gated_at_very_thin_floor(self):
        # Below the very-thin floor the "very thin" finding already covers the page;
        # readability must not also fire (no double-report on 120-149-word pages).
        assert rd._MIN_WORDS_FOR_READABILITY == rd._VERY_THIN_WORDS


# --------------------------------------------------------------------------- H7
class TestSchemaIndustryFromJsonLd:
    def test_product_schema_drives_ecommerce_recommendations(self):
        head = '<script type="application/ld+json">{"@type":"Product","name":"Widget"}</script>'
        inner = f"<main><h1>Widget</h1><p>{'buy ' * 60}</p></main>"
        soup = BeautifulSoup(f"<html><head>{head}</head><body>{inner}</body></html>", "lxml")
        gap = next(
            f
            for f in check_schema_coverage(soup, _URL)
            if f.get("check_id") == "seo-schema-coverage-gap"
        )
        # ecommerce recommends Offer/BreadcrumbList — proof the @type drove classification.
        assert "Offer" in gap["recommendation"]
