"""SSRF protection for user-supplied callback URLs.

Validates that callback URLs do not point to private/internal IP ranges,
localhost, or non-HTTP schemes. Resolves DNS to check resolved IPs.

Why the rules are shaped the way they are
-----------------------------------------
``ipaddress`` is not a security oracle. ``ip_address("100.64.0.5")`` reports
``is_private=False`` **and** ``is_reserved=False``, because RFC 6598 shared
address space (``100.64.0.0/10``) is neither. On this fleet that /10 is the
entire Tailscale tailnet: worker4 / bare-metal prod Postgres
(``100.64.0.5``), worker3 / the Docker registry (``100.64.0.6``) and the
k3s control plane (``100.64.0.7``). Any guard written as "block the private
ranges" therefore declares the whole estate a legitimate callback target.

``is_global`` is the correct discriminator and is the load-bearing rule here:
it is false for every IANA special-purpose range, including the /10 above, the
IETF protocol assignments (``192.0.0.0/24``), benchmarking (``198.18.0.0/15``),
multicast and reserved space. The explicit ``_BLOCKED_NETWORKS`` list is kept
as a second opinion and as documentation of intent.

Two further vectors this module closes:

* **IPv4 smuggled inside an IPv6 form.** ``::ffff:10.0.0.1`` matches none of
  the IPv6 networks and is not ``is_private``; the embedded v4 address is
  unwrapped and the whole rule set re-applied to the address traffic reaches.
* **Estate-internal names.** ``registry.internal`` is rejected on the *name*,
  before the resolver is consulted, because tailnet split-DNS hands back a
  public-*looking* CGNAT address and ``*.svc.cluster.local`` resolves only from
  inside the pod network.

DNS-rebinding TOCTOU: closable, but only by the caller. Validating a name says
nothing about where the *connection* goes if the HTTP client resolves the name
again at delivery time -- an attacker controlling the authoritative server
answers the two lookups differently, and re-validating every redirect hop just
re-opens the same window per hop. :func:`resolve_fetch_pin` therefore returns
the approved address and :func:`pin_target` rewrites the request onto it
(hostname preserved for ``Host`` and TLS SNI). Any fetch path that calls only
:func:`validate_fetch_url` and then hands the *hostname* to a transport is still
exposed; the streaming fetchers use the pinning pair.

Two entry points, one rule set
------------------------------
:func:`validate_callback_url` is the blocking form, for the one-shot
callback/webhook targets that are validated on a request thread.
:func:`validate_fetch_url` is the ``async`` form, for the *fetch* paths (brand
logo/CSS/manifest, the critique scanners, the scorecard fetchers) that run
inside the event loop and fan out over many sub-resources. They apply exactly
the same rules -- the async one only moves ``getaddrinfo`` off the loop and
consults a short-lived verdict cache. There is deliberately no third rule set:
the reason the CGNAT hole survived so long is that two copies of "the rules"
existed and only one of them was fixed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: How long a resolution verdict stays usable. Short on purpose: the cache
#: exists to stop one brand extraction resolving the same host once per
#: stylesheet/icon/logo, not to memoise DNS. A longer TTL would widen the
#: rebinding window the module docstring already declares open.
_RESOLUTION_TTL_SECONDS = 30.0

#: Hard cap on cache entries. A scan of a hostile sitemap can name thousands of
#: distinct hosts; without a bound the cache is an unbounded attacker-controlled
#: allocation. On overflow the cache is cleared outright rather than evicted
#: cleverly -- correctness here does not depend on any entry surviving.
_RESOLUTION_CACHE_MAX = 2048

#: ``hostname -> (expires_at_monotonic, failure_message_or_None, approved_addresses)``.
#: The addresses ride along so a pinning caller reuses this lookup instead of
#: opening a second one (which would be a second, independently-answerable query
#: -- the very window pinning exists to close).
_resolution_cache: dict[str, tuple[float, str | None, tuple[str, ...]]] = {}


def clear_resolution_cache() -> None:
    """Drop every cached resolution verdict.

    Exists for tests: a hermetic resolver that answers ``evil.example.com`` into
    the tailnet for one test and publicly for the next would otherwise be
    defeated by a verdict cached across the two. ``tests/dns_stub.install``
    calls this.
    """
    _resolution_cache.clear()


# Explicit deny list. ``is_global`` below already covers every one of these; the
# list is retained so the intent is readable and so a future change to CPython's
# classification cannot silently widen the guard. Entries marked (!) are the
# ones a "private ranges" list habitually omits -- and the (!) on the CGNAT /10
# is the live production hole this module was rewritten to close.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # --- IPv4 ---
    ipaddress.ip_network("0.0.0.0/8"),  # "this network" / unspecified
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),  # (!) RFC 6598 shared == Tailscale tailnet
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local (cloud metadata 169.254.169.254)
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),  # (!) RFC 6890 IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),  # (!) RFC 2544 benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # (!) multicast
    ipaddress.ip_network("240.0.0.0/4"),  # (!) reserved, incl. 255.255.255.255 broadcast
    # --- IPv6 ---
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("::1/128"),  # loopback
    ipaddress.ip_network("64:ff9b::/96"),  # (!) RFC 6052 NAT64 well-known prefix
    ipaddress.ip_network("64:ff9b:1::/48"),  # (!) RFC 8215 NAT64 local-use prefix
    ipaddress.ip_network("100::/64"),  # discard-only
    ipaddress.ip_network("2001:db8::/32"),  # documentation
    ipaddress.ip_network("fc00::/7"),  # unique-local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # (!) multicast
)

# Exact hostnames that always mean "this machine".
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

# Hostname labels that only ever name estate-internal services. Matched as whole
# label suffixes (``h == suffix or h.endswith("." + suffix)``) so ``notinternal``
# is not caught while ``registry.internal`` is, and the name is ``rstrip(".")``ed
# first so the trailing-dot FQDN form ``registry.internal.`` cannot bypass.
_BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    "internal",
    "local",
    "svc",
    "cluster.local",
    "localdomain",
    "localhost",
    "onion",
)


def _embedded_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address smuggled inside an IPv6 form, or ``None``.

    ``::ffff:100.64.0.1`` is the tailnet wearing an IPv6 hat -- the v6 address
    itself is neither private nor reserved, so the v4 rules must be applied to
    the address it actually reaches. Covers IPv4-mapped, 6to4, Teredo and the
    NAT64 prefixes.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    for candidate in (ip.ipv4_mapped, ip.sixtofour):
        if candidate is not None:
            return candidate
    if ip.teredo is not None:
        # (server, client) -- the client address is the one traffic reaches.
        return ip.teredo[1]
    if any(ip in net for net in _BLOCKED_NETWORKS if net.version == 6):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True unless *ip* is a globally-routable public address.

    Rejects private, loopback, link-local, multicast, reserved and unspecified,
    plus the ranges ``ipaddress`` leaves unflagged but that reach the estate
    (see ``_BLOCKED_NETWORKS``). An IPv4 address wrapped in an IPv6 form is
    unwrapped and re-checked against the whole rule set.
    """
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        return is_blocked_ip(embedded)

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if any(ip in net for net in _BLOCKED_NETWORKS if net.version == ip.version):
        return True
    # Backstop: ``is_global`` already excludes shared address space and the IANA
    # special-purpose registry, so anything it rejects is not a callback target.
    return not ip.is_global


