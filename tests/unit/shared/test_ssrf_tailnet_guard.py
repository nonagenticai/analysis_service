"""RED-proof for the RFC 6598 / IPv4-mapped SSRF hole in this service's guards.

``ipaddress.ip_address("100.64.0.5").is_private`` is **False**: CPython
classifies RFC 6598 shared address space (``100.64.0.0/10``) as neither private
nor reserved. That range is this fleet's entire Tailscale tailnet -- worker4 /
prod Postgres (``100.64.0.5``), worker3 / the Docker registry
(``100.64.0.6``), the k3s control plane (``100.64.0.7``). Any guard built
from an explicit "private networks" list, or from ``not is_private``, waves all
of them straight through.

Both of this service's guards had the hole, independently -- which is the point:

* :func:`src.shared.security.url_validator.validate_callback_url` -- an explicit
  ``_BLOCKED_NETWORKS`` list with the CGNAT /10 simply absent, no IPv4-mapped
  IPv6 unwrap, and a ``socket.gaierror`` branch that logged a warning and
  returned the URL (fail-OPEN).
* ``...scanners.security_scanner._is_ssrf_safe`` -- a second, vendored copy of
  the rules (``not is_private and not is_loopback and not is_link_local``) plus
  a ``startswith`` hostname prefix screen that ``registry.internal`` walks past.

Both are reached from attacker-controlled request fields (the critique/review
``callback_url`` on six routes; every scanner target URL).

Every DNS-touching test here patches ``socket.getaddrinfo``: a real lookup under
this repo's ``-n 4`` xdist is flaky, and ``--reruns 2`` would paper over it.
"""

from __future__ import annotations

import ipaddress
import socket
from unittest.mock import patch

import pytest

from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe
from src.shared.security.url_validator import validate_callback_url

# Live fleet addresses, all inside 100.64.0.0/10.
WORKER4_POSTGRES = "100.64.0.5"
WORKER3_REGISTRY = "100.64.0.6"
MASTER_K3S = "100.64.0.7"

# The shared conformance vector. Identical across every repo carrying this fix.
BLOCKED_IPS = (
    WORKER4_POSTGRES,
    WORKER3_REGISTRY,
    MASTER_K3S,
    "100.64.0.1",
    "::ffff:100.64.0.1",
    "::ffff:10.0.0.1",
    "169.254.169.254",
    "10.0.0.1",
    "127.0.0.1",
    "192.0.0.1",
    "198.18.0.1",
)

ALLOWED_IPS = (
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    # The /10 bracketing addresses. These are the assertions that prove the deny
    # is the CIDR and not `startswith("100.")` -- 100.63.255.255 is the address
    # immediately below the range and 100.128.0.0 the one immediately above.
    "100.63.255.255",
    "100.128.0.0",
)


def _host(ip: str) -> str:
    """Wrap an IPv6 literal in brackets for use in a URL authority."""
    return f"[{ip}]" if ":" in ip else ip


