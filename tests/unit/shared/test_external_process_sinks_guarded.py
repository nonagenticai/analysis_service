"""No tenant-derived value may reach an external process without the chokepoint.

Why this test exists, and why the Playwright one could not cover it
-------------------------------------------------------------------
``tests/unit/shared/test_playwright_sinks_guarded.py`` proves every
``BrowserContext`` in ``src/`` carries ``install_ssrf_guard``. That guard
intercepts at the Playwright route layer, which exists only for browsers this
process drives over CDP. It is structurally blind to a sink that *spawns* a
program and hands it the tenant URL as argv: the Lighthouse adapter launches a
whole separate Chromium, and the nuclei adapter an active prober, and neither
has any Python in its request path.

So a repo can pass the Playwright audit with a perfect score while shipping an
unguarded scanner subprocess -- which is exactly what it did.

What is asserted
----------------
Any value that can be traced from a function's **parameters** to the argv of an
external-process spawn must first pass through
:func:`~src.shared.security.external_process_target.resolve_external_scan_target`.

Nothing here hardcodes a filename, a module path or a count. The denominator is
derived: glob every ``.py`` under ``src/``, parse it, find the spawns, and run
a small forward taint analysis over each enclosing function.

How "validated" is recognised -- and why it is a *type*, not a call
-------------------------------------------------------------------
Three things clear taint:

1. Binding a name to the result of ``resolve_external_scan_target(...)``.
2. A parameter annotated ``ExternalScanTarget``.
3. Indexing a module-level CONSTANT table, e.g. ``_PRESETS[mode]`` -- the value
   that reaches argv comes from the constant, not from the caller.

Rule 2 is what lets a validated target be passed between helpers without the
analysis losing track of it, and it cannot be gamed by accident: to launder a
raw ``str`` through it you would have to annotate it as ``ExternalScanTarget``,
which ``ty`` rejects. The verdict is carried by the type, so it is checkable
both statically here and by the type checker in CI.

Merely *calling* the chokepoint is deliberately not enough. A function that
validates ``url`` and then spawns with the original ``url`` still fails, because
the raw parameter -- not the returned target -- is what reached argv.

Deliberate conservatism, and the escape hatch
---------------------------------------------
The seed is *every* parameter, not "parameters that look like a URL". Matching
on names would let the next sink slip through by calling its variable ``t``,
and the whole point of deriving the denominator is not to depend on what
someone chose to call something.

The price is that a genuinely non-tenant parameter reaching argv is also
flagged. That is not purely a false positive -- an unconstrained caller value on
a command line is an argument-injection question in its own right -- and rule 3
is the escape: map the parameter through a constant table and pass the table's
value. It keeps the gate passable without weakening it, which matters, because a
gate that cannot be satisfied only teaches people to delete it.

The detector is falsified at the bottom of this file against synthetic sources,
including four false-positive controls, so "0 findings" can never quietly mean
"the parser stopped matching".
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"

CHOKEPOINT_FUNC = "resolve_external_scan_target"
CHOKEPOINT_TYPE = "ExternalScanTarget"
CHOKEPOINT_MODULE = "src.shared.security.external_process_target"

# Spawn sinks that need a receiver to be unambiguous. ``subprocess.run`` is a
# process spawn; ``session.run`` is not, and matching on the bare attribute
# would drown the real findings in noise.
_QUALIFIED_SINKS: dict[str, frozenset[str]] = {
    "subprocess": frozenset(
        {"run", "call", "check_call", "check_output", "getoutput", "getstatusoutput", "Popen"}
    ),
    "os": frozenset({"system", "popen"}),
}

# Names distinctive enough to mean "spawn a process" wherever they appear --
# whether reached as ``asyncio.create_subprocess_exec`` or imported bare.
_BARE_SINKS: frozenset[str] = frozenset(
    {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "Popen",
        "check_output",
        "check_call",
        "getstatusoutput",
        "posix_spawn",
        "posix_spawnp",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    }
)

# Never tenant data; excluding them keeps a method's receiver out of the taint
# seed, which would otherwise make every method on a class a finding.
_NON_TENANT_PARAMS = frozenset({"self", "cls"})

# CONSTANT_CASE, the convention PEP 8 reserves for module-level constants. A
# subscript of one of these yields the constant's own value, so the caller can
# only choose *which* constant is used, never what it contains.
_CONSTANT_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class Violation:
    """One external-process spawn reachable from unvalidated tenant input."""

    where: str
    problem: str

    def __str__(self) -> str:
        return f"{self.where}: {self.problem}"


def _unwrap(node: ast.expr) -> ast.expr:
    """Strip ``await`` wrappers to reach the underlying expression."""
    while isinstance(node, ast.Await):
        node = node.value
    return node


def _called_name(func: ast.expr) -> str | None:
    """Return the trailing identifier of a call target, if it has one."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _receiver_name(func: ast.expr) -> str | None:
    """Return the immediate receiver identifier of ``a.b(...)``, if simple."""
    if not isinstance(func, ast.Attribute):
        return None
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _is_spawn(call: ast.Call) -> bool:
    """True if *call* starts an external process."""
    name = _called_name(call.func)
    if name is None:
        return False
    if name in _BARE_SINKS:
        return True
    receiver = _receiver_name(call.func)
    return receiver is not None and name in _QUALIFIED_SINKS.get(receiver, frozenset())


