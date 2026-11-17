"""Deterministic guards applied to LLM-authored UX findings before they are scored.

The UX reviewer is a language model reading truncated, script-stripped HTML. It
once ranked "No contact form or way to reach the business" CRITICAL on a page
with ``tel:`` and ``mailto:`` links, and listed praise as a finding. The guards
here act ONLY on the structured fields the reviewer's ``output_schema`` asks for
-- never on words in the title, which is how an earlier keyword filter dropped
"Password shown in clear text" as praise.

* ``kind == "strength"`` -> moved to the strengths list, never scored -- unless
  its severity is ``high``/``critical``: praise is never that severe, so it
  stays an issue flagged ``kind_conflict``.
* **Scope** -- a ``page`` claim whose text carries a region qualifier ("above
  the fold", "pricing", "checkout", "mobile menu", en/nl) or whose evidence
  selector lands only inside a header/footer/modal/pricing block is treated as
  ``region``. This only ever PROTECTS a finding from auto-dropping.
* **Contradiction** -- only a ``claim_type`` other than ``other`` with
  ``scope == "page"`` is tested, against the ONE probe for that claim type.
  A ``mailto:`` link does not contradict "no contact form"; a region
  ("above the fold") or conditional ("zero-results state") claim is never
  auto-dropped, because the parsed page cannot see a fold or a state.
* **Severity** -- a finding is ``verified`` when its ``evidence`` (CSS selector
  or exact text) is found on the page, or when its page-scoped absence claim is
  confirmed by the probe. Unverified findings keep their severity except that
  ``critical`` becomes ``high``, and carry NO score penalty
  (``compute_category_score``): the model is nondeterministic under load.
* **Probes** -- "no way to reach the business" is disproved by a phone number
  or email address (linked or written in the visible text) or a real contact
  form; a newsletter, login, search, comment, reply or review form is not one,
  and IBAN/VAT/KvK/postcode digits are not a phone number.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from bs4 import BeautifulSoup, Tag
from soupsieve import SelectorSyntaxError  # bs4's own selector engine (hard dep of .select)

from src.domains.critique.services.scanners.base import is_homepage_url

logger = logging.getLogger(__name__)

KINDS = ("issue", "strength")
SCOPES = ("page", "region", "conditional")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SEARCH_NAME_RE = re.compile(r"^(?:q|s|search|query)$", re.IGNORECASE)

# --- contact form -------------------------------------------------------------
#: Input types a visitor does not type into.
_NON_ENTRY_TYPES = frozenset({"hidden", "submit", "button", "image", "reset", "checkbox", "radio"})
_MESSAGE_FIELD_RE = re.compile(
    r"message|bericht|vraag|opmerking|toelichting|enquiry|inquiry", re.IGNORECASE
)
_NAME_FIELD_RE = re.compile(r"(?<!user)(?<!user[_-])name|naam", re.IGNORECASE)
_EMAIL_FIELD_RE = re.compile(r"e-?mail", re.IGNORECASE)
#: A submit label that says "contact us" -- including asking for a quote, an
#: appointment or a call back (en/nl) -- not "subscribe" or "log in".
_CONTACT_SUBMIT_RE = re.compile(
    r"contact|get in touch|\bsend\b|verstuur|versturen|verzend|bericht|"
    r"terugbel|bel (?:mij|me|ons) (?:terug|op)|call ?back|call me|"
    r"offerte|\bquote\b|aanvra(?:ag|gen)|"
    r"afspraak|appointment|gesprek|"
    r"\b(?:book|schedule|plan) (?:a |an |een )?(?:call|meeting|demo|consult)",
    re.IGNORECASE,
)
#: A submit label that signs up, logs in or searches. It vetoes the submit-only
#: path even when it borrows a contact word: "Nieuwsbrief aanvragen" is a sign-up.
_NON_CONTACT_SUBMIT_RE = re.compile(
    r"nieuwsbrief|newsletter|subscribe|abonne|aanmeld|meld (?:je |u )?aan|inschrij|"
    r"schrijf (?:je |u )?in|sign ?up|register|registre|log ?in|inlog|sign ?in|"
    r"\bsearch\b|\bzoek",
    re.IGNORECASE,
)

#: Words that say a form is FOR contacting the business (en/nl): read from submit
#: labels, the form's own name/aria/action, its heading and its field labels.
_CONTACT_INTENT_RE = re.compile(
    r"contact|get in touch|bericht|vraag|offerte|quote|message|enquiry|inquiry|"
    r"\bsend\b|verstuur|versturen|verzend|terugbel|call ?back",
    re.IGNORECASE,
)
#: A form that searches, or posts a comment/reply/review, is not a contact form.
_SEARCH_MARKER_RE = re.compile(r"search|zoek", re.IGNORECASE)
_FEEDBACK_MARKER_RE = re.compile(
    r"comment|repl(?:y|ies)|respond|(?<![a-z])review|reactie|reageer|recensie|beoordel",
    re.IGNORECASE,
)
_FEEDBACK_SUBMIT_RE = re.compile(
    r"post (?:a |your )?(?:comment|reply|review)|\breply\b|submit (?:a |your )?review|"
    r"reageer|plaats (?:een |je |uw )?(?:reactie|review|recensie)|"
    r"review (?:plaatsen|schrijven|versturen)|beoordeling",
    re.IGNORECASE,
)
_SEARCH_LABEL_RE = re.compile(r"\b(?:search|zoek)", re.IGNORECASE)
_SEARCH_SUBMIT_RE = re.compile(r"^\s*(?:search|zoek(?:en)?)\b", re.IGNORECASE)
#: WordPress/Disqus comment plumbing: a hidden field that only a comment form carries.
_FEEDBACK_FIELD_NAME_RE = re.compile(
    r"^(?:comment|comments|reply|review|rating|comment_post_id|comment_parent|replytocom)$",
    re.IGNORECASE,
)
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "legend")

# --- phone numbers ------------------------------------------------------------
#: Business identifiers whose digits parse as a phone number once separators go:
#: "NL91 ABNA 0417 1643 00" leaves 0417164300, a well-formed Dutch number.
_IDENTIFIER_RES = (
    re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b"),  # IBAN
    re.compile(r"\b[A-Z]{2}\s?\d{9}\s?B\s?\d{2}\b", re.IGNORECASE),  # NL VAT (btw-id)
    re.compile(
        r"\b(?:kvk|k\.v\.k\.?|coc|chamber of commerce|btw|vat|rsin|iban|bic|swift|"
        r"ondernemingsnummer|handelsregister)(?:[\s.-]*(?:nummer|nr|no|number|id))?\.?"
        r"\s*[:#]?\s*[A-Z]{0,2}[\d][\d .]{5,32}\d(?:\s?B\s?\d{2})?",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{4}\s?[A-Z]{2}\b"),  # NL postcode
)
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w+])(?:\+|00)?\(?\d[\d\s().-]{7,17}\d(?!\w)")
_PHONE_FORMATS = (
    re.compile(r"0[1-9]\d{8}"),  # NL national: 0 + 9 digits
    re.compile(r"\+31[1-9]\d{8}"),  # NL international
    re.compile(r"\+(?!31)[1-9]\d{7,13}"),  # other E.164 -- only with an explicit +/00
)
#: A phone LABEL preceding a number, anchored to the end of the window we look back
#: over, so "Bestelnummer: 0123456789" and "SKU 0412-8890-12" cannot borrow a label
#: from elsewhere on the page. The ``\w*`` tails cover only the CONCATENATED
#: compounds ("telefoonnummer", "phoneline") -- NOT "phone number", whose space
#: leaves a second word between the label and the digits. Two-word labels are the
#: common English form, so a BOUNDED run of intervening words is allowed as well
#: (at most two, see the inline comment below). The discriminator is unchanged: the LABEL
#: itself must still be phone-ish, so "Bestelnummer"/"Rekeningnummer" -- which
#: contain no phone word at a word boundary -- are still not labels.
_PHONE_LABEL_RE = re.compile(
    r"(?:\btel\.?(?=[\W]|$)|\btelefoon\w*|\btelephone\w*|\bphone\w*|\bmobiel\w*"
    r"|\bmobile\b|\bgsm\b|\bwhatsapp\b|\bbel(?:\s+ons)?\b|\bcall(?:\s+us)?\b"
    r"|\bcontact\w*|\bt\s*[:.]|[☎📞✆])"
    # At most TWO further words of at most twelve characters may sit between the
    # label and the digits: enough for "Phone number:", "Telefoon nummer:",
    # "Tel. nr.", "Telefoon nr kantoor:", and short enough that a running sentence
    # cannot reach across and vouch for an unrelated number. Deliberately NOT ``.*``.
    # Separator and word classes are disjoint, so this cannot backtrack quadratically,
    # and it is only ever applied to a <=40-character window.
    "(?:[\\s:;.,\\-\\u2013\\u2014()|]{1,4}\\w{1,12}){0,2}"
    # Trailing separators only. En/em dash written as escapes because a literal one
    # trips ruff's RUF001 ambiguous-character rule.
    "[\\s:;.,\\-\\u2013\\u2014()|]{0,12}$",
    re.IGNORECASE,
)
#: How far back from a number we look for its label. Long enough for "Telefoonnummer: ",
#: short enough that an unrelated sentence cannot reach across and vouch for it.
_PHONE_LABEL_WINDOW = 40
#: A shop that SELLS phones is not a shop that PRINTS one. "phone cases",
#: ``class="phone-cases"``, "Telefoonhoesjes SKU 0412889012" all carry a phone word
#: next to a digit run that is a product code. Applied to BOTH lanes -- the attribute
#: string and the text window -- because ``\btelefoon\w*`` matches "Telefoonhoesjes"
#: just as ``\bphone\w*`` matches "phone-cases". Short and explicit on purpose.
#:
#: ⛔ ONLY words that name the PRODUCT ITSELF may live here, and the list must stay at
#: these three. It earlier also carried "shop|categor|winkel", which named the SHOP
#: rather than the goods -- and because this pattern is tested against the WHOLE
#: 40-character pre-digit window (and the whole attribute string), those words matched
#: anywhere in it. An ordinary webshop footer -- "Webshop Nederland Telefoon: 020 123
#: 4567", a ``Shop`` nav item that ``get_text(" ")`` flattens into the window, or
#: ``class="webshop-telefoon"`` -- therefore lost its REAL phone and the customer was
#: told "No contact information" again, which is the exact false accusation this whole
#: change exists to stop. The product traps do not need them: "Telefoonhoesjes" is
#: caught by "hoesje" and "phone-cases"/"phone cases" by "case", so the three words
#: below close the product hole on their own. See the webshop cases in
#: ``test_contact_info_phone_probe.py``; a false ACCUSATION is far more expensive than
#: a false negative on a contrived page, so widen this only with a case in both
#: directions.
_PHONE_PRODUCT_CONTEXT_RE = re.compile(r"case|hoesje|accessoire", re.IGNORECASE)
#: Markup that NAMES an element as a phone number. Deliberately narrower than
#: ``_PHONE_LABEL_RE`` -- no "contact", because a ``class="contact-info"`` wrapper can
#: enclose a whole footer (SKUs included) while saying nothing about any one number.
#:
#: That asymmetry with the text lane -- where ``\bcontact\w*`` IS a label -- is
#: DELIBERATE, not an oversight, and must not be "fixed" in either direction.
#: ``<h3>Contact</h3><p>0412889012</p>`` is a real and very common presentation of a
#: phone number, so the text lane is right to read a number under a "Contact" heading
#: as contact information. An ATTRIBUTE is different: a class or id is inherited by a
#: whole subtree, so ``class="contact-info"`` would vouch for every digit run in the
#: footer. Same word, different blast radius.
_PHONE_ATTR_RE = re.compile(
    r"\btel(?:ephone|efoon)\w*|\btel\b|\bphone\w*|\bmobiel\w*", re.IGNORECASE
)
_PHONE_ATTRS = ("itemprop", "aria-label", "title", "class", "id")

#: Severities the model may not call praise: a "strength" at these is a conflict.
_ISSUE_SEVERITIES = frozenset({"critical", "high"})
#: Words that confine a claim to part of the page (en/nl). "No phone number above
#: the fold" says nothing about the footer, so it is never a page-level absence.
_REGION_QUALIFIER_RE = re.compile(
    r"above the fold|below the fold|boven de vouw|onder de vouw|\bhero\b|"
    r"\bheader\b|\bfooter\b|\bmodal\b|\bpop-?up\b|\bdialog\b|"
    r"pricing|\bprijzen|\bprijs|tarieven|checkout|check-out|afrekenen|\bkassa\b|"
    r"mobile (?:menu|nav)|mobiele? menu|hamburger",
    re.IGNORECASE,
)
#: Elements that ARE a region: a selector landing inside one points at a region.
_REGION_TAGS = ("header", "footer", "dialog")
_REGION_ATTR_RE = re.compile(r"modal|pricing|popup|dialog|prijzen|tarieven", re.IGNORECASE)


def _in_region(element: Tag) -> bool:
    for node in (element, *element.parents):
        if not isinstance(node, Tag):
            continue
        if node.name in _REGION_TAGS or node.get("role") == "dialog":
            return True
        attrs = " ".join([str(node.get("id", "")), *map(str, node.get("class") or [])])
        if _REGION_ATTR_RE.search(attrs):
            return True
    return False


def claims_a_region(finding: dict, soup: BeautifulSoup) -> bool:
    """The finding's text or evidence confines it to one region of the page."""
    text = " ".join(str(finding.get(k, "")) for k in ("title", "description", "evidence"))
    if _REGION_QUALIFIER_RE.search(text):
        return True
    evidence = " ".join(str(finding.get("evidence") or "").split())
    if not evidence or _BARE_TAG_RE.fullmatch(evidence):
        return False
    try:
        matched = soup.select(evidence)
    except SelectorSyntaxError, ValueError, NotImplementedError:
        return False
    return bool(matched) and all(_in_region(el) for el in matched)


