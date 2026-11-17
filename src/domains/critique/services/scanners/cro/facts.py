"""Measured CRO page facts (design 2026-10-17 CRO-G1..G3 + the on-page journey).

The values behind the checks, so the conversion report can quote the real H1,
list every CTA in page order, and draw the form field table instead of only
saying "a check failed". Built from the already-parsed soup with the SAME
helpers the checks use (``cta_elements``/``cta_text``, ``_primary_form``/
``_countable_fields``/``_has_label``, ``proof_signals``, ``objection_signals``,
``nav_link_elements``) so a fact can never contradict its check.

A value that cannot be determined from server-side HTML is ``None`` — never a
guess. ``hero.has_cta_above_fold`` is therefore always ``None`` (there is no
layout geometry in an httpx fetch); ``hero.cta_before_first_h2`` is the
document-order proxy, named for what it actually measures.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    _EMAIL_FIELD_RE,
    _STEP_CLASS_RE,
    ACTION_VERB_RE,
    FIRST_PERSON_RE,
    GENERIC_CTA_RE,
    MICROCOPY_RE,
    QUANT_BENEFIT_RE,
    TRUST_BADGE_RE,
    cta_elements,
    cta_text,
    page_text,
)
from src.domains.critique.services.scanners.cro.form_friction import (
    _has_label,
    _primary_form,
)
from src.domains.critique.services.scanners.cro.hero import nav_link_elements
from src.domains.critique.services.scanners.cro.objection import objection_signals
from src.domains.critique.services.scanners.cro.scoring import SECTION_WEIGHTS
from src.domains.critique.services.scanners.cro.social_proof import proof_signals
from src.domains.critique.services.scanners.cro.value_prop import _flesch_kincaid_grade

MAX_CTAS = 25
MAX_FIELDS = 30
_TEXT_MAX = 300

# Attention(hero) -> Understanding(valueprop) -> Trust(social+objection) ->
# Action(cta) -> Commitment(form) -> Fallback(footer); ``tech`` overlays all.
JOURNEY_STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("attention", "Attention", ("hero",)),
    ("understanding", "Understanding", ("valueprop",)),
    ("trust", "Trust", ("social", "objection")),
    ("action", "Action", ("cta",)),
    ("commitment", "Commitment", ("form",)),
    ("fallback", "Fallback", ("footer",)),
)
FRICTION_SECTION = "tech"


# ---------------------------------------------------------------------------
# payload shapes (mirror the CroFacts pydantic models in cro/schemas.py)
# ---------------------------------------------------------------------------


class HeroFacts(TypedDict):
    h1_text: str | None
    h1_length: int | None
    h1_word_count: int | None
    h1_count: int
    has_cta_above_fold: bool | None
    cta_before_first_h2: bool
    nav_link_count: int
    trust_signal: bool
    trust_signal_match: str | None


class ValuePropFacts(TypedDict):
    lead_text: str | None
    bullets: int
    quantified_benefit: bool | None
    quantified_benefit_match: str | None
    reading_grade: float | None


class CtaFact(TypedDict):
    text: str
    tag: str
    href_kind: str
    position_index: int
    action_verb: bool
    first_person: bool
    generic: bool
    has_microcopy: bool


class CtaSummary(TypedDict):
    count: int
    generic_count: int
    generic_ratio: float | None
    first_person_count: int
    with_microcopy_count: int
    truncated: bool


class FormFieldFact(TypedDict):
    type: str
    name: str | None
    label: str | None
    required: bool
    autocomplete: str | None


class FormFacts(TypedDict):
    present: bool
    field_count: int | None
    required_count: int | None
    textareas: int | None
    selects: int | None
    placeholder_only_labels: int | None
    email_type_ok: bool | None
    autocomplete: bool | None
    multistep: bool | None
    fields: list[FormFieldFact]


class TrustFacts(TypedDict):
    testimonials: bool
    named_testimonials: bool | None
    logos: bool
    ratings: bool
    metrics: bool
    faq: bool
    risk_reversal: bool
    security_assurance: bool
    contact: bool


class JourneyStage(TypedDict):
    stage: str
    label: str
    sections: list[str]
    section_scores: dict[str, float | None]
    score: float | None
    measured: bool


class JourneyFriction(TypedDict):
    section: str
    score: float | None


class Journey(TypedDict):
    stages: list[JourneyStage]
    friction: JourneyFriction
    weakest_stage: str | None


class CroFactsPayload(TypedDict, total=False):
    """The whole ``facts`` block. ``total=False``: ``{}`` = facts not built, and
    ``journey`` is present only when the caller passed the section scores."""

    hero: HeroFacts
    value_prop: ValuePropFacts
    ctas: list[CtaFact]
    cta_summary: CtaSummary
    form: FormFacts
    trust: TrustFacts
    journey: Journey


def _cap(text: str) -> str:
    return text[:_TEXT_MAX]


def _document_order(soup: BeautifulSoup) -> dict[int, int]:
    return {id(el): i for i, el in enumerate(soup.find_all(True))}


def ordered_ctas(soup: BeautifulSoup) -> list[Tag]:
    """``cta_elements`` re-sorted into document order (it collects by tag type)."""
    order = _document_order(soup)
    return sorted(cta_elements(soup), key=lambda el: order.get(id(el), len(order)))


# ---------------------------------------------------------------------------
# hero + value proposition
# ---------------------------------------------------------------------------


def _cta_before_first_h2(soup: BeautifulSoup, ctas: list[Tag]) -> bool:
    if not ctas:
        return False
    h2 = soup.find("h2")
    if not isinstance(h2, Tag):
        return True
    order = _document_order(soup)
    return order.get(id(ctas[0]), 0) < order.get(id(h2), 0)


def hero_facts(soup: BeautifulSoup, ctas: list[Tag], text: str) -> HeroFacts:
    h1s = [h for h in soup.find_all("h1") if isinstance(h, Tag)]
    h1_text = _cap(h1s[0].get_text(" ", strip=True)) if h1s else None
    full = h1s[0].get_text(" ", strip=True) if h1s else None
    trust = TRUST_BADGE_RE.search(text)
    return {
        "h1_text": h1_text,
        "h1_length": len(full) if full is not None else None,
        "h1_word_count": len(full.split()) if full is not None else None,
        "h1_count": len(h1s),
        "has_cta_above_fold": None,
        "cta_before_first_h2": _cta_before_first_h2(soup, ctas),
        "nav_link_count": len(nav_link_elements(soup)),
        "trust_signal": trust is not None,
        "trust_signal_match": trust.group(0) if trust else None,
    }


def value_prop_facts(soup: BeautifulSoup) -> ValuePropFacts:
    h1 = soup.find("h1")
    headline = h1.get_text(" ", strip=True) if isinstance(h1, Tag) else ""
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p", limit=5) if isinstance(p, Tag)]
    surface = " ".join([headline, *paras])
    lead = next((p for p in paras if p), None)
    quant = QUANT_BENEFIT_RE.search(surface) if surface.strip() else None
    body = soup.find("body") or soup
    grade = _flesch_kincaid_grade(body.get_text(" ", strip=True))
    return {
        "lead_text": _cap(lead) if lead else None,
        "bullets": len([li for li in soup.find_all("li") if isinstance(li, Tag)]),
        # None = no headline/lead copy at all, so "quantified?" has no answer.
        "quantified_benefit": (quant is not None) if surface.strip() else None,
        "quantified_benefit_match": quant.group(0) if quant else None,
        "reading_grade": round(grade, 1) if grade is not None else None,
    }


# ---------------------------------------------------------------------------
# CTA inventory
# ---------------------------------------------------------------------------


def _link_kind(href: str, page_host: str) -> str:
    if not href:
        return "none"
    low = href.lower()
    for prefix, kind in (("#", "anchor"), ("mailto:", "mailto"), ("tel:", "tel")):
        if low.startswith(prefix):
            return kind
    if low.startswith("javascript:"):
        return "javascript"
    parsed = urlparse(href)
    if not parsed.netloc:
        return "internal"
    if not page_host:
        return "absolute"  # no page URL (inline HTML) -> internal vs external unknowable
    host = parsed.netloc.lower().removeprefix("www.")
    return "internal" if host == page_host else "external"


def _href_kind(el: Tag, page_host: str) -> str:
    if el.name == "a":
        return _link_kind(str(el.get("href", "")).strip(), page_host)
    el_type = str(el.get("type", "")).lower()
    if el_type == "submit" or (el.name == "button" and not el_type and el.find_parent("form")):
        return "form_submit"
    return "button"


def _near_text(el: Tag) -> str:
    parent = el.parent
    return parent.get_text(" ", strip=True) if isinstance(parent, Tag) else ""


def cta_facts(ctas: list[Tag], url: str) -> tuple[list[CtaFact], CtaSummary]:
    """Every CTA in page order (capped at ``MAX_CTAS``) plus a whole-page summary."""
    page_host = urlparse(url).netloc.lower().removeprefix("www.") if url else ""
    rows: list[CtaFact] = []
    for index, el in enumerate(ctas):
        text = cta_text(el)
        rows.append(
            {
                "text": _cap(text),
                "tag": el.name,
                "href_kind": _href_kind(el, page_host),
                "position_index": index,
                "action_verb": bool(ACTION_VERB_RE.search(text)),
                "first_person": bool(FIRST_PERSON_RE.search(text)),
                "generic": bool(GENERIC_CTA_RE.match(text.strip())) if text else False,
                "has_microcopy": bool(MICROCOPY_RE.search(_near_text(el))),
            }
        )
    count = len(rows)
    generic = sum(1 for r in rows if r["generic"])
    summary: CtaSummary = {
        "count": count,
        "generic_count": generic,
        "generic_ratio": round(generic / count, 3) if count else None,
        "first_person_count": sum(1 for r in rows if r["first_person"]),
        "with_microcopy_count": sum(1 for r in rows if r["has_microcopy"]),
        "truncated": count > MAX_CTAS,
    }
    return rows[:MAX_CTAS], summary


# ---------------------------------------------------------------------------
# form field table
# ---------------------------------------------------------------------------


def _is_required(field: Tag) -> bool:
    return field.get("required") is not None or field.get("aria-required") == "true"


def _field_type(field: Tag) -> str:
    if field.name == "input":
        return str(field.get("type", "text")).lower() or "text"
    return str(field.name)


def _label_text(form: Tag, field: Tag) -> str | None:
    """The field's accessible label text, or ``None`` when it has no label."""
    if aria := field.get("aria-label"):
        return _cap(str(aria).strip()) or None
    labelled_by = field.get("aria-labelledby")
    if labelled_by:
        ref = form.find(id=str(labelled_by).split()[0])
        if isinstance(ref, Tag):
            return _cap(ref.get_text(" ", strip=True)) or None
    fid = field.get("id")
    label = form.find("label", attrs={"for": fid}) if fid else None
    if label is None:
        label = field.find_parent("label")
    if isinstance(label, Tag):
        return _cap(label.get_text(" ", strip=True)) or None
    return None


