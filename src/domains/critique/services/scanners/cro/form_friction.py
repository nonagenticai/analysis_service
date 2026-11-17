"""Form-friction CRO checks — the hardest-evidence conversion area.

Evidence: Marketo 9→5 fields = +34% conversion; the 2026 benchmark cliff
(3 fields ~23% → 5 ~17% → 7 ~11% → 10+ ~7%); Expedia removing one field =
+$12M/yr; Baymard on input types / autocomplete / inline-label anti-pattern;
multi-step vs single-step ~3x. Only the page's *primary* form (the one with the
most countable fields) is scored, so a stray search box does not penalise.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    _COMPANY_FIELD_RE,
    _EMAIL_FIELD_RE,
    _PHONE_FIELD_RE,
    _STEP_CLASS_RE,
    _cro_finding,
)

_NON_FIELD_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


def _countable_fields(form: Tag) -> list[Tag]:
    fields: list[Tag] = []
    for inp in form.find_all("input"):
        if not isinstance(inp, Tag):
            continue
        if str(inp.get("type", "text")).lower() in _NON_FIELD_INPUT_TYPES:
            continue
        fields.append(inp)
    for el in form.find_all(["select", "textarea"]):
        if isinstance(el, Tag):
            fields.append(el)
    return fields


def _primary_form(soup: BeautifulSoup) -> tuple[Tag | None, list[Tag]]:
    """Return the form with the most countable fields (the conversion form)."""
    best: Tag | None = None
    best_fields: list[Tag] = []
    for form in soup.find_all("form"):
        if not isinstance(form, Tag):
            continue
        fields = _countable_fields(form)
        if len(fields) > len(best_fields):
            best, best_fields = form, fields
    return best, best_fields


def has_conversion_form(soup: BeautifulSoup) -> bool:
    """True when the page has a primary form with at least one countable field.

    The exact precondition :func:`check_form_friction` uses: without it every
    form check is skipped, so the ``form`` section is NOT measured (design
    CRO-G5) rather than clean.
    """
    form, fields = _primary_form(soup)
    return form is not None and bool(fields)


def _has_label(form: Tag, field: Tag) -> bool:
    """True if a field has an associated <label>, aria-label, or aria-labelledby."""
    if field.get("aria-label") or field.get("aria-labelledby"):
        return True
    fid = field.get("id")
    if fid and form.find("label", attrs={"for": fid}):
        return True
    # Wrapped in a <label>…field…</label>
    parent = field.parent
    while parent is not None and parent is not form:
        if isinstance(parent, Tag) and parent.name == "label":
            return True
        parent = parent.parent
    return False


def _check_field_count(n: int, url: str) -> list[dict]:
    """Field count is the strongest signal — severity scales with the cliff."""
    if n >= 10:
        sev, note = "critical", "far above the ~5-field sweet spot"
    elif n >= 7:
        sev, note = "high", "past the 5-to-7 conversion cliff"
    elif n == 6:
        sev, note = "medium", "just above the ideal 3-5 range"
    elif n >= 4:
        sev, note = "low", "slightly above the 3-field ideal"
    else:
        return []
    return [
        _cro_finding(
            severity=sev,
            title=f"Conversion form has {n} fields",
            description=(
                f"The primary form asks for {n} fields ({note}). Every field reduces "
                "completion — Marketo saw +34% conversion cutting 9 fields to 5, and "
                "completion drops sharply beyond ~5-7 fields."
            ),
            url=url,
            recommendation=(
                "Remove every non-essential field; defer the rest to after signup or "
                "a later step. Aim for 3-5 fields."
            ),
            selector="form",
            check_id="cro-form-field-count",
            evidence=f"{n} countable fields",
            false_positive_risk="low",
        )
    ]


def _check_field_types(fields: list[Tag], url: str) -> list[dict]:
    """Textareas and multiple dropdowns measurably depress completion (HubSpot)."""
    out: list[dict] = []
    textareas = [f for f in fields if f.name == "textarea"]
    if textareas:
        out.append(
            _cro_finding(
                severity="low",
                title=f"{len(textareas)} free-text textarea(s) in the form",
                description=(
                    "Open textareas are among the highest-friction fields (HubSpot). "
                    "They demand effort and deter completion."
                ),
                url=url,
                recommendation="Make free-text fields optional, or drop them entirely.",
                selector="form textarea",
                check_id="cro-form-textarea",
            )
        )
    selects = [f for f in fields if f.name == "select"]
    if len(selects) >= 2:
        out.append(
            _cro_finding(
                severity="low",
                title=f"{len(selects)} dropdown fields",
                description=(
                    "Multiple <select> dropdowns measurably depress form completion "
                    "(HubSpot). Prefer sensible defaults or smart detection."
                ),
                url=url,
                recommendation="Reduce dropdowns; pre-fill or infer values where possible.",
                selector="form select",
                check_id="cro-form-multiple-selects",
            )
        )
    return out


def _check_placeholder_labels(form: Tag, fields: list[Tag], url: str) -> list[dict]:
    """Placeholder-as-label anti-pattern (Baymard) — high friction on mobile."""
    offenders = [
        f for f in fields if f.name == "input" and f.get("placeholder") and not _has_label(form, f)
    ]
    if not offenders:
        return []
    return [
        _cro_finding(
            severity="high",
            title=f"{len(offenders)} field(s) use a placeholder as the only label",
            description=(
                "Placeholder-only labels disappear on focus, causing recall errors and "
                "failed validation — worst on mobile (Baymard)."
            ),
            url=url,
            recommendation="Add a persistent <label> above every field.",
            selector="form input[placeholder]",
            check_id="cro-form-placeholder-label",
            false_positive_risk="low",
        )
    ]


def _check_input_types(inputs: list[Tag], url: str) -> list[dict]:
    """Correct mobile input types + autocomplete (Baymard)."""
    out: list[dict] = []
    for f in inputs:
        name_id = f"{f.get('name', '')} {f.get('id', '')} {f.get('placeholder', '')}"
        if _EMAIL_FIELD_RE.search(name_id) and str(f.get("type", "text")).lower() != "email":
            out.append(
                _cro_finding(
                    severity="low",
                    title="Email field is not type=email",
                    description=(
                        "An email field without type=email shows the wrong mobile "
                        "keyboard and skips built-in validation (Baymard)."
                    ),
                    url=url,
                    recommendation='Set type="email" and autocomplete="email".',
                    selector="form input",
                    check_id="cro-form-input-type",
                )
            )
            break
    if len(inputs) >= 3 and not any(f.get("autocomplete") for f in inputs):
        out.append(
            _cro_finding(
                severity="low",
                title="No autocomplete attributes on form fields",
                description=(
                    "Without autocomplete tokens the browser cannot autofill, adding "
                    "typing friction (Baymard)."
                ),
                url=url,
                recommendation='Add autocomplete="email name tel address-line1" tokens.',
                selector="form input",
                check_id="cro-form-autocomplete-missing",
            )
        )
    return out


def _check_required_friction(inputs: list[Tag], url: str) -> list[dict]:
    """Required phone/company is a top abandonment driver (Expedia: +$12M/yr)."""
    for f in inputs:
        required = f.get("required") is not None or f.get("aria-required") == "true"
        name_id = f"{f.get('name', '')} {f.get('id', '')}"
        if required and (_PHONE_FIELD_RE.search(name_id) or _COMPANY_FIELD_RE.search(name_id)):
            return [
                _cro_finding(
                    severity="medium",
                    title="High-friction field marked required",
                    description=(
                        "Requiring phone/company early is a top abandonment driver "
                        "(Expedia removed one such field for +$12M/yr)."
                    ),
                    url=url,
                    recommendation="Make phone/company optional or collect them later.",
                    selector="form input[required]",
                    check_id="cro-form-required-friction",
                )
            ]
    return []


def _check_multistep(form: Tag, n: int, url: str) -> list[dict]:
    """Long single-step forms convert worse — stepping lifts ~3x (Formstack)."""
    has_step = bool(
        form.find("fieldset") or form.find(attrs={"class": _STEP_CLASS_RE}) or form.find("progress")
    )
    if n <= 7 or has_step:
        return []
    return [
        _cro_finding(
            severity="low",
            title="Long single-step form",
            description=(
                f"A {n}-field single-step form feels daunting. Multi-step forms convert "
                "up to ~3x better (Formstack/Venture Harbour)."
            ),
            url=url,
            recommendation="Split the form into 2-3 progressive steps with a progress bar.",
            selector="form",
            check_id="cro-form-no-multistep",
        )
    ]


def check_form_friction(soup: BeautifulSoup, url: str) -> list[dict]:
    form, fields = _primary_form(soup)
    if form is None or not fields:
        return []  # no conversion form on the page → no form friction

    n = len(fields)
    inputs = [f for f in fields if f.name == "input"]

    findings: list[dict] = []
    findings.extend(_check_field_count(n, url))
    findings.extend(_check_field_types(fields, url))
    findings.extend(_check_placeholder_labels(form, fields, url))
    findings.extend(_check_input_types(inputs, url))
    findings.extend(_check_required_friction(inputs, url))
    findings.extend(_check_multistep(form, n, url))
    return findings
