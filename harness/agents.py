"""Sub-agent definitions and the generic sub-agent runner."""

from __future__ import annotations

from dataclasses import dataclass

from .llm import LLMCall, LLMClient, complete_structured, schema_json
from .prompts import NO_SKILLS, SUBAGENT_PROMPT
from .schemas import (
    AgentOutput,
    CodeArtifact,
    RequirementsSpec,
    ReviewReport,
    SecurityReport,
    TestSuite,
)
from .skills import SkillAttachment

SANDBOX = "sandbox"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    title: str
    description: str  # what the dispatcher sees in its catalog
    mission: str  # what the sub-agent itself is told
    output_model: type[AgentOutput]


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            name="requirements_analyst",
            title="Requirements Analyst",
            description="Turns a raw request into a testable spec (module, API, error contract, "
            "acceptance criteria, edge cases). Output: RequirementsSpec.",
            mission="Turn the raw request into an unambiguous, testable specification. The spec is "
            "the single source of truth for BOTH the implementer and the independent test author, "
            "so pin the module name, exact signatures, the error contract, acceptance criteria, "
            "edge cases and assumptions.",
            output_model=RequirementsSpec,
        ),
        AgentSpec(
            name="code_generator",
            title="Code Generator",
            description="Implements the spec as one self-contained Python module. Output: CodeArtifact.",
            mission="Implement the specification as a single self-contained Python module named "
            "exactly as in the spec. Production quality, no placeholders. If `revision_feedback` "
            "is among the inputs, fix every listed problem in `previous_attempt` and change "
            "nothing else.",
            output_model=CodeArtifact,
        ),
        AgentSpec(
            name="test_generator",
            title="Test Generator",
            description="Writes an independent black-box pytest suite from the spec only. "
            "Output: TestSuite.",
            mission="Write an independent pytest suite from the specification alone - you are the "
            "oracle that catches the implementer's mistakes. Import from the module named in the "
            "spec. Tests must be deterministic, fast and hermetic. The harness checks your suite by "
            "planting bugs into the implementation: tests that pass on buggy code are worthless. If "
            "`revision_feedback` is among the inputs, fix every listed problem in `previous_attempt` "
            "and keep the tests that are correct.",
            output_model=TestSuite,
        ),
        AgentSpec(
            name="security_auditor",
            title="Security Auditor",
            description="Audits the implementation for vulnerabilities and misuse risks; "
            "HIGH/CRITICAL findings fail the audit. Output: SecurityReport.",
            mission="Audit the implementation against the spec for security weaknesses, including "
            "abuse of inputs, unsafe APIs and resource exhaustion. Report each finding with "
            "severity, location, and a concrete fix. Do not report style issues.",
            output_model=SecurityReport,
        ),
        AgentSpec(
            name="code_reviewer",
            title="Code Reviewer",
            description="Final quality gate: checks spec coverage, sandbox results and the "
            "security report; approves or requests changes. Output: ReviewReport.",
            mission="Act as the final quality gate. APPROVE only if the sandbox passed, the audit "
            "has no blocking findings and every acceptance criterion is implemented. Otherwise "
            "REQUEST_CHANGES with concrete, actionable issues addressed to the implementer "
            "(what is wrong, where, how to fix).",
            output_model=ReviewReport,
        ),
    )
}


def agent_catalog() -> str:
    return "\n".join(f"- {a.name}: {a.description}" for a in AGENTS.values())


@dataclass(frozen=True)
class AgentRun:
    output: AgentOutput
    system_prompt: str
    user_prompt: str


class SubAgent:
    """Stateless specialist: role prompt + injected skills + scoped objective + routed inputs."""

    def __init__(self, spec: AgentSpec, llm: LLMClient):
        self.spec = spec
        self.llm = llm

    def system_prompt(self, attachments: list[SkillAttachment]) -> str:
        skills = "\n\n".join(a.skill.render() for a in attachments) or NO_SKILLS
        return SUBAGENT_PROMPT.format(
            title=self.spec.title,
            mission=self.spec.mission,
            skills=skills,
            schema=schema_json(self.spec.output_model),
        )

    @staticmethod
    def user_prompt(step_id: str, objective: str, inputs: list[tuple[str, str]]) -> str:
        parts = [f"# Objective (from Dispatcher, step {step_id})\n{objective}", "# Inputs"]
        parts += [f"## {label}\n{content}" for label, content in inputs]
        return "\n\n".join(parts)

    def run(
        self,
        *,
        step_id: str,
        objective: str,
        inputs: list[tuple[str, str]],
        attachments: list[SkillAttachment],
        revision: int = 0,
    ) -> AgentRun:
        system = self.system_prompt(attachments)
        user = self.user_prompt(step_id, objective, inputs)
        call = LLMCall(
            agent=self.spec.name,
            system=system,
            user=user,
            skills=tuple(a.skill.name for a in attachments),
            revision=revision,
        )
        output = complete_structured(self.llm, call, self.spec.output_model)
        return AgentRun(output=output, system_prompt=system, user_prompt=user)
