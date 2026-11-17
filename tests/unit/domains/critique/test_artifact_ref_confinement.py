"""``artifact_ref`` may only name an object inside the caller's own prefix.

``artifact_ref`` is caller-supplied and the bytes it names come back to the
caller inside the critique output, so it is a read primitive.
``w2v_common.storage.StorageService`` has no tenant concept at all —
confinement is entirely the caller's job, and this module is the proof that
this caller does it.

The regression these tests pin: the old ``_fetch_artifact`` short-circuited on
``ref.startswith("minio://")``, which let the request pick BOTH the bucket and
the key; and the non-URI branch treated the value as a bare key with no tenant
prefix at all. Both are gone.

Every escape below is asserted to raise, not merely to "not equal" something —
a confinement check that returns a wrong-but-harmless key is still a bug. And
every test here drives ``_artifact_object_key`` itself: a test that only asserts
stdlib behaviour survives deleting the entire guard, which is exactly the false
pass this file exists to prevent.
"""

from collections.abc import Coroutine
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.domains.critique.routes._helpers import _get_tenant_id, validate_artifact_ref
from src.domains.critique.routes.jobs import create_critique
from src.domains.critique.schemas import CritiqueRequest
from src.domains.critique.services.critique_service import CritiqueService
from src.domains.critique.services.critique_service.helpers import (
    UNAUTHENTICATED_TENANT_SENTINEL,
)

TENANT = "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"


def _key(ref: str, tenant: str | None = TENANT) -> str:
    return CritiqueService._artifact_object_key(ref, tenant)


def _mock_request() -> MagicMock:
    """Request whose ``critique_service`` is an un-called AsyncMock.

    Same direct-coroutine-invocation style as ``test_target_url_ssrf.py``: this
    repo has no ``TestClient``/``app`` fixture, and the guard under test lives
    in the handler.
    """
    request = MagicMock()
    request.state.tenant_id = TENANT
    service = AsyncMock()
    service.create_job = AsyncMock(return_value="job-should-not-exist")
    request.app.state.critique_service = service
    return request


def _swallow_task(coro: Coroutine[object, object, None], **_kwargs: object) -> MagicMock:
    """Close the detached background coroutine instead of scheduling it."""
    coro.close()
    return MagicMock()


# --------------------------------------------------------------------------- #
# 1. The legitimate own-tenant ref still resolves (the fix must not be a
#    blanket denial — a guard that rejects everything passes every escape test)
# --------------------------------------------------------------------------- #
class TestOwnTenantRefResolves:
    def test_plain_relative_key(self):
        assert _key("jobs/abc/index.html") == f"{TENANT}/jobs/abc/index.html"

    def test_single_segment_key(self):
        assert _key("index.html") == f"{TENANT}/index.html"

    @pytest.mark.parametrize(
        "ref",
        [
            "/jobs/abc/index.html",
            "//jobs/abc/index.html",
            "////jobs//abc//index.html",
        ],
    )
    def test_leading_slashes_are_relative_not_absolute(self, ref):
        """Pins the behaviour that an ``lstrip("/")`` line used to *claim*.

        There is no leading-slash strip in the implementation: prepending
        ``{tenant}/`` and normalising collapses any run of leading slashes on
        its own. Note what this test does NOT claim — re-adding an
        ``lstrip("/")`` would not fail it, because that line was provably a
        no-op (identical output with and without, for every input including
        ``/etc/passwd`` and ``////..//..//4151df14-4770-5a91-ab9f-cbf2d14e68be/x``). That is exactly why it
        was deleted rather than "covered".

        Mutation killed: dropping the ``posixpath.normpath`` call, or composing
        the key without the ``{tenant}/`` prepend that lets normalisation
        collapse the run — either leaves the slashes in the returned key.
        """
        assert _key(ref) == f"{TENANT}/jobs/abc/index.html"

    def test_interior_dot_segments_are_normalised_but_stay_inside(self):
        assert _key("jobs/./abc/../abc/index.html") == f"{TENANT}/jobs/abc/index.html"

    def test_a_different_tenant_gets_a_different_key_for_the_same_ref(self):
        assert (
            _key("jobs/abc/index.html", "4151df14-4770-5a91-ab9f-cbf2d14e68be")
            == "4151df14-4770-5a91-ab9f-cbf2d14e68be/jobs/abc/index.html"
        )


