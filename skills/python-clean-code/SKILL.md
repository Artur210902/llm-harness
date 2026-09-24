---
name: python-clean-code
version: "1.0"
description: Team standard for production Python modules - typing, docstrings, error contracts, dependency injection for testability, module hygiene.
applies_to: [code_generator, code_reviewer]
triggers: []
---
# Python clean code standard

- **PY-01** Every public function, method and constructor has full type hints; use
  `from __future__ import annotations` and modern syntax (`str | None`, `collections.abc`).
- **PY-02** Public API has a docstring that states behaviour, parameters and raised exceptions.
- **PY-03** Errors are part of the contract: raise a dedicated exception that subclasses the
  closest built-in (`class UnsafePathError(ValueError)`), never return `None`/`False` for
  invalid input, never swallow exceptions.
- **PY-04** Inject non-determinism (time, randomness, I/O) through parameters with sensible
  defaults (`clock: Callable[[], float] = time.monotonic`) so code is testable without sleeping.
- **PY-05** Standard library only unless the spec says otherwise; no global mutable state;
  constants in UPPER_CASE at module level.
- **PY-06** Validate arguments up-front with guard clauses; keep functions short and flat.
- **PY-07** Declare the public surface with `__all__`; private helpers start with `_`.

## Review checklist
Spec coverage - every acceptance criterion traceable to code; error contract matches spec;
no dead code; names say what things are; tests pass in the sandbox.
