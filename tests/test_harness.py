from pathlib import Path

import pytest
from rich.console import Console

from harness.agents import AGENTS
from harness.config import Settings
from harness.dispatcher import Dispatcher, PlanValidationError, validate_plan
from harness.llm import LLMCall, OutputParseError, parse_model
from harness.offline import OfflineScriptedClient, ratelimit_plan, upload_plan
from harness.sandbox import PytestSandbox
from harness.schemas import CodeArtifact, ExecutionPlan, Finding, SecurityReport
from harness.skills import SkillRegistry
from harness.tracing import Tracer

ROOT = Path(__file__).resolve().parents[1]
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
    finding = Finding(severity="HIGH", location="f", description="d", recommendation="r")

    assert SecurityReport(verdict="PASS", findings=[finding]).verdict == "FAIL"


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


@pytest.mark.parametrize(
    "script, request_text, revisions",
    [("upload", UPLOAD_REQUEST, 0), ("ratelimit", "потокобезопасный token bucket", 1)],
)
def test_offline_end_to_end(tmp_path, registry, script, request_text, revisions):
    tracer = Tracer(tmp_path, Console(quiet=True))
    dispatcher = Dispatcher(llm=OfflineScriptedClient(script), registry=registry,
                            sandbox=PytestSandbox(), tracer=tracer)

    result = dispatcher.run(request_text)
    tracer.close()

    assert result.gate.passed
    assert result.revisions == revisions
    assert result.final.status == "DELIVERED_WITH_RISKS"
    prompt = next((tmp_path / "prompts").glob("s2_code_generator*.md")).read_text(encoding="utf-8")
    assert '<skill name="python-clean-code"' in prompt  # runtime injection reached the sub-agent
