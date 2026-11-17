"""Pins that one failing network sub-check cannot erase its siblings' findings.

Every assertion here is on the FINDING LIST -- which check ids came back --
never on how many times a collaborator was called. A scan that reports fewer
findings than it found is the bug under test, so the finding list is the only
observable that can distinguish the fixed code from the broken code.

Two independent losses are covered:

``_check_exposed_files``
    ``_probe_exposed_file`` read the response body (``resp.status_code``,
    ``resp.text``, ``_is_false_positive_file``) *outside* its own ``try``, and
    the caller gathered the probes without ``return_exceptions``. A body that
    would not decode therefore escaped one probe and cancelled every sibling
    probe, so a single malformed response erased the whole exposed-files
    family. Both halves are fixed; either one alone would mask the other, so
    the mutation proof for this file must revert both.

``_check_directory_listing``
    Its outer handler named only builtins. Nothing in the httpx tree derives
    from ``OSError`` or ``RuntimeError``, so an httpx failure raised outside
    the inner per-directory guard escaped the coroutine instead of degrading to
    "no finding", and the check was reported as errored rather than clean.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from src.domains.critique.services.scanners.security_scanner import SecurityScanner
from src.domains.critique.services.scanners.security_scanner import (
    network_checks as network_checks_mod,
)

#: A public literal, so no probe in this module can need a name lookup.
SITE_URL = "http://8.8.8.8/"

#: ``UNIVERSAL_FILE_CHECKS`` always probes these three. ``.env`` plays the
#: poisoned response; the other two are the siblings that must survive it.
POISONED_FILE = ".env"
SURVIVING_CHECK_IDS = {"sec-exposed-git", "sec-exposed-dsstore"}


class _UndecodableResponse:
    """A 200 whose body cannot be decoded.

    Models the real shape this defect takes in production: a server that
    advertises an encoding or charset it does not honour, so httpx raises when
    the body is materialised rather than when it is fetched. ``status_code``
    and ``headers`` answer normally -- the raise happens on ``.text``, which is
    precisely the line that used to sit outside the probe's ``try``.
    """

    status_code = 200

    def __init__(self) -> None:
        """Start with headers that look entirely ordinary."""
        self.headers = httpx.Headers({"content-type": "text/plain"})

    @property
    def text(self) -> str:
        """Raise the way httpx does on a body it cannot decode.

        Raises:
            httpx.DecodingError: Always.
        """
        raise httpx.DecodingError("body claims gzip and is not gzip")


class _ExposedResponse:
    """A 200 carrying a body that reads as a genuinely exposed file."""

    status_code = 200

    def __init__(self, body: str) -> None:
        """Store the body served for this probe.

        Args:
            body: Response text the false-positive filter will inspect.
        """
        self.headers = httpx.Headers({"content-type": "text/plain"})
        self.text = body


class _FailingClient:
    """An ``AsyncClient`` stand-in that fails the moment it is entered.

    Models a dead origin: the failure comes from opening the connection, i.e.
    from OUTSIDE ``_check_directory_listing``'s inner per-directory ``try``, so
    only the outer handler can absorb it. That is the handler under test.
    """

    def __init__(self, **_kwargs: Any) -> None:
        """Accept and ignore whatever client kwargs the caller passes."""

    async def __aenter__(self) -> Any:
        """Fail the way a refused origin does.

        Raises:
            httpx.ConnectError: Always.
        """
        raise httpx.ConnectError("connection refused")

    async def __aexit__(self, *_exc: object) -> bool:
        """Never reached; present so the context manager is well formed.

        Returns:
            ``False``, so nothing is suppressed.
        """
        return False


@pytest.mark.asyncio
async def test_undecodable_body_on_one_probe_keeps_the_other_exposed_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreadable probe response must not cost the sibling probes.

    The poisoned probe is entitled to contribute nothing -- it could not
    complete. What it may not do is take the other two families' findings with
    it, which is what the pre-fix code did.
    """

    # Each surviving probe needs a body its own false-positive filter accepts:
    # ``.git/config`` is only believed when it contains a ``[core]`` stanza, so
    # a generic secret string would be discarded as a false positive and the
    # test would go green for the wrong reason.
    bodies = {
        ".git/config": "[core]\n\trepositoryformatversion = 0\n",
        ".DS_Store": "Bud1\x00notarealdsstore",
    }

    async def fake_guarded_get(_client: Any, url: str, **_kwargs: Any) -> Any:
        if url.endswith(POISONED_FILE):
            return _UndecodableResponse()
        for path, body in bodies.items():
            if url.endswith(path):
                return _ExposedResponse(body)
        raise AssertionError(f"unexpected probe URL: {url}")

    monkeypatch.setattr(network_checks_mod, "guarded_get", fake_guarded_get)
    findings = await SecurityScanner()._check_exposed_files(SITE_URL)

    returned_ids = {f["check_id"] for f in findings}
    assert returned_ids == SURVIVING_CHECK_IDS, (
        "the surviving probes' findings were lost when a sibling probe could not "
        f"decode its response; got {sorted(returned_ids)}"
    )


