"""``ReviewService``'s WM cap must not sit silently below its own client budget.

``ReviewService`` wraps ``WMClient.message()`` in ``asyncio.wait_for``. It was
the only one of this service's ~16 WM call sites to do so, and it capped at a
hardcoded 180s while the client it wraps is configured for 600s
(``WorkerManagementSettings.timeout_seconds``, raised deliberately with the
note "Matches the provider's 600s budget"). The inner client timeout could
therefore never fire: every slow review died at 180s as a ``TimeoutError``
attributed to nothing in particular.

Three things mark that 180 as an accidental leftover rather than a stage budget:

* ``ReviewSettings.timeout_seconds`` -- the ``REVIEW_`` operator knob, also 180
  -- was read by **nothing** in ``src/``. An operator setting
  ``REVIEW_TIMEOUT_SECONDS`` got silence and an unchanged 180s cap.
* ``asgi.py`` constructs ``ReviewService()`` with no arguments, so the hardcoded
  constructor default was the only value the service ever ran with.
* The 180 dates to the initial commit; the WM budget was raised to 600 later,
  and this call site was never revisited.

An identical 180s outlier in the orchestrator made a critic score zero items
while reporting success, so the invariant is pinned here rather than left as a
constant somebody has to notice.
"""

from __future__ import annotations

from unittest.mock import patch

from src.config import get_settings
from src.domains.review.services.review_service import ReviewService


class TestReviewTimeoutBudget:
    def test_default_cap_is_not_below_the_wm_client_timeout(self) -> None:
        """A cap under the client's own budget makes the client timeout dead code."""
        settings = get_settings()
        service = ReviewService()

        assert service.timeout_seconds >= settings.wm.timeout_seconds, (
            f"review caps WM at {service.timeout_seconds}s while its client is "
            f"configured for {settings.wm.timeout_seconds}s"
        )

    def test_operator_knob_is_actually_wired(self) -> None:
        """``REVIEW_TIMEOUT_SECONDS`` must reach the effective cap."""
        with patch("src.domains.review.services.review_service.get_settings") as mock_settings:
            mock_settings.return_value.review.timeout_seconds = 42
            service = ReviewService()

        assert service.timeout_seconds == 42, (
            "the REVIEW_ timeout setting is dead — ReviewService ignores it"
        )

    def test_explicit_argument_still_wins(self) -> None:
        """Callers (and tests) may still pin a cap explicitly."""
        service = ReviewService(timeout_seconds=7)
        assert service.timeout_seconds == 7

    def test_configured_default_matches_the_wm_budget(self) -> None:
        settings = get_settings()
        assert settings.review.timeout_seconds >= settings.wm.timeout_seconds