def is_blocked_hostname(hostname: str) -> bool:
    """True if *hostname* names an estate-internal host (checked without DNS)."""
    host = hostname.strip().rstrip(".").lower()
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES:
        return True
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _BLOCKED_HOST_SUFFIXES)


def parse_literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse *hostname* as a literal IP, tolerating the ``[::1]`` bracket form."""
    candidate = hostname.strip().strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _resolve_public_addresses(hostname: str) -> list[str]:
    """Resolve *hostname*, raise unless **every** answer is public, return them all.

    Every answer, not the first: a multi-``A`` record whose second address is
    ``100.64.0.5`` is an attack, and ``getaddrinfo`` makes no promise about
    ordering. The loop below has no early ``break`` for that reason.

    Fails CLOSED: a name we cannot resolve is a name we cannot vouch for. The
    previous implementation let any unresolvable host through on the theory that
    "delivery will fail naturally at request time", but delivery runs later and
    from a different network position (in-cluster, where split-DNS *does*
    answer), so the two lookups are not the same question -- and the optimistic
    answer is not the safe one.

    The addresses are **returned**, not merely judged, because a caller that is
    about to open a connection must be able to PIN one of them. Throwing them
    away is what left the fetch paths open to DNS rebinding: the transport then
    resolved the name a second time, independently, and could get a different
    (internal) answer than the one this function approved. There is deliberately
    still only one rule set -- :func:`_assert_resolves_public` is a thin wrapper
    over this, not a second copy.

    Args:
        hostname: The name to resolve.

    Returns:
        Every resolved address, in ``getaddrinfo`` order, all verified public.

    Raises:
        ValueError: if resolution fails, yields nothing usable, or yields any
            non-public address.
    """
    try:
        resolved_ips = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        logger.warning("DNS resolution failed for callback URL host: %s", hostname)
        raise ValueError(
            f"Callback URL host '{hostname}' could not be resolved: not allowed"
        ) from exc

    checked: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in resolved_ips:
        # Strip any IPv6 zone id before parsing.
        addr = parse_literal_ip(str(sockaddr[0]).split("%", 1)[0])
        if addr is None:
            continue
        if is_blocked_ip(addr):
            raise ValueError(
                f"Callback URL '{hostname}' resolves to private IP '{addr}': not allowed"
            )
        checked.append(str(addr))

    if not checked:
        raise ValueError(f"Callback URL host '{hostname}' could not be resolved: not allowed")
    return checked


def _assert_resolves_public(hostname: str) -> None:
    """:func:`_resolve_public_addresses`, for a caller that only wants the verdict.

    Args:
        hostname: The name to resolve.

    Raises:
        ValueError: exactly as :func:`_resolve_public_addresses`.
    """
    _resolve_public_addresses(hostname)


def _resolve_public_addresses_cached(hostname: str) -> list[str]:
    """:func:`_resolve_public_addresses`, memoised for ``_RESOLUTION_TTL_SECONDS``.

    Both verdicts are cached, not just the allow: a brand extraction that fans
    out over ten stylesheets on a blocked host should pay for one lookup, and a
    scan of a hostile sitemap should not be able to turn each ``<loc>`` into a
    fresh outbound DNS query.

    The approved **addresses** are cached alongside the verdict so the pinning
    callers share the same single lookup rather than opening a second one. A
    cached address is safe to pin: it was approved by the same rules, and the
    TTL is the same rebinding exposure the cache already carried.

    Args:
        hostname: The name to resolve.

    Returns:
        The approved addresses, in ``getaddrinfo`` order.

    Raises:
        ValueError: with the same message the uncached call would have raised.
    """
    now = time.monotonic()
    cached = _resolution_cache.get(hostname)
    if cached is not None and cached[0] > now:
        if cached[1] is not None:
            raise ValueError(cached[1])
        return list(cached[2])

    addresses: tuple[str, ...] = ()
    try:
        addresses = tuple(_resolve_public_addresses(hostname))
    except ValueError as exc:
        failure: str | None = str(exc)
        raise
    else:
        failure = None
    finally:
        # Bound the cache before inserting, so the insert cannot be the entry
        # that overflows it.
        if len(_resolution_cache) >= _RESOLUTION_CACHE_MAX:
            _resolution_cache.clear()
        _resolution_cache[hostname] = (now + _RESOLUTION_TTL_SECONDS, failure, addresses)
    return list(addresses)


def _assert_resolves_public_cached(hostname: str) -> None:
    """:func:`_resolve_public_addresses_cached`, for a caller that wants the verdict.

    Args:
        hostname: The name to resolve.

    Raises:
        ValueError: exactly as :func:`_resolve_public_addresses_cached`.
    """
    _resolve_public_addresses_cached(hostname)


def _validate_url_structure(url: str) -> str | None:
    """Apply every rule that needs no resolver, and say what is left to check.

    Splitting this out is what keeps the blocking and ``async`` entry points on
    one rule set instead of two -- the shape that produced the original CGNAT
    defect.

    Args:
        url: The URL to validate.

    Returns:
        The hostname still requiring a DNS check, or ``None`` when the URL was
        an IP literal and is therefore already fully decided.

    Raises:
        ValueError: If the URL fails any structural check.
    """
    if not url or not url.strip():
        raise ValueError("Callback URL is empty")

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError(f"Invalid URL: {exc}") from exc

    # Scheme check
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}': only http/https allowed")

    # Host check
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    hostname_lower = hostname.lower()
    if hostname_lower.rstrip(".") in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Callback to '{hostname}' is not allowed (localhost)")

    # Direct IP literal -- validated without touching the resolver.
    literal = parse_literal_ip(hostname_lower)
    if literal is not None:
        if is_blocked_ip(literal):
            raise ValueError(f"Callback to private IP '{hostname}' is not allowed")
        return None

    # Estate-internal names are refused on the NAME, before the resolver is
    # consulted -- split-DNS would otherwise hand back a public-looking CGNAT
    # address, and an in-cluster name that does not resolve from this host would
    # otherwise land in the DNS-failure branch below.
    if is_blocked_hostname(hostname_lower):
        raise ValueError(f"Callback to internal host '{hostname}' is not allowed")

    return hostname


def validate_fetch_url_blocking(url: str) -> str:
    """:func:`validate_fetch_url` for a caller that is already off the event loop.

    The scanner's TLS check opens a raw socket and is dispatched with
    ``asyncio.to_thread`` by its caller; wrapping its guard in a *second*
    ``to_thread`` would be pure overhead. Same rules, same cache, no dispatch.

    Args:
        url: The URL about to be dereferenced.

    Returns:
        The validated URL string.

    Raises:
        ValueError: If the URL fails any safety check.
    """
    hostname = _validate_url_structure(url)
    if hostname is not None:
        _assert_resolves_public_cached(hostname)
    return url


async def validate_fetch_url(url: str) -> str:
    """``async`` :func:`validate_callback_url`, for URLs we are about to fetch.

    Identical rules, including the DNS resolution and the fail-closed behaviour
    on an unresolvable name. The difference is only *where the lookup runs*:
    ``getaddrinfo`` is blocking, and these callers are inside the event loop on
    paths that fan out over stylesheets, icons, sitemap entries and redirect
    hops, so a direct call would stall every other coroutine in the process for
    the duration of the query. It is dispatched to a worker thread, and a
    short-TTL verdict cache keeps a fan-out over one host to a single lookup.

    An IP-literal URL never touches the resolver or the thread pool -- it is
    fully decided by the structural rules, so the common
    ``http://169.254.169.254/`` case still costs nothing.

    Args:
        url: The URL about to be dereferenced.

    Returns:
        The validated URL string.

    Raises:
        ValueError: If the URL fails any safety check, including a DNS answer
            that contains a non-public address and a name that will not resolve.
    """
    hostname = _validate_url_structure(url)
    if hostname is not None:
        await asyncio.to_thread(_assert_resolves_public_cached, hostname)
    return url


async def resolve_fetch_pin(url: str) -> str:
    """:func:`validate_fetch_url`, but return the IP the caller must CONNECT to.

    Same rules, same cache, same fail-closed behaviour -- the only difference is
    that the approved address is handed back instead of discarded. That is the
    whole fix for DNS rebinding on the fetch paths: a validator that returns a
    bool leaves the transport to resolve the name a *second* time, and an
    attacker who controls the authoritative server can answer the two lookups
    differently (public to the guard, ``10.0.0.7``/``169.254.169.254``/tailnet to
    httpx). Re-validating every redirect hop does not help, because each hop is
    re-validated with the same non-pinning shape.

    Pair with :func:`pin_target`, which turns the returned address into the
    request the transport should actually issue.

    An **IP literal** URL is returned as itself: the structural rules already
    decided it, no resolver is consulted, and pinning it is a no-op that keeps
    the call site branch-free.

    Args:
        url: The URL about to be fetched.

    Returns:
        The address to pin the connection to.

    Raises:
        ValueError: If the URL fails any safety check, including a DNS answer
            containing a non-public address and a name that will not resolve.
    """
    hostname = _validate_url_structure(url)
    if hostname is None:
        parsed = urlparse(url)
        return (parsed.hostname or "").strip("[]")
    addresses = await asyncio.to_thread(_resolve_public_addresses_cached, hostname)
    # First answer: the address the transport would have tried first anyway, now
    # fixed so the connect step cannot re-resolve to a different one. Every
    # address in the answer was checked, so which one is chosen is not a
    # security decision.
    return addresses[0]


def pin_target(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return ``(request_url, headers, extensions)`` for an IP-pinned request.

    The connection target host is rewritten to the literal *ip* while the
    original hostname is preserved for the ``Host`` header and for TLS
    (``sni_hostname`` drives both the ClientHello SNI and httpx's certificate
    hostname verification), so a pinned ``https`` fetch still verifies the
    certificate against the hostname -- not the IP -- and name-based virtual
    hosts still serve the right site.

    Mirrors ``w2v_common.net.safe_fetch._pin`` (and its in-repo twin in
    ``app_inspector_service``) so every pinned fetch on this fleet pins
    identically. It is a separate helper rather than a call into
    ``safe_fetch`` because the streaming, byte-capped fetch paths must keep
    their own loop: ``safe_get``/``safe_request`` buffer the whole body, so
    routing a capped stream through them would trade an SSRF bug for a memory
    blow-up.

    Args:
        url: The URL as written (hostname intact).
        ip: The address :func:`resolve_fetch_pin` approved.

    Returns:
        The rewritten request URL, the headers to merge in, and the httpx
        request extensions to pass.
    """
    parsed = urlparse(url)
    host_bracket = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host_bracket}:{parsed.port}" if parsed.port is not None else host_bracket
    if parsed.username:
        auth = parsed.username + (f":{parsed.password}" if parsed.password else "")
        netloc = f"{auth}@{netloc}"
    pinned_url = urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    # Trailing root dot stripped: RFC 6066 forbids it in the TLS SNI extension.
    hostname = (parsed.hostname or "").rstrip(".").lower()
    host_disp = f"[{hostname}]" if ":" in hostname else hostname
    host_header = f"{host_disp}:{parsed.port}" if parsed.port is not None else host_disp
    return pinned_url, {"Host": host_header}, {"sni_hostname": hostname}


def validate_callback_url(url: str) -> str:
    """Validate a callback URL for SSRF safety.

    Checks:
        - URL is well-formed
        - Scheme is http or https
        - Host is not localhost or an estate-internal name (checked pre-DNS)
        - Host IP is not in a private/internal/non-global range
        - DNS resolves, and every resolved IP is public

    Blocking. Use :func:`validate_fetch_url` from inside the event loop.

    Args:
        url: The callback URL to validate.

    Returns:
        The validated URL string.

    Raises:
        ValueError: If the URL fails any safety check.
    """
    hostname = _validate_url_structure(url)
    if hostname is not None:
        # DNS resolution check -- fails closed, see the helper.
        _assert_resolves_public(hostname)
    return url
