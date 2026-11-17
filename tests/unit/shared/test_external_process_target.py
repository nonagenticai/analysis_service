"""Behaviour of the external-process SSRF chokepoint.

The sibling ``test_external_process_sinks_guarded.py`` proves every spawn *uses*
the chokepoint. This file proves the chokepoint is worth using: that it rejects
what it claims to, and that the Chromium confinement string it produces is the
exact shape that was measured to work.

On the measurements encoded here
--------------------------------
The ``--host-resolver-rules`` behaviour was established against a live
Playwright Chromium driven at an instrumented origin server, not from
documentation:

* ``MAP <host> <ip>,MAP * ~NOTFOUND`` let the target load and blocked a ``302``
  to another hostname, a ``302`` to a bare IP literal, and ``<img>``/``fetch()``
  sub-resources to both -- the server observed only the target request.
* Without the rule, all of those reached the server.
* Rule order is load-bearing: Chromium returns on the first matching ``MAP``,
  so a leading catch-all would swallow the target too.

The delivery shape was established against lighthouse 13.4.1's real
``parseChromeFlags``: the value must be inner-quoted or it truncates at the
first space to a useless bare ``MAP``, and the whole expression must NOT carry
an outer quote pair, because Lighthouse strips one before parsing.

These tests are the regression net for those two findings, which are otherwise
invisible -- a silently-dropped resolver rule looks exactly like a working one.
"""

from __future__ import annotations

import shlex

import pytest

from src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter import _chrome_flags
from src.shared.security.external_process_target import (
    ExternalScanTarget,
    chromium_host_resolver_rules,
    resolve_external_scan_target,
    scanner_subprocess_env,
)
from tests.dns_stub import PUBLIC_ANSWER, TAILNET_ANSWER, DnsStub, install

TARGET = ExternalScanTarget(
    url="https://customer.example.com/pricing",
    hostname="customer.example.com",
    pinned_ip="93.184.216.34",
)


@pytest.fixture
def dns_stub(monkeypatch: pytest.MonkeyPatch) -> DnsStub:
    """Hermetic resolver for both DNS-resolving guards the chokepoint runs."""
    return install(monkeypatch)


class TestChromiumHostResolverRules:
    def test_target_is_pinned_to_the_address_we_resolved(self) -> None:
        rules = chromium_host_resolver_rules(TARGET)
        assert rules.split(",")[0] == "MAP customer.example.com 93.184.216.34"

    def test_everything_else_is_made_unresolvable(self) -> None:
        """The catch-all is what stops redirects, sub-resources and IP literals."""
        assert chromium_host_resolver_rules(TARGET).split(",")[-1] == "MAP * ~NOTFOUND"

    def test_the_catch_all_comes_last(self) -> None:
        """Order is load-bearing -- a leading ``MAP *`` would block the target too."""
        clauses = chromium_host_resolver_rules(TARGET).split(",")
        assert clauses.index("MAP * ~NOTFOUND") == len(clauses) - 1

    def test_a_rebound_address_cannot_be_reached(self) -> None:
        """Pinning is the point: the child cannot re-resolve to somewhere else."""
        rules = chromium_host_resolver_rules(
            ExternalScanTarget(
                url="https://rebind.example.com/",
                hostname="rebind.example.com",
                pinned_ip=PUBLIC_ANSWER,
            )
        )
        assert f"MAP rebind.example.com {PUBLIC_ANSWER}" in rules
        assert TAILNET_ANSWER not in rules


class TestLighthouseChromeFlagDelivery:
    def test_resolver_rules_survive_a_quote_aware_tokenizer(self) -> None:
        """Lighthouse splits this string; unquoted, the rule truncates to ``MAP``.

        ``shlex`` stands in for yargs-parser's tokenizer here -- both respect
        quotes -- and the real behaviour was confirmed against lighthouse 13.4.1.
        """
        tokens = shlex.split(_chrome_flags(TARGET))
        assert f"--host-resolver-rules={chromium_host_resolver_rules(TARGET)}" in tokens

    def test_the_rule_value_is_inner_quoted(self) -> None:
        assert '--host-resolver-rules="MAP customer.example.com' in _chrome_flags(TARGET)

    def test_the_expression_is_not_outer_quoted(self) -> None:
        """Lighthouse strips a wrapping quote pair, which would expose ours."""
        flags = _chrome_flags(TARGET)
        assert not (flags.startswith('"') and flags.endswith('"'))

    def test_headless_and_sandbox_flags_are_preserved(self) -> None:
        tokens = shlex.split(_chrome_flags(TARGET))
        assert {"--headless", "--no-sandbox", "--disable-gpu"} <= set(tokens)


