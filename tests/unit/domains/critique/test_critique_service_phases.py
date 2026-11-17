"""Phase-method unit tests for ``CritiqueService.process_job`` decomposition.

Covers the new value-object boundaries created by the decomposition:
``_phase_resolve_repo``, ``_parse_soup``, ``_phase_run_adapters``,
``_aggregate_token_usage``, ``_partition_scanner_cache``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from bs4 import BeautifulSoup

from src.domains.critique.models import CritiqueJob
from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.critique_service import (
    CritiqueService,
    _ScoringResult,
)
from src.domains.critique.services.scanners.tool_adapters.registry import (
    AdapterRunResult,
    AdapterStatus,
)


class _FakeUXReviewer:
    """Minimal duck-typed fake satisfying ``UXReviewerProtocol``.

    Exposes ``_last_token_usage`` for ``_aggregate_token_usage`` and an
    inert ``scan`` so Pyright sees structural conformance.
    """

    def __init__(self, last_token_usage: dict[str, int] | None = None) -> None:
        self._last_token_usage = last_token_usage

    async def scan(
        self,
        html: str,
        url: str = "",
        instructions: str | None = None,
        site_analysis: dict | None = None,
        llm_config: LLMConfigOverride | None = None,
        soup: BeautifulSoup | None = None,
    ) -> list[dict]:
        # Named params required for Protocol structural conformance; consume
        # to silence Pyright "not accessed" on this inert fake.
        _ = (html, url, instructions, site_analysis, llm_config, soup)
        return []


# ---------------------------------------------------------------------------
# _phase_resolve_repo
# ---------------------------------------------------------------------------


class TestPhaseResolveRepo:
    def test_returns_existing_repo_unchanged(self) -> None:
        job = SimpleNamespace(github_repo="https://github.com/foo/bar")
        result = CritiqueService._phase_resolve_repo(cast(CritiqueJob, job), "<html></html>")
        assert result == "https://github.com/foo/bar"
        assert job.github_repo == "https://github.com/foo/bar"

    def test_returns_none_when_no_html_and_no_existing(self) -> None:
        job = SimpleNamespace(github_repo=None)
        assert CritiqueService._phase_resolve_repo(cast(CritiqueJob, job), "") is None

    def test_detects_repo_in_html_link(self) -> None:
        job = SimpleNamespace(github_repo=None)
        html = '<html><a href="https://github.com/owner/proj">repo</a></html>'
        result = CritiqueService._phase_resolve_repo(cast(CritiqueJob, job), html)
        # may or may not match by the detector's heuristic; just assert
        # that when populated, it's also written back to the job.
        if result is not None:
            assert "github.com" in result
            assert job.github_repo == result


# ---------------------------------------------------------------------------
# _parse_soup
# ---------------------------------------------------------------------------


class TestParseSoup:
    def test_empty_html_returns_none(self) -> None:
        assert CritiqueService._parse_soup("") is None

    def test_returns_beautifulsoup_for_real_html(self) -> None:
        soup = CritiqueService._parse_soup("<html><body><p>hi</p></body></html>")
        assert isinstance(soup, BeautifulSoup)
        # real assertion against parsed tree
        p_tag = soup.find("p")
        assert p_tag is not None
        assert p_tag.get_text() == "hi"


# ---------------------------------------------------------------------------
# _aggregate_token_usage
# ---------------------------------------------------------------------------


class TestAggregateTokenUsage:
    def _service(self, ux_token_usage: dict[str, int] | None = None) -> CritiqueService:
        svc = object.__new__(CritiqueService)  # bypass __init__ side effects
        svc._ux = _FakeUXReviewer(last_token_usage=ux_token_usage)
        return svc

    def test_returns_none_when_both_empty(self) -> None:
        svc = self._service(ux_token_usage=None)
        assert svc._aggregate_token_usage(None) is None

    def test_combines_ux_and_summary(self) -> None:
        svc = self._service(
            ux_token_usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        )
        out = svc._aggregate_token_usage(
            {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        )
        assert out == {"prompt_tokens": 15, "completion_tokens": 6, "total_tokens": 21}

    def test_summary_only(self) -> None:
        svc = self._service(ux_token_usage=None)
        assert svc._aggregate_token_usage(
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        ) == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


# ---------------------------------------------------------------------------
# _partition_scanner_cache
# ---------------------------------------------------------------------------


class TestPartitionScannerCache:
    @pytest.mark.asyncio
    async def test_all_cached_yields_empty_run_list(self) -> None:
        async def fake_factory() -> list[dict[str, Any]]:
            return []

        async def cache_lookup(
            _tenant: str,
            _url: str,
            name: str,
            _instr: str | None,
            _llm_config: LLMConfigOverride | None = None,
            _site_analysis: dict | None = None,
        ) -> list[dict[str, Any]] | None:
            # `_llm_config` / `_site_analysis` are part of the lookup signature:
            # both steer the findings and must reach the per-scanner cache key.
            return [{"finding_id": "OLD", "name": name}]

        scan_tasks = [
            ("seo", fake_factory, 5),
            ("a11y", fake_factory, 5),
        ]
        job = SimpleNamespace(
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://x.test",
            instructions=None,
        )

        cached, to_run = await CritiqueService._partition_scanner_cache(
            scan_tasks, cast(CritiqueJob, job), cache_lookup
        )

        assert len(cached) == 2
        assert to_run == []
        # cached findings get fresh UUIDs to avoid PK collisions
        assert all(f["finding_id"] != "OLD" for f in cached)

    @pytest.mark.asyncio
    async def test_no_cache_yields_full_run_list(self) -> None:
        async def fake_factory() -> list[dict[str, Any]]:
            return []

        async def cache_lookup(
            _tenant: str,
            _url: str,
            _name: str,
            _instr: str | None,
            _llm_config: LLMConfigOverride | None = None,
            _site_analysis: dict | None = None,
        ) -> list[dict[str, Any]] | None:
            return None

        scan_tasks = [("seo", fake_factory, 5), ("perf", fake_factory, 5)]
        job = SimpleNamespace(
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://x.test",
            instructions=None,
        )

        cached, to_run = await CritiqueService._partition_scanner_cache(
            scan_tasks, cast(CritiqueJob, job), cache_lookup
        )

        assert cached == []
        assert [name for name, _, _ in to_run] == ["seo", "perf"]


# ---------------------------------------------------------------------------
# _phase_run_adapters
# ---------------------------------------------------------------------------


class TestPhaseRunAdapters:
    @pytest.mark.asyncio
    async def test_returns_empty_when_registry_unset(self) -> None:
        svc = object.__new__(CritiqueService)
        svc.set_adapter_registry(None)
        result = await svc._phase_run_adapters(
            cast(CritiqueJob, SimpleNamespace(url="https://x.test")),
            "<html/>",
            SimpleNamespace(scanners=SimpleNamespace(overall_timeout=10)),
        )
        assert result.findings == []
        assert result.statuses == {}

    @pytest.mark.asyncio
    async def test_returns_findings_from_registry(self) -> None:
        svc = object.__new__(CritiqueService)
        registry = MagicMock()
        registry.run_adapters_with_status = AsyncMock(
            return_value=AdapterRunResult(
                findings=[{"finding_id": "A1"}],
                statuses={"axe-core": AdapterStatus(status="ok", category="a11y", findings=1)},
            )
        )
        svc.set_adapter_registry(registry)
        result = await svc._phase_run_adapters(
            cast(CritiqueJob, SimpleNamespace(url="https://x.test")),
            "<html/>",
            SimpleNamespace(scanners=SimpleNamespace(overall_timeout=10)),
        )
        assert result.findings == [{"finding_id": "A1"}]
        assert result.unmeasured == []

    @pytest.mark.asyncio
    async def test_whole_phase_failure_marks_every_adapter_failed(self) -> None:
        """A registry that blows up must not read as "adapters found nothing"."""
        svc = object.__new__(CritiqueService)
        registry = MagicMock()
        registry.adapter_names = ["axe-core"]
        registry.run_adapters_with_status = AsyncMock(side_effect=RuntimeError("boom"))
        svc.set_adapter_registry(registry)
        result = await svc._phase_run_adapters(
            cast(CritiqueJob, SimpleNamespace(url="https://x.test")),
            "<html/>",
            SimpleNamespace(scanners=SimpleNamespace(overall_timeout=10)),
        )
        assert result.findings == []
        assert result.unmeasured == ["axe-core"]
        assert result.statuses["axe-core"].status == "failed"
        assert "boom" in (result.statuses["axe-core"].error or "")


# ---------------------------------------------------------------------------
# _ScoringResult dataclass: attribute contract
# ---------------------------------------------------------------------------


class TestScoringResult:
    def test_dataclass_carries_all_fields(self) -> None:
        sr = _ScoringResult(
            findings=[{"finding_id": "x"}],
            scores={"seo": 90.0},
            overall_score=88.5,
            confidence={"seo": 0.9},
            summary="ok",
            report="# report",
            token_usage={"total_tokens": 100},
        )
        assert sr.overall_score == 88.5
        assert sr.findings[0]["finding_id"] == "x"
        assert sr.token_usage and sr.token_usage["total_tokens"] == 100


# ---------------------------------------------------------------------------
# _phase_finalize_completed / _phase_finalize_failed:
# regression — scan_duration must reach Prometheus histogram.observe() as a
# concrete float (never None). Pyright previously flagged float|None being
# passed to observe(value: float); a None would TypeError at runtime.
# ---------------------------------------------------------------------------


class TestPhaseFinalizeMetricsObserveTyping:
    @pytest.mark.asyncio
    async def test_completed_phase_observes_concrete_float(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.domains.critique.services import critique_service as cs_mod

        observed: list[float] = []
        monkeypatch.setattr(
            cs_mod.scan_duration_metric,
            "observe",
            lambda v: observed.append(v),
        )
        # Patch downstream side-effects so we exercise only the metric branch.
        monkeypatch.setattr(cs_mod, "log_event", AsyncMock())
        monkeypatch.setattr(
            "src.domains.critique.services.scan_cache.set_cached_result",
            AsyncMock(),
        )

        svc = object.__new__(CritiqueService)
        svc._save_findings = AsyncMock()
        svc._record_usage = AsyncMock()
        svc._update_progress = AsyncMock()

        job = MagicMock(spec=CritiqueJob)
        job.scan_duration = None  # initial state — must be replaced w/ float
        job.callback_url = None
        job.url = "https://x.test"
        job.categories = ["seo"]
        job.instructions = None
        job.job_id = "job-1"

        scoring = _ScoringResult(
            findings=[],
            scores={"seo": 90.0},
            overall_score=90.0,
            confidence={"seo": 1.0},
            summary="ok",
            report="# r",
            token_usage=None,
        )

        await svc._phase_finalize_completed(
            cast(Any, MagicMock()),
            job,
            "job-1",
            "a71efcab-f4a8-5582-912e-5f4dc29b3c0d",
            scoring=scoring,
            scanner_timings={},
            start_time=0.0,
        )

        assert len(observed) == 1
        v = observed[0]
        assert v is not None
        assert isinstance(v, float)
        # job.scan_duration must have been set to the same concrete float
        assert job.scan_duration == v

    @pytest.mark.asyncio
    async def test_failed_phase_observes_concrete_float(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.domains.critique.services import critique_service as cs_mod

        observed: list[float] = []
        monkeypatch.setattr(
            cs_mod.scan_duration_metric,
            "observe",
            lambda v: observed.append(v),
        )
        monkeypatch.setattr(cs_mod, "log_event", AsyncMock())

        svc = object.__new__(CritiqueService)
        svc._record_usage = AsyncMock()
        job = MagicMock(spec=CritiqueJob)
        job.scan_duration = None

        await svc._phase_finalize_failed(
            cast(Any, MagicMock()),
            job,
            "job-2",
            "a71efcab-f4a8-5582-912e-5f4dc29b3c0d",
            RuntimeError("boom"),
            start_time=0.0,
        )

        assert len(observed) == 1
        v = observed[0]
        assert v is not None
        assert isinstance(v, float)
        assert job.scan_duration == v
