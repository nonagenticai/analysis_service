"""Hermetic, in-process DNS for tests that cross the SSRF guard.

WHY THIS EXISTS
---------------
``src.shared.security.url_validator.validate_callback_url`` fails **CLOSED** on
DNS failure: a name it cannot resolve is a name it cannot vouch for. That
inversion is deliberate and load-bearing — delivery happens later and from a
different network position (in-cluster, where tailnet split-DNS *does* answer
``registry.internal`` and friends), so the old "it will fail naturally at request
time" reasoning was simply false.

The price of that correctness is paid by every test that used a deliberately
non-resolving placeholder host, the moment the tenant-supplied *scan target*
``url`` was routed through the same guard as the callback URL:

* ``https://example.test/`` — RFC 6761 reserves ``.test`` precisely so it never
  resolves. Used throughout ``tests/unit/domains/critique/test_auth_info.py``.
* ``https://acme-critique-integration.example.com/`` and siblings — invented
  subdomains of ``example.com`` with no A record.
* ``https://example.com`` — this one *does* resolve, but only by making a real
  DNS query from the test process. Under this repo's ``-n 4`` xdist that is a
  flake source, and ``--reruns 2`` would mask it rather than surface it.

The correct repair is a hermetic resolver, NOT looser production rules and NOT
rewriting the placeholder hosts to real domains.

WHERE THE PATCH LANDS, AND WHY NOT DEEPER
-----------------------------------------
``url_validator`` does ``import socket`` and then calls
``socket.getaddrinfo(...)``, so the obvious target is
``src.shared.security.url_validator.socket.getaddrinfo``. That attribute path
reaches through to the *process-global* ``socket`` module and would therefore
also hijack resolution for psycopg (the real Postgres the integration tests
need), httpx, and everything else sharing the interpreter.

So :func:`install` rebinds the module-level name ``url_validator.socket`` to a
stub exposing exactly the four attributes the validator uses — ``getaddrinfo``,
``gaierror``, ``AF_UNSPEC``, ``SOCK_STREAM``. Tests reaching for the
conventional ``patch("src.shared.security.url_validator.socket.getaddrinfo",
...)`` still work; that just sets the attribute on the stub.

``w2v_common.net.safe_fetch`` is rebound the same way and to the *same* stub
instance, because it is the second DNS-resolving guard in the estate: it backs
``validate_public_url``, which ``playwright_guard`` and
``external_process_target`` both call. Leaving it on the real resolver would
mean a test that looks hermetic still makes a live query — and, worse, that
``dns_stub.map(...)`` would be honoured by one guard and ignored by the other,
so a rebinding assertion could pass for the wrong reason. Blast radius is
still exactly two modules; ``socket`` itself is never touched, so psycopg and
httpx keep the real resolver.

RESOLUTION POLICY
-----------------
1. An explicit per-test override (:meth:`DnsStub.map` / :meth:`DnsStub.fail`)
   always wins.
2. A hostname carrying an estate-internal DNS *label* (``internal``, ``svc``,
   ``registry``, ``postgres``, ...) resolves into ``100.64.0.0/10`` — the RFC
   6598 shared range that IS this fleet's Tailscale tailnet, and which
   ``ipaddress`` reports as neither ``is_private`` nor ``is_reserved``. A guard
   built from a "private networks" list waves every one of those addresses
   through, so asserting the rejection is the entire point: these must never be
   handed a public answer by the stub.
3. Everything else resolves to one fixed public address. Placeholder hosts are
   the overwhelming majority of the affected suites and none of them asserts a
   block — the block assertions in this tree use IP literals or estate-internal
   *names*, and the validator rejects both of those before the resolver is ever
   consulted.

Consequence when writing new tests: a case that needs "a public-*looking* name
whose DNS answer points into the estate" — the bypass a literal-IP-only check
misses — must say so explicitly with ``dns_stub.map(host, TAILNET_ANSWER)``.
Default-public will not do it for you, by design.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable

import pytest
from w2v_common.net import safe_fetch

from src.shared.security import url_validator

# A stable, unambiguously public answer — the same value the fleet conformance
# vector in ``tests/unit/shared/test_ssrf_tailnet_guard.py`` uses for "allowed".
PUBLIC_ANSWER = "93.184.216.34"

# Bare-metal production Postgres on the tailnet. Inside 100.64.0.0/10, and the
# address the whole RFC 6598 defect class is named after on this fleet.
TAILNET_ANSWER = "100.64.0.5"

# Whole DNS labels that only ever name something inside the estate. Compared
# label-wise, never as substrings, so ``notinternal.com`` is not caught.
INTERNAL_LABELS = frozenset(
    {
        "cluster",
        "internal",
        "localdomain",
        "localhost",
        "master",
        "npm",
        "pgbouncer",
        "postgres",
        "pypiserver",
        "registry",
        "svc",
        "worker1",
        "worker2",
        "worker3",
        "worker4",
    }
)

AddrInfo = tuple[int, int, int, str, tuple[str, int]]


class DnsStub:
    """A stand-in for the ``socket`` module as ``url_validator`` uses it.

    Exposes only the four attributes the validator touches. Every answer is
    synthesised in-process; no query ever leaves the test runner.

    Attributes:
        AF_UNSPEC: Forwarded from the real ``socket`` module.
        SOCK_STREAM: Forwarded from the real ``socket`` module.
        gaierror: The real exception type, so the validator's
            ``except socket.gaierror`` still catches what this stub raises.
    """

    AF_UNSPEC = socket.AF_UNSPEC
    SOCK_STREAM = socket.SOCK_STREAM
    gaierror = socket.gaierror

    def __init__(self) -> None:
        """Start with no overrides, so every host follows the default policy."""
        self._answers: dict[str, list[str] | None] = {}
        self.observer: Callable[[str, int], None] | None = None

    def observe(self, callback: Callable[[str, int], None]) -> None:
        """Call *callback* with ``(hostname, thread_ident)`` on every lookup.

        Two properties of the *guard* -- not of this stub -- are only visible
        from inside the resolver, and neither survives the obvious alternative
        of monkeypatching :meth:`getaddrinfo`, which would replace the thing
        under observation:

        * **which thread resolved.** ``getaddrinfo`` blocks, so a guard that
          calls it inline stalls the whole event loop. The thread id is the only
          direct evidence that the lookup was dispatched off-loop.
        * **how many lookups a fan-out costs.** Ten stylesheets on one host must
          collapse to one query via the verdict cache; counting calls here is
          the only way to say so without asserting on the cache's internals.

        Args:
            callback: Receives the normalised hostname and the resolving
                thread's :func:`threading.get_ident` value.
        """
        self.observer = callback

    def map(self, hostname: str, ip: str) -> None:
        """Pin *hostname* to *ip* for the remainder of the test.

        Args:
            hostname: Host to override; case and trailing dot are normalised.
            ip: Literal IPv4 or IPv6 address to answer with.
        """
        self._answers[_normalise(hostname)] = [ip]

    def map_many(self, hostname: str, ips: list[str]) -> None:
        """Pin *hostname* to a **multi-answer** record, in the order given.

        A real ``getaddrinfo`` returns every address a name carries and makes no
        promise about their order, so a guard that inspects only the first entry
        is defeated by putting the public address first. Order is preserved here
        precisely so a test can assert both arrangements.

        Args:
            hostname: Host to override; case and trailing dot are normalised.
            ips: Literal addresses to answer with, in answer order.
        """
        self._answers[_normalise(hostname)] = list(ips)

    def fail(self, hostname: str) -> None:
        """Make *hostname* unresolvable, exercising the fail-closed branch.

        Args:
            hostname: Host whose lookup should raise ``socket.gaierror``.
        """
        self._answers[_normalise(hostname)] = None

    @staticmethod
    def default_answer(host: str) -> str:
        """Return the policy answer for a host with no explicit override.

        An IP **literal** is echoed back unchanged, exactly as the real
        ``getaddrinfo`` does. This is load-bearing, not a nicety:
        ``validate_callback_url`` short-circuits literals before it reaches a
        resolver, but ``validate_public_url`` does not -- it hands
        ``100.64.0.6`` straight to ``getaddrinfo``. A stub that answered that
        with ``PUBLIC_ANSWER`` would convert the fleet's own tailnet addresses
        into "allowed" and quietly disarm every literal-IP block assertion in
        the tree.

        Args:
            host: Already-normalised hostname or IP literal.

        Returns:
            *host* itself when it is an IP literal; ``TAILNET_ANSWER`` for
            estate-internal-looking names; otherwise ``PUBLIC_ANSWER``.
        """
        try:
            return str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            pass
        if set(host.split(".")) & INTERNAL_LABELS:
            return TAILNET_ANSWER
        return PUBLIC_ANSWER

    def getaddrinfo(
        self,
        host: str | None,
        port: str | int | None,
        family: int = 0,
        type: int = 0,  # noqa: A002 — why: positional-signature parity with socket.getaddrinfo
        proto: int = 0,
        flags: int = 0,
    ) -> list[AddrInfo]:
        """Resolve *host* from this stub's policy table.

        Args:
            host: Hostname to resolve.
            port: Ignored; present for signature parity with ``socket``.
            family: Ignored; present for signature parity with ``socket``.
            type: Ignored; present for signature parity with ``socket``.
            proto: Ignored; present for signature parity with ``socket``.
            flags: Ignored; present for signature parity with ``socket``.

        Returns:
            A single-entry, ``getaddrinfo``-shaped answer list.

        Raises:
            socket.gaierror: If the host was explicitly marked unresolvable.
        """
        key = _normalise(host or "")
        if self.observer is not None:
            self.observer(key, threading.get_ident())
        ips = self._answers[key] if key in self._answers else [self.default_answer(key)]
        if ips is None:
            raise socket.gaierror(f"stubbed NXDOMAIN for {host!r}")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ips
        ]


def _normalise(hostname: str) -> str:
    """Lower-case a hostname and strip the trailing-dot FQDN form."""
    return hostname.strip().rstrip(".").lower()


def install(monkeypatch: pytest.MonkeyPatch) -> DnsStub:
    """Rebind both DNS-resolving SSRF guards to one fresh :class:`DnsStub`.

    The *same* instance is installed on ``url_validator`` and on
    ``w2v_common.net.safe_fetch`` so a single ``map``/``fail`` override is
    honoured by ``validate_callback_url`` and ``validate_public_url`` alike —
    ``external_process_target`` runs both against the same URL and would
    otherwise get two different answers.

    The fetch-path validators memoise their verdicts (see
    ``url_validator._RESOLUTION_TTL_SECONDS``), and that cache is module-global,
    so it outlives the ``monkeypatch`` undo. Without the clear below, a test
    that pinned ``rebind.example.com`` into the tailnet would leave a *blocked*
    verdict behind for the next test that maps the same name publicly — and the
    failure would land on whichever test happened to run second under ``-n 4``.
    Clearing on install (rather than on teardown) also covers the entry path:
    the very first test in a worker starts from an empty cache regardless of
    what imported the module.

    Args:
        monkeypatch: The test's monkeypatch fixture; undoes the rebinds on
            teardown.

    Returns:
        The installed stub, so the caller can pin specific answers.
    """
    url_validator.clear_resolution_cache()
    stub = DnsStub()
    monkeypatch.setattr(url_validator, "socket", stub)
    monkeypatch.setattr(safe_fetch, "socket", stub)
    return stub
