"""Content-quality / readability SEO checks (group: ``content``).

Search engines and AI answer engines both favour substantive, readable copy.
These are Tier-0 deterministic measures over the page's *main* text:

- **Depth** — total word count (thin content ranks and gets cited poorly).
- **Readability** — Flesch Reading Ease + Gunning Fog grade, computed by the
  **self-written** ``readability_metrics`` in ``content_utils`` (vetted formulas,
  vowel-group syllable heuristic). Deliberately NOT ``textstat`` — it hard-depends
  on ``nltk`` (PYSEC-2026-597, unfixed), which CI ``pip-audit`` rejects.

Findings are emitted in the ``seo-content-<check>`` namespace so the audit
scorer groups them under ``content``. A page with substantial, readable copy
emits nothing and scores 100 for this group.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import (
    main_text,
    readability_metrics,
    words,
)

# Word-count thresholds. Below THIN a content page is unlikely to satisfy intent;
# below VERY_THIN it is effectively a stub.
_THIN_WORDS = 300
_VERY_THIN_WORDS = 150
# Only assess prose readability once there is enough text for the formulas to be
# meaningful. Gated at the very-thin floor so a stub page reports depth only
# (below this, the "very thin content" finding already covers it — no double-report).
_MIN_WORDS_FOR_READABILITY = _VERY_THIN_WORDS
# Flesch Reading Ease: higher = easier. <30 is "very difficult" (post-graduate).
_FRE_VERY_HARD = 30.0
_FRE_HARD = 50.0
# Gunning Fog: approximate US school grade needed to read on first pass.
_FOG_TOO_HIGH = 16.0


def check_readability(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    text = main_text(soup)
    word_count = len(words(text))

    if word_count < _VERY_THIN_WORDS:
        findings.append(
            _seo_finding(
                severity="high",
                title=f"Very thin content ({word_count} words)",
                description=(
                    f"The main content is only {word_count} words. Pages this short rarely "
                    "satisfy search intent or earn AI-answer citations, and read as stubs to "
                    "ranking systems."
                ),
                url=url,
                recommendation="Expand the primary content to substantively cover the topic (aim 600+ words for content pages).",
                check_id="seo-content-very-thin",
                evidence=f"{word_count} words",
                false_positive_risk="medium",
                remediation_effort="high",
            )
        )
    elif word_count < _THIN_WORDS:
        findings.append(
            _seo_finding(
                severity="medium",
                title=f"Thin content ({word_count} words)",
                description=(
                    f"The main content is {word_count} words — below the ~300-word floor where "
                    "pages start to compete for informational and commercial queries."
                ),
                url=url,
                recommendation="Add depth: cover sub-topics, add examples, answer related questions.",
                check_id="seo-content-thin",
                evidence=f"{word_count} words",
                false_positive_risk="medium",
                remediation_effort="high",
            )
        )

    if word_count >= _MIN_WORDS_FOR_READABILITY:
        metrics = readability_metrics(text)
        fre = metrics["flesch_reading_ease"]
        fog = metrics["gunning_fog"]

        if fre < _FRE_VERY_HARD:
            findings.append(
                _seo_finding(
                    severity="medium",
                    title="Content is very difficult to read",
                    description=(
                        f"Flesch Reading Ease is {fre:.0f} (very difficult, post-graduate level). "
                        "Hard-to-read copy loses users and is less quotable by AI answer engines."
                    ),
                    url=url,
                    recommendation="Shorten sentences, cut jargon, and prefer plain language (aim FRE 50-70).",
                    check_id="seo-content-hard-to-read",
                    evidence=f"Flesch Reading Ease {fre:.0f}",
                    false_positive_risk="medium",
                )
            )
        elif fre < _FRE_HARD:
            findings.append(
                _seo_finding(
                    severity="low",
                    title="Content is fairly difficult to read",
                    description=(
                        f"Flesch Reading Ease is {fre:.0f} (fairly difficult). A broader "
                        "reading level widens reach and improves engagement signals."
                    ),
                    url=url,
                    recommendation="Simplify phrasing where possible; break up long sentences.",
                    check_id="seo-content-hard-to-read",
                    evidence=f"Flesch Reading Ease {fre:.0f}",
                    false_positive_risk="medium",
                )
            )

        if fog > _FOG_TOO_HIGH:
            findings.append(
                _seo_finding(
                    severity="low",
                    title=f"High reading-grade level (Gunning Fog {fog:.0f})",
                    description=(
                        f"The copy needs roughly a grade-{fog:.0f} reading level. Most web "
                        "audiences read comfortably around grade 8-10."
                    ),
                    url=url,
                    recommendation="Reduce sentence length and multi-syllable words to lower the reading grade.",
                    check_id="seo-content-grade-too-high",
                    evidence=f"Gunning Fog {fog:.0f}",
                    false_positive_risk="medium",
                )
            )

    return findings