def _is_email_field(field: Tag) -> bool:
    name_id = f"{field.get('name', '')} {field.get('id', '')} {field.get('placeholder', '')}"
    return bool(_EMAIL_FIELD_RE.search(name_id))


def _field_row(form: Tag, field: Tag) -> FormFieldFact:
    autocomplete = field.get("autocomplete")
    return {
        "type": _field_type(field),
        "name": str(field.get("name") or "") or None,
        "label": _label_text(form, field),
        "required": _is_required(field),
        "autocomplete": str(autocomplete) if autocomplete else None,
    }


def _no_form() -> FormFacts:
    """A fresh no-form payload (fresh list per call: never a shared mutable)."""
    return {
        "present": False,
        "field_count": None,
        "required_count": None,
        "textareas": None,
        "selects": None,
        "placeholder_only_labels": None,
        "email_type_ok": None,
        "autocomplete": None,
        "multistep": None,
        "fields": [],
    }


def form_facts(soup: BeautifulSoup) -> FormFacts:
    """The primary conversion form, or ``present=False`` with every metric ``None``."""
    form, fields = _primary_form(soup)
    if form is None or not fields:
        return _no_form()
    inputs = [f for f in fields if f.name == "input"]
    emails = [f for f in inputs if _is_email_field(f)]
    return {
        "present": True,
        "field_count": len(fields),
        "required_count": sum(1 for f in fields if _is_required(f)),
        "textareas": sum(1 for f in fields if f.name == "textarea"),
        "selects": sum(1 for f in fields if f.name == "select"),
        "placeholder_only_labels": sum(
            1 for f in inputs if f.get("placeholder") and not _has_label(form, f)
        ),
        # None = there is no email field, so "is it type=email?" has no answer.
        "email_type_ok": (
            all(str(f.get("type", "text")).lower() == "email" for f in emails) if emails else None
        ),
        "autocomplete": any(f.get("autocomplete") for f in inputs),
        "multistep": bool(
            form.find("fieldset")
            or form.find(attrs={"class": _STEP_CLASS_RE})
            or form.find("progress")
        ),
        "fields": [_field_row(form, f) for f in fields[:MAX_FIELDS]],
    }


