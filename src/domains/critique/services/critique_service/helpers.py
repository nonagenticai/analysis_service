"""Internal helper methods (HTTP fetch, retries, progress, persistence) for CritiqueService."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import re
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import httpx

if TYPE_CHECKING:
    from bs4 import BeautifulSoup
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.models import (
    CritiqueFinding,
    CritiqueJob,
    CritiqueUsage,
    FindingSuppression,
)
from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.metrics import webhook_deliveries
from src.shared.security.url_validator import validate_callback_url

logger = logging.getLogger(__name__)

# Redirect budget for the manual (SSRF-gated) hop loop in ``_fetch_html`` —
# parity with ``src/domains/scorecards/fetch.py``.
_MAX_REDIRECTS = 5

# Request headers that carry the tenant's credentials and must never survive a
# hop to a different origin.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie"})

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Any ``scheme://`` prefix. ``artifact_ref`` is a *relative object key* and
# nothing else — a URI form would let the caller name the bucket (or a local
# file, or an HTTP origin), which is exactly the confinement hole this guards.
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

# The placeholder ``routes/_helpers.py::_get_tenant_id`` returns when a request
# carries no tenant. Duplicated here rather than imported because the dependency
# must not run service -> routes; ``tests/.../test_artifact_ref_confinement.py``
# pins the two spellings equal so the copy cannot drift. Any artifact read
# arriving under this value is unauthenticated and must be refused, not confined
# to a prefix literally named "anonymous" that every such caller would share.
UNAUTHENTICATED_TENANT_SENTINEL = "anonymous"


def _is_percent_encoded(raw: str) -> bool:
    """True when *raw* is not its own percent-decoding fixed point.

    ``artifact_ref`` is used **literally**: it is never percent-decoded, and a
    ref that would change under :func:`urllib.parse.unquote` is refused outright
    rather than decoded. One ``unquote`` round is enough to detect that — any
    valid escape sequence changes the string on the first pass, so
    double-encodings (``%252e``), ``%00`` splices and overlong UTF-8 all trip
    this predicate too. There is no decode budget to tune and therefore no
    "how many rounds is enough?" question to get wrong.

    Why reject instead of decode (the judgement call, stated so the next reader
    does not have to rediscover it):

    * Decoding does not merely make a ``%``-bearing key unaddressable — it
      **silently resolves a different object**. ``report%2Ffinal.html`` becomes
      ``report/final.html`` and ``a%20b.html`` becomes ``a b.html``. A read
      primitive that quietly retargets is worse than one that refuses.
    * Decoding-then-normalising is also the only reason the traversal check has
      to reason about encoding at all (``posixpath.normpath`` cannot see that
      ``%2f`` is a separator). Refusing encoded refs deletes that whole class
      instead of racing it.
    * The cost is zero today: no producer emits percent-encoded refs (see the
      ``ARTIFACT_BUCKET`` note — this path currently reaches no artifacts at
      all), and a literal ``%`` that is not a valid escape (``100%pure.html``)
      is still accepted because ``unquote`` leaves it unchanged.
    """
    return unquote(raw) != raw


def _build_auth_args(
    auth_info: dict | None,
) -> tuple[dict[str, str], httpx.BasicAuth | None, dict[str, str] | None]:
    """Translate a persisted ``AuthInfo`` dict into httpx client arguments.

    Split out of :meth:`_fetch_html` so that method stays under the complexity
    ceiling once the manual redirect loop lives there; the branching here is
    pure translation with no I/O and no security decision of its own.

    Args:
        auth_info: Persisted AuthInfo dict (``critique_jobs.auth_info`` JSONB),
            or ``None``/``{"type": "none"}`` for an unauthenticated fetch.

    Returns:
        ``(headers, auth, cookies)`` ready to hand to ``httpx.AsyncClient``.
        Anything credential-bearing in the result is subject to the cross-origin
        drop performed by the caller — see :meth:`_fetch_html`.
    """
    headers: dict[str, str] = {"User-Agent": "CritiqueService/1.0"}
    auth_arg: httpx.BasicAuth | None = None
    cookies_arg: dict[str, str] | None = None
    if not auth_info:
        return headers, auth_arg, cookies_arg

    auth_type = auth_info.get("type") or "none"
    if auth_type == "basic":
        auth_arg = httpx.BasicAuth(auth_info.get("username") or "", auth_info.get("password") or "")
    elif auth_type == "bearer":
        if token := auth_info.get("token") or "":
            headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "cookies":
        if isinstance(auth_info.get("cookies"), list):
            cookies_arg = {
                c["name"]: c["value"]
                for c in auth_info["cookies"]
                if isinstance(c, dict) and c.get("name") and c.get("value") is not None
            }
        elif auth_info.get("cookie_header"):
            headers["Cookie"] = str(auth_info["cookie_header"])
    return headers, auth_arg, cookies_arg


def _url_origin(url: str) -> tuple[str, str, int | None]:
    """Return the ``(scheme, host, port)`` origin triple of *url*.

    All three components are compared when deciding whether to drop credentials
    on a redirect. Comparing only the host is not enough: an ``https -> http``
    downgrade on the *same* host still hands the tenant's bearer token or
    session cookie to a plaintext connection, which was the exact finding fixed
    in ``w2v_common`` 0.53.0. The scheme's default port is substituted when the
    URL is portless so ``https://h`` and ``https://h:443`` compare equal.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return (scheme, host, port if port is not None else _DEFAULT_PORTS.get(scheme))


