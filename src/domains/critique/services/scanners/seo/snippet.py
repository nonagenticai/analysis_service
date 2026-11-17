"""Featured-snippet / AI-answer readiness checks (group: ``snippet``).

Google featured snippets and AI answer engines reward pages that pose a question
in a heading and answer it immediately in a tight 40-60 word paragraph (or a
clean list/table). These are Tier-0/Tier-2 structural checks — no model needed
to see whether a question heading is followed by a concise answer.

Findings are opportunities (mostly ``low`` severity): a content page that never
structures an answerable question forfeits snippet + "People Also Ask" real
estate. Namespace ``seo-snippet-<check>`` → group ``snippet``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import main_text, words

# A heading is "answerable" if it opens with an interrogative or ends with "?".
_QUESTION_RE = re.compile(
    r"^\s*(what|how|why|when|where|who|which|is|are|can|do|does|should|will|"
    r"could|would|did|was|were)\b",
    re.IGNORECASE,
)
# Only assess snippet structure on pages with real content.
_MIN_WORDS = 200
# Featured-snippet paragraph sweet spot (Google research: ~40-60 words).
_ANSWER_MIN = 40
_ANSWER_MAX = 60


def _is_question(text: str) -> bool:
    return bool(text) and (text.rstrip().endswith("?") or bool(_QUESTION_RE.match(text)))


def _answer_word_count(heading: Tag) -> int:
    """Word count of the first answer block following a heading, else 0.

    Walks the document *forward* (not just same-level siblings) so an answer in a
    separate wrapper element is still found, stopping at the next heading of any
    level so we never borrow the following section's copy.
    """
    for el in heading.find_all_next():
        if not isinstance(el, Tag):
            continue
        if el.name in {"h1", "h2", "h3", "h4"}:
            break  # next section started without an answer
        if el.name in {"p", "ul", "ol", "table"}:
            return len(words(el.get_text(" ", strip=True)))
    return 0


def check_snippet(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    if len(words(main_text(soup))) < _MIN_WORDS:
        return findings

    headings = [h for h in soup.find_all(["h2", "h3"]) if isinstance(h, Tag)]
    question_headings = [h for h in headings if _is_question(h.get_text(" ", strip=True))]

    if not question_headings:
        findings.append(
            _seo_finding(
                severity="low",
                title="No question-format headings for snippet capture",
                description=(
                    "No <h2>/<h3> is phrased as a question. Question headings answered "
                    "concisely are how pages win featured snippets and 'People Also Ask' slots."
                ),
                url=url,
                recommendation="Add question-style subheadings (e.g. 'How does X work?') with direct answers.",
                check_id="seo-snippet-no-question-heading",
                false_positive_risk="medium",
            )
        )
        return findings

    snippet_ready = any(
        _ANSWER_MIN <= _answer_word_count(h) <= _ANSWER_MAX for h in question_headings
    )
    if not snippet_ready:
        findings.append(
            _seo_finding(
                severity="low",
                title="Question headings lack a concise answer",
                description=(
                    f"{len(question_headings)} question heading(s) found, but none is immediately "
                    f"followed by a {_ANSWER_MIN}-{_ANSWER_MAX} word answer. Snippets are pulled "
                    "from a tight direct answer right after the question."
                ),
                url=url,
                recommendation=f"Answer each question heading in a {_ANSWER_MIN}-{_ANSWER_MAX} word paragraph directly beneath it.",
                check_id="seo-snippet-no-concise-answer",
                false_positive_risk="medium",
            )
        )

    if len(question_headings) >= 2 and not _has_faq_schema(soup):
        findings.append(
            _seo_finding(
                severity="low",
                title="Multiple Q&A sections without FAQPage schema",
                description=(
                    f"The page has {len(question_headings)} question sections but no FAQPage "
                    "structured data. FAQPage markup makes the Q&A eligible for rich results."
                ),
                url=url,
                recommendation="Add FAQPage JSON-LD structured data describing the questions and answers.",
                check_id="seo-snippet-no-faq-schema",
                false_positive_risk="medium",
            )
        )

    return findings


def _has_faq_schema(soup: BeautifulSoup) -> bool:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if isinstance(script, Tag) and "faqpage" in script.get_text().lower():
            return True
    return False
