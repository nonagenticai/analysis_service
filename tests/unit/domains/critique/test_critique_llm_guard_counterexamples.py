"""Real findings the LLM guards must KEEP.

Each case is an independent auditor's counterexample against PR #228's first
version (b9184ab), where keyword filters suppressed true findings: a footer
``mailto:`` dropped a pricing-page lead-form claim, "clear text" read as praise,
a blanket cap demoted a broken checkout to medium, a score check deleted true
sentences, and title-word dedupe merged different (even opposite) defects.

Only public entry points are used, so the same file runs against the old code
to prove each case failed there.
"""

from __future__ import annotations

import pytest

from src.domains.critique.services import report_generator as rg
from src.domains.critique.services.scanners.ux_reviewer import UXReviewer
from tests.llm_test_doubles import FakeWMClient, install_wm_factory

FOOTER_MAILTO = '<footer><a href="mailto:sales@x.test">sales@x.test</a></footer>'
SEARCH_BOX = '<form role="search"><input type="search" name="q"></form>'


def _item(title: str, severity: str = "high", **fields: str) -> dict:
    base = {"kind": "issue", "claim_type": "other", "scope": "page"}
    return {**base, "severity": severity, "title": title, "description": ".", **fields}


async def _scan(body: str, url: str, *items: dict) -> dict[str, dict]:
    output = {"dimensions": {"form_usability": {"score": 40, "findings": list(items)}}}
    reviewer = UXReviewer(wm_client=FakeWMClient(outputs=[output]))
    findings = await reviewer.scan(f"<html><body>{body}</body></html>", url)
    return {f["title"]: f for f in findings}


# --- contradiction -----------------------------------------------------------


@pytest.mark.parametrize("scope", ["page", "region"])
async def test_mailto_does_not_contradict_a_lead_form_claim(scope: str) -> None:
    title = "No lead capture form on pricing page"
    item = _item(title, claim_type="missing_contact_form", scope=scope)
    found = await _scan(f"<h1>Pricing</h1>{FOOTER_MAILTO}", "https://x.test/pricing", item)
    assert found[title]["severity"] == "high"


async def test_region_contact_claim_is_not_contradicted_by_a_footer_link() -> None:
    title = "Contact information is not visible above the fold"
    item = _item(title, claim_type="missing_contact_method", scope="region")
    found = await _scan(f"<h1>Hi</h1>{FOOTER_MAILTO}", "https://x.test/about", item)
    assert title in found


async def test_conditional_search_claim_survives_a_search_box() -> None:
    title = "No feedback when search returns zero results"
    item = _item(title, "medium", claim_type="missing_search", scope="conditional")
    found = await _scan(SEARCH_BOX, "https://x.test/shop", item)
    assert found[title]["severity"] == "medium"


# --- issue vs strength is the model's structured kind, never title words ------


async def test_clear_text_password_is_a_critical_issue() -> None:
    title = "Password shown in clear text"
    item = _item(title, "critical", evidence='input[name="password"][type="text"]')
    body = '<form><input name="password" type="text"></form>'
    found = await _scan(body, "https://x.test/login", item)
    assert found[title]["severity"] == "critical"


async def test_professional_blurry_certifications_is_an_issue() -> None:
    title = "Professional certifications shown as blurry images"
    found = await _scan("<img src='/cert.png'>", "https://x.test/", _item(title, "medium"))
    assert found[title]["severity"] == "medium"


# --- severity ----------------------------------------------------------------


async def test_verified_broken_checkout_keeps_critical() -> None:
    title = "Checkout button does nothing on click"
    item = _item(title, "critical", evidence="#checkout")
    found = await _scan('<button id="checkout">Pay</button>', "https://x.test/cart", item)
    assert found[title]["severity"] == "critical"


async def test_unverified_broken_checkout_is_capped_at_high_not_medium() -> None:
    title = "Checkout button does nothing on click"
    found = await _scan("<button>Pay</button>", "https://x.test/cart", _item(title, "critical"))
    assert found[title]["severity"] == "high"


