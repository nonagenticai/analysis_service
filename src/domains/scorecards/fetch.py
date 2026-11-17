"""Shared page fetch + parse for the synchronous scorecard domains.

One capped httpx GET (mirrors artifact_score) and one lxml parse, factored out of
the byte-identical ``_default_fetch_html`` / ``_parse_soup`` copies in the
cro / seo_audit / marketing services. The only per-domain variation is the
``User-Agent``, which is a parameter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx

from src.shared.security.url_validator import pin_target, resolve_fetch_pin

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

MAX_HTML_SIZE = 10 * 1024 * 1024  # 10MB — parity with artifact_score / cro / seo
_MAX_REDIRECTS = 5


class SsrfBlockedError(ValueError):
    """A scorecard fetch targeted a non-public / unsafe host (SSRF guard).

    Raised for the caller-supplied URL and for every redirect hop. The scorecard
    services catch it in their ``except Exception`` fetch guard and surface a
    fail-closed 502 (best-effort competitor fetches simply skip the target), so a
    tenant cannot use a scorecard endpoint to probe ``169.254.169.254`` /
    ``10.x`` / ``localhost`` and read the internal-host signal out of the score.
    """


async def _resolve_pin_target(url: str) -> str:
    """Screen *url* and return the address the connection must be PINNED to.

    Two halves, unchanged from the predicate this replaces: the structural rules
    screen the URL string, and DNS decides whether the *name* leads anywhere in
    the estate. Without the second half a tenant only has to publish an ``A``
    record pointing at ``100.64.0.5`` to have the scorecard fetch it for them.
    The lookup runs off the event loop.

    The third half is what this function adds. The old screen returned a bare
    ``bool`` and discarded the address it had just approved, so the caller handed
    the *hostname* to httpx, which resolved it a **second** time, independently.
    An attacker who controls the authoritative server answers the guard with a
    public address and the transport with ``10.0.0.7`` -- and because the guard
    was re-applied per redirect hop in exactly the same non-pinning shape, every
    hop was checked and every hop was exploitable. Returning the address lets
    the caller pin it, which is the only thing that closes that window.

    Args:
        url: The URL about to be fetched.

    Returns:
        The approved IP to connect to, for :func:`pin_target`.

    Raises:
        SsrfBlockedError: if the URL fails the screen, including when its
            hostname resolves to a non-public address or does not resolve at
            all (fail-closed).
    """
    # Lazy import: the security_scanner package pulls in the scanner mixins and
    # must not become a load-time dependency of this fetch helper (the seo_audit
    # competitor path imports it the same way).
    from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe

    # Structural screen first, exactly as ``_is_ssrf_safe_resolved`` does it --
    # it carries denials the resolving guard does not (the historical
    # ``SSRF_BLOCKED_PREFIXES`` list, the bare ``0.0.0.0`` host).
    if not _is_ssrf_safe(url):
        raise SsrfBlockedError(f"refusing to fetch non-public/unsafe URL: {url}")
    try:
        return await resolve_fetch_pin(url)
    except (ValueError, TypeError, OSError) as exc:
        raise SsrfBlockedError(f"refusing to fetch non-public/unsafe URL: {url}") from exc


def parse_soup(html: str) -> BeautifulSoup | None:
    """Parse HTML once (lxml) so the scanners share it; ``None`` for empty HTML."""
    if not html:
        return None
    from bs4 import BeautifulSoup as _BeautifulSoup

    return _BeautifulSoup(html, "lxml")


async def fetch_html_capped(
    url: str,
    *,
    user_agent: str,
    max_bytes: int = MAX_HTML_SIZE,
    max_redirects: int = _MAX_REDIRECTS,
) -> str:
    """Fetch page HTML (httpx 30s, byte-capped), SSRF-gated and PINNED per hop.

    Redirects are followed manually (``follow_redirects=False``) so every hop's
    target is re-validated with the resolving screen. Auto-following would let a
    public URL that ``302``-redirects to ``http://169.254.169.254/…`` (or a name
    resolving to a private IP) bypass a one-shot initial check.

    Re-validating is necessary and was never sufficient. Each hop's connection is
    also **pinned** to the address the screen just approved: the request URL's
    host is rewritten to that literal while the ``Host`` header and the TLS SNI
    keep the original hostname (so certificate verification and name-based
    virtual hosts are unaffected). Without the pin the guard and httpx perform
    two independent DNS lookups, and an attacker controlling the record answers
    them differently -- the classic rebinding TOCTOU, re-opened once per hop by
    a per-hop check that does not pin.

    Every hop is re-resolved and re-pinned from **its own** hostname; the
    previous hop's address is never carried forward. The relative-``Location``
    join is against ``current`` (the real URL) rather than ``response.url``,
    which after pinning is the IP-literal form.

    The streaming loop stays local on purpose. ``w2v_common``'s ``safe_get`` /
    ``safe_request`` apply the same judgement but buffer the whole body, so
    routing this byte-capped stream through them would trade an SSRF bug for a
    memory blow-up. Only the judgement and the pinning are taken from the shared
    guard.
    """
    headers = {"User-Agent": user_agent}
    current = url
    current_ip = await _resolve_pin_target(url)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, headers=headers) as client:
        for _hop in range(max_redirects + 1):
            request_url, pin_headers, pin_extensions = pin_target(current, current_ip)
            async with client.stream(
                "GET", request_url, headers=pin_headers, extensions=pin_extensions
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise SsrfBlockedError(f"redirect without Location from {current}")
                    current = urljoin(current, location)
                    # Re-screen AND re-pin from the redirect target's OWN
                    # hostname on every hop.
                    current_ip = await _resolve_pin_target(current)
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        excess = total - max_bytes
                        chunks.append(chunk[: len(chunk) - excess])
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
    raise SsrfBlockedError(f"too many redirects (> {max_redirects}) fetching {url}")
