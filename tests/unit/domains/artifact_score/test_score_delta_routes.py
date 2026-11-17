"""Unit tests for the synchronous before/after score-delta route.

Same house style as ``test_artifact_score_routes.py``: the route is called
directly with a mock ``Request``; the per-side scorer is faked so the delta
math (``after - before``) and the finding diff are exercised deterministically
with no HTTP/LLM/DB traffic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from w2v_common.auth.middleware import AuthMiddleware

from src.domains.artifact_score.routes import score_delta_route
from src.domains.artifact_score.schemas import ArtifactSource, ScoreDeltaRequest
from src.domains.artifact_score.service import ArtifactScoreError

_DELTA_PATH = "/api/brand/score-delta"

_SCORER = "src.domains.artifact_score.service.score_artifact_with_findings"


def _make_request(tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e") -> MagicMock:
    request = MagicMock()
    request.state.tenant_id = tenant_id
    return request


def _payload(overall: float, category_scores: dict[str, float], source: str) -> dict:
    """A full score-artifact payload (all ArtifactScoreResponse fields present)."""
    return {
        "overall_score": overall,
        "category_scores": category_scores,
        "confidence": {c: 1.0 for c in category_scores},
        "findings_summary": [],
        "source": source,
        "categories": list(category_scores),
        "scored_at": "2026-07-04T00:00:00+00:00",
    }


_SEO_ISSUE = {"category": "seo", "severity": "medium", "check_id": "seo.meta_description"}
_A11Y_ISSUE = {"category": "accessibility", "severity": "high", "check_id": "a11y.img_alt"}


# ---------------------------------------------------------------------------
# Request validation — each side needs exactly one of url/html
# ---------------------------------------------------------------------------


class TestDeltaRequestValidation:
    def test_source_neither_is_invalid(self):
        with pytest.raises(ValidationError):
            ArtifactSource()

    def test_source_both_is_invalid(self):
        with pytest.raises(ValidationError):
            ArtifactSource(url="https://x.test", html="<html></html>")

    def test_valid_before_after(self):
        body = ScoreDeltaRequest(
            before=ArtifactSource(url="https://before.test"),
            after=ArtifactSource(html="<html></html>"),
        )
        assert body.before.url == "https://before.test"
        assert body.after.html == "<html></html>"


# ---------------------------------------------------------------------------
# Route — delta math + finding diff
# ---------------------------------------------------------------------------


class TestScoreDeltaRoute:
    async def test_delta_is_after_minus_before(self):
        request = _make_request()
        body = ScoreDeltaRequest(
            before=ArtifactSource(url="https://before.test"),
            after=ArtifactSource(url="https://after.test"),
        )
        # before: seo 50, a11y 70, two issues; after: seo 90 (fixed), a11y 70, one issue.
        before = (
            _payload(60.0, {"seo": 50.0, "accessibility": 70.0}, "https://before.test"),
            [_SEO_ISSUE, _A11Y_ISSUE],
        )
        after = (
            _payload(80.0, {"seo": 90.0, "accessibility": 70.0}, "https://after.test"),
            [_A11Y_ISSUE],
        )
        mock = AsyncMock(side_effect=[before, after])
        with patch(_SCORER, mock):
            result = await score_delta_route(body, request)

        assert result.overall_delta == 20.0
        assert result.score_deltas["seo"] == 40.0
        assert result.score_deltas["accessibility"] == 0.0
        # The seo issue disappeared in 'after' → resolved; nothing new; a11y persists.
        assert result.persistent_findings == 1
        assert [i.category for i in result.resolved_findings] == ["seo"]
        assert result.new_findings == []
        assert result.before.source == "https://before.test"
        assert result.after.source == "https://after.test"

    async def test_new_finding_is_reported(self):
        request = _make_request()
        body = ScoreDeltaRequest(
            before=ArtifactSource(html="<a></a>"),
            after=ArtifactSource(html="<b></b>"),
        )
        before = (_payload(90.0, {"seo": 90.0}, "inline-html"), [])
        after = (_payload(70.0, {"seo": 70.0}, "inline-html"), [_SEO_ISSUE])
        with patch(_SCORER, AsyncMock(side_effect=[before, after])):
            result = await score_delta_route(body, request)
        assert result.overall_delta == -20.0
        assert [i.category for i in result.new_findings] == ["seo"]
        assert result.resolved_findings == []

    async def test_all_unsupported_categories_is_422(self):
        request = _make_request()
        body = ScoreDeltaRequest(
            before=ArtifactSource(html="<a></a>"),
            after=ArtifactSource(html="<b></b>"),
            categories=["ux"],
        )
        with pytest.raises(HTTPException) as exc:
            await score_delta_route(body, request)
        assert exc.value.status_code == 422

    async def test_before_failure_is_502(self):
        request = _make_request()
        body = ScoreDeltaRequest(
            before=ArtifactSource(url="https://down.test"),
            after=ArtifactSource(html="<b></b>"),
        )
        with patch(_SCORER, AsyncMock(side_effect=ArtifactScoreError("fetch failed"))):
            with pytest.raises(HTTPException) as exc:
                await score_delta_route(body, request)
        assert exc.value.status_code == 502

    async def test_after_failure_is_502(self):
        request = _make_request()
        body = ScoreDeltaRequest(
            before=ArtifactSource(html="<a></a>"),
            after=ArtifactSource(url="https://down.test"),
        )
        before = (_payload(90.0, {"seo": 90.0}, "inline-html"), [])
        with patch(_SCORER, AsyncMock(side_effect=[before, ArtifactScoreError("boom")])):
            with pytest.raises(HTTPException) as exc:
                await score_delta_route(body, request)
        assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Auth — the delta route must require the license token (not exempt)
# ---------------------------------------------------------------------------


class TestScoreDeltaRequiresAuth:
    def test_path_is_not_public(self):
        from w2v_common.auth.middleware import PUBLIC_ENDPOINTS, PUBLIC_PREFIXES

        assert _DELTA_PATH not in PUBLIC_ENDPOINTS
        assert not _DELTA_PATH.startswith(PUBLIC_PREFIXES)

    async def test_no_token_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()
        request = MagicMock()
        request.url.path = _DELTA_PATH
        request.headers = MagicMock()
        request.headers.get = MagicMock(side_effect=lambda key, default=None: default)
        request.client = MagicMock()
        request.client.host = "8.8.8.8"
        request.state = MagicMock()
        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()
