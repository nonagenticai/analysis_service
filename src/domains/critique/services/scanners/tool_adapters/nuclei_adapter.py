"""ProjectDiscovery Nuclei security adapter.

Runs the ``nuclei`` CLI in safe mode against a target URL and parses
the JSONL output into standardized findings.

SSRF note
---------
nuclei is an external process with its own HTTP client, so
``install_ssrf_guard`` -- which intercepts at the Playwright
``BrowserContext`` route layer -- cannot see any request it makes.

:func:`resolve_external_scan_target` validates and resolves the target before
the binary is spawned, which closes **hop 0 only**. Unlike the Lighthouse
adapter there is no verified lever for constraining the child further: nuclei
is a Go binary with no resolver-pinning flag equivalent to Chromium's
``--host-resolver-rules``. It does not follow redirects unless a template opts
in, but a template that does is not contained by anything in this repo. The
complete answer for this adapter is egress containment at the network layer
(a the fleet deployment configuration NetworkPolicy / egress proxy), which is why
``scanner_subprocess_env`` deliberately preserves ``*_PROXY`` variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

from src.config import get_settings
from src.domains.critique.services.scanners.base import make_finding
from src.shared.security.external_process_target import (
    resolve_external_scan_target,
    scanner_subprocess_env,
    terminate_process_group,
)

logger = logging.getLogger(__name__)

# Nuclei severity string -> critique_service severity
_SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "info",
    "unknown": "info",
}


class NucleiAdapter:
    """ToolAdapter that runs ProjectDiscovery Nuclei in safe mode."""

    @property
    def tool_name(self) -> str:
        return "nuclei"

    @property
    def category(self) -> str:
        return "security"

    async def is_available(self) -> bool:
        """Return True when the nuclei binary is on PATH and enabled."""
        settings = get_settings()
        if not settings.external_tools.nuclei_enabled:
            return False
        nuclei_path = shutil.which(settings.external_tools.nuclei_path)
        if not nuclei_path:
            logger.debug("nuclei binary not found on PATH")
            return False
        return True

    async def scan(self, url: str, html: str | None = None) -> list[dict]:
        """Run nuclei with safe tags and return findings from JSONL output."""
        settings = get_settings()
        timeout = settings.external_tools.nuclei_timeout
        nuclei_bin = settings.external_tools.nuclei_path

        try:
            target = await resolve_external_scan_target(url)
        except (ValueError, OSError) as exc:
            logger.warning("nuclei refusing unsafe target %s: %s", url, exc)
            return []

        cmd: list[str] = [
            nuclei_bin,
            "-u",
            target.url,
            "-jsonl",
            "-silent",
            "-tags",
            "safe",
            "-no-color",
        ]

        proc = None
        try:
            # Own process group: nuclei runs a worker pool, and the timeout path
            # must be able to signal the whole tree, not just the parent.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=scanner_subprocess_env(),
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            logger.warning("nuclei timed out after %ds for %s", timeout, url)
            if proc is not None:
                await terminate_process_group(proc)
            return []
        except (OSError, FileNotFoundError) as exc:
            logger.warning("Failed to launch nuclei for %s: %s", url, exc)
            return []

        if proc.returncode not in (0, 1):
            # nuclei returns 1 when it finds results, 0 when clean
            logger.warning(
                "nuclei exited with code %d for %s: %s",
                proc.returncode,
                url,
                stderr.decode(errors="replace")[:500],
            )

        return self._parse_jsonl(stdout.decode(errors="replace"), url)

    @staticmethod
    def _description_from_entry(info: dict, matcher_name: str, fallback: str) -> str:
        parts: list[str] = []
        if info.get("description"):
            parts.append(info["description"])
        if matcher_name:
            parts.append(f"Matched: {matcher_name}.")
        return " ".join(parts) if parts else fallback

    @staticmethod
    def _recommendation_from_entry(info: dict) -> str:
        if info.get("remediation"):
            return info["remediation"]
        reference = info.get("reference")
        if isinstance(reference, list) and reference:
            return f"See: {reference[0]}"
        if isinstance(reference, str) and reference:
            return f"See: {reference}"
        return ""

    @staticmethod
    def _evidence_from_entry(entry: dict) -> str | None:
        extracted: list[str] = entry.get("extracted-results", [])
        if extracted:
            return ", ".join(extracted)[:500]
        curl_cmd = entry.get("curl-command", "")
        return curl_cmd[:500] if curl_cmd else None

    def _finding_from_entry(self, entry: dict, url: str) -> dict:
        info: dict = entry.get("info", {})
        severity = _SEVERITY_MAP.get(info.get("severity", "unknown").lower(), "info")
        template_id = entry.get("template-id", entry.get("templateID", "unknown"))
        matched_at = entry.get("matched-at", entry.get("matched", url))
        matcher_name = entry.get("matcher-name", entry.get("matcher_name", ""))
        title = info.get("name", template_id)
        fp_risk = "low" if info.get("classification", {}).get("cve-id") else "medium"
        return make_finding(
            category="security",
            severity=severity,
            title=title,
            description=self._description_from_entry(info, matcher_name, title),
            url=matched_at,
            recommendation=self._recommendation_from_entry(info),
            check_id=f"nuclei-{template_id}",
            evidence=self._evidence_from_entry(entry),
            source_tool="nuclei",
            false_positive_risk=fp_risk,
        )

    def _parse_jsonl(self, raw: str, url: str) -> list[dict]:
        """Parse nuclei JSONL output into standardized findings."""
        findings: list[dict] = []
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON nuclei output line: %s", line[:120])
                continue
            findings.append(self._finding_from_entry(entry, url))
        return findings