# --------------------------------------------------------------------------- #
# 2. URI forms are refused outright — the caller does not choose the bucket
# --------------------------------------------------------------------------- #
class TestUriRefsRejected:
    def test_minio_uri_naming_another_bucket_is_rejected(self):
        with pytest.raises(ValueError, match="not a URI"):
            _key("minio://other-bucket/secrets/dump.html")

    def test_minio_uri_naming_the_expected_bucket_is_also_rejected(self):
        # Even the "right" bucket is refused: allowing the URI form at all is
        # what reintroduces caller-chosen keys.
        with pytest.raises(ValueError, match="not a URI"):
            _key(f"minio://{CritiqueService.ARTIFACT_BUCKET}/jobs/abc/index.html")

    @pytest.mark.parametrize(
        "ref",
        [
            "s3://other-bucket/secrets/dump.html",
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "https://example.com/index.html",
            "MINIO://other-bucket/x",
        ],
    )
    def test_other_schemes_rejected(self, ref):
        with pytest.raises(ValueError, match="not a URI"):
            _key(ref)

    def test_percent_encoded_scheme_is_rejected_as_encoded(self):
        """Refused, but by the literal-key rule — not decoded and then refused.

        The message matters: the ref is never decoded, so "not a URI" would be
        a lie about why it was rejected.
        """
        with pytest.raises(ValueError, match="never percent-decoded"):
            _key("minio%3A%2F%2Fother-bucket/secrets/dump.html")

    def test_backslash_fold_that_manufactures_a_scheme_says_so(self):
        r"""``C:\\win\\x`` folds to ``C://win//x``, which MATCHES the scheme regex.

        The input was never a URI — folding made it one — so reporting "not a
        URI" misdescribes the rejection. Mutation killed: moving the scheme
        check back to a single post-fold call, which restores the misleading
        message for this input (and only for this input, which is why a
        message assertion is the only thing that catches it).
        """
        with pytest.raises(ValueError, match="folding its backslash separators"):
            _key("C:\\\\win\\\\x")

    def test_single_backslash_drive_path_is_a_confined_key_not_a_uri(self):
        r"""``C:\win\x`` folds to ``C:/win/x`` — no ``://``, so it is just a key.

        Asserted because the reviewer's claim that this input is rejected as a
        URI is wrong, and pinning the real behaviour stops the next reader from
        "fixing" a rejection that does not exist. It is harmless: confined, and
        it simply names no object.
        """
        assert _key("C:\\win\\x") == f"{TENANT}/C:/win/x"


# --------------------------------------------------------------------------- #
# 3. Traversal out of the prefix
# --------------------------------------------------------------------------- #
class TestTraversalRejected:
    @pytest.mark.parametrize(
        "ref",
        [
            "../4151df14-4770-5a91-ab9f-cbf2d14e68be/jobs/abc/index.html",
            "../../etc/passwd",
            "jobs/../../4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "..",
            "../",
            "./..",
        ],
    )
    def test_dot_dot_escape_rejected(self, ref):
        with pytest.raises(ValueError, match="outside prefix"):
            _key(ref)

    def test_backslash_separator_cannot_smuggle_a_traversal(self):
        with pytest.raises(ValueError, match="outside prefix"):
            _key("..\\4151df14-4770-5a91-ab9f-cbf2d14e68be\\index.html")

    def test_absolute_path_cannot_escape(self):
        # Normalising "<tenant>//etc/passwd" collapses the leading slash, so
        # this lands at "<tenant>/etc/passwd", NOT at the bucket root.
        assert _key("/etc/passwd") == f"{TENANT}/etc/passwd"

    def test_ref_resolving_to_the_bare_prefix_names_no_object(self):
        with pytest.raises(ValueError, match="outside prefix"):
            _key(".")

    @pytest.mark.parametrize("ref", ["", "   "])
    def test_empty_ref_rejected(self, ref):
        with pytest.raises(ValueError, match="empty"):
            _key(ref)


