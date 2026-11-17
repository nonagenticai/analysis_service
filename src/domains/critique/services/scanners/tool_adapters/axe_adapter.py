"""Axe-core accessibility adapter.

Launches headless Chromium via Playwright, injects axe-core, and runs
WCAG 2.1 AA checks against the target URL.
"""

from __future__ import annotations

import logging

from src.config import get_settings
from src.domains.critique.services.scanners.base import make_finding
from src.domains.critique.services.scanners.tool_adapters.protocol import AdapterScanError
from src.shared.security.browser_gate import browser_slot
from src.shared.security.playwright_guard import install_ssrf_guard

logger = logging.getLogger(__name__)

# axe-core impact -> critique_service severity
_IMPACT_TO_SEVERITY: dict[str, str] = {
    "critical": "critical",
    "serious": "high",
    "moderate": "medium",
    "minor": "low",
}

# JavaScript snippet that fetches the axe-core library from a CDN, injects it
# into the page, and runs it with the WCAG 2.1 AA tag set.
#
# This snippet is the reason the BrowserContext route guard is not optional
# here: it deliberately makes the page issue a further request of its own, from
# inside the pod network, after the entry URL has been cleared. The guard sees
# and judges that request (``cdnjs.cloudflare.com`` resolves to public space, so
# it is allowed and axe still loads -- verified against a live Chromium, guarded
# and unguarded, with identical axe output). Every *other* request the tenant's
# page provokes -- ``<img>``, XHR, redirect target -- is judged the same way.
#
# The context is created with ``bypass_csp=True``. A target whose own CSP is
# ``script-src 'self'`` (nonagentic.ai, and any well-hardened site) otherwise
# blocks this injected ``<script>``, ``onerror`` fires, and the scan fails --
# reproduced against live nonagentic.ai 2026-10-18: ``bypass_csp=False`` ->
# ``Page.evaluate: Event``; ``bypass_csp=True`` -> axe runs. Bypassing the
# page's CSP does NOT bypass the SSRF route guard, which judges every request
# at the BrowserContext regardless of CSP.
_AXE_INJECT_AND_RUN = """
async () => {
    if (!window.axe) {
        const script = document.createElement('script');
        script.src = 'https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js';
        await new Promise((resolve, reject) => {
            script.onload = resolve;
            // Reject with an Error, never the DOM Event itself: Playwright
            // serialises a rejected Event as the bare string "Event", which is
            // all the 2026-10-18 live log ever said about this failure.
            script.onerror = () => reject(new Error('axe-core script failed to load from ' + script.src));
            (document.head || document.documentElement).appendChild(script);
        });
    }
    return await window.axe.run(document, {
        runOnly: {
            type: 'tag',
            values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
        }
    });
}
"""