class TestResolveExternalScanTarget:
    async def test_public_target_is_accepted_and_pinned(self, dns_stub: DnsStub) -> None:
        target = await resolve_external_scan_target("https://customer.example.com/pricing")
        assert target.url == "https://customer.example.com/pricing"
        assert target.hostname == "customer.example.com"
        assert target.pinned_ip == PUBLIC_ANSWER

    async def test_public_name_resolving_into_the_tailnet_is_refused(
        self, dns_stub: DnsStub
    ) -> None:
        """The RFC 6598 case a literal-IP-only check waves straight through."""
        dns_stub.map("customer.example.com", TAILNET_ANSWER)
        with pytest.raises(ValueError):
            await resolve_external_scan_target("https://customer.example.com/")

    async def test_estate_internal_name_is_refused(self, dns_stub: DnsStub) -> None:
        with pytest.raises(ValueError):
            await resolve_external_scan_target("https://registry.internal:5000/v2/")

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:6379/",
            "http://127.0.0.1:5432/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.5:5432/",
            "http://[::ffff:100.64.0.5]/",
            "http://192.168.1.1/",
        ],
    )
    async def test_non_public_literals_are_refused(self, dns_stub: DnsStub, url: str) -> None:
        with pytest.raises(ValueError):
            await resolve_external_scan_target(url)

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/"])
    async def test_non_http_schemes_are_refused(self, dns_stub: DnsStub, url: str) -> None:
        with pytest.raises(ValueError):
            await resolve_external_scan_target(url)

    async def test_unresolvable_host_fails_closed(self, dns_stub: DnsStub) -> None:
        """A name we cannot vouch for is not handed to an external process."""
        dns_stub.fail("nxdomain.example.com")
        with pytest.raises(ValueError):
            await resolve_external_scan_target("https://nxdomain.example.com/")


class TestTheDnsStubDoesNotLaunderIpLiterals:
    """The hermetic resolver must not turn a tailnet literal into a public answer.

    ``validate_callback_url`` short-circuits IP literals before any resolver is
    consulted, so the stub was never asked about one. ``validate_public_url``
    does *not* -- it hands ``100.64.0.6`` straight to ``getaddrinfo``. When the
    stub was first extended to cover that second guard it answered literals from
    its name policy, i.e. ``PUBLIC_ANSWER``, silently converting the fleet's own
    registry address into "allowed". Every literal-IP block assertion in the tree
    would then have passed for the wrong reason.
    """

    @pytest.mark.parametrize("literal", [TAILNET_ANSWER, "100.64.0.6", "10.0.0.1", "127.0.0.1"])
    def test_literals_are_echoed_not_replaced(self, dns_stub: DnsStub, literal: str) -> None:
        assert dns_stub.default_answer(literal) == literal

    def test_validate_public_url_still_refuses_a_tailnet_literal(self, dns_stub: DnsStub) -> None:
        from w2v_common.net import SSRFError, validate_public_url

        with pytest.raises(SSRFError):
            validate_public_url("http://100.64.0.6:5000/v2/")

    def test_names_still_follow_the_policy(self, dns_stub: DnsStub) -> None:
        """False-positive control: the literal rule must not shadow names."""
        assert dns_stub.default_answer("registry.internal") == TAILNET_ANSWER
        assert dns_stub.default_answer("customer.example.com") == PUBLIC_ANSWER


class TestScannerSubprocessEnv:
    def test_service_credentials_are_withheld(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A third-party binary running attacker-chosen input gets no secrets."""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h/db"
        )  # pragma: allowlist secret
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("W2V_LICENSE_TOKEN", "tok")  # pragma: allowlist secret
        monkeypatch.setenv("MINIO_SECRET_KEY", "sk")  # pragma: allowlist secret

        env = scanner_subprocess_env()

        assert "DATABASE_URL" not in env
        assert "REDIS_URL" not in env
        assert "W2V_LICENSE_TOKEN" not in env
        assert "MINIO_SECRET_KEY" not in env

    def test_the_tool_can_still_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/analysisservice")
        env = scanner_subprocess_env()
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/analysisservice"

    def test_proxy_variables_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Egress containment is the real fix; do not route the child around it."""
        monkeypatch.setenv("HTTPS_PROXY", "http://egress.example:3128")
        monkeypatch.setenv("no_proxy", "localhost")
        env = scanner_subprocess_env()
        assert env["HTTPS_PROXY"] == "http://egress.example:3128"
        assert env["no_proxy"] == "localhost"
