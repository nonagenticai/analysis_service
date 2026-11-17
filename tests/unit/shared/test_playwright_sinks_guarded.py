"""Every Playwright BrowserContext in ``src/`` must carry the SSRF route guard.

Why this test is shaped the way it is
-------------------------------------
The guard in ``src/shared/security/playwright_guard.py`` only protects the
contexts it is actually installed on. When it landed it was wired into exactly
one of the two browser sinks in this repo, and CI stayed green for a day with
``axe_adapter`` driving a headless Chromium at a tenant-supplied URL with no
route interception at all -- a page free to fetch the estate's registry,
Postgres or the k8s API from inside the pod network.

A test naming the known-good files, or asserting a count, cannot catch that: it
passes by construction for every sink it was not told about, and a *new*
unguarded context is exactly the sink nobody thinks to add to the list. So the
denominator is **derived**: glob every ``.py`` under ``src/``, parse it, and
find the sinks. Nothing here hardcodes a filename, a module path or a count.

Which calls create a context
----------------------------
``new_context`` is not the only one. ``launch_persistent_context`` returns a
BrowserContext too, and an audit that only knows the first name lets a totally
unguarded persistent context ship CI-green -- verified: a synthetic
``launch_persistent_context`` with no guard at all, driving a page at the
estate's registry, produced **zero** findings here while the sibling
``video_production_service`` audit correctly reported it. Hence
``_CONTEXT_FACTORIES``: the set is the single place a new factory name is added,
and both the audit and the derived denominator read it.

What counts as guarded
----------------------
1. The context is bound to a name (an un-assigned factory call gives the
   caller no handle to install a guard on, so it can never be guarded).
2. ``install_ssrf_guard(<that name>)`` is called in the same function.
3. The guard call comes *after* the factory call and *before* the first use
   of the context -- a handler registered after the first navigation does not
   retroactively cover requests already in flight.

``browser.new_page()`` is failed outright: it creates an implicit context the
caller never sees, which is unguardable by construction.

The detector itself is falsified at the bottom of this file against synthetic
sources -- including a false-positive control -- so "0 findings" can never mean
"the parser silently stopped matching".
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"

GUARD_FUNC = "install_ssrf_guard"

# Every Playwright call that hands back a BrowserContext. Kept as a set rather
# than a literal at each use site so adding a factory cannot half-land: the
# audit, the derived denominator and the import check all read this one name.
_CONTEXT_FACTORIES = frozenset({"new_context", "launch_persistent_context"})


@dataclass(frozen=True)
class Violation:
    """One unguarded (or badly-ordered) browser sink."""

    where: str
    problem: str

    def __str__(self) -> str:
        return f"{self.where}: {self.problem}"


def _unwrap(node: ast.expr) -> ast.expr:
    """Strip ``await``/parenthesised wrappers to reach the underlying call."""
    while isinstance(node, ast.Await):
        node = node.value
    return node


def _attr_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    """Every ``<something>.<name>(...)`` call in *tree*."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def _is_guard_ref(func: ast.expr) -> bool:
    """True for a reference to the guard, whether imported plain or dotted."""
    if isinstance(func, ast.Name):
        return func.id == GUARD_FUNC
    return isinstance(func, ast.Attribute) and func.attr == GUARD_FUNC


def _guard_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``install_ssrf_guard(...)`` call, plain or dotted."""
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_guard_ref(node.func)
    ]


def _assigned_name(scope: ast.AST, call: ast.Call) -> str | None:
    """Return the single ``Name`` *call*'s result is bound to, if any."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _unwrap(node.value) is call:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                return node.targets[0].id
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _unwrap(node.value) is call
            and isinstance(node.target, ast.Name)
        ):
            return node.target.id
    return None


def _first_attribute_use(scope: ast.AST, var: str, *, after: int) -> int | None:
    """Line of the earliest ``var.<anything>`` access below line *after*."""
    lines = [
        node.lineno
        for node in ast.walk(scope)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == var
        and node.lineno > after
    ]
    return min(lines) if lines else None


def _scopes(tree: ast.Module) -> list[ast.AST]:
    """Every place a sink can live, INNERMOST first.

    Order is load-bearing. Each sink is judged once, by the first scope that
    contains it, so the innermost enclosing function must come first -- judging
    at module scope would let a guard installed in one function vouch for a
    context created in a different one. ``ast.walk`` is breadth-first, so
    reversing it puts the deepest functions ahead of their parents.
    """
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    scopes: list[ast.AST] = list(reversed(functions))
    scopes.append(tree)
    return scopes