# --- summary numbers ---------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    ["Images total 3.5 MB and hurt performance.", "Performance suffers from a 2.8 s LCP."],
)
async def test_summary_keeps_non_score_numbers(
    monkeypatch: pytest.MonkeyPatch, sentence: str
) -> None:
    install_wm_factory(monkeypatch, lambda: FakeWMClient(outputs=[sentence]))
    summary, _ = await rg.generate_summary([], "https://x.test/", {"performance": 59.7})
    assert summary.endswith(sentence)


# Clifford render scores: the auditor's sentences each carry a count that the old
# i-th-category/i-th-number rewrite replaced with one of these.
CLIFFORD_SCORES = {
    "performance": 79.7, "accessibility": 83.5, "security": 56.5, "ux": 61.8, "seo": 89.3,
}  # fmt: skip


@pytest.mark.parametrize(
    "sentence",
    [
        "The performance score is dragged down by 3 render-blocking scripts.",
        "Fixing 12 missing alt tags would lift the SEO score.",
        "Accessibility is 2 points short of a B.",
        "UX scored 61.8 out of 100.",
    ],
)
async def test_summary_counts_and_true_scores_are_never_altered(
    monkeypatch: pytest.MonkeyPatch, sentence: str
) -> None:
    install_wm_factory(monkeypatch, lambda: FakeWMClient(outputs=[sentence]))
    summary, _ = await rg.generate_summary([], "https://x.test/", CLIFFORD_SCORES)
    assert summary.endswith("\n\n" + sentence)


async def test_out_of_100_is_a_scale_not_a_score(monkeypatch: pytest.MonkeyPatch) -> None:
    sentence = "UX scored 52.4 out of 100 (52.4/100)."
    install_wm_factory(monkeypatch, lambda: FakeWMClient(outputs=[sentence]))
    scores = {"security": 100.0, "ux": 52.4}
    summary, _ = await rg.generate_summary([], "https://x.test/", scores)
    assert summary.endswith("\n\n" + sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "The security and performance scores are 56.5 and 79.7 respectively.",
        "Security (56.5) trails performance (79.7).",
        "UX at 61.8 lags performance.",
        "Overall, UX scored 61.8 while SEO reached 89.3.",
    ],
)
async def test_correctly_attributed_multi_category_sentence_is_kept(
    monkeypatch: pytest.MonkeyPatch, sentence: str
) -> None:
    install_wm_factory(monkeypatch, lambda: FakeWMClient(outputs=[sentence]))
    summary, _ = await rg.generate_summary([], "https://x.test/", CLIFFORD_SCORES)
    assert summary.endswith("\n\n" + sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Security (79.7) trails performance (56.5).",
        "UX at 89.3 lags SEO.",
    ],
)
async def test_positionally_swapped_score_sentence_is_dropped(
    monkeypatch: pytest.MonkeyPatch, sentence: str
) -> None:
    install_wm_factory(monkeypatch, lambda: FakeWMClient(outputs=["Keep me.\n\n" + sentence]))
    summary, _ = await rg.generate_summary([], "https://x.test/", CLIFFORD_SCORES)
    assert summary.endswith("\n\nKeep me.")


async def test_summary_drops_a_score_quoted_against_another_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Headers are missing. The security and performance scores are 79.7 and 56.5."
    install_wm_factory(monkeypatch, lambda: FakeWMClient(outputs=[text]))
    summary, _ = await rg.generate_summary([], "https://x.test/", CLIFFORD_SCORES)
    assert summary.endswith("\n\nHeaders are missing.")


# --- contact probes: what is NOT a way to reach the business ------------------

_NO_CONTACT_ITEM = _item("No way to reach the business", claim_type="missing_contact_method")
_NO_FORM_ITEM = _item("No contact form", claim_type="missing_contact_form")
_NO_PHONE_ITEM = _item("No phone number", claim_type="missing_phone")

