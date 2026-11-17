"""Module-level constants, finding helper, and detection utilities."""

from __future__ import annotations

import httpx

from src.domains.critique.services.scanners.base import make_finding

SSRF_BLOCKED_PREFIXES = (
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
    "127.",
    "0.",
    "169.254.",
)

# Common files that should never be publicly accessible
UNIVERSAL_FILE_CHECKS: list[tuple[str, str, str]] = [
    (".env", "sec-exposed-env", "critical"),
    (".git/config", "sec-exposed-git", "critical"),
    (".DS_Store", "sec-exposed-dsstore", "high"),
]

# Platform-specific files — only checked when the platform is detected
PLATFORM_FILE_CHECKS: dict[str, list[tuple[str, str, str]]] = {
    "wordpress": [("wp-config.php", "sec-exposed-wpconfig", "high")],
    "apache": [(".htaccess", "sec-exposed-htaccess", "high")],
}

# Combined list for backward compatibility in tests
EXPOSED_FILE_CHECKS: list[tuple[str, str, str]] = [
    *UNIVERSAL_FILE_CHECKS,
    *[entry for checks in PLATFORM_FILE_CHECKS.values() for entry in checks],
]

# Common directories to check for directory listing
DIRECTORY_LISTING_PATHS = ["/images/", "/uploads/", "/static/", "/assets/"]


def _detect_platform(html: str | None, headers: httpx.Headers | None) -> set[str]:
    """Detect web platform from HTML content and response headers."""
    platforms: set[str] = set()
    if html:
        lower_html = html.lower()
        if any(
            marker in lower_html
            for marker in (
                "/wp-content/",
                "/wp-includes/",
                "wp-json",
                'name="generator" content="wordpress',
            )
        ):
            platforms.add("wordpress")
    if headers:
        powered_by = (headers.get("x-powered-by") or "").lower()
        if "wordpress" in powered_by:
            platforms.add("wordpress")
        server = (headers.get("server") or "").lower()
        if "apache" in server:
            platforms.add("apache")
    return platforms


def _is_false_positive_file(file_path: str, content_type: str, body: str) -> bool:
    """Check if a 200 response for an exposed file is actually a false positive.

    Many sites return 200 with an HTML error page or SPA catch-all for any path.
    """
    ct_lower = content_type.lower()
    is_html = "text/html" in ct_lower

    if file_path in (".env", ".DS_Store", ".htaccess"):
        return is_html

    if file_path == ".git/config":
        return "[core]" not in body

    if file_path == "wp-config.php":
        body_lower = body.lower()
        return "<?php" not in body_lower and "db_name" not in body_lower

    return False


def _sec_finding(
    *,
    severity: str,
    title: str,
    description: str,
    url: str = "",
    recommendation: str = "",
    selector: str | None = None,
    check_id: str | None = None,
    evidence: str | None = None,
    false_positive_risk: str = "low",
    remediation_effort: str = "low",
) -> dict:
    """Security scanner finding with default metadata."""
    return make_finding(
        category="security",
        severity=severity,
        title=title,
        description=description,
        url=url,
        recommendation=recommendation,
        selector=selector,
        check_id=check_id,
        evidence=evidence,
        source_tool="security_scanner",
        false_positive_risk=false_positive_risk,
        remediation_effort=remediation_effort,
    )