def _resolves_to(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """A ``getaddrinfo`` return value for a name resolving to *ip*."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]


def test_cpython_really_does_not_flag_cgnat_as_private() -> None:
    """The premise of the whole bug class -- assert it, do not trust it."""
    addr = ipaddress.ip_address(WORKER4_POSTGRES)
    assert addr.is_private is False
    assert addr.is_reserved is False
    # ...but it is emphatically not globally routable, which is the right rule.
    assert addr.is_global is False


class TestCallbackUrlConformanceVector:
    """``validate_callback_url`` against the shared vector."""

    @pytest.mark.parametrize("ip", BLOCKED_IPS)
    def test_blocked_literal_rejected(self, ip: str) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_callback_url(f"http://{_host(ip)}/webhook")

    @pytest.mark.parametrize("ip", ALLOWED_IPS)
    def test_allowed_literal_accepted(self, ip: str) -> None:
        """The guard must not become a blanket deny.

        ``100.63.255.255`` / ``100.128.0.0`` bracket the CGNAT /10: if either of
        these is rejected the implementation is prefix-matching ``100.``, not
        testing membership of ``100.64.0.0/10``.
        """
        url = f"https://{_host(ip)}/webhook"
        assert validate_callback_url(url) == url

    @pytest.mark.parametrize(
        "ip",
        ["224.0.0.1", "255.255.255.255", "0.0.0.0", "::1", "fd00::1", "fe80::1"],
    )
    def test_other_non_global_literals_rejected(self, ip: str) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_callback_url(f"http://{_host(ip)}/webhook")

    def test_public_ipv6_literal_still_allowed(self) -> None:
        url = "https://[2606:4700:4700::1111]/webhook"
        assert validate_callback_url(url) == url


class TestCallbackUrlInternalNames:
    """Estate-internal names must die on the NAME, before the resolver runs."""

    @pytest.mark.parametrize(
        "host",
        [
            "registry.internal",
            "postgres.internal",
            "npm.internal",
            "analysis-service.w2v.svc",
            "analysis-service.w2v.svc.cluster.local",
            "somebox.local",
            # Trailing-dot FQDN form: the same name, and it must not bypass.
            "registry.internal.",
            "postgres.internal.",
        ],
    )
    def test_internal_hostname_rejected_before_dns(self, host: str) -> None:
        """Split-DNS makes these resolve to public-*looking* tailnet IPs.

        ``getaddrinfo`` is patched to blow up, proving no DNS round-trip is
        relied upon for the rejection.
        """
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo",
            side_effect=AssertionError("DNS must not be consulted for internal suffixes"),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                validate_callback_url(f"https://{host}/webhook")

    def test_lookalike_public_name_not_over_blocked(self) -> None:
        """The suffix check is label-scoped: ``notinternal.com`` is fine."""
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo",
            return_value=_resolves_to("93.184.216.34"),
        ):
            url = "https://notinternal.com/webhook"
            assert validate_callback_url(url) == url


class TestCallbackUrlDns:
    """Resolution-time behaviour, including the inverted fail-open branch."""

    @pytest.mark.parametrize("ip", [WORKER4_POSTGRES, "10.0.0.1", "::ffff:100.64.0.1"])
    def test_name_resolving_into_the_estate_rejected(self, ip: str) -> None:
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo",
            return_value=_resolves_to(ip),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                validate_callback_url("https://rebind.example.com/webhook")

    def test_public_name_still_allowed(self) -> None:
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo",
            return_value=_resolves_to("93.184.216.34"),
        ):
            url = "https://hooks.example.com/webhook"
            assert validate_callback_url(url) == url

    def test_dns_failure_now_fails_closed(self) -> None:
        """Inverted: a name we cannot resolve is a name we cannot vouch for.

        The old branch logged a warning and RETURNED the URL, reasoning that
        "delivery will fail naturally at request time". It does not: delivery
        runs later and from inside the cluster, where split-DNS answers names
        that do not resolve here.
        """
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                validate_callback_url("https://nonexistent.example.com/webhook")

    def test_empty_resolution_fails_closed(self) -> None:
        """A resolver answering with nothing usable is also a refusal."""
        with patch("src.shared.security.url_validator.socket.getaddrinfo", return_value=[]):
            with pytest.raises(ValueError, match="not allowed"):
                validate_callback_url("https://silent.example.com/webhook")


class TestScannerSsrfSafeConformanceVector:
    """``_is_ssrf_safe`` gates every scanner fetch. Same vector, same verdicts."""

    @pytest.mark.parametrize("ip", BLOCKED_IPS)
    def test_blocked_literal(self, ip: str) -> None:
        assert _is_ssrf_safe(f"http://{_host(ip)}/") is False

    @pytest.mark.parametrize("ip", ALLOWED_IPS)
    def test_allowed_literal(self, ip: str) -> None:
        assert _is_ssrf_safe(f"https://{_host(ip)}/") is True

    @pytest.mark.parametrize("ip", ["224.0.0.1", "192.0.0.1", "198.18.0.1", "::1", "fd00::1"])
    def test_other_non_global_literals_blocked(self, ip: str) -> None:
        assert _is_ssrf_safe(f"http://{_host(ip)}/") is False

    @pytest.mark.parametrize(
        "host",
        [
            "registry.internal",
            "postgres.internal",
            "analysis-service.w2v.svc.cluster.local",
            "worker4.local",
            "registry.internal.",
        ],
    )
    def test_internal_hostname_blocked(self, host: str) -> None:
        """``startswith`` prefix matching never sees these -- suffixes are the rule."""
        assert _is_ssrf_safe(f"https://{host}/v2/_catalog") is False

    @pytest.mark.parametrize("scheme", ["file", "gopher", "ftp", "data"])
    def test_non_http_schemes_blocked(self, scheme: str) -> None:
        assert _is_ssrf_safe(f"{scheme}://example.com/x") is False

    def test_public_url_still_allowed(self) -> None:
        assert _is_ssrf_safe("https://example.com/page") is True
        assert _is_ssrf_safe("http://93.184.216.34/page") is True

    def test_legacy_prefix_screen_retained(self) -> None:
        """Belt-and-braces: the historical prefix deny still applies to names."""
        assert _is_ssrf_safe("http://10.something.com/") is False