NEWSLETTER = (
    '<form><label for="nl">Nieuwsbrief</label><input id="nl" type="email" name="email">'
    "<button>Aanmelden</button></form>"
)
NEWSLETTER_WITH_NAME = (
    '<form><input name="name"><input type="email" name="email">'
    "<button type=submit>Subscribe</button></form>"
)
#: A lone email field whose name happens to carry a message word is still a newsletter.
NEWSLETTER_MESSAGE_WORD = (
    '<form><input type="email" name="enquiry_updates" placeholder="E-mail">'
    "<button>Send</button></form>"
)
LOGIN = (
    '<form><input type="email" name="email"><input type="password" name="password">'
    "<button>Log in</button></form>"
)

#: An account sign-up with a free-text field is still not a way to contact anyone.
SIGNUP = (
    '<form><input name="naam"><input type="email" name="email">'
    '<input type="password" name="wachtwoord"><textarea name="opmerking"></textarea>'
    "<button>Account aanmaken</button></form>"
)


#: The auditor's counterexample: the stock WordPress comment form (name, email,
#: textarea, "Post comment") has every field a contact form has.
WP_COMMENT = (
    '<div id="respond"><h3 id="reply-title">Leave a Reply</h3>'
    '<form action="https://x.test/wp-comments-post.php" method="post" id="commentform">'
    '<label for="comment">Comment</label><textarea id="comment" name="comment"></textarea>'
    '<label for="author">Name</label><input id="author" name="author">'
    '<label for="email">Email</label><input id="email" name="email" type="email">'
    '<input name="submit" type="submit" value="Post Comment">'
    '<input type="hidden" name="comment_post_ID" value="42"></form></div>'
)
#: A search box whose placeholder carries a message word.
SEARCH_MESSAGES = (
    '<form action="/inbox"><input type="text" name="term" placeholder="Search messages">'
    "<button>Go</button></form>"
)
SEARCH_TEXTAREA = (
    '<form role="search" action="/"><textarea name="s" placeholder="Zoek berichten"></textarea>'
    "<button>Zoek</button></form>"
)
REVIEW = (
    '<form class="product-reviews"><input name="naam"><input type="email" name="email">'
    '<textarea name="tekst"></textarea><button>Plaats review</button></form>'
)
REPLY = (
    '<form><input name="name"><input type="email" name="email">'
    '<textarea name="body" aria-label="Your message"></textarea><button>Reply</button></form>'
)
DUTCH_REACTIE = (
    '<form><input name="naam"><input type="email" name="email">'
    '<textarea name="tekst"></textarea><button>Plaats reactie</button></form>'
)

#: A free-text box with no contact intent and no name/email is not a way to reach anyone.
NOTES = '<form><textarea name="notes"></textarea><button>Opslaan</button></form>'


@pytest.mark.parametrize(
    "form",
    [
        NEWSLETTER,
        NEWSLETTER_WITH_NAME,
        NEWSLETTER_MESSAGE_WORD,
        LOGIN,
        SIGNUP,
        WP_COMMENT,
        SEARCH_MESSAGES,
        SEARCH_TEXTAREA,
        REVIEW,
        REPLY,
        DUTCH_REACTIE,
        NOTES,
    ],
)
@pytest.mark.parametrize("item", [_NO_CONTACT_ITEM, _NO_FORM_ITEM])
async def test_newsletter_or_login_form_is_not_a_contact_form(form: str, item: dict) -> None:
    found = await _scan(f"<h1>Hi</h1>{form}", "https://x.test/about", dict(item))
    assert item["title"] in found


@pytest.mark.parametrize(
    "form",
    [
        '<form><textarea name="bericht"></textarea><button>Verstuur</button></form>',
        '<form><input name="naam"><input type="email" name="email">'
        "<button>Neem contact op</button></form>",
        '<form><label>Uw vraag <input name="q2"></label><input type="email"><button>Go</button></form>',
        # name + email + free text, no comment/search/review marker anywhere
        '<form><input name="name"><input type="email" name="email">'
        '<textarea name="body"></textarea><button type="submit">Submit</button></form>',
        # a bare textarea whose heading says what it is for
        '<h2>Vraag een offerte aan</h2><form><textarea name="t"></textarea><button>OK</button></form>',
    ],
)
async def test_a_real_contact_form_disproves_both_claims(form: str) -> None:
    for item in (_NO_CONTACT_ITEM, _NO_FORM_ITEM):
        found = await _scan(f"<h1>Hi</h1>{form}", "https://x.test/about", dict(item))
        assert item["title"] not in found


