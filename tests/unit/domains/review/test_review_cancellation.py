"""Regression: a cancelled review run must unwind, not fabricate per-review errors.

``_run_independent_reviews`` gathers with ``return_exceptions=True`` and matched
on ``isinstance(result, BaseException)``. ``asyncio.CancelledError`` IS a
``BaseException``, so a cancellation was swallowed and written back as
``results[rt] = {"error": "..."}`` for every outstanding review: the run finished
carrying FABRICATED per-review errors that never happened instead of unwinding.

The sibling ``analyze/services/review_generator.py`` gets this right — it
re-raises ``CancelledError``.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from src.domains.review.schemas import AnalysisInput
from src.domains.review.services.review_service import ReviewService


def _service() -> ReviewService:
    return ReviewService.__new__(ReviewService)


async def _run(service: ReviewService, results: dict[str, dict], boom: BaseException) -> None:
    async def _generate_review(rt: str, *args: Any, **kwargs: Any) -> dict:
        raise boom

    # `setattr` on the instance rather than a direct assignment: the stub is
    # deliberately narrower than the real bound method, and the point of the
    # test is the loop's handling of the OUTCOME, not the call signature.
    setattr(service, "_generate_review", _generate_review)  # noqa: B010
    await ReviewService._run_independent_reviews(
        service,
        ["ux", "seo"],
        results,
        # The reviews never get far enough to read `analysis`; every call raises.
        analysis=cast(AnalysisInput, None),
        message="",
        user_context=None,
        on_progress=None,
    )


class TestReviewCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_propagates_and_fabricates_no_errors(self) -> None:
        results: dict[str, dict] = {}
        service = _service()

        with pytest.raises(asyncio.CancelledError):
            await _run(service, results, asyncio.CancelledError())

        assert results == {}, f"a cancellation was recorded as a review error: {results}"

    @pytest.mark.asyncio
    async def test_real_failure_is_still_recorded_as_an_error(self) -> None:
        """The cancellation carve-out must not stop genuine failures being captured."""
        results: dict[str, dict] = {}
        await _run(_service(), results, RuntimeError("llm exploded"))

        assert set(results) == {"ux", "seo"}
        assert "llm exploded" in results["ux"]["error"]

    @pytest.mark.asyncio
    async def test_real_failure_logs_a_traceback(self, monkeypatch: Any) -> None:
        """``gather(return_exceptions=True)`` returns the exception as a VALUE,
        so we are not inside an ``except`` block: ``exc_info=result`` is the only
        form that yields a traceback (a bare ``logger.exception()`` here would be
        BLIND). Without it a real failure left no traceback at all.

        Spied on the logger rather than ``caplog`` because this module logs
        through structlog, whose handler ``caplog`` does not observe.
        """
        calls: list[dict[str, Any]] = []
        boom = RuntimeError("llm exploded")

        def _spy(*args: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr("src.domains.review.services.review_service.logger.error", _spy)
        await _run(_service(), {}, boom)

        assert calls, "the review failure was not logged"
        assert calls[0].get("exc_info") is boom, (
            f"review failure logged with NO traceback: {calls[0]}"
        )
