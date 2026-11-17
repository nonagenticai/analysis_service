"""Per-hop SSRF-guarded ``GET`` for the critique security scanner.

Why this exists
---------------
``SecurityScanner.scan`` validates the job URL once, at entry
(``scanner.py``). That single check does not hold for the requests the scan
then makes, for two independent reasons:

* **The URL changes.** ``_probe_exposed_file`` and ``_check_directory_listing``
  re-derive their targets with ``urljoin`` from the scheme/netloc of the
  original, so the thing actually dereferenced is not the string that was
  screened.
* **Redirects.** Three sinks ran ``follow_redirects=True``. A perfectly public
  host that the entry check admits can answer ``302 Location:
  http://169.254.169.254/latest/meta-data/`` -- or, on this fleet, anything in
  the RFC 6598 ``100.64.0.0/10`` tailnet, where prod Postgres
  (``100.64.0.5``), the registry (``100.64.0.6``) and the k3s control
  plane (``100.64.0.7``) live. ``ipaddress.is_private`` is ``False`` for all
  of it, which is precisely how that range stayed reachable for so long.

So the check has to move to the connection, and repeat on every hop.
:func:`_walk_guarded` is the one place that loop lives; :func:`guarded_get` and
:func:`guarded_head` are thin method bindings over it, and the ``httpx`` sinks
in this package -- plus the two SEO live scanners, which had the identical
entry-only screen -- call them instead of ``client.get`` / ``client.head``.
Keeping the walk in one function is what makes the resolving screen below reach
``HEAD`` as well as ``GET`` for free: a second, hand-copied loop is exactly how
one of the two methods ends up screened and the other not. ``tls_checks`` cannot
use them -- it opens a raw ``socket`` rather than speaking HTTP -- and therefore
carries its own guard.

Which predicate, and why
------------------------
``_is_ssrf_safe_resolved`` from this package: the structural rules of
:mod:`src.shared.security.url_validator` **and** a DNS check that every address
the name answers with is public.

The structural half alone is not a guard, and per-hop re-application did not
make it one. It only ever inspected the URL *string*, so
``http://cdn.attacker.example/logo.png`` with an ``A`` record pointing at
``100.64.0.5`` walked through the entry check, through every redirect hop,
and through the sitemap ``<loc>`` screen -- re-applying a blind predicate on
each hop re-applies the blindness. An attacker-controlled ``A`` record costs
nothing and defeated the whole mechanism.

The lookup is dispatched with ``asyncio.to_thread`` and sits behind a short-TTL
verdict cache, so the reason the structural-only version was chosen -- not
putting a blocking ``getaddrinfo`` on the event loop in an async scanner --
no longer requires giving up resolution.

Failing soft, on purpose
------------------------
Every check in this scanner degrades on network trouble: it logs and returns no
finding, and the scan continues. A blocked target is not a more serious event
than an unreachable one from the scan's point of view, so
:class:`ScannerSsrfBlockedError` subclasses ``OSError`` -- the exception type
each of these call sites already catches. That makes the block degrade through
the ``except`` clauses that exist rather than needing new ones, which is also
why adding the guard cannot turn a scannable site into a crashed job.

How much, as well as where from
-------------------------------
Deciding *who* the scanner may talk to says nothing about *how much* they may
make it hold. Every origin this package dereferences is chosen by the tenant,
and two of the sinks go on to read the whole body (``_is_false_positive_file``
inspects ``resp.text``; ``_check_directory_listing`` lowercases it looking for
"index of"). ``client.get`` buffers the entire body *before* it returns, so a
hostile origin answering a 5 kB ``.env`` probe with an endless stream OOMs the
pod, and a length check written after the ``await`` cannot prevent it -- the
bytes are already resident. :func:`guarded_get` therefore streams under a byte
cap. The cap is on **by default** rather than opt-in: there is no sink in this
package with a legitimate need for an unbounded read, and an opt-in cap would
leave the next sink to be written unprotected and the omission invisible.

:func:`guarded_head` is the one binding *without* a ``max_bytes``, which is an
exception to "capped by default" and so is justified rather than assumed: a
response to ``HEAD`` carries no body by HTTP framing, and ``httpx`` enforces
that against the origin instead of trusting it. Measured, not asserted -- an
origin answering ``HEAD`` with ``Content-Length: 41943040`` *and* 40 MB of body
bytes yields ``len(response.content) == 0``, and the send fails server-side with
``BrokenPipeError`` because the client never reads a body the method does not
permit. There is nothing on that path for a cap to bound, so a ``max_bytes``
parameter there would be a knob that does nothing -- precisely the "looks wired
without being wired" failure the paragraph above rejects.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx

# Status codes that carry a ``Location`` we would otherwise follow blindly.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Default hop budget. Matches ``_check_redirect_chain``'s own loop bound so the
#: two agree on what counts as a pathological chain.
MAX_REDIRECTS = 10

#: Default body cap, in decoded bytes. Everything this package fetches is an
#: HTML page or a small text probe (``.env``, ``.git/config``, a directory
#: index); 2 MiB is far above any of those and far below anything that
#: threatens the pod, several of which are in flight concurrently. It is a
#: module constant rather than a ``ScannerSettings`` field because that class is
#: one of the legacy/inert ones (see the service CLAUDE.md) -- adding a knob
#: there would look wired without being wired.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Headers that describe the *wire* body and stop being true once we have
# decoded and re-framed it -- see ``_materialize``.
_FRAMING_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding"})


class ScannerSsrfBlockedError(OSError):
    """A scanner fetch, or one of its redirect hops, targeted an unsafe host.

    Subclasses ``OSError`` deliberately. Each sink in this package already
    catches ``OSError`` and degrades to "no finding for this check", so a block
    surfaces exactly the way a refused connection does and the surrounding scan
    keeps running. Callers that want to say something louder (``scanner.scan``
    emits a ``sec-ssrf-blocked`` info finding) catch this type explicitly,
    before their broader ``OSError`` clause.
    """


class ScannerResponseTooLargeError(OSError):
    """A scanner fetch's response body exceeded the byte cap.

    A *sibling* of :class:`ScannerSsrfBlockedError` rather than a subclass,
    because the two are different verdicts about different things: the SSRF
    block is a security decision about the *target* ("we refuse to talk to that
    host at all"); this is a resource decision about the *body* ("that host
    answered, and the answer is too big to hold"). Folding them together would
    make ``scanner.scan`` -- which catches ``ScannerSsrfBlockedError`` ahead of
    its broad ``OSError`` clause specifically to emit a ``sec-ssrf-blocked``
    finding -- report an oversized homepage as an attempted SSRF, and would let
    the SSRF regression suite pass for the wrong reason.

    It subclasses ``OSError`` for the same reason its sibling does: every sink
    in this package already catches ``OSError`` and degrades to "no finding for
    this check". So capping a body can turn a check into a no-op, but it can
    never crash a scan job. Falling *through* to ``scan``'s generic
    ``sec-scan-error`` finding is the intended outcome for the entry fetch: an
    origin that answers the top-level request with megabytes is a fact worth
    surfacing, and mislabelling it as an SSRF block would not be.
    """


def _is_ssrf_safe(url: str) -> bool:
    """Proxy to the package-level ``_is_ssrf_safe``, resolved at call time.

    Deferred rather than imported at module scope for two reasons: this module
    is loaded *during* the package ``__init__`` (before the predicate is bound
    on the package namespace), and looking it up per call keeps the existing
    tests that patch ``...security_scanner.urlparse`` effective on this path.

    Args:
        url: The URL to screen.

    Returns:
        ``True`` if the URL is safe to dereference.
    """
    from src.domains.critique.services.scanners import security_scanner as _pkg

    return _pkg._is_ssrf_safe(url)


async def _is_ssrf_safe_resolved(url: str) -> bool:
    """Proxy to the package-level ``_is_ssrf_safe_resolved``, resolved at call time.

    Deferred for the same two reasons as :func:`_is_ssrf_safe`: this module is
    imported *during* the package ``__init__``, and a per-call lookup keeps the
    existing monkeypatching of the package namespace effective on this path.

    Args:
        url: The URL to screen.

    Returns:
        ``True`` if the URL is safe to dereference.
    """
    from src.domains.critique.services.scanners import security_scanner as _pkg

    return await _pkg._is_ssrf_safe_resolved(url)


def _is_ssrf_safe_resolved_blocking(url: str) -> bool:
    """Proxy to the package-level blocking resolving screen, resolved at call time.

    Same deferral rationale as :func:`_is_ssrf_safe`. Exists for ``tls_checks``,
    whose frame is already off the event loop (``SecurityScanner.scan``
    dispatches it with ``asyncio.to_thread``), so a second dispatch would be
    pure overhead.

    Args:
        url: The URL to screen.

    Returns:
        ``True`` if the URL is safe to dereference.
    """
    from src.domains.critique.services.scanners import security_scanner as _pkg

    return _pkg._is_ssrf_safe_resolved_blocking(url)


def ensure_safe_url(url: str) -> None:
    """Raise unless *url* passes the structural (no-DNS) SSRF screen.

    A **partial** check with **no production callers left**: ``guarded_get``
    and every scanner now use :func:`ensure_safe_url_resolved`. Retained only as
    the directly-testable structural half. Do not reach for it in new code --
    on its own it is blind to a public name with an internal ``A`` record.

    Args:
        url: The URL about to be fetched.

    Raises:
        ScannerSsrfBlockedError: if the URL fails the SSRF screen.
    """
    if not _is_ssrf_safe(url):
        raise ScannerSsrfBlockedError(f"refusing to fetch non-public/unsafe URL: {url}")


async def ensure_safe_url_resolved(url: str) -> None:
    """Raise unless *url* is safe for this scanner to dereference.

    The full screen: structural rules **plus** "and the name does not resolve
    into the estate". This is the one the fetch path must use; a URL that only
    passes :func:`ensure_safe_url` has had its string inspected and nothing more.

    Args:
        url: The URL about to be fetched.

    Raises:
        ScannerSsrfBlockedError: if the URL fails the SSRF screen, including
            when its hostname resolves to a non-public address or does not
            resolve at all (fail-closed).
    """
    if not await _is_ssrf_safe_resolved(url):
        raise ScannerSsrfBlockedError(f"refusing to fetch non-public/unsafe URL: {url}")


def _materialize(response: httpx.Response, body: bytes) -> httpx.Response:
    """Rebuild a streamed response as an ordinary, body-carrying one.

    ``httpx`` only exposes ``.content`` / ``.text`` / ``.json()`` on a response
    whose body *it* buffered; one obtained from ``client.stream`` raises
    ``ResponseNotRead`` for those, and its stream is dead once the ``async
    with`` block exits. Callers of :func:`guarded_get` read ``.text`` and
    ``.headers`` and pass the object around freely, so once the body has been
    read under a cap we hand back an equivalent response carrying it. The same
    trick is used by ``src/domains/brand/security.py`` and by
    ``w2v_common.net.safe_fetch``; it is copied rather than imported because
    brand's version raises brand's exception type and belongs to another domain.

    The framing headers are dropped rather than copied: ``aiter_bytes`` yields
    *decoded* bytes, so an upstream ``Content-Length`` / ``Content-Encoding``
    describes the wire body and not *body*. Copying them would leave the rebuilt
    response lying about itself -- a ``gzip`` ``Content-Encoding`` on
    already-inflated bytes makes ``.text`` undecodable downstream, which for
    this package would silently turn a real directory-listing finding into no
    finding.

    Args:
        response: The streamed response to copy status, headers and request from.
        body: The decoded bytes read from that response, already size-checked.

    Returns:
        A response equivalent to *response* whose ``.content`` is *body*.
    """
    kept = [
        (key, value)
        for key, value in response.headers.multi_items()
        if key.lower() not in _FRAMING_HEADERS
    ]
    return httpx.Response(
        status_code=response.status_code,
        headers=kept,
        content=body,
        request=response.request,
        extensions=response.extensions,
        history=response.history,
    )


async def _get_capped(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    headers: dict[str, str] | None,
) -> httpx.Response:
    """``GET`` *url* streaming, refusing to hold more than *max_bytes* of body.

    The whole point of the cap is that it is enforced *while* the body arrives.
    ``client.get`` reads the body into memory before it returns, so a check on
    ``len(response.content)`` afterwards is decorative -- the pod has already
    paid for every byte a hostile origin chose to send. Here the running total
    is checked per chunk and the ``async with`` is left (closing the connection)
    the moment it goes over, so peak memory is bounded by *max_bytes* plus one
    chunk.

    A ``30x`` is returned **unread**: the hop decision in :func:`_walk_guarded`
    needs only the status and the ``Location`` header, and a redirect body is
    noise. That keeps the per-hop redirect walk byte-for-byte identical to what
    it was before the cap existed, including its "return the last response
    rather than raise" endings.

    Args:
        client: The client to issue the request on.
        url: The already-screened URL for this hop.
        max_bytes: Maximum number of decoded body bytes to accept.
        headers: Optional per-request headers, passed through unchanged.

    Returns:
        A materialised response (see :func:`_materialize`).

    Raises:
        ScannerResponseTooLargeError: if the body exceeds *max_bytes*, or if the
            origin declares a ``Content-Length`` that already exceeds it.
    """
    async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
        if response.status_code in _REDIRECT_STATUSES:
            return _materialize(response, b"")

        # Cheap pre-check: a declared length is the *wire* length, so if it is
        # already over the cap the decoded body cannot be under it. Refusing
        # here means not reading a single byte from an origin that has
        # announced it is oversized.
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise ScannerResponseTooLargeError(
                f"response from {url} declares {declared} bytes, exceeds maximum {max_bytes} bytes"
            )

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ScannerResponseTooLargeError(
                    f"response body from {url} exceeds maximum {max_bytes} bytes"
                )
            chunks.append(chunk)
        return _materialize(response, b"".join(chunks))


async def _walk_guarded(
    send: Callable[[str], Awaitable[httpx.Response]],
    url: str,
    max_redirects: int,
) -> httpx.Response:
    """Drive *send* along the redirect chain, screening every hop first.

    The single redirect loop in this package. *send* issues one request against
    one already-screened URL and must not follow redirects itself; keeping it a
    parameter is what lets ``GET`` and ``HEAD`` share this walk instead of
    growing a second, subtly-different copy of it. Two consequences are worth
    stating, because both are the kind of thing a hand-copied second loop loses:

    * the **resolving** screen (:func:`ensure_safe_url_resolved`) applies to
      ``HEAD`` exactly as it does to ``GET``, without either being remembered
      separately;
    * the byte cap stays per-*hop* rather than per-*call* -- the ``GET``
      binding's *send* is :func:`_get_capped`, and this loop calls it again for
      every ``Location`` it follows, so hop 7 is bounded exactly as hop 0 is.

    A redirect that arrives without a ``Location``, and a chain that outruns
    *max_redirects*, both end the walk by returning the last response rather
    than raising. That is the pre-existing behaviour of these checks (a ``30x``
    simply is not a ``200``, so no finding is produced) and it keeps a merely
    badly-configured site from being reported as a blocked one.

    Args:
        send: Issues one non-redirect-following request for a given URL.
        url: The first URL. Screened before the first byte is sent.
        max_redirects: Maximum number of ``30x`` hops to follow.

    Returns:
        The final response: the first non-redirect one, or the last one seen
        when the hop budget or the ``Location`` header runs out.

    Raises:
        ScannerSsrfBlockedError: if the initial URL or a redirect target fails
            the resolving SSRF screen.
    """
    current_url = url
    response: httpx.Response | None = None

    for _hop in range(max_redirects + 1):
        await ensure_safe_url_resolved(current_url)
        response = await send(current_url)

        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("location", "")
        if not location:
            return response
        current_url = urljoin(current_url, location)

    if response is None:  # pragma: no cover - the loop always runs at least once
        raise ScannerSsrfBlockedError(f"no response obtained for {url}")
    return response


async def guarded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = MAX_RESPONSE_BYTES,
) -> httpx.Response:
    """``GET`` *url* through *client*, re-validating the target on every hop.

    ``follow_redirects=False`` is forced per request, overriding whatever the
    client was constructed with: the guarantee this function makes has to hold
    for the connection it actually opens, not for the constructor call a future
    edit might change. Each ``Location`` is resolved against the current URL
    and screened again before it is followed, because a one-shot check admits
    the first host and says nothing about where that host points next.

    A redirect that arrives without a ``Location``, and a chain that outruns
    *max_redirects*, both end the walk by returning the last response rather
    than raising. That is the pre-existing behaviour of these checks (a ``30x``
    simply is not a ``200``, so no finding is produced) and it keeps a merely
    badly-configured site from being reported as a blocked one.

    *max_bytes* closes the orthogonal half of the problem: screening *who* may
    be dereferenced says nothing about *how much* they may send. A host that
    passes the SSRF screen can still answer a probe with an unbounded body, and
    ``resp.text`` at the call site would hold all of it. The body is therefore
    streamed and abandoned the moment the running total exceeds the cap
    (:func:`_get_capped`). It defaults to :data:`MAX_RESPONSE_BYTES` rather than
    to ``None`` so a sink is protected by *existing* rather than by remembering;
    passing ``None`` restores the historical unbounded buffered read and should
    need a reason.

    Args:
        client: The client to issue the request on.
        url: The URL to fetch. Screened before the first byte is sent.
        max_redirects: Maximum number of ``30x`` hops to follow. ``0`` fetches
            exactly one URL and never follows.
        headers: Optional per-request headers, passed through unchanged.
        max_bytes: Maximum number of decoded body bytes to accept, or ``None``
            for an unbounded buffered read.

    Returns:
        The final response, per :func:`_walk_guarded`: the first non-redirect
        one, or the last one seen when the hop budget or the ``Location`` header
        runs out.

    Raises:
        ScannerSsrfBlockedError: if the initial URL or a redirect target fails
            the SSRF screen. Being an ``OSError``, this is caught by the
            fail-soft ``except`` clause each call site already has.
        ScannerResponseTooLargeError: if *max_bytes* is set and the body exceeds
            it. Also an ``OSError``, and fail-soft for the same reason.
    """

    async def _send(target: str) -> httpx.Response:
        if max_bytes is not None:
            return await _get_capped(client, target, max_bytes, headers)
        if headers is None:
            return await client.get(target, follow_redirects=False)
        return await client.get(target, headers=headers, follow_redirects=False)

    return await _walk_guarded(_send, url, max_redirects)


async def guarded_head(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """``HEAD`` *url* through *client*, re-validating the target on every hop.

    The ``HEAD`` twin of :func:`guarded_get`, for the broken-link probe in the
    SEO internal-linking scanner. A cheap status probe is still a request: it
    opens a connection to whatever the ``Location`` chain names, and the status
    code it returns is itself the signal an attacker reads back out of the
    findings, so it needs the same per-hop resolving screen a body fetch does --
    and gets it from the same :func:`_walk_guarded` loop, not from a copy.

    There is no *max_bytes* here, and that is a decision rather than an
    oversight: a ``HEAD`` response carries no body to cap. See the module
    docstring for the measurement behind that claim.

    Args:
        client: The client to issue the request on.
        url: The URL to probe. Screened before the first byte is sent.
        max_redirects: Maximum number of ``30x`` hops to follow.
        headers: Optional per-request headers, passed through unchanged.

    Returns:
        The final response, per :func:`_walk_guarded`.

    Raises:
        ScannerSsrfBlockedError: if the initial URL or a redirect target fails
            the resolving SSRF screen.
    """

    async def _send(target: str) -> httpx.Response:
        if headers is None:
            return await client.head(target, follow_redirects=False)
        return await client.head(target, headers=headers, follow_redirects=False)

    return await _walk_guarded(_send, url, max_redirects)