if TYPE_CHECKING:
    from src.domains.critique.services.scanners.a11y import A11yScanner
    from src.domains.critique.services.scanners.code_quality_scanner import CodeQualityScanner
    from src.domains.critique.services.scanners.cro import CROScanner
    from src.domains.critique.services.scanners.perf import PerformanceScanner
    from src.domains.critique.services.scanners.security_scanner import SecurityScanner
    from src.domains.critique.services.scanners.seo import SEOScanner
    from src.domains.critique.services.scanners.ux_reviewer import UXReviewerProtocol


class CritiqueHelpersMixin:
    """Mixin: HTTP fetch, retry, progress, scanner runners, persistence helpers."""

    # Cross-mixin attributes initialized by ``CritiqueService.__init__`` —
    # declared here so ty can resolve them from inside this mixin.
    _seo: SEOScanner
    _a11y: A11yScanner
    _performance: PerformanceScanner
    _security: SecurityScanner
    _ux: UXReviewerProtocol
    _code_quality: CodeQualityScanner
    _cro: CROScanner

    MAX_HTML_SIZE = 10 * 1024 * 1024  # 10MB

    # Filler for the ``minio://{bucket}/{key}`` URI handed to the brand domain's
    # ``StorageService``. It does NOT name the bucket that is read.
    #
    # ⚠️ WHAT THIS PATH ACTUALLY READS — do not infer anything friendlier from
    # the string below:
    #   * ``brand.services.storage_service.StorageService._minio_key`` splits the
    #     URI and *throws the bucket component away*, returning only the key.
    #   * The underlying ``w2v_common`` storage client then resolves that key
    #     against ``settings.storage.minio_bucket``, which for this service
    #     defaults to ``"analysis-service"`` (``src/config/infra.py``).
    #   * So every ``artifact_ref`` resolves inside **analysis_service's own
    #     bucket**, whose only writer is the brand domain's logo upload
    #     (``{tenant_id}/logos/{asset_id}.{ext}``).
    #
    # CONSEQUENCE, STATED PLAINLY: **this feature does not currently work.**
    # ``artifact_ref`` cannot reach an html_render_service artifact — no such
    # object exists in the bucket that is read — so every legitimate ref 404s
    # (surfacing as ``artifact_ref not found``). That was true before the
    # confinement fix and is still true after it; the fix changed which keys are
    # *permitted*, not which bucket is *read*, and routing the read elsewhere is
    # deliberately out of scope here (it is a behaviour change, not a fix).
    #
    # FOLLOW-UP required to make this reachable (all three, or it stays dead):
    #   1. Give this path a reader bound to html_render_service's bucket — a
    #      second storage client, or a ``StorageService`` that honours the bucket
    #      component of the URI instead of discarding it. Note that widening
    #      ``_minio_key`` is a cross-domain change: it would also make every
    #      brand-domain ``minio://`` row bucket-sensitive.
    #   2. Verify html_render_service's actual key layout. ``_tenant_prefix``
    #      below assumes a ``{tenant_id}/`` first segment; that assumption is
    #      inherited from the brand-logo layout in THIS bucket and has not been
    #      checked against the html_render bucket. If that bucket is not
    #      tenant-partitioned, the confinement prefix is wrong too and a
    #      different tenancy anchor is needed before the read is opened up.
    #   3. Only then revisit this constant — while the bucket is discarded it is
    #      documentation, not routing.
    ARTIFACT_BUCKET = "html-render-service"

    async def _resolve_html(
        self,
        job: Any,
        *,
        html_input: str | None = None,
        artifact_ref: str | None = None,
    ) -> str:
        """Resolve the primary HTML to score.

        Three sources, exactly one of which is used (enforced upstream by
        ``CritiqueRequest._exactly_one_source``):

          - ``html_input``   score a pre-rendered HTML string in place.
          - ``artifact_ref`` read an object out of this service's OWN MinIO
                             bucket, confined to ``{tenant_id}/`` (S2S). It
                             cannot reach html_render_service artifacts — see
                             the ``ARTIFACT_BUCKET`` note above — so in practice
                             this branch raises "not found" today.
          - neither          fall back to the historical live fetch of ``job.url``.

        Both static paths respect ``MAX_HTML_SIZE`` so a huge blob cannot blow
        the scanners' memory budget.
        """
        if html_input is not None:
            return html_input[: self.MAX_HTML_SIZE]
        if artifact_ref:
            return await self._fetch_artifact(artifact_ref, tenant_id=job.tenant_id)
        return await self._fetch_html(job.url, auth_info=job.auth_info)

    @staticmethod
    def _tenant_prefix(tenant_id: str | None) -> str:
        """Return the object-key prefix that confines *tenant_id*'s artifacts.

        The ``{tenant_id}/...`` layout is copied from the brand domain's LOGO
        keys (``brand/services/storage_service.py::store`` writes
        ``{tenant_id}/logos/{asset_id}.{ext}``) — which is the layout of the
        bucket this path actually reads, and the only layout in it. It is *not*
        known to be the layout of html_render_service's artifacts; that is
        follow-up item 2 in the ``ARTIFACT_BUCKET`` note above. Stated so nobody
        reads this docstring as evidence that the prefix matches the artifacts
        the feature advertises.

        Two rejections, both at this boundary rather than assumed:

        * A tenant id that could contribute a separator or a traversal segment
          (``/``, ``\\``, ``.``, ``..``). It arrives from the auth middleware,
          not the request body, but a prefix containing ``/`` or ``..`` turns
          the confinement check below into a no-op.
        * The ``"anonymous"`` sentinel that ``routes/_helpers.py::_get_tenant_id``
          substitutes when the request carries no tenant. It is a well-formed
          single segment, so the check above admits it — and admitting it would
          collapse every unauthenticated caller into one shared prefix, i.e. a
          cross-caller read primitive wearing a valid-looking tenant id. Only
          the check here rejects it: ``_get_tenant_id`` is shared with other
          domains and is deliberately left alone.
        """
        tid = (tenant_id or "").strip()
        if not tid or "/" in tid or "\\" in tid or tid in {".", ".."}:
            raise ValueError("artifact fetch requires a simple, non-empty tenant id")
        if tid == UNAUTHENTICATED_TENANT_SENTINEL:
            raise ValueError(
                "artifact fetch requires an authenticated tenant id; "
                f"{UNAUTHENTICATED_TENANT_SENTINEL!r} is the no-tenant sentinel"
            )
        return tid

    @classmethod
    def _artifact_object_key(cls, artifact_ref: str, tenant_id: str | None) -> str:
        """Compose the confined MinIO object key for *artifact_ref*.

        ``artifact_ref`` is fully caller-controlled, and the object it names is
        returned to the caller inside the critique output — so it is a read
        primitive, not a hint. ``w2v_common.storage.StorageService`` has no
        tenant concept whatsoever; confinement is entirely this method's job.

        The rule, in order:

        1. Reject any ref that is not its own percent-decoding fixed point. The
           ref is used **literally**; it is never decoded. See
           :func:`_is_percent_encoded` for why refusing beats decoding.
        2. Reject **any** ``scheme://`` form, checked on the ref as sent. There
           is deliberately no ``minio://`` passthrough any more: it let the
           caller pick the bucket *and* the key, i.e. name any object in the
           store. ``s3://``, ``file://`` and ``http://`` fall to the same rule.
        3. Fold backslashes to ``/`` so a Windows-style separator cannot smuggle
           a segment past ``posixpath``, then re-run the scheme check — folding
           can *manufacture* a ``://`` (``C:\\\\win`` becomes ``C://win``). That
           second rejection gets its own message: the input was never a URI, it
           became one, and a message saying "not a URI" would misdescribe it.
        4. Compose ``{tenant}/{ref}``, ``posixpath.normpath`` it, and require
           the result to be the prefix itself plus at least one more
           **segment**. The ``base + "/"`` comparison is what makes it a
           segment test and not a string-prefix test: a sibling prefix like
           ``{tenant}evil/x`` shares the string prefix ``{tenant}`` and must
           still be refused.

        Note there is no leading-``/`` strip: prepending ``{tenant}/`` and
        normalising already collapses leading slashes (``/etc/passwd`` and
        ``////..//x`` behave identically with and without one), so an explicit
        ``lstrip("/")`` would be a no-op reading like a security control. The
        "absolute-looking ref is a relative key" behaviour is pinned by test,
        not by that line.

        Returns:
            An object key guaranteed to live strictly under ``{tenant_id}/``.

        Raises:
            ValueError: If the ref is empty, percent-encoded, is (or folds
                into) a URI, names no object, or resolves outside the tenant's
                prefix.
        """
        prefix = cls._tenant_prefix(tenant_id)
        ref = artifact_ref.strip()
        if not ref:
            raise ValueError("artifact_ref is empty")
        if _is_percent_encoded(ref):
            raise ValueError(
                "artifact_ref must be a literal object key and is never "
                "percent-decoded; refusing a percent-encoded ref rather than "
                "silently resolving a different object"
            )
        if _URI_SCHEME_RE.match(ref):
            raise ValueError(
                "artifact_ref must be a relative object key, not a URI: "
                f"{artifact_ref!r} (the bucket is not caller-selectable)"
            )

        folded = ref.replace("\\", "/").strip()
        if not folded:
            raise ValueError("artifact_ref is empty")
        if _URI_SCHEME_RE.match(folded):
            raise ValueError(
                "artifact_ref must be a relative object key: folding its "
                "backslash separators yields a URI/drive-letter form "
                f"({folded!r}), which names no relative object key"
            )

        normalized = posixpath.normpath(f"{prefix}/{folded}")
        base = posixpath.normpath(prefix)
        if normalized == base or not normalized.startswith(base + "/"):
            # Deliberately does NOT echo the composed key: this message reaches
            # the caller (route 400) and the persisted job ``error_message``.
            raise ValueError(
                "refusing to access outside prefix: artifact_ref must name an "
                "object strictly inside the calling tenant's own prefix"
            )
        return normalized

    async def _fetch_artifact(self, artifact_ref: str, *, tenant_id: str | None) -> str:
        """Read an object from this service's own MinIO bucket and return it as text.

        ``artifact_ref`` is a **relative object key inside the calling tenant's
        prefix**. The key actually read is composed server-side by
        :meth:`_artifact_object_key`; nothing the caller sends can select a
        bucket or escape ``{tenant_id}/``.

        The bucket is NOT html_render_service's. This reuses the brand domain's
        ``StorageService``, which discards the bucket component of the URI below
        and reads ``settings.storage.minio_bucket`` (``analysis-service``) — so
        the only objects reachable here are the brand-logo keys this service
        writes itself, and a real html_render artifact ref raises "not found".
        See the ``ARTIFACT_BUCKET`` note for the follow-up that would change
        that; it is out of scope for the confinement fix.

        Raises:
            ValueError: If the ref fails confinement, or the object is absent
                (which, per the paragraph above, is the normal outcome today).
        """
        from src.domains.brand.services.storage_service import StorageService

        key = self._artifact_object_key(artifact_ref, tenant_id)
        uri = f"minio://{self.ARTIFACT_BUCKET}/{key}"
        t0 = time.monotonic()
        data = await StorageService().retrieve(uri)
        if data is None:
            raise ValueError(f"artifact_ref not found: {artifact_ref}")
        if len(data) > self.MAX_HTML_SIZE:
            logger.warning(
                "Artifact exceeded %d bytes, truncating: key=%s",
                self.MAX_HTML_SIZE,
                key,
            )
            data = data[: self.MAX_HTML_SIZE]
        logger.info(
            "Artifact fetched: key=%s size=%d elapsed=%.2fs",
            key,
            len(data),
            round(time.monotonic() - t0, 2),
        )
        return data.decode("utf-8", errors="replace")

    async def _fetch_html(self, url: str, auth_info: dict | None = None) -> str:
        """Fetch HTML content from a URL with a size limit, SSRF-gated per hop.

        ``auth_info`` is the persisted AuthInfo dict (see
        ``critique_jobs.auth_info`` JSONB column +
        ``critique/schemas.py::AuthInfo``). When present, applies the
        configured auth to the outbound httpx request: ``basic`` →
        ``httpx.BasicAuth``; ``bearer`` → ``Authorization: Bearer
        <token>``; ``cookies`` → cookie jar OR raw ``Cookie`` header.
        ``type="none"`` (or no auth_info) is the prior behaviour.

        Two security properties this method is responsible for:

        * **SSRF, on every hop.** ``url`` is tenant-supplied. Redirects are
          followed manually (``follow_redirects=False``) so the target of each
          hop is re-validated, because a public URL that ``302``-redirects to
          ``http://169.254.169.254/…`` — or to a name resolving into the
          ``100.64.0.0/10`` tailnet — defeats a one-shot check on the initial
          URL alone.
        * **Cross-origin credential drop.** This client carries the tenant's
          credentials (``httpx.BasicAuth``, ``Authorization: Bearer``, a cookie
          jar or a raw ``Cookie`` header). Auto-following redirects would
          forward all of them to whatever host the redirect names. As soon as a
          hop's ``(scheme, host, port)`` differs from the original target's, the
          credentialed client is closed and the remaining hops run on a fresh,
          credential-free client. Scheme and port are part of the comparison, so
          an ``https -> http`` downgrade on the same host also drops — that
          downgrade was the exact leak fixed in ``w2v_common`` 0.53.0.

        Failure is fail-closed and LOUD: a blocked URL raises ``ValueError``,
        which ``process_job`` catches to finalize the job as failed. There is no
        silent empty-HTML fallback, because scoring an empty document would
        report a scan that never happened.

        Per-scanner auth (security_scanner, network_checks,
        code_quality_scanner, axe-core Playwright context, ...) is a
        tracked follow-up — those each spin their own httpx.AsyncClient
        and still see unauthenticated traffic. For sites that gate ONLY
        the page HTML (the common case), this is enough.

        Args:
            url: Page to fetch. Untrusted; validated before every request.
            auth_info: Persisted AuthInfo dict, or ``None`` for no auth.

        Returns:
            The response body, decoded and truncated to ``MAX_HTML_SIZE``.

        Raises:
            ValueError: If the URL (or any redirect target) fails the SSRF
                guard, if a redirect carries no ``Location``, or if the
                redirect budget is exhausted.
            httpx.HTTPStatusError: If a non-redirect response is an error.
        """
        is_authed = bool(auth_info and auth_info.get("type") not in (None, "none"))
        logger.info("Fetching HTML: url=%s authed=%s", url, is_authed)
        t0 = time.monotonic()
        headers, auth_arg, cookies_arg = _build_auth_args(auth_info)
        validate_callback_url(url)
        origin = _url_origin(url)
        current = url
        credentialed = True

        client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=False,
            headers=headers,
            auth=auth_arg,
            cookies=cookies_arg,
        )
        try:
            for _hop in range(_MAX_REDIRECTS + 1):
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError(f"redirect without Location from {current}")
                        next_url = str(response.url.join(location))
                        validate_callback_url(next_url)
                        if credentialed and _url_origin(next_url) != origin:
                            logger.info(
                                "Cross-origin redirect: dropping credentials for hop to %s",
                                next_url,
                            )
                            stripped = {
                                k: v
                                for k, v in headers.items()
                                if k.lower() not in _CREDENTIAL_HEADERS
                            }
                            await client.aclose()
                            client = httpx.AsyncClient(
                                timeout=30.0,
                                follow_redirects=False,
                                headers=stripped,
                            )
                            credentialed = False
                        current = next_url
                        continue

                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.MAX_HTML_SIZE:
                            logger.warning(
                                "HTML response exceeded %d bytes, truncating: url=%s",
                                self.MAX_HTML_SIZE,
                                url,
                            )
                            excess = total - self.MAX_HTML_SIZE
                            chunks.append(chunk[: len(chunk) - excess])
                            break
                        chunks.append(chunk)
                    elapsed = round(time.monotonic() - t0, 2)
                    logger.info(
                        "HTML fetched: url=%s status=%d size=%d elapsed=%.2fs",
                        current,
                        response.status_code,
                        total,
                        elapsed,
                    )
                    return b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            await client.aclose()

        raise ValueError(f"too many redirects (> {_MAX_REDIRECTS}) fetching {url}")

    async def _run_scanner_with_timeout(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]],
        timeout: int | float,  # noqa: ASYNC109 — why: scanner-config deadline forwarded to asyncio.wait_for inside the wrapper
    ) -> tuple[list[dict[str, Any]], float]:
        """Run a scanner with timeout protection and retry logic."""
        from src.domains.critique.services import critique_service as _pkg

        settings = _pkg.get_settings()
        max_retries = settings.scanners.max_retries
        last_error: Exception | None = None
        t0 = time.monotonic()

        for attempt in range(1, max_retries + 1):
            try:
                coro = coro_factory()
                findings = await asyncio.wait_for(coro, timeout=timeout)
                elapsed = round(time.monotonic() - t0, 2)
                return findings, elapsed
            except TimeoutError:
                last_error = TimeoutError(f"{name} timed out after {timeout}s")
                logger.warning("Scanner %s timed out (attempt %d/%d)", name, attempt, max_retries)
            except (OSError, ValueError, RuntimeError) as e:
                last_error = e
                logger.warning(
                    "Scanner %s failed (attempt %d/%d): %s", name, attempt, max_retries, e
                )

            if attempt < max_retries:
                backoff = min(2**attempt, 10)
                await asyncio.sleep(backoff)

        # All retries exhausted
        elapsed = round(time.monotonic() - t0, 2)
        category = name.split("_")[0] if "_" in name else name
        error_desc = str(last_error) if last_error else "Unknown error"
        logger.error("Scanner %s failed after %d attempts: %s", name, max_retries, error_desc)
        return [
            {
                "finding_id": str(uuid.uuid7()),
                "category": category,
                "severity": "info",
                "title": f"Scanner failed: {name}",
                "description": (
                    f"The {name} scanner failed after {max_retries} attempts. "
                    f"Last error: {error_desc}"
                ),
                "url": "",
                "recommendation": "Try running the scan again later.",
                "selector": None,
            }
        ], elapsed

    async def _update_progress(
        self,
        job_id: str,
        *,
        status: str = "scanning",
        completed: int = 0,
        total: int = 0,
        current_scanner: str | None = None,
        scanners: list[str] | None = None,
        scanner_timings: dict[str, float] | None = None,
    ) -> None:
        """Update scan progress in Redis for SSE consumers."""
        try:
            from src.shared.cache.redis_client import get_redis_client

            client = get_redis_client()
            import json

            progress: dict = {"status": status, "completed": completed, "total": total}
            if current_scanner:
                progress["current_scanner"] = current_scanner
            if scanners:
                progress["scanners"] = scanners
            if scanner_timings:
                progress["scanner_timings"] = scanner_timings
            await client.set(f"critique:progress:{job_id}", json.dumps(progress), ttl=600)
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            logger.debug("Progress update failed for %s: %s", job_id, e)

    # ------------------------------------------------------------------
    # Scanner runners (delegate to scanner instances on ``self``)
    # ------------------------------------------------------------------

    async def _run_seo(self, html: str, url: str, soup: BeautifulSoup | None = None) -> list[dict]:
        return await asyncio.to_thread(self._seo.scan, html, url, soup=soup)

    async def _run_seo_live(self, url: str) -> list[dict]:
        return await self._seo.scan_live(url)

    async def _run_a11y(self, html: str, url: str, soup: BeautifulSoup | None = None) -> list[dict]:
        return await asyncio.to_thread(self._a11y.scan, html, url, soup=soup)

    async def _run_cro(self, html: str, url: str, soup: BeautifulSoup | None = None) -> list[dict]:
        return await asyncio.to_thread(self._cro.scan, html, url, soup=soup)

    async def _run_performance(
        self, html: str, url: str, soup: BeautifulSoup | None = None
    ) -> list[dict]:
        return await asyncio.to_thread(self._performance.scan, html, url, soup=soup)

    async def _run_security(
        self,
        url: str,
        html: str | None = None,
        soup: BeautifulSoup | None = None,
        site_analysis: dict | None = None,
    ) -> list[dict]:
        return await self._security.scan(url, html=html, soup=soup, site_analysis=site_analysis)

    async def _run_security_static(
        self,
        html: str,
        soup: BeautifulSoup | None = None,
        url: str = "",
        site_analysis: dict | None = None,
    ) -> list[dict]:
        # HTML-only security subset (no live headers/TLS/CORS/network) for
        # scoring a pre-rendered artifact with no live origin.
        return await self._security.scan_static(
            html, soup=soup, url=url, site_analysis=site_analysis
        )

    async def _run_ux(
        self,
        html: str,
        url: str,
        instructions: str | None,
        site_analysis: dict | None = None,
        llm_config: LLMConfigOverride | None = None,
        soup: BeautifulSoup | None = None,
    ) -> list[dict]:
        return await self._ux.scan(
            html, url, instructions, site_analysis=site_analysis, llm_config=llm_config, soup=soup
        )

    async def _run_code_quality(self, repo_url: str) -> list[dict]:
        return await self._code_quality.scan(repo_url)

    # ------------------------------------------------------------------
    # Side-effect helpers (webhook, suppressions, persistence, usage)
    # ------------------------------------------------------------------

    async def _deliver_webhook(self, job: CritiqueJob) -> None:
        """Deliver webhook notification for completed job."""
        if not job.callback_url:
            return
        try:
            from src.domains.critique.services.webhook import deliver_webhook

            payload = {
                "job_id": job.job_id,
                "status": job.status,
                "url": job.url,
                "scores": job.category_scores,
                "overall_score": job.overall_score,
                "findings_count": len(job.findings) if job.findings else 0,
                "summary": job.summary,
            }
            await deliver_webhook(job.callback_url, payload)
            webhook_deliveries.inc(status="success")
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            webhook_deliveries.inc(status="failure")
            logger.error("Webhook delivery failed for job %s: %s", job.job_id, e)

    @staticmethod
    def _suppression_index(
        suppressions: list[FindingSuppression], now: datetime
    ) -> tuple[set[str], set[str], set[str]]:
        """Index active suppression rules into (check_ids, title_hashes, categories)."""
        check_ids: set[str] = set()
        title_hashes: set[str] = set()
        categories: set[str] = set()
        for s in suppressions:
            if s.expires_at and s.expires_at < now:
                continue
            if s.check_id:
                check_ids.add(s.check_id)
            if s.title_hash:
                title_hashes.add(s.title_hash)
            if s.category and not s.check_id and not s.title_hash:
                categories.add(s.category)
        return check_ids, title_hashes, categories

    @staticmethod
    def _is_suppressed(
        finding: dict,
        check_ids: set[str],
        title_hashes: set[str],
        categories: set[str],
    ) -> bool:
        check_id = finding.get("check_id")
        if check_id and check_id in check_ids:
            return True
        title = finding.get("title", "")
        title_hash = hashlib.sha256(title.encode()).hexdigest()[:64]
        if title_hash in title_hashes:
            return True
        return finding.get("category", "") in categories

    async def _apply_suppressions(
        self,
        db: AsyncSession,
        findings: list[dict],
        *,
        tenant_id: str,
        domain: str | None,
    ) -> list[dict]:
        """Query active suppression rules and mark matching findings as suppressed."""
        try:
            now = datetime.now(UTC)
            conditions = [
                FindingSuppression.tenant_id == tenant_id,
                FindingSuppression.status.in_(["suppressed", "acknowledged"]),
            ]
            if domain:
                conditions.append(
                    or_(
                        FindingSuppression.domain.is_(None),
                        FindingSuppression.domain == domain,
                    )
                )
            else:
                conditions.append(FindingSuppression.domain.is_(None))

            result = await db.execute(select(FindingSuppression).where(and_(*conditions)))
            suppressions = list(result.scalars().all())

            if not suppressions:
                return findings

            check_ids, title_hashes, categories = self._suppression_index(suppressions, now)
            for f in findings:
                f["is_suppressed"] = self._is_suppressed(f, check_ids, title_hashes, categories)

        # ``SQLAlchemyError`` is load-bearing. The guarded body's only I/O is
        # ``db.execute``, and every failure it can raise -- ``OperationalError``
        # (connection dropped, pgbouncer reset), ``ProgrammingError``,
        # ``DBAPIError`` -- derives from ``SQLAlchemyError -> Exception`` and
        # from NO builtin in the old tuple. So the handler that exists to make
        # suppression fail-soft caught nothing a real database raises: the
        # error escaped instead, and the caller lost the findings it was
        # supposed to be annotating.
        except (OSError, ValueError, RuntimeError, TimeoutError, SQLAlchemyError) as e:
            logger.warning("Failed to apply suppressions: %s", e)
            for f in findings:
                f["is_suppressed"] = False

        return findings

    async def _save_findings(
        self, db: AsyncSession, job_id: str, findings: list[dict], *, tenant_id: str
    ) -> None:
        """Dual-write findings to the normalized critique_findings table.

        ``tenant_id`` is the PARENT ``CritiqueJob``'s tenant and is required, not
        derived: ``critique_findings`` carries its own ``tenant_id`` under an RLS
        policy whose ``WITH CHECK`` rejects a wrongly stamped row under enforce.
        Passing it explicitly keeps the stamp on the caller that owns the job.
        (The DB column is nullable until the 0008 → contract migration lands, so
        an UNstamped row would not be rejected — it would be world-readable
        instead. Never make the stamp optional here.)
        """
        try:
            for f in findings:
                row = CritiqueFinding(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    finding_id=f.get("finding_id", str(uuid.uuid7())),
                    category=f.get("category", "other"),
                    severity=f.get("severity", "info"),
                    title=f.get("title", ""),
                    description=f.get("description", ""),
                    url=f.get("url"),
                    recommendation=f.get("recommendation"),
                    check_id=f.get("check_id"),
                    wcag_criterion=f.get("wcag_criterion"),
                    evidence=f.get("evidence"),
                    source_tool=f.get("source_tool"),
                    false_positive_risk=f.get("false_positive_risk"),
                    remediation_effort=f.get("remediation_effort"),
                )
                db.add(row)
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            logger.warning("Failed to save normalized findings for %s: %s", job_id, e)

    async def _record_usage(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        pages_scanned: int,
        findings_count: int,
        is_error: bool,
    ) -> None:
        """Record usage for the tenant."""
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await db.execute(
            select(CritiqueUsage).where(
                CritiqueUsage.tenant_id == tenant_id,
                CritiqueUsage.date == today,
            )
        )
        usage = result.scalar_one_or_none()

        if usage:
            usage.scan_count += 1
            usage.pages_scanned += pages_scanned
            usage.findings_count += findings_count
            if is_error:
                usage.error_count += 1
        else:
            usage = CritiqueUsage(
                tenant_id=tenant_id,
                date=today,
                scan_count=1,
                pages_scanned=pages_scanned,
                findings_count=findings_count,
                error_count=1 if is_error else 0,
            )
            db.add(usage)
