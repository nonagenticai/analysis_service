"""Every tenant URL written into a persisted row must be SSRF-screened first.

Why this test is shaped the way it is
-------------------------------------
``POST /api/critique`` and ``POST /api/critique/batch`` screen the scan target
with ``validate_callback_url`` before the job row is written.
``POST /api/critique/schedules`` did not -- and neither did ``PUT`` on the same
row, nor the scheduler that later turns that row into a real scan job. So a
tenant refused at the guarded endpoint could submit the identical internal URL
at the unguarded one and have the platform dereference it on a timer, hourly,
forever.

A test naming ``schedules.py``, or asserting "4 routes validate", cannot catch
that: it passes by construction for every write path it was not told about, and
the *next* unguarded write path is exactly the one nobody thinks to add to the
list. So the denominator is **derived** -- glob every ``.py`` under ``src/``,
parse it, and find the writes. Nothing here hardcodes a route, a filename, or a
count.

What counts as a write path
---------------------------
A URL-shaped value (``url`` / ``urls`` / ``*_url`` / ``*_urls``, read off an
object or a mapping) that, inside one function, flows into a **persistence
sink**:

1. a keyword argument of an **ORM model constructor** -- and the set of ORM
   model classes is itself derived, by parsing every ``models.py`` for
   ``class X(Base)``, so a new table is covered the day it is declared;
2. an assignment onto a URL-shaped attribute (``schedule.url = body.url``);
3. an argument to a ``create_*`` / ``update_*`` call -- the repository/service
   convention this repo writes rows through (``service.create_job(url=...)``).

This is deliberately narrower than "every URL a route touches". The other
domains screen at the *fetcher* instead (``scorecards/fetch.py``,
``brand/security.py``, ``security_scanner/http_guard.py``), which is an equally
valid place to put the check and is covered by their own tests; asserting
route-level validation on them would report guarded code as a defect. What is
special about a **persisted** URL is that the fetch happens later, somewhere
else, possibly repeatedly -- so the write is the last point at which anything
about the request is still available to reject it.

What counts as a guard
----------------------
A call, before the sink, to a function that screens URLs. The set of those is
also derived: rooted at the two primitives (:data:`_PRIMITIVE_GUARDS`) and
closed transitively over "a function that passes *one of its own URL-named
parameters* to a guard". Both qualifiers earn their place --

* *its own parameter*, so a route handler that validates ``body.url`` and then
  writes it does not thereby become a blanket URL screen (which is what a plain
  "does this function mention the validator" check would wrongly accept);
* *URL-named*, so merely forwarding an unrelated argument (a session, a
  service) into something that happens to be a guard does not promote the
  caller. Without it the closure runs away: the first draft admitted 56 names,
  including ``process_job`` and ``check_and_run_schedules``.

What survives is the real wrappers -- ``ensure_safe_url``,
``_ensure_public_url``, ``_ensure_schedule_target_allowed``, and the scanner
entry points that screen the ``url`` they are handed.

The detector is falsified at the bottom of this file against synthetic sources,
including a false-positive control, so "0 findings" can never quietly mean "the
parser stopped matching".
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"

#: The two functions that actually decide whether a URL may be dereferenced.
#: Everything else recognised as a guard is derived from these by closure --
#: these two are named because a fixpoint needs a root, not because the list of
#: guards is maintained by hand.
_PRIMITIVE_GUARDS = frozenset({"validate_callback_url", "_is_ssrf_safe"})

#: Callables that run another callable, so ``f(guard, x)`` really is ``guard(x)``.
#: The scheduler screens off the event loop this way because the validator does
#: blocking DNS; without this normalisation the guard would be invisible here.
_INDIRECT_INVOKERS = frozenset({"to_thread"})

#: Prefixes of the repository/service methods this repo persists rows through.
_WRITE_CALL_PREFIXES = ("create_", "update_")


@dataclass(frozen=True)
class Violation:
    """One tenant URL written to a row without being screened."""

    where: str
    problem: str

    def __str__(self) -> str:
        return f"{self.where}: {self.problem}"


def _is_url_name(name: str) -> bool:
    """True for an identifier that denotes a URL by this repo's convention."""
    return name in {"url", "urls"} or name.endswith(("_url", "_urls"))


