"""INP-related performance checks (scripts, third-party scripts, sync XHR, document.write)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.perf.base import _perf_finding


def check_scripts(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    scripts = soup.find_all("script")
    external = [s for s in scripts if isinstance(s, Tag) and s.get("src")]

    if len(external) > 15:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{len(external)} external scripts",
                description=(f"Loading {len(external)} external JavaScript files."),
                url=url,
                recommendation=("Bundle scripts to reduce HTTP requests. Consider code splitting."),
                selector="script[src]",
                check_id="perf-excessive-scripts",
                remediation_effort="medium",
            )
        )

    inline = [s for s in scripts if isinstance(s, Tag) and not s.get("src")]
    total_inline = sum(len(s.string or "") for s in inline)
    if total_inline > 100000:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"Large inline JavaScript: {total_inline // 1000}KB",
                description=f"Inline scripts total {total_inline:,} characters.",
                url=url,
                recommendation=("Move large inline scripts to external files for caching."),
                selector="script:not([src])",
                check_id="perf-large-inline-js",
                remediation_effort="medium",
            )
        )

    return findings


def check_third_party_scripts(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for excessive third-party scripts."""
    findings: list[dict] = []
    parsed_url = urlparse(url)
    page_host = parsed_url.hostname or ""

    third_party: list[str] = []
    for script in soup.find_all("script", src=True):
        if not isinstance(script, Tag):
            continue
        src = str(script.get("src", ""))
        if src.startswith("http"):
            src_host = urlparse(src).hostname or ""
            if src_host and src_host != page_host:
                third_party.append(src)

    if len(third_party) > 10:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{len(third_party)} third-party scripts loaded",
                description=(
                    f"{len(third_party)} scripts are loaded from "
                    "third-party domains. Excessive third-party scripts "
                    "increase page load time and create single points "
                    "of failure."
                ),
                url=url,
                recommendation=(
                    "Audit third-party scripts and remove unnecessary "
                    "ones. Consider self-hosting critical libraries and "
                    "loading non-essential scripts asynchronously."
                ),
                selector="script[src]",
                check_id="perf-excessive-third-party",
                remediation_effort="medium",
            )
        )
    return findings


def check_sync_xhr(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag synchronous XMLHttpRequest calls in inline scripts."""
    findings: list[dict] = []
    # Pattern: .open("METHOD", url, false)  -- third arg is false
    sync_xhr_re = re.compile(
        r"XMLHttpRequest.*?\.open\s*\([^)]*,\s*[^)]*,\s*false\s*\)",
        re.DOTALL,
    )

    for script in soup.find_all("script"):
        if not isinstance(script, Tag) or script.get("src"):
            continue
        content = script.string or ""
        if sync_xhr_re.search(content):
            findings.append(
                _perf_finding(
                    severity="medium",
                    title="Synchronous XMLHttpRequest detected",
                    description=(
                        "An inline script uses synchronous "
                        "XMLHttpRequest (the third argument to open() "
                        "is false). Synchronous XHR blocks the main "
                        "thread and freezes the UI until the request "
                        "completes."
                    ),
                    url=url,
                    recommendation=(
                        "Use asynchronous XMLHttpRequest (omit the "
                        "third argument or set it to true), or migrate "
                        "to the Fetch API."
                    ),
                    selector="script",
                    check_id="perf-sync-xhr",
                    remediation_effort="medium",
                )
            )
            break  # Report once
    return findings


def check_document_write(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag inline scripts that use document.write()."""
    findings: list[dict] = []
    doc_write_re = re.compile(r"document\.write\s*\(")

    for script in soup.find_all("script"):
        if not isinstance(script, Tag) or script.get("src"):
            continue
        content = script.string or ""
        if doc_write_re.search(content):
            findings.append(
                _perf_finding(
                    severity="high",
                    title="document.write() detected in inline script",
                    description=(
                        "An inline script uses document.write(). "
                        "In modern browsers, document.write() can be "
                        "blocked on slow connections, causes full page "
                        "re-parsing, and is a significant performance "
                        "anti-pattern."
                    ),
                    url=url,
                    recommendation=(
                        "Replace document.write() with DOM manipulation "
                        "methods such as document.createElement(), "
                        "appendChild(), or insertAdjacentHTML()."
                    ),
                    selector="script",
                    check_id="perf-document-write",
                    remediation_effort="medium",
                )
            )
            break  # Report once
    return findings