# ---------------------------------------------------------------------------
# trust inventory + journey
# ---------------------------------------------------------------------------


def trust_facts(soup: BeautifulSoup, text: str) -> TrustFacts:
    proof = proof_signals(soup, text)
    objection = objection_signals(soup, text)
    return {
        "testimonials": proof["testimonials"],
        # Attribution is only judged when there are testimonials to attribute.
        "named_testimonials": proof["named"] if proof["testimonials"] else None,
        "logos": proof["logos"],
        "ratings": proof["ratings"],
        "metrics": proof["metrics"],
        "faq": objection["faq"],
        "risk_reversal": objection["risk_reversal"],
        "security_assurance": objection["security_assurance"],
        "contact": objection["contact"],
    }


def _stage_score(scores: Mapping[str, float | None], sections: tuple[str, ...]) -> float | None:
    measured = {s: v for s in sections if (v := scores.get(s)) is not None}
    weight = sum(SECTION_WEIGHTS.get(s, 0.0) for s in measured)
    if not measured or weight <= 0:
        return None
    return round(sum(v * SECTION_WEIGHTS.get(s, 0.0) for s, v in measured.items()) / weight, 1)


def build_journey(subsection_scores: Mapping[str, float | None]) -> Journey:
    """The 6-stage on-page journey with section scores, ``tech`` as friction overlay.

    A stage score is the section-weight mean of its MEASURED sections (``None``
    when none was measured). ``weakest_stage`` is the lowest-scoring measured
    stage below 100 (first in journey order on a tie), or ``None`` when no
    measured stage lost points.
    """
    stages: list[JourneyStage] = []
    for key, label, sections in JOURNEY_STAGES:
        score = _stage_score(subsection_scores, sections)
        stages.append(
            {
                "stage": key,
                "label": label,
                "sections": list(sections),
                "section_scores": {s: subsection_scores.get(s) for s in sections},
                "score": score,
                "measured": score is not None,
            }
        )
    # Only a stage that lost points can be the "biggest leak"; all-100 -> None.
    leaking = [s for s in stages if s["score"] is not None and s["score"] < 100.0]
    weakest = min(leaking, key=lambda s: s["score"]) if leaking else None
    return {
        "stages": stages,
        "friction": {
            "section": FRICTION_SECTION,
            "score": subsection_scores.get(FRICTION_SECTION),
        },
        "weakest_stage": weakest["stage"] if weakest else None,
    }


def build_cro_facts(
    soup: BeautifulSoup,
    url: str = "",
    *,
    subsection_scores: Mapping[str, float | None] | None = None,
) -> CroFactsPayload:
    """All CRO facts for one page. Pure; the caller makes it exception-safe."""
    text = page_text(soup)
    ctas = ordered_ctas(soup)
    cta_rows, cta_summary = cta_facts(ctas, url)
    facts: CroFactsPayload = {
        "hero": hero_facts(soup, ctas, text),
        "value_prop": value_prop_facts(soup),
        "ctas": cta_rows,
        "cta_summary": cta_summary,
        "form": form_facts(soup),
        "trust": trust_facts(soup, text),
    }
    if subsection_scores is not None:
        facts["journey"] = build_journey(subsection_scores)
    return facts


__all__ = [
    "JOURNEY_STAGES",
    "MAX_CTAS",
    "MAX_FIELDS",
    "build_cro_facts",
    "build_journey",
    "ordered_ctas",
]
