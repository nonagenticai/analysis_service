"""Static CRO experiment catalog (design 2026-10-17 CRO-G4).

One authored A/B hypothesis per ``cro-*`` check where a test is the right way
to validate the fix: the hypothesis, the primary metric to read, and the
variant to build. Deterministic data — no LLM. Checks that are plain defects
(a missing viewport meta, an oversized DOM, unsized images, a duplicate H1) are
deliberately NOT listed: they should just be fixed, and the report renders
"hypothesis: not authored" for them.
"""

from __future__ import annotations

from typing import TypedDict


class Experiment(TypedDict):
    """One authored experiment: why it should win, what to measure, what to build."""

    hypothesis: str
    primary_metric: str
    variant: str


_FORM_SUBMIT = "form submission rate"
_CTA_CTR = "primary CTA click-through rate"
_VISIT_CONV = "visit-to-lead conversion rate"


def _x(hypothesis: str, primary_metric: str, variant: str) -> Experiment:
    return {"hypothesis": hypothesis, "primary_metric": primary_metric, "variant": variant}


CRO_EXPERIMENTS: dict[str, Experiment] = {
    # form
    "cro-form-field-count": _x(
        "Cutting the form to its 3-5 essential fields raises completions, because each "
        "extra field adds effort before any value is delivered.",
        _FORM_SUBMIT,
        "Same form with every non-essential field removed (collect the rest after signup).",
    ),
    "cro-form-textarea": _x(
        "Making the free-text field optional removes the highest-effort step and lifts "
        "completions.",
        _FORM_SUBMIT,
        "Textarea marked optional, or removed and replaced by a follow-up email.",
    ),
    "cro-form-multiple-selects": _x(
        "Pre-filling or removing dropdowns shortens the form and lifts completions.",
        _FORM_SUBMIT,
        "Dropdowns replaced by sensible defaults or inferred values.",
    ),
    "cro-form-placeholder-label": _x(
        "Persistent labels above each field reduce recall errors and failed submits, "
        "especially on mobile.",
        "form error rate",
        "Every field gets a visible <label>; the placeholder only shows an example.",
    ),
    "cro-form-input-type": _x(
        "The correct mobile keyboard for the email field reduces typos and abandonment.",
        "mobile form submission rate",
        'Email input set to type="email" with autocomplete="email".',
    ),
    "cro-form-autocomplete-missing": _x(
        "Letting the browser autofill the form cuts typing time and lifts completions.",
        "time to complete form",
        "autocomplete tokens (name, email, tel) added to every matching field.",
    ),
    "cro-form-required-friction": _x(
        "Making phone/company optional removes the main abandonment trigger without "
        "hurting lead quality.",
        _FORM_SUBMIT,
        "Phone and company fields optional (or moved to a second step).",
    ),
    "cro-form-no-multistep": _x(
        "Splitting a long form into short steps lowers perceived effort and raises completions.",
        "form completion rate",
        "Same fields across 2-3 steps with a progress indicator; easiest fields first.",
    ),
    # hero
    "cro-hero-no-h1": _x(
        "A clear headline stating the core benefit tells visitors within seconds that "
        "they are in the right place, lowering bounce.",
        "bounce rate",
        "One <h1> that names the outcome the visitor gets.",
    ),
    "cro-hero-headline-length": _x(
        "A 6-12 word headline is read in one glance, so more visitors take in the "
        "value proposition.",
        "bounce rate",
        "Headline rewritten to 6-12 words focused on the benefit.",
    ),
    "cro-hero-no-cta": _x(
        "A visible primary CTA in the hero gives ready visitors a way to act.",
        _CTA_CTR,
        "One prominent primary button in the hero section.",
    ),
    "cro-hero-nav-distraction": _x(
        "Fewer competing navigation links concentrate attention on the conversion goal.",
        _VISIT_CONV,
        "Top navigation trimmed to the essentials (or removed) on this page.",
    ),
    "cro-hero-no-trust-signal": _x(
        "A trust mark near the headline reduces first-impression risk.",
        _CTA_CTR,
        "A guarantee, security badge or review-score badge beside the hero CTA.",
    ),
    # cta
    "cro-cta-generic-text": _x(
        "Benefit-led action copy tells visitors what they get, so more of them click.",
        _CTA_CTR,
        "CTA text starting with an action verb tied to the benefit ('Get my free quote').",
    ),
    "cro-cta-no-first-person": _x(
        "First-person CTA copy ('Start my trial') increases ownership and clicks.",
        _CTA_CTR,
        "Primary CTA copy changed from 'your' to 'my' phrasing.",
    ),
    "cro-cta-no-microcopy": _x(
        "A reassurance line under the CTA lowers click anxiety.",
        _CTA_CTR,
        "Microcopy under the primary CTA ('No credit card required - cancel anytime').",
    ),
    "cro-cta-single-on-long-page": _x(
        "Repeating the primary CTA as visitors scroll captures intent at more points.",
        _CTA_CTR,
        "The primary CTA repeated every ~1-1.5 viewports.",
    ),
    # social
    "cro-social-no-testimonials": _x(
        "Customer testimonials near the CTA reduce doubt and lift conversion.",
        _VISIT_CONV,
        "2-3 named customer testimonials placed before the primary CTA.",
    ),
    "cro-social-unnamed-testimonials": _x(
        "Named, role-attributed testimonials read as real and persuade more.",
        _VISIT_CONV,
        "Each testimonial attributed to a name, role and company (photo if possible).",
    ),
    "cro-social-no-logos": _x(
        "A strip of recognisable customer logos borrows authority and builds trust.",
        _VISIT_CONV,
        "'Trusted by' logo strip below the hero.",
    ),
    "cro-social-no-ratings": _x(
        "An aggregate star rating gives a fast, concrete trust cue.",
        _VISIT_CONV,
        "Star rating with review count shown near the CTA.",
    ),
    "cro-social-no-metrics": _x(
        "A specific usage number ('12,000+ teams') makes proof concrete.",
        _VISIT_CONV,
        "Headline metric or 'as seen in' press line under the hero.",
    ),
    # valueprop
    "cro-valueprop-no-quantified-benefit": _x(
        "A quantified benefit is more believable than a vague claim and lifts intent.",
        _CTA_CTR,
        "Headline or lead line with a concrete number ('Cut onboarding time by 40%').",
    ),
    "cro-valueprop-few-bullets": _x(
        "Scannable benefit bullets let visitors grasp the offer without reading prose.",
        "scroll depth to primary CTA",
        "Top 3-5 benefits as a bulleted list under the lead paragraph.",
    ),
    "cro-valueprop-long-paragraph": _x(
        "Short paragraphs are read rather than skipped, so more of the message lands.",
        "scroll depth to primary CTA",
        "Long paragraphs split into one-idea blocks of ~40 words.",
    ),
    "cro-valueprop-no-subheads": _x(
        "Descriptive subheads let scanning visitors find the part they care about.",
        "scroll depth to primary CTA",
        "Page broken into sections with benefit-led <h2> subheads.",
    ),
    "cro-valueprop-reading-grade": _x(
        "Simpler copy (grade 7-8) is understood by more visitors and converts better.",
        _VISIT_CONV,
        "Copy rewritten with shorter sentences and plainer words.",
    ),
    # objection
    "cro-objection-no-faq": _x(
        "Answering the top objections on the page removes reasons to delay.",
        _VISIT_CONV,
        "FAQ answering the top 4-6 buyer objections, placed above the final CTA.",
    ),
    "cro-objection-no-risk-reversal": _x(
        "A guarantee or free trial shifts risk off the visitor and lifts conversion.",
        _VISIT_CONV,
        "Money-back guarantee or free-trial line beside the primary CTA.",
    ),
    "cro-objection-no-trust-links": _x(
        "A visible privacy/security statement reassures visitors who are asked for data.",
        _FORM_SUBMIT,
        "Short data-handling statement plus a privacy link next to the form or CTA.",
    ),
    "cro-objection-no-legitimacy": _x(
        "Visible contact details signal a real, accountable business.",
        _VISIT_CONV,
        "Contact email/phone and an About link in the header or footer.",
    ),
    # tech (only the ones with a measurable user-facing hypothesis)
    "cro-tech-render-blocking": _x(
        "A faster first render keeps more visitors on the page long enough to convert.",
        "bounce rate",
        "Render-blocking CSS/JS deferred or inlined for the above-the-fold content.",
    ),
    "cro-tech-small-fonts": _x(
        "Readable mobile type keeps mobile visitors reading to the CTA.",
        "mobile scroll depth to primary CTA",
        "Body text at >= 16px on mobile.",
    ),
    "cro-tech-small-touch-target": _x(
        "Larger touch targets reduce mis-taps and lift mobile CTA clicks.",
        "mobile primary CTA click-through rate",
        "Buttons and links at >= 44x44px with spacing between them.",
    ),
    # footer
    "cro-footer-no-final-cta": _x(
        "A closing CTA converts visitors who read to the end, the warmest audience.",
        _CTA_CTR,
        "Closing section repeating the primary CTA above the footer.",
    ),
    "cro-footer-no-contact": _x(
        "Contact links in the footer catch late-stage questions that would otherwise leave.",
        "contact/lead rate",
        "Contact email, phone and support link added to the footer.",
    ),
}


def experiment_for(check_id: str | None) -> Experiment | None:
    """The authored experiment for ``check_id``, or ``None`` when none is authored."""
    if not check_id:
        return None
    exp = CRO_EXPERIMENTS.get(check_id)
    if exp is None:
        return None
    # A copy, so a caller mutating a response row never edits the catalog.
    return _x(exp["hypothesis"], exp["primary_metric"], exp["variant"])


__all__ = ["CRO_EXPERIMENTS", "Experiment", "experiment_for"]
