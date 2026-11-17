"""HTML-content-based security checks (mixed content, secrets, SRI, forms, meta refresh)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.security_scanner.helpers import _sec_finding

_SECRET_PATTERN = re.compile(
    r"""(?:api[_\-]?key|secret[_\-]?key|api[_\-]?secret|token|password|passwd|"""
    r"""access[_\-]?key|private[_\-]?key)\s*[:=]\s*['"]([A-Za-z0-9+/=_\-]{16,})['"]""",
    re.IGNORECASE,
)


#: Hosts whose response body legitimately changes without the URL changing
#: (per-user-agent font CSS, continuously updated tag/analytics loaders, vendor
#: SDKs whose docs forbid pinning). An ``integrity`` hash on these BREAKS the
#: resource the next time the vendor ships, so "add SRI" is wrong advice.
_DYNAMIC_RESOURCE_HOSTS = (
    "fonts.googleapis.com",
    "googletagmanager.com",
    "google-analytics.com",
    "js.stripe.com",
    "connect.facebook.net",
    "maps.googleapis.com",
    "www.google.com",
    "www.gstatic.com",
    "static.hotjar.com",
    "use.typekit.net",
)


def _serves_dynamic_content(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _DYNAMIC_RESOURCE_HOSTS)


class HtmlChecksMixin:
    """Mixin for HTML-content security checks used by ``SecurityScanner``."""

    def _check_mixed_content(self, soup: BeautifulSoup, url: str) -> list[dict]:
        """Check for mixed content (HTTP resources on HTTPS pages)."""
        findings: list[dict] = []
        parsed = urlparse(url)

        # Only flag mixed content on HTTPS pages
        if parsed.scheme != "https":
            return findings

        # Tags and attributes that load external resources
        resource_tags = {
            "img": ["src"],
            "script": ["src"],
            "link": ["href"],
            "iframe": ["src"],
            "video": ["src"],
            "audio": ["src"],
            "source": ["src"],
            "object": ["data"],
            "embed": ["src"],
        }

        mixed_urls: list[str] = []
        for tag_name, attrs in resource_tags.items():
            for tag in soup.find_all(tag_name):
                for attr in attrs:
                    value = tag.get(attr, "")
                    if isinstance(value, str) and value.startswith("http://"):
                        mixed_urls.append(value)

        if mixed_urls:
            sample = mixed_urls[:5]
            extra = f" (and {len(mixed_urls) - 5} more)" if len(mixed_urls) > 5 else ""
            url_list = ", ".join(sample)
            findings.append(
                _sec_finding(
                    severity="high",
                    title="Mixed content detected",
                    description=(
                        f"Found {len(mixed_urls)} resource(s) loaded over HTTP on an HTTPS"
                        f" page: {url_list}{extra}"
                    ),
                    url=url,
                    recommendation=(
                        "Load all resources over HTTPS to prevent mixed content warnings"
                        " and potential man-in-the-middle attacks."
                    ),
                    check_id="sec-mixed-content",
                    remediation_effort="medium",
                )
            )

        return findings

    def _check_html_secrets(self, html: str, url: str) -> list[dict]:
        """Scan HTML for exposed secrets (API keys, tokens, passwords)."""
        findings: list[dict] = []

        matches = _SECRET_PATTERN.findall(html)
        if matches:
            count = min(len(matches), 10)
            redacted = [f"{m[:4]}...{m[-4:]}" if len(m) > 8 else "****" for m in matches[:count]]
            findings.append(
                _sec_finding(
                    severity="critical",
                    title=f"Potential secrets exposed in HTML ({count} found)",
                    description=(
                        f"Found {len(matches)} potential secret(s) (API keys, "
                        "tokens, or passwords) in the page HTML. These are "
                        "visible to anyone viewing the page source."
                    ),
                    url=url,
                    recommendation=(
                        "Move all secrets to server-side environment variables. "
                        "Never embed API keys or tokens in client-side HTML."
                    ),
                    check_id="sec-html-secret-exposed",
                    evidence=f"Redacted values: {', '.join(redacted)}",
                    false_positive_risk="medium",
                    remediation_effort="medium",
                )
            )
        return findings

    def _check_sri(self, soup: BeautifulSoup, url: str) -> list[dict]:
        """Check for missing Subresource Integrity on CDN scripts/styles."""
        findings: list[dict] = []
        parsed = urlparse(url)
        page_host = parsed.hostname or ""

        missing_sri: list[str] = []
        candidates = [(t, str(t.get("src", ""))) for t in soup.find_all("script", src=True)]
        candidates += [(t, str(t.get("href", ""))) for t in soup.find_all("link", rel="stylesheet")]
        for tag, ref in candidates:
            # ``integrity`` is a VALUED attribute (an empty one verifies nothing).
            if not ref.startswith("http") or str(tag.get("integrity", "")).strip():
                continue
            host = (urlparse(ref).hostname or "").lower()
            if not host or host == page_host or _serves_dynamic_content(host):
                continue
            missing_sri.append(ref)

        if missing_sri:
            sample = missing_sri[:5]
            extra = f" (and {len(missing_sri) - 5} more)" if len(missing_sri) > 5 else ""
            findings.append(
                _sec_finding(
                    severity="medium",
                    title=f"Missing SRI on {len(missing_sri)} external resource(s)",
                    description=(
                        f"{len(missing_sri)} external script(s)/stylesheet(s) "
                        "from CDNs lack integrity attributes. Without SRI, a "
                        "compromised CDN could inject malicious code."
                    ),
                    url=url,
                    recommendation=(
                        "Add integrity and crossorigin attributes to all "
                        "external <script> and <link> tags from third-party CDNs."
                    ),
                    check_id="sec-sri-missing",
                    evidence=f"Resources: {', '.join(sample)}{extra}",
                    remediation_effort="medium",
                )
            )
        return findings

    def _check_meta_refresh_redirect(self, soup: BeautifulSoup, url: str) -> list[dict]:
        """Flag <meta http-equiv='refresh'> tags that perform redirects."""
        findings: list[dict] = []
        meta_refreshes = soup.find_all(
            "meta", attrs={"http-equiv": re.compile(r"^refresh$", re.IGNORECASE)}
        )

        for meta in meta_refreshes:
            content = str(meta.get("content", "")).lower()
            if "url=" in content:
                findings.append(
                    _sec_finding(
                        severity="medium",
                        title="Meta refresh redirect detected",
                        description=(
                            "A <meta http-equiv='refresh'> tag redirects to "
                            "another URL. Meta refresh redirects can be used "
                            "for phishing and are not accessible to all users."
                        ),
                        url=url,
                        recommendation=(
                            "Use HTTP 301/302 redirects instead of meta "
                            "refresh redirects for better security and "
                            "accessibility."
                        ),
                        selector='meta[http-equiv="refresh"]',
                        check_id="sec-meta-refresh-redirect",
                        evidence=content[:200],
                    )
                )
        return findings

    def _check_form_action_external(self, soup: BeautifulSoup, url: str) -> list[dict]:
        """Flag forms whose action points to an external domain."""
        findings: list[dict] = []
        if not url:
            return findings

        page_domain = urlparse(url).netloc.lower()
        external_forms: list[str] = []

        for form in soup.find_all("form", action=True):
            action = str(form.get("action", "")).strip()
            if not action or action.startswith("#"):
                continue
            parsed_action = urlparse(action)
            action_domain = (parsed_action.netloc or "").lower()
            if action_domain and action_domain != page_domain:
                external_forms.append(action)

        if external_forms:
            sample = external_forms[:5]
            extra = f" (and {len(external_forms) - 5} more)" if len(external_forms) > 5 else ""
            findings.append(
                _sec_finding(
                    severity="medium",
                    title=f"{len(external_forms)} form(s) submit to external domain(s)",
                    description=(
                        "One or more forms have action attributes pointing to "
                        "external domains. This may indicate data exfiltration "
                        "or phishing if unintended."
                    ),
                    url=url,
                    recommendation=(
                        "Verify that external form actions are intentional. "
                        "Keep form submissions on the same origin where "
                        "possible."
                    ),
                    selector="form[action]",
                    check_id="sec-form-external-action",
                    evidence=f"Actions: {', '.join(sample)}{extra}",
                    false_positive_risk="medium",
                )
            )
        return findings

    def _check_form_insecure(self, soup: BeautifulSoup, url: str) -> list[dict]:
        """Flag forms whose action uses http:// instead of HTTPS."""
        findings: list[dict] = []
        insecure_forms: list[str] = []

        for form in soup.find_all("form", action=True):
            action = str(form.get("action", "")).strip()
            if action.startswith("http://"):
                insecure_forms.append(action)

        if insecure_forms:
            sample = insecure_forms[:5]
            extra = f" (and {len(insecure_forms) - 5} more)" if len(insecure_forms) > 5 else ""
            findings.append(
                _sec_finding(
                    severity="high",
                    title=f"{len(insecure_forms)} form(s) submit over insecure HTTP",
                    description=(
                        "One or more forms have action attributes using "
                        "http:// instead of https://. Form data sent over "
                        "HTTP is transmitted in plain text and vulnerable to "
                        "interception."
                    ),
                    url=url,
                    recommendation=(
                        "Change all form action URLs to use HTTPS to protect user data in transit."
                    ),
                    selector="form[action^='http://']",
                    check_id="sec-form-insecure",
                    evidence=f"Actions: {', '.join(sample)}{extra}",
                )
            )
        return findings