def _spawn_calls(scope: ast.AST) -> list[ast.Call]:
    """Every external-process spawn inside *scope*."""
    return [node for node in ast.walk(scope) if isinstance(node, ast.Call) and _is_spawn(node)]


def _is_chokepoint_call(value: ast.expr) -> bool:
    """True if *value* is a call to the chokepoint, plain or dotted."""
    call = _unwrap(value)
    return isinstance(call, ast.Call) and _called_name(call.func) == CHOKEPOINT_FUNC


def _annotation_is_validated(annotation: ast.expr | None) -> bool:
    """True if *annotation* names the validated-target type.

    Accepts the bare name, a dotted reference, and the stringified form a
    ``from __future__ import annotations`` module may still write by hand.
    """
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return CHOKEPOINT_TYPE in annotation.value
    return any(
        isinstance(node, ast.Name | ast.Attribute)
        and (
            getattr(node, "id", None) == CHOKEPOINT_TYPE
            or getattr(node, "attr", "") == CHOKEPOINT_TYPE
        )
        for node in ast.walk(annotation)
    )


def _parameters(scope: ast.AST) -> list[ast.arg]:
    """Every declared parameter of *scope*, or ``[]`` at module level."""
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        return []
    spec = scope.args
    params = [*spec.posonlyargs, *spec.args, *spec.kwonlyargs]
    if spec.vararg is not None:
        params.append(spec.vararg)
    if spec.kwarg is not None:
        params.append(spec.kwarg)
    return params


def _bound_names(target: ast.expr) -> set[str]:
    """Every plain name bound by an assignment target, unpacking tuples."""
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _bindings(scope: ast.AST) -> list[tuple[list[ast.expr], ast.expr]]:
    """Every ``(targets, value)`` binding in *scope*, in source order.

    Covers plain and annotated assignment, augmented assignment, ``for`` and
    ``async for`` targets, ``with``/``async with`` aliases and the walrus --
    every construct that can move a tainted value onto a new name.
    """
    found: list[tuple[int, list[ast.expr], ast.expr]] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            found.append((node.lineno, list(node.targets), node.value))
        # ``AnnAssign`` is the only one of the two whose value is optional
        # (a bare ``x: int`` declaration binds nothing).
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value is not None:
            found.append((node.lineno, [node.target], node.value))
        elif isinstance(node, ast.For | ast.AsyncFor):
            found.append((node.lineno, [node.target], node.iter))
        elif isinstance(node, ast.NamedExpr):
            found.append((node.lineno, [node.target], node.value))
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None:
                    found.append((node.lineno, [item.optional_vars], item.context_expr))
    found.sort(key=lambda entry: entry[0])
    return [(targets, value) for _lineno, targets, value in found]