@pytest.mark.asyncio
async def test_directory_listing_degrades_to_no_finding_on_an_httpx_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An httpx failure outside the per-directory guard must not escape.

    ``_check_directory_listing`` is documented fail-soft "per candidate and
    overall". Returning an empty finding list is that contract; raising is the
    defect, because the raise propagates out of the check and is reported as a
    check that could not run.
    """

    monkeypatch.setattr(network_checks_mod.httpx, "AsyncClient", _FailingClient)
    findings = await SecurityScanner()._check_directory_listing(SITE_URL)

    assert findings == [], f"expected a fail-soft empty result, got {findings}"


@pytest.mark.asyncio
async def test_a_dead_directory_listing_probe_does_not_error_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other three families report, and dir-listing is clean, not errored.

    This is the caller-level view of the same guard. The three siblings are
    stubbed to known findings so that only ``_check_directory_listing`` runs for
    real against the failing client. If the httpx failure escapes that check,
    ``_run_async_checks`` records a ``sec-check-errored`` finding for it -- a
    scan that says "this check did not run" when the check is perfectly able to
    say "nothing here".
    """
    scanner = SecurityScanner()

    def _stub(check_id: str) -> Any:
        async def _run(*_args: Any, **_kwargs: Any) -> list[dict]:
            return [{"check_id": check_id, "severity": "info", "title": check_id}]

        return _run

    monkeypatch.setattr(scanner, "_check_exposed_files", _stub("stub-exposed"))
    monkeypatch.setattr(scanner, "_check_cors", _stub("stub-cors"))
    monkeypatch.setattr(scanner, "_check_redirect_chain", _stub("stub-redirect"))

    monkeypatch.setattr(network_checks_mod.httpx, "AsyncClient", _FailingClient)
    findings = await scanner._run_async_checks(SITE_URL, None, httpx.Headers({}))

    returned_ids = [f["check_id"] for f in findings]
    assert {"stub-exposed", "stub-cors", "stub-redirect"} <= set(returned_ids), (
        f"sibling check families were lost; got {returned_ids}"
    )
    assert "sec-check-errored" not in returned_ids, (
        "a dead directory-listing origin was reported as a check that could not "
        "run, rather than degrading to no finding"
    )


@pytest.mark.asyncio
async def test_cancellation_is_not_reported_as_an_exposed_file_probe_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``return_exceptions=True`` must not swallow cancellation.

    ``CancelledError`` is control flow. Absorbing it would make a cancelled
    scan look like a scan that ran and found nothing -- the same class of lie
    this module exists to prevent.
    """

    async def fake_guarded_get(_client: Any, _url: str, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(network_checks_mod, "guarded_get", fake_guarded_get)
    with pytest.raises(asyncio.CancelledError):
        await SecurityScanner()._check_exposed_files(SITE_URL)


@pytest.mark.asyncio
async def test_a_probe_raising_after_its_own_guard_keeps_the_sibling_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from OUTSIDE the probe's ``try`` must not cancel the siblings.

    This is the pin for the ``return_exceptions=True`` half of the fix, and it
    is the only test here that the try-scope half cannot satisfy.
    ``_sec_finding`` is called after the probe's ``except`` has closed, so no
    widening of that handler can ever absorb a failure there -- the caller's
    ``gather`` is the only thing that can. Reverting the gather alone therefore
    reds this test and nothing else.

    The injected error is a ``RuntimeError``, which the probe's own tuple DOES
    name. That is deliberate: it escaping anyway is positive evidence that the
    call site sits outside the guarded block, rather than evidence about which
    types the guard lists.
    """
    real_sec_finding = network_checks_mod._sec_finding

    def exploding_sec_finding(**kwargs: Any) -> dict:
        if kwargs.get("check_id") == "sec-exposed-env":
            raise RuntimeError("finding construction failed")
        return real_sec_finding(**kwargs)

    bodies = {
        ".env": "AWS_ACCESS_KEY_ID=notarealkey",
        ".git/config": "[core]\n\trepositoryformatversion = 0\n",
        ".DS_Store": "Bud1\x00notarealdsstore",
    }

    async def fake_guarded_get(_client: Any, url: str, **_kwargs: Any) -> Any:
        for path, body in bodies.items():
            if url.endswith(path):
                return _ExposedResponse(body)
        raise AssertionError(f"unexpected probe URL: {url}")

    monkeypatch.setattr(network_checks_mod, "guarded_get", fake_guarded_get)
    monkeypatch.setattr(network_checks_mod, "_sec_finding", exploding_sec_finding)

    findings = await SecurityScanner()._check_exposed_files(SITE_URL)

    returned_ids = {f["check_id"] for f in findings}
    assert returned_ids == SURVIVING_CHECK_IDS, (
        "a probe that raised after its own guard closed took the sibling probes' "
        f"findings with it; got {sorted(returned_ids)}"
    )
