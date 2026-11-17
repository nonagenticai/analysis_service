"""Keyword-strategy SEO checks (group: ``keyword``).

The "primary keyword" is detected deterministically: the highest-frequency
stop-word-filtered term in the page's main content (a self-written term-frequency
pass — no NLP dependency, no premium API). We then verify the classic placement
signals (title / H1 / intro) and flag keyword stuffing.

Search-*intent* classification of the detected keyword is intentionally NOT here
— it is the one genuinely fuzzy judgment, handled by an opt-in Tier-3 micro-LLM
call in a later phase. Everything in this module is Tier 0.
"""

from __future__ import annotations

from collections import Counter

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import (
    content_words,
    main_text,
    phrase_in_tokens,
    words,
)

# Need enough content for term frequency to be meaningful, and enough repetition
# to trust a term as the page's focus.
_MIN_CONTENT_WORDS = 60
_MIN_PRIMARY_COUNT = 3
_INTRO_WORDS = 100
# Density above this reads as keyword stuffing (over-optimization penalty).
_STUFFING_DENSITY = 0.03


def detect_primary_keyword(soup: BeautifulSoup) -> str | None:
    """Return the page's most likely primary keyword, or ``None`` if unclear.

    Deterministic: the most frequent stop-word-filtered unigram in the main
    content, provided the content is substantial and the term actually recurs.
    Shared with the opt-in intent classifier so both agree on the target term.
    """
    tokens = content_words(main_text(soup))
    if len(tokens) < _MIN_CONTENT_WORDS:
        return None
    counts = Counter(tokens)
    term, freq = counts.most_common(1)[0]
    if freq < _MIN_PRIMARY_COUNT:
        return None
    return term


def top_keywords(soup: BeautifulSoup, n: int = 15) -> list[str]:
    """Return the page's top-``n`` content keywords by term frequency.

    Multi-term sibling of :func:`detect_primary_keyword` on the same Tier-0 TF
    pass (stop-word-filtered unigrams over the main content). Used by the opt-in
    competitor content-gap analyzer to build deterministic keyword sets. Returns
    ``[]`` for thin content (same substance guard as the single-term detector),
    so a low-content page never yields a spurious keyword set. Pure, non-mutating.
    """
    tokens = content_words(main_text(soup))
    if len(tokens) < _MIN_CONTENT_WORDS:
        return []
    return [term for term, _ in Counter(tokens).most_common(n)]


def check_keyword(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    primary = detect_primary_keyword(soup)
    if primary is None:
        return findings

    text = main_text(soup)
    tokens = words(text)
    total = len(tokens)

    title = soup.find("title")
    title_text = title.get_text(" ", strip=True).lower() if isinstance(title, Tag) else ""
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True).lower() if isinstance(h1, Tag) else ""
    intro = " ".join(tokens[:_INTRO_WORDS])

    if title_text and not phrase_in_tokens(primary, title_text):
        findings.append(
            _seo_finding(
                severity="medium",
                title="Primary keyword missing from the title tag",
                description=(
                    f"The page's dominant term ('{primary}') does not appear in the <title>. "
                    "The title is the single strongest on-page relevance signal."
                ),
                url=url,
                recommendation=f"Work '{primary}' naturally into the title tag.",
                check_id="seo-keyword-missing-in-title",
                evidence=f"primary='{primary}'",
                false_positive_risk="medium",
            )
        )

    if h1_text and not phrase_in_tokens(primary, h1_text):
        findings.append(
            _seo_finding(
                severity="low",
                title="Primary keyword missing from the H1",
                description=(
                    f"The dominant term ('{primary}') is absent from the <h1>. Aligning the H1 "
                    "with the page's focus reinforces topical relevance."
                ),
                url=url,
                recommendation=f"Include '{primary}' in the H1 where it reads naturally.",
                check_id="seo-keyword-missing-in-h1",
                evidence=f"primary='{primary}'",
                false_positive_risk="medium",
            )
        )

    if total >= _INTRO_WORDS and not phrase_in_tokens(primary, intro):
        findings.append(
            _seo_finding(
                severity="low",
                title="Primary keyword missing from the opening content",
                description=(
                    f"'{primary}' does not appear in the first {_INTRO_WORDS} words. Stating the "
                    "topic early helps both readers and search engines confirm relevance."
                ),
                url=url,
                recommendation=f"Introduce '{primary}' within the opening paragraph.",
                check_id="seo-keyword-not-in-intro",
                evidence=f"primary='{primary}'",
                false_positive_risk="medium",
            )
        )

    if total:
        density = tokens.count(primary) / total
        if density > _STUFFING_DENSITY:
            findings.append(
                _seo_finding(
                    severity="medium",
                    title=f"Possible keyword stuffing ('{primary}' at {density * 100:.1f}%)",
                    description=(
                        f"'{primary}' makes up {density * 100:.1f}% of the copy (>{_STUFFING_DENSITY * 100:.0f}%). "
                        "Over-optimization reads as spam to search engines and hurts readability."
                    ),
                    url=url,
                    recommendation="Reduce repetition; use synonyms and natural phrasing.",
                    check_id="seo-keyword-stuffing",
                    evidence=f"density={density * 100:.1f}%",
                    false_positive_risk="medium",
                )
            )

    return findings
