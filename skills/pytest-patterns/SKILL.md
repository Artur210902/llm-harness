---
name: pytest-patterns
version: "1.1"
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
