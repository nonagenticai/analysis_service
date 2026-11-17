"""SSRF guard for the brand domain's outbound fetches.

Every URL the brand pipeline dereferences is attacker-influenced:

* ``POST /api/brand/from-url`` and ``/api/brand/batch-extract`` take the target
  URL straight off the request body and hand it to a headless Chromium
  (``stages/fetch.py``).
* ``POST /api/brand/logo/download`` takes a URL off the body and ``GET``\\ s it.
* ``POST /api/brands/compare`` with ``extract_if_missing`` synthesises
  ``https://{domain}`` from a tenant-supplied domain and runs the same pipeline.
* The *secondary* fetches (stylesheet ``href``, ``<link rel=manifest>``, logo
  ``src``) are read out of the fetched page, so a merely-public first hop can
  still steer them anywhere -- being reachable is not the same as being trusted.

Which guard, and why this one
-----------------------------
The rules live in :mod:`src.shared.security.url_validator` (RFC 6598 CGNAT /10
== the tailnet, IPv4-in-IPv6 unwrapping, estate-internal name suffixes). This
module reaches them through ``_is_ssrf_safe_resolved``: the structural rules
**and** a DNS check that every address the hostname answers with is public.

It previously used the structural-only ``_is_ssrf_safe``, on the argument that
it "delegates to the same canonical rules". That argument was wrong on the
dimension that decides the outcome. A structural check inspects the URL
*string*, and an attacker who controls a public name simply points its ``A``
record wherever they like::

    http://cdn.attacker.example/logo.png   A -> 100.64.0.5   (a CGNAT address)

That defeated the entry check, and it defeated the per-hop redirect
revalidation too, because re-applying a name-blind predicate on every hop
re-applies the blindness. Resolution is the load-bearing half, and it is not
optional.

The reason resolution was skipped -- ``getaddrinfo`` is blocking and this path
fans out over up to ten stylesheet URLs concurrently -- is real, and is handled
rather than traded away: the lookup runs under ``asyncio.to_thread`` behind a
short-TTL verdict cache, so a fan-out over one host costs one query and the
event loop is never held. See
:func:`~src.shared.security.url_validator.validate_fetch_url`.

Residual, stated rather than hidden:

* **DNS-rebind TOCTOU at the Playwright sink.** ``page.goto`` re-resolves the
  name itself after we have validated it. Pinning the validated address into
  the connection needs browser-level IP pinning or an egress NetworkPolicy.
  (The BrowserContext route guard added separately narrows, but does not close,
  this.)

In scope, and closed by this module: non-HTTP schemes (``file:///etc/passwd``),
estate-internal names (``registry.internal``), literal link-local / RFC 1918 /
CGNAT addresses (``169.254.169.254``, ``10.0.0.1``, ``100.64.0.5``), public
*names* that resolve to any of those, the ``302``-into-the-tailnet redirect,
and a name that will not resolve at all (fail-closed).

Also in scope, and orthogonal to *where* we are allowed to fetch from: *how
much* a permitted host may make us hold. ``get_guarded`` takes an optional
``max_bytes`` and streams under it, because a size check performed after
``client.get`` has already buffered the body cannot prevent the OOM it claims
to prevent.
"""

from __future__ import annotations

import httpx

# Status codes that carry a ``Location`` we would otherwise follow blindly.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

MAX_REDIRECTS = 5

# Headers that describe the *wire* body and stop being true once we have
# decoded and re-framed it -- see ``_materialize``.
_FRAMING_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding"})


class BrandSsrfBlockedError(ValueError):
    """A brand fetch targeted a non-public / unsafe host.

    Subclasses ``ValueError`` deliberately: ``BaseStage.execute`` already treats
    ``ValueError`` as a stage failure (and ``PipelineRunner._execute_stages``
    aborts the whole pipeline when the ``fetch`` stage fails), and the
    ``/brand/logo/download`` route already maps ``ValueError`` to a 400. So the
    block surfaces through paths that exist rather than needing new ones.
    """