# --------------------------------------------------------------------------- #
# 3b. Percent-encoding is REFUSED, never decoded.
#
#     The ref is used literally. This is the deliberate alternative to
#     decode-then-normalise, which is not merely lossy for keys containing "%"
#     — it silently resolves a DIFFERENT object ("a%20b.html" -> "a b.html").
#     Refusing also deletes the whole "did we decode enough rounds?" question:
#     the old implementation had a `_REF_DECODE_ROUNDS = 3` budget whose value
#     no test could pin, because 2 and 3 behaved identically.
# --------------------------------------------------------------------------- #
class TestPercentEncodedRefsRefused:
    @pytest.mark.parametrize(
        "ref",
        [
            # Traversal at every encoding depth the old decode budget had to
            # out-run. Depth >= 4 is the case a 3-round budget silently let
            # through as a literal key; depth 1-2 is what it caught.
            "%2e%2e/4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "%2e%2e%2f4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "..%2f4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "%252e%252e%252f4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "%25252e%25252e%25252f4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "%2525252e%2525252e%2525252f4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            # NUL splice and an overlong-UTF-8 separator, same rule.
            "index.html%00.png",
            "%c0%ae4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            # Encoded scheme and encoded backslash.
            "minio%3A%2F%2Fbucket/x",
            "..%5c4151df14-4770-5a91-ab9f-cbf2d14e68be%5cindex.html",
        ],
    )
    def test_any_encoded_ref_is_refused(self, ref):
        """Mutation killed: deleting the ``_is_percent_encoded`` gate.

        Without the gate every one of these is either honoured literally (the
        deep-encoding rows, which a bounded decoder never unwraps) or decoded
        and re-checked — i.e. the exact decode-vs-normalise race the gate
        removes. With the gate all of them fail closed, with the message that
        says why.
        """
        with pytest.raises(ValueError, match="never percent-decoded"):
            _key(ref)

    def test_encoding_gate_runs_before_normalisation(self):
        """An encoded ref that is *harmless* is still refused.

        ``report%2Ffinal.html`` decodes to ``report/final.html`` — inside the
        prefix, so a decode-then-check implementation ACCEPTS it and returns a
        key the caller never asked for. Mutation killed: re-introducing the
        decode, which turns this raise into a successful, silently retargeted
        read.
        """
        with pytest.raises(ValueError, match="never percent-decoded"):
            _key("report%2Ffinal.html")

    def test_space_encoding_is_refused_rather_than_retargeted(self):
        # Same class, blunter: "a%20b.html" and "a b.html" are different keys.
        with pytest.raises(ValueError, match="never percent-decoded"):
            _key("a%20b.html")

    def test_literal_percent_that_is_not_an_escape_still_resolves(self):
        """The gate is "not a decoding fixed point", not "contains a %".

        ``100%pure.html`` is unchanged by ``unquote``, so it is a legal literal
        key. Mutation killed: replacing the fixed-point test with a blunt
        ``"%" in ref``, which would deny this valid key.
        """
        assert _key("100%pure.html") == f"{TENANT}/100%pure.html"


# --------------------------------------------------------------------------- #
# 4. Sibling prefix — the reason this is a SEGMENT test, not a string-prefix
#    test. "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/..." shares the string prefix "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d".
# --------------------------------------------------------------------------- #
class TestSiblingPrefixRejected:
    @pytest.mark.parametrize(
        "ref",
        [
            "../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/index.html",
            "../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d-evil/index.html",
            "jobs/../../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/index.html",
            "..\\ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil\\index.html",
        ],
    )
    def test_sibling_prefix_sharing_the_string_prefix_is_rejected(self, ref):
        with pytest.raises(ValueError, match="outside prefix"):
            _key(ref)

    @pytest.mark.parametrize(
        ("ref", "victim"),
        [
            (
                "../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/index.html",
                "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil",
            ),
            (
                "jobs/../../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/index.html",
                "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil",
            ),
            (
                "..\\ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil\\index.html",
                "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil",
            ),
        ],
    )
    def test_naive_startswith_would_have_admitted_this_but_the_real_guard_does_not(
        self, ref, victim
    ):
        """Drives the REAL function on both sides of the comparison.

        Mutation killed: replacing ``normalized.startswith(base + "/")`` with a
        naive ``normalized.startswith(base)``.

        The proof is in three parts, none of which is an assertion about
        ``posixpath``:

        1. ``_key(ref)`` raises for ``ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d`` — the guard refuses it.
        2. The very same ref, resolved for the VICTIM tenant, is a perfectly
           ordinary key. So the rejection above is tenant isolation, not the ref
           being malformed; a real object is being reached for.
        3. The key the victim owns starts with the attacker's prefix as a bare
           string. That is precisely what a naive ``startswith`` would accept,
           and it is computed from the guard's own output, not hand-rolled.
        """
        with pytest.raises(ValueError, match="outside prefix"):
            _key(ref)

        victim_key = _key("index.html", victim)
        assert victim_key.startswith(TENANT)  # a naive prefix check admits it...
        assert not victim_key.startswith(TENANT + "/")  # ...the segment check does not


