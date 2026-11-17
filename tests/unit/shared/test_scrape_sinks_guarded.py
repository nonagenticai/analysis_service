"""Every delegated-scrape sink in ``src/`` must screen its URL first.

Why this test exists, and why the two sibling gates could not cover it
---------------------------------------------------------------------
``test_playwright_sinks_guarded.py`` proves every ``BrowserContext`` carries
``install_ssrf_guard``; ``test_external_process_sinks_guarded.py`` proves no
tenant value reaches argv unvalidated. Both are blind to a *third* shape: the
service does not fetch at all, it asks a **peer service** to fetch, and hands it
the tenant's URL. There is no browser and no subprocess in that path — only an
HTTP call to a sibling — so both existing audits score it perfect while it is
wide open. That is exactly what ``POST /api/brand/icp-from-url`` was: the
tenant's URL went straight into ``KagScrapeClient.scrape_url`` with no screen
anywhere in the module.

Delegating the dereference does not delegate the responsibility. This service
accepts the attacker-controlled input, and the peer hands the fetched body back,
so the ``200`` is an exfiltration channel. Measured rather than assumed: the KAG
gateway types its target as pydantic ``HttpUrl`` — pure syntax, which accepts
``http://169.254.169.254/`` — and proxies it onward with no SSRF screen of its
own; the real checks live two hops downstream, in other repos, behind their own
config switches. A guard that only exists over there is not this service's
guard.

What is asserted
----------------
For every call to a delegated-scrape method in ``src/``, the enclosing function
must invoke a URL guard on a **strictly earlier line**. A guard installed after
the sink is not a guard.

Nothing here hardcodes a filename, a module path or a count. The denominator is
derived: glob every ``.py`` under ``src/``, parse it, and find the sinks.

How "guarded" is recognised, and its honest limit
-------------------------------------------------
Two things count as a guard call:

0. Only a **resolving** screen counts at all. The structural predicates
   (``ensure_public_url``, ``_is_ssrf_safe``) inspect the URL *string* and
   nothing else, so ``https://cdn.attacker.example/`` with an ``A`` record on
   ``100.64.0.5`` satisfies every rule they apply. They were listed here as
   sufficient guards until this gate was tightened; no caller had reached for
   one, but the next author to do so would have satisfied the gate with the
   exact predicate the resolving PR removed from every fetch path. They are
   still real functions with their own unit tests and they still run as the
   cheap first half *inside* the resolving forms -- they just cannot clear a
   sink on their own. See :data:`BLIND_SCREENS`.
1. A direct call to one of :data:`GUARD_FUNCS`.
2. A call to a **module-local helper** that itself guards — computed to a fixed
   point, so a chain of helpers resolves. This matters because the natural way
   to screen a *list* of URLs is a small ``async`` helper, and a gate that only
   recognised the direct call would push people to inline the guard or, worse,
   to delete the gate.

The limit, stated rather than hidden: this checks that a guard runs before the
sink in the same function, **not** that the guard was applied to the exact value
that reached the sink. Full taint tracking is what
``test_external_process_sinks_guarded.py`` does, and it can do it because its
chokepoint returns a distinct *type* the type checker also enforces. The scrape
clients take plain ``str``, so there is no type to carry the verdict. Ordering
plus module-local resolution is the strongest honest rule available here; it
catches the real defect class (a sink with no screen anywhere near it) and it is
falsified below rather than assumed.

The detector is falsified at the bottom of this file against synthetic sources,
including false-positive controls, so "0 findings" can never quietly mean "the
parser stopped matching".
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"

# Methods that hand a URL to a peer service which then dereferences it. Named by
# METHOD, not by client class, so a sink reached through an alias, a protocol or
# a dependency-injected stand-in is still found.
SCRAPE_SINKS = frozenset({"scrape_url", "scrape_bulk", "scrape_bulk_sync"})

# The repo's URL screens that are sufficient in front of a delegated fetch --
# i.e. the RESOLVING ones. Every one of these applies the structural rules *and*
# then checks where the hostname actually resolves to, failing closed on a name
# that will not resolve. Each is asserted to still exist below, so a rename
# cannot silently empty this set and turn the gate vacuous.
#
# The DNS-blind screens are deliberately absent; see :data:`BLIND_SCREENS`.
GUARD_FUNCS = frozenset(
    {
        "validate_callback_url",
        "validate_public_url",
        "validate_fetch_url",
        "validate_fetch_url_blocking",
        "ensure_public_url_resolved",
        "_is_ssrf_safe_resolved",
        "_is_ssrf_safe_resolved_blocking",
    }
)

# Where each guard name must still be defined. A pair, not a bare list, so the
# non-vacuity check proves the *name* resolves rather than merely appearing.
# ``validate_public_url`` is absent because it lives in ``w2v_common.net``, not
# in this tree -- it is pinned by the sibling ``playwright_guard`` gate instead.
GUARD_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("validate_callback_url", "src/shared/security/url_validator.py"),
    ("validate_fetch_url", "src/shared/security/url_validator.py"),
    ("validate_fetch_url_blocking", "src/shared/security/url_validator.py"),
    ("ensure_public_url_resolved", "src/domains/brand/security.py"),
    (
        "_is_ssrf_safe_resolved",
        "src/domains/critique/services/scanners/security_scanner/__init__.py",
    ),
    (
        "_is_ssrf_safe_resolved_blocking",
        "src/domains/critique/services/scanners/security_scanner/__init__.py",
    ),
)

# The DNS-blind structural screens. They are real functions and they are
# legitimately used -- as the cheap first half *inside* a resolving guard, and
# as directly-testable predicates in their own unit tests -- but in front of a
# delegated fetch they decide nothing an attacker cannot arrange, so they must
# never satisfy this gate. Pinned by name, and asserted disjoint from
# GUARD_FUNCS below, so the widening cannot be undone by accident.
BLIND_SCREENS: tuple[tuple[str, str], ...] = (
    ("ensure_public_url", "src/domains/brand/security.py"),
    ("_is_ssrf_safe", "src/domains/critique/services/scanners/security_scanner/__init__.py"),
)


@dataclass(frozen=True)
class Violation:
    """One delegated-scrape sink with no screen ahead of it."""

    where: str
    problem: str

    def __str__(self) -> str:
        return f"{self.where}: {self.problem}"


def _called_name(call: ast.Call) -> str | None:
    """Return the bare name a call invokes, plain (``f()``) or dotted (``m.f()``)."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function definition anywhere in *tree*, nested ones included."""
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _guard_ref_lines(scope: ast.AST, names: frozenset[str] | set[str]) -> list[int]:
    """Lines in *scope* where any of *names* is referenced.

    A *reference*, not strictly a call, because the off-thread dispatch idiom
    this repo uses for blocking resolvers -- ``asyncio.to_thread(guard, url)``,
    as in ``playwright_guard.install_ssrf_guard`` -- passes the guard as an
    argument. A call-only detector would report every off-thread guard as no
    guard at all, and the obvious "fix" for that false positive is to delete
    the gate.

    Imports do not count: an ``ImportFrom`` alias is not a ``Name`` node, so
    merely importing a guard never satisfies this.
    """
    lines: list[int] = []
    for node in ast.walk(scope):
        if (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr in names
        ):
            lines.append(node.lineno)
    return lines