@pytest.mark.parametrize(
    "footer",
    [
        "IBAN: NL91 ABNA 0417 1643 00",
        "NL91ABNA0417164300",
        "BTW-nummer NL 8123 45678 B01",
        "btw-id: NL812345678B01",
        "KvK 0412 3456 78",
        "KvK-nummer: 04123456",
        "Postbus 1234, 6800 AB Arnhem",
    ],
)
async def test_business_identifiers_are_not_a_phone_or_contact_method(footer: str) -> None:
    body = f"<h1>Hi</h1><footer><p>{footer}</p></footer>"
    for item in (_NO_PHONE_ITEM, _NO_CONTACT_ITEM):
        found = await _scan(body, "https://x.test/about", dict(item))
        assert item["title"] in found, footer


@pytest.mark.parametrize("number", ["06 42 24 90 79", "+31 (0)26 123 4567", "0031 6 4224 9079"])
async def test_a_written_phone_number_disproves_missing_phone(number: str) -> None:
    body = f"<footer><p>Bel ons: {number}</p></footer>"
    found = await _scan(body, "https://x.test/about", dict(_NO_PHONE_ITEM))
    assert _NO_PHONE_ITEM["title"] not in found


@pytest.mark.parametrize(
    "footer",
    ["Bel ons: 06 42 24 90 79", "Mail: info@clifford.test", "info [at] x.test of 0264567890"],
)
async def test_a_written_number_or_address_disproves_no_way_to_reach(footer: str) -> None:
    body = f"<footer><p>{footer}</p></footer>"
    found = await _scan(body, "https://x.test/about", dict(_NO_CONTACT_ITEM))
    assert _NO_CONTACT_ITEM["title"] not in found


@pytest.mark.parametrize(
    "footer", ["KvK 12345678 010 123 4567", "KvK-nummer: 12345678 06 42 24 90 79"]
)
async def test_an_identifier_does_not_swallow_the_phone_number_after_it(footer: str) -> None:
    body = f"<footer><p>{footer}</p></footer>"
    for item in (_NO_PHONE_ITEM, _NO_CONTACT_ITEM):
        found = await _scan(body, "https://x.test/about", dict(item))
        assert item["title"] not in found, footer


# --- kind / scope guards -----------------------------------------------------


async def test_high_severity_strength_stays_an_issue_with_kind_conflict() -> None:
    title = "Checkout flow is great"
    item = _item(title, "high", kind="strength")
    found = await _scan("<p>x</p>", "https://x.test/", item)
    assert found[title]["kind_conflict"] is True
    assert found[title]["severity"] == "high"


async def test_low_severity_strength_is_not_scored() -> None:
    title = "Clear hero copy"
    found = await _scan("<p>x</p>", "https://x.test/", _item(title, "info", kind="strength"))
    assert title not in found


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "No contact details above the fold"},
        {"title": "No contact option", "description": "Het mobiele menu biedt geen contact."},
        {"title": "No contact option", "evidence": "footer .contact"},
        {"title": "No contact option", "evidence": "#pricing-cta"},
        {"title": "No contact option", "evidence": "div.adres"},
        {"title": "No contact option", "evidence": "a.koop"},
    ],
)
async def test_region_qualified_page_claim_is_treated_as_region(fields: dict) -> None:
    body = (
        '<header><a href="tel:+31642249079">bel</a></header>'
        '<section id="pricing"><a id="pricing-cta" href="#">Koop</a></section>'
        '<footer><div class="contact adres">Adres</div></footer>'
        '<div class="modal"><a class="koop" href="#">Koop nu</a></div>'
    )
    item = _item("", claim_type="missing_contact_method") | fields
    found = await _scan(body, "https://x.test/about", item)
    assert found[fields["title"]]["scope"] == "region"


# --- score stability under a nondeterministic model --------------------------

_CART = '<h1>Cart</h1><button id="checkout">Pay</button>'
_VERIFIED = _item("Checkout button does nothing on click", "high", evidence="#checkout")