#: An exact-text snippet shorter than this ("Go", "Home") proves nothing.
_MIN_SNIPPET_CHARS = 8
_BARE_TAG_RE = re.compile(r"[a-zA-Z][\w-]*")


def _hrefs(soup: BeautifulSoup) -> list[str]:
    return [
        str(a.get("href", "")).strip().lower()
        for a in soup.find_all("a", href=True)
        if isinstance(a, Tag)
    ]


def _visible_text(soup: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(soup), "lxml")
    for tag in clone.find_all(["script", "style", "noscript", "template"]):
        tag.decompose()
    return clone.get_text(" ", strip=True)


def _field_label(field: Tag, soup: BeautifulSoup) -> str:
    """Everything that names a form field: attributes and its ``<label>``."""
    parts = [
        str(field.get(attr, ""))
        for attr in ("name", "id", "autocomplete", "placeholder", "aria-label", "title")
    ]
    field_id = field.get("id")
    if field_id:
        label = soup.find("label", attrs={"for": field_id})
        if isinstance(label, Tag):
            parts.append(label.get_text(" ", strip=True))
    wrapper = field.find_parent("label")
    if isinstance(wrapper, Tag):
        parts.append(wrapper.get_text(" ", strip=True))
    return " ".join(parts)


def _submit_labels(form: Tag) -> str:
    labels: list[str] = []
    for el in form.find_all(["button", "input"]):
        if not isinstance(el, Tag):
            continue
        kind = str(el.get("type", "submit" if el.name == "button" else "text")).lower()
        if kind in {"submit", "image"}:
            labels.extend(
                [
                    el.get_text(" ", strip=True),
                    str(el.get("value", "")),
                    str(el.get("aria-label", "")),
                ]
            )
    return " ".join(labels)


