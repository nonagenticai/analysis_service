"""External tool adapter registry for critique_service.

Manages registration and execution of external scanning tool adapters
(axe-core, Lighthouse, ZAP, Nuclei) with timeout and error isolation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from src.domains.critique.services.scanners.tool_adapters.protocol import ToolAdapter
from src.shared.security.url_validator import validate_callback_url

logger = logging.getLogger(__name__)

#: ``ok`` -- the adapter ran (``findings`` may be 0, which then MEANS clean).
#: ``failed`` / ``timeout`` -- it measured nothing; its 0 findings mean nothing.
#: ``skipped`` -- the registry refused the target (SSRF guard), nothing ran.
AdapterStatusValue = Literal["ok", "failed", "timeout", "skipped"]


@dataclass(frozen=True)
class AdapterStatus:
    """Outcome of one adapter on one target -- surfaced in the job's progress."""

    status: AdapterStatusValue
    category: str
    findings: int = 0
    error: str | None = None
    elapsed_seconds: float | None = None

    @property
    def measured(self) -> bool:
        """True only when the adapter actually produced a measurement."""
        return self.status == "ok"

    def as_dict(self) -> dict[str, object]:
        """JSON-safe form for ``job.progress["external_tools"]``."""
        out: dict[str, object] = {
            "status": self.status,
            "category": self.category,
            "findings": self.findings,
        }
        if self.error is not None:
            out["error"] = self.error
        if self.elapsed_seconds is not None:
            out["elapsed_seconds"] = self.elapsed_seconds
        return out


@dataclass
class AdapterRunResult:
    """Findings plus a per-adapter status, keyed by ``tool_name``."""

    findings: list[dict] = field(default_factory=list)
    statuses: dict[str, AdapterStatus] = field(default_factory=dict)

    @property
    def unmeasured(self) -> list[str]:
        """Adapters that ran (or were meant to) but measured nothing."""
        return sorted(name for name, st in self.statuses.items() if not st.measured)