def _guarding_helpers(tree: ast.Module) -> set[str]:
    """Names of functions in *tree* that guard, directly or via another helper.

    Computed to a fixed point so ``a -> b -> validate_callback_url`` resolves.
    Bounded by the number of functions in the module, because each pass either
    adds a name or stops.
    """
    functions = _functions(tree)
    guarding: set[str] = set()
    while True:
        grew = False
        for func in functions:
            if func.name in guarding:
                continue
            # Exclude the function's own name so a recursive call cannot make a
            # function vouch for itself.
            if _guard_ref_lines(func, (GUARD_FUNCS | guarding) - {func.name}):
                guarding.add(func.name)
                grew = True
        if not grew:
            return guarding


def _guard_lines(func: ast.AST, helpers: set[str]) -> list[int]:
    """Lines within *func* on which a guard (direct or via *helpers*) is invoked."""
    return _guard_ref_lines(func, GUARD_FUNCS | helpers)


def _sink_calls(scope: ast.AST) -> list[ast.Call]:
    """Every delegated-scrape call in *scope*."""
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call) and _called_name(node) in SCRAPE_SINKS
    ]


def audit_source(source: str, where: str) -> list[Violation]:
    """Return every unscreened delegated-scrape sink in *source*.

    Exposed as a plain function so the detector can be falsified against
    synthetic inputs, not only against the tree it happens to be pointed at.

    Args:
        source: Python source text.
        where: Human-readable location prefix for the findings.

    Returns:
        One :class:`Violation` per offending sink, empty when the file is clean.
    """
    tree = ast.parse(source)
    helpers = _guarding_helpers(tree)
    violations: list[Violation] = []
    seen: set[int] = set()

    # Innermost first (``ast.walk`` is breadth-first, so reversing puts the
    # deepest functions ahead of their parents). Each sink is judged once, by
    # the closest enclosing function -- judging at an outer scope would let a
    # guard in a sibling branch vouch for a sink it never runs before.
    for func in reversed(_functions(tree)):
        guards = _guard_lines(func, helpers - {func.name})
        for call in _sink_calls(func):
            if id(call) in seen:
                continue
            seen.add(id(call))
            name = _called_name(call)
            earlier = [line for line in guards if line < call.lineno]
            if not earlier:
                violations.append(
                    Violation(
                        f"{where}:{call.lineno}",
                        f"`{name}(...)` hands a URL to a peer service that will "
                        f"dereference it, but `{func.name}` runs no URL screen "
                        "before it. A delegated fetch is still this service's "
                        "SSRF: it accepts the input and gets the body back.",
                    )
                )

    # A sink at module scope (import-time work) has no enclosing function to
    # carry a guard, so it can never be screened. Flag it outright.
    for call in _sink_calls(tree):
        if id(call) in seen:
            continue
        seen.add(id(call))
        violations.append(
            Violation(
                f"{where}:{call.lineno}",
                f"`{_called_name(call)}(...)` runs at module scope, where no "
                "per-request URL screen can precede it.",
            )
        )

    return violations