def _form_identity(form: Tag) -> str:
    """The form's own name: id, class, name, aria-label, title."""
    return " ".join(
        [
            *(str(form.get(attr, "")) for attr in ("id", "name", "aria-label", "title")),
            *map(str, form.get("class") or []),
        ]
    )


def _form_heading(form: Tag, soup: BeautifulSoup) -> str:
    """The legend inside the form, its ``aria-labelledby`` target, or the heading before it."""
    parts: list[str] = []
    labelledby = str(form.get("aria-labelledby", ""))
    for ref in labelledby.split():
        target = soup.find(id=ref)
        if isinstance(target, Tag):
            parts.append(target.get_text(" ", strip=True))
    inner = form.find(_HEADING_TAGS)
    heading = inner if isinstance(inner, Tag) else form.find_previous(_HEADING_TAGS)
    if isinstance(heading, Tag):
        parts.append(heading.get_text(" ", strip=True))
    return " ".join(parts)


def _is_search_form(form: Tag, entry_fields: list[Tag], labels: list[str], submit: str) -> bool:
    if form.get("role") == "search" or form.find("search") or form.find_parent("search"):
        return True
    if form.find("input", attrs={"type": "search"}):
        return True
    if _SEARCH_MARKER_RE.search(" ".join([_form_identity(form), str(form.get("action", ""))])):
        return True
    names = [str(f.get("name", f.get("id", ""))) for f in entry_fields]
    if names and all(_SEARCH_NAME_RE.fullmatch(n) for n in names):
        return True
    if any(_SEARCH_LABEL_RE.search(label) for label in labels):
        return True  # placeholder="Search messages" is a search box, not a message field
    return bool(_SEARCH_SUBMIT_RE.search(submit))


