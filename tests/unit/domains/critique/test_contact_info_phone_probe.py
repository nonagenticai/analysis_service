"""A printed phone number is contact information -- a printed SKU is not.

The customer-facing sentence is authored twice (``seo/eeat.py`` -> ``seo-eeat-no-contact``
and ``marketing/brand.py`` -> ``mkt-brand-no-contact-info``). Neither consulted a phone
probe, so a site whose footer simply prints "Bel ons: 020 123 4567" -- no ``tel:`` link,
no "Contact" nav item -- was told in a delivered report that it has no contact
information.

Consulting the raw ``has_phone`` probe fixes that and buys a WORSE bug: ``has_phone``
accepts anything normalising to ``0[1-9]\\d{8}``, i.e. any ten-digit run starting with
0, with no requirement that the page presents it as a phone. An order number, SKU,
tracking code, bare bank account or ISBN-10 all qualify, so a page with no contact
details at all would have the finding SILENTLY suppressed -- a false negative, which
nobody can see and nobody reports.

Both directions are pinned here, at both sites: presented-as-a-phone must NOT raise the
finding, and a bare digit run must STILL raise it. The call sites therefore use
``has_labelled_phone``, which requires phone-shaped context.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.marketing import MarketingScanner
from src.domains.critique.services.scanners.seo.eeat import check_eeat

#: Numbers a page presents AS a phone number. None of these carry a literal "tel:" or
#: "contact us" string, so only the phone probe can see them -- ``CONTACT_RE`` in
#: ``marketing/base.py`` matches the literal ``tel:``, which would make a ``Tel:``
#: label pass for the wrong reason.
PHONE_BODIES = {
    "dutch_imperative_label": "<footer><p>Bel ons: 020 123 4567</p></footer>",
    "dutch_noun_label": "<footer><p>Telefoon 020 123 4567</p></footer>",
    "abbreviated_label": "<footer><p>T: 020 123 4567</p></footer>",
    "english_label_international": "<footer><p>Call us +31 20 123 4567</p></footer>",
    "class_names_the_element": '<footer><span class="phone">020 123 4567</span></footer>',
    "microdata_itemprop": '<footer><span itemprop="telephone">020 123 4567</span></footer>',
    # TWO-WORD labels. The first review of this fix shipped ``\w*`` tails, which cover
    # only the concatenated compound ("telefoonnummer") -- a space leaves a second word
    # between the label and the digits, which a separators-only tail cannot cross. So
    # the most common English form of the label was still falsely accused.
    "english_two_word_label": "<footer><p>Phone number: 020 123 4567</p></footer>",
    "dutch_two_word_label": "<footer><p>Telefoon nummer: 020 123 4567</p></footer>",
    "abbreviated_two_word_label": "<footer><p>Tel. nr. 020 123 4567</p></footer>",
    "two_intervening_words": "<footer><p>Telefoon nr kantoor: 020 123 4567</p></footer>",
    # Same gap in the attribute lane: a ``\b``-anchored "telefoon" cannot end inside
    # "telefoonnummer", the way Dutch markup overwhelmingly spells it.
    "class_compound_nl": '<footer><span class="telefoonnummer">020 123 4567</span></footer>',
    "aria_label_compound_nl": (
        '<footer><span aria-label="Telefoonnummer">020 123 4567</span></footer>'
    ),
    # AN ORDINARY WEBSHOP STILL PRINTS ITS PHONE NUMBER. The product denylist added to
    # close "Telefoonhoesjes SKU 0412889012" reached too far: it also carried
    # "shop|categor|winkel", which name the SHOP rather than the goods, and it is
    # matched against the WHOLE 40-character pre-digit window -- so any of those words
    # ANYWHERE in the window disqualified a real, correctly labelled phone. The first
    # case below was driven through both call sites and emitted "No contact
    # information": the precise false accusation this whole change exists to stop.
    # These are not contrived -- ``get_text(" ")`` flattens a ``Shop`` nav item or a
    # "Categorieen" menu into the window of a phone number printed in the footer.
    "webshop_word_in_the_footer": "<footer>Webshop Nederland Telefoon: 020 123 4567</footer>",
    "winkel_prefixes_the_label": "<p>Onze winkel: Telefoon 020 123 4567</p>",
    "shop_nav_item_flattens_into_the_window": (
        '<footer><a href="/shop">Shop</a><p>Tel: 020 123 4567</p></footer>'
    ),
    "category_nav_flattens_into_the_window": (
        "<nav>Categorieen</nav><footer><p>T: 020 123 4567</p></footer>"
    ),
    "webshop_sentence_before_the_label": (
        "<footer><p>Onze webshop is 24/7 open. Bel ons: 020 123 4567</p></footer>"
    ),
    # The ATTRIBUTE lane had the identical hole: Dutch markup spells the class exactly
    # this way, and "webshop" disqualified the element outright.
    "class_webshop_telefoon": (
        '<footer><span class="webshop-telefoon">020 123 4567</span></footer>'
    ),
}

#: Ten-digit runs starting with 0 that are NOT phone numbers. Every one of these
#: returns True from the raw ``has_phone`` probe -- that is precisely the defect.
NON_PHONE_BODIES = {
    "sku": "<footer><p>SKU 0412-8890-12</p><p>All rights reserved.</p></footer>",
    "order_number": "<footer><p>Bestelnummer: 0123456789</p></footer>",
    "tracking_number": "<footer><p>Track &amp; trace 0345678901</p></footer>",
    "bare_bank_account": "<footer><p>Rekeningnummer 0417164300</p></footer>",
    "isbn_10": "<footer><p>ISBN 0-306-40615-2</p></footer>",
    "invoice_number": "<footer><p>Factuur 0998877665</p></footer>",
    # An ancestor whose markup names a phone must not vouch for a descendant SKU.
    "phone_named_wrapper_around_sku": (
        '<footer id="telephone"><div><p>SKU 0412-8890-12</p></div></footer>'
    ),
    # A shop that SELLS phones is not a shop that PRINTS one. Widening the label to
    # reach across a word made "Telefoonhoesjes SKU 0412889012" readable as a labelled
    # phone, and the attribute lane already read ``class="phone-cases"`` that way.
    "product_class_phone_cases": (
        '<footer><div class="phone-cases">Telefoonhoesjes SKU 0412889012</div></footer>'
    ),
    "product_title_phone_cases": (
        '<footer><table><tr><td title="phone cases">0412889012</td></tr></table></footer>'
    ),
    # THE BOUND. A phone word and a digit run with three real words between them is a
    # running sentence, not a label. If the bounded gap is ever widened towards ``.*``
    # this is the test that goes red -- without it the bound is untested.
    "label_three_words_from_the_digits": (
        "<footer><p>Telefoon van onze klanten 0123456789</p></footer>"
    ),
    # The original control: nothing resembling contact details at all.
    "nothing_at_all": "<footer><p>All rights reserved.</p></footer>",
}


def _eeat_ids(body: str) -> set[str | None]:
    soup = BeautifulSoup(
        f"<html><body><main><p>We do cloud backups.</p></main>{body}</body></html>", "lxml"
    )
    return {f.get("check_id") for f in check_eeat(soup, "https://example.test/x")}


def _marketing_ids(body: str) -> set[str | None]:
    html = f"<html><body><main><p>We do cloud backups.</p></main>{body}</body></html>"
    return {f.get("check_id") for f in MarketingScanner().scan(html, "https://example.test/x")}


class TestEeatContactPhoneProbe:
    @pytest.mark.parametrize("body", PHONE_BODIES.values(), ids=list(PHONE_BODIES))
    def test_phone_presented_as_a_phone_is_not_no_contact_information(self, body: str):
        assert "seo-eeat-no-contact" not in _eeat_ids(body)

    @pytest.mark.parametrize("body", NON_PHONE_BODIES.values(), ids=list(NON_PHONE_BODIES))
    def test_digit_run_that_is_not_a_phone_is_still_flagged(self, body: str):
        assert "seo-eeat-no-contact" in _eeat_ids(body)


class TestContactHeadingAsymmetryIsDeliberate:
    """The word "contact" counts in the TEXT lane and not in the ATTRIBUTE lane.

    That is a decision, not an oversight (owner ruling, 2026-09-15). A number under a
    "Contact" heading is a real and very common presentation of a phone number, so
    reading it as contact information is correct. An attribute is different: a class
    or id is inherited by a whole subtree, so ``class="contact-info"`` would vouch for
    every digit run in the footer. This pins both halves so neither is "fixed" into
    the other's behaviour. Unlike the cases above it passes on the pre-fix code too --
    it guards a decision rather than closing a hole.
    """

    def test_a_number_under_a_contact_heading_is_contact_information(self):
        body = "<footer><h3>Contact</h3><p>0412889012</p></footer>"
        assert "seo-eeat-no-contact" not in _eeat_ids(body)

    def test_a_contact_class_on_the_element_itself_does_not_make_a_sku_a_phone(self):
        # The SKU is the element's OWN text, so the own-strings restriction cannot be
        # what saves this: only "contact" being absent from ``_PHONE_ATTR_RE`` does.
        # Adding it there turns this red -- which is the point of the asymmetry.
        body = '<footer><p class="contact-info">SKU 0412-8890-12</p></footer>'
        assert "seo-eeat-no-contact" in _eeat_ids(body)


class TestBrandContactPhoneProbe:
    @pytest.mark.parametrize("body", PHONE_BODIES.values(), ids=list(PHONE_BODIES))
    def test_phone_presented_as_a_phone_is_not_no_contact_information(self, body: str):
        assert "mkt-brand-no-contact-info" not in _marketing_ids(body)

    @pytest.mark.parametrize("body", NON_PHONE_BODIES.values(), ids=list(NON_PHONE_BODIES))
    def test_digit_run_that_is_not_a_phone_is_still_flagged(self, body: str):
        assert "mkt-brand-no-contact-info" in _marketing_ids(body)
