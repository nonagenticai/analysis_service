"""Code quality scanner using CGH (Code Guardian Hub) API.

Analyzes GitHub repositories for code quality issues via external API.
"""

from __future__ import annotations

import logging
import os

import httpx

from src.domains.critique.services.scanners.base import make_finding

logger = logging.getLogger(__name__)

# Map CGH severity to critique severity
CGH_SEVERITY_MAP = {
    "error": "high",
    "warning": "medium",
    "info": "low",
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}


class CodeQualityScanner:
    """Scans GitHub repos for code quality issues via CGH API."""

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 120,
    ) -> None:
        self._api_url = api_url or os.environ.get("CGH_API_URL", "")
        self._api_key = api_key or os.environ.get("CGH_API_KEY", "")
        self._timeout = timeout

    @property
    def is_configured(self) -> bool:
        """Check if the CGH API is configured."""
        return bool(self._api_url)

    async def scan(self, repo_url: str) -> list[dict]:
        """Scan a GitHub repository for code quality issues.

        Args:
            repo_url: GitHub repository URL to analyze.

        Returns:
            List of code quality finding dicts.
        """
        if not self.is_configured:
            return [
                make_finding(
                    category="code_quality",
                    severity="info",
                    title="Code quality scanner not configured",
                    description="CGH API URL not set. Set CGH_API_URL and CGH_API_KEY to enable code quality scanning.",
                    recommendation="Configure CGH_API_URL environment variable.",
                    source_tool="code_quality_scanner",
                    check_id="cq-not-configured",
                )
            ]

        import time

        logger.info("Starting CGH API scan: repo=%s", repo_url)
        t0 = time.monotonic()
        try:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._api_url.rstrip('/')}/analyze",
                    json={"repo_url": repo_url},
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

            findings: list[dict] = []
            for item in data.get("findings", []):
                findings.append(self._normalize_cgh_finding(item, repo_url))
            elapsed = round(time.monotonic() - t0, 2)
            logger.info(
                "CGH API scan completed: repo=%s findings=%d elapsed=%.2fs",
                repo_url,
                len(findings),
                elapsed,
            )
            return findings

        except httpx.TimeoutException:
            logger.error("CGH API timeout for %s", repo_url)
            return [
                make_finding(
                    category="code_quality",
                    severity="info",
                    title="Code quality scan timed out",
                    description=f"CGH API timed out analyzing {repo_url}.",
                    url=repo_url,
                    source_tool="code_quality_scanner",
                    check_id="cq-timeout",
                )
            ]
        # ``httpx.HTTPError`` is load-bearing and must stay. Every httpx failure
        # class derives from it and from ``Exception`` ONLY -- not from ``OSError``:
        # ``HTTPStatusError`` (raised by the ``raise_for_status()`` above on a CGH
        # 401/500) and the whole ``RequestError`` family (``ConnectError`` when the
        # CGH pod is down, ``RemoteProtocolError`` when it drops the connection
        # mid-response) are NOT caught by any builtin listed here. Without this name
        # they escaped ``scan``, and ``CritiquePhases._phase_run_scanners`` gathers
        # scanners with ``return_exceptions=True``: the raise was logged, the scanner
        # contributed ZERO findings, and the job still finalized and SCORED -- an
        # unreachable CGH read as clean code. ``TimeoutException`` keeps its own arm
        # above only for its more specific finding text.
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
            logger.error("CGH API error for %s: %s", repo_url, e)
            return [
                make_finding(
                    category="code_quality",
                    severity="info",
                    title="Code quality scan error",
                    description=f"CGH API error: {e}",
                    url=repo_url,
                    source_tool="code_quality_scanner",
                    check_id="cq-error",
                )
            ]

    def _normalize_cgh_finding(self, item: dict, repo_url: str) -> dict:
        """Normalize a CGH API finding to the standard format."""
        cgh_severity = item.get("severity", "info").lower()
        severity = CGH_SEVERITY_MAP.get(cgh_severity, "info")

        return make_finding(
            category="code_quality",
            severity=severity,
            title=item.get("title", item.get("rule", "Code quality issue")),
            description=item.get("message", item.get("description", "")),
            url=repo_url,
            recommendation=item.get("fix", item.get("recommendation", "")),
            check_id=f"cq-{item.get('rule', 'unknown')}",
            source_tool="code_quality_scanner",
            evidence=item.get("file", ""),
            remediation_effort=item.get("effort"),
        )
