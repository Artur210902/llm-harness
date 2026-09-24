---
name: requirements-engineering
version: "1.0"
description: Turns a vague feature request into a testable specification - exact API and error contract, Given/When/Then criteria, edge-case taxonomy, abuse cases, explicit assumptions.
applies_to: [requirements_analyst]
triggers: []
---
# Requirements engineering

- **REQ-01** Pin the exact public API: module name, signatures with types, return types and the
  error contract (which exception, when, and its base class).
- **REQ-02** Write acceptance criteria as Given/When/Then; each one must be checkable by a single
  automated test.
- **REQ-03** Walk the edge-case taxonomy and keep what applies: empty/None, boundaries (0, 1,
  max, max+1), huge inputs, special characters/unicode, platform differences (Windows vs POSIX),
  time (clock jumps, long idle), concurrency (simultaneous callers), numeric (NaN, inf, negative).
- **REQ-04** State assumptions and out-of-scope items explicitly instead of guessing silently.
- **REQ-05** If any input comes from an untrusted party, add abuse cases (what an attacker would
  send) as acceptance criteria, not as an afterthought.