def _launch_vars(scope: ast.AST) -> set[str]:
    """Names bound to a ``*.launch(...)`` result, i.e. Browser objects."""
    return {
        name
        for call in _attr_calls(scope, "launch")
        if (name := _assigned_name(scope, call)) is not None
    }


def _audit_implicit_contexts(
    scope: ast.AST, where: str, seen: set[int], browsers: set[str]
) -> list[Violation]:
    """Flag ``browser.new_page()`` -- an implicit context nobody can guard."""
    violations: list[Violation] = []
    for call in _attr_calls(scope, "new_page"):
        if id(call) in seen or not isinstance(call.func, ast.Attribute):
            continue
        recv = call.func.value
        if isinstance(recv, ast.Name) and recv.id in browsers:
            seen.add(id(call))
            violations.append(
                Violation(
                    f"{where}:{call.lineno}",
                    f"`{recv.id}.new_page()` creates an IMPLICIT BrowserContext the "
                    "caller never sees, so no SSRF route guard can be installed on "
                    "it. Create an explicit context, guard it, then open the page.",
                )
            )
    return violations


def _audit_one_context(
    scope: ast.AST, call: ast.Call, where: str, factory: str
) -> Violation | None:
    """Judge a single context-factory call. ``None`` means it is properly guarded."""
    at = f"{where}:{call.lineno}"

    var = _assigned_name(scope, call)
    if var is None:
        return Violation(
            at,
            f"`{factory}()` result is not bound to a name, so "
            f"`{GUARD_FUNC}` can never be installed on it.",
        )

    guards = [
        g
        for g in _guard_calls(scope)
        if g.args and isinstance(g.args[0], ast.Name) and g.args[0].id == var
    ]
    if not guards:
        return Violation(
            at,
            f"BrowserContext `{var}` is never passed to `{GUARD_FUNC}`. "
            "An unguarded context lets the page fetch sub-resources, "
            "follow redirects and issue XHR to internal targets.",
        )

    guard_line = min(g.lineno for g in guards)
    if guard_line <= call.lineno:
        return Violation(
            at,
            f"`{GUARD_FUNC}({var})` on line {guard_line} does not follow the "
            f"`{factory}()` that creates it.",
        )

    first_use = _first_attribute_use(scope, var, after=call.lineno)
    if first_use is not None and first_use < guard_line:
        return Violation(
            at,
            f"`{var}` is used on line {first_use}, BEFORE "
            f"`{GUARD_FUNC}` on line {guard_line}. A route handler "
            "registered after the first navigation does not cover it.",
        )

    return None


def audit_source(source: str, where: str) -> list[Violation]:
    """Return every unguarded browser sink in *source*.

    Exposed as a plain function so the detector can be falsified against
    synthetic inputs, not only against the tree it happens to be pointed at.
    """
    tree = ast.parse(source)
    violations: list[Violation] = []
    seen: set[int] = set()

    for scope in _scopes(tree):
        violations.extend(_audit_implicit_contexts(scope, where, seen, _launch_vars(scope)))

        for factory in sorted(_CONTEXT_FACTORIES):
            for call in _attr_calls(scope, factory):
                if id(call) in seen:
                    continue
                seen.add(id(call))
                if (violation := _audit_one_context(scope, call, where, factory)) is not None:
                    violations.append(violation)

    return violations


def _python_sources() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _sink_files() -> list[Path]:
    """Files that create a BrowserContext at all -- the denominator, derived."""
    found: list[Path] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(_attr_calls(tree, factory) for factory in _CONTEXT_FACTORIES):
            found.append(path)
    return found


class TestEveryBrowserContextIsGuarded:
    def test_src_tree_is_actually_scanned(self) -> None:
        """Non-vacuity: the glob must find real modules to parse."""
        sources = _python_sources()
        assert SRC_ROOT.is_dir(), f"src tree not found at {SRC_ROOT}"
        assert len(sources) > 100, (
            f"only {len(sources)} python files under {SRC_ROOT} -- the glob is "
            "not reaching the source tree, so a green result means nothing"
        )

    def test_at_least_one_browser_sink_exists(self) -> None:
        """Non-vacuity: if nobody creates a context, the audit proves nothing.

        Deliberately a floor, not an equality. A count assertion turns every
        legitimately-added sink into a test failure, which trains people to bump
        the number -- and a count says nothing about whether the sinks are safe.
        """
        assert _sink_files(), (
            f"none of {sorted(_CONTEXT_FACTORIES)} found anywhere under src/ -- either "
            "every Playwright sink was removed, or this detector has stopped matching"
        )

    def test_every_new_context_in_src_is_ssrf_guarded(self) -> None:
        violations: list[Violation] = []
        for path in _python_sources():
            violations.extend(
                audit_source(
                    path.read_text(encoding="utf-8"),
                    str(path.relative_to(REPO_ROOT)),
                )
            )

        assert not violations, "unguarded Playwright sink(s) found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )

    def test_sink_modules_import_the_guard(self) -> None:
        """A guarded call must resolve to the real guard, not a local stub."""
        for path in _sink_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name == GUARD_FUNC for alias in node.names)
                for node in ast.walk(tree)
            )
            assert imported, (
                f"{path.relative_to(REPO_ROOT)} creates a BrowserContext but never "
                f"imports `{GUARD_FUNC}`"
            )