def _is_feedback_form(form: Tag, submit: str) -> bool:
    """A comment, reply or review form (WordPress ``wp-comments-post.php`` and kin)."""
    if _FEEDBACK_MARKER_RE.search(" ".join([_form_identity(form), str(form.get("action", ""))])):
        return True
    if _FEEDBACK_SUBMIT_RE.search(submit):
        return True
    return any(
        isinstance(f, Tag)
        and (
            _FEEDBACK_FIELD_NAME_RE.fullmatch(str(f.get("name", "")))
            or _FEEDBACK_FIELD_NAME_RE.fullmatch(str(f.get("id", "")))
        )
        for f in form.find_all(["input", "textarea", "select"])
    )


def is_contact_form(form: Tag, soup: BeautifulSoup) -> bool:
    """A form a visitor uses to CONTACT the business.

    Never a login (password), a newsletter (a lone email), a search, or a
    comment/reply/review form. A free-text field alone is not enough -- a
    WordPress comment box has one -- so a form qualifies when it says it is for
    contact (submit label, heading, aria/name, action or a field label says
    contact/message/send/vraag/offerte, en/nl), or it takes a name, an email and
    a free-text message.
    """
    fields: list[Tag] = []
    for field in form.find_all(["input", "textarea", "select"]):
        if not isinstance(field, Tag):
            continue
        kind = str(field.get("type", "text")).lower() if field.name == "input" else field.name
        if kind == "password":
            return False
        if kind not in _NON_ENTRY_TYPES:
            fields.append(field)
    if not fields:
        return False
    submit = _submit_labels(form)
    labels = [_field_label(f, soup) for f in fields]
    if _is_search_form(form, fields, labels, submit) or _is_feedback_form(form, submit):
        return False
    is_email = [
        str(f.get("type", "")).lower() == "email" or bool(_EMAIL_FIELD_RE.search(label))
        for f, label in zip(fields, labels, strict=True)
    ]
    if len(fields) == 1 and is_email[0]:
        return False  # newsletter sign-up
    has_name = any(_NAME_FIELD_RE.search(label) for label in labels)
    message_fields = [
        label
        for field, label in zip(fields, labels, strict=True)
        if field.name == "textarea" or _MESSAGE_FIELD_RE.search(label)
    ]
    if message_fields:
        context = " ".join(
            [
                submit,
                _form_identity(form),
                str(form.get("action", "")),
                _form_heading(form, soup),
                *message_fields,
            ]
        )
        return bool(_CONTACT_INTENT_RE.search(context)) or (has_name and any(is_email))
    return (
        has_name
        and any(is_email)
        and bool(_CONTACT_SUBMIT_RE.search(submit))
        and not _NON_CONTACT_SUBMIT_RE.search(submit)
    )


