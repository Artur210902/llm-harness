"""Root agent: plans, delegates to sub-agents with injected skills, gates and revises."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .agents import AGENTS, SANDBOX, AgentSpec, SubAgent, agent_catalog
from .llm import LLMCall, LLMClient, OutputParseError, complete_structured, schema_json
from .prompts import DISPATCHER_METAPROMPT, FINALIZER_PROMPT
from .sandbox import PytestSandbox
from .schemas import (
    Artifact,
    CodeArtifact,
    ExecutionPlan,
    FinalReport,
    PlanStep,
    ReviewReport,
    SandboxReport,
    SecurityReport,
    TestSuite,
)
from .skills import SkillRegistry
from .tracing import Tracer

REQUEST = "request"


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GateResult:
    passed: bool
    problems: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        return "\n".join(f"- {p}" for p in self.problems)


@dataclass
class RunResult:
    plan: ExecutionPlan
    outputs: dict[str, Artifact]
    gate: GateResult
    revisions: int
    final: FinalReport

    def latest(self, kind: type[Artifact]) -> Artifact | None:
        return next((o for o in reversed(self.outputs.values()) if isinstance(o, kind)), None)


def validate_plan(plan: ExecutionPlan, agents: dict[str, AgentSpec], registry: SkillRegistry) -> None:
    """Enforce the metaprompt's hard constraints; the dispatcher gets the errors back on failure."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for step in plan.steps:
        where = f"step {step.id} ({step.agent})"
        if step.id in seen or step.id == REQUEST:
            errors.append(f"{where}: duplicate or reserved id")
        if step.agent != SANDBOX and step.agent not in agents:
            errors.append(f"{where}: unknown agent")
        for name in step.skills:
            if name not in registry:
                errors.append(f"{where}: unknown skill '{name}'")
            elif step.agent not in registry.get(name).applies_to:
                errors.append(f"{where}: skill '{name}' does not apply to this agent")
        for ref in step.inputs:
            if ref != REQUEST and ref not in seen:
                errors.append(f"{where}: input '{ref}' is not an earlier step")
        if step.agent == "test_generator" and any(seen.get(r) == "code_generator" for r in step.inputs):
            errors.append(f"{where}: test_generator must not see the implementation")
        if step.agent == SANDBOX:
            routed = {seen.get(r) for r in step.inputs}
            if not {"code_generator", "test_generator"} <= routed:
                errors.append(f"{where}: sandbox needs code_generator and test_generator inputs")
        seen[step.id] = step.agent

    agents_used = [s.agent for s in plan.steps]
    if agents_used.count("code_generator") != 1:
        errors.append("plan must contain exactly one code_generator step")
    if "test_generator" in agents_used and SANDBOX not in agents_used:
        errors.append("a sandbox step must follow code_generator and test_generator")
    if not plan.steps or plan.steps[-1].agent != "code_reviewer":
        errors.append("the last step must be code_reviewer")
    else:
        routed = {seen.get(r) for r in plan.steps[-1].inputs}
        for needed in (SANDBOX, "security_auditor"):
            if needed in agents_used and needed not in routed:
                errors.append(f"code_reviewer must receive the {needed} output")
    if errors:
        raise PlanValidationError("\n".join(f"- {e}" for e in errors))