def _is_constant_lookup(value: ast.expr) -> bool:
    """True for ``SOME_CONSTANT[...]`` -- the value comes from the constant."""
    return (
        isinstance(value, ast.Subscript)
        and isinstance(value.value, ast.Name)
        and bool(_CONSTANT_NAME.match(value.value.id))
    )


def _seed_taint(scope: ast.AST) -> tuple[dict[str, set[str]], set[str]]:
    """Seed ``(tainted, validated)`` from *scope*'s own parameters.

    Parameters are where a tenant URL enters a scanner adapter. Each seeds its
    own provenance so a later finding can name the root, not just the local
    that happened to reach argv.
    """
    tainted: dict[str, set[str]] = {}
    validated: set[str] = set()
    for param in _parameters(scope):
        if param.arg in _NON_TENANT_PARAMS:
            continue
        if _annotation_is_validated(param.annotation):
            validated.add(param.arg)
        else:
            tainted[param.arg] = {param.arg}
    return tainted, validated


def _apply_binding(
    targets: list[ast.expr],
    value: ast.expr,
    tainted: dict[str, set[str]],
    validated: set[str],
) -> None:
    """Propagate taint across one binding, in place."""
    bound: set[str] = set()
    for target in targets:
        bound |= _bound_names(target)

    if _is_chokepoint_call(value) or _is_constant_lookup(value):
        validated |= bound
        for name in bound:
            tainted.pop(name, None)
        return

    sources = {node.id for node in ast.walk(value) if isinstance(node, ast.Name)}
    roots = {root for name in sources & tainted.keys() for root in tainted[name]}
    if not roots:
        return
    for name in bound - validated:
        tainted.setdefault(name, set()).update(roots)


def _taint(scope: ast.AST) -> dict[str, frozenset[str]]:
    """Map each tainted name in *scope* to the parameters it traces back to.

    Propagated to fixpoint so a binding order the walk did not anticipate (a
    loop, a helper defined below its use) cannot hide a flow.
    """
    tainted, validated = _seed_taint(scope)
    bindings = _bindings(scope)

    def snapshot() -> dict[str, frozenset[str]]:
        return {name: frozenset(roots) for name, roots in tainted.items()}

    # A fixpoint bounded by the number of bindings: each pass can only add
    # names or widen provenance, and both are finite.
    for _pass in range(len(bindings) + 1):
        before = snapshot()
        for targets, value in bindings:
            _apply_binding(targets, value, tainted, validated)
        if snapshot() == before:
            break

    return snapshot()


def _argument_names(call: ast.Call) -> set[str]:
    """Every plain name appearing in *call*'s arguments (never its callee)."""
    names: set[str] = set()
    for node in [*call.args, *(kw.value for kw in call.keywords)]:
        names |= {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}
    return names


def _scopes(tree: ast.Module) -> list[ast.AST]:
    """Every place a spawn can live, INNERMOST first.

    Order is load-bearing: a spawn is judged once, by the first scope that
    contains it, and that must be its own function -- a chokepoint call in a
    *different* function must never vouch for it.
    """
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    scopes: list[ast.AST] = list(reversed(functions))
    scopes.append(tree)
    return scopes


def audit_source(source: str, where: str) -> list[Violation]:
    """Return every tenant-reachable, unvalidated external-process spawn.

    A plain function so the detector can be falsified against synthetic inputs,
    not only against the tree it happens to be pointed at.
    """
    tree = ast.parse(source)
    violations: list[Violation] = []
    seen: set[int] = set()

    for scope in _scopes(tree):
        tainted = _taint(scope)
        for call in _spawn_calls(scope):
            if id(call) in seen:
                continue
            seen.add(id(call))
            reached = sorted(_argument_names(call) & tainted.keys())
            if not reached:
                continue
            roots = sorted({root for name in reached for root in tainted[name]})
            names = ", ".join(f"`{name}`" for name in reached)
            origins = ", ".join(f"`{root}`" for root in roots)
            violations.append(
                Violation(
                    f"{where}:{call.lineno}",
                    f"{names} reaches an external process spawned here, and traces "
                    f"back to parameter(s) {origins} without passing through "
                    f"`{CHOKEPOINT_FUNC}`. The spawned program resolves and fetches "
                    "its target itself, so no in-process route guard can see it. "
                    f"Validate first and pass the resulting `{CHOKEPOINT_TYPE}`.",
                )
            )

    return violations


