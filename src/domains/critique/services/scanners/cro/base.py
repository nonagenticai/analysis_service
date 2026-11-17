"""Shared finding factory + text helpers for the CRO scanner package.

Every CRO check emits a finding through :func:`_cro_finding`, which pins
``category="cro"`` and ``source_tool="cro_scanner"``. Findings carry a stable
``check_id`` in the ``cro-<section>-<check>`` namespace — the ``<section>``
token (``form``/``hero``/``cta``/``social``/``valueprop``/``objection``/
``tech``/``footer``) is what :func:`cro.scoring.compute_cro_score` groups by to
produce the per-section weighted-decay sub-scores.

CRO is modelled as *absence detection*: a check emits a penalty finding when a
conversion signal is missing or weak. A page that does everything right emits
no findings and scores 100. Severity encodes the strength of the CRO evidence
behind the signal (form-field-count and page-speed are the hardest-evidence
areas → higher severities; soft heuristics → low/info).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

from src.domains.critique.services.scanners.base import make_finding

_NON_TEXT_PARENTS = {"script", "style", "noscript", "template"}


def _cro_finding(
    *,
    severity: str,
    title: str,
    description: str,
    url: str = "",
    recommendation: str = "",
    selector: str | None = None,
    check_id: str | None = None,
    evidence: str | None = None,
    false_positive_risk: str = "medium",
    remediation_effort: str = "low",
) -> dict:
    """CRO scanner finding with default metadata."""
    return make_finding(
        category="cro",
        severity=severity,
        title=title,
        description=description,
        url=url,
        recommendation=recommendation,
        selector=selector,
        check_id=check_id,
        evidence=evidence,
        source_tool="cro_scanner",
        false_positive_risk=false_positive_risk,
        remediation_effort=remediation_effort,
    )


# --------------------------------------------------------------------------
# Token regexes (compiled once). Deterministic signal detectors — the "intelligence"
# is in which tokens map to which conversion signal at which severity.
# --------------------------------------------------------------------------

# Strong action verbs that open a converting CTA (Unbounce/CXL).
ACTION_VERB_RE = re.compile(
    r"\b(get|start|try|download|book|claim|build|create|join|grab|unlock|"
    r"buy|order|subscribe|register|reserve|request|discover|see)\b",
    re.IGNORECASE,
)
# Low-value generic CTA text (penalised when it is the *only* kind present).
GENERIC_CTA_RE = re.compile(
    r"^(submit|click here|continue|next|go|ok|enter|more|read more|learn more)$",
    re.IGNORECASE,
)
# First-person framing lifts clicks (ContentVerve "my" vs "your").
FIRST_PERSON_RE = re.compile(r"\b(my|me|i)\b", re.IGNORECASE)
# Reassurance microcopy that reduces click anxiety.
MICROCOPY_RE = re.compile(
    r"no credit card|free|cancel anytime|money[- ]back|30[- ]day|"
    r"no obligation|instant|risk[- ]free|secure",
    re.IGNORECASE,
)
# Above-fold trust badges / authority marks.
TRUST_BADGE_RE = re.compile(
    r"norton|mcafee|\bssl\b|trustpilot|\bg2\b|capterra|verisign|secured?|"
    r"guarantee|gdpr|soc\s*2|iso\s*27001|\bpci\b|badge|award|certified",
    re.IGNORECASE,
)
# Risk-reversal / objection-handling.
GUARANTEE_RE = re.compile(
    r"money[- ]back|refund|guarantee|risk[- ]free|no questions asked|"
    r"satisfaction guaranteed|full refund",
    re.IGNORECASE,
)
FREE_TRIAL_RE = re.compile(
    r"free trial|no credit card|cancel anytime|start free|free forever|free plan",
    re.IGNORECASE,
)
SECURITY_RE = re.compile(
    r"\bssl\b|encrypt|gdpr|soc\s*2|iso\s*27001|\bpci\b|privacy|secure|"
    r"data protection|hipaa",
    re.IGNORECASE,
)
MEDIA_MENTION_RE = re.compile(
    r"forbes|techcrunch|\bwsj\b|wall street journal|as seen in|featured in|"
    r"bloomberg|the verge|mashable|entrepreneur",
    re.IGNORECASE,
)
# Quantified counts of customers / reviews (social proof).
COUNT_RE = re.compile(
    r"\d[\d,]*\+?\s*(reviews?|customers?|users?|clients?|companies|downloads?|"
    r"members?|businesses|teams)",
    re.IGNORECASE,
)
# Quantified benefit tokens in copy (%/$/number+unit) — the "ultra-specific" U.
QUANT_BENEFIT_RE = re.compile(
    r"\d+\s*%|[$£€]\s*\d|\bx\d+|\d+x\b|\d[\d,]*\s*(hours?|days?|minutes?|"
    r"seconds?|hrs?|customers?|users?)",
    re.IGNORECASE,
)
# Named-attribution on a testimonial ("— Jane Doe", "Jane Doe, CEO").
# U+2014 = em dash, U+2013 = en dash (escaped to avoid ambiguous-unicode lint).
NAMED_ATTRIBUTION_RE = re.compile(
    r"[\u2014\u2013-]\s*[A-Z][a-z]+\s+[A-Z]|,\s*(ceo|cto|founder|director|manager|head of|vp)\b"
)

_TESTIMONIAL_CLASS_RE = re.compile(r"testimonial|review|quote|customer-story", re.IGNORECASE)
_LOGO_CLASS_RE = re.compile(
    r"logo|clients?|trusted[- ]by|brands?|partners?|as[- ]seen", re.IGNORECASE
)
_RATING_CLASS_RE = re.compile(r"star|rating|review-score", re.IGNORECASE)
_FAQ_CLASS_RE = re.compile(r"faq|accordion|questions?", re.IGNORECASE)
_STEP_CLASS_RE = re.compile(r"step|progress|wizard|stage", re.IGNORECASE)
_BTN_CLASS_RE = re.compile(r"btn|button|cta|call-to-action", re.IGNORECASE)

# Input names/ids that signify high-friction fields.
_PHONE_FIELD_RE = re.compile(r"phone|tel|mobile", re.IGNORECASE)
_COMPANY_FIELD_RE = re.compile(r"company|organi[sz]ation|business", re.IGNORECASE)
_EMAIL_FIELD_RE = re.compile(r"email|e-mail", re.IGNORECASE)


def page_text(soup: BeautifulSoup) -> str:
    """Visible page text (script/style/etc. excluded), collapsed whitespace.

    Non-mutating: walks text nodes rather than ``extract()``-ing tags, so the
    shared soup passed to every scanner is left intact.
    """
    root = soup.find("body") or soup
    parts: list[str] = []
    for node in root.descendants:
        if isinstance(node, NavigableString):
            parent = node.parent
            if parent is not None and parent.name in _NON_TEXT_PARENTS:
                continue
            parts.append(str(node))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def cta_elements(soup: Tag) -> list[Tag]:
    """Collect plausible call-to-action elements within a subtree.

    Accepts any ``Tag`` (``BeautifulSoup`` is a ``Tag`` subclass), so it can scope
    to the whole page or a region such as the footer. A CTA is a ``<button>``, a
    submit/button ``<input>``, an ``<a>`` styled as a button (class/role), or an
    ``<a>`` whose text opens with an action verb.
    """
    ctas: list[Tag] = []
    seen: set[int] = set()

    def _add(el: Tag) -> None:
        if id(el) not in seen:
            seen.add(id(el))
            ctas.append(el)

    for btn in soup.find_all("button"):
        if isinstance(btn, Tag):
            _add(btn)
    for inp in soup.find_all("input"):
        if isinstance(inp, Tag) and str(inp.get("type", "")).lower() in {"submit", "button"}:
            _add(inp)
    for a in soup.find_all("a"):
        if not isinstance(a, Tag):
            continue
        classes = " ".join(a.get("class") or [])
        role = str(a.get("role", ""))
        text = a.get_text(" ", strip=True)
        if _BTN_CLASS_RE.search(classes) or role == "button" or ACTION_VERB_RE.search(text):
            _add(a)
    return ctas


def cta_text(el: Tag) -> str:
    """Human-visible text of a CTA element (``value`` for inputs)."""
    if el.name == "input":
        return str(el.get("value", "")).strip()
    return el.get_text(" ", strip=True)