class AxeAdapter:
    """ToolAdapter that runs axe-core accessibility checks via Playwright."""

    @property
    def tool_name(self) -> str:
        return "axe-core"

    @property
    def category(self) -> str:
        return "accessibility"

    async def is_available(self) -> bool:
        """Return True when Playwright is importable and axe is enabled in config."""
        settings = get_settings()
        if not settings.external_tools.axe_enabled:
            return False
        try:
            import playwright.async_api  # noqa: F401

            return True
        except ImportError:
            logger.debug("playwright is not installed; axe-core adapter unavailable")
            return False

    async def scan(self, url: str, html: str | None = None) -> list[dict]:
        """Launch headless Chromium, inject axe-core, and return findings.

        Raises:
            AdapterScanError: the scan could not run (no Playwright, browser
                launch / navigation / injection failure, malformed result).
                ``[]`` is returned ONLY when axe ran and found no violations.
        """
        settings = get_settings()
        timeout_ms = settings.external_tools.axe_timeout * 1000

        try:
            from playwright.async_api import (
                Error as PlaywrightError,
            )
            from playwright.async_api import (
                TimeoutError as PlaywrightTimeoutError,
            )
            from playwright.async_api import (
                async_playwright,
            )
        except ImportError:
            raise AdapterScanError("playwright is not installed") from None

        findings: list[dict] = []

        try:
            # Browser-concurrency gate, held across the browser's whole lifetime.
            # The pod's memory budget is shared, so the ceiling has to be too:
            # this adapter and the brand pipeline both launch Chromium, and
            # gating only one of them leaves the pod exactly as OOM-able as
            # before. See ``src/shared/security/browser_gate.py``.
            async with browser_slot(), async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(bypass_csp=True)
                    # SSRF gate, installed on the CONTEXT and BEFORE any page
                    # exists. ``url`` is tenant-supplied and only ever screened
                    # by ``validate_callback_url`` in the sibling ``registry``,
                    # which is a hop-0 structural check: it cannot see the
                    # sub-resources, in-page fetches or 30x redirects the
                    # browser goes on to issue by itself. Installing after the
                    # first navigation would miss that navigation, so this must
                    # stay above ``new_page``.
                    #
                    # The entry check in ``registry`` stays: ``file://`` and
                    # ``chrome://`` never touch the network stack, so a route
                    # handler is never consulted for them. The two are
                    # complementary; neither alone is sufficient.
                    await install_ssrf_guard(context)
                    page = await context.new_page()
                    page.set_default_timeout(timeout_ms)

                    await page.goto(url, wait_until="load", timeout=timeout_ms)

                    # Inject axe-core and run WCAG 2.1 AA audit
                    axe_results = await page.evaluate(_AXE_INJECT_AND_RUN)
                    if not isinstance(axe_results, dict) or not isinstance(
                        axe_results.get("violations"), list
                    ):
                        # A result without a violations list is not "0
                        # violations" -- it is no measurement at all.
                        raise AdapterScanError(
                            f"axe-core returned no violations list ({type(axe_results).__name__})"
                        )

                    violations: list[dict] = axe_results["violations"]
                    for violation in violations:
                        impact = violation.get("impact", "minor")
                        severity = _IMPACT_TO_SEVERITY.get(impact, "low")

                        # Extract WCAG criterion tags (e.g. "wcag111")
                        wcag_criterion = _extract_wcag_criterion(violation.get("tags", []))

                        # Each violation can affect multiple DOM nodes
                        nodes: list[dict] = violation.get("nodes", [])
                        selectors = [_first_selector(node) for node in nodes]
                        selector = selectors[0] if selectors else None

                        node_count = len(nodes)
                        title = violation.get("help", violation.get("id", "Unknown"))
                        description_parts = [
                            violation.get("description", ""),
                        ]
                        if node_count > 1:
                            description_parts.append(
                                f"Affects {node_count} element(s) on the page."
                            )

                        findings.append(
                            make_finding(
                                category="accessibility",
                                severity=severity,
                                title=title,
                                description=" ".join(description_parts),
                                url=url,
                                recommendation=violation.get(
                                    "helpUrl", "See axe-core documentation."
                                ),
                                selector=selector,
                                check_id=f"axe-{violation.get('id', 'unknown')}",
                                wcag_criterion=wcag_criterion,
                                source_tool="axe-core",
                            )
                        )
                finally:
                    await browser.close()

        # ``PlaywrightError``/``PlaywrightTimeoutError`` are listed because this
        # block drives a real browser (launch/goto/evaluate) and Playwright's
        # exceptions derive from ``Exception`` only -- none of the builtins
        # below catch them. In particular Playwright's ``TimeoutError`` is a
        # DIFFERENT CLASS that merely shares its name with the builtin, so the
        # builtin ``TimeoutError`` (via ``OSError``) does NOT catch a navigation
        # timeout. Without these two names a browser-launch failure or a slow
        # target escapes ``scan`` and kills the whole critique run instead of
        # skipping one accessibility scan.
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            RuntimeError,
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as exc:
            logger.warning("axe-core scan failed for %s: %s", url, exc)
            # Re-raised, not swallowed: returning ``[]`` here reported a failed
            # scan as a clean page. The registry isolates it per adapter and
            # records ``status="failed"`` -- the run still continues.
            raise AdapterScanError(f"{type(exc).__name__}: {exc}") from exc

        return findings


def _extract_wcag_criterion(tags: list[str]) -> str | None:
    """Parse axe-core tags like 'wcag111' into dotted notation '1.1.1'."""
    for tag in tags:
        if tag.startswith("wcag") and len(tag) >= 7:
            digits = tag[4:]
            if digits.isdigit() and len(digits) >= 3:
                # e.g. "111" -> "1.1.1", "143" -> "1.4.3"
                return ".".join(digits)
    return None


def _first_selector(node: dict) -> str | None:
    """Return the first CSS selector string from an axe node result."""
    target = node.get("target", [])
    if target and isinstance(target, list):
        first = target[0]
        if isinstance(first, list):
            return " ".join(first)
        return str(first)
    return None
