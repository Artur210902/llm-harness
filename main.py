"""Run the LLM harness on the bundled test cases (or on your own request).

    python main.py                       # both cases
    python main.py --case ratelimit      # one case
    python main.py --case upload --no-skills   # ablation: same plan, no skills injected
    python main.py --request "..."       # custom request (needs a real LLM)
    python main.py --model qwen --repeat 3     # override HARNESS_MODEL, repeat each case

Without HARNESS_API_KEY/OPENAI_API_KEY + HARNESS_MODEL the offline scripted model is used.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console

from harness.config import Settings
from harness.dispatcher import Dispatcher, RunResult
from harness.llm import LLMClient, OpenAICompatibleClient
from harness.mutation import MutationTester
from harness.offline import OfflineScriptedClient
from harness.sandbox import PytestSandbox
from harness.skills import SkillRegistry
from harness.tracing import Tracer

ROOT = Path(__file__).resolve().parent


def load_cases() -> dict[str, dict]:
    cases = (yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "cases").glob("*.yaml")))
    return {c["id"]: c for c in cases}


def build_llm(settings: Settings, case: dict, force_offline: bool) -> LLMClient:
    if settings.has_llm and not force_offline:
        return OpenAICompatibleClient(api_key=settings.api_key, model=settings.model,
                                      base_url=settings.base_url, temperature=settings.temperature,
                                      ssl_verify=settings.ssl_verify, max_tokens=settings.max_tokens)
    if not case.get("offline_script"):
        sys.exit("Custom requests need a real LLM: set HARNESS_API_KEY and HARNESS_MODEL (see .env.example).")
    return OfflineScriptedClient(case["offline_script"])


def run_case(case: dict, settings: Settings, registry: SkillRegistry, console: Console, *,
             use_skills: bool = True, force_offline: bool = False) -> tuple[RunResult, Path]:
    llm = build_llm(settings, case, force_offline)
    suffix = "" if use_skills else "_noskills"
    run_dir = settings.runs_dir / f"{datetime.now():%Y%m%d_%H%M%S_%f}_{case['id']}{suffix}"
    tracer = Tracer(run_dir, console)
    sandbox = PytestSandbox(settings.sandbox_timeout_s)
    try:
        tracer.run_started(case["title"], case["request"], llm.name, use_skills)
        dispatcher = Dispatcher(
            llm=llm, registry=registry, sandbox=sandbox, tracer=tracer,
            max_revisions=settings.max_revisions, use_skills=use_skills,
            mutation=MutationTester(sandbox, settings.max_mutants) if settings.max_mutants else None,
            mutation_threshold=settings.mutation_threshold, parallel=settings.parallel,
        )
        return dispatcher.run(case["request"]), run_dir
    finally:
        tracer.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    cases = load_cases()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", choices=[*cases, "all"], default="all")
    parser.add_argument("--request", help="custom user request instead of a bundled case")
    parser.add_argument("--offline", action="store_true", help="force the offline scripted model")
    parser.add_argument("--no-skills", action="store_true", help="ablation: do not inject any skills")
    parser.add_argument("--model", help="override HARNESS_MODEL for this run")
    parser.add_argument("--repeat", type=int, default=1, help="run each case N times")
    args = parser.parse_args(argv)

    settings = Settings.from_env(ROOT)
    if args.model:
        settings = dataclasses.replace(settings, model=args.model)
    registry = SkillRegistry(settings.skills_dir)
    console = Console(highlight=False, width=120)
    if args.request:
        selected = [{"id": "custom", "title": "Custom request", "request": args.request}]
    else:
        selected = list(cases.values()) if args.case == "all" else [cases[args.case]]

    statuses = []
    for case in selected:
        for _ in range(args.repeat):
            result, _ = run_case(case, settings, registry, console,
                                 use_skills=not args.no_skills, force_offline=args.offline)
            statuses.append((case["id"], result.final.status))
    console.print("\n[bold]Summary:[/] " + ", ".join(f"{k} → {v}" for k, v in statuses))
    return 0 if all(s != "FAILED" for _, s in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
