"""Offline scripted LLM: lets `main.py` run end-to-end without an API key.

This is a stand-in for the model only. The harness around it (plan validation,
skill resolution and injection, prompt assembly, the pytest sandbox, the mutation
check, quality gate, revision routing and policy overrides) runs for real. Replies are
reactive the way a model's would be: they depend on WHICH skills were injected into the
call, on the sandbox result routed to the reviewer, on the gate problems shown to the
dispatcher, and on whether the call is a revision.
With `--no-skills` the scripted agents answer the way an un-skilled model typically
does, which makes the effect of skills visible offline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from textwrap import dedent

from .llm import LLMCall

Handler = Callable[[LLMCall], dict]


def _code(text: str) -> str:
    return dedent(text).lstrip("\n")


def _has(call: LLMCall, skill: str) -> bool:
    return skill in call.skills


def _sandbox_failed(call: LLMCall) -> bool:
    return '"status": "failed"' in call.user or '"status": "error"' in call.user


# =============================================================================
# Case "upload": resolve_upload_path
# =============================================================================

UPLOAD_SECURE = _code('''
    """Safe resolution of client-supplied upload file names."""

    from __future__ import annotations

    import os
    import re
    from pathlib import Path

    __all__ = ["MAX_FILENAME_LENGTH", "UnsafePathError", "resolve_upload_path"]

    MAX_FILENAME_LENGTH = 255
    _ALLOWED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


    class UnsafePathError(ValueError):
        """Raised when a client-supplied file name cannot be stored safely."""


    def resolve_upload_path(base_dir: str | os.PathLike[str], filename: str) -> Path:
        """Return the absolute path at which ``filename`` may be stored inside ``base_dir``.

        The name is checked against an allow-list grammar and the resolved path is
        verified to stay inside the upload directory. Callers must still create the
        file atomically, e.g. ``open(path, "xb")``.

        Raises:
            UnsafePathError: if the name is empty, too long, outside the allowed
                grammar, or would resolve outside ``base_dir``.
        """
        if not isinstance(filename, str) or not filename:
            raise UnsafePathError("file name must be a non-empty string")
        if len(filename) > MAX_FILENAME_LENGTH:
            raise UnsafePathError(f"file name longer than {MAX_FILENAME_LENGTH} characters")
        if not _ALLOWED_NAME.fullmatch(filename) or ".." in filename:
            raise UnsafePathError("file name contains forbidden characters")

        base = Path(base_dir).resolve()
        candidate = (base / filename).resolve()
        if candidate.parent != base:
            raise UnsafePathError("file name resolves outside the upload directory")
        return candidate
''')

UPLOAD_NAIVE = _code('''
    import os


    def resolve_upload_path(base_dir, filename):
        # Build the destination path for the uploaded file.
        return os.path.join(base_dir, filename)
''')

_UPLOAD_TESTS = '''
    import pytest

    from upload_paths import MAX_FILENAME_LENGTH, UnsafePathError, resolve_upload_path


    @pytest.mark.parametrize("name", ["report.pdf", "photo_01.JPG", "archive.tar.gz", "a", "v1-final.txt"])
    def test_accepts_plain_file_names(tmp_path, name):
        result = resolve_upload_path(tmp_path, name)

        assert result == (tmp_path / name).resolve()
        assert result.parent == tmp_path.resolve()


    def test_accepts_str_base_dir(tmp_path):
        assert resolve_upload_path(str(tmp_path), "ok.txt") == (tmp_path / "ok.txt").resolve()


    @pytest.mark.parametrize(
        "name",
        [
            "../secret.txt",
            "..\\\\secret.txt",
            "sub/../../etc/passwd",
            "/etc/passwd",
            "C:\\\\Windows\\\\win.ini",
            "..",
            ".",
            ".hidden",
            "a..b",
            "name\\x00.txt",
            "dir/file.txt",
            "",
            " ",
        ],
    )
    def test_rejects_unsafe_names(tmp_path, name):
        with pytest.raises(UnsafePathError):
            resolve_upload_path(tmp_path, name)


    {at_limit}
    def test_rejects_name_one_past_length_limit(tmp_path):
        with pytest.raises(UnsafePathError):
            resolve_upload_path(tmp_path, "a" * (MAX_FILENAME_LENGTH + 1))


    def test_unsafe_path_error_is_a_value_error(tmp_path):
        with pytest.raises(ValueError):
            resolve_upload_path(tmp_path, "../x")
'''
_AT_LIMIT = '''def test_accepts_name_exactly_at_length_limit(tmp_path):
        name = "a" * (MAX_FILENAME_LENGTH - 4) + ".txt"

        assert resolve_upload_path(tmp_path, name).name == name

'''
# The first attempt misses the "exactly at the limit" side of the boundary (a typical model slip,
# against TST-03); the mutation check catches it and the dispatcher routes a test revision.
UPLOAD_TESTS_SKILLED_V1 = _code(_UPLOAD_TESTS.replace("{at_limit}", ""))
UPLOAD_TESTS_SKILLED = _code(_UPLOAD_TESTS.replace("{at_limit}", _AT_LIMIT))

UPLOAD_TESTS_NAIVE = _code('''
    import os

    from upload_paths import resolve_upload_path


    def test_returns_path_inside_base_dir():
        assert resolve_upload_path("uploads", "report.pdf") == os.path.join("uploads", "report.pdf")


    def test_keeps_file_name():
        assert resolve_upload_path("uploads", "a.txt").endswith("a.txt")
''')


def upload_plan(call: LLMCall) -> dict:
    return {
        "analysis": (
            "Security-sensitive file-system task: the file name is fully controlled by a remote "
            "client and becomes part of a server path. Main risk is path traversal (CWE-22) via "
            "'..', absolute paths, drive letters or separators; secondary risks are NUL bytes, "
            "over-long names and TOCTOU when the caller later opens the path. Unstated: the error "
            "contract and the allowed file-name grammar - the spec must pin both. Pipeline: spec "
            "with abuse cases -> secure implementation + independent adversarial tests -> sandbox "
            "-> dedicated security audit -> review."
        ),
        "steps": [
            {"id": "s1", "agent": "requirements_analyst",
             "objective": "Specify resolve_upload_path: exact signature, allowed file-name grammar, "
                          "length limit, the dedicated exception and its base class, and abuse cases "
                          "(traversal, absolute paths, separators, NUL byte, over-long names).",
             "skills": ["requirements-engineering"],
             "skill_rationale": "Need Given/When/Then criteria and an explicit abuse-case section.",
             "inputs": ["request"]},
            {"id": "s2", "agent": "code_generator",
             "objective": "Implement the module from spec s1 with allow-list validation and a "
                          "post-resolution containment check; reject every abuse case with the "
                          "spec'd exception.",
             "skills": ["secure-coding", "python-clean-code"],
             "skill_rationale": "secure-coding for CWE-22 containment; clean-code for the error contract.",
             "inputs": ["s1"]},
            {"id": "s3", "agent": "test_generator",
             "objective": "Write a black-box pytest suite from spec s1 covering valid names, every "
                          "listed attack payload, and both sides of the length boundary.",
             "skills": ["pytest-patterns", "secure-coding"],
             "skill_rationale": "pytest-patterns for parametrized boundaries; secure-coding for payloads.",
             "inputs": ["s1"]},
            {"id": "s4", "agent": "sandbox", "objective": "Run s3 against s2.",
             "skills": [], "skill_rationale": "tool", "inputs": ["s2", "s3"]},
            {"id": "s5", "agent": "security_auditor",
             "objective": "Audit s2 for path traversal, platform-specific name tricks and unsafe "
                          "use by callers (TOCTOU, overwrite).",
             "skills": ["secure-coding"],
             "skill_rationale": "Provides the checklist and severity scale.",
             "inputs": ["s1", "s2"]},
            {"id": "s6", "agent": "code_reviewer",
             "objective": "Gate: verify spec coverage, sandbox result and absence of blocking "
                          "security findings; approve or request concrete changes.",
             "skills": ["python-clean-code"],
             "skill_rationale": "Team review checklist.",
             "inputs": ["s1", "s2", "s3", "s4", "s5"]},
        ],
    }


def upload_spec(call: LLMCall) -> dict:
    if not _has(call, "requirements-engineering"):
        return {"module_name": "upload_paths",
                "summary": "Function that returns the path where an uploaded file is saved.",
                "public_api": ["def resolve_upload_path(base_dir, filename)"],
                "acceptance_criteria": ["Returns the path of the file inside base_dir."],
                "edge_cases": [], "assumptions": [], "applied_skill_rules": []}
    return {
        "module_name": "upload_paths",
        "summary": "Map a client-supplied file name to a safe absolute path inside the upload "
                   "directory, or refuse it.",
        "public_api": [
            "MAX_FILENAME_LENGTH: int = 255",
            "class UnsafePathError(ValueError)",
            "def resolve_upload_path(base_dir: str | os.PathLike[str], filename: str) -> pathlib.Path",
        ],
        "acceptance_criteria": [
            "Given a name matching [A-Za-z0-9][A-Za-z0-9._-]*, when resolved, then the result is "
            "base_dir.resolve() / name.",
            "Given base_dir as str or Path, then both are accepted.",
            "Given a name containing '/', '\\\\', ':', NUL or whitespace, then UnsafePathError is raised.",
            "Given '..', '.', a leading dot or any '..' sequence, then UnsafePathError is raised.",
            "Given an absolute POSIX or Windows path, then UnsafePathError is raised.",
            "Given a name of exactly 255 chars it is accepted; 256 chars raises UnsafePathError.",
            "Given an empty name, then UnsafePathError is raised.",
            "UnsafePathError is a subclass of ValueError.",
        ],
        "edge_cases": ["unicode look-alike separators", "Windows reserved device names (CON, NUL)",
                       "base_dir given as relative path", "names differing only in case"],
        "assumptions": ["Sub-directories are not allowed; the file goes directly into base_dir.",
                        "The function does not create or open the file (caller responsibility).",
                        "Out of scope: content-type sniffing, antivirus, quotas."],
        "applied_skill_rules": ["REQ-01", "REQ-02", "REQ-03", "REQ-04", "REQ-05"],
    }


def upload_code(call: LLMCall) -> dict:
    if _has(call, "secure-coding"):
        return {"module_name": "upload_paths", "code": UPLOAD_SECURE,
                "design_notes": "Allow-list grammar (SEC-01) before touching the file system, then "
                                "resolve() and a containment check against the resolved base (SEC-02). "
                                "Dedicated ValueError subclass as the error contract (PY-03).",
                "applied_skill_rules": ["SEC-01", "SEC-02", "SEC-05", "SEC-06", "SEC-07",
                                        "PY-01", "PY-02", "PY-03", "PY-07"]}
    return {"module_name": "upload_paths", "code": UPLOAD_NAIVE,
            "design_notes": "Joins the directory and the file name.", "applied_skill_rules": []}


def upload_tests(call: LLMCall) -> dict:
    if _has(call, "pytest-patterns"):
        return {"module_name": "upload_paths",
                "test_code": UPLOAD_TESTS_SKILLED if call.revision else UPLOAD_TESTS_SKILLED_V1,
                "covered_criteria": ["AC1 valid names", "AC2 str/Path base", "AC3-AC5 attack payloads",
                                     "AC6 length boundary", "AC7 empty name", "AC8 ValueError base"],
                "applied_skill_rules": ["TST-01", "TST-02", "TST-03", "TST-04", "TST-06", "TST-07",
                                        "TST-08", *(["TST-09"] if call.revision else [])]}
    return {"module_name": "upload_paths", "test_code": UPLOAD_TESTS_NAIVE,
            "covered_criteria": ["returns path"], "applied_skill_rules": []}


def upload_audit(call: LLMCall) -> dict:
    if not _has(call, "secure-coding"):
        return {"verdict": "PASS", "summary": "No obvious problems found in a quick review.",
                "findings": [{"rule_id": None, "severity": "MEDIUM", "location": "resolve_upload_path",
                              "description": "Input is not validated.",
                              "recommendation": "Consider adding validation."}],
                "applied_skill_rules": []}
    if "os.path.join(base_dir, filename)" in call.user:
        return {"verdict": "FAIL", "summary": "Path traversal.",
                "findings": [{"rule_id": "SEC-02", "severity": "HIGH", "location": "resolve_upload_path",
                              "description": "User-controlled name joined without containment check; "
                                             "'../x' and absolute paths escape base_dir (CWE-22).",
                              "recommendation": "Validate with an allow-list and verify the resolved "
                                                "path stays inside base_dir."}],
                "applied_skill_rules": ["SEC-01", "SEC-02"]}
    return {
        "verdict": "PASS",
        "summary": "Traversal is blocked twice (allow-list grammar, then post-resolve containment). "
                   "No blocking findings; two hardening notes for callers.",
        "findings": [
            {"rule_id": "SEC-01", "severity": "LOW", "location": "_ALLOWED_NAME",
             "description": "Windows reserved device names (CON, NUL, COM1, LPT1, optionally with an "
                            "extension) match the allow-list; on a Windows host they refer to devices.",
             "recommendation": "Reject names whose stem (case-insensitive) is a reserved device name."},
            {"rule_id": "SEC-06", "severity": "INFO", "location": "resolve_upload_path (callers)",
             "description": "Validation does not protect the later write: an existing file could be "
                            "overwritten or a symlink planted between check and use.",
             "recommendation": "Create files with open(path, 'xb') and keep the upload dir non-writable "
                               "for other users."},
        ],
        "applied_skill_rules": ["SEC-01", "SEC-02", "SEC-03", "SEC-05", "SEC-06", "SEC-07"],
    }


def upload_review(call: LLMCall) -> dict:
    if _sandbox_failed(call):
        return {"verdict": "REQUEST_CHANGES", "issues": ["Sandbox tests fail; fix the implementation."],
                "summary": "Tests do not pass.", "applied_skill_rules": []}
    if not _has(call, "python-clean-code"):
        return {"verdict": "APPROVE", "issues": [], "summary": "Looks fine, tests pass.",
                "applied_skill_rules": []}
    return {"verdict": "APPROVE", "issues": [],
            "summary": "All 8 acceptance criteria are implemented and covered by the sandbox run; error "
                       "contract matches the spec; security findings are non-blocking.",
            "applied_skill_rules": ["PY-01", "PY-02", "PY-03", "PY-07", "SEC-02"]}


# =============================================================================
# Case "ratelimit": TokenBucket
# =============================================================================

_BUCKET = '''
    """Thread-safe token-bucket rate limiter."""

    from __future__ import annotations

    import threading
    import time
    from collections.abc import Callable

    __all__ = ["TokenBucket"]


    class TokenBucket:
        """Token bucket holding up to ``capacity`` tokens, refilled at ``refill_rate`` tokens/s.

        Raises:
            ValueError: if ``capacity`` or ``refill_rate`` is not positive.
        """

        def __init__(
            self,
            capacity: float,
            refill_rate: float,
            clock: Callable[[], float] = time.monotonic,
        ) -> None:
            if capacity <= 0:
                raise ValueError("capacity must be positive")
            if refill_rate <= 0:
                raise ValueError("refill_rate must be positive")
            self._capacity = float(capacity)
            self._rate = float(refill_rate)
            self._clock = clock
            self._tokens = float(capacity)
            self._updated = clock()
            self._lock = threading.Lock()

        @property
        def tokens(self) -> float:
            """Tokens available right now (after applying the pending refill)."""
            with self._lock:
                self._refill()
                return self._tokens

        def try_acquire(self, tokens: float = 1.0) -> bool:
            """Take ``tokens`` if available; never blocks.

            Raises:
                ValueError: if ``tokens`` is not positive or exceeds the capacity.
            """
            if tokens <= 0:
                raise ValueError("tokens must be positive")
            if tokens > self._capacity:
                raise ValueError("tokens exceeds bucket capacity")
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                return False

        def _refill(self) -> None:
            now = self._clock()
            elapsed = max(0.0, now - self._updated)
            {refill}
            self._updated = now
'''
BUCKET_V1 = _code(_BUCKET.replace("{refill}", "self._tokens = self._tokens + elapsed * self._rate"))
BUCKET_V2 = _code(_BUCKET.replace(
    "{refill}", "self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)"))

BUCKET_NAIVE = _code('''
    import time


    class TokenBucket:
        def __init__(self, capacity, refill_rate):
            self.capacity = capacity
            self.refill_rate = refill_rate
            self.tokens = capacity
            self.last = time.time()

        def try_acquire(self, n=1):
            now = time.time()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill_rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False
''')

_BUCKET_TESTS = '''
    import sys
    import threading

    import pytest

    from rate_limiter import TokenBucket


    class FakeClock:
        def __init__(self, start: float = 0.0) -> None:
            self.now = start

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds


    @pytest.fixture
    def clock():
        return FakeClock()


    def test_new_bucket_is_full(clock):
        assert TokenBucket(capacity=4, refill_rate=1, clock=clock).tokens == pytest.approx(4)


    def test_allows_burst_up_to_capacity_then_denies(clock):
        bucket = TokenBucket(capacity=3, refill_rate=1, clock=clock)

        assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]


    def test_refills_proportionally_to_elapsed_time(clock):
        bucket = TokenBucket(capacity=2, refill_rate=0.5, clock=clock)
        assert bucket.try_acquire(2)

        clock.advance(2)

        assert bucket.try_acquire()
        assert not bucket.try_acquire()


    def test_tokens_never_exceed_capacity_after_long_idle(clock):
        bucket = TokenBucket(capacity=5, refill_rate=10, clock=clock)

        clock.advance(3600)

        assert bucket.tokens == pytest.approx(5)


    def test_clock_going_backwards_adds_no_tokens_and_refill_resumes(clock):
        bucket = TokenBucket(capacity=1, refill_rate=1, clock=clock)
        assert bucket.try_acquire()

        clock.advance(-10)
        assert not bucket.try_acquire()

        clock.advance(1)
        assert bucket.tokens == pytest.approx(1)


    @pytest.mark.parametrize("capacity, rate", [(0, 1), (-1, 1), (1, 0), (1, -5)])
    def test_rejects_non_positive_configuration(capacity, rate):
        with pytest.raises(ValueError):
            TokenBucket(capacity=capacity, refill_rate=rate)


    @pytest.mark.parametrize("tokens", [0, -1, 10.5])
    def test_rejects_invalid_acquire_amounts(clock, tokens):
        bucket = TokenBucket(capacity=10, refill_rate=1, clock=clock)

        with pytest.raises(ValueError):
            bucket.try_acquire(tokens)


    {concurrency}'''
_CONCURRENCY_NAIVE = '''
    def test_concurrent_acquire_never_oversubscribes(clock):
        bucket = TokenBucket(capacity=100, refill_rate=1, clock=clock)
        start = threading.Barrier(8)
        results = []
        results_lock = threading.Lock()

        def worker():
            start.wait()
            for _ in range(50):
                ok = bucket.try_acquire()
                with results_lock:
                    results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(results) == 100
'''
# With concurrency-safety injected (CONC-06) the test forces thread switches, so it can fail.
_CONCURRENCY_FORCED = '''
    @pytest.fixture
    def frequent_thread_switches():
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        yield
        sys.setswitchinterval(previous)


    @pytest.mark.parametrize("attempt", range(10))
    def test_concurrent_acquire_never_oversubscribes(clock, frequent_thread_switches, attempt):
        bucket = TokenBucket(capacity=100, refill_rate=1, clock=clock)
        start = threading.Barrier(8)
        granted = []

        def worker():
            start.wait()
            granted.append(sum(bucket.try_acquire() for _ in range(200)))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(granted) == 100
'''

BUCKET_TESTS_NAIVE = _code('''
    from rate_limiter import TokenBucket


    def test_acquire_until_empty():
        bucket = TokenBucket(2, 1)
        assert bucket.try_acquire()
        assert bucket.try_acquire()
        assert not bucket.try_acquire()
''')


def ratelimit_plan(call: LLMCall) -> dict:
    return {
        "analysis": (
            "Stateful, time-dependent, concurrently used component for an API gateway. Risks: races "
            "between check and consume, token overflow after long idle, wall-clock jumps, invalid "
            "configuration (zero/negative/non-finite) silently disabling limiting. Unstated: time "
            "source and error contract - the spec must pin an injectable monotonic clock so tests "
            "never sleep. Pipeline: spec -> implementation + independent tests with fake clock -> "
            "sandbox -> audit (resource/DoS angle) -> review."
        ),
        "steps": [
            {"id": "s1", "agent": "requirements_analyst",
             "objective": "Specify TokenBucket: constructor with injectable clock, non-blocking "
                          "try_acquire(tokens=1.0), tokens property, error contract, and criteria for "
                          "burst, refill, capacity cap, clock going backwards and concurrent callers.",
             "skills": ["requirements-engineering"],
             "skill_rationale": "Time and concurrency edge-case taxonomy.", "inputs": ["request"]},
            {"id": "s2", "agent": "code_generator",
             "objective": "Implement rate_limiter.TokenBucket per spec s1: atomic refill+consume, "
                          "monotonic injectable clock, strict argument validation.",
             "skills": ["python-clean-code"],
             "skill_rationale": "DI of the clock and error contract.", "inputs": ["s1"]},
            {"id": "s3", "agent": "test_generator",
             "objective": "Write deterministic pytest tests from spec s1 with a fake clock, covering "
                          "every criterion including an exact-invariant multithreaded test.",
             "skills": ["pytest-patterns"],
             "skill_rationale": "Fake clock instead of sleep, parametrized negatives.", "inputs": ["s1"]},
            {"id": "s4", "agent": "sandbox", "objective": "Run s3 against s2.",
             "skills": [], "skill_rationale": "tool", "inputs": ["s2", "s3"]},
            {"id": "s5", "agent": "security_auditor",
             "objective": "Audit s2 for ways to bypass or disable limiting: config values, numeric "
                          "edge cases, unbounded state.",
             "skills": ["secure-coding"],
             "skill_rationale": "Resource-limit rules (SEC-05).", "inputs": ["s1", "s2"]},
            {"id": "s6", "agent": "code_reviewer",
             "objective": "Gate: spec coverage, thread safety, sandbox result, audit result.",
             "skills": ["python-clean-code", "pytest-patterns"],
             "skill_rationale": "Code and test quality checklists.",
             "inputs": ["s1", "s2", "s3", "s4", "s5"]},
        ],
    }


def ratelimit_spec(call: LLMCall) -> dict:
    if not _has(call, "requirements-engineering"):
        return {"module_name": "rate_limiter", "summary": "Token bucket rate limiter.",
                "public_api": ["class TokenBucket(capacity, refill_rate)", "try_acquire(n=1) -> bool"],
                "acceptance_criteria": ["try_acquire returns True while tokens are available.",
                                        "Tokens refill over time."],
                "edge_cases": [], "assumptions": [], "applied_skill_rules": []}
    return {
        "module_name": "rate_limiter",
        "summary": "Non-blocking, thread-safe token bucket with an injectable monotonic clock.",
        "public_api": [
            "class TokenBucket",
            "TokenBucket.__init__(self, capacity: float, refill_rate: float, "
            "clock: Callable[[], float] = time.monotonic) -> None",
            "TokenBucket.try_acquire(self, tokens: float = 1.0) -> bool",
            "TokenBucket.tokens -> float  # read-only, after pending refill",
        ],
        "acceptance_criteria": [
            "Given a new bucket, then tokens == capacity.",
            "Given capacity C and a frozen clock, when try_acquire() is called C+1 times, then C calls "
            "return True and the last returns False.",
            "Given an empty bucket with rate r, when t seconds pass, then min(C, r*t) tokens are available.",
            "Given any idle period, then tokens never exceed capacity.",
            "Given the clock goes backwards, then no tokens are added.",
            "Given capacity <= 0 or refill_rate <= 0, then ValueError.",
            "Given tokens <= 0 or tokens > capacity in try_acquire, then ValueError.",
            "Given 8 threads x 50 calls with a frozen clock and C=100, then exactly 100 succeed.",
        ],
        "edge_cases": ["fractional tokens", "clock jumps backwards", "very long idle",
                       "simultaneous callers", "request larger than capacity", "NaN/inf configuration"],
        "assumptions": ["Single process; distributed limiting is out of scope.",
                        "No blocking acquire-with-timeout in this iteration."],
        "applied_skill_rules": ["REQ-01", "REQ-02", "REQ-03", "REQ-04"],
    }


def ratelimit_code(call: LLMCall) -> dict:
    if not call.skills:
        return {"module_name": "rate_limiter", "code": BUCKET_NAIVE,
                "design_notes": "Simple token bucket.", "applied_skill_rules": []}
    rules = ["PY-01", "PY-02", "PY-03", "PY-04", "PY-06", "PY-07"]
    if _has(call, "concurrency-safety"):
        rules += ["CONC-01", "CONC-02", "CONC-03"]
    if call.revision == 0:
        return {"module_name": "rate_limiter", "code": BUCKET_V1,
                "design_notes": "Lazy refill on access; refill and consume share one lock so "
                                "check-then-act is atomic; clock injected, negative elapsed clamped.",
                "applied_skill_rules": rules}
    return {"module_name": "rate_limiter", "code": BUCKET_V2,
            "design_notes": "Revision: refill now clamps to capacity (min(capacity, ...)); nothing "
                            "else changed.",
            "applied_skill_rules": rules}


def ratelimit_tests(call: LLMCall) -> dict:
    if not _has(call, "pytest-patterns"):
        return {"module_name": "rate_limiter", "test_code": BUCKET_TESTS_NAIVE,
                "covered_criteria": ["acquire"], "applied_skill_rules": []}
    rules = ["TST-01", "TST-02", "TST-03", "TST-04", "TST-05", "TST-07", "TST-08", "TST-09"]
    concurrency = _CONCURRENCY_NAIVE
    if _has(call, "concurrency-safety"):
        rules += ["CONC-05", "CONC-06"]
        concurrency = _CONCURRENCY_FORCED
    return {"module_name": "rate_limiter", "test_code": _code(_BUCKET_TESTS.replace("{concurrency}", concurrency)),
            "covered_criteria": [f"AC{i}" for i in range(1, 9)], "applied_skill_rules": rules}


def ratelimit_audit(call: LLMCall) -> dict:
    if not _has(call, "secure-coding"):
        return {"verdict": "PASS", "summary": "No issues found.", "findings": [], "applied_skill_rules": []}
    return {
        "verdict": "PASS",
        "summary": "Limiter cannot be bypassed through the public API; one hardening gap in configuration.",
        "findings": [
            {"rule_id": "SEC-05", "severity": "LOW", "location": "TokenBucket.__init__",
             "description": "capacity/refill_rate accept float('inf') and NaN: inf disables limiting, NaN "
                            "makes every acquire fail. Values come from operator config, not clients.",
             "recommendation": "Reject non-finite values with math.isfinite() in the constructor."},
        ],
        "applied_skill_rules": ["SEC-05", "SEC-07"],
    }


def ratelimit_review(call: LLMCall) -> dict:
    if _sandbox_failed(call):
        return {
            "verdict": "REQUEST_CHANGES",
            "issues": ["test_tokens_never_exceed_capacity_after_long_idle fails: _refill() adds "
                       "elapsed * rate without clamping, so after 1h idle the bucket holds 36005 tokens "
                       "and allows a burst far above capacity (violates AC4). Clamp with "
                       "min(self._capacity, ...)."],
            "summary": "Correct locking and API, but the capacity invariant is broken.",
            "applied_skill_rules": ["PY-06", "CONC-01", "TST-08"],
        }
    if not call.skills:
        return {"verdict": "APPROVE", "issues": [], "summary": "Tests pass.", "applied_skill_rules": []}
    return {"verdict": "APPROVE", "issues": [],
            "summary": "All 8 criteria covered and green; refill+consume atomic under one lock; clock "
                       "injected. Non-finite config is a non-blocking audit note.",
            "applied_skill_rules": ["PY-01", "PY-04", "CONC-01", "CONC-02", "TST-05"]}


# =============================================================================
# Shared: dispatcher finalizer (derives the report from the digest it receives)
# =============================================================================

_FINDING = re.compile(r"^\s*finding \[(\w+)\] [^:]*: (.*?) Recommendation: (.*)$", re.MULTILINE)


def revise(call: LLMCall) -> dict:
    """Routes the revision from the gate problems it is shown, like the real dispatcher would."""
    problems = call.user.split("# Quality gate problems\n", 1)[1].split("\n\n#", 1)[0]
    weak = [p for p in problems.splitlines() if "tests too weak" in p]
    code = [p for p in problems.splitlines() if p not in weak]
    target = "both" if weak and code else "test_generator" if weak else "code_generator"
    return {
        "target": target,
        "rationale": ("The mutation check shows the suite does not detect planted bugs; the code is not at "
                      "fault for that. " if weak else "")
                     + ("The failing test matches an acceptance criterion, so the implementation is wrong."
                        if code else ""),
        "feedback_for_code": "\n".join(code),
        "feedback_for_tests": ("Add tests that pin exact behaviour on BOTH sides of every documented limit "
                               "(exactly at the limit and one past it); " + "; ".join(weak)) if weak else "",
    }


def finalize(call: LLMCall) -> dict:
    passed = "passed=True" in call.user
    revisions = int(re.search(r"revisions=(\d+)", call.user).group(1))
    code = re.search(r"code_generator: (\S+), (\d+) lines", call.user)
    sandbox = re.search(r"sandbox: (\w+): (\d+) passed, (\d+) failed", call.user)
    risks = [f"[{sev}] {desc} -> {fix}" for sev, desc, fix in _FINDING.findall(call.user)]
    built = f"{code.group(1)} ({code.group(2)} lines)" if code else "the module"
    tests = f"{sandbox.group(2)} passed / {sandbox.group(3)} failed in sandbox" if sandbox else "no sandbox run"
    if not passed:
        return {"status": "FAILED", "summary": f"Built {built}, but the quality gate is still failing "
                f"after {revisions} revision(s): {tests}.", "residual_risks": risks}
    rev = f" after {revisions} revision(s)" if revisions else " on the first attempt"
    return {"status": "DELIVERED_WITH_RISKS" if risks else "DELIVERED",
            "summary": f"Built {built}; verified by an independent test suite ({tests}), security audit "
                       f"and code review{rev}.",
            "residual_risks": risks}


SCRIPTS: dict[str, dict[str, Handler]] = {
    "upload": {
        "dispatcher": upload_plan, "requirements_analyst": upload_spec, "code_generator": upload_code,
        "test_generator": upload_tests, "security_auditor": upload_audit, "code_reviewer": upload_review,
        "dispatcher.finalize": finalize, "dispatcher.revise": revise,
    },
    "ratelimit": {
        "dispatcher": ratelimit_plan, "requirements_analyst": ratelimit_spec,
        "code_generator": ratelimit_code, "test_generator": ratelimit_tests,
        "security_auditor": ratelimit_audit, "code_reviewer": ratelimit_review,
        "dispatcher.finalize": finalize, "dispatcher.revise": revise,
    },
}


class OfflineScriptedClient:
    def __init__(self, script: str):
        if script not in SCRIPTS:
            raise ValueError(f"no offline script '{script}'; available: {', '.join(SCRIPTS)}")
        self.script = script
        self.name = f"offline-scripted[{script}] (no API key; set HARNESS_API_KEY for a real LLM)"

    def complete(self, call: LLMCall) -> str:
        return json.dumps(SCRIPTS[self.script][call.agent](call), ensure_ascii=False)