async def _ux_score(*items: dict) -> float:
    output = {"dimensions": {"form_usability": {"score": 40, "findings": list(items)}}}
    findings = await UXReviewer(wm_client=FakeWMClient(outputs=[output])).scan(
        f"<html><body>{_CART}</body></html>", "https://x.test/cart"
    )
    return rg.compute_scores(findings)["ux"]


async def test_differing_unverified_llm_findings_give_an_identical_ux_score() -> None:
    run_a = await _ux_score(_VERIFIED, _item("Dense copy", "critical"), _item("Weak CTA", "high"))
    run_b = await _ux_score(_VERIFIED, _item("Low contrast footer", "medium"))
    assert run_a == run_b
    # The verified finding still moves the score; only unverified claims are inert.
    assert run_a < 100.0


# --- a category with no verified evidence is NOT SCORED ----------------------

_SEO_VERIFIED = {
    "category": "seo",
    "severity": "high",
    "title": "Missing title",
    "check_id": "seo-t",
}
_SEC_VERIFIED = {
    "category": "security",
    "severity": "medium",
    "title": "No CSP",
    "check_id": "sec-c",
}


async def _ux_findings(*items: dict) -> list[dict]:
    output = {"dimensions": {"form_usability": {"score": 40, "findings": list(items)}}}
    return await UXReviewer(wm_client=FakeWMClient(outputs=[output])).scan(
        f"<html><body>{_CART}</body></html>", "https://x.test/cart"
    )


async def test_all_unverified_category_is_not_scored_and_overall_is_renormalised() -> None:
    ux = await _ux_findings(*(_item(f"Claim {i}", "medium") for i in range(9)))
    assert len(ux) == 9 and all(f["verified"] is False for f in ux)
    findings = [*ux, _SEO_VERIFIED, _SEC_VERIFIED]

    scores = rg.compute_scores(findings)
    assert "ux" not in scores
    assert rg.unscored_categories(findings) == {"ux": 9}
    # Renormalised over the two scored categories: UX's weight does not dilute them.
    assert rg.compute_overall_score(scores) == rg.compute_overall_score(
        rg.compute_scores([_SEO_VERIFIED, _SEC_VERIFIED])
    )
    assert rg.compute_overall_score(scores) < rg.compute_overall_score({**scores, "ux": 100.0})


async def test_mixed_verified_and_unverified_category_gets_a_number() -> None:
    findings = [*await _ux_findings(_VERIFIED, _item("Dense copy", "critical")), _SEO_VERIFIED]
    scores = rg.compute_scores(findings)
    assert 0.0 < scores["ux"] < 100.0
    assert rg.unscored_categories(findings) == {}


async def test_two_different_llm_runs_give_an_identical_overall_score() -> None:
    run_a = [*await _ux_findings(_item("Dense copy", "critical"), _item("Weak CTA", "high")),
             _SEO_VERIFIED, _SEC_VERIFIED]  # fmt: skip
    run_b = [*await _ux_findings(*(_item(f"Other {i}", "low") for i in range(5))),
             _SEO_VERIFIED, _SEC_VERIFIED]  # fmt: skip
    overall_a = rg.compute_overall_score(rg.compute_scores(run_a))
    overall_b = rg.compute_overall_score(rg.compute_scores(run_b))
    assert overall_a == overall_b


def test_unscored_category_renders_without_number_grade_or_radar_axis() -> None:
    from src.domains.critique.services.html_report_generator import generate_html_report

    findings = [
        {"category": "ux", "severity": "high", "title": f"Claim {i}", "verified": False}
        for i in range(9)
    ] + [
        {**_SEO_VERIFIED, "severity": "low"},
        _SEC_VERIFIED,
        {"category": "performance", "severity": "low", "title": "Big JS", "check_id": "p"},
    ]
    scores = rg.compute_scores(findings)
    overall = rg.compute_overall_score(scores)
    # A stale stored ``ux: 100.0`` must not resurface either.
    html = generate_html_report("https://x.test/", findings, {**scores, "ux": 100.0}, "s", overall)

    assert "<h2>UX (Not scored — 9 unverified findings) - 9 findings</h2>" in html
    assert "Not scored — 9 unverified findings</div>" in html  # the score card
    assert "UX (A+)" not in html and "Score: 100" not in html
    assert "Overall score based on 3 of 4 categories" in html
    radar = html[html.index("<svg") : html.index("</svg>")]
    assert ">UX<" not in radar  # no axis, so no 0 or 100 spike

    md = rg.generate_markdown_report("https://x.test/", findings, scores, "s")
    assert "| UX | Not scored — 9 unverified findings |" in md
    assert "Overall score based on 3 of 4 categories" in md