def _symbol(node: ast.expr) -> str | None:
    """Render *node* as a stable symbol, or ``None`` if it is not URL-shaped.

    Covers the two ways a URL arrives here: an attribute of a request body or an
    ORM row (``body.url``, ``original.callback_url``) and a mapping subscript
    (``row["url"]``, which is how the scheduler reads a claimed schedule).
    """
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}" if _is_url_name(node.attr) else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        key = node.slice.value
        return f"{node.value.id}[{key!r}]" if _is_url_name(key) else None
    return None


def _callee_name(func: ast.expr) -> str | None:
    """Bare name of a called function, whether referenced plain or dotted."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _normalize_call(call: ast.Call) -> tuple[str | None, list[ast.expr]]:
    """Resolve *call* to ``(callee_name, effective_positional_args)``.

    ``asyncio.to_thread(validate_callback_url, url)`` is reported as
    ``("validate_callback_url", [url])``: the guard is genuinely invoked on that
    argument, and a detector that only looked at ``call.func`` would score it as
    a call to ``to_thread`` and miss the screen entirely.
    """
    name = _callee_name(call.func)
    if name in _INDIRECT_INVOKERS and call.args:
        inner = _callee_name(call.args[0])
        if inner is not None:
            return inner, list(call.args[1:])
    return name, list(call.args)


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function in the module, nested ones included."""
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _scopes(tree: ast.Module) -> list[ast.AST]:
    """Every place a write can live, INNERMOST first.

    Order is load-bearing. Each sink is judged once, by the first scope that
    contains it, so the innermost enclosing function must come first -- judging
    at an outer scope would let a screen installed in one function vouch for a
    write performed in a different one, and would also count the same write
    twice. ``ast.walk`` is breadth-first, so reversing it puts the deepest
    functions ahead of their parents.
    """
    scopes: list[ast.AST] = list(reversed(_functions(tree)))
    scopes.append(tree)
    return scopes


def _param_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = fn.args
    return {
        arg.arg
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
        if arg is not None
    }


def _screens_own_parameter(fn: ast.FunctionDef | ast.AsyncFunctionDef, guards: set[str]) -> bool:
    """True if *fn* hands one of its own URL parameters straight to a known guard.

    Both halves are load-bearing:

    * *its own parameter*, so a route handler that validates ``body.url`` and
      then writes it does not itself become a blanket URL screen;
    * a *URL* parameter, so merely forwarding some unrelated argument (a
      session, a service) to something that happens to be a guard does not
      promote the caller. Without that, the closure runs away and half the
      scanner call tree ends up counted as a screen.
    """
    params = {name for name in _param_names(fn) if _is_url_name(name)}
    if not params:
        return False
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        name, args = _normalize_call(call)
        if name not in guards:
            continue
        for arg in [*args, *(kw.value for kw in call.keywords)]:
            if isinstance(arg, ast.Name) and arg.id in params:
                return True
    return False


def derive_guard_names(sources: dict[str, str]) -> set[str]:
    """Close :data:`_PRIMITIVE_GUARDS` over wrappers that screen their own argument.

    Args:
        sources: ``{where: source}`` for the tree to analyse.

    Returns:
        Every function name that may be relied on as a URL screen.
    """
    trees = [ast.parse(src) for src in sources.values()]
    functions = [fn for tree in trees for fn in _functions(tree)]

    guards = set(_PRIMITIVE_GUARDS)
    changed = True
    while changed:
        changed = False
        for fn in functions:
            if fn.name not in guards and _screens_own_parameter(fn, guards):
                guards.add(fn.name)
                changed = True
    return guards


def derive_orm_models(sources: dict[str, str]) -> set[str]:
    """Names of declarative ORM classes -- ``class X(Base)`` -- across *sources*."""
    models: set[str] = set()
    for src in sources.values():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "Base" for base in node.bases
            ):
                models.add(node.name)
    return models