def has_contact_form(soup: BeautifulSoup) -> bool:
    """A ``<form>`` that :func:`is_contact_form` accepts."""
    return any(
        isinstance(form, Tag) and is_contact_form(form, soup) for form in soup.find_all("form")
    )


def _normalize_phone(candidate: str) -> str:
    digits = candidate.replace("(0)", "")
    plus = digits.lstrip().startswith(("+", "00"))
    digits = re.sub(r"\D", "", digits)
    if plus:
        digits = "+" + (digits[2:] if candidate.lstrip().startswith("00") else digits)
    return digits


def _is_phone(candidate: str) -> bool:
    normalized = _normalize_phone(candidate)
    return any(fmt.fullmatch(normalized) for fmt in _PHONE_FORMATS)


#: A KvK is 8 digits and a VAT/RSIN 9 -- fewer than 6 is not an identifier on its own.
_MIN_IDENTIFIER_DIGITS = 6


def _strip_identifier(match: re.Match[str]) -> str:
    """Blank an identifier span, but hand back a valid phone number glued to its end.

    "KvK 12345678 010 123 4567" matches as one digit run; tokenised at each
    space, the tail "010 123 4567" is a phone number and "12345678" is still a
    whole KvK, so the tail survives. "KvK 0412 3456 78" has no such split.
    """
    span = match.group(0)
    for split in (m.start() for m in re.finditer(r"\s+", span)):
        head, tail = span[:split], span[split:].strip()
        if len(re.sub(r"\D", "", head)) >= _MIN_IDENTIFIER_DIGITS and _is_phone(tail):
            return f" | {tail} "
    return " | "