def test_unverified_findings_are_listed_with_a_badge() -> None:
    from src.domains.critique.services.html_report_generator import generate_html_report

    finding = {"category": "ux", "severity": "high", "title": "Dense copy", "verified": False}
    html = generate_html_report(
        "https://x.test/", [finding], {"ux": 100.0}, "s", overall_score=100.0
    )
    assert 'Dense copy <span class="unverified-badge"' in html


def test_unscored_findings_are_counted_in_printed_text_next_to_the_score() -> None:
    """A PDF has no tooltip: the heading itself must say what the score leaves out."""
    from src.domains.critique.services.html_report_generator import generate_html_report

    findings = [
        {"category": "ux", "severity": "high", "title": f"Claim {i}", "verified": False}
        for i in range(9)
    ] + [
        {"category": "ux", "severity": "low", "title": "Seen", "verified": True},
        {"category": "seo", "severity": "low", "title": "Meta", "verified": True},
    ]
    html = generate_html_report(
        "https://x.test/", findings, {"ux": 89.3, "seo": 70.0}, "s", overall_score=80.0
    )
    assert "<h2>UX (Score: 89.3) (9 unverified, not scored) - 10 findings</h2>" in html
    assert "9 unverified, not scored</div>" in html  # the score card too
    assert "Ux" not in html
    assert "<h2>SEO (Score: 70.0) - 1 findings</h2>" in html  # no note when nothing is unscored


# --- dedupe ------------------------------------------------------------------


def test_distinct_defects_are_never_merged() -> None:
    url = "https://x.test/"
    findings = [
        {"category": "accessibility", "severity": "high", "title": "4 images missing alt text",
         "check_id": "a11y-img-missing-alt", "url": url, "source_tool": "a11y"},
        {"category": "seo", "severity": "medium", "title": "Images missing alt attribute",
         "check_id": "seo-images-missing-alt", "url": url, "source_tool": "seo"},
        {"category": "accessibility", "severity": "low", "title": "2 images with empty alt text",
         "check_id": "a11y-img-empty-alt", "url": url, "source_tool": "a11y"},
        {"category": "performance", "severity": "low", "title": "28 elements with inline styles",
         "check_id": "perf-excessive-inline-styles", "url": url, "source_tool": "perf"},
        {"category": "ux", "severity": "medium", "title": "Focus outline removed via inline style",
         "claim_type": "other", "scope": "region", "url": url, "source_tool": "ux_reviewer"},
        {"category": "performance", "severity": "medium",
         "title": "2 below-fold images without lazy loading", "check_id": "perf-no-lazy-loading",
         "url": url, "source_tool": "perf"},
        {"category": "ux", "severity": "medium", "title": "Hero image is lazy-loaded",
         "claim_type": "other", "scope": "region", "url": url, "source_tool": "ux_reviewer"},
    ]  # fmt: skip
    titles = [f["title"] for f in rg.deduplicate_findings(findings)]
    # Only the explicit EQUIVALENT_CHECKS pair (seo alt == a11y alt) merges.
    assert titles == [
        "4 images missing alt text",
        "2 images with empty alt text",
        "28 elements with inline styles",
        "Focus outline removed via inline style",
        "2 below-fold images without lazy loading",
        "Hero image is lazy-loaded",
    ]


# --- submit-only contact forms: a quote/appointment request is contact ------