class Dispatcher:
    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: SkillRegistry,
        sandbox: PytestSandbox,
        tracer: Tracer,
        agents: dict[str, AgentSpec] = AGENTS,
        max_revisions: int = 2,
        use_skills: bool = True,
    ):
        self.llm = llm
        self.registry = registry
        self.sandbox = sandbox
        self.tracer = tracer
        self.agents = agents
        self.max_revisions = max_revisions
        self.use_skills = use_skills

    # --- public -----------------------------------------------------------------

    def run(self, request: str) -> RunResult:
        plan = self.plan(request)
        outputs: dict[str, Artifact] = {}
        for step in plan.steps:
            self._run_step(plan, step, request, outputs)

        gate = self._quality_gate(plan, outputs)
        revisions = 0
        while not gate.passed and revisions < self.max_revisions:
            revisions += 1
            rerun = self._steps_to_rerun(plan)
            self.tracer.revision(revisions, [s.id for s in rerun])
            for step in rerun:
                self._run_step(plan, step, request, outputs, feedback=gate.feedback(), revision=revisions)
            gate = self._quality_gate(plan, outputs)

        final = self._finalize(request, plan, outputs, gate, revisions)
        self._save_artifacts(outputs)
        return RunResult(plan=plan, outputs=outputs, gate=gate, revisions=revisions, final=final)

    def plan(self, request: str) -> ExecutionPlan:
        system = DISPATCHER_METAPROMPT.format(
            agents=agent_catalog(), skills=self.registry.catalog(), schema=schema_json(ExecutionPlan)
        )
        user = f"# User request\n{request.strip()}"
        self.tracer.save("prompts/s0_dispatcher.md", f"# SYSTEM\n\n{system}\n\n# USER\n\n{user}\n")
        for attempt in (1, 2):
            self.tracer.planning(len(self.agents), len(self.registry), attempt)
            try:
                plan = complete_structured(self.llm, LLMCall("dispatcher", system, user), ExecutionPlan)
                validate_plan(plan, self.agents, self.registry)
            except (OutputParseError, PlanValidationError) as exc:
                self.tracer.warn(f"plan rejected: {exc}")
                user = f"{user}\n\n# Your previous plan was rejected\n{exc}\nReturn a corrected plan."
                continue
            self.tracer.plan(plan)
            return plan
        raise PlanValidationError("dispatcher failed to produce a valid plan")

    # --- execution --------------------------------------------------------------

    def _run_step(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        request: str,
        outputs: dict[str, Artifact],
        *,
        feedback: str | None = None,
        revision: int = 0,
    ) -> None:
        if step.agent == SANDBOX:
            code = self._latest(outputs, step.inputs, CodeArtifact)
            tests = self._latest(outputs, step.inputs, TestSuite)
            report = self.sandbox.run(code, tests)
            outputs[step.id] = report
            self.tracer.tool(step, report)
            return

        if self.use_skills:
            attachments, warnings = self.registry.resolve(
                step.agent, step.skills, f"{step.objective}\n{request}"
            )
            for warning in warnings:
                self.tracer.warn(warning)
        else:
            attachments = []

        agent_of = {s.id: s.agent for s in plan.steps}
        inputs = [
            (REQUEST, request.strip()) if ref == REQUEST
            else (f"{ref} · {agent_of[ref]} → {type(outputs[ref]).__name__}", outputs[ref].render_for_prompt())
            for ref in step.inputs
        ]
        if feedback is not None and step.agent == "code_generator" and step.id in outputs:
            inputs.append(("previous_attempt", outputs[step.id].render_for_prompt()))
            inputs.append(("revision_feedback", feedback))

        agent = SubAgent(self.agents[step.agent], self.llm)
        system = agent.system_prompt(attachments)
        user = agent.user_prompt(step.id, step.objective, inputs)
        self.tracer.delegate(step, attachments, revision, [label.split(" ·")[0] for label, _ in inputs],
                             (len(system), len(user)))
        started = time.perf_counter()
        run = agent.run(step_id=step.id, objective=step.objective, inputs=inputs,
                        attachments=attachments, revision=revision)
        elapsed = time.perf_counter() - started

        self.tracer.save_prompts(step.id, step.agent, revision, run.system_prompt, run.user_prompt)
        known_rules = set().union(*(a.skill.rule_ids for a in attachments))
        cited = run.output.applied_skill_rules
        self.tracer.result(step, run.output, elapsed, cited, set(cited) - known_rules)
        outputs[step.id] = run.output

    @staticmethod
    def _latest(outputs: dict[str, Artifact], refs: list[str], kind: type[Artifact]):
        for ref in reversed(refs):
            if isinstance(outputs.get(ref), kind):
                return outputs[ref]
        raise PlanValidationError(f"no {kind.__name__} among inputs {refs}")

    @staticmethod
    def _steps_to_rerun(plan: ExecutionPlan) -> list[PlanStep]:
        """The code_generator step and every step that transitively depends on it."""
        generator = next(s for s in plan.steps if s.agent == "code_generator")
        dirty = {generator.id}
        rerun = []
        for step in plan.steps:
            if step.id in dirty or dirty & set(step.inputs):
                dirty.add(step.id)
                rerun.append(step)
        return rerun

    def _quality_gate(self, plan: ExecutionPlan, outputs: dict[str, Artifact]) -> GateResult:
        problems: list[str] = []
        for step in plan.steps:
            out = outputs.get(step.id)
            if isinstance(out, SandboxReport) and out.status != "passed":
                failing = [ln for ln in out.output_tail.splitlines() if ln.startswith(("FAILED", "ERROR"))]
                problems.append(f"sandbox {out.headline()}; " + ("; ".join(failing) or out.output_tail[-500:]))
            elif isinstance(out, SecurityReport) and out.verdict == "FAIL":
                problems += [f"security [{f.severity}] {f.rule_id or ''} {f.location}: {f.description} "
                             f"Fix: {f.recommendation}" for f in out.findings if f.severity in {"CRITICAL", "HIGH"}]
            elif isinstance(out, ReviewReport) and out.verdict == "REQUEST_CHANGES":
                problems += [f"review: {issue}" for issue in out.issues] or ["review: changes requested"]
        gate = GateResult(passed=not problems, problems=problems)
        self.tracer.gate(gate.passed, gate.problems)
        return gate

    # --- closing ----------------------------------------------------------------

    def _finalize(self, request: str, plan: ExecutionPlan, outputs: dict[str, Artifact],
                  gate: GateResult, revisions: int) -> FinalReport:
        digest = []
        for step in plan.steps:
            out = outputs[step.id]
            digest.append(f"- {step.id} {step.agent}: {out.headline()}")
            if isinstance(out, SecurityReport):
                digest += [f"  finding [{f.severity}] {f.rule_id or '-'}: {f.description} "
                           f"Recommendation: {f.recommendation}" for f in out.findings]
            if isinstance(out, ReviewReport):
                digest += [f"  review note: {out.summary}"] + [f"  issue: {i}" for i in out.issues]
        user = (
            f"# User request\n{request.strip()}\n\n# Quality gate\npassed={gate.passed}; "
            f"revisions={revisions}\n{gate.feedback()}\n\n# Step results\n" + "\n".join(digest)
        )
        call = LLMCall("dispatcher.finalize", FINALIZER_PROMPT.format(schema=schema_json(FinalReport)), user)
        report = complete_structured(self.llm, call, FinalReport)
        forced = not gate.passed and report.status != "FAILED"
        if forced:
            report = report.model_copy(update={"status": "FAILED"})
        self.tracer.final(report, revisions, forced)
        return report

    def _save_artifacts(self, outputs: dict[str, Artifact]) -> None:
        code = next((o for o in reversed(outputs.values()) if isinstance(o, CodeArtifact)), None)
        tests = next((o for o in reversed(outputs.values()) if isinstance(o, TestSuite)), None)
        if code:
            self.tracer.save(f"artifacts/{code.module_name}.py", code.code)
        if tests:
            self.tracer.save(f"artifacts/test_{tests.module_name}.py", tests.test_code)
