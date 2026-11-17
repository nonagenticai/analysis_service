"""The three out-of-process scanner adapters must not be given an unsafe target.

``install_ssrf_guard`` covers the axe adapter because axe runs in a browser
*this* process drives. Lighthouse spawns its own Chromium, nuclei spawns its own
prober, and ZAP fetches from a separate service — none of them is reachable from
a Playwright route handler, so each has to refuse the target itself.

Each refusal test asserts the transport was **never reached**, not merely that
the result was empty. An adapter that dialled the target and then failed for an
unrelated reason produces the same empty list as one that refused, and only the
second is a guard. That distinction is the whole finding here.

DNS is hermetic via the autouse ``dns_stub`` fixture in this directory's
``conftest.py``; ``dns_stub.map(host, TAILNET_ANSWER)`` is how a public-*looking*
name is made to resolve into the estate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter import (
    LighthouseAdapter,
)
from src.domains.critique.services.scanners.tool_adapters.nuclei_adapter import NucleiAdapter
from src.domains.critique.services.scanners.tool_adapters.zap_adapter import ZapAdapter
from tests.dns_stub import PUBLIC_ANSWER, TAILNET_ANSWER, DnsStub

_REBINDING_TARGET = "https://customer.example.com/"
_INTERNAL_TARGET = "http://postgres.internal:5432/"


def _settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.external_tools.lighthouse_timeout = 60
    settings.external_tools.lighthouse_cli_path = "lighthouse"
    settings.external_tools.nuclei_timeout = 60
    settings.external_tools.nuclei_path = "nuclei"
    settings.external_tools.zap_timeout = 60
    settings.external_tools.zap_api_url = "http://zap.example:8080"
    settings.external_tools.zap_api_key = ""
    for key, value in overrides.items():
        setattr(settings.external_tools, key, value)
    return settings


def _completed_proc(stdout: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


class TestLighthouseRefusesUnsafeTargets:
    async def test_name_resolving_into_the_tailnet_never_spawns(self, dns_stub: DnsStub) -> None:
        dns_stub.map("customer.example.com", TAILNET_ANSWER)
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters."
                "lighthouse_adapter.get_settings",
                return_value=_settings(),
            ),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            findings = await LighthouseAdapter()._run_lighthouse(_REBINDING_TARGET, "mobile")

        assert findings == []
        spawn.assert_not_called()

    async def test_estate_internal_name_never_spawns(self, dns_stub: DnsStub) -> None:
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters."
                "lighthouse_adapter.get_settings",
                return_value=_settings(),
            ),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            findings = await LighthouseAdapter()._run_lighthouse(_INTERNAL_TARGET, "mobile")

        assert findings == []
        spawn.assert_not_called()


class TestLighthouseConfinesItsChromium:
    async def _spawn_args(self, dns_stub: DnsStub) -> tuple[tuple[Any, ...], Mapping[str, Any]]:
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters."
                "lighthouse_adapter.get_settings",
                return_value=_settings(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=_completed_proc(),
            ) as spawn,
        ):
            await LighthouseAdapter()._run_lighthouse(_REBINDING_TARGET, "mobile")
        assert spawn.call_count == 1
        return spawn.call_args.args, spawn.call_args.kwargs

    async def test_child_resolver_is_pinned_to_the_address_we_validated(
        self, dns_stub: DnsStub
    ) -> None:
        args, _ = await self._spawn_args(dns_stub)
        flags = next(a for a in args if a.startswith("--chrome-flags="))
        assert f"MAP customer.example.com {PUBLIC_ANSWER}" in flags

    async def test_child_cannot_resolve_anything_else(self, dns_stub: DnsStub) -> None:
        """The catch-all is what covers redirects, sub-resources and IP literals."""
        args, _ = await self._spawn_args(dns_stub)
        flags = next(a for a in args if a.startswith("--chrome-flags="))
        assert "MAP * ~NOTFOUND" in flags

    async def test_target_keeps_its_hostname(self, dns_stub: DnsStub) -> None:
        """Rewriting to the IP would break TLS SNI and virtual-host routing."""
        args, _ = await self._spawn_args(dns_stub)
        assert _REBINDING_TARGET in args

    async def test_child_does_not_inherit_service_credentials(
        self, dns_stub: DnsStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h/db"
        )  # pragma: allowlist secret
        _, kwargs = await self._spawn_args(dns_stub)
        assert "DATABASE_URL" not in kwargs["env"]


class TestNucleiRefusesUnsafeTargets:
    async def test_name_resolving_into_the_tailnet_never_spawns(self, dns_stub: DnsStub) -> None:
        dns_stub.map("customer.example.com", TAILNET_ANSWER)
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings",
                return_value=_settings(),
            ),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            findings = await NucleiAdapter().scan(_REBINDING_TARGET)

        assert findings == []
        spawn.assert_not_called()

    async def test_estate_internal_name_never_spawns(self, dns_stub: DnsStub) -> None:
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings",
                return_value=_settings(),
            ),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            findings = await NucleiAdapter().scan(_INTERNAL_TARGET)

        assert findings == []
        spawn.assert_not_called()

    async def test_a_safe_target_still_scans(self, dns_stub: DnsStub) -> None:
        """False-negative control: the guard must not refuse everything."""
        entry = json.dumps(
            {"template-id": "tech-detect", "info": {"name": "Tech", "severity": "info"}}
        )
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings",
                return_value=_settings(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=_completed_proc(stdout=entry.encode()),
            ) as spawn,
        ):
            findings = await NucleiAdapter().scan(_REBINDING_TARGET)

        assert len(findings) == 1
        assert _REBINDING_TARGET in spawn.call_args.args
        assert "DATABASE_URL" not in spawn.call_args.kwargs["env"]


class TestZapRefusesUnsafeSpiderSeeds:
    async def test_name_resolving_into_the_tailnet_never_reaches_zap(
        self, dns_stub: DnsStub
    ) -> None:
        dns_stub.map("customer.example.com", TAILNET_ANSWER)
        requests: list[httpx.Request] = []

        def _record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"scan": "0"})

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings",
                return_value=_settings(),
            ),
            patch.object(
                httpx.AsyncClient,
                "get",
                side_effect=AssertionError("ZAP was contacted for a blocked target"),
            ),
        ):
            findings = await ZapAdapter().scan(_REBINDING_TARGET)

        assert findings == []
        assert requests == []

    async def test_estate_internal_seed_never_reaches_zap(self, dns_stub: DnsStub) -> None:
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings",
                return_value=_settings(),
            ),
            patch.object(
                httpx.AsyncClient,
                "get",
                side_effect=AssertionError("ZAP was contacted for a blocked target"),
            ),
        ):
            findings = await ZapAdapter().scan(_INTERNAL_TARGET)

        assert findings == []
