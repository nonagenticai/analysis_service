"""Trace continuity tests for RequestIdMiddleware — w2v_common ContextVar glue.

Complements ``tests/unit/domains/critique/test_middleware.py`` (which covers
the local ``request.state.request_id`` assignment) by asserting that the same
id is also bound to ``w2v_common.observability.request_context`` so outbound
httpx S2S calls propagate the trace id to peer services.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from w2v_common.observability.request_context import (
    clear_request_id,
    get_request_id,
)

from src.shared.middleware.request_id import RequestIdMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, str]:
        return {"request_id": get_request_id()}

    return app


def test_inbound_header_bound_to_contextvar():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe", headers={"X-Request-Id": "trace-abc-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "trace-abc-123"
    assert resp.json()["request_id"] == "trace-abc-123"


def test_generated_id_bound_to_contextvar():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe")
    assert resp.status_code == 200
    rid = resp.headers["X-Request-Id"]
    assert rid
    # uuid7 hex form is 32 chars, plus 4 hyphens = 36
    assert len(rid) >= 32
    assert resp.json()["request_id"] == rid


def test_case_insensitive_header_bound_to_contextvar():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe", headers={"x-request-id": "lowercase-id"})
    assert resp.headers["X-Request-Id"] == "lowercase-id"
    assert resp.json()["request_id"] == "lowercase-id"