def _aliases(fn: ast.AST, symbol: str) -> set[str]:
    """Local names that stand in for *symbol* (``x = body.url``, ``for u in body.urls``)."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _symbol(node.value) == symbol
        ):
            names.add(node.targets[0].id)
        elif (
            isinstance(node, ast.For | ast.AsyncFor | ast.comprehension)
            and isinstance(node.target, ast.Name)
            and _symbol(node.iter) == symbol
        ):
            names.add(node.target.id)
    return names


def _guard_lines(fn: ast.AST, symbol: str, guards: set[str]) -> list[int]:
    """Lines where *symbol* (or an alias of it) is passed to a guard."""
    accepted = {symbol} | _aliases(fn, symbol)
    lines: list[int] = []
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        name, args = _normalize_call(call)
        if name not in guards:
            continue
        for arg in [*args, *(kw.value for kw in call.keywords)]:
            rendered = _symbol(arg) or (arg.id if isinstance(arg, ast.Name) else None)
            if rendered in accepted:
                lines.append(call.lineno)
                break
    return lines


def _loads_a_row(scope: ast.AST, orm_models: set[str]) -> bool:
    """True if *scope* names an ORM model at all, i.e. it works with real rows.

    Used to tell ``schedule.url = body.url`` (a row write, in a function that
    ``select()``s ``CritiqueSchedule`` two lines up) from ``brand.logo_url =
    ...`` in the brand pipeline, which mutates a pydantic *response* object that
    is never persisted. Without this, the second is reported as a defect.

    Residual, stated rather than hidden: a function that receives an
    already-loaded row from its caller and never names the model class is not
    recognised. Every row write in this repo today loads the row it mutates.
    """
    return any(isinstance(node, ast.Name) and node.id in orm_models for node in ast.walk(scope))


def _sinks(fn: ast.AST, orm_models: set[str]) -> list[tuple[str, ast.stmt | ast.expr, str]]:
    """Every ``(symbol, node, description)`` writing a URL to a persisted row."""
    found: list[tuple[str, ast.stmt | ast.expr, str]] = []
    row_scope = _loads_a_row(fn, orm_models)

    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name, args = _normalize_call(node)
            callee = _callee_name(node.func)
            if callee in orm_models:
                kind = f"ORM model `{callee}(...)`"
            elif name is not None and name.startswith(_WRITE_CALL_PREFIXES):
                kind = f"persistence call `{name}(...)`"
            else:
                continue
            for arg in [*args, *(kw.value for kw in node.keywords)]:
                if (symbol := _symbol(arg)) is not None:
                    found.append((symbol, node, kind))
        elif (
            row_scope
            and isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and _is_url_name(node.targets[0].attr)
            and (symbol := _symbol(node.value)) is not None
        ):
            target = node.targets[0]
            owner = target.value.id if isinstance(target.value, ast.Name) else "<expr>"
            found.append((symbol, node, f"row attribute `{owner}.{target.attr} = ...`"))

    return found


def find_sinks(source: str, orm_models: set[str]) -> list[tuple[str, int, str]]:
    """Every distinct URL-write in *source*, each judged by its innermost scope."""
    out: list[tuple[str, int, str]] = []
    seen: set[tuple[int, str]] = set()
    for scope in _scopes(ast.parse(source)):
        for symbol, node, kind in _sinks(scope, orm_models):
            if (key := (id(node), symbol)) in seen:
                continue
            seen.add(key)
            out.append((symbol, node.lineno, kind))
    return out


def audit_source(
    source: str, where: str, *, guards: set[str], orm_models: set[str]
) -> list[Violation]:
    """Return every unscreened tenant-URL write in *source*.

    Exposed as a plain function so the detector can be falsified against
    synthetic inputs, not only against the tree it happens to be pointed at.
    """
    violations: list[Violation] = []
    seen: set[tuple[int, str]] = set()

    for scope in _scopes(ast.parse(source)):
        name = getattr(scope, "name", "<module>")
        for symbol, node, kind in _sinks(scope, orm_models):
            if (key := (id(node), symbol)) in seen:
                continue
            seen.add(key)

            line = node.lineno
            lines = _guard_lines(scope, symbol, guards)
            at = f"{where}:{line}"
            if not lines:
                violations.append(
                    Violation(
                        at,
                        f"`{symbol}` is written to persistence via {kind} in "
                        f"`{name}` without ever being screened. A stored URL is "
                        "dereferenced later, elsewhere, and possibly repeatedly -- "
                        "the write is the last point at which it can be refused.",
                    )
                )
            elif min(lines) > line:
                violations.append(
                    Violation(
                        at,
                        f"`{symbol}` is written via {kind} in `{name}` on line "
                        f"{line}, but only screened afterwards on line {min(lines)}. "
                        "A check after the write does not stop the write.",
                    )
                )
    return violations


def _python_sources() -> dict[str, str]:
    return {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(SRC_ROOT.rglob("*.py"))
    }


def _model_sources() -> dict[str, str]:
    return {where: src for where, src in _python_sources().items() if where.endswith("models.py")}


class TestEveryPersistedUrlIsScreened:
    def test_src_tree_is_actually_scanned(self) -> None:
        """Non-vacuity: the glob must find real modules to parse."""
        sources = _python_sources()
        assert SRC_ROOT.is_dir(), f"src tree not found at {SRC_ROOT}"
        assert len(sources) > 100, (
            f"only {len(sources)} python files under {SRC_ROOT} -- the glob is "
            "not reaching the source tree, so a green result means nothing"
        )

    def test_orm_model_set_is_derived_and_non_empty(self) -> None:
        models = derive_orm_models(_model_sources())
        assert "CritiqueSchedule" in models, (
            "the ORM-class derivation stopped matching `class X(Base)` -- with an "
            f"empty model set every model constructor stops being a sink. Got: {sorted(models)}"
        )

    def test_guard_set_is_derived_beyond_the_primitives(self) -> None:
        """The closure must actually close: wrappers have to be discovered."""
        guards = derive_guard_names(_python_sources())
        assert guards >= _PRIMITIVE_GUARDS
        derived = guards - _PRIMITIVE_GUARDS
        assert derived, (
            "no guard wrapper was derived from the primitives -- the fixpoint is "
            "not running, so only literal `validate_callback_url(x)` would count"
        )

    def test_at_least_one_url_write_path_exists(self) -> None:
        """Non-vacuity: if nothing writes a URL, the audit proves nothing.

        A floor, not an equality. A count assertion turns every legitimately
        added write path into a failure, which trains people to bump the number
        -- and a count says nothing about whether the writes are screened.
        """
        sources = _python_sources()
        orm_models = derive_orm_models(_model_sources())
        total = sum(len(find_sinks(src, orm_models)) for src in sources.values())
        assert total, (
            "no tenant-URL persistence sink found anywhere under src/ -- either "
            "every write path was removed, or this detector has stopped matching"
        )

    def test_every_persisted_url_in_src_is_screened(self) -> None:
        sources = _python_sources()
        guards = derive_guard_names(sources)
        orm_models = derive_orm_models(_model_sources())

        violations: list[Violation] = []
        for where, src in sources.items():
            violations.extend(audit_source(src, where, guards=guards, orm_models=orm_models))

        assert not violations, "unscreened tenant-URL write path(s) found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )


class TestTheDetectorItselfCatchesThings:
    """Falsify the detector. A gate never shown to fail is not a gate."""

    GUARDS = frozenset(_PRIMITIVE_GUARDS)
    MODELS = frozenset({"CritiqueSchedule"})

    def _audit(self, src: str) -> list[Violation]:
        return audit_source(
            src, "synthetic.py", guards=set(self.GUARDS), orm_models=set(self.MODELS)
        )

    def test_unscreened_orm_write_is_reported(self) -> None:
        src = """