def _python_sources() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _sink_files() -> list[Path]:
    """Files that call a delegated-scrape method at all -- the denominator, derived."""
    return [
        path
        for path in _python_sources()
        if _sink_calls(ast.parse(path.read_text(encoding="utf-8")))
    ]


class TestEveryDelegatedScrapeIsScreened:
    def test_src_tree_is_actually_scanned(self) -> None:
        """Non-vacuity: the glob must find real modules to parse."""
        assert SRC_ROOT.is_dir(), f"src tree not found at {SRC_ROOT}"
        sources = _python_sources()
        assert len(sources) > 100, (
            f"only {len(sources)} python files under {SRC_ROOT} -- the glob is "
            "not reaching the source tree, so a green result means nothing"
        )

    def test_at_least_one_scrape_sink_exists(self) -> None:
        """Non-vacuity: if nobody delegates a scrape, the audit proves nothing.

        A floor, not an equality. A count assertion turns every legitimately
        added sink into a failure, which trains people to bump the number --
        and a count says nothing about whether the sinks are safe.
        """
        assert _sink_files(), (
            "no delegated-scrape call found anywhere under src/ -- either every "
            "sink was removed, or this detector has stopped matching"
        )

    def test_every_guard_name_still_resolves(self) -> None:
        """Non-vacuity: a renamed guard must break the gate, not empty it.

        ``GUARD_FUNCS`` is the only hardcoded thing here. If one of those names
        stopped existing, every sink would silently look unguarded -- or, worse,
        a sink calling the renamed guard would look unguarded and someone would
        "fix" it by widening the set. Pin the definitions instead.
        """
        for name, relative in GUARD_DEFINITIONS:
            assert name in GUARD_FUNCS, f"{name} is pinned but missing from GUARD_FUNCS"
            path = REPO_ROOT / relative
            assert path.is_file(), f"{relative} no longer exists"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            assert name in defined, f"`{name}` is no longer defined in {relative}"

    def test_no_dns_blind_screen_counts_as_a_guard(self) -> None:
        """A structural screen must never be a sufficient guard here.

        ``ensure_public_url`` and ``_is_ssrf_safe`` inspect the URL *string*
        only. A public name whose ``A`` record points at ``100.64.0.5``
        walks through both. Listing them here would let the next author satisfy
        this gate with the exact predicate #167 removed from every fetch path.
        """
        blind = {name for name, _ in BLIND_SCREENS}
        assert not (GUARD_FUNCS & blind), (
            f"DNS-blind screen(s) {sorted(GUARD_FUNCS & blind)} are listed as "
            "sufficient guards in front of a delegated fetch -- they are not"
        )

    def test_every_blind_screen_still_resolves(self) -> None:
        """Non-vacuity for the exclusion: a renamed blind screen must not empty it."""
        for name, relative in BLIND_SCREENS:
            path = REPO_ROOT / relative
            assert path.is_file(), f"{relative} no longer exists"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            assert name in defined, (
                f"`{name}` is no longer defined in {relative} -- the exclusion "
                "above is now naming nothing"
            )

    def test_every_delegated_scrape_in_src_is_screened(self) -> None:
        violations: list[Violation] = []
        for path in _python_sources():
            violations.extend(
                audit_source(
                    path.read_text(encoding="utf-8"),
                    str(path.relative_to(REPO_ROOT)),
                )
            )

        assert not violations, "unscreened delegated-scrape sink(s) found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )

    def test_sink_modules_import_a_guard(self) -> None:
        """A guard call must resolve to a real guard, not a same-named local stub."""
        for path in _sink_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            assert imported & GUARD_FUNCS, (
                f"{path.relative_to(REPO_ROOT)} delegates a scrape but imports none "
                f"of {sorted(GUARD_FUNCS)}"
            )


