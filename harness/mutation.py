"""Mutation check: does the generated test suite actually detect bugs?

A green sandbox only proves that the tests agree with the code. To check the oracle
itself, the harness plants small deterministic bugs (mutants) into the generated
module and re-runs the same tests: a good suite fails on (kills) most of them.

Operators (AST-based):
  DropLock     remove every `with <...lock...>:` at once and force thread switches inside the
               former critical sections (is thread safety tested at all?)
  DropClamp    `min(a, b)` / `max(a, b)` -> the computed argument (missing bound)
  CompareSwap  `>=`<->`>`, `<=`<->`<`, `==`<->`!=` (off-by-one at a boundary)
  FlipReturn   `return True` <-> `return False`
  DropRaise    `raise ...` -> `pass` (error contract not enforced)
"""

from __future__ import annotations

import ast
import copy
import itertools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .schemas import CodeArtifact, MutationReport, Survivor, TestSuite

_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.GtE: ast.Gt, ast.Gt: ast.GtE, ast.LtE: ast.Lt, ast.Lt: ast.LtE, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}


# What a surviving mutant says about the tests, phrased without implementation code: this
# text is routed to test_generator, which must stay a black-box oracle.
BEHAVIOUR = {
    "DropLock": "removing all locking (fully unsynchronized access) is not detected - no test exposes a race",
    "DropClamp": "removing a min/max bound (e.g. a cap or a floor) is not detected",
    "CompareSwap": "an off-by-one change at a boundary comparison is not detected",
    "FlipReturn": "inverting a boolean result is not detected",
    "DropRaise": "silently skipping a documented error is not detected",
}


def weakness(report: MutationReport, threshold: float) -> str | None:
    """Why the suite is too weak to trust, or None. Harness policy, not model opinion."""
    reasons = []
    if any(s.operator == "DropLock" for s in report.survivors):
        reasons.append("thread safety is untested")
    if report.score < threshold:
        reasons.append(f"mutation score {report.score:.2f} < {threshold:.2f}")
    if not reasons:
        return None
    counts = {op: sum(s.operator == op for s in report.survivors) for op in BEHAVIOUR}
    behaviours = [f"{BEHAVIOUR[op]} (x{n})" for op, n in counts.items() if n]
    return f"tests too weak ({report.headline()}; {', '.join(reasons)}): " + "; ".join(behaviours)


@dataclass(frozen=True)
class Mutant:
    operator: str
    line: int
    description: str
    code: str


# --- node operators: (name, matches node?, build the replacement node) -------------------


def _is_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.With) and any("lock" in ast.unparse(i.context_expr).lower() for i in node.items)


def _is_compare(node: ast.AST) -> bool:
    return isinstance(node, ast.Compare) and type(node.ops[0]) in _SWAP


def _swap_compare(node: ast.Compare) -> ast.Compare:
    mutated = copy.copy(node)
    mutated.ops = [_SWAP[type(node.ops[0])](), *node.ops[1:]]
    return mutated


def _is_clamp(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"min", "max"}
            and len(node.args) == 2 and not node.keywords)


def _unclamp(node: ast.Call) -> ast.expr:
    """The argument that carries the computation in `min(bound, value)` / `max(floor, value)`."""
    first, second = node.args
    if isinstance(first, ast.Constant):
        return second
    if isinstance(second, ast.Constant):
        return first
    simple = (ast.Name, ast.Attribute)
    return first if isinstance(second, simple) and not isinstance(first, simple) else second


def _is_bool_return(node: ast.AST) -> bool:
    return isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool)


def _flip_return(node: ast.Return) -> ast.Return:
    return ast.Return(value=ast.Constant(not node.value.value))


def _is_raise(node: ast.AST) -> bool:
    return isinstance(node, ast.Raise) and node.exc is not None


NodeOperator = tuple[str, Callable[[ast.AST], bool], Callable[[ast.AST], ast.AST]]
NODE_OPERATORS: tuple[NodeOperator, ...] = (
    ("DropClamp", _is_clamp, _unclamp),
    ("CompareSwap", _is_compare, _swap_compare),
    ("FlipReturn", _is_bool_return, _flip_return),
    ("DropRaise", _is_raise, lambda _: ast.Pass()),
)