async def create_schedule(body, db):
    row = CritiqueSchedule(url=body.url, cron=body.cron)
    db.add(row)
"""
        problems = self._audit(src)
        assert len(problems) == 1, problems
        assert "body.url" in problems[0].problem
        assert "without ever being screened" in problems[0].problem

    def test_screened_orm_write_is_clean(self) -> None:
        """False-positive control: the fixed shape must produce NO findings."""
        src = """
async def create_schedule(body, db):
    validate_callback_url(body.url)
    row = CritiqueSchedule(url=body.url, cron=body.cron)
    db.add(row)
"""
        assert self._audit(src) == []

    def test_unscreened_row_attribute_write_is_reported(self) -> None:
        src = """
async def update_schedule(body, db):
    result = await db.execute(select(CritiqueSchedule))
    s = result.scalar_one_or_none()
    if body.url is not None:
        s.url = body.url
"""
        problems = self._audit(src)
        assert len(problems) == 1, problems
        assert "row attribute" in problems[0].problem

    def test_attribute_write_on_a_non_row_is_not_reported(self) -> None:
        """False-positive control: the brand pipeline mutates a response model.

        ``brand`` is a ``BrandResponse`` assembled from the page that was just
        fetched -- nothing is persisted and nothing re-fetches it later. Before
        the ``_loads_a_row`` condition existed this was reported as a defect.
        """
        src = """
