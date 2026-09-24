---
name: secure-coding
version: "1.2"
description: OWASP/CWE-based secure coding rules for code that touches untrusted input, the file system, processes or resource limits; includes an audit severity scale and attack payloads for tests.
applies_to: [code_generator, test_generator, security_auditor, code_reviewer]
triggers: [upload, filename, file name, path, untrusted, user input, subprocess, pickle, deserializ, sql, загруз, пользовател]
---
# Secure coding (Python)

## Rules
- **SEC-01** Validate untrusted input with an **allow-list** (explicit grammar, e.g. a regex with
  `fullmatch`), never with a deny-list of "bad" characters. Check type, length and charset first.
- **SEC-02** Path containment (CWE-22): build the path, call `.resolve()`, then verify
  `candidate.is_relative_to(base.resolve())`. Never trust `os.path.join` with user data:
  an absolute second argument discards the base, and `..` walks out of it.
- **SEC-03** Never execute or deserialize untrusted data: no `eval`, `exec`, `pickle`,
  `yaml.load` (use `yaml.safe_load`), `marshal`.
- **SEC-04** No shell: call `subprocess.run([...], shell=False)` with an argument list.
  Build SQL only with parameters.
- **SEC-05** Bound every resource an attacker or a misconfiguration controls: input lengths,
  counts, sizes, rates. Reject non-finite numbers (`math.isfinite`) where a limit is configured.
- **SEC-06** Mind TOCTOU: validating a path does not make later use safe. Document that callers
  must create files atomically (`open(path, "xb")`) and must not follow attacker-made symlinks.
- **SEC-07** Fail closed: on any doubt, raise a dedicated exception. Error messages must not echo
  secrets or absolute server paths back to the client.
- **SEC-08** No hard-coded secrets, tokens or credentials; read them from the environment.

## Severity scale (for audits)
- CRITICAL: remote code execution or arbitrary file write/read reachable from untrusted input.
- HIGH: exploitable bypass of a security control (e.g. path traversal, injection, auth bypass).
- MEDIUM: exploitable only under unusual configuration, or a missing defence-in-depth layer.
- LOW: hardening gap with limited impact or platform-specific edge cases.
- INFO: guidance for callers, documentation or monitoring.
CRITICAL and HIGH block release.

## Attack payloads for tests (path handling)
`../x`, `..\\x`, `a/../../x`, `/etc/passwd`, `C:\\Windows\\win.ini`, `..`, `.`, `.hidden`,
`name\x00.txt`, `dir/file.txt`, empty string, whitespace-only, name longer than 255 chars.