class TestTheDetectorItselfCatchesThings:
    """Falsify the detector. A gate never shown to fail is not a gate."""

    def test_unscreened_sink_is_reported(self) -> None:
        src = """
async def extract(body, client):
    scraped = await client.scrape_url(body.url)
    return scraped.text
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "runs no URL screen" in problems[0].problem

    def test_screened_sink_is_clean(self) -> None:
        """False-positive control: the fixed shape must produce NO findings."""
        src = """
async def extract(body, client):
    validate_callback_url(body.url)
    scraped = await client.scrape_url(body.url)
    return scraped.text
"""
        assert audit_source(src, "synthetic.py") == []

    def test_guard_after_the_sink_is_reported(self) -> None:
        """Screening a URL you have already sent is not screening it."""
        src = """
async def extract(body, client):
    scraped = await client.scrape_url(body.url)
    validate_callback_url(body.url)
    return scraped.text
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "runs no URL screen" in problems[0].problem

    def test_bulk_sink_is_covered_too(self) -> None:
        """The list-taking sink is not a blind spot."""
        src = """
async def fetch_many(client, urls):
    return await client.scrape_bulk(urls)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "scrape_bulk" in problems[0].problem

    def test_module_local_guard_helper_counts(self) -> None:
        """False-positive control: screening via a helper is real screening."""
        src = """
async def _screen(url):
    await asyncio.to_thread(validate_callback_url, url)

async def fetch_many(client, urls):
    for url in urls:
        await _screen(url)
    return await client.scrape_bulk(urls)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_helper_chain_resolves_to_a_fixed_point(self) -> None:
        """False-positive control: two hops of indirection still count."""
        src = """
def _inner(url):
    ensure_public_url_resolved(url)

def _outer(url):
    _inner(url)

async def fetch(client, url):
    _outer(url)
    return await client.scrape_url(url)
"""
        assert audit_source(src, "synthetic.py") == []

    @pytest.mark.parametrize("blind", [name for name, _ in BLIND_SCREENS])
    def test_a_dns_blind_screen_does_not_clear_a_sink(self, blind: str) -> None:
        """The loaded gun: a structural screen in front of a delegated fetch.

        This is the shape the gate used to accept. It reads like a guarded sink
        and it is not one -- ``https://cdn.attacker.example/`` with an ``A``
        record on ``100.64.0.5`` passes every rule either predicate applies,
        and the peer service then fetches it and hands the body back.
        """
        src = f"""
async def fetch(client, url):
    {blind}(url)
    return await client.scrape_url(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, f"`{blind}` is DNS-blind but cleared a delegated-scrape sink"
        assert "runs no URL screen" in problems[0].problem

    @pytest.mark.parametrize("blind", [name for name, _ in BLIND_SCREENS])
    def test_a_blind_screen_helper_chain_does_not_clear_a_sink_either(self, blind: str) -> None:
        """And it must not launder itself through the helper fixed point."""
        src = f"""
def _inner(url):
    {blind}(url)

async def fetch(client, url):
    _inner(url)
    return await client.scrape_url(url)
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_helper_that_does_not_guard_does_not_count(self) -> None:
        """A helper is not a guard merely because it is a helper."""
        src = """
def _normalize(url):
    return url.rstrip("/")

async def fetch(client, url):
    url = _normalize(url)
    return await client.scrape_url(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1

    def test_a_guarding_function_does_not_vouch_for_its_own_sink(self) -> None:
        """Self-reference must not launder a sink.

        A recursive (or merely self-naming) function would otherwise appear in
        its own helper set and clear itself.
        """
        src = """
async def fetch(client, url):
    result = await client.scrape_url(url)
    validate_callback_url(url)
    return result
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1

    def test_sink_in_a_nested_function_is_not_missed(self) -> None:
        src = """
def outer():
    async def inner(client, url):
        return await client.scrape_url(url)
    return inner
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1

    def test_guard_in_another_function_does_not_vouch(self) -> None:
        """A guard elsewhere in the module is not a guard on this path."""
        src = """
def admit(url):
    validate_callback_url(url)

async def fetch(client, url):
    return await client.scrape_url(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1

    def test_module_scope_sink_is_reported(self) -> None:
        src = """
PRELOADED = client.scrape_url("https://example.com/")
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "module scope" in problems[0].problem
