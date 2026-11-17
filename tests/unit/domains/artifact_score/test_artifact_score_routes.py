"""Unit tests for the synchronous artifact-score route and its scoring core.

Routes are called directly with a mock ``Request`` (the brand/analyze/icp house
style) — no live TestClient. The static scanners and the fetch are faked so no
HTTP traffic leaves the test and no LLM/DB is touched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from w2v_common.auth.middleware import AuthMiddleware

from src.domains.artifact_score.routes import score_artifact_route
from src.domains.artifact_score.schemas import ArtifactScoreRequest
from src.domains.artifact_score.service import ArtifactScoreError, resolve_categories

# Route path is under /api and NOT /api/health* → auth-required by design.
_SCORE_PATH = "/api/brand/score-artifact"

_SEO_FINDINGS = [
    {
        "category": "seo",
        "severity": "medium",
        "title": "Missing meta description",
        "check_id": "seo.meta_description",
    },
]
_A11Y_FINDINGS = [
    {
        "category": "accessibility",
        "severity": "high",
        "title": "Image missing alt text",
        "check_id": "a11y.img_alt",
    },
]
_PERF_FINDINGS = [
    {
        "category": "performance",
        "severity": "low",
        "title": "Unoptimized image",
        "check_id": "perf.img_size",
    },
]


def _make_request(tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e") -> MagicMock:
    """Build a mock Request exposing request.state.tenant_id (set by AuthMiddleware)."""
    request = MagicMock()
    request.state.tenant_id = tenant_id
    return request


def _patch_scanners(
    seo: list[dict] | None = None,
    a11y: list[dict] | None = None,
    perf: list[dict] | None = None,
):
    """Patch the three static scanner instances' ``.scan`` with canned findings."""
    from src.domains.artifact_score import service

    return (
        patch.object(service._SCANNERS["seo"], "scan", return_value=seo or _SEO_FINDINGS),
        patch.object(
            service._SCANNERS["accessibility"], "scan", return_value=a11y or _A11Y_FINDINGS
        ),
        patch.object(service._SCANNERS["performance"], "scan", return_value=perf or _PERF_FINDINGS),
    )


# ---------------------------------------------------------------------------
# resolve_categories — the static-only gate
# ---------------------------------------------------------------------------


class TestResolveCategories:
    def test_none_defaults_to_all_static(self):
        assert resolve_categories(None) == ["seo", "accessibility", "performance"]

    def test_drops_unsupported_categories(self):
        # ux / security / seo_live are not runnable synchronously — dropped.
        assert resolve_categories(["seo", "ux", "security", "seo_live"]) == ["seo"]

    def test_all_unsupported_reduces_to_empty(self):
        assert resolve_categories(["ux", "security"]) == []

    def test_dedupes_preserving_order(self):
        assert resolve_categories(["performance", "seo", "performance"]) == ["performance", "seo"]


# ---------------------------------------------------------------------------
# Request validation — exactly one of url/html (→ 422 at the API boundary)
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_neither_url_nor_html_is_invalid(self):
        with pytest.raises(ValidationError):
            ArtifactScoreRequest()

    def test_both_url_and_html_is_invalid(self):
        with pytest.raises(ValidationError):
            ArtifactScoreRequest(url="https://x.test", html="<html></html>")

    def test_url_only_ok(self):
        body = ArtifactScoreRequest(url="https://x.test")
        assert body.url == "https://x.test"

    def test_html_only_ok(self):
        body = ArtifactScoreRequest(html="<html><body>hi</body></html>")
        assert body.html == "<html><body>hi</body></html>"
        assert body.url is None


# ---------------------------------------------------------------------------
# Route happy paths
# ---------------------------------------------------------------------------