def _python_sources() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _spawn_files() -> list[Path]:
    """Files that spawn an external process at all -- the denominator, derived."""
    return [
        path
        for path in _python_sources()
        if _spawn_calls(ast.parse(path.read_text(encoding="utf-8")))
    ]


def _chokepoint_user_files() -> list[Path]:
    """Files that claim validation by calling the chokepoint by name."""
    found: list[Path] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call) and _called_name(node.func) == CHOKEPOINT_FUNC
            for node in ast.walk(tree)
        ):
            found.append(path)
    return found


class TestEveryExternalProcessSinkIsValidated:
    def test_src_tree_is_actually_scanned(self) -> None:
        """Non-vacuity: the glob must find real modules to parse."""
        assert SRC_ROOT.is_dir(), f"src tree not found at {SRC_ROOT}"
        sources = _python_sources()
        assert len(sources) > 100, (
            f"only {len(sources)} python files under {SRC_ROOT} -- the glob is not "
            "reaching the source tree, so a green result means nothing"
        )

    def test_at_least_one_spawn_sink_exists(self) -> None:
        """Non-vacuity: with no spawn anywhere, the audit proves nothing.

        A floor, not an equality. A count assertion turns every legitimately
        added sink into a failure, which trains people to bump the number --
        and the number says nothing about whether the sink is safe.
        """
        assert _spawn_files(), (
            "no external-process spawn found anywhere under src/ -- either every "
            "subprocess sink was removed, or this detector has stopped matching"
        )

    def test_no_tenant_url_reaches_an_external_process_unvalidated(self) -> None:
        violations: list[Violation] = []
        for path in _python_sources():
            violations.extend(
                audit_source(
                    path.read_text(encoding="utf-8"),
                    str(path.relative_to(REPO_ROOT)),
                )
            )

        assert not violations, "unguarded external-process sink(s) found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )

    def test_chokepoint_callers_import_the_real_chokepoint(self) -> None:
        """A validated call must resolve to the real module, not a local stub."""
        for path in _chokepoint_user_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = any(
                isinstance(node, ast.ImportFrom)
                and node.module == CHOKEPOINT_MODULE
                and any(alias.name == CHOKEPOINT_FUNC for alias in node.names)
                for node in ast.walk(tree)
            )
            assert imported, (
                f"{path.relative_to(REPO_ROOT)} calls `{CHOKEPOINT_FUNC}` but does not "
                f"import it from `{CHOKEPOINT_MODULE}`"
            )


class TestTheDetectorItselfCatchesThings:
    """Falsify the detector. A gate never shown to fail is not a gate."""

    def test_raw_parameter_reaching_a_spawn_is_reported(self) -> None:
        src = """
async def scan(self, url):
    cmd = [binary, "-u", url]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        # Reported at the local that reached argv, traced back to the root.
        assert "`cmd`" in problems[0].problem
        assert "parameter(s) `url`" in problems[0].problem

    def test_validated_target_is_clean(self) -> None:
        """False-positive control: the fixed shape must produce NO findings."""
        src = """
async def scan(self, url):
    target = await resolve_external_scan_target(url)
    cmd = [binary, "-u", target.url]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_spawn_with_no_tenant_input_is_clean(self) -> None:
        """False-positive control: a constant argv is nobody's SSRF."""
        src = """
def versions():
    return subprocess.run(["node", "--version"], capture_output=True)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_validating_but_still_passing_the_raw_url_is_reported(self) -> None:
        """Calling the chokepoint is not enough -- its *result* must be used."""
        src = """
