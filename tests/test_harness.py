import json
import sys
from pathlib import Path

import pytest
from rich.console import Console

from harness.agents import AGENTS
from harness.config import Settings
from harness.dispatcher import (
    Dispatcher,
    GateResult,
    PlanValidationError,
    execution_waves,
    steps_to_rerun,
    validate_plan,
)
from harness.llm import LLMCall, OutputParseError, TruncatedReplyError, complete_structured, parse_model
from harness.mutation import MutationTester, generate_mutants, weakness
from harness.offline import (
    BUCKET_NAIVE,
    BUCKET_V2,
    UPLOAD_NAIVE,
    UPLOAD_SECURE,
    OfflineScriptedClient,
    ratelimit_plan,
    ratelimit_tests,
    upload_plan,
)
from harness.sandbox import PytestSandbox
from harness.schemas import (
    CodeArtifact,
    ExecutionPlan,
    Finding,
    MutationReport,
    ReviewReport,
    SandboxReport,
    SecurityReport,
    Survivor,
    TestSuite,
)
from harness.skills import SkillRegistry
from harness.tracing import Tracer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from probes import probe  # noqa: E402

UPLOAD_REQUEST = "Write resolve_upload_path(base_dir, filename) for files uploaded by clients."


@pytest.fixture(scope="module")
def registry():
    return SkillRegistry(ROOT / "skills")


def _plan(builder) -> ExecutionPlan:
    return ExecutionPlan.model_validate(builder(LLMCall("dispatcher", "", "")))


def test_registry_loads_skills_with_rule_ids(registry):
    assert len(registry) >= 2
    assert {"SEC-02", "SEC-05"} <= registry.get("secure-coding").rule_ids
    assert "secure-coding" in registry.catalog()
    assert "SEC-02" not in registry.catalog()  # dispatcher sees metadata only


def test_resolve_combines_planner_choice_and_auto_trigger(registry):
    attachments, warnings = registry.resolve("code_generator", ["python-clean-code"], "потокобезопасный лимитер")

    assert [(a.skill.name, a.source) for a in attachments] == [
        ("python-clean-code", "planner"),
        ("concurrency-safety", "auto-trigger"),
    ]
    assert warnings == []


def test_resolve_refuses_skill_outside_applies_to(registry):
    attachments, warnings = registry.resolve("requirements_analyst", ["pytest-patterns"], "")

    assert attachments == []
    assert "does not apply" in warnings[0]


@pytest.mark.parametrize("builder", [upload_plan, ratelimit_plan])
def test_bundled_plans_are_valid(registry, builder):
    validate_plan(_plan(builder), AGENTS, registry)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p.steps[1].__setattr__("agent", "wizard"), "unknown agent"),
        (lambda p: p.steps[0].inputs.append("s5"), "not an earlier step"),
        (lambda p: p.steps[2].inputs.append("s2"), "must not see the implementation"),
        (lambda p: p.steps[0].skills.append("secure-coding"), "does not apply"),
        (lambda p: p.steps.pop(), "last step must be code_reviewer"),
    ],
)
def test_validation_rejects_broken_plans(registry, mutate, message):
    plan = _plan(upload_plan)
    mutate(plan)

    with pytest.raises(PlanValidationError, match=message):
        validate_plan(plan, AGENTS, registry)


def test_blocking_finding_forces_security_fail():
    finding = Finding(severity="HIGH", location="f", description="d", recommendation="r",
                      exploit="resolve_upload_path(base, '../x') returns a path outside base")

    assert SecurityReport(verdict="PASS", findings=[finding]).verdict == "FAIL"


def test_blocking_finding_without_exploit_is_downgraded():
    finding = Finding(severity="HIGH", location="f", description="symlinks in base_dir", recommendation="r")

    report = SecurityReport(verdict="FAIL", findings=[finding])

    assert report.findings[0].severity == "MEDIUM"
    assert report.verdict == "PASS"  # the verdict follows demonstrable blocking findings only


def test_only_blocking_review_issues_hold_delivery():
    suggestions_only = ReviewReport(verdict="REQUEST_CHANGES", suggestions=["use os.path.isabs"])
    blocking = ReviewReport(verdict="APPROVE", issues=["AC3 not implemented"])

    assert suggestions_only.verdict == "APPROVE"
    assert blocking.verdict == "REQUEST_CHANGES"


@pytest.mark.parametrize("raw, expected", [(None, True), ("true", True), ("false", False), ("0", False),
                                           ("No", False)])
