"""Root agent: plans, delegates to sub-agents with injected skills, gates and routes revisions."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .agents import AGENTS, SANDBOX, AgentSpec, SubAgent, agent_catalog
from .llm import LLMCall, LLMClient, OutputParseError, complete_structured, schema_json
from .mutation import MutationTester, weakness
from .prompts import DISPATCHER_METAPROMPT, FINALIZER_PROMPT, REVISION_PROMPT
from .sandbox import PytestSandbox
from .schemas import (
    Artifact,
    CodeArtifact,
    ExecutionPlan,
    FinalReport,
    PlanStep,
    RequirementsSpec,
    ReviewReport,
    RevisionDecision,
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
    weak_tests: bool = False  # the mutation check says the tests cannot be trusted

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
    if agents_used.count("test_generator") > 1:
        errors.append("plan must contain at most one test_generator step")
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


def steps_to_rerun(plan: ExecutionPlan, agents: tuple[str, ...]) -> list[PlanStep]:
    """The steps of the given agents and every step that transitively depends on them."""
    dirty: set[str] = set()
    rerun = []
    for step in plan.steps:
        if step.agent in agents or dirty & set(step.inputs):
            dirty.add(step.id)
            rerun.append(step)
    return rerun


def execution_waves(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Group steps into waves: a step runs once every step it consumes (within `steps`) is done."""
    pending = list(steps)
    waves = []
    while pending:
        waiting = {s.id for s in pending}
        wave = [s for s in pending if not waiting & set(s.inputs)]
        if not wave:  # unreachable for a validated plan (inputs reference earlier steps only)
            raise PlanValidationError("plan has a dependency cycle")
        waves.append(wave)
        pending = [s for s in pending if s not in wave]
    return waves


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
        mutation: MutationTester | None = None,
        mutation_threshold: float = 0.6,
        parallel: bool = True,
    ):
        self.llm = llm
        self.registry = registry
        self.sandbox = sandbox
        self.tracer = tracer
        self.agents = agents
        self.max_revisions = max_revisions
        self.use_skills = use_skills
        self.mutation = mutation
        self.mutation_threshold = mutation_threshold
        self.parallel = parallel

    # --- public -----------------------------------------------------------------

    def run(self, request: str) -> RunResult:
        plan = self.plan(request)
        outputs: dict[str, Artifact] = {}
        self._execute(plan, plan.steps, request, outputs)

        gate = self._quality_gate(plan, outputs)
        revisions = 0
        while not gate.passed and revisions < self.max_revisions:
            revisions += 1
            decision = self._decide_revision(request, plan, outputs, gate)
            rerun = steps_to_rerun(plan, decision.agents)
            self.tracer.revision(revisions, decision, [s.id for s in rerun])
            feedback = {"code_generator": decision.feedback_for_code, "test_generator": decision.feedback_for_tests}
            self._execute(plan, rerun, request, outputs,
                          feedback={a: feedback[a] or gate.feedback() for a in decision.agents},
                          revision=revisions)
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

    def _execute(self, plan: ExecutionPlan, steps: list[PlanStep], request: str, outputs: dict[str, Artifact],
                 *, feedback: dict[str, str] | None = None, revision: int = 0) -> None:
        """Run steps wave by wave; independent steps of a wave run concurrently."""
        feedback = feedback or {}
        for wave in execution_waves(steps):
            def run(step: PlanStep) -> Artifact:
                return self._run_step(plan, step, request, outputs,
                                      feedback=feedback.get(step.agent), revision=revision)

            if self.parallel and len(wave) > 1:
                with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                    results = list(pool.map(run, wave))
            else:
                results = [run(step) for step in wave]
            # written after the wave, in plan order: steps of one wave never read each other
            for step, output in zip(wave, results):
                outputs[step.id] = output

    def _run_step(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        request: str,
        outputs: dict[str, Artifact],
        *,
        feedback: str | None = None,
        revision: int = 0,
    ) -> Artifact:
        if step.agent == SANDBOX:
            return self._run_sandbox(step, outputs)

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
        if feedback and step.id in outputs:
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
        return run.output

    def _run_sandbox(self, step: PlanStep, outputs: dict[str, Artifact]) -> SandboxReport:
        code = self._latest(outputs, step.inputs, CodeArtifact)
        tests = self._latest(outputs, step.inputs, TestSuite)
        report = self.sandbox.run(code, tests)
        if report.status == "passed" and self.mutation is not None:
            report = report.model_copy(update={"mutation": self.mutation.run(code, tests, report.duration_s)})
        self.tracer.tool(step, report)
        return report

    @staticmethod
    def _latest(outputs: dict[str, Artifact], refs: list[str], kind: type[Artifact]):
        for ref in reversed(refs):
            if isinstance(outputs.get(ref), kind):
                return outputs[ref]
        raise PlanValidationError(f"no {kind.__name__} among inputs {refs}")

    def _quality_gate(self, plan: ExecutionPlan, outputs: dict[str, Artifact]) -> GateResult:
        problems: list[str] = []
        weak_tests = False
        for step in plan.steps:
            out = outputs.get(step.id)
            if isinstance(out, SandboxReport) and out.status != "passed":
                failing = [ln for ln in out.output_tail.splitlines() if ln.startswith(("FAILED", "ERROR"))]
                problems.append(f"sandbox {out.headline()}; " + ("; ".join(failing) or out.output_tail[-500:]))
            elif isinstance(out, SandboxReport) and out.mutation is not None:
                if problem := weakness(out.mutation, self.mutation_threshold):
                    problems.append(problem)
                    weak_tests = True
            elif isinstance(out, SecurityReport) and out.verdict == "FAIL":
                problems += [f"security [{f.severity}] {f.rule_id or ''} {f.location}: {f.description} "
                             f"Fix: {f.recommendation}" for f in out.findings if f.severity in {"CRITICAL", "HIGH"}]
            elif isinstance(out, ReviewReport) and out.verdict == "REQUEST_CHANGES":
                problems += [f"review: {issue}" for issue in out.issues] or ["review: changes requested"]
        gate = GateResult(passed=not problems, problems=problems, weak_tests=weak_tests)
        self.tracer.gate(gate.passed, gate.problems)
        return gate

    # --- revisions --------------------------------------------------------------

    def _decide_revision(self, request: str, plan: ExecutionPlan, outputs: dict[str, Artifact],
                         gate: GateResult) -> RevisionDecision:
        """The dispatcher routes the revision; the harness enforces policy on its decision."""
        spec = next((o for o in outputs.values() if isinstance(o, RequirementsSpec)), None)
        sandbox = next((o for o in reversed(outputs.values()) if isinstance(o, SandboxReport)), None)
        parts = [f"# User request\n{request.strip()}", f"# Quality gate problems\n{gate.feedback()}",
                 "# Step results\n" + "\n".join(self._digest(plan, outputs))]
        if spec:
            parts.append("# Acceptance criteria (spec)\n" + "\n".join(f"- {c}" for c in spec.acceptance_criteria))
        if sandbox and sandbox.status != "passed":
            parts.append(f"# Sandbox output (tail)\n```\n{sandbox.output_tail[-2000:]}\n```")
        call = LLMCall("dispatcher.revise", REVISION_PROMPT.format(schema=schema_json(RevisionDecision)),
                       "\n\n".join(parts))
        try:
            decision = complete_structured(self.llm, call, RevisionDecision)
        except OutputParseError as exc:
            self.tracer.warn(f"revision decision unparseable ({exc}); defaulting to code_generator")
            decision = RevisionDecision(target="code_generator", rationale="fallback: unparseable decision")

        has_tests = any(s.agent == "test_generator" for s in plan.steps)
        agents = set(decision.agents)
        if gate.weak_tests and has_tests:
            agents.add("test_generator")
        if not has_tests:
            agents.discard("test_generator")
        agents = agents or {"code_generator"}
        target = "both" if len(agents) == 2 else agents.pop()
        if target != decision.target:
            self.tracer.warn(f"revision target {decision.target} overridden by harness policy -> {target}")
            decision = decision.model_copy(update={"target": target})
        return decision

    # --- closing ----------------------------------------------------------------

    @staticmethod
    def _digest(plan: ExecutionPlan, outputs: dict[str, Artifact]) -> list[str]:
        digest = []
        for step in plan.steps:
            if (out := outputs.get(step.id)) is None:
                continue
            digest.append(f"- {step.id} {step.agent}: {out.headline()}")
            if isinstance(out, SecurityReport):
                digest += [f"  finding [{f.severity}] {f.rule_id or '-'}: {f.description} "
                           f"Recommendation: {f.recommendation}" for f in out.findings]
            if isinstance(out, ReviewReport):
                digest += [f"  review note: {out.summary}"] + [f"  issue: {i}" for i in out.issues]
        return digest

    def _finalize(self, request: str, plan: ExecutionPlan, outputs: dict[str, Artifact],
                  gate: GateResult, revisions: int) -> FinalReport:
        user = (
            f"# User request\n{request.strip()}\n\n# Quality gate\npassed={gate.passed}; "
            f"revisions={revisions}\n{gate.feedback()}\n\n# Step results\n" + "\n".join(self._digest(plan, outputs))
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
