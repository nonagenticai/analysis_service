"""The ICP prompt must ASK for the optional keys without REQUIRING them.

`required` is a decoding constraint, so REQUIRED_ICP_KEYS deliberately holds only the three
facets a landing page always states outright — naming more there would force the grammar to
emit keys the page cannot ground, turning honest omission into fabrication.

But a key that is neither required nor mentioned is never emitted in practice. Measured
2026-08-31: clay.com returned a clean 200 with `product`, `offering`, six `value_props` and
four pricing tiers, and `category`/`positioning`/`pains`/`problems` all absent — leaving the
downstream dossier's market and problem facets at confidence 0.0. The prompt had sent only
Domain / Page title / Page content.

These tests pin both halves: the optional keys are named in the prompt, AND they stay out of
the grammar's required set.
"""

from __future__ import annotations

from src.domains.icp.service import (
    ICP_OUTPUT_SCHEMA,
    REQUIRED_ICP_KEYS,
    build_prompt,
)

_ASKED_FOR = ("category", "positioning", "pains", "problems")


def test_prompt_names_the_optional_keys_the_dossier_needs() -> None:
    prompt = build_prompt("Some page copy.", title="Merqi", domain="merqi.nl")
    for key in _ASKED_FOR:
        assert key in prompt, f"prompt must name {key} or the model never emits it"


def test_prompt_makes_omission_the_correct_answer_not_a_failure() -> None:
    # The anti-fabrication fence lives in the wording: asking must not read as compelling.
    prompt = build_prompt("Some page copy.")
    # Collapse wrapping: the guidance is a wrapped block, so a literal substring would be
    # asserting on line breaks rather than on wording.
    lowered = " ".join(prompt.lower().split())
    assert "only when the page states them" in lowered
    assert "omitting a field is the correct answer" in lowered
    assert "never infer" in lowered


def test_asking_does_not_promote_a_key_into_the_grammar() -> None:
    # The regression that would undo the fence: someone "fixes" a missing key by adding it
    # to required. Then the model emits it whether or not the page grounds it.
    for key in _ASKED_FOR:
        assert key not in REQUIRED_ICP_KEYS, f"{key} must stay optional — asking, not forcing"
    assert set(ICP_OUTPUT_SCHEMA["required"]) == set(REQUIRED_ICP_KEYS)
    assert set(REQUIRED_ICP_KEYS) == {"company", "product", "value_props"}


def test_the_page_content_still_reaches_the_model_after_the_guidance() -> None:
    # Guidance is prepended; the content must still be present and not truncated away.
    prompt = build_prompt("UNIQUE_PAGE_MARKER", title="t", domain="d")
    assert "UNIQUE_PAGE_MARKER" in prompt
    assert prompt.index("category") < prompt.index("UNIQUE_PAGE_MARKER")