# --------------------------------------------------------------------------- #
# 5. The tenant prefix itself must be a single, non-empty segment
# --------------------------------------------------------------------------- #
class TestTenantPrefixValidated:
    @pytest.mark.parametrize("tenant", [None, "", "   ", "a/b", "a\\b", ".", ".."])
    def test_unusable_tenant_id_refuses_the_fetch(self, tenant):
        with pytest.raises(ValueError, match="tenant id"):
            _key("index.html", tenant)

    @pytest.mark.parametrize("tenant", ["anonymous", "  anonymous  "])
    def test_anonymous_sentinel_is_refused(self, tenant):
        """The no-tenant sentinel is a well-formed segment — and must still lose.

        ``_get_tenant_id`` substitutes ``"anonymous"`` when a request carries no
        tenant. It contains no separator and no dot segment, so the hardening
        above admits it; admitting it would confine every unauthenticated caller
        to ONE shared ``anonymous/`` prefix, which is a cross-caller read
        primitive rather than isolation.

        Mutation killed: deleting the sentinel check in ``_tenant_prefix``
        (nothing else in the guard distinguishes this value from a real
        tenant id).
        """
        with pytest.raises(ValueError, match="authenticated tenant id"):
            _key("index.html", tenant)

    def test_the_route_can_no_longer_produce_the_sentinel_at_all(self):
        """The hole is now closed at the SOURCE as well as at the guard.

        This used to assert ``_get_tenant_id(request) ==
        UNAUTHENTICATED_TENANT_SENTINEL`` — i.e. that the route really did
        substitute ``"anonymous"`` for a tenant-less request, keeping the two
        spellings of the sentinel pinned equal. Since migration 0013 made
        ``tenant_id`` a native ``uuid`` column, ``_get_tenant_id`` has no default
        left to substitute: a tenant-less request is a 401 before any key is
        built, so no caller can ever reach the shared ``anonymous/`` prefix by
        that route.

        The ``_tenant_prefix`` check and :data:`UNAUTHENTICATED_TENANT_SENTINEL`
        deliberately STAY (see the test above): ``_artifact_object_key`` is
        reachable from paths that never pass through this route, so the deep
        guard is defence in depth, not a relocation. What changes here is only
        that the route no longer manufactures the value the guard exists to
        reject.
        """
        request = MagicMock()
        del request.state.tenant_id  # force the (now absent) getattr default
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401

        # And the deep guard still refuses the sentinel if it arrives some other way.
        with pytest.raises(ValueError, match="authenticated tenant id"):
            _key("index.html", UNAUTHENTICATED_TENANT_SENTINEL)