async def scan(self, url):
    target = await resolve_external_scan_target(url)
    cmd = [binary, "-u", url]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "`cmd`" in problems[0].problem

    def test_validating_after_building_argv_is_reported(self) -> None:
        """Order matters: argv built from the raw parameter is already tainted."""
        src = """
async def scan(self, url):
    cmd = [binary, "-u", url]
    target = await resolve_external_scan_target(url)
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_annotated_validated_parameter_is_clean(self) -> None:
        """A target handed between helpers keeps its verdict via the type."""
        src = """
async def run_one(target: ExternalScanTarget):
    cmd = [binary, target.url]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_unannotated_relay_parameter_is_reported(self) -> None:
        """The same relay without the annotation must NOT be waved through."""
        src = """
async def run_one(target):
    cmd = [binary, target.url]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_a_non_url_parameter_reaching_argv_is_reported(self) -> None:
        """The seed is every parameter -- documented conservatism, asserted."""
        src = """
async def run_one(target: ExternalScanTarget, mode):
    cmd = [binary, target.url, mode]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "parameter(s) `mode`" in problems[0].problem

    def test_constant_table_lookup_clears_taint(self) -> None:
        """False-positive control: the documented escape hatch must work."""
        src = """
_PRESETS = {"mobile": "--preset=mobile", "desktop": "--preset=desktop"}

async def run_one(target: ExternalScanTarget, mode):
    preset = _PRESETS[mode]
    cmd = [binary, target.url, preset]
    proc = await asyncio.create_subprocess_exec(*cmd)
"""
        assert audit_source(src, "synthetic.py") == []

    def test_subscripting_a_tainted_container_does_not_clear_taint(self) -> None:
        """The escape is a CONSTANT table, not any subscript at all."""
        src = """
async def run_one(payload):
    chosen = payload["url"]
    proc = await asyncio.create_subprocess_exec(binary, chosen)
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "parameter(s) `payload`" in problems[0].problem

    def test_taint_through_an_fstring_is_reported(self) -> None:
        src = """
def scan(url):
    arg = f"--target={url}"
    subprocess.run([binary, arg])
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems
        assert "`arg`" in problems[0].problem

    def test_taint_through_a_keyword_argument_is_reported(self) -> None:
        src = """
def scan(url):
    subprocess.Popen(binary, env=make_env(url))
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_os_system_shape_is_detected(self) -> None:
        src = """
def scan(url):
    os.system("curl " + url)
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_bare_imported_spawn_is_detected(self) -> None:
        """``from subprocess import Popen`` must not hide the sink."""
        src = """
def scan(url):
    Popen([binary, url])
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_unrelated_dot_run_is_not_a_spawn(self) -> None:
        """False-positive control: ``run`` needs the ``subprocess`` receiver."""
        src = """
async def go(url):
    await session.run(url)
    await asyncio.run(handler(url))
"""
        assert audit_source(src, "synthetic.py") == []

    def test_spawn_in_a_nested_function_is_not_missed(self) -> None:
        src = """
def outer():
    def inner(url):
        subprocess.run([binary, url])
    return inner
"""
        problems = audit_source(src, "synthetic.py")
        assert len(problems) == 1, problems

    def test_a_chokepoint_call_in_a_sibling_function_does_not_vouch(self) -> None:
        """Validation must live in the scope that spawns, not merely the file."""
        src = """
async def validate(url):
    return await resolve_external_scan_target(url)

def scan(url):
    subprocess.run([binary, url])
"""
        assert len(audit_source(src, "synthetic.py")) == 1

    def test_taint_survives_a_loop_binding(self) -> None:
        src = """
def scan(urls):
    for candidate in urls:
        subprocess.run([binary, candidate])
"""
        assert len(audit_source(src, "synthetic.py")) == 1
