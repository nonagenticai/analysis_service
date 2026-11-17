"""Value-proposition / message-clarity CRO checks (all deterministic).

Evidence: 79% of users scan rather than read (NN/G); quantified/ultra-specific
copy converts; short paragraphs, bullets, and subheads aid scanning; low reading
grade widens the audience. Reading grade is a self-computed Flesch-Kincaid grade
(no third-party dependency) — the persuasiveness judgment is left to the opt-in
LLM tier.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    QUANT_BENEFIT_RE,
    _cro_finding,
)

_LONG_PARAGRAPH_WORDS = 60
_MIN_WORDS_FOR_GRADE = 120
_MAX_READING_GRADE = 9.0

_WORD_RE = re.compile(r"[A-Za-z]+")
_SENTENCE_RE = re.compile(r"[.!?]+")
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+")


def _count_syllables(word: str) -> int:
    """Heuristic syllable count for one lowercase alphabetic word (min 1)."""
    groups = _VOWEL_GROUP_RE.findall(word)
    count = len(groups)
    if word.endswith("e") and count > 1:  # drop a common silent 'e'
        count -= 1
    return max(count, 1)


def _flesch_kincaid_grade(text: str) -> float | None:
    """Self-contained Flesch-Kincaid grade level; ``None`` if text is too small.

    FK grade = 0.39*(words/sentences) + 11.8*(syllables/words) - 15.59.
    """
    words = _WORD_RE.findall(text.lower())
    if len(words) < _MIN_WORDS_FOR_GRADE:
        return None
    sentences = max(len([s for s in _SENTENCE_RE.split(text) if s.strip()]), 1)
    syllables = sum(_count_syllables(w) for w in words)
    return 0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59


def check_value_prop(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []

    # Headline + first paragraphs = the value-prop surface.
    h1 = soup.find("h1")
    headline = h1.get_text(" ", strip=True) if isinstance(h1, Tag) else ""
    lead_paras = [
        p.get_text(" ", strip=True) for p in soup.find_all("p", limit=5) if isinstance(p, Tag)
    ]
    value_surface = " ".join([headline, *lead_paras])

    # 1. Quantified benefit tokens.
    if value_surface.strip() and not QUANT_BENEFIT_RE.search(value_surface):
        findings.append(
            _cro_finding(
                severity="low",
                title="Value proposition is not quantified",
                description=(
                    "The headline and lead copy contain no specific numbers (%, $, time "
                    "saved, multiples). Ultra-specific claims are more persuasive than "
                    "vague ones."
                ),
                url=url,
                recommendation="Add a concrete metric ('Cut onboarding time by 40%').",
                check_id="cro-valueprop-no-quantified-benefit",
            )
        )

    # 2. Benefit bullets.
    bullets = [li for li in soup.find_all("li") if isinstance(li, Tag)]
    if len(bullets) < 3:
        findings.append(
            _cro_finding(
                severity="low",
                title="Few or no benefit bullets",
                description=(
                    f"Only {len(bullets)} list item(s) found. Scannable bullet lists let "
                    "visitors grasp benefits without reading prose (79% scan, NN/G)."
                ),
                url=url,
                recommendation="Summarise the top 3-5 benefits as a bulleted list.",
                check_id="cro-valueprop-few-bullets",
            )
        )

    # 3. Overly long paragraphs.
    long_paras = [
        p
        for p in soup.find_all("p")
        if isinstance(p, Tag) and len(p.get_text(" ", strip=True).split()) > _LONG_PARAGRAPH_WORDS
    ]
    if long_paras:
        findings.append(
            _cro_finding(
                severity="low",
                title=f"{len(long_paras)} long paragraph(s)",
                description=(
                    f"{len(long_paras)} paragraph(s) exceed {_LONG_PARAGRAPH_WORDS} words. "
                    "Dense text is skipped by scanning readers."
                ),
                url=url,
                recommendation="Split long paragraphs; one idea each, ~40 words max.",
                check_id="cro-valueprop-long-paragraph",
            )
        )

    # 4. Subheads as scan anchors.
    if not soup.find(["h2", "h3"]):
        findings.append(
            _cro_finding(
                severity="low",
                title="No subheadings",
                description=(
                    "No <h2>/<h3> subheadings were found. Subheads anchor the F-pattern "
                    "scan and let visitors navigate the message."
                ),
                url=url,
                recommendation="Break the page into sections with descriptive subheads.",
                check_id="cro-valueprop-no-subheads",
            )
        )

    # 5. Reading grade (textstat / Flesch-Kincaid).
    findings.extend(_check_reading_grade(soup, url))

    return findings


def _check_reading_grade(soup: BeautifulSoup, url: str) -> list[dict]:
    body = soup.find("body") or soup
    text = body.get_text(" ", strip=True)
    grade = _flesch_kincaid_grade(text)
    if grade is None or grade <= _MAX_READING_GRADE:
        return []
    return [
        _cro_finding(
            severity="low",
            title=f"Reading grade {grade:.0f} is high",
            description=(
                f"The copy reads at a grade-{grade:.0f} level. Landing-page copy converts "
                "best around grade 7-8; complex language loses scanning readers."
            ),
            url=url,
            recommendation="Simplify sentences and vocabulary toward a grade 7-8 reading level.",
            check_id="cro-valueprop-reading-grade",
            evidence=f"Flesch-Kincaid grade {grade:.1f}",
            false_positive_risk="high",
        )
    ]
