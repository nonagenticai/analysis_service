"""Static check catalog for the CRO scanner (design 2026-10-17 X-G1).

Every ``cro-*`` check_id the scanner can emit, with its section, a short human
title and (where the check has a fixed threshold) the target it compares
against. ``build_checks`` turns this into the pass / fail / not_run inventory.
A test asserts every ``check_id="cro-…"`` literal in this package is listed here.

Form-friction checks carry ``requires="form"``: on a page with no conversion
form they never run, so they are reported ``not_run`` — never ``pass``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.domains.critique.services.scanners.cro.scoring import _section_of
from src.domains.scorecards.findings import CheckSpec

if TYPE_CHECKING:
    from src.domains.critique.services.scanners.cro.facts import CroFactsPayload

FORM_REQUIREMENT = "form"
NO_FORM_REASON = "No conversion form on this page"
FOOTER_REQUIREMENT = "footer"
NO_FOOTER_REASON = "No footer or closing section on this page"

# (check_id, title, target, requires)
_ROWS: tuple[tuple[str, str, str | None, str | None], ...] = (
    # form (8)
    ("cro-form-field-count", "Form field count", "3-5 fields", FORM_REQUIREMENT),
    ("cro-form-textarea", "No free-text textarea", "0 textareas", FORM_REQUIREMENT),
    ("cro-form-multiple-selects", "At most one dropdown", "<= 1 select", FORM_REQUIREMENT),
    (
        "cro-form-placeholder-label",
        "Persistent field labels",
        "0 placeholder-only",
        FORM_REQUIREMENT,
    ),
    ("cro-form-input-type", "Email field is type=email", None, FORM_REQUIREMENT),
    ("cro-form-autocomplete-missing", "Autocomplete tokens", None, FORM_REQUIREMENT),
    ("cro-form-required-friction", "Phone/company not required", None, FORM_REQUIREMENT),
    (
        "cro-form-no-multistep",
        "Long form split into steps",
        "<= 7 fields per step",
        FORM_REQUIREMENT,
    ),
    # hero (6)
    ("cro-hero-no-h1", "Page has an H1 headline", None, None),
    ("cro-hero-multiple-h1", "Exactly one H1", "1 H1", None),
    ("cro-hero-headline-length", "Headline length", "3-12 words, <= 70 chars", None),
    ("cro-hero-no-cta", "A call-to-action exists", None, None),
    ("cro-hero-nav-distraction", "Few competing nav links", "<= 6 links", None),
    ("cro-hero-no-trust-signal", "Trust signal present", None, None),
    # cta (4)
    ("cro-cta-generic-text", "Action-verb CTA copy", None, None),
    ("cro-cta-no-first-person", "First-person CTA copy", None, None),
    ("cro-cta-no-microcopy", "Reassurance microcopy near a CTA", None, None),
    ("cro-cta-single-on-long-page", "CTA repeated on a long page", None, None),
    # social (5)
    ("cro-social-no-testimonials", "Testimonials or reviews", None, None),
    ("cro-social-unnamed-testimonials", "Named testimonial attribution", None, None),
    ("cro-social-no-logos", "Client/logo trust strip", ">= 3 logos", None),
    ("cro-social-no-ratings", "Star ratings", None, None),
    ("cro-social-no-metrics", "Quantified proof or media mentions", None, None),
    # valueprop (5)
    ("cro-valueprop-no-quantified-benefit", "Quantified benefit", None, None),
    ("cro-valueprop-few-bullets", "Benefit bullets", ">= 3 list items", None),
    ("cro-valueprop-long-paragraph", "Short paragraphs", "<= 60 words", None),
    ("cro-valueprop-no-subheads", "Subheadings", None, None),
    ("cro-valueprop-reading-grade", "Reading grade", "<= grade 9", None),
    # objection (4)
    ("cro-objection-no-faq", "FAQ section", None, None),
    ("cro-objection-no-risk-reversal", "Risk reversal (guarantee / trial)", None, None),
    ("cro-objection-no-trust-links", "Security / privacy assurance", None, None),
    ("cro-objection-no-legitimacy", "Contact / legitimacy signals", None, None),
    # tech (6)
    ("cro-tech-render-blocking", "No render-blocking resources", None, None),
    ("cro-tech-cls-images", "Images have dimensions", None, None),
    ("cro-tech-large-dom", "DOM size", None, None),
    ("cro-tech-no-viewport", "Mobile viewport meta", None, None),
    ("cro-tech-small-fonts", "Readable mobile font sizes", None, None),
    ("cro-tech-small-touch-target", "Touch target size", None, None),
    # footer (2)
    ("cro-footer-no-final-cta", "Closing CTA", None, FOOTER_REQUIREMENT),
    ("cro-footer-no-contact", "Footer contact link", None, FOOTER_REQUIREMENT),
)


def _spec(check_id: str, title: str, target: str | None, requires: str | None) -> CheckSpec:
    section = _section_of(check_id)
    if section is None:  # pragma: no cover — guarded by the catalog test
        raise ValueError(f"malformed CRO check_id {check_id!r}")
    return CheckSpec(
        check_id=check_id, bucket=section, title=title, requires=requires, target=target
    )


CRO_CHECKS: tuple[CheckSpec, ...] = tuple(_spec(*row) for row in _ROWS)
CRO_CHECK_IDS: frozenset[str] = frozenset(spec.check_id for spec in CRO_CHECKS)

__all__ = [
    "CRO_CHECKS",
    "CRO_CHECK_IDS",
    "FORM_REQUIREMENT",
    "NO_FORM_REASON",
    "measured_from_facts",
]


def _fmt(value: object) -> str | None:
    return None if value is None else str(value)


def measured_from_facts(facts: CroFactsPayload) -> dict[str, str]:
    """``check_id -> measured value`` for the checks whose value a fact holds.

    Only values that were actually measured are returned (a ``None`` fact is
    omitted, so the check row reads ``measured: null`` rather than ``"None"``).
    A section missing from ``facts`` contributes nothing.
    """
    raw: dict[str, str | None] = {}
    if (hero := facts.get("hero")) is not None:
        words, chars = hero["h1_word_count"], hero["h1_length"]
        raw["cro-hero-multiple-h1"] = _fmt(hero["h1_count"])
        raw["cro-hero-headline-length"] = (
            f"{words} words / {chars} chars" if words is not None and chars is not None else None
        )
        raw["cro-hero-nav-distraction"] = _fmt(hero["nav_link_count"])
    if (value_prop := facts.get("value_prop")) is not None:
        grade = value_prop["reading_grade"]
        raw["cro-valueprop-few-bullets"] = _fmt(value_prop["bullets"])
        raw["cro-valueprop-reading-grade"] = f"grade {grade}" if grade is not None else None
    if (form := facts.get("form")) is not None:
        raw["cro-form-field-count"] = _fmt(form["field_count"])
        raw["cro-form-textarea"] = _fmt(form["textareas"])
        raw["cro-form-multiple-selects"] = _fmt(form["selects"])
        raw["cro-form-placeholder-label"] = _fmt(form["placeholder_only_labels"])
    return {cid: v for cid, v in raw.items() if v is not None}
