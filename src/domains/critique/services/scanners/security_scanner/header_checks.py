"""Response-header-based security checks (no network beyond initial GET)."""

from __future__ import annotations

import re

import httpx

from src.domains.critique.services.scanners.security_scanner.helpers import _sec_finding

#: ``Server`` header value -> hosting platform that offers NO way to set custom
#: response headers. (Netlify/Vercel/Cloudflare Pages all do, so they are absent.)
_HEADER_LOCKED_HOSTS: dict[str, str] = {"github.com": "GitHub Pages"}

_PROXY_ALTERNATIVE = (
    "Serve the site through a CDN or reverse proxy that can add response headers"
    " (for example Cloudflare in front of the custom domain), or move to a host"
    " that supports custom headers."
)

_STATIC_HOST_ALTERNATIVES: dict[str, str] = {
    "content-security-policy": (
        'Add a <meta http-equiv="Content-Security-Policy" content="..."> tag'
        " to <head> (frame-ancestors and report-uri are not honoured in a meta"
        " tag), or serve the site through a CDN or reverse proxy that adds the"
        " header (for example Cloudflare)."
    ),
    "strict-transport-security": (
        "Enable 'Enforce HTTPS' in the GitHub Pages settings (HTTP is then"
        " redirected). HSTS itself can only be sent by a CDN or reverse proxy in"
        " front of the site, for example Cloudflare."
    ),
    "referrer-policy": (
        'Add <meta name="referrer" content="strict-origin-when-cross-origin"> to <head>.'
    ),
}


def _header_locked_static_host(headers: httpx.Headers) -> str | None:
    """Name of a hosting platform that cannot send custom headers, if detected."""
    return _HEADER_LOCKED_HOSTS.get(headers.get("server", "").strip().lower())


