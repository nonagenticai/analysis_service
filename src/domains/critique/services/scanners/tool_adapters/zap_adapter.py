"""OWASP ZAP security adapter.

Communicates with a running ZAP instance via its REST API to spider a
target URL, wait for passive scanning, and retrieve security alerts.

SSRF note
---------
The httpx calls here go to the operator-configured ``zap_api_url``, not to the
tenant -- but the tenant URL is handed to ZAP as the ``url`` spider seed, and
**ZAP** then fetches it. That makes this an out-of-process fetch sink in the
same class as the Lighthouse and nuclei adapters, just reached over REST
instead of argv, and equally invisible to ``install_ssrf_guard``.

:func:`resolve_external_scan_target` validates the seed before it is handed
over, closing hop 0. It closes nothing beyond that: ZAP's spider follows links
by design, and no verified ZAP API parameter constrains which hosts it may
reach. Egress containment at the network layer -- for the ZAP deployment, not
for this pod -- is the only complete answer here.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from src.config import get_settings
from src.domains.critique.services.scanners.base import make_finding
from src.shared.security.external_process_target import resolve_external_scan_target

logger = logging.getLogger(__name__)

# ZAP risk integer -> critique_service severity
_RISK_TO_SEVERITY: dict[int, str] = {
    0: "info",
    1: "low",
    2: "medium",
    3: "high",
}

# ZAP confidence integer -> false-positive risk label
_CONFIDENCE_TO_FP_RISK: dict[int, str] = {
    0: "high",  # false positive
    1: "high",  # low confidence
    2: "medium",  # medium confidence
    3: "low",  # high confidence
    4: "low",  # confirmed
}

# Polling interval when waiting for the spider / passive scan to finish
_POLL_INTERVAL_SECONDS = 3


class ZapAdapter:
    """ToolAdapter that drives OWASP ZAP via its REST API."""

    @property
    def tool_name(self) -> str:
        return "zap"

    @property
    def category(self) -> str:
        return "security"

    async def is_available(self) -> bool:
        """Ping ZAP's REST API and confirm the adapter is enabled."""
        settings = get_settings()
        if not settings.external_tools.zap_enabled:
            return False
        api_url = settings.external_tools.zap_api_url.rstrip("/")
        api_key = settings.external_tools.zap_api_key
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                params: dict[str, str] = {}
                if api_key:
                    params["apikey"] = api_key
                resp = await client.get(f"{api_url}/JSON/core/view/version/", params=params)
                resp.raise_for_status()
                return True
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("ZAP API unreachable at %s: %s", api_url, exc)
            return False

    async def scan(self, url: str, html: str | None = None) -> list[dict]:
        """Spider the URL, wait for passive scan, and return alert findings."""
        settings = get_settings()
        api_url = settings.external_tools.zap_api_url.rstrip("/")
        api_key = settings.external_tools.zap_api_key
        timeout = settings.external_tools.zap_timeout

        try:
            target = await resolve_external_scan_target(url)
        except (ValueError, OSError) as exc:
            logger.warning("ZAP refusing unsafe spider seed %s: %s", url, exc)
            return []

        base_params: dict[str, str] = {}
        if api_key:
            base_params["apikey"] = api_key

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # ------ 1. Start the spider ------
                spider_params = {**base_params, "url": target.url, "maxChildren": "10"}
                resp = await client.get(
                    f"{api_url}/JSON/spider/action/scan/",
                    params=spider_params,
                )
                resp.raise_for_status()
                scan_id = resp.json().get("scan", "0")

                # Wait for spider to complete
                await self._wait_for_spider(client, api_url, base_params, scan_id, timeout)

                # ------ 2. Wait for passive scan to finish ------
                await self._wait_for_passive_scan(client, api_url, base_params, timeout)

                # ------ 3. Fetch alerts ------
                alert_params = {**base_params, "baseurl": target.url, "start": "0", "count": "200"}
                resp = await client.get(
                    f"{api_url}/JSON/alert/view/alerts/",
                    params=alert_params,
                )
                resp.raise_for_status()
                alerts: list[dict] = resp.json().get("alerts", [])

        # Not "dead code shadowed by the clause below" -- it is FIRST, so
        # nothing shadows it. It is dead because nothing in the guarded block
        # can raise the builtin ``TimeoutError``: every call is an httpx one
        # (httpx raises ``TimeoutException``, which is an ``HTTPError``, not the
        # builtin) and the two poll helpers use ``asyncio.sleep`` with a
        # hand-checked deadline rather than ``asyncio.wait_for``, so no
        # ``asyncio.TimeoutError`` is raised either. The distinct
        # "timed out" log it exists to emit therefore never fired; a ZAP timeout
        # was logged as a generic "scan failed". Named for what httpx really
        # raises, and kept ahead of the ``HTTPError`` clause it is a subclass of.
        except httpx.TimeoutException:
            logger.warning("ZAP scan timed out after %ds for %s", timeout, url)
            return []
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("ZAP scan failed for %s: %s", url, exc)
            return []

        return self._alerts_to_findings(alerts, url)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_spider(
        self,
        client: httpx.AsyncClient,
        api_url: str,
        base_params: dict[str, str],
        scan_id: str,
        timeout: int,  # noqa: ASYNC109 — why: ZAP poll deadline; checked against elapsed inside the loop
    ) -> None:
        """Poll until the spider completes or the timeout elapses."""
        elapsed = 0.0
        while elapsed < timeout:
            resp = await client.get(
                f"{api_url}/JSON/spider/view/status/",
                params={**base_params, "scanId": scan_id},
            )
            resp.raise_for_status()
            status = int(resp.json().get("status", "0"))
            if status >= 100:
                return
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS
        logger.warning("ZAP spider did not complete within %ds", timeout)

    async def _wait_for_passive_scan(
        self,
        client: httpx.AsyncClient,
        api_url: str,
        base_params: dict[str, str],
        timeout: int,  # noqa: ASYNC109 — why: ZAP passive-scan poll deadline; checked against elapsed inside the loop
    ) -> None:
        """Poll until the passive scan queue is empty or timeout elapses."""
        elapsed = 0.0
        while elapsed < timeout:
            resp = await client.get(
                f"{api_url}/JSON/pscan/view/recordsToScan/",
                params=base_params,
            )
            resp.raise_for_status()
            remaining = int(resp.json().get("recordsToScan", "0"))
            if remaining == 0:
                return
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS
        logger.warning("ZAP passive scan did not complete within %ds", timeout)

    def _alerts_to_findings(self, alerts: list[dict], url: str) -> list[dict]:
        """Convert ZAP alert dicts into standardized findings."""
        findings: list[dict] = []
        seen_ids: set[str] = set()

        for alert in alerts:
            alert_ref = alert.get("pluginId", alert.get("alertRef", ""))
            # De-duplicate by pluginId so we get one finding per rule
            if alert_ref in seen_ids:
                continue
            seen_ids.add(alert_ref)

            risk = int(alert.get("risk", "0"))
            confidence = int(alert.get("confidence", "2"))
            severity = _RISK_TO_SEVERITY.get(risk, "info")
            fp_risk = _CONFIDENCE_TO_FP_RISK.get(confidence, "medium")

            description_parts = [alert.get("description", "")]
            solution = alert.get("solution", "")
            evidence = alert.get("evidence", "") or None

            findings.append(
                make_finding(
                    category="security",
                    severity=severity,
                    title=alert.get("alert", alert.get("name", "Unknown ZAP alert")),
                    description=" ".join(description_parts).strip(),
                    url=alert.get("url", url),
                    recommendation=solution,
                    check_id=f"zap-{alert_ref}",
                    evidence=evidence[:500] if evidence else None,
                    source_tool="zap",
                    false_positive_risk=fp_risk,
                )
            )

        return findings