class BrandResponseTooLargeError(ValueError):
    """A brand fetch's response body exceeded the byte cap its caller set.

    A *sibling* of :class:`BrandSsrfBlockedError`, not a subclass of it, because
    the two are different verdicts about different things: the SSRF block means
    "we refuse to talk to that host at all" and is a security decision about the
    *target*; this one means "that host answered, and the answer is too big to
    hold" and is a resource decision about the *body*. Folding them together
    would make ``logger.warning("Blocked ... fetch: %s")`` report a size cap as
    an SSRF attempt and vice versa, and would make the SSRF regression suite
    (which asserts ``BrandSsrfBlockedError`` per vector) pass for the wrong
    reason.

    It subclasses ``ValueError`` for the same reason ``BrandSsrfBlockedError``
    does: ``BaseStage.execute`` treats ``ValueError`` as a stage failure and the
    ``/brand/logo/download`` route maps it to a 400, so an oversized body
    degrades through paths that already exist. That also preserves the previous
    behaviour of ``logo_service``'s (post-hoc, useless) size check, which raised
    a plain ``ValueError``.
    """


def _materialize(response: httpx.Response, body: bytes) -> httpx.Response:
    """Rebuild a streamed response as an ordinary, body-carrying one.

    ``httpx`` only exposes ``.content`` / ``.text`` / ``.json()`` on a response
    whose body *it* buffered; a response obtained from ``client.stream`` raises
    ``ResponseNotRead`` for those, and its stream is dead once the ``async
    with`` block exits. Every caller of :func:`get_guarded` wants the buffered
    API (logo bytes, stylesheet text, manifest JSON), so once the body has been
    read under a cap we hand back an equivalent response carrying it. This is
    the same trick ``w2v_common.net.safe_fetch`` uses; the technique is copied
    rather than the module imported, because brand deliberately runs its own
    guard (see the module docstring).

    The framing headers are dropped rather than copied: ``aiter_bytes`` yields
    *decoded* bytes, so an upstream ``Content-Length`` / ``Content-Encoding``
    describes the wire body and not ``body``. Copying them would leave the
    rebuilt response lying about itself (a gzip ``Content-Encoding`` on
    already-inflated bytes makes ``.text`` undecodable downstream).

    Args:
        response: The streamed response to copy status, headers and request from.
        body: The decoded bytes read from that response, already size-checked.

    Returns:
        A response equivalent to *response* whose ``.content`` is *body*.
    """
    headers = [
        (key, value)
        for key, value in response.headers.multi_items()
        if key.lower() not in _FRAMING_HEADERS
    ]
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=body,
        request=response.request,
        extensions=response.extensions,
        history=response.history,
    )


async def _get_capped(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
) -> httpx.Response:
    """``GET`` *url* streaming, refusing to hold more than *max_bytes* of body.

    The point of the cap is that it has to be enforced *while* the body arrives.
    ``client.get`` reads the whole body into memory before it returns, so a
    check on ``len(response.content)`` afterwards is decorative -- the pod has
    already paid for every byte a hostile origin chose to send. Here the running
    total is checked per chunk and the ``async with`` is left (closing the
    connection) the moment it goes over, so peak memory is bounded by
    *max_bytes* plus one chunk.

    A ``30x`` is returned unread: the hop decision needs only the status and the
    ``Location`` header, and a redirect's body is noise. That keeps redirect
    handling in :func:`get_guarded` identical whether or not a cap is set.

    Args:
        client: The client to issue the request on.
        url: The already-validated URL for this hop.
        max_bytes: Maximum number of decoded body bytes to accept.

    Returns:
        A materialised response (see :func:`_materialize`).

    Raises:
        BrandResponseTooLargeError: if the body exceeds *max_bytes*, or if the
            origin declares a ``Content-Length`` that already exceeds it.
    """
    async with client.stream("GET", url, follow_redirects=False) as response:
        if response.status_code in _REDIRECT_STATUSES:
            return _materialize(response, b"")

        # Cheap pre-check: a declared length is the *wire* length, so if it is
        # already over the cap the decoded body cannot be under it. Refusing
        # here means not reading a single byte of an origin that announced it
        # is oversized.
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise BrandResponseTooLargeError(
                f"response from {url} declares {declared} bytes, exceeds maximum {max_bytes} bytes"
            )

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise BrandResponseTooLargeError(
                    f"response body from {url} exceeds maximum {max_bytes} bytes"
                )
            chunks.append(chunk)
        return _materialize(response, b"".join(chunks))