class HeaderChecksMixin:
    """Mixin for response-header security checks used by ``SecurityScanner``."""

    def _check_security_headers(self, headers: httpx.Headers, url: str) -> list[dict]:
        findings: list[dict] = []

        header_checks = {
            "content-security-policy": {
                "severity": "high",
                "title": "Missing Content-Security-Policy header",
                "description": "No CSP header found. This leaves the site vulnerable to XSS attacks.",
                "recommendation": "Add a Content-Security-Policy header to restrict resource loading.",
                "check_id": "sec-missing-csp",
                "remediation_effort": "high",
            },
            "strict-transport-security": {
                "severity": "high",
                "title": "Missing Strict-Transport-Security header",
                "description": (
                    "No HSTS header found. The site may be vulnerable to protocol downgrade"
                    " attacks."
                ),
                "recommendation": (
                    "Add Strict-Transport-Security: max-age=31536000; includeSubDomains."
                ),
                "check_id": "sec-missing-hsts",
                "remediation_effort": "low",
            },
            "x-frame-options": {
                "severity": "medium",
                "title": "Missing X-Frame-Options header",
                "description": (
                    "No X-Frame-Options header. The page may be vulnerable to clickjacking."
                ),
                "recommendation": "Add X-Frame-Options: DENY or SAMEORIGIN.",
                "check_id": "sec-missing-xfo",
                "remediation_effort": "low",
            },
            "x-content-type-options": {
                "severity": "medium",
                "title": "Missing X-Content-Type-Options header",
                "description": (
                    "No X-Content-Type-Options header. Browsers may MIME-sniff responses."
                ),
                "recommendation": "Add X-Content-Type-Options: nosniff.",
                "check_id": "sec-missing-xcto",
                "remediation_effort": "low",
            },
            "referrer-policy": {
                "severity": "low",
                "title": "Missing Referrer-Policy header",
                "description": (
                    "No Referrer-Policy header. Full URLs may leak in referrer headers."
                ),
                "recommendation": "Add Referrer-Policy: strict-origin-when-cross-origin.",
                "check_id": "sec-missing-referrer-policy",
                "remediation_effort": "low",
            },
            "permissions-policy": {
                "severity": "low",
                "title": "Missing Permissions-Policy header",
                "description": ("No Permissions-Policy header to restrict browser feature access."),
                "recommendation": (
                    "Add Permissions-Policy to restrict camera, microphone, geolocation, etc."
                ),
                "check_id": "sec-missing-permissions-policy",
                "remediation_effort": "low",
            },
        }

        platform = _header_locked_static_host(headers)
        for header_name, check in header_checks.items():
            if header_name in headers:
                continue
            severity = check["severity"]
            description = check["description"]
            recommendation = check["recommendation"]
            evidence = None
            if platform is not None:
                # The owner cannot act on "add header X" here -- the platform
                # does not let them set response headers. Say so, give the
                # actionable route, and stop ranking static-site HSTS as HIGH
                # (no session/cookies to downgrade; the host already redirects).
                if header_name == "strict-transport-security":
                    severity = "medium"
                description = (
                    f"{description} The site is served by {platform}, which does not"
                    " allow custom response headers."
                )
                recommendation = _STATIC_HOST_ALTERNATIVES.get(header_name, _PROXY_ALTERNATIVE)
                evidence = f"server: {headers.get('server', '')}"
            findings.append(
                _sec_finding(
                    severity=severity,
                    title=check["title"],
                    description=description,
                    url=url,
                    recommendation=recommendation,
                    check_id=check["check_id"],
                    evidence=evidence,
                    remediation_effort=check["remediation_effort"],
                )
            )

        return findings

    def _check_cookie_flags(self, response: httpx.Response, url: str) -> list[dict]:
        findings: list[dict] = []
        cookies = response.cookies

        for cookie in cookies.jar:
            name = cookie.name
            issues = []
            if not cookie.secure:
                issues.append("Secure")
            if not cookie.has_nonstandard_attr("HttpOnly"):
                issues.append("HttpOnly")
            samesite = cookie.get_nonstandard_attr("SameSite")
            if not samesite:
                issues.append("SameSite")

            if issues:
                findings.append(
                    _sec_finding(
                        severity="medium",
                        title=f"Cookie '{name}' missing security flags",
                        description=f"Cookie '{name}' is missing: {', '.join(issues)}.",
                        url=url,
                        recommendation=f"Set {', '.join(issues)} flags on cookie '{name}'.",
                        check_id="sec-cookie-flags",
                    )
                )

        return findings

    def _check_server_header(self, headers: httpx.Headers, url: str) -> list[dict]:
        findings: list[dict] = []
        server = headers.get("server", "")
        if server and any(c.isdigit() for c in server):
            findings.append(
                _sec_finding(
                    severity="low",
                    title="Server version disclosure",
                    description=f"Server header reveals version info: '{server}'.",
                    url=url,
                    recommendation="Remove version numbers from the Server header.",
                    check_id="sec-server-disclosure",
                )
            )
        return findings

    def _check_csp_details(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Analyze Content-Security-Policy header for common weaknesses."""
        findings: list[dict] = []
        csp = headers.get("content-security-policy", "")
        if not csp:
            return findings

        if "'unsafe-inline'" in csp:
            findings.append(
                _sec_finding(
                    severity="high",
                    title="CSP allows unsafe-inline",
                    description=(
                        "The Content-Security-Policy contains 'unsafe-inline', which"
                        " significantly weakens XSS protection."
                    ),
                    url=url,
                    recommendation=(
                        "Remove 'unsafe-inline' from CSP and use nonces or hashes instead."
                    ),
                    check_id="sec-csp-unsafe-inline",
                    evidence=csp,
                    remediation_effort="high",
                )
            )

        if "'unsafe-eval'" in csp:
            findings.append(
                _sec_finding(
                    severity="high",
                    title="CSP allows unsafe-eval",
                    description=(
                        "The Content-Security-Policy contains 'unsafe-eval', which allows"
                        " JavaScript code execution from strings (e.g., eval())."
                    ),
                    url=url,
                    recommendation=(
                        "Remove 'unsafe-eval' from CSP and refactor code that relies on eval()."
                    ),
                    check_id="sec-csp-unsafe-eval",
                    evidence=csp,
                    remediation_effort="high",
                )
            )

        # Check for wildcard * in directive values (but not in report-uri etc.)
        directives = csp.split(";")
        for directive in directives:
            directive = directive.strip()
            parts = directive.split()
            if len(parts) >= 2:
                sources = parts[1:]
                for source in sources:
                    if source.strip() == "*":
                        findings.append(
                            _sec_finding(
                                severity="medium",
                                title="CSP contains wildcard source",
                                description=(
                                    f"The CSP directive '{parts[0]}' uses a wildcard (*),"
                                    " allowing resources from any origin."
                                ),
                                url=url,
                                recommendation=(
                                    "Replace wildcard sources with specific trusted domains."
                                ),
                                check_id="sec-csp-wildcard",
                                evidence=csp,
                                remediation_effort="high",
                            )
                        )
                        # Only report wildcard once
                        return findings

        return findings

    def _check_technology_disclosure(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Check for technology disclosure in response headers."""
        findings: list[dict] = []
        disclosure_headers = [
            "x-powered-by",
            "x-aspnet-version",
            "x-generator",
        ]
        disclosed: list[str] = []
        for header_name in disclosure_headers:
            value = headers.get(header_name)
            if value:
                disclosed.append(f"{header_name}: {value}")

        if disclosed:
            findings.append(
                _sec_finding(
                    severity="low",
                    title="Technology stack disclosed in headers",
                    description=(
                        f"Response headers reveal technology information: "
                        f"{'; '.join(disclosed)}. This helps attackers "
                        "fingerprint the server."
                    ),
                    url=url,
                    recommendation=(
                        "Remove technology-disclosing headers like "
                        "X-Powered-By, X-AspNet-Version, and X-Generator."
                    ),
                    check_id="sec-tech-disclosure",
                    evidence="; ".join(disclosed),
                )
            )
        return findings

    def _check_hsts_quality(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Check HSTS header quality (max-age, includeSubDomains)."""
        findings: list[dict] = []
        hsts = headers.get("strict-transport-security", "")
        if not hsts:
            return findings

        max_age_match = re.search(r"max-age=(\d+)", hsts, re.IGNORECASE)
        if max_age_match:
            max_age = int(max_age_match.group(1))
            if max_age < 31536000:
                findings.append(
                    _sec_finding(
                        severity="medium",
                        title="HSTS max-age too short",
                        description=(
                            f"HSTS max-age is {max_age} seconds "
                            f"({max_age // 86400} days). Recommended minimum "
                            "is 31536000 (1 year)."
                        ),
                        url=url,
                        recommendation=(
                            "Set Strict-Transport-Security max-age to at least 31536000 (1 year)."
                        ),
                        check_id="sec-hsts-short-max-age",
                        evidence=hsts,
                    )
                )

        if "includesubdomains" not in hsts.lower():
            findings.append(
                _sec_finding(
                    severity="low",
                    title="HSTS missing includeSubDomains",
                    description=(
                        "The HSTS header does not include the "
                        "includeSubDomains directive. Subdomains are "
                        "not protected by HSTS."
                    ),
                    url=url,
                    recommendation=(
                        "Add includeSubDomains to the Strict-Transport-Security header."
                    ),
                    check_id="sec-hsts-no-subdomains",
                    evidence=hsts,
                )
            )
        return findings

    def _check_csp_reporting(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Flag CSP headers that lack report-uri or report-to directives."""
        findings: list[dict] = []
        csp = headers.get("content-security-policy", "")
        if not csp:
            return findings

        has_reporting = "report-uri" in csp.lower() or "report-to" in csp.lower()
        if not has_reporting:
            findings.append(
                _sec_finding(
                    severity="low",
                    title="CSP has no reporting directive",
                    description=(
                        "The Content-Security-Policy header exists but does "
                        "not include a report-uri or report-to directive. "
                        "Without reporting, CSP violations go unnoticed."
                    ),
                    url=url,
                    recommendation=(
                        "Add a report-uri or report-to directive to the CSP "
                        "header so you can monitor policy violations."
                    ),
                    check_id="sec-csp-no-reporting",
                    evidence=csp[:200],
                    remediation_effort="medium",
                )
            )
        return findings

    def _check_corp(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Check for missing Cross-Origin-Resource-Policy header."""
        findings: list[dict] = []
        if "cross-origin-resource-policy" not in headers:
            findings.append(
                _sec_finding(
                    severity="low",
                    title="Missing Cross-Origin-Resource-Policy header",
                    description=(
                        "No Cross-Origin-Resource-Policy (CORP) header found. "
                        "CORP prevents other origins from loading your "
                        "resources, mitigating side-channel attacks like "
                        "Spectre."
                    ),
                    url=url,
                    recommendation=(
                        "Add Cross-Origin-Resource-Policy: same-origin (or "
                        "same-site / cross-origin as appropriate)."
                    ),
                    check_id="sec-missing-corp",
                    false_positive_risk="medium",
                    remediation_effort="medium",
                )
            )
        return findings

    def _check_coop(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Check for missing Cross-Origin-Opener-Policy header."""
        findings: list[dict] = []
        if "cross-origin-opener-policy" not in headers:
            findings.append(
                _sec_finding(
                    severity="low",
                    title="Missing Cross-Origin-Opener-Policy header",
                    description=(
                        "No Cross-Origin-Opener-Policy (COOP) header found. "
                        "COOP isolates your browsing context from cross-origin "
                        "documents, protecting against Spectre-like attacks."
                    ),
                    url=url,
                    recommendation=(
                        "Add Cross-Origin-Opener-Policy: same-origin to "
                        "isolate the browsing context."
                    ),
                    check_id="sec-missing-coop",
                    false_positive_risk="medium",
                    remediation_effort="medium",
                )
            )
        return findings

    def _check_coep(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Check for missing Cross-Origin-Embedder-Policy header."""
        findings: list[dict] = []
        if "cross-origin-embedder-policy" not in headers:
            findings.append(
                _sec_finding(
                    severity="low",
                    title="Missing Cross-Origin-Embedder-Policy header",
                    description=(
                        "No Cross-Origin-Embedder-Policy (COEP) header found. "
                        "COEP ensures all sub-resources are loaded with "
                        "explicit permission, enabling cross-origin isolation."
                    ),
                    url=url,
                    recommendation=(
                        "Add Cross-Origin-Embedder-Policy: require-corp to "
                        "enable cross-origin isolation."
                    ),
                    check_id="sec-missing-coep",
                    false_positive_risk="medium",
                    remediation_effort="medium",
                )
            )
        return findings

    def _check_dns_prefetch_control(self, headers: httpx.Headers, url: str) -> list[dict]:
        """Check for missing X-DNS-Prefetch-Control header."""
        findings: list[dict] = []
        if "x-dns-prefetch-control" not in headers:
            findings.append(
                _sec_finding(
                    severity="info",
                    title="Missing X-DNS-Prefetch-Control header",
                    description=(
                        "No X-DNS-Prefetch-Control header found. This header "
                        "controls DNS prefetching, which can leak information "
                        "about which links a user might visit."
                    ),
                    url=url,
                    recommendation=(
                        "Add X-DNS-Prefetch-Control: off to prevent "
                        "unintended DNS prefetch requests, or explicitly set "
                        "it to 'on' if prefetching is desired."
                    ),
                    check_id="sec-dns-prefetch",
                    false_positive_risk="high",
                )
            )
        return findings