async def run(ctx, brand, result):
    brand.logo_url = logo_intel.primary_logo_url
"""
        assert self._audit(src) == []

    def test_unscreened_create_call_is_reported(self) -> None:
        src = """
async def fire(service, row):
    await service.create_job(url=row["url"])
"""
        problems = self._audit(src)
        assert len(problems) == 1, problems
        assert "row['url']" in problems[0].problem

    def test_screening_a_different_url_does_not_vouch(self) -> None:
        """The guard must name *this* URL, not merely appear in the function."""
        src = """
async def rerun(original, service):
    validate_callback_url(original.callback_url)
    await service.create_job(url=original.url, callback_url=original.callback_url)
"""
        problems = self._audit(src)
        assert len(problems) == 1, problems
        assert "original.url" in problems[0].problem

    def test_screen_after_the_write_is_reported(self) -> None:
        src = """
async def create_schedule(body, db):
    row = CritiqueSchedule(url=body.url)
    validate_callback_url(body.url)
    db.add(row)
"""
        problems = self._audit(src)
        assert len(problems) == 1, problems
        assert "does not stop the write" in problems[0].problem

    def test_loop_alias_counts_as_screening_the_collection(self) -> None:
        """``for u in body.urls: validate(u)`` is how batch screens its list."""
        src = """
async def create_batch(body, service):
    for candidate in body.urls:
        validate_callback_url(candidate)
    await service.create_batch(urls=body.urls)
"""
        assert self._audit(src) == []

    def test_guard_reached_through_to_thread_counts(self) -> None:
        src = """
async def fire(service, row):
    await asyncio.to_thread(validate_callback_url, row["url"])
    await service.create_job(url=row["url"])
"""
        assert self._audit(src) == []

    def test_non_url_field_is_not_a_sink(self) -> None:
        """False-positive control: non-URL columns must not be dragged in."""
        src = """
async def update_tags(body, job):
    job.tags = body.tags
    await service.create_job(categories=body.categories)
"""
        assert self._audit(src) == []

    def test_write_in_a_nested_function_is_not_missed(self) -> None:
        src = """
async def outer(service):
    async def inner(body):
        await service.create_job(url=body.url)
    return inner
"""
        problems = self._audit(src)
        assert len(problems) == 1, problems
        assert "body.url" in problems[0].problem


class TestGuardDerivation:
    """The wrapper closure is the part most likely to silently over-accept."""

    def test_wrapper_that_screens_its_parameter_becomes_a_guard(self) -> None:
        sources = {
            "w.py": """
async def ensure_target_allowed(url):
    validate_callback_url(url)
"""
        }
        assert "ensure_target_allowed" in derive_guard_names(sources)

    def test_function_that_screens_something_else_is_not_a_guard(self) -> None:
        """A route validating ``body.url`` must not become a blanket URL screen.

        Otherwise ``create_critique(...)`` would count as a guard, and calling it
        with an unscreened URL would look safe.
        """
        sources = {
            "r.py": """
async def create_critique(body, service):
    validate_callback_url(body.url)
    await service.create_job(url=body.url)
"""
        }
        assert "create_critique" not in derive_guard_names(sources)

    def test_closure_is_transitive(self) -> None:
        sources = {
            "a.py": """
def inner_guard(url):
    _is_ssrf_safe(url)

def outer_guard(url):
    inner_guard(url)
"""
        }
        guards = derive_guard_names(sources)
        assert guards >= {"inner_guard", "outer_guard"}
