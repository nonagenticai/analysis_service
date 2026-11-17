"""Unit tests for the deterministic CRO scanner (no LLM, no DB, no network).

Each test drives a small fixture HTML page exercising one conversion failure
mode and asserts the expected ``cro-<section>-*`` check_id fires at the expected
severity — plus a "clean" page that should score near-perfect.
"""

from __future__ import annotations

from src.domains.critique.services.scanners.cro import CROScanner

_URL = "https://example.test"


def _findings(html: str) -> list[dict]:
    return CROScanner().scan(html, _URL)


def _ids(html: str) -> set[str]:
    return {f["check_id"] for f in _findings(html)}


def _by_id(html: str, check_id: str) -> dict | None:
    for f in _findings(html):
        if f["check_id"] == check_id:
            return f
    return None


# --------------------------------------------------------------------------
# Form friction — the hardest-evidence section
# --------------------------------------------------------------------------


class TestFormFriction:
    def test_ten_field_form_is_critical(self):
        fields = "".join(
            f'<label for="f{i}">F{i}</label><input id="f{i}" name="f{i}">' for i in range(10)
        )
        html = f"<html><body><form>{fields}<button>Go</button></form></body></html>"
        f = _by_id(html, "cro-form-field-count")
        assert f is not None
        assert f["severity"] == "critical"

    def test_five_field_form_is_low(self):
        fields = "".join(
            f'<label for="f{i}">F{i}</label><input id="f{i}" name="f{i}">' for i in range(5)
        )
        html = f"<html><body><form>{fields}<button>Go</button></form></body></html>"
        f = _by_id(html, "cro-form-field-count")
        assert f is not None and f["severity"] == "low"

    def test_three_field_form_no_count_penalty(self):
        fields = "".join(
            f'<label for="f{i}">F{i}</label><input id="f{i}" name="f{i}">' for i in range(3)
        )
        html = f"<html><body><form>{fields}<button>Go</button></form></body></html>"
        assert "cro-form-field-count" not in _ids(html)

    def test_placeholder_as_label_is_high(self):
        html = (
            "<html><body><form>"
            '<input name="email" placeholder="Email">'
            "<button>Go</button></form></body></html>"
        )
        f = _by_id(html, "cro-form-placeholder-label")
        assert f is not None and f["severity"] == "high"

    def test_no_form_means_no_form_findings(self):
        html = "<html><body><h1>Hi</h1><a class='btn'>Get started</a></body></html>"
        assert not any(i.startswith("cro-form-") for i in _ids(html))

    def test_required_phone_flagged(self):
        html = (
            "<html><body><form>"
            '<label for="p">Phone</label><input id="p" name="phone" required>'
            "<button>Go</button></form></body></html>"
        )
        assert "cro-form-required-friction" in _ids(html)


# --------------------------------------------------------------------------
# Hero / CTA
# --------------------------------------------------------------------------


class TestHeroAndCta:
    def test_missing_h1_is_high(self):
        html = "<html><body><p>No headline here</p><a class='btn'>Get it</a></body></html>"
        f = _by_id(html, "cro-hero-no-h1")
        assert f is not None and f["severity"] == "high"

    def test_no_cta_is_high(self):
        html = "<html><body><h1>Welcome</h1><p>Just text.</p></body></html>"
        f = _by_id(html, "cro-hero-no-cta")
        assert f is not None and f["severity"] == "high"

    def test_generic_only_cta_flagged(self):
        html = "<html><body><h1>Hi</h1><form><input name='e'><button>Submit</button></form></body></html>"
        assert "cro-cta-generic-text" in _ids(html)

    def test_action_verb_cta_not_generic(self):
        html = "<html><body><h1>Hi</h1><a class='btn'>Get my free quote</a></body></html>"
        assert "cro-cta-generic-text" not in _ids(html)

    def test_nav_distraction_flagged(self):
        links = "".join(f"<a href='/{i}'>Link {i}</a>" for i in range(8))
        html = f"<html><body><header><nav>{links}</nav></header><h1>Hi</h1><a class='btn'>Get</a></body></html>"
        assert "cro-hero-nav-distraction" in _ids(html)


# --------------------------------------------------------------------------
# Social proof / value prop / objection
# --------------------------------------------------------------------------


class TestContentSections:
    def test_no_testimonials_is_medium(self):
        html = "<html><body><h1>Hi</h1><a class='btn'>Get</a></body></html>"
        f = _by_id(html, "cro-social-no-testimonials")
        assert f is not None and f["severity"] == "medium"

    def test_testimonial_block_suppresses_missing(self):
        html = (
            "<html><body><h1>Hi</h1><a class='btn'>Get</a>"
            "<blockquote>Great — Jane Doe, CEO at Foo</blockquote></body></html>"
        )
        assert "cro-social-no-testimonials" not in _ids(html)

    def test_long_paragraph_flagged(self):
        words = " ".join(["word"] * 70)
        html = f"<html><body><h1>Hi</h1><a class='btn'>Get</a><p>{words}</p></body></html>"
        assert "cro-valueprop-long-paragraph" in _ids(html)

    def test_faq_details_suppresses_missing(self):
        html = (
            "<html><body><h1>Hi</h1><a class='btn'>Get</a>"
            "<details><summary>FAQ</summary>Answer</details></body></html>"
        )
        assert "cro-objection-no-faq" not in _ids(html)


# --------------------------------------------------------------------------
# Technical friction — reused perf/mobile/a11y checks re-tagged as cro-tech-*
# --------------------------------------------------------------------------


class TestTechFriction:
    def test_missing_viewport_flagged(self):
        html = "<html><head></head><body><h1>Hi</h1><a class='btn'>Get</a></body></html>"
        assert "cro-tech-no-viewport" in _ids(html)

    def test_viewport_present_not_flagged(self):
        html = (
            "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "</head><body><h1>Hi</h1><a class='btn'>Get</a></body></html>"
        )
        assert "cro-tech-no-viewport" not in _ids(html)

    def test_all_cro_findings_carry_category_cro(self):
        html = "<html><body><h1>Hi</h1></body></html>"
        assert all(f["category"] == "cro" for f in _findings(html))


# --------------------------------------------------------------------------
# Clean page — near-perfect
# --------------------------------------------------------------------------

_STRONG_PAGE = """
<html><head><title>Great product</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body>
<header><nav><a>Home</a><a>Pricing</a></nav></header>
<main>
<h1>Cut onboarding time by 40% with Acme</h1>
<p>Short lead copy.</p>
<h2>Benefits</h2>
<ul><li>Fast</li><li>Secure (SOC 2)</li><li>Loved by 12,000+ customers</li></ul>
<a class="btn" href="/signup">Start my free trial</a>
<p>No credit card required. Cancel anytime.</p>
<section class="testimonials"><blockquote>"Saved us hours" — Jane Doe, CEO at Foo</blockquote></section>
<section class="logos"><img><img><img></section>
<div class="rating">★★★★★</div>
<details><summary>FAQ</summary>Answer</details>
<p>Money-back guarantee. <a href="/privacy">Privacy</a></p>
<form><label>Email<input type="email" name="email" autocomplete="email"></label>
<button>Get my quote</button></form>
</main>
<footer><a href="/signup" class="btn">Start free</a><a href="mailto:hi@acme.com">Contact</a></footer>
</body></html>
"""


class TestCleanPage:
    def test_strong_page_has_no_high_or_critical_findings(self):
        sevs = {f["severity"] for f in _findings(_STRONG_PAGE)}
        assert "critical" not in sevs
        assert "high" not in sevs
