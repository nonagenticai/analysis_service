"""Client for the findings-video endpoint of video-production-service.

The compiler domain was merged into video-production-service (discovery name
``video_production``); the route lives on its ``/api/compile`` router.
"""

from __future__ import annotations

import logging

from w2v_common.clients.base import BaseServiceClient

logger = logging.getLogger(__name__)

#: The callee route. video_production_service ``src/api/routes/compilation.py``
#: mounts ``APIRouter(prefix="/api/compile")`` and ``_compilation_extras.py``
#: registers ``"/findings-video"`` on it -> ``POST /api/compile/findings-video``.
#: This client used to POST ``/api/findings-video``, a path no service serves,
#: so every review-video request 404'd (surfaced as a 503 by the route).
FINDINGS_VIDEO_PATH = "/api/compile/findings-video"


def _map_finding_to_input(finding: dict) -> dict:
    """Map a ReviewFinding dict to the video_compiler FindingInput format."""
    evidence_items = finding.get("evidence", [])[:3]
    description = "; ".join(evidence_items) if evidence_items else finding.get("description", "")

    return {
        "title": finding.get("description", ""),
        "description": description,
        "severity": finding.get("severity", "info"),
        "category": finding.get("review_type", ""),
    }


class VideoCompilerClient(BaseServiceClient):
    """Async HTTP client for video-production-service's findings-video route.

    Extends BaseServiceClient to get automatic retry logic (3x on
    429/500/502/503/504), connection pooling, and health checks.

    video-production is an IN-CELL w2v peer (``CELL_AUTH_MODE=enforce`` live,
    ``CELL_ALLOWED_CALLERS`` includes ``analysis-service``). The hop is
    authenticated by the per-request ``X-W2V-Cell-Token`` that
    BaseServiceClient's ``identity_event_hook`` mints for an in-cell
    destination from the bound (vouched) tenant — NOT by the shared
    ``DOWNSTREAM_LICENSE_TOKEN``. The client therefore bakes no licence: the
    shared token used to ride here as ``X-Tenant-Id: Bearer <token>``, which an
    enforce receiver refuses for a delegating subject anyway
    (``CELL_TOKEN_REQUIRED``) and which the cell template retires.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            service_name="video_compiler",
            timeout=timeout,
        )

    async def request_findings_video(
        self,
        findings: list[dict],
        title: str = "Website Review",
        subtitle: str = "",
    ) -> dict:
        """Call video_compiler_service to generate a findings video.

        Args:
            findings: List of finding dicts (from extract_findings).
            title: Video title.
            subtitle: Video subtitle.

        Returns:
            Dict with ``job_id`` and ``poll_url`` from the video compiler.

        Raises:
            httpx.HTTPStatusError: On non-retryable HTTP error.
            httpx.TransportError: On network failure after all retries.
        """
        payload = {
            "title": title,
            "subtitle": subtitle,
            "findings": [_map_finding_to_input(f) for f in findings],
        }

        logger.info(
            "Requesting findings video: base_url=%s, findings_count=%d",
            self.base_url,
            len(findings),
        )

        data = await self._request("POST", FINDINGS_VIDEO_PATH, json=payload)

        logger.info("Video compilation requested: job_id=%s", data.get("job_id"))
        return data
