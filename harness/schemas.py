"""Typed contracts between the dispatcher, sub-agents and tools.

Every sub-agent answers with JSON that must validate against one of these models;
the JSON Schema of the model is embedded into the sub-agent's system prompt.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MODULE_NAME = r"^[a-z_][a-z0-9_]{0,63}$"


class Artifact(BaseModel):
    """Anything stored on the run blackboard and routed between steps."""

    def headline(self) -> str:
        return type(self).__name__

    def render_for_prompt(self) -> str:
        return f"```json\n{self.model_dump_json(indent=2)}\n```"


class AgentOutput(Artifact):
    applied_skill_rules: list[str] = Field(
        default_factory=list,
        description="IDs of skill rules you actually applied in this answer, e.g. 'SEC-02'.",
    )


# --- Dispatcher -----------------------------------------------------------------


class PlanStep(BaseModel):
    id: str = Field(description="Unique step id: 's1', 's2', ...")
    agent: str = Field(description="Sub-agent name from the catalog, or 'sandbox'.")
    objective: str = Field(description="Imperative, verifiable instruction for this step.")
    skills: list[str] = Field(default_factory=list, description="Skill names to inject.")
    skill_rationale: str = Field(default="", description="Why these skills (or none).")
    inputs: list[str] = Field(
        default_factory=list, description="'request' and/or ids of EARLIER steps to route in."
    )


class ExecutionPlan(Artifact):
    analysis: str = Field(description="Request classification, risks, what the user left unsaid.")
    steps: list[PlanStep]


class RevisionDecision(Artifact):
    target: Literal["code_generator", "test_generator", "both"] = Field(
        description="Who must revise: the implementation, the test suite, or both."
    )
    rationale: str = Field(description="Why this target: which gate problem is whose fault.")
    feedback_for_code: str = Field(default="", description="Actionable instructions for code_generator.")
    feedback_for_tests: str = Field(
        default="", description="Actionable instructions for test_generator; never include implementation code."
    )

    @property
    def agents(self) -> tuple[str, ...]:
        return ("code_generator", "test_generator") if self.target == "both" else (self.target,)


class FinalReport(Artifact):
    status: Literal["DELIVERED", "DELIVERED_WITH_RISKS", "FAILED"]
    summary: str
    residual_risks: list[str] = Field(default_factory=list)


# --- Sub-agents -----------------------------------------------------------------


class RequirementsSpec(AgentOutput):
    module_name: str = Field(pattern=MODULE_NAME, description="Python module to create.")
    summary: str
    public_api: list[str] = Field(description="Exact signatures with type hints.")
    acceptance_criteria: list[str] = Field(description="Testable criteria (Given/When/Then).")
    edge_cases: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    def headline(self) -> str:
        return (
            f"spec for `{self.module_name}`: {len(self.public_api)} API items, "
            f"{len(self.acceptance_criteria)} criteria, {len(self.edge_cases)} edge cases"
        )


class CodeArtifact(AgentOutput):
    module_name: str = Field(pattern=MODULE_NAME)
    code: str = Field(description="Full source of the module.")
    design_notes: str = ""

    def headline(self) -> str:
        return f"{self.module_name}.py, {len(self.code.splitlines())} lines"

    def render_for_prompt(self) -> str:
        return (
            f"module `{self.module_name}.py` (rules applied: "
            f"{', '.join(self.applied_skill_rules) or 'none'})\n"
            f"```python\n{self.code.rstrip()}\n```\nDesign notes: {self.design_notes or '-'}"
        )


class TestSuite(AgentOutput):
    __test__ = False  # not a pytest class

    module_name: str = Field(pattern=MODULE_NAME, description="Module under test.")
    test_code: str = Field(description="Full source of the pytest file.")
    covered_criteria: list[str] = Field(default_factory=list)

    @property
    def test_count(self) -> int:
        return self.test_code.count("def test_")

    def headline(self) -> str:
        return f"test_{self.module_name}.py, {self.test_count} test functions"

    def render_for_prompt(self) -> str:
        return f"```python\n{self.test_code.rstrip()}\n```"


Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
BLOCKING: frozenset[str] = frozenset({"CRITICAL", "HIGH"})


class Finding(BaseModel):
    rule_id: str | None = Field(default=None, description="Skill rule ID, if one applies.")
    severity: Severity
    location: str
    description: str
    recommendation: str


class SecurityReport(AgentOutput):
    verdict: Literal["PASS", "FAIL"]
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""

    @model_validator(mode="after")
    def _blocking_findings_fail(self) -> SecurityReport:
        # Harness policy, not model opinion: a blocking finding always fails the audit.
        if any(f.severity in BLOCKING for f in self.findings):
            self.verdict = "FAIL"
        return self

    def headline(self) -> str:
        counts = Counter(f.severity for f in self.findings)
        found = ", ".join(f"{n} {sev}" for sev, n in counts.items()) or "none"
        return f"verdict {self.verdict}, findings: {found}"


class ReviewReport(AgentOutput):
    verdict: Literal["APPROVE", "REQUEST_CHANGES"]
    issues: list[str] = Field(default_factory=list, description="Actionable, for the implementer.")
    summary: str = ""

    def headline(self) -> str:
        return f"verdict {self.verdict}, {len(self.issues)} issue(s)"


# --- Tools ----------------------------------------------------------------------


class Survivor(BaseModel):
    operator: str
    line: int
    description: str


class MutationReport(BaseModel):
    """How many planted bugs the test suite detected (see harness/mutation.py)."""

    total: int
    killed: int
    survivors: list[Survivor] = Field(default_factory=list)

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 1.0

    def headline(self) -> str:
        return f"{self.killed}/{self.total} mutants killed ({self.score:.2f})"


class SandboxReport(Artifact):
    status: Literal["passed", "failed", "error", "timeout"]
    passed: int = 0
    failed: int = 0
    errors: int = 0
    duration_s: float = 0.0
    output_tail: str = ""
    mutation: MutationReport | None = None

    def headline(self) -> str:
        text = (
            f"{self.status}: {self.passed} passed, {self.failed} failed, "
            f"{self.errors} errors in {self.duration_s:.2f}s"
        )
        return f"{text}; {self.mutation.headline()}" if self.mutation else text

    def render_for_prompt(self) -> str:
        return f"```json\n{json.dumps(self.model_dump(), indent=2)}\n```"
