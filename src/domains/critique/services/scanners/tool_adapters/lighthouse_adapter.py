"""Google Lighthouse performance adapter.

Runs the Lighthouse CLI in headless mode and extracts Core Web Vitals
and failed audits as findings.

SSRF note
---------
Unlike the sibling axe adapter, this one does **not** drive a browser in this
process -- it spawns the Lighthouse CLI, which launches a Chromium of its own.
``install_ssrf_guard`` intercepts at the ``BrowserContext`` route layer and so
has no reach into that child at all: it fetches the target, every sub-resource
on it, and every redirect, with nothing in this process in the path.

The containment is therefore pushed into the child itself. The target is
validated and resolved by :func:`resolve_external_scan_target`, and the
resulting address is pinned into the child's resolver with
``--host-resolver-rules``; the trailing ``MAP * ~NOTFOUND`` makes every other
name -- and every bare IP literal -- unresolvable inside that Chromium. See
``src/shared/security/external_process_target.py`` for the measurements behind
that claim and for what it does not cover.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from src.config import get_settings
from src.domains.critique.services.scanners.base import make_finding
from src.shared.security.external_process_target import (
    ExternalScanTarget,
    chromium_host_resolver_rules,
    resolve_external_scan_target,
    scanner_subprocess_env,
    terminate_process_group,
)

logger = logging.getLogger(__name__)

# Core Web Vital audit IDs and human-readable names
_CORE_WEB_VITALS: dict[str, str] = {
    "largest-contentful-paint": "Largest Contentful Paint (LCP)",
    "first-contentful-paint": "First Contentful Paint (FCP)",
    "cumulative-layout-shift": "Cumulative Layout Shift (CLS)",
    "total-blocking-time": "Total Blocking Time (TBT)",
    "interactive": "Time to Interactive (TTI)",
    "speed-index": "Speed Index",
}

# Lighthouse score thresholds  (0.0-1.0) -> severity
_SCORE_THRESHOLDS: list[tuple[float, str]] = [
    (0.50, "critical"),
    (0.75, "high"),
    (0.90, "medium"),
    (1.00, "low"),
]


def _chrome_flags(target: ExternalScanTarget) -> str:
    """Build the ``--chrome-flags`` value confining Lighthouse's own Chromium.

    The resolver-rule value contains spaces, and Lighthouse parses this string
    with ``yargs-parser`` before handing the flags to ``chrome-launcher``
    (verified against lighthouse 13.4.1). Unquoted, the value would truncate at
    the first space to a useless bare ``MAP``; the inner double quotes are what
    keep it whole, and Lighthouse strips them again before spawning Chrome.

    The whole string is deliberately *not* wrapped in outer quotes: Lighthouse
    strips a wrapping quote pair from the entire expression first, which would
    consume ours and re-expose the inner ones.

    Args:
        target: A validated target -- the annotation is load-bearing, see
            ``tests/unit/shared/test_external_process_sinks_guarded.py``.

    Returns:
        The value for ``--chrome-flags=``.
    """
    rules = chromium_host_resolver_rules(target)
    return f'--headless --no-sandbox --disable-gpu --host-resolver-rules="{rules}"'


def _score_to_severity(score: float | None) -> str:
    """Map a Lighthouse 0-1 score to a critique_service severity."""
    if score is None:
        return "info"
    for threshold, severity in _SCORE_THRESHOLDS:
        if score < threshold:
            return severity
    return "info"


class LighthouseAdapter:
    """ToolAdapter that runs Google Lighthouse via CLI."""

    @property
    def tool_name(self) -> str:
        return "lighthouse"

    @property
    def category(self) -> str:
        return "performance"

    async def is_available(self) -> bool:
        """Return True when both node and lighthouse are on PATH and enabled."""
        settings = get_settings()
        if not settings.external_tools.lighthouse_enabled:
            return False
        node_path = shutil.which(settings.external_tools.lighthouse_node_path)
        lh_path = shutil.which(settings.external_tools.lighthouse_cli_path)
        if not node_path or not lh_path:
            logger.debug(
                "lighthouse adapter unavailable: node=%s lighthouse=%s",
                node_path,
                lh_path,
            )
            return False
        return True

    async def scan(self, url: str, html: str | None = None) -> list[dict]:
        """Run Lighthouse CLI and return performance findings."""
        findings: list[dict] = []

        for mode in ("mobile", "desktop"):
            mode_findings = await self._run_lighthouse(url, mode)
            findings.extend(mode_findings)

        return findings

    async def _run_lighthouse(self, url: str, mode: str) -> list[dict]:
        """Execute a single Lighthouse run for the given emulated device mode.

        The target is validated and its address captured *before* the CLI is
        spawned, and the address is pinned into the child Chromium's resolver.
        A target that fails validation yields no findings rather than raising,
        matching the per-adapter isolation contract in ``registry.run_adapters``.
        """
        settings = get_settings()
        timeout = settings.external_tools.lighthouse_timeout
        lh_path = settings.external_tools.lighthouse_cli_path

        try:
            target = await resolve_external_scan_target(url)
        except (ValueError, OSError) as exc:
            logger.warning("lighthouse refusing unsafe target %s (%s): %s", url, mode, exc)
            return []

        # Write JSON output to a temp file to avoid stdout size issues
        tmp_dir = tempfile.mkdtemp(prefix="lh_")
        output_path = str(Path(tmp_dir) / "report.json")

        cmd: list[str] = [
            lh_path,
            target.url,
            "--output=json",
            f"--output-path={output_path}",
            f"--chrome-flags={_chrome_flags(target)}",
            "--quiet",
        ]
        if mode == "desktop":
            cmd.append("--preset=desktop")

        proc = None
        try:
            # ``start_new_session`` puts the CLI -- and the Chromium it launches
            # -- in their own process group, which is what makes the timeout
            # path able to signal the whole tree without signalling ourselves.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=scanner_subprocess_env(),
                start_new_session=True,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            if proc.returncode != 0:
                logger.warning(
                    "lighthouse exited with code %d for %s (%s): %s",
                    proc.returncode,
                    url,
                    mode,
                    stderr.decode(errors="replace")[:500],
                )
                return []

            report_file = Path(output_path)
            if not report_file.exists():
                logger.warning("lighthouse output file not found: %s", output_path)
                return []

            raw = report_file.read_text(encoding="utf-8")
            report = json.loads(raw)

        except TimeoutError:
            # ``wait_for`` cancelled the await, not the child. Reap the tree or
            # every slow target leaks a headless Chromium until the pod OOMs.
            logger.warning("lighthouse timed out after %ds for %s (%s)", timeout, url, mode)
            if proc is not None:
                await terminate_process_group(proc)
            return []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse lighthouse output for %s (%s): %s", url, mode, exc)
            return []
        finally:
            # Clean up temp directory
            try:
                for f in Path(tmp_dir).iterdir():
                    f.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except OSError:
                pass

        return self._parse_report(report, url, mode)

    def _parse_report(self, report: dict, url: str, mode: str) -> list[dict]:
        """Extract findings from a Lighthouse JSON report."""
        findings: list[dict] = []
        audits: dict = report.get("audits", {})
        categories: dict = report.get("categories", {})
        mode_label = mode.capitalize()

        # Overall performance score
        perf_category = categories.get("performance", {})
        perf_score = perf_category.get("score")
        if perf_score is not None and perf_score < 0.90:
            findings.append(
                make_finding(
                    category="performance",
                    severity=_score_to_severity(perf_score),
                    title=f"Lighthouse performance score: {int(perf_score * 100)} ({mode_label})",
                    description=(
                        f"The Lighthouse performance score for {mode_label} emulation "
                        f"is {int(perf_score * 100)}/100, which is below the recommended "
                        f"threshold of 90."
                    ),
                    url=url,
                    recommendation=(
                        "Review the specific audit failures below and address the "
                        "highest-impact items first."
                    ),
                    check_id=f"lh-perf-score-{mode}",
                    evidence=f"score={perf_score}",
                    source_tool="lighthouse",
                )
            )

        # Core Web Vitals
        for audit_id, display_name in _CORE_WEB_VITALS.items():
            audit = audits.get(audit_id)
            if not audit:
                continue
            score = audit.get("score")
            if score is not None and score < 0.90:
                numeric_value = audit.get("numericValue")
                display_value = audit.get("displayValue", "")

                findings.append(
                    make_finding(
                        category="performance",
                        severity=_score_to_severity(score),
                        title=f"{display_name}: {display_value} ({mode_label})",
                        description=(
                            f"{display_name} scored {int(score * 100)}/100 on "
                            f"{mode_label} emulation. "
                            + (
                                f"Measured value: {numeric_value:.0f}ms. "
                                if numeric_value is not None
                                and audit_id != "cumulative-layout-shift"
                                else ""
                            )
                            + audit.get("description", "")
                        ),
                        url=url,
                        recommendation=audit.get(
                            "description",
                            f"Improve {display_name} to meet the recommended threshold.",
                        ),
                        check_id=f"lh-{audit_id}-{mode}",
                        evidence=(
                            f"score={score}, value={numeric_value}"
                            if numeric_value is not None
                            else f"score={score}"
                        ),
                        source_tool="lighthouse",
                    )
                )

        # Other failed audits from the performance category
        perf_audit_refs: list[dict] = perf_category.get("auditRefs", [])
        cwv_ids = set(_CORE_WEB_VITALS.keys())
        for ref in perf_audit_refs:
            audit_id = ref.get("id", "")
            if audit_id in cwv_ids:
                continue  # Already reported above
            audit = audits.get(audit_id)
            if not audit:
                continue
            score = audit.get("score")
            if score is not None and score < 0.50:
                findings.append(
                    make_finding(
                        category="performance",
                        severity=_score_to_severity(score),
                        title=f"{audit.get('title', audit_id)} ({mode_label})",
                        description=audit.get("description", ""),
                        url=url,
                        recommendation=audit.get(
                            "description",
                            "Review this Lighthouse audit for improvement opportunities.",
                        ),
                        check_id=f"lh-{audit_id}-{mode}",
                        evidence=audit.get("displayValue"),
                        source_tool="lighthouse",
                    )
                )

        return findings
