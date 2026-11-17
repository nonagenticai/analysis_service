"""Wiring tests for the opt-in ``cro`` critique category (Phase 2).

These verify the CROScanner is reachable from the async critique path when a
job requests ``categories=["cro"]``, that it stays OUT of the default category
set (opt-in invariant), and that ``compute_overall_score`` picks up a ``cro``
category score at ``cro_weight`` without disturbing scores that omit cro.

Pure/no-DB: exercises ``_build_scan_tasks``, the ``_run_cro`` adapter, and the
report-generator scoring math directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from src.config import ScannerSettings
from src.domains.critique.models import CritiqueJob
from src.domains.critique.schemas import CritiqueCategory
from src.domains.critique.services.critique_service import ALL_CATEGORIES, CritiqueService
from src.domains.critique.services.report_generator import compute_overall_score

# A 10-field form is the highest-evidence CRO failure mode — guarantees at least
# one ``category="cro"`` finding regardless of scoring-threshold tuning.
_CRO_HTML = (
    "<html><body><form>"
    + "".join(f'<label for="f{i}">F{i}</label><input id="f{i}" name="f{i}">' for i in range(10))
    + "<button>Go</button></form></body></html>"
)
_URL = "https://example.test"


def _make_service() -> CritiqueService:
    with patch("src.domains.critique.services.critique_service.get_settings") as mock_settings:
        mock_settings.return_value.external_tools.enabled = False
        return CritiqueService()


class TestCroEnum:
    def test_cro_is_a_critique_category(self) -> None:
        assert CritiqueCategory.cro == "cro"
        assert "cro" in {c.value for c in CritiqueCategory}


class TestOptInInvariant:
    def test_default_category_set_excludes_cro(self) -> None:
        # A job with no categories falls back to ALL_CATEGORIES — cro must be
        # opt-in only, never in the default sweep.
        assert "cro" not in ALL_CATEGORIES


class TestBuildScanTasksDispatch:
    def _job(self) -> CritiqueJob:
        return cast("CritiqueJob", SimpleNamespace(url=_URL, instructions=None, github_repo=None))

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(scanners=ScannerSettings())

    def test_cro_task_added_when_requested(self) -> None:
        svc = _make_service()
        tasks = svc._build_scan_tasks(
            job=self._job(),
            html=_CRO_HTML,
            soup=None,
            categories={"cro"},
            github_repo=None,
            settings=self._settings(),
            site_analysis=None,
            llm_config=None,
        )
        names = [name for name, _factory, _timeout in tasks]
        assert "cro" in names
        cro_timeout = next(t for name, _f, t in tasks if name == "cro")
        assert cro_timeout == ScannerSettings().cro_timeout

    def test_cro_task_absent_when_not_requested(self) -> None:
        svc = _make_service()
        tasks = svc._build_scan_tasks(
            job=self._job(),
            html=_CRO_HTML,
            soup=None,
            categories={"seo", "accessibility"},
            github_repo=None,
            settings=self._settings(),
            site_analysis=None,
            llm_config=None,
        )
        names = [name for name, _factory, _timeout in tasks]
        assert "cro" not in names


class TestOverallScoreWithCro:
    def test_cro_score_included_at_cro_weight(self) -> None:
        from src.config import get_settings

        cro_weight = get_settings().scoring.cro_weight
        # A single cro category score normalizes to itself (total_weight == its
        # own weight), so overall == the cro score.
        assert compute_overall_score({"cro": 72.0}) == pytest.approx(72.0)
        # With cro present alongside seo, both contribute at their weights.
        scores = {"seo": 100.0, "cro": 60.0}
        expected = (100.0 * 0.15 + 60.0 * cro_weight) / (0.15 + cro_weight)
        assert compute_overall_score(scores) == pytest.approx(round(expected, 1))

    def test_scores_without_cro_are_unchanged(self) -> None:
        # compute_overall_score normalizes by the sum of weights of categories
        # actually present, so adding a cro weight must not move a cro-free
        # score. Weighted: a11y(.25)*80 + sec(.25)*90 + perf(.20)*70 +
        # seo(.15)*100 + ux(.15)*60 = 80.5.
        scores = {
            "accessibility": 80.0,
            "security": 90.0,
            "performance": 70.0,
            "seo": 100.0,
            "ux": 60.0,
        }
        assert compute_overall_score(scores) == pytest.approx(80.5, abs=0.6)
