"""TLS/SSL related security checks."""

from __future__ import annotations

import logging
import socket
import ssl
from datetime import UTC, datetime
from urllib.parse import urlparse

from src.domains.critique.services.scanners.security_scanner.helpers import _sec_finding
from src.domains.critique.services.scanners.security_scanner.http_guard import (
    _is_ssrf_safe_resolved_blocking,
)

logger = logging.getLogger(__name__)


#: Days-left thresholds for TLS expiry findings (see ``_tls_expiry_findings``).
TLS_EXPIRY_WARN_DAYS = 21
TLS_EXPIRY_HIGH_DAYS = 7


class TLSChecksMixin:
    """Mixin providing TLS / certificate checks for ``SecurityScanner``."""

    def _check_tls(self, url: str) -> list[dict]:
        """Inspect the origin's TLS version and certificate expiry.

        This is the one sink in the package that does not speak HTTP: it opens
        a raw ``socket`` to ``(hostname, port)``, so it cannot go through
        :func:`guarded_get` and carries the SSRF screen itself. Unguarded, it
        was a straight TCP connect to an attacker-chosen host -- the most
        direct probe primitive in the scanner, and the only one reachable with
        no HTTP layer in the way at all.

        The call is **blocking**; ``SecurityScanner.scan`` runs it under
        ``asyncio.to_thread`` so a slow or black-holed origin cannot stall the
        event loop for the full five-second connect timeout. That is also why
        the guard here is the *blocking* resolving form: this frame is already
        off the loop, so dispatching the ``getaddrinfo`` to a second thread
        would buy nothing. It must be a resolving form either way -- a raw
        ``socket.create_connection((hostname, port))`` re-resolves the name
        itself, so a structural screen of the URL string decides nothing about
        where the TCP connect actually lands.

        Args:
            url: The URL whose origin should be inspected.

        Returns:
            TLS findings. A blocked target yields a ``sec-ssrf-blocked`` info
            finding; a failed handshake yields nothing -- this check is
            fail-soft either way.
        """
        findings: list[dict] = []
        parsed = urlparse(url)

        if parsed.scheme != "https":
            findings.append(
                _sec_finding(
                    severity="high",
                    title="Not using HTTPS",
                    description="The site is served over HTTP, not HTTPS.",
                    url=url,
                    recommendation="Enable HTTPS with a valid TLS certificate.",
                    check_id="sec-no-https",
                )
            )
            return findings

        hostname = parsed.hostname or ""
        port = parsed.port or 443

        if not _is_ssrf_safe_resolved_blocking(url):
            logger.warning("Blocked SSRF attempt in TLS check for %s", url)
            return [
                _sec_finding(
                    severity="info",
                    title="TLS check skipped (internal URL)",
                    description=f"TLS check skipped for internal/private URL: {url}",
                    url=url,
                    check_id="sec-ssrf-blocked",
                )
            ]

        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(
                socket.create_connection((hostname, port), timeout=5),
                server_hostname=hostname,
            ) as ssock:
                version = ssock.version()
                if version and version in ("TLSv1", "TLSv1.1"):
                    findings.append(
                        _sec_finding(
                            severity="high",
                            title=f"Outdated TLS version: {version}",
                            description=(
                                f"The server uses {version}, which is deprecated and insecure."
                            ),
                            url=url,
                            recommendation="Upgrade to TLS 1.2 or TLS 1.3.",
                            check_id="sec-tls-outdated",
                        )
                    )

                # Certificate expiry check
                cert = ssock.getpeercert()
                if cert:
                    not_after_raw = cert.get("notAfter", "")
                    not_after_str = str(not_after_raw) if not_after_raw else ""
                    if not_after_str:
                        # Python ssl cert dates are in format: 'Mon DD HH:MM:SS YYYY GMT'
                        not_after = datetime.strptime(
                            not_after_str, "%b %d %H:%M:%S %Y %Z"
                        ).replace(tzinfo=UTC)
                        now = datetime.now(UTC)
                        days_remaining = (not_after - now).days
                        findings.extend(
                            self._tls_expiry_findings(url, not_after_str, days_remaining)
                        )
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as e:
            logger.debug("TLS check failed for %s: %s", url, e)

        return findings

    @staticmethod
    def _tls_expiry_findings(url: str, not_after_str: str, days_remaining: int) -> list[dict]:
        """Build expiry-related findings for a parsed cert ``notAfter``."""
        if days_remaining < 0:
            return [
                _sec_finding(
                    severity="critical",
                    title="TLS certificate expired",
                    description=(
                        f"The TLS certificate expired on {not_after_str}"
                        f" ({abs(days_remaining)} days ago)."
                    ),
                    url=url,
                    recommendation="Renew the TLS certificate immediately.",
                    check_id="sec-tls-expired",
                )
            ]
        # Certificates are renewed automatically (ACME renews at ~30 days left;
        # GitHub Pages / Cloudflare / managed hosts do it for you), so a cert
        # with 65 days left is the NORMAL state, not a finding. Only a cert that
        # is inside the window where automated renewal should already have
        # happened is reported, and severity scales with the days left.
        if days_remaining <= TLS_EXPIRY_HIGH_DAYS:
            return [
                _sec_finding(
                    severity="high",
                    title="TLS certificate expiring soon",
                    description=(
                        f"The TLS certificate expires on {not_after_str}"
                        f" ({days_remaining} days remaining)."
                    ),
                    url=url,
                    recommendation="Renew the TLS certificate before it expires.",
                    check_id="sec-tls-expiring-soon",
                )
            ]
        if days_remaining <= TLS_EXPIRY_WARN_DAYS:
            return [
                _sec_finding(
                    severity="medium",
                    title="TLS certificate expiring",
                    description=(
                        f"The TLS certificate expires on {not_after_str}"
                        f" ({days_remaining} days remaining) and has not been"
                        " renewed; automated renewal normally happens earlier."
                    ),
                    url=url,
                    recommendation=(
                        "Check that automatic certificate renewal is working, or"
                        " renew the certificate manually."
                    ),
                    check_id="sec-tls-expiring",
                )
            ]
        return []
