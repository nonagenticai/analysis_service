"""Top-level ``SecurityScanner`` coordinator class."""

from __future__ import annotations

import asyncio
import logging

import httpx
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.security_scanner.header_checks import (
    HeaderChecksMixin,
)
from src.domains.critique.services.scanners.security_scanner.helpers import _sec_finding
from src.domains.critique.services.scanners.security_scanner.html_checks import HtmlChecksMixin
from src.domains.critique.services.scanners.security_scanner.http_guard import (
    ScannerSsrfBlockedError,
    guarded_get,
)
from src.domains.critique.services.scanners.security_scanner.network_checks import (
    NetworkChecksMixin,
    _is_ssrf_safe_resolved,
)
from src.domains.critique.services.scanners.security_scanner.tls_checks import TLSChecksMixin
from src.shared.concurrency import bounded_slot

logger = logging.getLogger(__name__)

# Ceiling on concurrent ``_check_tls`` probes.
#
# ``asyncio.to_thread`` dispatches into the loop's **default** executor, which
# is process-wide and shared with every other ``to_thread`` caller in this
# service (the Playwright SSRF guard's off-thread ``getaddrinfo``, among
# others). Its worker count is a CPython default sized for incidental
# offloading, not for a fan-out. ``_check_tls`` holds a worker for the duration
# of a raw blocking TLS handshake against a tenant-supplied host -- up to the
# connect timeout when that host black-holes -- so N concurrent critique jobs
# park N workers on unreachable origins and starve every unrelated ``to_thread``
# in the pod. Off-loading the socket stopped it blocking the event loop; it
# moved the exhaustion into the executor rather than removing it.
#
# The bound is small and deliberate: these threads are almost entirely idle in
# a syscall, so this is sized to leave the shared executor usable, not to
# maximise scan throughput.
MAX_CONCURRENT_TLS_PROBES = 8

# Registry key for the gate above. Distinct from the Chromium gate's -- the two
# ration different resources and must not share a counter.
_TLS_GATE = "tls-probe"


