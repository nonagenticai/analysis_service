"""CI-enforced guard: no UNDOCUMENTED bare ``httpx`` client under ``src/``.

Why this exists
---------------
Outbound service-to-service calls must forward the identity triple
``X-Tenant-Id`` / ``X-License-Id`` / ``X-Subject-Id``. Where the convention is
used it works and needs no discipline:
``w2v_common.auth.identity_context.get_identity_headers`` is auto-installed as an
httpx ``event_hooks={"request": [...]}`` handler by
``w2v_common.clients.base.BaseServiceClient``, so **every** subclass forwards
correctly without a single line of per-call-site code.

The droppers are therefore never the clients — they are the raw
``httpx.AsyncClient(...)`` / ``httpx.Client(...)`` constructions that bypass the
base class entirely. That is a *class* of defect, not an instance: each one is
individually plausible at review time ("it's just a fetch"), and nothing tells
you the headers silently vanished. Hence a static gate rather than a round of
one-off fixes.

How it decides
--------------
An AST walk over every ``src/**/*.py`` finds each construction of ``httpx.Client``
/ ``httpx.AsyncClient`` (attribute form, and the ``from httpx import AsyncClient``
form). Each hit is keyed by ``(path, enclosing-function-qualname)`` — stable
across line-number churn, unlike a ``file:line`` allowlist. Any key not in
:data:`ALLOWED` fails the test.

An exemption is NOT just a line in a list. Every entry carries a **category** and
a **written reason**, and three of the four categories are *structurally
substantiated* — the guard re-checks the claim against the source of the exempted
function, so an exemption cannot quietly outlive the thing that justified it:

* ``SSRF_SINK`` — the function must still CALL one of the guarded fetch helpers
  (:data:`_SSRF_MARKERS`), checked against AST identifiers rather than source
  text. If someone drops ``get_guarded`` for a raw ``client.get``, the exemption
  stops applying and this test reds — and it stays red even though the
  function's docstring still talks about the guard at length. (The orchestrator's
  first cut of this guard grepped source text and passed a mutation, because a
  *docstring* mentioned ``safe_get``. Prose is not code.)
* ``IDENTITY_FORWARDED`` — the function must still reference
  ``get_identity_headers`` / ``forward_headers``. Currently unused in this repo:
  no bare client here forwards the triple by hand. Kept because the next one
  might, and an unclaimed category is cheaper than a hand-rolled exception.
* ``GUARDED_POOL`` — a long-lived pooled client that does not fetch anything
  itself; it is handed to consumers that each fetch through the guard. Those
  consumers are named in the entry and re-verified one level down: each must
  exist AND reference a guard marker. Delete the guard from a consumer and this
  reds.
* ``NON_W2V_PEER`` — reason only. These call something outside the w2v S2S
  convention (an OWASP ZAP REST API, the code-graph-helper API) where the triple
  is not the auth channel; there is nothing mechanical left to assert, so the
  reason must stand on its own and is held to a minimum length.

And a **stale entry that matches nothing fails the test** (non-vacuity): when a
call site is deleted or routed through ``BaseServiceClient``, its exemption must
go with it. An allowlist that can accumulate dead entries is how a guard rots
into decoration. :func:`test_guard_actually_finds_the_known_constructions` adds
the other direction — a scanner that silently finds nothing passes every other
test in this file, so a floor is pinned.

And one guard on the guard: :func:`test_ssrf_markers_actually_reach_a_chokepoint`
recomputes, from the AST, that every declared marker transitively reaches one of
the terminal screens in :data:`_SSRF_CHOKEPOINTS`. Adding a plausible-looking
name that fetches raw is the obvious way to defeat the substantiation check; it
is now a red test.

Why every entry and zero "fixes"
--------------------------------
The audit that prompted this file named three sites as identity-dropping S2S
calls. All three are **false positives**, and two of them dangerously so: the
critique and review webhooks POST to a CUSTOMER-supplied callback URL, so
attaching ``X-Tenant-Id: Bearer <license>`` would hand a live credential to
whatever host the customer registered. The third (``fetch_html_capped``) fetches
an arbitrary audit target. Nothing in ``src/`` constructs a bare client aimed at
a w2v peer — every real peer already goes through a ``BaseServiceClient``
subclass. That is the honest finding. The value of this file is the floor it
sets for the NEXT call site.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

_SRC = Path(__file__).resolve().parent.parent / "src"

# Names whose presence in an exempted function proves the fetch still goes
# through an SSRF chokepoint. Keep this list to helpers that genuinely screen
# (and, where applicable, pin) the target; adding a name that does not is how the
# substantiation check gets quietly defeated.
#   get_guarded                  brand/security.py — screen + cap, per hop
#   guarded_get / guarded_head   security_scanner/http_guard.py — same, per hop
#   _is_ssrf_safe_resolved       resolving screen used before a scanner fan-out
#   validate_callback_url        shared/security/url_validator.py
#   pin_target / _resolve_pin_target  screen + pin the approved IP (anti-rebind)
#   resolve_external_scan_target external_process_target.py — seed screen
_SSRF_MARKERS = (
    "get_guarded",
    "guarded_get",
    "guarded_head",
    "_is_ssrf_safe_resolved",
    "validate_callback_url",
    # NOT "pin_target": it is pure URL/Host/SNI rewriting (url_validator.py:495) that
    # takes an address `resolve_fetch_pin` ALREADY approved — it screens nothing itself.
    # Declaring it a marker would exempt any future function that references only it
    # while doing zero validation. Removed 2026-08-19 after the reachability arm below
    # proved it reaches no chokepoint. `_resolve_pin_target` (which does) stays.
    "_resolve_pin_target",
    "resolve_external_scan_target",
)

# The TERMINAL screens every marker above must transitively reach. Unlike the
# sibling repos, this service does not fetch through ``w2v_common.net.safe_get``
# at all -- the streaming, byte-capped paths keep their own loop (see
# ``pin_target``'s docstring), so the chokepoints are the screens themselves:
#   validate_public_url            w2v_common.net -- the estate screen, used by
#                                  playwright_guard / external_process_target.
#                                  External to src/, hence an axiom here.
#   _assert_resolves_public        url_validator.py -- blocking DNS screen,
#                                  fails closed on an unresolvable host.
#   _resolve_public_addresses_cached  url_validator.py -- the resolving screen
#                                  behind resolve_fetch_pin / validate_fetch_url;
#                                  returns only addresses it proved public.
# A name is seeded ONLY if it performs the screen itself. A helper that merely
# rewrites a URL (``pin_target``) is not a chokepoint and must not be seeded --
# seeding it would make the marker it is meant to prove trivially true.
_SSRF_CHOKEPOINTS = (
    "validate_public_url",
    "_assert_resolves_public",
    "_resolve_public_addresses_cached",
)

# Names whose presence proves the function still stamps the identity triple by
# hand instead of inheriting the BaseServiceClient event hook.
_IDENTITY_MARKERS = ("get_identity_headers", "forward_headers")

SSRF_SINK = "SSRF_SINK"
IDENTITY_FORWARDED = "IDENTITY_FORWARDED"
GUARDED_POOL = "GUARDED_POOL"
NON_W2V_PEER = "NON_W2V_PEER"

_MARKERS: dict[str, tuple[str, ...]] = {
    SSRF_SINK: _SSRF_MARKERS,
    IDENTITY_FORWARDED: _IDENTITY_MARKERS,
    GUARDED_POOL: (),
    NON_W2V_PEER: (),
}

# A reason short enough to be a shrug is not a reason.
_MIN_REASON_CHARS = 60


class Exemption(NamedTuple):
    category: str
    reason: str
    # GUARDED_POOL only: (path, qualname) of each consumer that actually fetches
    # with this client. Each must exist and reference an _SSRF_MARKERS name.
    consumers: tuple[tuple[str, str], ...] = ()


class Construction(NamedTuple):
    path: str  # posix, relative to the repo root
    qualname: str  # enclosing def/class chain, or "<module>"
    lineno: int
    # Identifiers actually REFERENCED in the enclosing function — names and
    # attribute accesses, collected from the AST. Deliberately not the source
    # text: a guard that reads prose proves nothing about code (same defect
    # class as an Any-ratchet that counts comments).
    identifiers: frozenset[str]


# ── The allowlist ────────────────────────────────────────────────────────────
# Key: (path relative to repo root, enclosing function qualname).
# Adding an entry here is a DECISION, not a formality — read the module docstring
# before you add one. The default answer for a new S2S call is
# "subclass BaseServiceClient", which needs no entry at all.
ALLOWED: dict[tuple[str, str], Exemption] = {
    # --- Shared pooled transport ---
    ("src/asgi.py", "_init_brand_domain"): Exemption(
        GUARDED_POOL,
        "Builds the process-wide brand HTTP pool (app.state.http_client) at startup and "
        "fetches nothing itself. It is injected into LogoService and the brand pipeline, "
        "which fetch tenant-supplied brand asset URLs through get_guarded. Identity headers "
        "must not be baked into a pool whose every request targets a customer's own website.",
        (("src/domains/brand/services/logo_service.py", "LogoService.download_and_store"),),
    ),
    # --- SSRF sinks: arbitrary tenant-, page- or LLM-supplied URLs. Forwarding
    # the license token here would post a live credential to a third-party host. ---
    ("src/domains/critique/services/webhook.py", "deliver_webhook"): Exemption(
        SSRF_SINK,
        "POSTs a job-completion notification to the CUSTOMER's callback_url, screened by "
        "validate_callback_url. The destination is third-party by construction, so "
        "forwarding X-Tenant-Id: Bearer <license> would deliver a live credential to "
        "whatever host the customer registered. Named by the audit as an identity-dropping "
        "S2S call; it is not one.",
    ),
    ("src/domains/review/services/webhook.py", "deliver_webhook"): Exemption(
        SSRF_SINK,
        "Same contract as the critique webhook, for the review domain: customer-supplied "
        "callback_url behind validate_callback_url, with retry. Also named by the audit, "
        "also a false positive — the triple must NOT be forwarded to a customer host.",
    ),
    ("src/domains/scorecards/fetch.py", "fetch_html_capped"): Exemption(
        SSRF_SINK,
        "Fetches the audit target's page HTML. follow_redirects=False with every hop "
        "re-screened and re-pinned via _resolve_pin_target/pin_target (anti-rebinding), and "
        "the body streamed under a byte cap — which is why it is not routed through "
        "w2v_common.safe_get, per its docstring. The target is the customer's own site, so "
        "the license token must not ride along. Third audit finding; also a false positive.",
    ),
    ("src/domains/brand/services/css_parser.py", "CSSBrandParser._fetch_css"): Exemption(
        SSRF_SINK,
        "Fetches a stylesheet href resolved against a tenant-chosen page — attacker-"
        "controlled even when the page is public. Transport for get_guarded (screen per hop "
        "+ byte cap); an external origin must never receive our credential.",
    ),
    ("src/domains/brand/services/logo_service.py", "LogoService.download_and_store"): Exemption(
        SSRF_SINK,
        "Downloads a logo URL discovered on the tenant's page. This is the "
        "`if self._http_client: ... else: build one` fallback branch, and both branches "
        "fetch through get_guarded with MAX_SIZE_BYTES. Destination is a customer/CDN host.",
    ),
    (
        "src/domains/brand/services/metadata_extractor.py",
        "MetadataExtractor.extract_manifest",
    ): Exemption(
        SSRF_SINK,
        "Fetches a <link rel=manifest> href resolved against a tenant-chosen page, so it can "
        "point anywhere; get_guarded screens each hop under _MAX_MANIFEST_BYTES. Sending the "
        "identity triple would leak the license to an arbitrary origin.",
    ),
    (
        "src/domains/critique/services/critique_service/helpers.py",
        "CritiqueHelpersMixin._fetch_html",
    ): Exemption(
        SSRF_SINK,
        "Two constructions in one function: the credentialed client for the audit target, and "
        "the stripped rebuild after a cross-origin redirect. Every hop is re-screened by "
        "validate_callback_url. This function already deliberately DROPS the customer's own "
        "site credentials across an origin change — adding ours would invert that.",
    ),
    (
        "src/domains/critique/services/scanners/security_scanner/network_checks.py",
        "NetworkChecksMixin._probe_exposed_file",
    ): Exemption(
        SSRF_SINK,
        "Probes the scan target for exposed files (.env, .git/config) via guarded_get. The "
        "target is an arbitrary customer URL under active security scan — the single worst "
        "place to attach a bearer credential.",
    ),
    (
        "src/domains/critique/services/scanners/security_scanner/network_checks.py",
        "NetworkChecksMixin._check_cors",
    ): Exemption(
        SSRF_SINK,
        "Sends a crafted Origin header to the scan target through guarded_get to read back "
        "Access-Control-Allow-Origin. Arbitrary external host; no identity forwarded.",
    ),
    (
        "src/domains/critique/services/scanners/security_scanner/network_checks.py",
        "NetworkChecksMixin._check_redirect_chain",
    ): Exemption(
        SSRF_SINK,
        "Walks the target's redirect chain manually, re-screening each hop with "
        "_is_ssrf_safe_resolved — the whole point is to observe where an external site "
        "redirects, so the destination is untrusted by definition.",
    ),
    (
        "src/domains/critique/services/scanners/security_scanner/network_checks.py",
        "NetworkChecksMixin._check_directory_listing",
    ): Exemption(
        SSRF_SINK,
        "Requests common directory paths on the scan target through guarded_get to detect "
        "autoindex pages. Arbitrary external host; forwarding the triple would leak it.",
    ),
    (
        "src/domains/critique/services/scanners/security_scanner/scanner.py",
        "SecurityScanner.scan",
    ): Exemption(
        SSRF_SINK,
        "Top-level security scan of a customer URL: header checks, TLS, cookie flags, all "
        "issued through guarded_get behind _is_ssrf_safe_resolved. Never a w2v peer.",
    ),
    ("src/domains/critique/services/scanners/seo/geo.py", "scan_geo_live"): Exemption(
        SSRF_SINK,
        "Live GEO/AI-crawler audit: fetches /robots.txt and llms.txt on the customer origin "
        "via guarded_get after _is_ssrf_safe_resolved. External origin, no credential.",
    ),
    (
        "src/domains/critique/services/scanners/seo/internal_linking_live.py",
        "scan_internal_linking_live",
    ): Exemption(
        SSRF_SINK,
        "Crawls the customer site's sitemap and internal links with a bounded fan-out; "
        "follow_redirects=False because guarded_get/guarded_head walk and re-screen each hop, "
        "gated by _is_ssrf_safe_resolved. Fans out over many external URLs.",
    ),
    ("src/domains/critique/services/scanners/seo/technical.py", "scan_live"): Exemption(
        SSRF_SINK,
        "Live technical-SEO checks (robots.txt, sitemap.xml, canonical) against the customer "
        "origin, gated by _is_ssrf_safe_resolved. External origin; identity must not ride.",
    ),
    # --- Peers outside the w2v S2S convention: the triple is not their auth
    # channel, and sending it would be at best noise, at worst misread. ---
    (
        "src/domains/critique/services/scanners/code_quality_scanner.py",
        "CodeQualityScanner.scan",
    ): Exemption(
        NON_W2V_PEER,
        "code-graph-helper (CGH_API_URL) is an external analysis API authenticated by its own "
        "Authorization: Bearer CGH_API_KEY. It is not in w2v_common.discovery and does not "
        "read X-Tenant-Id/X-License-Id; the w2v Bearer form would only leak a license token "
        "to a service that has no use for it.",
    ),
    (
        "src/domains/critique/services/scanners/tool_adapters/zap_adapter.py",
        "ZapAdapter.is_available",
    ): Exemption(
        NON_W2V_PEER,
        "Health-pings the OWASP ZAP REST API at the operator-configured zap_api_url. ZAP "
        "authenticates with its own `apikey` query parameter and is not a w2v mesh peer; it "
        "would ignore the identity triple.",
    ),
    (
        "src/domains/critique/services/scanners/tool_adapters/zap_adapter.py",
        "ZapAdapter.scan",
    ): Exemption(
        NON_W2V_PEER,
        "Drives the same OWASP ZAP REST API (spider, passive scan, alerts) with the `apikey` "
        "parameter. The scan SEED is separately screened by resolve_external_scan_target so "
        "ZAP is not pointed at internal space; the ZAP host itself takes no w2v identity.",
    ),
}


def _walk_scoped(
    tree: ast.Module,
) -> tuple[list[tuple[ast.AST, str]], dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return (node, enclosing-qualname) pairs plus a qualname -> function-node map.

    A nested ``def`` extends the qualname but keeps REPORTING its outermost
    enclosing function — which is the unit a reviewer reasons about — and the
    outermost function is what lands in the map, so the substantiation check sees
    the whole body (nested helpers included) rather than one closure.
    """
    pairs: list[tuple[ast.AST, str]] = []
    scopes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit(node: ast.AST, prefix: str, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                reported = prefix if in_function else name
                if not in_function:
                    scopes[name] = child
                pairs.append((child, reported))
                visit(child, reported, True)
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                pairs.append((child, name))
                visit(child, name, in_function)
            else:
                pairs.append((child, prefix))
                visit(child, prefix, in_function)

    visit(tree, "", False)
    return pairs, scopes


def _is_httpx_client_call(node: ast.Call, imported_names: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in ("Client", "AsyncClient"):
        return isinstance(func.value, ast.Name) and func.value.id == "httpx"
    return isinstance(func, ast.Name) and func.id in imported_names


def _referenced_identifiers(node: ast.AST) -> frozenset[str]:
    """Every name / attribute actually referenced inside ``node``.

    AST-based on purpose — see :class:`Construction`. Docstrings and comments are
    not Name/Attribute nodes, so a marker can only match code.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return frozenset(names)


def _httpx_imported_names(tree: ast.Module) -> set[str]:
    """Names bound by ``from httpx import Client/AsyncClient [as x]``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "httpx":
            names.update(
                a.asname or a.name for a in node.names if a.name in ("Client", "AsyncClient")
            )
    return names


def _function_identifiers(root: Path = _SRC) -> dict[tuple[str, str], frozenset[str]]:
    """(path, qualname) -> identifiers referenced, for EVERY top-level function/method."""
    repo = root.parent
    table: dict[tuple[str, str], frozenset[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(repo).as_posix()
        _, scopes = _walk_scoped(tree)
        for qualname, node in scopes.items():
            table[(rel, qualname)] = _referenced_identifiers(node)
    return table


def find_constructions(root: Path = _SRC) -> list[Construction]:
    """Every bare ``httpx`` client construction under ``root``, keyed for the allowlist."""
    repo = root.parent
    found: list[Construction] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "httpx" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        imported = _httpx_imported_names(tree)
        rel = path.relative_to(repo).as_posix()
        pairs, scopes = _walk_scoped(tree)
        for node, qualname in pairs:
            if not isinstance(node, ast.Call) or not _is_httpx_client_call(node, imported):
                continue
            owner = scopes.get(qualname)
            names = _referenced_identifiers(owner) if owner else frozenset()
            found.append(Construction(rel, qualname or "<module>", node.lineno, names))
    return found


# ── The gate ─────────────────────────────────────────────────────────────────


def test_no_undocumented_bare_httpx_client() -> None:
    """Every bare httpx client under src/ must carry a written exemption."""
    offenders = [c for c in find_constructions() if (c.path, c.qualname) not in ALLOWED]
    assert not offenders, (
        "bare httpx client construction(s) with no exemption:\n"
        + "\n".join(f"  {c.path}:{c.lineno} in {c.qualname}()" for c in offenders)
        + "\n\nA raw httpx client bypasses BaseServiceClient's identity event hook, so the "
        "X-Tenant-Id / X-License-Id / X-Subject-Id triple is silently DROPPED on that call. "
        "Subclass w2v_common.clients.base.BaseServiceClient instead. If this is an "
        "SSRF-guarded fetch of an arbitrary URL (where forwarding a license token would "
        "leak a credential to a third party), add an entry to ALLOWED in "
        "tests/test_no_bare_httpx_client.py with a category and a written reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Non-vacuity: an exemption may not outlive the call site that excused it."""
    live = {(c.path, c.qualname) for c in find_constructions()}
    stale = sorted(key for key in ALLOWED if key not in live)
    assert not stale, (
        "ALLOWED entries match nothing in src/ — the call site was moved, renamed or "
        f"fixed, so the exemption must be deleted: {stale}"
    )


def test_every_exemption_states_a_category_and_a_reason() -> None:
    thin = {
        key: exemption
        for key, exemption in ALLOWED.items()
        if exemption.category not in _MARKERS or len(exemption.reason) < _MIN_REASON_CHARS
    }
    assert not thin, (
        "every exemption needs a known category and a reason of at least "
        f"{_MIN_REASON_CHARS} chars explaining why the identity triple must not be "
        f"forwarded here: {sorted(thin)}"
    )


def test_exemption_claims_are_still_true_in_the_source() -> None:
    """A structurally substantiated exemption must keep being substantiated.

    ``SSRF_SINK`` and ``IDENTITY_FORWARDED`` are claims about the code, so they
    are re-verified rather than trusted. Delete ``get_guarded`` from a sink, or
    ``forward_headers`` from a proxy, and the exemption stops applying here —
    exactly the moment a reviewer needs to be told.
    """
    by_key = {(c.path, c.qualname): c for c in find_constructions()}
    unsubstantiated: list[str] = []
    for key, exemption in ALLOWED.items():
        markers = _MARKERS[exemption.category]
        if not markers:
            continue
        construction = by_key.get(key)
        if construction is None:  # covered by the staleness test
            continue
        if not any(marker in construction.identifiers for marker in markers):
            unsubstantiated.append(
                f"{key[0]}:{key[1]} claims {exemption.category} but references none of {markers}"
            )
    assert not unsubstantiated, "\n".join(unsubstantiated)


def test_guarded_pool_consumers_are_still_guarded() -> None:
    """GUARDED_POOL is a claim about someone ELSE's function — check it there.

    A pooled client is safe only because every consumer fetches through the SSRF
    guard. So each declared consumer must (a) exist and (b) still reference a
    guard marker; a renamed or unguarded consumer reds this.
    """
    identifiers = _function_identifiers()
    broken: list[str] = []
    for key, exemption in ALLOWED.items():
        if exemption.category != GUARDED_POOL:
            continue
        if not exemption.consumers:
            broken.append(f"{key} claims GUARDED_POOL but names no consumers")
            continue
        for consumer in exemption.consumers:
            names = identifiers.get(consumer)
            if names is None:
                broken.append(f"{key} names consumer {consumer}, which does not exist")
            elif not any(marker in names for marker in _SSRF_MARKERS):
                broken.append(
                    f"{key} pool is consumed by {consumer}, which references none of "
                    f"{_SSRF_MARKERS} — the pool is no longer demonstrably guarded"
                )
    assert not broken, "\n".join(broken)


def test_ssrf_markers_actually_reach_a_chokepoint() -> None:
    """Guard the guard: a marker that screens nothing is a backdoor.

    The substantiation check is only as good as :data:`_SSRF_MARKERS`. The cheap
    way to defeat it is to add a plausible-sounding helper that fetches raw, and
    nothing else in this file would notice. So recompute the reachability from
    the AST: seed with :data:`_SSRF_CHOKEPOINTS` (the terminal screens) and close
    over "function F references a name already known to reach one". Every
    declared marker must land in that set.
    """
    identifiers = _function_identifiers()
    by_name: dict[str, list[frozenset[str]]] = {}
    for (_path, qualname), names in identifiers.items():
        by_name.setdefault(qualname.rsplit(".", 1)[-1], []).append(names)

    reaching = set(_SSRF_CHOKEPOINTS)
    changed = True
    while changed:
        changed = False
        for name, bodies in by_name.items():
            if name in reaching:
                continue
            if any(ref in reaching for body in bodies for ref in body):
                reaching.add(name)
                changed = True

    unreachable = [m for m in _SSRF_MARKERS if m not in reaching]
    assert not unreachable, (
        f"declared SSRF marker(s) {unreachable} do not transitively reach any of "
        f"{_SSRF_CHOKEPOINTS} anywhere in src/ -- an exemption substantiated by one of "
        "these proves nothing about the target having been screened. Remove the marker, "
        "or route the helper through a chokepoint."
    )


def test_guard_actually_finds_the_known_constructions() -> None:
    """The scanner must not be silently finding nothing (self-check).

    A guard whose finder is broken passes every other test in this file. Pin a
    floor and three known sites so a scanner regression reds instead of relaxing.
    """
    found = find_constructions()
    keys = {(c.path, c.qualname) for c in found}
    assert len(found) >= 20, f"scanner found only {len(found)} constructions; it is broken"
    assert ("src/domains/critique/services/webhook.py", "deliver_webhook") in keys
    assert ("src/domains/scorecards/fetch.py", "fetch_html_capped") in keys
    assert ("src/asgi.py", "_init_brand_domain") in keys
