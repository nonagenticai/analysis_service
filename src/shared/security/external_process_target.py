"""SSRF containment for scan targets handed to an **external process**.

Why this module exists — and why the Playwright guard cannot do its job
----------------------------------------------------------------------
:func:`src.shared.security.playwright_guard.install_ssrf_guard` intercepts at
the ``BrowserContext`` route layer. That layer only exists for browsers this
process drives over CDP. Two critique adapters do not drive a browser: they
``asyncio.create_subprocess_exec`` an *external* binary and hand it the
tenant-supplied URL as argv.

* ``lighthouse`` launches **its own** Chromium, which fetches the target, every
  sub-resource on it, and follows every redirect.
* ``nuclei`` actively probes the target with its own Go HTTP client.

No route handler in this process is in that path. The only in-repo levers are
(a) validating and *pinning* the target before the process is spawned and
(b) constraining what the spawned process is permitted to reach.

What each lever actually buys — measured, not assumed
-----------------------------------------------------
**Pre-flight resolve-and-verify** (:func:`resolve_external_scan_target`) runs
the URL through *both* fleet validators — the repo's ``validate_callback_url``
(estate-internal name suffixes, the explicit RFC 6598 ``100.64.0.0/10`` tailnet
block, ``is_global`` backstop, fail-closed DNS) and ``w2v_common``'s
``validate_public_url`` (cloud-metadata names, and it returns the resolved
address). It covers **hop 0 only**. Redirects and sub-resources are decided
inside the external process, where this check has no reach.

**Chromium resolver pinning** (:func:`chromium_host_resolver_rules`) is the
lever that actually closes the rest — for Lighthouse alone, because only
Lighthouse's child is a Chromium. ``--host-resolver-rules`` was measured
against a live Playwright Chromium with an instrumented origin server; with
``MAP <target-host> <pinned-ip>,MAP * ~NOTFOUND`` in place the server observed
**only** the target request, while the same probes without the rule reached it
on all three vectors:

===========================================  ==============  ==============
vector                                       without rule    with rule
===========================================  ==============  ==============
``302`` to a different hostname               reached         ``ERR_NAME_NOT_RESOLVED``
``302`` to a bare **IP literal**              reached         ``ERR_NAME_NOT_RESOLVED``
``<img>`` / ``fetch()`` to another host + IP  reached         ``ERR_NAME_NOT_RESOLVED``
===========================================  ==============  ==============

The IP-literal row is the non-obvious one: Chromium routes literals through
``HostMappingRules`` too, so ``MAP * ~NOTFOUND`` catches ``http://100.64.0.5``
and the class of redirect a name-only allowlist would miss. Pinning the target
host to the address *we* resolved also closes the DNS-rebinding TOCTOU window,
which the in-process route guard does not close.

What is still open, stated plainly
----------------------------------
* **nuclei and ZAP redirects/crawl are NOT contained by anything here.** They
  get hop-0 validation and nothing more. nuclei does not follow redirects
  unless a template opts in; ZAP's spider follows links by design.
* A pre-flight walk of the redirect chain was considered and rejected: an
  attacker who controls the redirector serves one answer to our probe and
  another to the tool, so it defends only against misconfiguration while
  costing a round-trip and implying coverage it does not have.
* The only complete answer for a spawned process is **egress containment at the
  network layer** (NetworkPolicy / forced egress proxy for the pod). That is a
  the fleet deployment configuration change and is deliberately not attempted here. Note
  that :func:`scanner_subprocess_env` preserves ``*_PROXY`` variables precisely
  so such a proxy keeps working once it lands.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from urllib.parse import urlsplit

from w2v_common.net import SSRFError, validate_public_url

from src.shared.security.url_validator import validate_callback_url

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalScanTarget",
    "chromium_host_resolver_rules",
    "resolve_external_scan_target",
    "scanner_subprocess_env",
    "terminate_process_group",
]

# Environment variables a spawned scanner legitimately needs. Everything else --
# DATABASE_URL, REDIS_URL, the MinIO keys, the license token -- is withheld: the
# child is a third-party binary running attacker-chosen input (nuclei fetches its
# own template corpus; Lighthouse loads the target's JavaScript), and none of it
# has any business holding this service's credentials.
_ENV_PASSTHROUGH = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "TZ",
        "NODE_OPTIONS",
        "CHROME_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        # Kept on purpose: if the estate later forces scanner egress through a
        # proxy (the network-layer fix this module cannot make), stripping these
        # would silently route the child around it.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


@dataclass(frozen=True)
class ExternalScanTarget:
    """A scan target that has passed every in-process SSRF check.

    Carrying the verdict in a distinct type -- rather than passing the same
    ``str`` onward -- is what lets
    ``tests/unit/shared/test_external_process_sinks_guarded.py`` tell a
    validated target from a raw tenant string by static analysis alone.

    Attributes:
        url: The validated URL, byte-identical to the input. Deliberately not
            rewritten to the pinned IP: an external tool needs the hostname for
            TLS SNI, certificate verification and virtual-host routing.
        hostname: The host component, lower-cased and trailing-dot stripped.
        pinned_ip: The single address ``hostname`` resolved to at validation
            time. Callers that can constrain their child's resolver should pin
            to exactly this.
    """

    url: str
    hostname: str
    pinned_ip: str


def _resolve_sync(url: str) -> ExternalScanTarget:
    """Validate *url* against both fleet guards and capture the resolved address.

    Both validators are run because neither is a superset of the other:
    ``validate_callback_url`` blocks ``.onion`` / ``.localdomain`` and carries
    the explicit special-purpose network table, while ``validate_public_url``
    blocks the cloud-metadata names and is the only one that hands back the
    address it resolved.

    Args:
        url: Tenant-supplied scan target.

    Returns:
        The validated target, carrying the address to pin to.

    Raises:
        ValueError: The URL is malformed, or fails either guard. ``SSRFError``
            is a ``ValueError`` subclass, so one ``except`` covers both.
    """
    validate_callback_url(url)
    pinned = validate_public_url(url)

    hostname = (urlsplit(url).hostname or "").strip().rstrip(".").lower()
    if not hostname:
        raise ValueError(f"Scan target has no hostname: {url}")
    if pinned is None:
        # Only reachable via ``allow_hosts``, which this module never passes.
        raise SSRFError(f"Scan target {url} was not pinned to an address")

    return ExternalScanTarget(url=url, hostname=hostname, pinned_ip=pinned)


async def resolve_external_scan_target(url: str) -> ExternalScanTarget:
    """Validate a tenant URL before an **external process** is given it.

    Both guards call :func:`socket.getaddrinfo`, which blocks; running them
    inline would stall the event loop for the duration of the lookup, so the
    work is dispatched to a worker thread exactly as
    ``playwright_guard._origin_is_public`` does.

    This closes hop 0. It does **not** constrain what the spawned process does
    afterwards -- see the module docstring for what remains open, and use
    :func:`chromium_host_resolver_rules` where the child is a Chromium.

    Args:
        url: Tenant-supplied scan target.

    Returns:
        The validated target.

    Raises:
        ValueError: The URL fails validation (``SSRFError`` included).
        OSError: A resolver failure that is not ``gaierror``. Callers refuse
            the target on this too -- fail closed, matching the guards.
    """
    return await asyncio.to_thread(_resolve_sync, url)


def chromium_host_resolver_rules(target: ExternalScanTarget) -> str:
    """Build a ``--host-resolver-rules`` value that confines a child Chromium.

    The first rule pins the validated host to the address *this* process
    resolved, so the child cannot re-resolve it to somewhere else. The trailing
    catch-all makes every other name -- and, as measured, every bare IP literal
    -- unresolvable, which is what stops redirects and sub-resources reaching
    the estate.

    Rule order is load-bearing: Chromium's ``HostMappingRules`` returns on the
    first matching ``MAP``, so the specific rule must precede ``MAP *``.

    The value contains spaces. Lighthouse parses ``--chrome-flags`` with
    ``yargs-parser`` (verified against lighthouse 13.4.1), so a caller
    embedding this in a chrome-flags string **must** wrap it in double quotes;
    unquoted, the value truncates at the first space to the useless ``MAP``.

    Args:
        target: A validated target from :func:`resolve_external_scan_target`.

    Returns:
        The rule string, without surrounding quotes.
    """
    return f"MAP {target.hostname} {target.pinned_ip},MAP * ~NOTFOUND"


def scanner_subprocess_env() -> dict[str, str]:
    """Return a minimal environment for a spawned third-party scanner.

    The default for ``create_subprocess_exec`` is to inherit the parent
    environment wholesale, which in this pod means handing a third-party binary
    ``DATABASE_URL``, ``REDIS_URL``, the object-store keys and the license
    token. None of the scanners need any of it.

    Returns:
        The allow-listed subset of :data:`os.environ` (see
        ``_ENV_PASSTHROUGH``), proxy variables included so a future
        network-layer egress proxy is not bypassed.
    """
    return {key: value for key, value in os.environ.items() if key in _ENV_PASSTHROUGH}


async def terminate_process_group(proc: asyncio.subprocess.Process, *, grace: float = 5.0) -> None:
    """Kill a timed-out scanner child and everything it spawned, then reap it.

    ``asyncio.wait_for(proc.communicate(), timeout=...)`` cancels the *await*.
    It sends the child nothing. Both scanners here fork further processes --
    Lighthouse launches a Chromium of its own, nuclei runs a worker pool -- so
    signalling ``proc.pid`` alone would leave those children running with the
    pipes still open. Each slow target therefore used to leak a whole process
    tree until the pod hit its memory limit and was OOMKilled.

    The child must be spawned with ``start_new_session=True`` for this to be
    safe: that puts it in its own process group, which is what makes a
    group-wide signal target the scanner instead of this service. If the
    child's group turns out to be *our* group, this function signals nothing
    and falls back to killing the single process -- taking analysis_service
    down is never an acceptable way to clean up a scan.

    ``SIGTERM`` first so Chromium gets to tear its own children down, escalating
    to ``SIGKILL`` if the group is still alive after *grace* seconds. The child
    is always awaited afterwards; an unawaited child stays a zombie in the pod's
    PID table.

    Args:
        proc: The (possibly still running) child.
        grace: Seconds to wait after ``SIGTERM`` before ``SIGKILL``.
    """
    if proc.returncode is not None:
        return

    pgid: int | None = None
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError, OSError:
        pgid = None

    own_pgid = os.getpgrp()
    if pgid is not None and pgid == own_pgid:
        # The child was not spawned with start_new_session=True. Signalling
        # this group would signal us. Degrade to the single process.
        logger.error(
            "scanner child pid=%s shares this service's process group (%s); "
            "refusing killpg and killing the single process instead",
            proc.pid,
            pgid,
        )
        pgid = None

    def _signal(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.kill() if sig == signal.SIGKILL else proc.terminate()
        except ProcessLookupError, PermissionError, OSError:
            # Already gone, or we lost the right to signal it. Either way there
            # is nothing left to clean up here.
            pass

    _signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except TimeoutError:
        logger.warning(
            "scanner child pid=%s ignored SIGTERM after %.1fs; escalating to SIGKILL",
            proc.pid,
            grace,
        )
    except ProcessLookupError:
        return

    _signal(signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except TimeoutError, ProcessLookupError:
        logger.error("scanner child pid=%s survived SIGKILL; leaving it to the pod", proc.pid)
