"""Unit tests for VideoCompilerClient."""

from __future__ import annotations

import base64
import functools
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.domains.review.services.video_client import VideoCompilerClient, _map_finding_to_input


@pytest.mark.unit
class TestMapFindingToInput:
    def test_maps_fields_correctly(self) -> None:
        finding = {
            "description": "layout_consistency: 8/10",
            "evidence": ["Good grid", "Consistent margins", "Clean layout", "Extra"],
            "severity": "low",
            "review_type": "design_review",
        }
        result = _map_finding_to_input(finding)
        assert result["title"] == "layout_consistency: 8/10"
        assert result["description"] == "Good grid; Consistent margins; Clean layout"
        assert result["severity"] == "low"
        assert result["category"] == "design_review"

    def test_empty_evidence_uses_description(self) -> None:
        finding = {
            "description": "Some issue",
            "evidence": [],
            "severity": "high",
            "review_type": "usability_review",
        }
        result = _map_finding_to_input(finding)
        assert result["description"] == "Some issue"

    def test_missing_fields_use_defaults(self) -> None:
        result = _map_finding_to_input({})
        assert result["title"] == ""
        assert result["severity"] == "info"
        assert result["category"] == ""

    def test_evidence_truncated_to_three(self) -> None:
        finding = {
            "description": "many items",
            "evidence": ["a", "b", "c", "d", "e"],
            "severity": "medium",
            "review_type": "perf",
        }
        result = _map_finding_to_input(finding)
        # Only first 3 evidence items joined
        assert result["description"] == "a; b; c"