def has_phone(soup: BeautifulSoup) -> bool:
    """A ``tel:`` link, or a well-formed NL/international number in the visible text.

    IBAN, VAT (btw), KvK and postcode spans are removed first: their digits
    otherwise read as a phone number.
    """
    if any(h.startswith("tel:") for h in _hrefs(soup)):
        return True
    text = _visible_text(soup)
    for identifier in _IDENTIFIER_RES:
        text = identifier.sub(_strip_identifier, text)
    return any(_is_phone(candidate) for candidate in _PHONE_CANDIDATE_RE.findall(text))


def _attr_labelled_phone(soup: BeautifulSoup) -> bool:
    """An element whose own markup names it a phone and whose own text holds one.

    ``<span class="phone">020 123 4567</span>``, ``itemprop="telephone"``, an
    ``aria-label`` naming a phone. Only the element's DIRECT strings count -- a
    ``<footer id="telephone">`` wrapping a SKU three elements down must not vouch
    for it.

    A product/category context disqualifies the element: ``class="phone-cases"`` and
    ``title="phone cases"`` name what the shop SELLS, not what the number IS, and the
    digits next to them are a SKU.
    """
    for element in soup.find_all(True):
        if not isinstance(element, Tag):
            continue
        attrs = " ".join(
            " ".join(value) if isinstance(value := element.get(attr, ""), list) else str(value)
            for attr in _PHONE_ATTRS
        )
        if not _PHONE_ATTR_RE.search(attrs) or _PHONE_PRODUCT_CONTEXT_RE.search(attrs):
            continue
        own_text = " ".join(str(s).strip() for s in element.find_all(string=True, recursive=False))
        if any(_is_phone(candidate) for candidate in _PHONE_CANDIDATE_RE.findall(own_text)):
            return True
    return False


def has_labelled_phone(soup: BeautifulSoup) -> bool:
    """A number the page PRESENTS as a phone number -- not merely digits that parse.

    :func:`has_phone` answers "do these digits normalise to a valid number?", which is
    the right question for a UX claim about a number the page itself called a phone.
    It is the WRONG question for "does this site publish contact details?": an order
    number, SKU, tracking code, bank account or ISBN-10 is a ten-digit run starting
    with 0, so ``has_phone`` returns True for a page carrying no contact details at
    all. Suppressing a "no contact information" finding on that basis trades a visible
    false positive for a silent false negative.

    So a number counts here only with phone-shaped CONTEXT:

    1. a ``tel:`` href -- the number is a dialable link;
    2. markup that names the element a phone (``itemprop="telephone"``,
       ``class="phone"``, an ``aria-label``) around the number's own text;
    3. a phone label adjacent to the digits in the visible text ("Tel:", "Telefoon",
       "Bel ons", "T:", "Call us", "☎").

    A bare digit run with no such context is NOT contact information.
    """
    if any(h.startswith("tel:") for h in _hrefs(soup)):
        return True
    if _attr_labelled_phone(soup):
        return True
    text = _visible_text(soup)
    for identifier in _IDENTIFIER_RES:
        text = identifier.sub(_strip_identifier, text)
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        if not _is_phone(match.group(0)):
            continue
        window = text[max(0, match.start() - _PHONE_LABEL_WINDOW) : match.start()]
        if _PHONE_PRODUCT_CONTEXT_RE.search(window):
            continue  # "Telefoonhoesjes SKU 0412889012" -- a product, not a number.
        if _PHONE_LABEL_RE.search(window):
            return True
    return False


def has_email(soup: BeautifulSoup) -> bool:
    """A ``mailto:`` link, or an email address in the visible text."""
    return any(h.startswith("mailto:") for h in _hrefs(soup)) or bool(
        _EMAIL_RE.search(_visible_text(soup))
    )


def has_search(soup: BeautifulSoup) -> bool:
    return bool(
        soup.find("input", attrs={"type": "search"})
        or soup.find(attrs={"role": "search"})
        or soup.find("search")
        or soup.find("input", attrs={"name": _SEARCH_NAME_RE})
    )