# --------------------------------------------------------------------------- #
# 5b. Route admission: a confinement failure is a 4xx on the REQUEST, not a
#     job row that fails asynchronously.
# --------------------------------------------------------------------------- #
class TestRouteRejectsBeforePersisting:
    @pytest.mark.parametrize(
        "ref",
        [
            "../4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/index.html",
            "minio://other-bucket/secrets/dump.html",
            "%2e%2e%2f4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "..",
        ],
    )
    async def test_bad_ref_is_400_and_no_job_is_persisted(self, ref):
        """Drives ``create_critique`` itself, not just the helper.

        Mutation killed: removing the ``validate_artifact_ref`` call from
        ``create_critique`` (a test that only called the helper would survive
        that deletion, which is the whole failure mode this file is fixing).

        Without the call the request is answered ``200`` with a real ``job_id``
        and the rejection only surfaces later, swallowed by ``process_job``'s
        broad ``except ... ValueError ...`` into a failed job.
        """
        request = _mock_request()
        with pytest.raises(HTTPException) as exc:
            await create_critique(CritiqueRequest(artifact_ref=ref), request, AsyncMock())
        assert exc.value.status_code == 400
        assert "artifact_ref" in exc.value.detail
        request.app.state.critique_service.create_job.assert_not_called()

    async def test_good_ref_still_creates_a_job(self):
        """The route gate is not a blanket deny."""
        request = _mock_request()
        service = request.app.state.critique_service
        service.create_job = AsyncMock(return_value="job-ok")
        with patch("src.domains.critique.routes.jobs.asyncio") as mock_asyncio:
            mock_asyncio.create_task = MagicMock(side_effect=_swallow_task)
            result = await create_critique(
                CritiqueRequest(artifact_ref="jobs/abc/index.html"), request, AsyncMock()
            )
        assert result.job_id == "job-ok"

    def test_route_rejection_does_not_echo_the_composed_key(self):
        """The 400 body (and the persisted ``error_message``) must not leak the
        server-side prefix composition.

        Mutation killed: restoring the old
        ``f"refusing to access outside prefix: {prefix}/{relpath}"`` message.
        """
        with pytest.raises(HTTPException) as exc:
            validate_artifact_ref("../4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html", TENANT)
        assert "4151df14-4770-5a91-ab9f-cbf2d14e68be" not in exc.value.detail
        assert f"{TENANT}/" not in exc.value.detail

    def test_anonymous_caller_cannot_use_artifact_ref_at_all(self):
        with pytest.raises(HTTPException) as exc:
            validate_artifact_ref("index.html", UNAUTHENTICATED_TENANT_SENTINEL)
        assert exc.value.status_code == 400

    def test_good_ref_passes_the_route_gate(self):
        # Not a blanket deny: a legal ref must reach ``create_job``.
        assert validate_artifact_ref("jobs/abc/index.html", TENANT) is None
        assert validate_artifact_ref(None, TENANT) is None

    def test_schema_still_accepts_the_ref_so_the_gate_is_the_route_not_pydantic(self):
        """The guard deliberately lives at the route, matching the SSRF guard.

        ``CritiqueRequest`` must keep constructing with a hostile ref — S2S
        callers and tests deserialize this schema, and a validator here would
        move the rejection somewhere ``create_critique`` no longer owns.
        """
        assert (
            CritiqueRequest(artifact_ref="../4151df14-4770-5a91-ab9f-cbf2d14e68be/x").artifact_ref
            == "../4151df14-4770-5a91-ab9f-cbf2d14e68be/x"
        )


# --------------------------------------------------------------------------- #
# 6. End-to-end through _fetch_artifact: the storage layer only ever sees the
#    composed key, and a rejected ref never reaches storage at all.
# --------------------------------------------------------------------------- #
class TestFetchArtifactUsesComposedKey:
    async def test_storage_receives_tenant_scoped_uri(self):
        with patch.object(CritiqueService, "_init_adapters", return_value=None):
            svc = CritiqueService()
        storage = MagicMock()
        storage.retrieve = AsyncMock(return_value=b"<html>ok</html>")
        with patch(
            "src.domains.brand.services.storage_service.StorageService", return_value=storage
        ):
            html = await svc._fetch_artifact("jobs/abc/index.html", tenant_id=TENANT)

        assert html == "<html>ok</html>"
        storage.retrieve.assert_awaited_once_with(
            f"minio://{CritiqueService.ARTIFACT_BUCKET}/{TENANT}/jobs/abc/index.html"
        )

    @pytest.mark.parametrize(
        "ref",
        [
            "minio://other-bucket/secrets/dump.html",
            "../4151df14-4770-5a91-ab9f-cbf2d14e68be/index.html",
            "../ee0a5549-a6f9-5c7b-861c-bdb1d029ce6devil/index.html",
        ],
    )
    async def test_rejected_ref_never_touches_storage(self, ref):
        with patch.object(CritiqueService, "_init_adapters", return_value=None):
            svc = CritiqueService()
        storage = MagicMock()
        storage.retrieve = AsyncMock(return_value=b"<html>leaked</html>")
        with (
            patch(
                "src.domains.brand.services.storage_service.StorageService", return_value=storage
            ),
            pytest.raises(ValueError),
        ):
            await svc._fetch_artifact(ref, tenant_id=TENANT)

        storage.retrieve.assert_not_awaited()
