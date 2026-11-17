"""Network-roundtrip security checks (exposed files, CORS, dir listing, redirects)."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin, urlparse

import httpx

from src.domains.critique.services.scanners.security_scanner.helpers import (
    DIRECTORY_LISTING_PATHS,
    PLATFORM_FILE_CHECKS,
    UNIVERSAL_FILE_CHECKS,
    _detect_platform,
    _is_false_positive_file,
    _sec_finding,
)
from src.domains.critique.services.scanners.security_scanner.http_guard import guarded_get


def _is_ssrf_safe(url: str) -> bool:
    """Lazy proxy to the package-level ``_is_ssrf_safe``.

    Defined as a function (not a top-level import) to avoid a circular
    import: this module is loaded *during* the package ``__init__`` execution
    (before ``_is_ssrf_safe`` is bound on the package namespace).
    """
    from src.domains.critique.services.scanners import security_scanner as _pkg

    return _pkg._is_ssrf_safe(url)


async def _is_ssrf_safe_resolved(url: str) -> bool:
    """Lazy proxy to the package-level ``_is_ssrf_safe_resolved``.

    Same circular-import deferral as :func:`_is_ssrf_safe`. This is the form the
    fetching checks must use: the structural screen inspects the URL *string*,
    so a public name whose ``A`` record points at ``100.64.0.5`` passed it,
    and the ``Location`` re-check below inherited exactly the same blindness.

    Args:
        url: The URL about to be dereferenced.

    Returns:
        ``True`` if the URL is safe to fetch.
    """
    from src.domains.critique.services.scanners import security_scanner as _pkg

    return await _pkg._is_ssrf_safe_resolved(url)


logger = logging.getLogger(__name__)


class NetworkChecksMixin:
    """Mixin for HTTP-roundtrip security checks used by ``SecurityScanner``."""

    def _augment_platforms_from_site_analysis(self, platforms: set[str]) -> None:
        """Augment ``platforms`` set in-place using ``self._site_analysis`` tech_stack."""
        site_analysis = getattr(self, "_site_analysis", None)
        if not isinstance(site_analysis, dict):
            return
        tech_stack = site_analysis.get("tech_stack", {})
        if isinstance(tech_stack, dict):
            tech_str = " ".join(str(v) for v in tech_stack.values()).lower()
        elif isinstance(tech_stack, str):
            tech_str = tech_stack.lower()
        else:
            tech_str = ""
        if "wordpress" in tech_str:
            platforms.add("wordpress")
        if "apache" in tech_str:
            platforms.add("apache")

    @staticmethod
    def _files_to_check_for(platforms: set[str]) -> list:
        files = list(UNIVERSAL_FILE_CHECKS)
        for platform, checks in PLATFORM_FILE_CHECKS.items():
            if platform in platforms:
                files.extend(checks)
        return files

    @staticmethod
    async def _probe_exposed_file(
        base_url: str,
        file_path: str,
        check_id: str,
        severity: str,
        semaphore: asyncio.Semaphore,
    ) -> dict | None:
        """Probe one candidate sensitive-file URL under the shared concurrency cap.

        ``file_url`` is *derived* -- ``urljoin`` rebuilds it from the scanned
        origin plus a path out of the check table -- so it is not the string
        ``SecurityScanner.scan`` screened on the way in, and it gets screened
        again here via :func:`guarded_get`. The hop budget is ``0``: this check
        has always declined to follow redirects (a ``30x`` is not a ``200`` and
        yields no finding), and that stays true.

        Args:
            base_url: Scheme and netloc of the site being scanned.
            file_path: Relative path of the sensitive file to probe.
            check_id: Finding identifier to emit on a hit.
            severity: Severity to emit on a hit.
            semaphore: Caps how many probes run against the origin at once.

        Returns:
            A finding when the file is exposed, otherwise ``None``. Blocked and
            failed probes both return ``None`` -- this check is fail-soft.
        """
        file_url = urljoin(base_url + "/", file_path)
        async with semaphore:
            try:
                async with httpx.AsyncClient(
                    timeout=5.0, follow_redirects=False, verify=True
                ) as client:
                    resp = await guarded_get(client, file_url, max_redirects=0)
                    # Reading the response is part of the probe, not
                    # bookkeeping after it. ``resp.text`` decodes the body and
                    # can raise (``DecodingError`` on a lying
                    # ``Content-Encoding``, a codec error on a mislabelled
                    # charset), and ``_is_false_positive_file`` parses that
                    # body. These three lines used to sit OUTSIDE this ``try``:
                    # one such raise escaped ``_probe_exposed_file``, and the
                    # caller's bare ``gather`` then cancelled every sibling
                    # probe, so a single malformed response erased the whole
                    # exposed-files family. Keeping the read inside the guard
                    # preserves the documented per-probe fail-soft contract: a
                    # probe that could not complete yields ``None`` and its
                    # siblings still report.
                    if resp.status_code != 200:
                        return None
                    if _is_false_positive_file(
                        file_path, resp.headers.get("content-type", ""), resp.text
                    ):
                        return None
            except (
                ValueError,
                TypeError,
                KeyError,
                OSError,
                RuntimeError,
                httpx.HTTPError,
            ) as exc:
                logger.warning("Failed to check exposed file %s: %s", file_path, exc)
                return None
        return _sec_finding(
            severity=severity,
            title=f"Exposed sensitive file: {file_path}",
            description=(
                f"The file '{file_path}' is publicly accessible at"
                f" {file_url}. This may expose sensitive configuration"
                " data."
            ),
            url=file_url,
            recommendation=(f"Block public access to '{file_path}' via server configuration."),
            check_id=check_id,
            remediation_effort="medium",
        )

    async def _check_exposed_files(
        self,
        url: str,
        html: str | None = None,
        headers: httpx.Headers | None = None,
    ) -> list[dict]:
        """Check for commonly exposed sensitive files."""
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        semaphore = asyncio.Semaphore(5)

        platforms = _detect_platform(html, headers)
        self._augment_platforms_from_site_analysis(platforms)

        tasks = [
            self._probe_exposed_file(base_url, file_path, check_id, severity, semaphore)
            for file_path, check_id, severity in self._files_to_check_for(platforms)
        ]
        # ``return_exceptions=True`` is load-bearing. Without it, one probe
        # that raises propagates immediately and cancels every sibling probe,
        # so a single bad response costs the entire exposed-files family --
        # and, because this coroutine then escapes into the caller's gather,
        # it degrades the other three network check families too.
        #
        # The kwarg ALONE would be worse than the bug: an exception object is
        # truthy and is not ``None``, so it would sail through the filter
        # below and be returned as though it were a finding dict. Every result
        # is therefore ``isinstance``-filtered, not just null-checked.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        findings: list[dict] = []
        for result in results:
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    # ``CancelledError`` and friends are control flow, not a
                    # probe outcome; they must not be swallowed.
                    raise result
                # NOTE, deliberate and worth knowing before you change it: a
                # dropped probe is recorded ONLY in this log line -- unlike
                # ``_run_async_checks``, which emits a ``sec-check-errored``
                # finding for every sub-check that did not complete. This is
                # the one place the estate's "a check that could not run must
                # be visibly distinct from a check that found nothing" rule is
                # honoured by a log rather than by a finding.
                #
                # It is defensible because the unit differs: a sub-check is a
                # whole family and its absence silently changes the score,
                # whereas ONE file path out of a candidate list is documented
                # fail-soft per probe (see ``_probe_exposed_file``) and a
                # blocked or refused probe already yields ``None`` here. Making
                # a raised probe louder than a refused one would misreport the
                # far more common case. If exposed-files ever needs per-path
                # provenance, promote this to a finding -- do not simply widen
                # the log.
                logger.warning("Exposed-file probe failed for %s: %s", url, result)
                continue
            if result is not None:
                findings.append(result)
        return findings

    async def _check_cors(self, url: str) -> list[dict]:
        """Check for CORS misconfigurations.

        Runs on its own connection, so it re-screens the target rather than
        relying on ``scan``'s one-shot entry check. It used to follow redirects
        automatically, which meant an admitted public origin could bounce the
        probe into the tailnet; the hops are now walked manually and each one is
        re-validated (:func:`guarded_get`). The observed headers still come from
        the end of the chain, as before.

        Args:
            url: The URL to send the cross-origin probe to.

        Returns:
            CORS findings, or an empty list if the probe was blocked or failed
            -- this check is fail-soft.
        """
        findings: list[dict] = []
        evil_origin = "https://evil.example.com"

        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=False, verify=True
            ) as client:
                resp = await guarded_get(client, url, headers={"Origin": evil_origin})
                acao = resp.headers.get("access-control-allow-origin", "")
                acac = resp.headers.get("access-control-allow-credentials", "").lower()

                if acao == evil_origin:
                    findings.append(
                        _sec_finding(
                            severity="critical",
                            title="CORS reflects arbitrary origin",
                            description=(
                                "The server reflects the Origin header in"
                                " Access-Control-Allow-Origin, allowing any site to make"
                                " authenticated cross-origin requests."
                            ),
                            url=url,
                            recommendation=("Configure CORS to only allow trusted origins."),
                            check_id="sec-cors-reflected",
                            remediation_effort="medium",
                        )
                    )
                elif acao == "*" and acac == "true":
                    findings.append(
                        _sec_finding(
                            severity="high",
                            title="CORS wildcard with credentials",
                            description=(
                                "The server uses Access-Control-Allow-Origin: * with"
                                " Access-Control-Allow-Credentials: true, which is a"
                                " dangerous misconfiguration."
                            ),
                            url=url,
                            recommendation=(
                                "Do not combine wildcard origin with credentials. Specify"
                                " allowed origins explicitly."
                            ),
                            check_id="sec-cors-wildcard-credentials",
                            remediation_effort="medium",
                        )
                    )
                elif acao == "*":
                    findings.append(
                        _sec_finding(
                            severity="low",
                            title="CORS allows all origins",
                            description=(
                                "The server uses Access-Control-Allow-Origin: *. This is"
                                " acceptable for public APIs but may be overly permissive"
                                " for other resources."
                            ),
                            url=url,
                            recommendation=(
                                "Consider restricting CORS to specific trusted origins if"
                                " the resource is not intended to be fully public."
                            ),
                            check_id="sec-cors-wildcard",
                            remediation_effort="medium",
                        )
                    )
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
            logger.debug("CORS check failed for %s: %s", url, e)

        return findings

    async def _check_redirect_chain(self, url: str) -> list[dict]:
        """Check for long redirect chains or HTTP downgrades.

        This is the one check in this class that drives the chain by hand with a
        bare ``client.get`` instead of :func:`guarded_get`, because it needs to
        *observe* each hop rather than merely survive it. That makes its own two
        screens -- the entry URL and each ``Location`` -- load-bearing, so both
        resolve.
        """
        findings: list[dict] = []
        if not await _is_ssrf_safe_resolved(url):
            return findings

        try:
            chain: list[str] = [url]
            current_url = url
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                verify=True,
            ) as client:
                for _ in range(10):
                    resp = await client.get(current_url)
                    if resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = resp.headers.get("location", "")
                    if not location:
                        break
                    # Resolve with urljoin, not a startswith("/") special case:
                    # the hand-rolled form leaves a bare relative Location
                    # ("next") unresolved — httpx would then be handed a
                    # schemeless target — and mangles a protocol-relative
                    # "//host/path" into "scheme://current-host//host/path",
                    # silently rewriting which host the chain reports.
                    location = urljoin(current_url, location)
                    chain.append(location)
                    if current_url.startswith("https://") and location.startswith("http://"):
                        findings.append(
                            _sec_finding(
                                severity="high",
                                title="Redirect downgrades HTTPS to HTTP",
                                description=(
                                    f"A redirect from {current_url} leads to "
                                    f"an HTTP URL: {location}. This exposes "
                                    "users to man-in-the-middle attacks."
                                ),
                                url=current_url,
                                recommendation=("Ensure all redirects stay on HTTPS."),
                                check_id="sec-redirect-to-http",
                                evidence=f"Chain: {' -> '.join(chain)}",
                            )
                        )
                    # The screen itself is not new and is correctly placed: the
                    # Location comes from the *response*, so admitting the entry
                    # URL says nothing about where that host points next, and it
                    # sits below the downgrade check so a hop INTO a blocked
                    # target still yields its finding.
                    #
                    # What was missing is the REPORT. It used to `break` in
                    # silence, so a chain truncated for a security reason was
                    # indistinguishable from one that simply ended -- the
                    # scanner's own defensive action was invisible to the person
                    # reading the result. Record why the walk stopped.
                    #
                    # Resolving, like the entry screen: a ``Location`` naming a
                    # host that merely *resolves* into the estate is the exact
                    # bypass a structural re-check waves through, and this loop
                    # feeds the value straight back into ``client.get``.
                    if not await _is_ssrf_safe_resolved(location):
                        logger.warning(
                            "Redirect chain for %s stopped: hop to non-public/unsafe URL %s",
                            url,
                            location,
                        )
                        findings.append(
                            _sec_finding(
                                severity="info",
                                title="Redirect chain stopped (redirect to internal URL)",
                                description=(
                                    f"A redirect from {current_url} led to an"
                                    f" internal/private URL: {location}. The chain was not"
                                    " followed any further."
                                ),
                                url=current_url,
                                check_id="sec-ssrf-blocked",
                                evidence=f"Chain: {' -> '.join(chain)}",
                            )
                        )
                        break
                    current_url = location

            if len(chain) > 4:  # original + 3 redirects = 4 entries
                findings.append(
                    _sec_finding(
                        severity="medium",
                        title=f"Long redirect chain ({len(chain) - 1} hops)",
                        description=(
                            f"The URL goes through {len(chain) - 1} redirects "
                            "before reaching the final destination. Long chains "
                            "slow page load and may confuse crawlers."
                        ),
                        url=url,
                        recommendation=("Reduce the redirect chain to at most 1-2 hops."),
                        check_id="sec-redirect-chain-long",
                        evidence=f"Chain: {' -> '.join(chain[:6])}",
                    )
                )
        # This check drives the chain with a raw ``client.get``/``client.head``
        # rather than ``guarded_get``, so every httpx failure lands here
        # untranslated. NOTHING in the httpx tree derives from ``OSError`` or
        # ``RuntimeError``: ``ConnectError``, ``ReadTimeout``,
        # ``RemoteProtocolError`` and friends all hang off
        # ``httpx.HTTPError -> Exception``. The old builtin-only tuple therefore
        # caught none of them and a single flaky origin escaped this coroutine
        # into ``scan``'s ``asyncio.gather``. The sibling ``_check_cors`` above
        # already names the base class; this is the same tuple.
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
            logger.debug("Redirect chain check failed for %s: %s", url, e)

        return findings

    async def _check_directory_listing(self, url: str) -> list[dict]:
        """Check for directory listing on common directories.

        Each ``dir_url`` is derived from the scanned origin, so it is a
        different string from the one ``scan`` screened, and this check used to
        follow redirects automatically on top of that. Both holes are closed by
        routing every probe through :func:`guarded_get`, which screens the
        derived URL and re-screens each hop.

        Args:
            url: The URL whose origin supplies the directory candidates.

        Returns:
            Directory-listing findings, or an empty list if every probe was
            blocked or failed -- this check is fail-soft, per candidate and
            overall.
        """
        findings: list[dict] = []
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=False, verify=True
            ) as client:
                for dir_path in DIRECTORY_LISTING_PATHS:
                    dir_url = urljoin(base_url + "/", dir_path.lstrip("/"))
                    try:
                        resp = await guarded_get(client, dir_url)
                        if resp.status_code == 200:
                            body = resp.text.lower()
                            if "index of" in body or "directory listing" in body:
                                findings.append(
                                    _sec_finding(
                                        severity="medium",
                                        title=f"Directory listing enabled: {dir_path}",
                                        description=(
                                            f"Directory listing is enabled at {dir_url}."
                                            " This may expose internal file structure."
                                        ),
                                        url=dir_url,
                                        recommendation=(
                                            "Disable directory listing in server configuration."
                                        ),
                                        check_id="sec-directory-listing",
                                    )
                                )
                            # Only check first directory that returns 200
                            break
                    except httpx.HTTPError, OSError, ValueError:
                        continue
        # ``httpx.HTTPError`` belongs here for the same reason it belongs in the
        # sibling handlers above: nothing in the httpx tree derives from
        # ``OSError`` or ``RuntimeError``. The inner ``except`` only guards the
        # per-directory probe, so a failure raised while opening or closing the
        # client -- or by any httpx call outside that inner block -- escaped
        # this coroutine and cost the caller its whole directory-listing family.
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
            logger.debug("Directory listing check failed for %s: %s", url, e)

        return findings
