"""Regression tests for cache-key steering plumbing and scanner outcome handling.

Two defects, both of which ship SILENTLY:

* A correct ``_compute_cache_key`` is INERT unless every caller forwards the
  steering inputs. These tests assert the read path (``_phase_check_cache``,
  ``_partition_scanner_cache``) and the write path
  (``set_scanner_cached_result``) actually pass ``llm_config`` /
  ``site_analysis`` through, so the money bug cannot come back via a caller.
* A scanner outcome that is neither a valid tuple nor an ``Exception`` matched
  NEITHER branch of the result loop: the scanner still counted as COMPLETED,
  contributed ZERO findings, and the job finalized and SCORED over a silently
  short finding set — reported clean.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from src.domains.critique.models import CritiqueJob
from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.critique_service import CritiqueService

_CFG = LLMConfigOverride(model="premium-model")
_SITE = {"stack": "django"}


def _job() -> CritiqueJob:
    return cast(
        CritiqueJob,
        SimpleNamespace(
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            url="https://example.com",
            categories=["seo"],
            instructions=None,
            job_id="job-1",
            domain="example.com",
        ),
    )


# ---------------------------------------------------------------------------
# Defect 1 — steering must reach the cache key on BOTH read and write paths
# ---------------------------------------------------------------------------


class TestSteeringReachesTheCacheKey:
    @pytest.mark.asyncio
    async def test_phase_check_cache_forwards_steering(self, monkeypatch: Any) -> None:
        """Without this forwarding the key fix ships INERT: the read still looks
        up the steering-agnostic key and serves the cheap model's result."""
        captured: dict[str, Any] = {}

        async def _fake_get_cached_result(*args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            return

        monkeypatch.setattr(
            "src.domains.critique.services.scan_cache.get_cached_result",
            _fake_get_cached_result,
        )
        service = CritiqueService.__new__(CritiqueService)
        took_cached_path = await CritiqueService._phase_check_cache(
            service,
            cast(Any, None),
            _job(),
            "job-1",
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            llm_config=_CFG,
            site_analysis=_SITE,
        )

        assert took_cached_path is False
        assert _CFG in captured["args"], "llm_config never reached get_cached_result"
        assert _SITE in captured["args"], "site_analysis never reached get_cached_result"

    @pytest.mark.asyncio
    async def test_partition_scanner_cache_forwards_steering(self) -> None:
        captured: dict[str, Any] = {}

        async def _fake_get(*args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            return

        async def _factory() -> list[dict[str, Any]]:
            return []

        cached, to_run = await CritiqueService._partition_scanner_cache(
            [("ux", _factory, 10)],
            _job(),
            _fake_get,
            llm_config=_CFG,
            site_analysis=_SITE,
        )

        assert cached == []
        assert len(to_run) == 1
        assert _CFG in captured["args"], "llm_config never reached the per-scanner cache read"
        assert _SITE in captured["args"], "site_analysis never reached the per-scanner cache read"

    @pytest.mark.asyncio
    async def test_scanner_cache_write_carries_steering(self, monkeypatch: Any) -> None:
        """The WRITE must be keyed identically to the read, or the premium run
        stores its findings under a steering-agnostic key and hands them to the
        next cheap-model request."""
        set_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "src.domains.critique.services.scan_cache.set_scanner_cached_result", set_mock
        )
        monkeypatch.setattr(
            "src.domains.critique.services.scan_cache.get_scanner_cached_result",
            AsyncMock(return_value=None),
        )

        service = CritiqueService.__new__(CritiqueService)
        service._update_progress = AsyncMock()  # type: ignore[method-assign]

        async def _factory() -> list[dict[str, Any]]:
            return [{"id": "f1"}]

        monkeypatch.setattr(
            CritiqueService, "_build_scan_tasks", lambda self, **kw: [("ux", _factory, 10)]
        )
        monkeypatch.setattr(
            CritiqueService,
            "_run_scanner_with_timeout",
            AsyncMock(return_value=([{"id": "f1"}], 1.0)),
        )

        await CritiqueService._phase_run_scanners(
            service,
            job_id="job-1",
            job=_job(),
            html="<html></html>",
            soup=None,
            categories={"ux"},
            github_repo=None,
            settings=SimpleNamespace(scanners=SimpleNamespace(ux_timeout=10)),
            site_analysis=_SITE,
            llm_config=_CFG,
        )

        set_mock.assert_awaited_once()
        await_args = set_mock.await_args
        # Guard, not decoration: if the write were never awaited, `await_args`
        # would be None and the two assertions below would raise AttributeError
        # instead of FAILING on their own terms — i.e. the test would still be
        # red, but for the wrong reason.
        assert await_args is not None, "the per-scanner cache write never happened"
        assert _CFG in await_args.args, "llm_config never reached the cache WRITE"
        assert _SITE in await_args.args, "site_analysis never reached the cache WRITE"


class TestResultCacheWriteIsKeyedOnSteering:
    """End-to-end proof for the RESULT cache (not just the per-scanner one).

    Threading a parameter through ``_phase_finalize_completed`` proves nothing
    on its own — what matters is that the Redis key the entry lands under
    actually differs, so a later default-model job cannot read the premium
    entry. This asserts the real key, computed by the real function.
    """

    @pytest.mark.asyncio
    async def test_write_key_differs_by_steering_and_matches_the_read_key(
        self, monkeypatch: Any
    ) -> None:
        from src.domains.critique.services import scan_cache

        written: list[str] = []

        class _FakeRedis:
            async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
                written.append(key)
                return True

            async def get(self, key: str) -> None:
                written.append(key)
                return

        monkeypatch.setattr(scan_cache, "get_redis_client", lambda: _FakeRedis())
        monkeypatch.setattr(
            scan_cache,
            "get_settings",
            lambda: SimpleNamespace(cache=SimpleNamespace(enabled=True, ttl=60)),
        )

        payload = {"findings": [], "overall_score": 99.0}
        await scan_cache.set_cached_result("t", "https://x/", ["seo"], payload)
        await scan_cache.set_cached_result("t", "https://x/", ["seo"], payload, None, _CFG)
        await scan_cache.get_cached_result("t", "https://x/", ["seo"], None, _CFG)

        default_key, premium_key, premium_read_key = written
        assert default_key != premium_key, (
            "the premium run wrote to the DEFAULT model's key — a later "
            "cheap-model request would be served premium findings"
        )
        assert premium_read_key == premium_key, (
            "read and write disagree on the key: the premium run would never "
            "hit its own entry, and would fall through to the default one"
        )


# ---------------------------------------------------------------------------
# Defect 3 — unknown / BaseException scanner outcomes
# ---------------------------------------------------------------------------


class TestScannerOutcomeHandling:
    @staticmethod
    def _service(monkeypatch: Any, outcome: Any) -> tuple[CritiqueService, AsyncMock]:
        monkeypatch.setattr(
            "src.domains.critique.services.scan_cache.get_scanner_cached_result",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "src.domains.critique.services.scan_cache.set_scanner_cached_result",
            AsyncMock(return_value=True),
        )
        progress = AsyncMock()
        service = CritiqueService.__new__(CritiqueService)
        service._update_progress = progress  # type: ignore[method-assign]

        async def _factory() -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(
            CritiqueService, "_build_scan_tasks", lambda self, **kw: [("ux", _factory, 10)]
        )

        async def _run(*args: Any, **kwargs: Any) -> Any:
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(CritiqueService, "_run_scanner_with_timeout", _run)
        return service, progress

    async def _run_phase(self, service: CritiqueService) -> Any:
        return await CritiqueService._phase_run_scanners(
            service,
            job_id="job-1",
            job=_job(),
            html="<html></html>",
            soup=None,
            categories={"ux"},
            github_repo=None,
            settings=SimpleNamespace(scanners=SimpleNamespace(ux_timeout=10)),
            site_analysis=None,
            llm_config=None,
        )

    @pytest.mark.asyncio
    async def test_unusable_outcome_is_not_counted_as_completed(self, monkeypatch: Any) -> None:
        """An outcome that is neither a tuple nor an exception must NOT advance
        ``completed``: it contributed zero findings, so counting it lets the job
        score over a short finding set and report the site clean."""
        service, progress = self._service(monkeypatch, outcome=None)

        findings, timings, _incomplete = await self._run_phase(service)

        assert findings == []
        assert timings == {}
        completed_values = [
            c.kwargs["completed"] for c in progress.await_args_list if "completed" in c.kwargs
        ]
        assert completed_values, "no progress update was recorded"
        assert max(completed_values) == 0, (
            f"an unusable scanner outcome was counted as completed: {completed_values}"
        )

    @pytest.mark.asyncio
    async def test_crashed_scanner_is_not_counted_as_completed(self, monkeypatch: Any) -> None:
        """A scanner that RAISED must not advance ``completed`` either.

        It is the exact same measurement gap as the unusable-outcome case above
        -- zero findings contributed -- only strictly worse. Counting it reports
        a full ``completed == total``, which is indistinguishable from a clean,
        complete scan to every progress/SSE consumer, so the short finding set
        is papered over. The job still reaches ``status="completed"`` in
        ``finalize``, so nothing hangs on the shortfall.
        """
        service, progress = self._service(monkeypatch, outcome=RuntimeError("scanner blew up"))

        findings, _timings, _incomplete = await self._run_phase(service)

        assert findings == []
        completed_values = [
            c.kwargs["completed"] for c in progress.await_args_list if "completed" in c.kwargs
        ]
        assert completed_values, "no progress update was recorded"
        assert max(completed_values) == 0, (
            f"a crashed scanner was counted as completed: {completed_values}"
        )

    @pytest.mark.asyncio
    async def test_cancellation_propagates_instead_of_scoring_a_short_scan(
        self, monkeypatch: Any
    ) -> None:
        """``CancelledError`` is a ``BaseException``. It must unwind, never be
        absorbed into a job that then finalizes over the surviving findings."""
        service, _ = self._service(monkeypatch, outcome=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await self._run_phase(service)

    @pytest.mark.asyncio
    async def test_real_scanner_failure_logs_a_traceback(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``gather(return_exceptions=True)`` hands the exception back as a
        VALUE, so ``exc_info=result`` is the only way to get a traceback; without
        it a genuine failure left no trace at all."""
        service, _ = self._service(monkeypatch, outcome=RuntimeError("scanner exploded"))

        with caplog.at_level("ERROR"):
            findings, _, _ = await self._run_phase(service)

        assert findings == []
        records = [r for r in caplog.records if "failed" in r.getMessage()]
        assert records, "the scanner failure was not logged"
        assert records[0].exc_info is not None, "scanner failure logged with NO traceback"