class ToolAdapterRegistry:
    """Registry for external tool adapters with timeout and error isolation.

    Attributes:
        _adapters: List of registered ToolAdapter implementations.
    """

    def __init__(self) -> None:
        self._adapters: list[ToolAdapter] = []

    def register(self, adapter: ToolAdapter) -> None:
        """Register an external tool adapter.

        Args:
            adapter: ToolAdapter implementation to register.
        """
        self._adapters.append(adapter)
        logger.info("Registered external tool adapter: %s", adapter.tool_name)

    async def get_available_adapters(self) -> list[ToolAdapter]:
        """Return adapters that report themselves as available."""
        available: list[ToolAdapter] = []
        for adapter in self._adapters:
            try:
                if await adapter.is_available():
                    available.append(adapter)
            except (ImportError, OSError, RuntimeError) as e:
                logger.debug(
                    "Adapter %s availability check failed: %s",
                    adapter.tool_name,
                    e,
                )
        return available

    async def run_adapters(
        self,
        url: str,
        html: str | None = None,
        *,
        timeout: int = 120,  # noqa: ASYNC109 — why: adapter-suite deadline forwarded to per-adapter asyncio.wait_for
    ) -> list[dict]:
        """Findings-only view of :meth:`run_adapters_with_status`.

        Loses the failed-vs-clean distinction -- callers that score or report
        must use :meth:`run_adapters_with_status`.
        """
        result = await self.run_adapters_with_status(url, html, timeout=timeout)
        return result.findings

    async def run_adapters_with_status(
        self,
        url: str,
        html: str | None = None,
        *,
        timeout: int = 120,  # noqa: ASYNC109 — why: adapter-suite deadline forwarded to per-adapter asyncio.wait_for
    ) -> AdapterRunResult:
        """Run all available adapters, collecting findings. Failures are isolated.

        SSRF chokepoint. Every adapter drives a live agent at *url* with no
        further validation of its own — the axe adapter hands it to a headless
        Chromium ``page.goto`` and the ZAP adapter hands it to the spider as a
        scan target — so a single guard here covers axe / zap / lighthouse /
        nuclei at once. This is defence-in-depth behind the route-level check:
        jobs persisted before that check existed, and any future caller that
        reaches the registry directly, are still gated.

        A rejected target is treated exactly like an adapter that fails: log a
        warning, return no findings, and mark every registered adapter
        ``skipped`` (never ``ok``). Crashing would take down a
        whole critique run over one bad target, which is the opposite of the
        per-adapter isolation contract the rest of this method implements.

        Args:
            url: Live scan target, tenant-supplied and therefore untrusted.
            html: Pre-fetched page HTML passed through to each adapter.
            timeout: Per-adapter deadline in seconds.

        Returns:
            Findings from every adapter that succeeded plus one
            :class:`AdapterStatus` per adapter that was due to run. An adapter
            that is not available (disabled / not installed) gets no status:
            it was never part of this run's coverage.
        """
        try:
            validate_callback_url(url)
        except ValueError as exc:
            logger.warning("Refusing to run external tool adapters against unsafe URL: %s", exc)
            return AdapterRunResult(
                statuses={
                    a.tool_name: AdapterStatus(
                        status="skipped", category=a.category, error="unsafe target URL"
                    )
                    for a in self._adapters
                }
            )

        available = await self.get_available_adapters()
        if not available:
            return AdapterRunResult()

        async def _run_one(adapter: ToolAdapter) -> tuple[list[dict], AdapterStatus]:
            t0 = time.monotonic()
            try:
                logger.info("Starting adapter: %s url=%s", adapter.tool_name, url)
                findings = await asyncio.wait_for(adapter.scan(url, html), timeout=timeout)
                elapsed = round(time.monotonic() - t0, 2)
                logger.info(
                    "Adapter completed: %s findings=%d elapsed=%.2fs",
                    adapter.tool_name,
                    len(findings),
                    elapsed,
                )
                return findings, AdapterStatus(
                    status="ok",
                    category=adapter.category,
                    findings=len(findings),
                    elapsed_seconds=elapsed,
                )
            except TimeoutError:
                elapsed = round(time.monotonic() - t0, 2)
                logger.warning(
                    "Adapter %s timed out after %ds (elapsed=%.2fs)",
                    adapter.tool_name,
                    timeout,
                    elapsed,
                )
                return [], AdapterStatus(
                    status="timeout",
                    category=adapter.category,
                    error=f"timed out after {timeout}s",
                    elapsed_seconds=elapsed,
                )
            except Exception as e:  # noqa: BLE001 — per-adapter absorb, see comment below
                # Catch every non-timeout exception type: the documented contract
                # is "one bad adapter returns [] and never takes out sibling
                # adapters". The previous narrow tuple (ImportError, OSError,
                # RuntimeError) let off-tuple exceptions (KeyError from adapter
                # parsing, custom domain errors from adapter modules, etc.)
                # escape into gather() and cancel every sibling via task
                # cancellation.
                elapsed = round(time.monotonic() - t0, 2)
                logger.warning(
                    "Adapter %s failed after %.2fs (%s): %s",
                    adapter.tool_name,
                    elapsed,
                    type(e).__name__,
                    e,
                )
                return [], AdapterStatus(
                    status="failed",
                    category=adapter.category,
                    error=_short_error(e),
                    elapsed_seconds=elapsed,
                )

        # return_exceptions=True is belt-and-braces against BaseException
        # slipping past the inner Exception absorb (e.g. CancelledError).
        results = await asyncio.gather(
            *[_run_one(a) for a in available],
            return_exceptions=True,
        )
        run = AdapterRunResult()
        for adapter, result in zip(available, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Adapter %s raised unexpectedly past absorb (%s): %s",
                    adapter.tool_name,
                    type(result).__name__,
                    result,
                )
                run.statuses[adapter.tool_name] = AdapterStatus(
                    status="failed", category=adapter.category, error=_short_error(result)
                )
                continue
            adapter_findings, status = result
            run.findings.extend(adapter_findings)
            run.statuses[adapter.tool_name] = status
        return run

    @property
    def adapter_names(self) -> list[str]:
        """Return the names of all registered adapters."""
        return [a.tool_name for a in self._adapters]


_MAX_ERROR_CHARS = 300


def _short_error(exc: BaseException) -> str:
    """Caller-safe one-line error for the job's progress (type + truncated text)."""
    text = " ".join(str(exc).split())
    msg = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return msg[:_MAX_ERROR_CHARS]
