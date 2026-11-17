"""Base stage ABC with timing and error handling."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from functools import cache

import httpx

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.shared.upstream_errors import PASSTHROUGH_STATUSES

logger = logging.getLogger(__name__)


@cache
def _stage_failure_types() -> tuple[type[Exception], ...]:
    """The exceptions a single stage may fail with without failing the run.

    Why this is computed rather than written inline: the pipeline's whole
    contract is *degrade one stage, keep the brand*. The handler used to be a
    literal ``(ValueError, TypeError, KeyError, RuntimeError, OSError)``, which
    reads as broad but misses the two libraries the stages actually talk to.
    ``httpx.HTTPError`` and Playwright's ``Error`` both derive straight from
    ``Exception``, **not** from ``OSError`` — so a connect error on a stylesheet
    fetch, or a navigation timeout in Chromium, escaped ``execute``, escaped
    ``PipelineRunner``, and 502'd an extraction that should have come back
    missing one field.

    Why it is a lazy, cached function rather than a module-level constant: the
    Playwright class can only be named by importing ``playwright.async_api``,
    which is a multi-second, browser-bindings-heavy import. ``base.py`` is
    imported by every stage (including the ones that never touch a browser) and
    by the pipeline unit tests, so making it a load-time dependency would tax
    all of them. Resolving on first ``execute`` and caching pays it once, only
    in a process that is actually running stages. Playwright is a required
    dependency (``pyproject.toml``), so the ``ImportError`` arm is a guard
    against a broken install, not a supported configuration — but it degrades to
    the non-Playwright tuple rather than making every stage unimportable.

    Deliberately *outside* the net, even though ``httpx.HTTPError`` is listed:
    an upstream status this service re-emits verbatim — the set is
    :data:`src.shared.upstream_errors.PASSTHROUGH_STATUSES`, today just ``402``.
    ``llm_analysis`` calls ``worker_management``, which answers ``402
    byo_llm_not_configured`` when the TENANT has no LLM provider configured.
    Degrading that is the worst possible outcome: the caller gets ``200`` with a
    quietly brand-analysis-free brand and never learns there is a Settings page
    to click. It is not a partial extraction, it is a caller-fixable
    misconfiguration, so it escapes ``execute`` and the route re-emits it (see
    ``BaseStage.execute`` and ``src/domains/brand/routes.py``).

    Every OTHER upstream status keeps degrading exactly as before, for the same
    reasons the ``upstream_errors`` module docstring gives: ``401``/``403`` is
    OUR S2S credential, ``404`` is OUR worker name, ``400``/``422`` is a body WE
    built, ``429`` is ambiguous, and ``5xx``/timeouts/connect errors are a real
    upstream fault — none of them are anything the caller can act on, so losing
    one stage beats losing the extraction.

    Deliberately still *inside* the net: ``BrandSsrfBlockedError`` and
    ``BrandResponseTooLargeError`` subclass ``ValueError`` (see
    ``src/domains/brand/security.py``) because second-order URLs harvested from
    a fetched page — stylesheet ``href``, logo ``src`` — must degrade the stage
    that found them rather than fail the extraction. Do not lift them out.

    Returns:
        The exception classes ``BaseStage.execute`` converts into a stage error.
    """
    failures: list[type[Exception]] = [
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        # TimeoutError is the builtin that ``asyncio.TimeoutError`` aliases;
        # it is an OSError subclass, listed here for the reader, not for reach.
        TimeoutError,
        OSError,
        httpx.HTTPError,
    ]
    try:
        from playwright.async_api import Error as PlaywrightError
    except ImportError:  # pragma: no cover — browser extras absent
        logger.debug("Playwright not importable; stage errors will not cover browser failures")
    else:
        failures.append(PlaywrightError)
    return tuple(failures)


class BaseStage(ABC):
    """Abstract base for all pipeline stages."""

    name: str = ""

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.name}")

    async def execute(self, ctx: PipelineContext, brand: BrandResponse) -> StageResult:
        """Run the stage with timing and error handling.

        Failures in :func:`_stage_failure_types` become a recorded stage error
        and the run continues with whatever the other stages found; anything
        else is a programming error and is allowed to propagate.

        The one carve-out is an upstream status in
        :data:`~src.shared.upstream_errors.PASSTHROUGH_STATUSES` (today ``402``
        only), which is re-raised so the route can answer the caller with the
        upstream's actionable ``{code, message}`` instead of a degraded ``200``.
        See :func:`_stage_failure_types` for why that one status and no other.
        """
        result = StageResult(stage_name=self.name)
        start = time.monotonic()
        try:
            await self.run(ctx, brand, result)
        except _stage_failure_types() as e:
            # Reuses the single source of truth in ``src.shared.upstream_errors``
            # rather than restating "is this a 402" here — the route's mapper
            # (``upstream_http_exception``) keys off the same frozenset, so
            # widening the set can never leave the escape and the re-emit
            # disagreeing about which statuses reach the caller.
            #
            # ``getattr`` rather than ``e.response``: httpx always attaches one,
            # but this runs INSIDE an except block — an ``AttributeError`` here
            # would replace the original stage failure with a meaningless one and
            # lose the real cause. A ``Mock(spec=...)`` double is enough to
            # trigger it. Matches ``upstream_errors.upstream_http_exception``.
            response = getattr(e, "response", None)
            if (
                isinstance(e, httpx.HTTPStatusError)
                and response is not None
                and response.status_code in PASSTHROUGH_STATUSES
            ):
                raise
            result.add_error(str(e), exc=e)
            self.logger.warning("Stage %s failed: %s", self.name, e)
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    @abstractmethod
    async def run(
        self, ctx: PipelineContext, brand: BrandResponse, result: StageResult
    ) -> None: ...

    def should_run(self, ctx: PipelineContext) -> bool:
        """Check if this stage should execute given the config."""
        return ctx.config.should_run(self.name)