_QUOTE_FORM = (
    '<form><input name="naam"><input type="email" name="e-mail"><input type="tel" name="telefoon">'
    "<button>Offerte aanvragen</button></form>"
)
_BOOKING_FORM = (
    '<form><label>Naam <input name="n"></label><label>E-mail <input type="email" name="m"></label>'
    '<input type="date" name="datum"><button type="submit">Plan een gesprek</button></form>'
)
_NEWSLETTER_NAMED = (
    '<form><input name="naam"><input type="email" name="email"><button>Aanmelden</button></form>'
)
#: Borrows "aanvragen" but is a sign-up: the veto must beat the contact word.
_NEWSLETTER_AANVRAGEN = (
    '<form><input name="naam"><input type="email" name="email">'
    "<button>Nieuwsbrief aanvragen</button></form>"
)


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        (_QUOTE_FORM, True),
        (_BOOKING_FORM, True),
        (_NEWSLETTER_NAMED, False),
        (_NEWSLETTER_AANVRAGEN, False),
        ('<form><input name="naam"><input type="email"><button>Subscribe</button></form>', False),
    ],
)
def test_submit_label_intent_decides_a_form_without_a_message_box(
    form: str, expected: bool
) -> None:
    from bs4 import BeautifulSoup

    from src.domains.critique.services.scanners.ux_claims import has_contact_form

    assert has_contact_form(BeautifulSoup(f"<h1>Hi</h1>{form}", "lxml")) is expected


async def test_a_quote_request_form_disproves_missing_contact_form() -> None:
    for item in (_NO_CONTACT_ITEM, _NO_FORM_ITEM):
        found = await _scan(f"<h1>Hi</h1>{_QUOTE_FORM}", "https://x.test/about", dict(item))
        assert item["title"] not in found


# --- zero scored categories: the OVERALL is not scored, in every format -----

_ALL_UNVERIFIED = [
    {"category": "ux", "severity": "high", "title": "Claim a", "verified": False},
    {"category": "cro", "severity": "medium", "title": "Claim b", "verified": False},
]


def test_zero_scored_categories_print_no_overall_number_or_grade_in_html_and_markdown() -> None:
    from src.domains.critique.services.html_report_generator import generate_html_report

    scores = rg.compute_scores(_ALL_UNVERIFIED)
    assert scores == {} and rg.overall_is_unscored(scores, rg.unscored_categories(_ALL_UNVERIFIED))
    # The pipeline stores 100.0 for a scan with no scored category.
    html = generate_html_report("https://x.test/", _ALL_UNVERIFIED, {}, "s", 100.0)
    assert "Score: Not scored (Not scored — none of 2 categories has verified evidence)" in html
    assert "100.0" not in html and "A+" not in html and "0 of 2" not in html

    md = rg.generate_markdown_report("https://x.test/", _ALL_UNVERIFIED, scores, "s")
    assert "**Overall Score: Not scored**" in md
    assert "/100" not in md and "0 of 2" not in md


def test_a_clean_scan_still_has_an_overall_score() -> None:
    assert not rg.overall_is_unscored({}, {})
    assert not rg.overall_is_unscored({"seo": 90.0}, {"ux": 2})


async def test_zero_scored_categories_json_export_has_no_overall_or_grade() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from src.domains.critique.routes import get_critique_report
    from src.domains.critique.schemas import CritiqueJSONReportResponse

    job = MagicMock(
        job_id="j", status="completed", url="https://x.test/",
        findings=[{**f, "finding_id": f["title"], "description": "d"} for f in _ALL_UNVERIFIED],
        category_scores={"ux": 100.0}, overall_score=100.0, summary="s", scan_duration=1.0,
        pages_scanned=1, schema_version="2.0",
    )  # fmt: skip
    request = MagicMock()
    request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
    request.app.state.critique_service = AsyncMock(get_job=AsyncMock(return_value=job))

    report = await get_critique_report("j", request, "json", AsyncMock())
    assert isinstance(report, CritiqueJSONReportResponse)
    assert report.overall_score is None and report.grade is None
    assert report.category_scores == {}  # a stale stored ux score does not resurface
    assert report.metadata["unscored_categories"] == {"cro": 1, "ux": 1}