class _Replace(ast.NodeTransformer):
    """Replace one node (by identity); a statement may be replaced by a list of statements."""

    def __init__(self, target: ast.AST, build: Callable[[ast.AST], ast.AST | list[ast.stmt]]):
        self.target, self.build = target, build

    def visit(self, node: ast.AST):
        if node is self.target:
            return self.build(node)
        return super().visit(node)


def _thread_switch() -> ast.stmt:
    return ast.parse("__import__('time').sleep(0)").body[0]


def _with_switches(statements: list[ast.stmt]) -> list[ast.stmt]:
    """Put a thread switch before every statement, recursively into if/for/while/try blocks."""
    result: list[ast.stmt] = []
    for statement in statements:
        for block in ("body", "orelse", "finalbody"):
            if isinstance(getattr(statement, block, None), list) and not isinstance(
                    statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                setattr(statement, block, _with_switches(getattr(statement, block)))
        result += [_thread_switch(), statement]
    return result


class _DropLocks(ast.NodeTransformer):
    """Remove the lock AND make the race observable.

    Under CPython's GIL a thread switch happens only at calls and backward jumps, so a lock-free
    check-then-act without calls in between is accidentally atomic and no test could tell. The
    mutant therefore also yields before each statement of the former critical section, the way a
    preemptive interpreter (free-threaded CPython, PyPy, a future refactoring) may.
    """

    def visit_With(self, node: ast.With):  # noqa: N802 (ast visitor naming)
        self.generic_visit(node)
        return _with_switches(node.body) if _is_lock(node) else node


# --- mutant generation ------------------------------------------------------------------


def _unparse(tree: ast.Module) -> str:
    return ast.unparse(ast.fix_missing_locations(tree))


def _drop_lock_mutant(source: str) -> list[Mutant]:
    tree = ast.parse(source)
    locks = [n for n in ast.walk(tree) if _is_lock(n)]
    if not locks:
        return []
    mutated = _DropLocks().visit(tree)
    return [Mutant("DropLock", locks[0].lineno,
                   f"removed all {len(locks)} lock block(s) and forced thread switches inside them",
                   _unparse(mutated))]


def _node_mutants(source: str, name: str, matches, build) -> list[Mutant]:
    count = sum(1 for n in ast.walk(ast.parse(source)) if matches(n))
    mutants = []
    for index in range(count):
        tree = ast.parse(source)  # fresh tree per mutant; ast.walk order is deterministic
        target = next(itertools.islice((n for n in ast.walk(tree) if matches(n)), index, None))
        before = ast.unparse(target)
        replacement = build(target)
        mutants.append(Mutant(name, target.lineno, f"`{before[:70]}` -> `{ast.unparse(replacement)[:70]}`",
                              _unparse(_Replace(target, lambda _, r=replacement: r).visit(tree))))
    return sorted(mutants, key=lambda m: m.line)


def generate_mutants(source: str, limit: int) -> list[Mutant]:
    """Up to `limit` mutants, round-robin over operators so every kind of bug is represented.

    DropLock comes first: for concurrent code it is the most important question.
    """
    try:
        groups = [_drop_lock_mutant(source)] + [_node_mutants(source, *op) for op in NODE_OPERATORS]
    except SyntaxError:
        return []
    interleaved = (m for row in itertools.zip_longest(*groups) for m in row if m is not None)
    return list(itertools.islice(interleaved, limit))


class MutationTester:
    """Runs the generated tests against each mutant in the pytest sandbox."""

    def __init__(self, sandbox, max_mutants: int = 12, workers: int = 4):
        self.sandbox = sandbox
        self.max_mutants = max_mutants
        self.workers = workers

    def run(self, code: CodeArtifact, tests: TestSuite, baseline_s: float) -> MutationReport:
        mutants = generate_mutants(code.code, self.max_mutants)
        # a mutant can turn a loop infinite: bound it relative to the green baseline run
        timeout = int(min(self.sandbox.timeout_s, max(10, 5 * baseline_s)))

        def is_killed(mutant: Mutant) -> bool:
            report = self.sandbox.run(code.model_copy(update={"code": mutant.code}), tests,
                                      extra_args=("-x",), timeout_s=timeout)
            return report.status != "passed"

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            killed = list(pool.map(is_killed, mutants))
        survivors = [Survivor(operator=m.operator, line=m.line, description=m.description)
                     for m, dead in zip(mutants, killed, strict=True) if not dead]
        return MutationReport(total=len(mutants), killed=len(mutants) - len(survivors), survivors=survivors)