class SecurityScanner(
    HeaderChecksMixin,
    TLSChecksMixin,
    HtmlChecksMixin,
    NetworkChecksMixin,
):
    """Scans websites for security issues via HTTP requests."""

    async def scan_static(
        self,
        html: str,
        *,
        soup: BeautifulSoup | None = None,
        url: str = "",
        site_analysis: dict | None = None,
    ) -> list[dict]:
        """Scan pre-rendered HTML for the security issues detectable without a
        live origin. Makes **no** outbound request — runs only the six HTML-only
        checks (mixed-content, HTML secrets, SRI, meta-refresh redirect,
        external/insecure form actions). Used when scoring a stored artifact;
        the live checks (headers/TLS/CORS/exposed-files/redirects) are skipped
        and surfaced as ``security_live`` in the job's ``progress``.
        """
        self._site_analysis = site_analysis
        if soup is None:
            soup = BeautifulSoup(html, "html.parser")
        findings: list[dict] = []
        findings.extend(self._check_mixed_content(soup, url))
        findings.extend(self._check_html_secrets(html, url))
        findings.extend(self._check_sri(soup, url))
        findings.extend(self._check_meta_refresh_redirect(soup, url))
        findings.extend(self._check_form_action_external(soup, url))
        findings.extend(self._check_form_insecure(soup, url))
        return findings

    async def _run_async_checks(
        self, url: str, html: str | None, headers: httpx.Headers
    ) -> list[dict]:
        """Run the four network sub-checks concurrently, recording any failure.

        Args:
            url: The URL being scanned.
            html: Pre-fetched HTML, forwarded to the exposed-files check.
            headers: Response headers from the entry fetch.

        Returns:
            Every finding the sub-checks produced, plus one
            ``sec-check-errored`` finding per check that did not complete.
        """
        findings: list[dict] = []
        # Async checks — run in parallel.
        #
        # ``return_exceptions=True`` is load-bearing, not defensive tidiness.
        # Without it ``gather`` propagates the FIRST exception and cancels
        # nothing else usefully: one flaky origin escaping one sub-check
        # killed all four, and the escape then landed in ``scan``'s outer
        # handler, which appends a single generic finding. The job still
        # finalized and SCORED -- a site that reset the connection was
        # scored as though it had no network-layer security findings at all.
        #
        # A check that COULD NOT RUN must be visibly distinct from a check
        # that ran and found nothing, so each failure is recorded as its own
        # ``sec-check-errored`` finding naming the check. Silently
        # substituting an empty list would reproduce the original defect with
        # better manners.
        check_names = (
            "exposed_files",
            "cors",
            "directory_listing",
            "redirect_chain",
        )
        results = await asyncio.gather(
            self._check_exposed_files(url, html=html, headers=headers),
            self._check_cors(url),
            self._check_directory_listing(url),
            self._check_redirect_chain(url),
            return_exceptions=True,
        )
        for check_name, result in zip(check_names, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    # ``CancelledError`` and friends are control flow, not a
                    # scan outcome; they must not be reported as a finding.
                    raise result
                logger.warning(
                    "Security sub-check %s failed for %s: %s: %s",
                    check_name,
                    url,
                    type(result).__name__,
                    result,
                )
                findings.append(
                    _sec_finding(
                        severity="info",
                        title=f"Security check could not complete ({check_name})",
                        description=(
                            f"The {check_name} check did not run to completion for"
                            f" {url}, so this scan carries no result for it. This is"
                            " NOT evidence that the check found nothing."
                        ),
                        url=url,
                        recommendation=(
                            "Re-run the scan; if it keeps failing, the origin is"
                            " likely unreachable or resetting connections."
                        ),
                        check_id="sec-check-errored",
                        evidence=f"{check_name}: {type(result).__name__}: {result}",
                    )
                )
                continue
            findings.extend(result)

        return findings

    async def scan(
        self,
        url: str,
        html: str | None = None,
        *,
        soup: BeautifulSoup | None = None,
        site_analysis: dict | None = None,
    ) -> list[dict]:
        """Scan URL for security issues. Makes HTTP requests.

        Args:
            url: The URL to scan.
            html: Optional pre-fetched HTML content for mixed-content analysis.
            soup: Optional pre-parsed BeautifulSoup tree to avoid redundant parsing.
            site_analysis: Optional pre-computed site analysis with tech stack info.
        """
        self._site_analysis = site_analysis
        if not await _is_ssrf_safe_resolved(url):
            logger.warning("Blocked SSRF attempt to %s", url)
            return [
                _sec_finding(
                    severity="info",
                    title="Scan skipped (internal URL)",
                    description=f"Security scan skipped for internal/private URL: {url}",
                    url=url,
                    check_id="sec-ssrf-blocked",
                )
            ]

        findings: list[dict] = []
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=False,
                verify=True,
            ) as client:
                # The entry check above screened the URL the caller handed us;
                # it says nothing about where that origin redirects to. Walking
                # the hops through ``guarded_get`` re-screens each one, so a
                # public host cannot ``302`` this scan onto the tailnet.
                response = await guarded_get(client, url)
                headers = response.headers

                findings.extend(self._check_security_headers(headers, url))
                findings.extend(self._check_cookie_flags(response, url))
                findings.extend(self._check_server_header(headers, url))
                # ``_check_tls`` opens a raw blocking socket; off-loading it
                # keeps a black-holed origin from stalling the event loop (and
                # every other concurrent scan) for the connect timeout. The slot
                # is held across the ``to_thread`` await, not merely acquired
                # before it, so the ceiling counts probes that are *resident* in
                # the shared default executor rather than probes that have been
                # started. See ``MAX_CONCURRENT_TLS_PROBES`` above.
                async with bounded_slot(_TLS_GATE, MAX_CONCURRENT_TLS_PROBES):
                    findings.extend(await asyncio.to_thread(self._check_tls, url))
                findings.extend(self._check_csp_details(headers, url))
                findings.extend(self._check_technology_disclosure(headers, url))
                findings.extend(self._check_hsts_quality(headers, url))

            # Header-based cross-origin and CSP reporting checks
            findings.extend(self._check_csp_reporting(headers, url))
            findings.extend(self._check_corp(headers, url))
            findings.extend(self._check_coop(headers, url))
            findings.extend(self._check_coep(headers, url))
            findings.extend(self._check_dns_prefetch_control(headers, url))

            # Check mixed content if HTML was provided
            if html is not None:
                if soup is None:
                    soup = BeautifulSoup(html, "html.parser")
                findings.extend(self._check_mixed_content(soup, url))
                findings.extend(self._check_html_secrets(html, url))
                findings.extend(self._check_sri(soup, url))
                findings.extend(self._check_meta_refresh_redirect(soup, url))
                findings.extend(self._check_form_action_external(soup, url))
                findings.extend(self._check_form_insecure(soup, url))

            findings.extend(await self._run_async_checks(url, html, headers))

        except ScannerSsrfBlockedError as e:
            # Must precede the ``OSError`` clause below, which would otherwise
            # swallow this and mislabel a refused target as a scan error. Only
            # the top-level fetch can reach here: the four sub-checks catch
            # their own block and degrade to "no finding".
            logger.warning("Blocked SSRF attempt during scan of %s: %s", url, e)
            findings.append(
                _sec_finding(
                    severity="info",
                    title="Scan stopped (redirect to internal URL)",
                    description=(
                        f"Security scan of {url} stopped because a redirect led to an"
                        f" internal/private URL: {e}"
                    ),
                    url=url,
                    check_id="sec-ssrf-blocked",
                )
            )
        except httpx.ConnectError as e:
            findings.append(
                _sec_finding(
                    severity="info",
                    title="Connection failed",
                    description=f"Could not connect to {url}: {e}",
                    url=url,
                    check_id="sec-connection-failed",
                )
            )
        except httpx.TimeoutException:
            findings.append(
                _sec_finding(
                    severity="info",
                    title="Request timed out",
                    description=f"Security scan timed out for {url}",
                    url=url,
                    check_id="sec-timeout",
                )
            )
        # ``httpx.HTTPError`` is load-bearing and must stay. The two arms above name
        # only two LEAVES of the httpx tree; every other failure of ``guarded_get``
        # -- ``RemoteProtocolError`` (server disconnected without a response),
        # ``ReadError``/``WriteError``, ``ProxyError``, ``DecodingError`` -- derives
        # from ``RequestError -> HTTPError -> Exception`` and from NO builtin here,
        # so it escaped ``scan``. Scanning arbitrary customer origins makes those
        # routine. ``CritiquePhases._phase_run_scanners`` gathers with
        # ``return_exceptions=True``, so the escape did not crash the run: the
        # security scanner silently contributed ZERO findings and the job still
        # finalized and SCORED -- a site that reset the connection was scored as if
        # it had no security findings. The sibling ``network_checks.py`` handlers
        # already name the base class; this one did not.
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
            logger.error("Security scan error for %s: %s", url, e)
            findings.append(
                _sec_finding(
                    severity="info",
                    title="Scan error",
                    description=f"Security scan encountered an error: {e}",
                    url=url,
                    check_id="sec-scan-error",
                )
            )

        return findings
