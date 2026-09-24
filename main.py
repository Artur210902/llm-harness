"""Run the LLM harness on the bundled test cases (or on your own request).

    python main.py                       # both cases
    python main.py --case ratelimit      # one case
    python main.py --case upload --no-skills   # ablation: same plan, no skills injected
    python main.py --request "..."       # custom request (needs a real LLM)

Without HARNESS_API_KEY/OPENAI_API_KEY + HARNESS_MODEL the offline scripted model is used.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console

from harness.config import Settings
from harness.dispatcher import Dispatcher
from harness.llm import LLMClient, OpenAICompatibleClient
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
                                      ssl_verify=settings.ssl_verify)
    if not case.get("offline_script"):
        sys.exit("Custom requests need a real LLM: set HARNESS_API_KEY and HARNESS_MODEL (see .env.example).")
    return OfflineScriptedClient(case["offline_script"])


def run_case(case: dict, settings: Settings, registry: SkillRegistry, args: argparse.Namespace,
             console: Console) -> str:
    llm = build_llm(settings, case, args.offline)
    suffix = "_noskills" if args.no_skills else ""
    run_dir = settings.runs_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{case['id']}{suffix}"
    tracer = Tracer(run_dir, console)
    try:
        tracer.run_started(case["title"], case["request"], llm.name, not args.no_skills)
        dispatcher = Dispatcher(
            llm=llm, registry=registry, sandbox=PytestSandbox(settings.sandbox_timeout_s),
            tracer=tracer, max_revisions=settings.max_revisions, use_skills=not args.no_skills,
        )
        return dispatcher.run(case["request"]).final.status
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
    args = parser.parse_args(argv)

    settings = Settings.from_env(ROOT)
    registry = SkillRegistry(settings.skills_dir)
    console = Console(highlight=False, width=120)
    if args.request:
        selected = [{"id": "custom", "title": "Custom request", "request": args.request}]
    else:
        selected = list(cases.values()) if args.case == "all" else [cases[args.case]]

    statuses = {case["id"]: run_case(case, settings, registry, args, console) for case in selected}
    console.print("\n[bold]Summary:[/] " + ", ".join(f"{k} → {v}" for k, v in statuses.items()))
    return 0 if all(s != "FAILED" for s in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