def ensure_public_url(url: str) -> None:
    """Raise :class:`BrandSsrfBlockedError` unless *url* passes the no-DNS screen.

    A **partial** check with, deliberately, **no production callers left** --
    every brand fetch path now goes through :func:`ensure_public_url_resolved`.
    It survives as the directly-testable structural half: the vectors it does
    decide (non-HTTP schemes, estate-internal names, literal RFC 1918 / CGNAT /
    link-local addresses) are worth asserting on their own, without a resolver
    in the picture.

    Do not reach for it in new code. On its own it is blind to a public name
    with an internal ``A`` record, which is the bypass the resolving form
    exists to close; if you cannot ``await``, that is a signal the call site is
    on the wrong side of the sync/async boundary, not a reason to use this.

    Args:
        url: The URL to screen.

    Raises:
        BrandSsrfBlockedError: if the URL fails the structural screen.
    """
    # Lazy import: the security_scanner package pulls in the critique scanner
    # mixins, which must not become a load-time dependency of the brand domain.
    # ``scorecards/fetch.py`` imports it the same way for the same reason.
    from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe

    if not _is_ssrf_safe(url):
        raise BrandSsrfBlockedError(f"refusing to fetch non-public/unsafe URL: {url}")


async def ensure_public_url_resolved(url: str) -> None:
    """Raise :class:`BrandSsrfBlockedError` unless *url* is safe to dereference.

    The full screen -- structural rules plus DNS -- and the one every brand
    fetch path uses. See the module docstring for why the structural half alone
    was not a guard.

    Args:
        url: The URL to screen.

    Raises:
        BrandSsrfBlockedError: if the URL fails the screen, including when its
            hostname resolves to a non-public address or does not resolve at
            all (fail-closed).
    """
    from src.domains.critique.services.scanners.security_scanner import (
        _is_ssrf_safe_resolved,
    )

    if not await _is_ssrf_safe_resolved(url):
        raise BrandSsrfBlockedError(f"refusing to fetch non-public/unsafe URL: {url}")


async def get_guarded(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int | None = None,
) -> httpx.Response:
    """``GET`` *url* through *client*, re-validating the target on every hop.

    ``follow_redirects=False`` is forced per-request (it overrides whatever the
    client was constructed with, which matters for the shared app-level client),
    and each ``Location`` is resolved against the current URL and re-checked
    before it is followed. Auto-following would let a public URL that ``302``\\ s
    to ``http://169.254.169.254/latest/meta-data/`` walk straight past a
    one-shot check of the URL the tenant supplied.

    *max_bytes* closes the other half of the problem. Deciding *who* we may talk
    to says nothing about *how much* they may send: a host that passes the SSRF
    screen can still answer a "logo" request with an unbounded body and OOM the
    pod. When a cap is given, the body is streamed and abandoned the moment the
    running total exceeds it (:func:`_get_capped`); when it is ``None`` the
    request is the plain buffered ``client.get`` it has always been, so callers
    that have not opted in keep their exact previous semantics.

    Args:
        client: The client to issue the request on.
        url: The URL to fetch. Validated before the first byte is sent.
        max_redirects: Maximum number of ``30x`` hops to follow.
        max_bytes: Maximum number of decoded body bytes to accept, or ``None``
            for the historical unbounded read.

    Returns:
        The final (non-redirect) response, with ``.content`` available.

    Raises:
        BrandSsrfBlockedError: if any hop is unsafe, if a redirect arrives
            without a ``Location``, or if the redirect chain exceeds
            *max_redirects*.
        BrandResponseTooLargeError: if *max_bytes* is set and the body exceeds it.
    """
    await ensure_public_url_resolved(url)
    current = url
    for _hop in range(max_redirects + 1):
        if max_bytes is None:
            response = await client.get(current, follow_redirects=False)
        else:
            response = await _get_capped(client, current, max_bytes)
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("location")
        if not location:
            raise BrandSsrfBlockedError(f"redirect without Location from {current}")
        current = str(response.url.join(location))
        await ensure_public_url_resolved(current)
    raise BrandSsrfBlockedError(f"too many redirects (> {max_redirects}) fetching {url}")
