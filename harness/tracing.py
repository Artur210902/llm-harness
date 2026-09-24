"""Delegation log: human-readable console output plus a machine-readable run folder.

runs/<timestamp>_<case>/
  trace.jsonl          one JSON event per line (plan, delegate, result, tool, gate, ...)
  plan.json            the validated execution plan
  prompts/<step>.md    exact system + user prompts sent to each sub-agent (skills visible)
  artifacts/           generated module, tests and the final report
"""

from __future__ import annotations

import functools
import json
import threading
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from .schemas import Artifact, ExecutionPlan, FinalReport, PlanStep, RevisionDecision, SandboxReport
from .skills import SkillAttachment

_STATUS_STYLE = {"DELIVERED": "green", "DELIVERED_WITH_RISKS": "yellow", "FAILED": "red"}


def _short(text: str, limit: int = 150) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _atomic(method):
    """Steps of one wave run in parallel: keep each event's console block and JSON line intact."""
    @functools.wraps(method)
    def wrapper(self: Tracer, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Tracer:
    def __init__(self, run_dir: Path, console: Console | None = None):
        self.run_dir = run_dir
        self.console = console or Console(highlight=False)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._trace = (run_dir / "trace.jsonl").open("a", encoding="utf-8")
        self._lock = threading.RLock()

    def close(self) -> None:
        self._trace.close()

    # --- persistence ------------------------------------------------------------

    @_atomic
    def _event(self, kind: str, **payload: Any) -> None:
        record = {"ts": round(time.time(), 3), "event": kind, **payload}
        self._trace.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._trace.flush()

    @_atomic
    def save(self, relative: str, content: str) -> Path:
        path = self.run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _print(self, text: str) -> None:
        self.console.print(text, highlight=False)

    # --- run lifecycle ----------------------------------------------------------

    def run_started(self, title: str, request: str, llm: str, use_skills: bool) -> None:
        self._event("run_started", title=title, request=request, llm=llm, use_skills=use_skills)
        skills = "[green]enabled[/]" if use_skills else "[red]DISABLED (ablation)[/]"
        body = f"{escape(request.strip())}\n\n[dim]llm:[/] {escape(llm)}   [dim]skills:[/] {skills}"
        self.console.print(Panel(body, title=f"[bold]{escape(title)}[/]", border_style="cyan"))

    def planning(self, n_agents: int, n_skills: int, attempt: int) -> None:
        self._event("planning", attempt=attempt)
        retry = f" (retry {attempt})" if attempt > 1 else ""
        self._print(
            f"[bold magenta]DISPATCHER[/] planning{retry}: {n_agents} sub-agents + sandbox, "
            f"{n_skills} skills in catalog (metadata only)"
        )

    def plan(self, plan: ExecutionPlan) -> None:
        self._event("plan", plan=plan.model_dump())
        self.save("plan.json", plan.model_dump_json(indent=2))
        self._print(f"[bold magenta]DISPATCHER[/] plan accepted: {len(plan.steps)} steps")
        self._print(f"  [dim]analysis:[/] {escape(_short(plan.analysis, 300))}")
        for s in plan.steps:
            skills = ", ".join(s.skills) or "-"
            self._print(
                f"  {s.id:<3} {s.agent:<21} [dim]skills:[/] {escape(skills):<45} "
                f"[dim]in:[/] {', '.join(s.inputs)}"
            )

    @_atomic
    def warn(self, message: str) -> None:
        self._event("warning", message=message)
        self._print(f"[yellow]  ! {escape(message)}[/]")

    # --- delegation -------------------------------------------------------------

    @_atomic
    def delegate(
        self,
        step: PlanStep,
        attachments: list[SkillAttachment],
        revision: int,
        input_labels: list[str],
        prompt_chars: tuple[int, int],
    ) -> None:
        skills_payload = [
            {"name": a.skill.name, "version": a.skill.version, "source": a.source,
             "reason": a.reason, "approx_tokens": a.skill.approx_tokens}
            for a in attachments
        ]
        self._event("delegate", step=step.id, agent=step.agent, revision=revision,
                    objective=step.objective, skills=skills_payload, inputs=input_labels)
        rev = f" [yellow](revision {revision})[/]" if revision else ""
        self._print(f"\n[bold cyan]▶ {step.id} DISPATCH → {step.agent}[/]{rev}")
        self._print(f"    objective: {escape(_short(step.objective))}")
        self._print(f"    inputs   : {escape(', '.join(input_labels))}")
        if not attachments:
            self._print("    skills   : [dim]none[/]")
        for i, a in enumerate(attachments):
            label = "skills   :" if i == 0 else "          "
            style = "green" if a.source == "planner" else "blue"
            self._print(
                f"    {label} [{style}]+ {a.skill.name} v{a.skill.version}[/] "
                f"~{a.skill.approx_tokens} tok [{style}]\\[{a.source}][/] [dim]{escape(a.reason)}[/]"
            )
        system_chars, user_chars = prompt_chars
        self._print(f"    prompt   : [dim]system {system_chars} chars, user {user_chars} chars[/]")

    @_atomic
    def save_prompts(self, step_id: str, agent: str, revision: int, system: str, user: str) -> None:
        suffix = f".rev{revision}" if revision else ""
        self.save(f"prompts/{step_id}_{agent}{suffix}.md",
                  f"# SYSTEM\n\n{system}\n\n# USER\n\n{user}\n")

    @_atomic
    def result(self, step: PlanStep, output: Artifact, elapsed_s: float,
               applied_rules: list[str], unknown_rules: set[str]) -> None:
        self._event("result", step=step.id, agent=step.agent, elapsed_s=round(elapsed_s, 3),
                    headline=output.headline(), applied_rules=applied_rules,
                    unknown_rules=sorted(unknown_rules))
        self._print(f"[bold]◀ {step.id} {step.agent}[/] ✔ {elapsed_s:.2f}s  {escape(output.headline())}")
        if applied_rules:
            self._print(f"    applied skill rules: [green]{', '.join(applied_rules)}[/]")
        if unknown_rules:
            self.warn(f"{step.agent} cited rules not present in injected skills: "
                      f"{', '.join(sorted(unknown_rules))}")

    @_atomic
    def tool(self, step: PlanStep, report: SandboxReport) -> None:
        self._event("tool", step=step.id, tool=step.agent, report=report.model_dump())
        style = "green" if report.status == "passed" else "red"
        self._print(f"\n[bold cyan]▶ {step.id} TOOL → sandbox[/] (pytest, isolated temp dir)")
        self._print(f"[bold]◀ {step.id} sandbox[/] [{style}]{escape(report.headline())}[/]")
        if report.status != "passed":
            failing = [ln for ln in report.output_tail.splitlines() if ln.startswith(("FAILED", "ERROR"))]
            for line in failing[:5]:
                self._print(f"    [red]{escape(_short(line, 140))}[/]")
        if report.mutation is not None:
            for s in report.mutation.survivors:
                self._print(f"    [yellow]survived {s.operator} (line {s.line}): {escape(_short(s.description, 110))}[/]")

    # --- gate, revisions, final -------------------------------------------------

    @_atomic
    def gate(self, passed: bool, problems: list[str]) -> None:
        self._event("gate", passed=passed, problems=problems)
        if passed:
            self._print("\n[bold green]QUALITY GATE passed[/]")
            return
        self._print(f"\n[bold red]QUALITY GATE failed[/] ({len(problems)} problem(s))")
        for problem in problems:
            self._print(f"    [red]- {escape(_short(problem, 200))}[/]")

    @_atomic
    def revision(self, number: int, decision: RevisionDecision, rerun_ids: list[str]) -> None:
        self._event("revision", number=number, decision=decision.model_dump(), rerun=rerun_ids)
        self._print(
            f"[bold magenta]DISPATCHER[/] revision {number}: feedback → {' + '.join(decision.agents)}, "
            f"re-running dependent steps {', '.join(rerun_ids)}"
        )
        self._print(f"    rationale: {escape(_short(decision.rationale, 200))}")

    def final(self, report: FinalReport, revisions: int, forced: bool) -> None:
        self._event("final", report=report.model_dump(), revisions=revisions, policy_override=forced)
        self.save("artifacts/final_report.json", report.model_dump_json(indent=2))
        style = _STATUS_STYLE[report.status]
        lines = [escape(report.summary)]
        if report.residual_risks:
            lines.append("\n[bold]Residual risks:[/]")
            lines += [f"- {escape(r)}" for r in report.residual_risks]
        if forced:
            lines.append("\n[dim]status forced by harness policy (gate not passed)[/]")
        lines.append(f"\n[dim]revisions: {revisions} · run folder: {self.run_dir}[/]")
        self.console.print(Panel("\n".join(lines), title=f"[bold {style}]{report.status}[/]",
                                 border_style=style))
