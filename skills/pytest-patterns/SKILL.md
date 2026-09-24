---
name: pytest-patterns
version: "1.3"
description: Testing methodology for black-box pytest suites - equivalence classes, boundaries, negative tests, deterministic time, isolated file system.
applies_to: [test_generator, code_reviewer]
triggers: []
---
# pytest patterns

- **TST-01** Arrange-Act-Assert; one behaviour per test; test names read as specifications
  (`test_rejects_names_longer_than_255_chars`).
- **TST-02** Cover equivalence classes with `@pytest.mark.parametrize`, not copy-pasted tests.
- **TST-03** Test boundaries on both sides: exactly at the limit, and one past it.
- **TST-04** Negative tests assert the exact exception type from the spec with `pytest.raises`;
  also assert the documented base class if the spec promises one.
- **TST-05** Never `time.sleep`. Inject a fake clock (a small class with `now` and `advance()`)
  when the API accepts a clock; freeze it for concurrency tests.
- **TST-06** Use the `tmp_path` fixture for any file-system interaction; never touch real paths.
- **TST-07** Tests are derived from the SPEC only (black box): import only the public API the spec
  names, from the module the spec names. Do not rely on private attributes.
- **TST-08** Map every acceptance criterion to at least one test and report the mapping in
  `covered_criteria`.
- **TST-09** Write tests that kill mutants: the harness plants bugs (off-by-one comparisons, a
  dropped min/max bound, a skipped `raise`, an inverted boolean) and re-runs your suite. Assert exact
  values after each state change, not just truthiness; after an unusual event (e.g. the clock going
  backwards) also check that later behaviour is still exactly right.
- **TST-10** Tests run on Windows and POSIX. Do not build expected values with the same path
  machinery the code uses (`os.path.normpath/join`, `resolve`) and do not assert behaviour for
  platform-dependent names (trailing dots or spaces, `...`, device names like `CON`) unless the
  spec defines it explicitly.