@pytest.mark.unit
class TestVideoCompilerClient:
    def test_constructor_sets_fields(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309", timeout=300.0)
        assert client.base_url == "http://localhost:6309"
        assert client.service_name == "video_compiler"
        assert client.timeout == 300.0

    def test_constructor_strips_trailing_slash(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309/")
        assert client.base_url == "http://localhost:6309"

    def test_default_timeout(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309")
        assert client.timeout == 120.0

    async def test_request_findings_video_success(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309", timeout=30.0)

        expected_response = {
            "job_id": "vid-001",
            "poll_url": "/api/video/vid-001/status",
        }

        findings = [
            {
                "description": "contrast_ratios: 5/10",
                "evidence": ["Low contrast on buttons"],
                "severity": "high",
                "review_type": "design_review",
            }
        ]

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = expected_response

            result = await client.request_findings_video(
                findings=findings,
                title="Test Review",
                subtitle="Job 123",
            )

        assert result == expected_response
        assert result["job_id"] == "vid-001"

        # Verify _request was called with correct args
        mock_request.assert_awaited_once_with(
            "POST",
            "/api/compile/findings-video",
            json={
                "title": "Test Review",
                "subtitle": "Job 123",
                "findings": [
                    {
                        "title": "contrast_ratios: 5/10",
                        "description": "Low contrast on buttons",
                        "severity": "high",
                        "category": "design_review",
                    }
                ],
            },
        )

    async def test_request_findings_video_maps_multiple_findings(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309")

        findings = [
            {
                "description": "Issue 1",
                "evidence": ["e1", "e2"],
                "severity": "high",
                "review_type": "design_review",
            },
            {
                "description": "Issue 2",
                "evidence": [],
                "severity": "low",
                "review_type": "usability_review",
            },
        ]

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"job_id": "vid-002", "poll_url": "/status"}

            result = await client.request_findings_video(findings=findings, title="Multi")

        call_kwargs = mock_request.call_args
        payload = call_kwargs.kwargs["json"]
        assert len(payload["findings"]) == 2
        assert payload["findings"][0]["title"] == "Issue 1"
        assert payload["findings"][0]["description"] == "e1; e2"
        assert payload["findings"][1]["title"] == "Issue 2"
        assert payload["findings"][1]["description"] == "Issue 2"
        assert result["job_id"] == "vid-002"

    async def test_request_findings_video_propagates_http_error(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = httpx.HTTPStatusError(
                "Internal Server Error",
                request=httpx.Request("POST", "http://localhost:6309/api/compile/findings-video"),
                response=httpx.Response(500),
            )

            with pytest.raises(httpx.HTTPStatusError):
                await client.request_findings_video(findings=[{"description": "test"}])

    async def test_request_findings_video_propagates_transport_error(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = httpx.ConnectError("Connection refused")

            with pytest.raises(httpx.ConnectError):
                await client.request_findings_video(findings=[{"description": "test"}])

    async def test_request_findings_video_default_title_subtitle(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {"job_id": "vid-003"}

            await client.request_findings_video(findings=[])

        call_kwargs = mock_request.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["title"] == "Website Review"
        assert payload["subtitle"] == ""
        assert payload["findings"] == []

    async def test_close_delegates_to_base(self) -> None:
        client = VideoCompilerClient(base_url="http://localhost:6309")
        # Should not raise even when no client has been created
        await client.close()
        assert client._client is None

    async def test_never_bakes_the_shared_downstream_token(self, monkeypatch) -> None:
        """video-production is an in-cell peer: the hop is authenticated by the
        cell JWT, so the shared DOWNSTREAM_LICENSE_TOKEN must not ride on it
        even when the pod has it configured."""
        monkeypatch.setenv("DOWNSTREAM_LICENSE_TOKEN", "shared-tok")
        client = VideoCompilerClient(base_url="http://localhost:6309")
        try:
            real = await client._get_client()
            assert real.headers.get("X-Tenant-Id") is None
            assert real.headers.get("Authorization") is None
        finally:
            await client.close()


@pytest.mark.unit
class TestFindingsVideoWireRoute:
    """The request must reach the route the callee actually serves.

    video_production_service (origin/main) mounts ``APIRouter(prefix="/api/compile")``
    in ``src/api/routes/compilation.py`` and registers ``"/findings-video"`` on it
    in ``_compilation_extras.py``. The fake below serves exactly that route and
    404s everything else, like the real FastAPI app — so a client that POSTs any
    other path (it used to send ``/api/findings-video``) fails here instead of
    passing against a mocked ``_request`` that accepts whatever path it is given.
    """

    async def test_posts_to_the_callee_compile_router(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.method == "POST" and request.url.path == "/api/compile/findings-video":
                return httpx.Response(
                    200,
                    json={
                        "job_id": "vid-9",
                        "status": "pending",
                        "poll_url": "/api/compile/jobs/vid-9",
                    },
                )
            return httpx.Response(404, json={"detail": "Not Found"})

        client = VideoCompilerClient(base_url="http://video-production.test")
        client.set_transport(httpx.MockTransport(handler))
        try:
            result = await client.request_findings_video(
                findings=[{"description": "d", "severity": "high", "review_type": "design_review"}]
            )
        finally:
            await client.close()

        assert seen == [("POST", "/api/compile/findings-video")]
        assert result["job_id"] == "vid-9"
        assert result["poll_url"] == "/api/compile/jobs/vid-9"


@pytest.mark.unit
class TestFindingsVideoHopIsCellAuthenticated:
    """The real hooks run: the hop carries a fresh cell JWT and no shared token.

    Only the socket is replaced. The signer is a throwaway Ed25519 key; the
    destination is ``service_url("video_production")`` so the audience resolves
    exactly as it does in the pod.
    """

    _TENANT = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

    async def test_in_cell_hop_sends_cell_token_not_shared_licence(self, monkeypatch) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from w2v_common.auth.cell_jwt import CELL_TOKEN_HEADER, reset_cell_state
        from w2v_common.auth.identity_context import clear_identity_context, set_identity_context
        from w2v_common.clients import base as client_base
        from w2v_common.discovery import service_url
        from w2v_common.tenant import TenantSource

        pem = (
            Ed25519PrivateKey.generate()
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        monkeypatch.setenv("CELL_JWT_SIGNING_KEY", pem)
        monkeypatch.setenv("CELL_JWT_KID", "analysis-service-test")
        monkeypatch.setenv("CELL_SERVICE_NAME", "analysis-service")
        monkeypatch.setenv("CELL_DEPLOYMENT_ID", "test-cell")
        monkeypatch.setenv("DOWNSTREAM_LICENSE_TOKEN", "shared-tok")
        reset_cell_state()

        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(
                200, json={"job_id": "vid-1", "poll_url": "/api/compile/jobs/vid-1"}
            )

        monkeypatch.setattr(
            client_base.httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
        )

        set_identity_context(
            tenant_id=self._TENANT,
            license_id=None,
            subject_id=None,
            tenant_source=TenantSource.CELL_JWT,
        )
        client = VideoCompilerClient(base_url=service_url("video_production"))
        try:
            await client.request_findings_video(findings=[{"description": "d"}])
        finally:
            await client.close()
            clear_identity_context()
            reset_cell_state()

        assert len(sent) == 1
        headers = sent[0].headers
        assert "shared-tok" not in " ".join(headers.values())
        assert not headers.get("X-Tenant-Id", "").lower().startswith("bearer")
        token = headers.get(CELL_TOKEN_HEADER)
        assert token, "no cell token on an in-cell hop"
        payload_b64 = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        assert claims["aud"] == "video-production-service"
        assert claims["iss"] == "analysis-service"
        assert claims["tenant"] == self._TENANT
