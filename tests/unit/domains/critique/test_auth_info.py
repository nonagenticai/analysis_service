"""Tests for AuthInfo schema validation + _fetch_html auth application."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.domains.critique.schemas import AuthInfo, CritiqueRequest


class TestAuthInfoValidation:
    def test_none_default_no_required_fields(self) -> None:
        ai = AuthInfo()
        assert ai.type == "none"
        assert ai.username is None
        assert ai.password is None

    def test_basic_requires_username_password(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AuthInfo(type="basic", username="alice")  # missing password
        assert "type=basic requires" in str(exc.value)

    def test_basic_accepts_empty_password(self) -> None:
        # password="" is a valid (if foolish) password; the validator only
        # rejects the None case because the wizard sometimes omits it.
        ai = AuthInfo(type="basic", username="alice", password="")
        assert ai.username == "alice"
        assert ai.password == ""

    def test_bearer_requires_token(self) -> None:
        with pytest.raises(ValidationError):
            AuthInfo(type="bearer")
        ai = AuthInfo(type="bearer", token="abc")
        assert ai.token == "abc"

    def test_cookies_requires_at_least_one_form(self) -> None:
        with pytest.raises(ValidationError):
            AuthInfo(type="cookies")
        ai = AuthInfo(type="cookies", cookies=[{"name": "session", "value": "xyz"}])
        assert ai.cookies == [{"name": "session", "value": "xyz"}]
        ai2 = AuthInfo(type="cookies", cookie_header="session=xyz; csrf=abc")
        assert ai2.cookie_header == "session=xyz; csrf=abc"

    def test_critique_request_carries_auth_info(self) -> None:
        req = CritiqueRequest(
            url="https://example.test/",
            auth_info=AuthInfo(type="bearer", token="ABC"),
        )
        assert req.auth_info is not None
        assert req.auth_info.type == "bearer"
        # serialises round-trip for persistence in the JSONB column
        dumped = req.auth_info.model_dump(exclude_none=True)
        assert dumped == {"type": "bearer", "token": "ABC"}


class TestFetchHtmlAuthApplication:
    """Verify _fetch_html constructs the httpx client with the right
    auth/headers/cookies for each AuthInfo type.

    We don't run the actual GET (network would be needed); we instead
    inspect the httpx.AsyncClient + Request shape via a transport mock.
    """

    @pytest.mark.asyncio
    async def test_no_auth_info_sends_no_auth(self) -> None:
        import httpx

        from src.domains.critique.services.critique_service.service import CritiqueService

        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["cookies"] = dict(request.headers).get("cookie", "")
            return httpx.Response(200, content=b"<html></html>")

        # Patch httpx.AsyncClient inside the helpers module to use our
        # MockTransport so no real network happens.
        from unittest.mock import patch

        from src.domains.critique.services.critique_service import helpers as helpers_mod

        class _PatchedClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        with patch.object(helpers_mod.httpx, "AsyncClient", _PatchedClient):
            svc = CritiqueService.__new__(CritiqueService)
            html = await svc._fetch_html("https://example.test/", auth_info=None)

        assert html == "<html></html>"
        assert "authorization" not in {k.lower() for k in captured["headers"]}
        assert captured["cookies"] == ""

    @pytest.mark.asyncio
    async def test_bearer_sets_authorization_header(self) -> None:
        import httpx

        from src.domains.critique.services.critique_service.service import CritiqueService

        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=b"<html></html>")

        from unittest.mock import patch

        from src.domains.critique.services.critique_service import helpers as helpers_mod

        class _PatchedClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        with patch.object(helpers_mod.httpx, "AsyncClient", _PatchedClient):
            svc = CritiqueService.__new__(CritiqueService)
            await svc._fetch_html(
                "https://example.test/",
                auth_info={"type": "bearer", "token": "tok-123"},
            )
        assert captured["headers"]["authorization"] == "Bearer tok-123"

    @pytest.mark.asyncio
    async def test_basic_sets_basic_auth_header(self) -> None:
        import base64

        import httpx

        from src.domains.critique.services.critique_service.service import CritiqueService

        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=b"<html></html>")

        from unittest.mock import patch

        from src.domains.critique.services.critique_service import helpers as helpers_mod

        class _PatchedClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        with patch.object(helpers_mod.httpx, "AsyncClient", _PatchedClient):
            svc = CritiqueService.__new__(CritiqueService)
            await svc._fetch_html(
                "https://example.test/",
                auth_info={
                    "type": "basic",
                    "username": "alice",
                    "password": "hunter2",  # pragma: allowlist secret
                },
            )
        expected = "Basic " + base64.b64encode(b"alice:hunter2").decode(  # pragma: allowlist secret
            "ascii"
        )
        assert captured["headers"]["authorization"] == expected

    @pytest.mark.asyncio
    async def test_cookies_list_sets_cookie_header(self) -> None:
        import httpx

        from src.domains.critique.services.critique_service.service import CritiqueService

        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=b"<html></html>")

        from unittest.mock import patch

        from src.domains.critique.services.critique_service import helpers as helpers_mod

        class _PatchedClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        with patch.object(helpers_mod.httpx, "AsyncClient", _PatchedClient):
            svc = CritiqueService.__new__(CritiqueService)
            await svc._fetch_html(
                "https://example.test/",
                auth_info={
                    "type": "cookies",
                    "cookies": [
                        {"name": "sess", "value": "xyz"},
                        {"name": "csrf", "value": "abc"},
                    ],
                },
            )
        cookie_hdr = captured["headers"]["cookie"]
        # httpx may serialise in any order; check both pairs present
        assert "sess=xyz" in cookie_hdr
        assert "csrf=abc" in cookie_hdr

    @pytest.mark.asyncio
    async def test_cookies_raw_header_passthrough(self) -> None:
        import httpx

        from src.domains.critique.services.critique_service.service import CritiqueService

        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=b"<html></html>")

        from unittest.mock import patch

        from src.domains.critique.services.critique_service import helpers as helpers_mod

        class _PatchedClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        with patch.object(helpers_mod.httpx, "AsyncClient", _PatchedClient):
            svc = CritiqueService.__new__(CritiqueService)
            await svc._fetch_html(
                "https://example.test/",
                auth_info={
                    "type": "cookies",
                    "cookie_header": "sess=raw-passthrough; remember=1",
                },
            )
        assert captured["headers"]["cookie"] == "sess=raw-passthrough; remember=1"