class TestScoreArtifactRoute:
    async def test_html_input_scores_all_static_categories(self):
        request = _make_request()
        body = ArtifactScoreRequest(html="<html><body>content</body></html>")
        p_seo, p_a11y, p_perf = _patch_scanners()
        with p_seo, p_a11y, p_perf:
            result = await score_artifact_route(body, request)
        assert result.source == "inline-html"
        assert result.categories == ["seo", "accessibility", "performance"]
        assert set(result.category_scores) == {"seo", "accessibility", "performance"}
        assert 0.0 <= result.overall_score <= 100.0
        # Every scored category has a confidence entry.
        assert set(result.confidence) == {"seo", "accessibility", "performance"}
        # Findings roll up to one row per category, sorted, with the top severity.
        summ = {i.category: i for i in result.findings_summary}
        assert summ["accessibility"].top_severity == "high"
        assert summ["accessibility"].count == 1
        assert result.scored_at.endswith("+00:00") or result.scored_at.endswith("Z")

    async def test_url_input_fetches_then_scores(self):
        request = _make_request()
        body = ArtifactScoreRequest(url="https://merqi.nl")
        fake_fetch = AsyncMock(return_value="<html><body>fetched</body></html>")
        p_seo, p_a11y, p_perf = _patch_scanners()
        with (
            patch("src.domains.artifact_score.service._default_fetch_html", fake_fetch),
            p_seo,
            p_a11y,
            p_perf,
        ):
            result = await score_artifact_route(body, request)
        fake_fetch.assert_awaited_once_with("https://merqi.nl")
        assert result.source == "https://merqi.nl"
        assert result.category_scores  # non-empty

    async def test_subset_categories_only_runs_requested(self):
        request = _make_request()
        body = ArtifactScoreRequest(html="<html></html>", categories=["seo"])
        p_seo, p_a11y, p_perf = _patch_scanners()
        with p_seo, p_a11y, p_perf:
            result = await score_artifact_route(body, request)
        assert result.categories == ["seo"]
        assert set(result.category_scores) == {"seo"}

    async def test_all_unsupported_categories_is_422(self):
        request = _make_request()
        body = ArtifactScoreRequest(html="<html></html>", categories=["ux"])
        with pytest.raises(HTTPException) as exc:
            await score_artifact_route(body, request)
        assert exc.value.status_code == 422

    async def test_fetch_failure_is_502(self):
        request = _make_request()
        body = ArtifactScoreRequest(url="https://down.test")
        failing = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch("src.domains.artifact_score.service._default_fetch_html", failing):
            with pytest.raises(HTTPException) as exc:
                await score_artifact_route(body, request)
        assert exc.value.status_code == 502

    async def test_scan_failure_is_502(self):
        request = _make_request()
        body = ArtifactScoreRequest(html="<html></html>")
        from src.domains.artifact_score import service

        with patch.object(service._SCANNERS["seo"], "scan", side_effect=ValueError("boom")):
            with pytest.raises(HTTPException) as exc:
                await score_artifact_route(body, request)
        assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Scoring core — ArtifactScoreError contract
# ---------------------------------------------------------------------------


class TestScoreArtifactCore:
    async def test_empty_html_raises(self):
        from src.domains.artifact_score.service import score_artifact

        with pytest.raises(ArtifactScoreError):
            await score_artifact(html="   ", url=None, categories=["seo"])

    async def test_clean_artifact_is_not_scored_as_an_f(self):
        """Zero findings on THIS path can only mean clean, and must score 100.

        ``compute_scores`` derives its categories from the findings, so a clean
        scan hands ``compute_overall_score`` an empty dict -- whose documented
        default is ``0.0``, the worst possible score. That default exists for
        the async critique pipeline, where a crashed scanner also yields zero
        findings. Here it cannot: ``gather_findings`` gathers WITHOUT
        ``return_exceptions``, so a scanner failure is already a 502 (pinned by
        ``test_scan_failure_is_502`` above). A flawless page must not be graded F.
        """
        # NOTE: not ``_patch_scanners([], [], [])`` -- that helper does
        # ``seo or _SEO_FINDINGS``, so an empty list falls back to findings and
        # the test would silently exercise the non-clean path.
        from src.domains.artifact_score import service
        from src.domains.artifact_score.service import score_artifact

        with (
            patch.object(service._SCANNERS["seo"], "scan", return_value=[]),
            patch.object(service._SCANNERS["accessibility"], "scan", return_value=[]),
            patch.object(service._SCANNERS["performance"], "scan", return_value=[]),
        ):
            payload = await score_artifact(
                html="<html><body>clean</body></html>",
                url=None,
                categories=resolve_categories(None),
            )

        assert payload["category_scores"] == {}, "a clean scan has no scored categories"
        assert payload["overall_score"] == 100.0, (
            "a clean artifact was scored 0.0 (grade F) because an ABSENT finding "
            "set is indistinguishable from an unmeasured one to compute_overall_score"
        )

    async def test_a_finding_still_lowers_the_score(self):
        """The 100.0 must come from a real clean scan, not from a blanket floor."""
        from src.domains.artifact_score import service
        from src.domains.artifact_score.service import score_artifact

        with (
            patch.object(service._SCANNERS["seo"], "scan", return_value=_SEO_FINDINGS),
            patch.object(service._SCANNERS["accessibility"], "scan", return_value=[]),
            patch.object(service._SCANNERS["performance"], "scan", return_value=[]),
        ):
            payload = await score_artifact(
                html="<html><body>x</body></html>",
                url=None,
                categories=resolve_categories(None),
            )

        assert payload["category_scores"], "the seo finding must produce a category score"
        assert payload["overall_score"] < 100.0


# ---------------------------------------------------------------------------
# Auth — the route must require the license token (not exempt)
# ---------------------------------------------------------------------------


class TestScoreArtifactRequiresAuth:
    def _make_auth_request(
        self, headers: dict | None = None, client_host: str = "8.8.8.8"
    ) -> MagicMock:
        request = MagicMock()
        request.url.path = _SCORE_PATH
        request.headers = MagicMock()
        request.headers.get = MagicMock(
            side_effect=lambda key, default=None: (headers or {}).get(key, default)
        )
        request.client = MagicMock()
        request.client.host = client_host
        request.state = MagicMock()
        return request

    def test_path_is_not_public(self):
        from w2v_common.auth.middleware import PUBLIC_ENDPOINTS, PUBLIC_PREFIXES

        assert _SCORE_PATH not in PUBLIC_ENDPOINTS
        assert not _SCORE_PATH.startswith(PUBLIC_PREFIXES)

    async def test_no_token_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()
        request = self._make_auth_request(headers={})
        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()