def test_ssl_verify_setting(tmp_path, monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("HARNESS_SSL_VERIFY", raising=False)
    else:
        monkeypatch.setenv("HARNESS_SSL_VERIFY", raw)

    assert Settings.from_env(tmp_path).ssl_verify is expected


def test_parse_model_accepts_fenced_json_and_rejects_garbage():
    fenced = '```json\n{"module_name": "m", "code": "x = 1"}\n```'

    assert parse_model(fenced, CodeArtifact).module_name == "m"
    with pytest.raises(OutputParseError):
        parse_model("no json here", CodeArtifact)


def test_truncated_reply_is_retried_with_a_conciseness_hint():
    class Truncating:
        name = "truncating"

        def __init__(self):
            self.calls: list[LLMCall] = []

        def complete(self, call: LLMCall) -> str:
            self.calls.append(call)
            if len(self.calls) == 1:
                raise TruncatedReplyError("cut off", raw='{"module_name": "m", "co')
            return '{"module_name": "m", "code": "x = 1"}'

    llm = Truncating()

    assert complete_structured(llm, LLMCall("code_generator", "", "task"), CodeArtifact).code == "x = 1"
    assert "concise" in llm.calls[1].user


def test_invalid_revision_keeps_previous_attempt_and_fails_the_run(tmp_path, registry):
    class BrokenTestRevision(OfflineScriptedClient):
        def complete(self, call: LLMCall) -> str:
            if call.agent == "test_generator" and call.revision:
                return "sorry, here are the tests: def test_x(): ..."
            return super().complete(call)

    sandbox = PytestSandbox()
    tracer = Tracer(tmp_path, Console(quiet=True))
    dispatcher = Dispatcher(llm=BrokenTestRevision("upload"), registry=registry, sandbox=sandbox,
                            tracer=tracer, mutation=MutationTester(sandbox))

    result = dispatcher.run(UPLOAD_REQUEST)
    tracer.close()

    assert result.final.status == "FAILED"  # weak tests were never fixed, and the run still finished
    assert result.revisions == dispatcher.max_revisions
    assert (tmp_path / "prompts" / "s3_test_generator.rev1.invalid_reply.txt").is_file()


def test_failure_evidence_keeps_facts_and_drops_source_lines():
    tail = """________ test_refill ________

    def test_refill():
        bucket = TokenBucket(capacity=10, refill_rate=1, clock=clock)
>       assert bucket.available == 0
E       assert 5.0 == 0

token_bucket.py:42: in available
    return self._secret_formula()
FAILED test_token_bucket.py::test_refill - assert 5.0 == 0"""

    evidence = SandboxReport(status="failed", failed=1, output_tail=tail).failure_evidence()

    assert "E       assert 5.0 == 0" in evidence and "FAILED test_token_bucket.py::test_refill" in evidence
    assert "_secret_formula" not in evidence  # implementation source never reaches the test oracle


def test_rerun_covers_target_and_its_dependents_only():
    plan = _plan(upload_plan)

    assert [s.id for s in steps_to_rerun(plan, ("test_generator",))] == ["s3", "s4", "s6"]
    assert [s.id for s in steps_to_rerun(plan, ("code_generator",))] == ["s2", "s4", "s5", "s6"]


def test_independent_steps_share_a_wave():
    waves = execution_waves(_plan(upload_plan).steps)

    assert [[s.id for s in wave] for wave in waves] == [["s1"], ["s2", "s3"], ["s4", "s5"], ["s6"]]


class _FixedLLM:
    name = "fixed"

    def __init__(self, reply: dict):
        self.reply = reply

    def complete(self, call: LLMCall) -> str:
        return json.dumps(self.reply)


@pytest.mark.parametrize("weak_tests, expected", [(False, "code_generator"), (True, "both")])
def test_policy_adds_test_generator_when_tests_are_weak(tmp_path, registry, weak_tests, expected):
    llm = _FixedLLM({"target": "code_generator", "rationale": "r", "feedback_for_code": "fix"})
    dispatcher = Dispatcher(llm=llm, registry=registry, sandbox=PytestSandbox(),
                            tracer=Tracer(tmp_path, Console(quiet=True)))
    gate = GateResult(passed=False, problems=["p"], weak_tests=weak_tests)

    decision = dispatcher._decide_revision("req", _plan(upload_plan), {}, gate)

    assert decision.target == expected


# --- mutation check -----------------------------------------------------------------

_SNIPPET = '''
import threading
_lock = threading.Lock()

def take(n, limit):
    if n <= 0:
        raise ValueError("n")
    with _lock:
        return min(limit, n) >= 1
'''


def test_mutants_cover_every_operator_and_drop_all_locks():
    mutants = generate_mutants(_SNIPPET, limit=20)

    assert {m.operator for m in mutants} == {"DropLock", "DropClamp", "CompareSwap", "DropRaise"}
    drop_lock = next(m for m in mutants if m.operator == "DropLock")
    assert "with _lock" not in drop_lock.code
    assert "n > 0" not in drop_lock.code and "n <= 0" in drop_lock.code  # one bug per mutant
    assert next(m for m in mutants if m.operator == "DropClamp").code.count("min(") == 0


def test_weakness_policy():
    lock = Survivor(operator="DropLock", line=1, description="d")

    assert weakness(MutationReport(total=10, killed=9, survivors=[lock]), 0.6).startswith("tests too weak")
    assert weakness(MutationReport(total=10, killed=5), 0.6) is not None
    assert weakness(MutationReport(total=10, killed=6), 0.6) is None
    assert "self._" not in (weakness(MutationReport(total=4, killed=1, survivors=[lock]), 0.6) or "")


_ATOMIC_BY_ACCIDENT = '''
import threading

class Counter:
    def __init__(self, limit):
        self._left = limit
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            if self._left >= 1:
                self._left -= 1
                return True
            return False
'''

_HAMMER = '''
import threading
from counter import Counter

def test_never_over_grants():
    for _ in range(10):
        counter, start, granted = Counter(100), threading.Barrier(8), []
        def worker():
            start.wait()
            granted.append(sum(counter.take() for _ in range(200)))
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert sum(granted) == 100
'''


def test_drop_lock_mutant_exposes_a_race_the_gil_would_hide():
    """Without the forced thread switch, this check-then-act is accidentally atomic under the GIL."""
    tests = TestSuite(module_name="counter", test_code=_HAMMER)
    no_lock = next(m for m in generate_mutants(_ATOMIC_BY_ACCIDENT, 20) if m.operator == "DropLock")
    sandbox = PytestSandbox()

    assert sandbox.run(CodeArtifact(module_name="counter", code=_ATOMIC_BY_ACCIDENT), tests).status == "passed"
    assert "sleep(0)" in no_lock.code and "_lock:" not in no_lock.code
    assert sandbox.run(CodeArtifact(module_name="counter", code=no_lock.code), tests).status == "failed"


def test_skilled_concurrency_suite_kills_the_drop_lock_mutant():
    skills = ("pytest-patterns", "concurrency-safety")
    tests = TestSuite.model_validate(ratelimit_tests(LLMCall("test_generator", "", "", skills=skills)))
    no_lock = next(m for m in generate_mutants(BUCKET_V2, 20) if m.operator == "DropLock")
    sandbox = PytestSandbox()

    assert sandbox.run(CodeArtifact(module_name="rate_limiter", code=BUCKET_V2), tests).status == "passed"
    assert sandbox.run(CodeArtifact(module_name="rate_limiter", code=no_lock.code), tests).status == "failed"


@pytest.mark.parametrize(
    "case_id, module, good, bad",
    [("upload", "upload_paths", UPLOAD_SECURE, UPLOAD_NAIVE), ("ratelimit", "rate_limiter", BUCKET_V2, BUCKET_NAIVE)],
)
def test_independent_probes_separate_good_from_bad_code(case_id, module, good, bad):
    assert probe(case_id, module, good)["failed"] == 0
    assert probe(case_id, module, bad)["failed"] > 0


@pytest.mark.parametrize(
    "script, request_text, revised",
    [("upload", UPLOAD_REQUEST, "test_generator"), ("ratelimit", "потокобезопасный token bucket", "code_generator")],
)
def test_offline_end_to_end(tmp_path, registry, script, request_text, revised):
    sandbox = PytestSandbox()
    tracer = Tracer(tmp_path, Console(quiet=True))
    dispatcher = Dispatcher(llm=OfflineScriptedClient(script), registry=registry, sandbox=sandbox,
                            tracer=tracer, mutation=MutationTester(sandbox))

    result = dispatcher.run(request_text)
    tracer.close()

    assert result.gate.passed
    assert result.revisions == 1
    assert result.final.status == "DELIVERED_WITH_RISKS"
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(e for e in events if e["event"] == "revision")["decision"]["target"] == revised
    assert result.latest(SandboxReport).mutation.total > 0
    prompt = next((tmp_path / "prompts").glob("s2_code_generator*.md")).read_text(encoding="utf-8")
    assert '<skill name="python-clean-code"' in prompt  # runtime injection reached the sub-agent