class TestTheDetectorItselfCatchesThings:
    """Falsify the detector. A gate never shown to fail is not a gate."""

    def test_unguarded_context_is_reported(self) -> None:
        src = """
async def scan(url):
    browser = await pw.chromium.launch()
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "never passed to" in problems[0].problem

    def test_guarded_context_is_clean(self) -> None:
        """False-positive control: the fixed shape must produce NO findings."""
        src = """
async def scan(url):
    browser = await pw.chromium.launch()
    context = await browser.new_context()
    await install_ssrf_guard(context)
    page = await context.new_page()
    await page.goto(url)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_guard_installed_after_first_use_is_reported(self) -> None:
        src = """
async def scan(url):
    browser = await pw.chromium.launch()
    context = await browser.new_context()
    page = await context.new_page()
    await install_ssrf_guard(context)
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "BEFORE" in problems[0].problem

    def test_guarding_a_different_context_is_reported(self) -> None:
        """The guard must name *this* context, not merely appear in the file."""
        src = """
async def scan(url):
    browser = await pw.chromium.launch()
    safe = await browser.new_context()
    await install_ssrf_guard(safe)
    other = await browser.new_context()
    page = await other.new_page()
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "`other`" in problems[0].problem

    def test_unassigned_context_is_reported(self) -> None:
        src = """
async def scan(url):
    browser = await pw.chromium.launch()
    page = await (await browser.new_context()).new_page()
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert any("not bound to a name" in p.problem for p in problems)

    def test_implicit_context_via_browser_new_page_is_reported(self) -> None:
        src = """
async def scan(url):
    browser = await pw.chromium.launch()
    page = await browser.new_page()
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1
        assert "IMPLICIT" in problems[0].problem

    def test_sink_in_a_nested_function_is_not_missed(self) -> None:
        src = """
async def outer():
    async def inner(url):
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url)
    return inner
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "never passed to" in problems[0].problem

    def test_unguarded_launch_persistent_context_is_reported(self) -> None:
        """The drift this set exists to stop.

        Before ``_CONTEXT_FACTORIES`` this file only looked at ``new_context``,
        so this exact source -- a persistent context with no guard whatsoever,
        driving a page at a caller-supplied URL -- produced ZERO findings and
        shipped CI-green, while the sibling repo's audit reported it.
        """
        src = """
async def scan(url):
    context = await pw.chromium.launch_persistent_context("/tmp/profile")
    page = await context.new_page()
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "never passed to" in problems[0].problem

    def test_guarded_launch_persistent_context_is_clean(self) -> None:
        """False-positive control for the factory added above."""
        src = """
async def scan(url):
    context = await pw.chromium.launch_persistent_context("/tmp/profile")
    await install_ssrf_guard(context)
    page = await context.new_page()
    await page.goto(url)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_persistent_context_guarded_after_first_use_is_reported(self) -> None:
        """Ordering is judged for the new factory too, not only for new_context."""
        src = """
async def scan(url):
    context = await pw.chromium.launch_persistent_context("/tmp/profile")
    page = await context.new_page()
    await install_ssrf_guard(context)
    await page.goto(url)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "BEFORE" in problems[0].problem

    def test_every_declared_factory_is_actually_detected(self) -> None:
        """No factory may sit in the set without the detector reacting to it.

        Guards against the half-landed edit: a name added to
        ``_CONTEXT_FACTORIES`` that ``audit_source`` never reaches would make
        the set decorative, and the resulting "0 findings" indistinguishable
        from real coverage.
        """
        for factory in sorted(_CONTEXT_FACTORIES):
            src = f"""
async def scan(url):
    browser = await pw.chromium.launch()
    context = await browser.{factory}()
    page = await context.new_page()
    await page.goto(url)
"""
            problems = audit_source(src, "synthetic.py")
            assert len(problems) == 1, f"{factory} produced {problems}"
            assert "never passed to" in problems[0].problem, factory
