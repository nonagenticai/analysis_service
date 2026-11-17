"""Security scanner package — public API preserved via re-exports.

This package was decomposed from a single ``security_scanner.py`` (1329 LOC)
into focused submodules. All identifiers previously importable as
``from src.domains.critique.services.scanners.security_scanner import X``
remain available at this same path.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

# Re-exports of third-party modules so test code that patches
# ``...security_scanner.httpx.AsyncClient`` or ``...security_scanner.urlparse``
# continues to work after the package decomposition. ``mock.patch`` on these
# attributes patches the underlying module globally, which is what callers of
# ``scanner.scan()`` rely on.
import httpx  # noqa: F401

from src.domains.critique.services.scanners.security_scanner.helpers import (
    DIRECTORY_LISTING_PATHS,
    EXPOSED_FILE_CHECKS,
    PLATFORM_FILE_CHECKS,
    SSRF_BLOCKED_PREFIXES,
    UNIVERSAL_FILE_CHECKS,
    _detect_platform,
    _is_false_positive_file,
    _sec_finding,
)
from src.shared.security.url_validator import (
    ALLOWED_SCHEMES,
    is_blocked_hostname,
    is_blocked_ip,
    parse_literal_ip,
    validate_fetch_url,
    validate_fetch_url_blocking,
)

logger = logging.getLogger(__name__)


def _is_ssrf_safe(url: str) -> bool:
    """Check if URL is safe from SSRF attacks.

    Delegates the address and hostname rules to the shared guard in
    :mod:`src.shared.security.url_validator` so this path cannot drift from the
    callback-URL path. It previously ran its own in-repo copy of the rules
    (``not is_private and not is_loopback and not is_link_local``) -- and that
    second copy is exactly how the bug survived: it let the entire Tailscale
    tailnet through, because ``100.64.0.0/10`` is RFC 6598 shared address space
    and ``ipaddress`` reports it as neither private nor reserved. So
    ``https://100.64.0.5/`` (worker4 / prod Postgres) and
    ``https://registry.internal/v2/_catalog`` were both "safe". Multicast, the
    IPv4-mapped IPv6 form (``::ffff:100.64.0.1``) and non-HTTP schemes were
    equally unguarded.

    Structural only -- no DNS, deliberately, because this is also the cheap
    pre-screen in front of the resolving check and runs per-link over sitemaps
    inside async scanners. On its own it is **not sufficient**: an attacker who
    controls a public name can point its ``A`` record at ``100.64.0.5`` and
    walk straight through, and re-applying a blind predicate on every redirect
    hop re-applies the same blindness. Every path that actually *fetches* must
    therefore use :func:`_is_ssrf_safe_resolved` (or its blocking twin), which
    runs this screen first and then checks where the name resolves to.

    Defined at package level (not in ``helpers``) so tests that patch
    ``...security_scanner.urlparse`` continue to affect this lookup.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return False
        if hostname in ("localhost", "0.0.0.0"):  # noqa: S104  # nosec B104 — SSRF guard, comparing hostname value not binding
            return False
        # Whole-label suffix check, applied before the IP parse: `registry.internal`
        # walks straight past a `startswith` prefix screen, and `rstrip(".")` inside
        # the shared helper means the trailing-dot FQDN form cannot bypass it either.
        if is_blocked_hostname(hostname):
            return False
        addr = parse_literal_ip(hostname)
        if addr is None:
            # Not a literal address. The historical prefix screen is retained as a
            # belt-and-braces extra deny (it catches names like `10.something.com`,
            # which an existing test asserts are blocked). It only ever adds
            # denials -- the suffix check above is the real rule.
            return not any(hostname.startswith(p) for p in SSRF_BLOCKED_PREFIXES)
        return not is_blocked_ip(addr)
    except ValueError, TypeError, KeyError, OSError, RuntimeError:
        return False


async def _is_ssrf_safe_resolved(url: str) -> bool:
    """:func:`_is_ssrf_safe` **plus** "and it does not resolve into the estate".

    This is the predicate every fetch path wants. The structural screen alone
    only ever inspected the *string*: ``http://cdn.attacker.example/logo.png``
    with an ``A`` record pointing at ``100.64.0.5`` (worker4 / prod Postgres)
    passed it, and so did every redirect hop and every sitemap ``<loc>``
    re-checked with the same blind rule. Resolving closes that, and closes it
    identically for the entry URL, the ``Location`` header and the ``<loc>``.

    The lookup runs off the event loop and behind a short-TTL verdict cache
    (see :func:`~src.shared.security.url_validator.validate_fetch_url`), so a
    fan-out over one host's stylesheets costs one query rather than ten.

    Fails closed on an unresolvable name, which is the deliberate inversion of
    the old "delivery will fail naturally at request time" reasoning: delivery
    happens later and from a different network position (in-cluster, where
    split-DNS *does* answer), so the two lookups are not the same question.

    Args:
        url: The URL about to be dereferenced.

    Returns:
        ``True`` if the URL is safe to fetch.
    """
    if not _is_ssrf_safe(url):
        return False
    try:
        await validate_fetch_url(url)
    except (ValueError, TypeError, OSError) as exc:
        logger.debug("SSRF resolving screen rejected %s: %s", url, exc)
        return False
    return True


def _is_ssrf_safe_resolved_blocking(url: str) -> bool:
    """:func:`_is_ssrf_safe_resolved` for a caller already off the event loop.

    Used by the TLS check, which opens a raw socket and is dispatched with
    ``asyncio.to_thread`` by ``SecurityScanner.scan``; a second dispatch would
    be pure overhead.

    Args:
        url: The URL about to be dereferenced.

    Returns:
        ``True`` if the URL is safe to fetch.
    """
    if not _is_ssrf_safe(url):
        return False
    try:
        validate_fetch_url_blocking(url)
    except (ValueError, TypeError, OSError) as exc:
        logger.debug("SSRF resolving screen rejected %s: %s", url, exc)
        return False
    return True


# Imported last to avoid a circular import: ``scanner`` (and its mixins) need
# to import ``_is_ssrf_safe`` from this package.
from src.domains.critique.services.scanners.security_scanner.scanner import (  # noqa: E402
    SecurityScanner,
)

__all__ = [
    "DIRECTORY_LISTING_PATHS",
    "EXPOSED_FILE_CHECKS",
    "PLATFORM_FILE_CHECKS",
    "SSRF_BLOCKED_PREFIXES",
    "UNIVERSAL_FILE_CHECKS",
    "SecurityScanner",
    "_detect_platform",
    "_is_false_positive_file",
    "_is_ssrf_safe",
    "_is_ssrf_safe_resolved",
    "_is_ssrf_safe_resolved_blocking",
    "_sec_finding",
]