def _jsonld_has_breadcrumbs(soup: BeautifulSoup) -> bool:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or ""
        if "BreadcrumbList" not in raw:
            continue
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            continue
        return True
    return False


def has_breadcrumbs(soup: BeautifulSoup) -> bool:
    return bool(
        soup.find(attrs={"aria-label": re.compile(r"breadcrumb", re.I)})
        or soup.find(class_=re.compile(r"breadcrumb", re.I))
        or _jsonld_has_breadcrumbs(soup)
    )


def has_contact_method(soup: BeautifulSoup) -> bool:
    """A way to reach the business: a phone number, an email address, or a contact form.

    A number or address written in the visible text counts as much as a
    ``tel:``/``mailto:`` link -- a visitor can dial or type it. A "Contact" nav
    link, a postal address, business identifiers (KvK/VAT/IBAN), a newsletter,
    login, search or comment form do not disprove "no way to reach the business".
    """
    return has_phone(soup) or has_email(soup) or has_contact_form(soup)


#: claim_type -> probe answering "is the claimed-absent thing present on the page?".
#: One probe per claim; a claim is never contradicted by a DIFFERENT subject.
CLAIM_PROBES: dict[str, Callable[[BeautifulSoup], bool]] = {
    "missing_contact_form": has_contact_form,
    "missing_contact_method": has_contact_method,
    "missing_phone": has_phone,
    "missing_email": has_email,
    "missing_search": has_search,
    "missing_breadcrumbs": has_breadcrumbs,
}
CLAIM_TYPES = (*CLAIM_PROBES, "other")

#: Claims that do not apply to a site root at all.
_NOT_APPLICABLE_ON_HOMEPAGE = frozenset({"missing_breadcrumbs"})


def evidence_found(evidence: str | None, soup: BeautifulSoup, text: str) -> bool:
    """``evidence`` is a CSS selector that matches, or a text snippet on the page."""
    snippet = " ".join((evidence or "").split())
    if len(snippet) >= _MIN_SNIPPET_CHARS and snippet.lower() in " ".join(text.split()).lower():
        return True
    if _BARE_TAG_RE.fullmatch(snippet):
        return False  # "button" matches any button -- it points at nothing
    try:
        return bool(snippet) and bool(soup.select(snippet))
    except SelectorSyntaxError, ValueError, NotImplementedError:
        return False


def apply_page_guards(
    findings: list[dict], soup: BeautifulSoup, url: str
) -> tuple[list[dict], list[dict]]:
    """Split ``findings`` into (scored issues, strengths) and verify each issue.

    Findings must carry the structured fields the reviewer parses
    (``kind``/``claim_type``/``scope``/``evidence``). Missing fields default to
    the value that suppresses nothing: ``issue`` / ``other`` / no scope.
    """
    homepage = is_homepage_url(url)
    text = _visible_text(soup)
    issues: list[dict] = []
    strengths: list[dict] = []
    for finding in findings:
        title = finding.get("title", "")
        if finding.get("kind") == "strength":
            if finding.get("severity") not in _ISSUE_SEVERITIES:
                strengths.append(finding)
                continue
            logger.info("LLM UX finding %r for %s: %s 'strength'", title, url, finding["severity"])
            finding["kind"] = "issue"
            finding["kind_conflict"] = True
        claim = finding.get("claim_type") or "other"
        if finding.get("scope") == "page" and claims_a_region(finding, soup):
            logger.info("LLM UX finding %r for %s: page scope narrowed to region", title, url)
            finding["scope"] = "region"
        page_scoped = finding.get("scope") == "page"
        probe = CLAIM_PROBES.get(claim)
        absence_confirmed = False
        if probe is not None and page_scoped:
            if homepage and claim in _NOT_APPLICABLE_ON_HOMEPAGE:
                logger.info(
                    "Dropping LLM UX finding %r for %s: %s n/a on a homepage", title, url, claim
                )
                continue
            if probe(soup):
                logger.info("Dropping LLM UX finding %r for %s: %s contradicted", title, url, claim)
                continue
            absence_confirmed = True
        verified = absence_confirmed or evidence_found(finding.get("evidence"), soup, text)
        finding["verified"] = verified
        if not verified and finding.get("severity") == "critical":
            logger.info("Unverified LLM UX finding %r for %s: critical -> high", title, url)
            finding["severity"] = "high"
        issues.append(finding)
    return issues, strengths
