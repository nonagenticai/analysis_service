"""Partial-failure contract for ``ReviewGenerator.generate_reviews``.

Defect class: a bare ``asyncio.gather(*tasks)`` propagates the first exception
immediately and throws away every sibling result that already completed — here,
one expensive LLM call per review type. The fan-out must therefore settle with
``return_exceptions=True``, return the reviews that succeeded, drop (and log)
the ones that raised, re-raise only on a total wipe-out so a zero-review job can
never report "completed", and never swallow ``asyncio.CancelledError``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from src.domains.analyze.services.review_generator import ReviewGenerator
from tests.llm_test_doubles import FakeWMClient

if TYPE_CHECKING:
    from src.domains.analyze.schemas import LLMConfigOverride

ReviewPayload = dict[str, object]
Behaviour = ReviewPayload | BaseException
StubGenerateReview = Callable[..., Awaitable[ReviewPayload]]

ALL_TYPES: list[str] = ["design", "usability", "feature", "content_quality"]


def _ok(review_type: str) -> ReviewPayload:
    """Build a minimal successful single-review payload."""
    return {
        "review_type": review_type,
        "result": {"overall_score": 7.0},
        "llm_calls": 1,
        "artifacts": [],
        "validation_errors": [],
    }


def _stub_generate_review(behaviours: Mapping[str, Behaviour]) -> StubGenerateReview:
    """Return a replacement for the SINGULAR ``generate_review`` — no LLM calls.

    Args:
        behaviours: Per-review-type payload to return, or exception to raise.

    Returns:
        An async function suitable for patching onto ``ReviewGenerator``.
    """

    async def _generate_review(
        _self: ReviewGenerator,
        *,
        review_type: str,
        analysis_result: dict[str, object],
        user_context: dict[str, object] | None = None,
        job_id: str = "",
        custom_prompt: str | None = None,
        llm_config: LLMConfigOverride | None = None,
    ) -> ReviewPayload:
        await asyncio.sleep(0)
        behaviour = behaviours[review_type]
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    return _generate_review


@pytest.fixture
def generator() -> ReviewGenerator:
    """A ReviewGenerator whose WM client is never reached (generate_review is stubbed)."""
    return ReviewGenerator(wm_client=FakeWMClient())


@pytest.fixture
def analysis_result() -> dict[str, object]:
    """Minimal analysis payload — the stub ignores it."""
    return {"pages": [], "site_analysis": None, "narrative_extraction": None}


@pytest.mark.asyncio
async def test_one_failing_type_keeps_the_other_three(
    generator: ReviewGenerator, analysis_result: dict[str, object]
) -> None:
    """The money test: one type raising must not discard three completed reviews."""
    behaviours: dict[str, Behaviour] = {
        "design": _ok("design"),
        "usability": RuntimeError("WM exploded"),
        "feature": _ok("feature"),
        "content_quality": _ok("content_quality"),
    }
    with patch.object(ReviewGenerator, "generate_review", _stub_generate_review(behaviours)):
        reviews = await generator.generate_reviews(
            review_types=ALL_TYPES, analysis_result=analysis_result, job_id="job-1"
        )

    assert set(reviews) == {"design", "feature", "content_quality"}
    assert reviews["design"]["result"] == {"overall_score": 7.0}


@pytest.mark.asyncio
async def test_failed_type_is_dropped_not_returned_as_error_payload(
    generator: ReviewGenerator, analysis_result: dict[str, object]
) -> None:
    """A failure must be absent from the mapping, not present as a fake review."""
    behaviours: dict[str, Behaviour] = {
        "design": _ok("design"),
        "usability": ValueError("bad JSON"),
    }
    with patch.object(ReviewGenerator, "generate_review", _stub_generate_review(behaviours)):
        reviews = await generator.generate_reviews(
            review_types=["design", "usability"],
            analysis_result=analysis_result,
            job_id="job-2",
        )

    assert "usability" not in reviews
    assert list(reviews) == ["design"]
    assert all("error" not in payload for payload in reviews.values())


@pytest.mark.asyncio
async def test_total_wipeout_reraises_the_first_exception(
    generator: ReviewGenerator, analysis_result: dict[str, object]
) -> None:
    """Every type failing must raise — a zero-review job can never report completed."""
    first = RuntimeError("first failure")
    behaviours: dict[str, Behaviour] = {
        "design": first,
        "usability": ValueError("second failure"),
    }
    with (
        patch.object(ReviewGenerator, "generate_review", _stub_generate_review(behaviours)),
        pytest.raises(RuntimeError) as excinfo,
    ):
        await generator.generate_reviews(
            review_types=["design", "usability"],
            analysis_result=analysis_result,
            job_id="job-3",
        )

    assert excinfo.value is first


@pytest.mark.asyncio
async def test_cancelled_error_propagates_even_with_successes(
    generator: ReviewGenerator, analysis_result: dict[str, object]
) -> None:
    """Cancellation is not a review failure and must never be swallowed."""
    behaviours: dict[str, Behaviour] = {
        "design": _ok("design"),
        "usability": asyncio.CancelledError(),
    }
    with (
        patch.object(ReviewGenerator, "generate_review", _stub_generate_review(behaviours)),
        pytest.raises(asyncio.CancelledError),
    ):
        await generator.generate_reviews(
            review_types=["design", "usability"],
            analysis_result=analysis_result,
            job_id="job-4",
        )


@pytest.mark.asyncio
async def test_all_success_returns_every_requested_type(
    generator: ReviewGenerator, analysis_result: dict[str, object]
) -> None:
    """Positive control: nothing is dropped when nothing fails."""
    behaviours: dict[str, Behaviour] = {rt: _ok(rt) for rt in ALL_TYPES}
    with patch.object(ReviewGenerator, "generate_review", _stub_generate_review(behaviours)):
        reviews = await generator.generate_reviews(
            review_types=ALL_TYPES, analysis_result=analysis_result, job_id="job-5"
        )

    assert set(reviews) == set(ALL_TYPES)


@pytest.mark.asyncio
async def test_keys_follow_the_request_not_the_echoed_review_type(
    generator: ReviewGenerator, analysis_result: dict[str, object]
) -> None:
    """Keys are the REQUESTED types; the caller derives failures as ``rt not in result``."""
    echoed = _ok("something-else")
    behaviours: dict[str, Behaviour] = {"design": echoed, "usability": _ok("usability")}
    with patch.object(ReviewGenerator, "generate_review", _stub_generate_review(behaviours)):
        reviews = await generator.generate_reviews(
            review_types=["design", "usability"],
            analysis_result=analysis_result,
            job_id="job-6",
        )

    assert set(reviews) == {"design", "usability"}
    assert reviews["design"]["review_type"] == "something-else"
