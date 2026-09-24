"""Skills ablation on a real LLM: cases x {skills, no skills} x models x N runs.

    python scripts/ablation.py --models qwen-nothink,qwen --repeat 2

Each run goes through the full harness (main.run_case). Besides the harness verdict, the final
code is checked by an independent probe (scripts/probes.py) that no agent sees. Results are
appended to runs/ablation_<ts>.jsonl as they come and summarized in runs/ablation_<ts>.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from probes import probe  # noqa: E402

from harness.config import Settings  # noqa: E402
from harness.schemas import CodeArtifact, SandboxReport, SecurityReport, TestSuite  # noqa: E402
from harness.skills import SkillRegistry  # noqa: E402
from main import load_cases, run_case  # noqa: E402


def measure(case_id: str, settings: Settings, registry: SkillRegistry, use_skills: bool) -> dict:
    started = time.perf_counter()
    result, run_dir = run_case(load_cases()[case_id], settings, registry, Console(quiet=True),
                               use_skills=use_skills)
    code = result.latest(CodeArtifact)
    tests = result.latest(TestSuite)
    sandbox = result.latest(SandboxReport)
    audit = result.latest(SecurityReport)
    return {
        "status": result.final.status,
        "revisions": result.revisions,
        "tests": tests.test_count if tests else 0,
        "sandbox": sandbox.status if sandbox else "-",
        "mutation": round(sandbox.mutation.score, 2) if sandbox and sandbox.mutation else None,
        "blocking_findings": sum(f.severity in {"CRITICAL", "HIGH"} for f in audit.findings) if audit else 0,
        "probe": probe(case_id, code.module_name, code.code) if code else {"error": "no code"},
        "minutes": round((time.perf_counter() - started) / 60, 1),
        "run_dir": run_dir.name,
    }


def summarize(rows: list[dict]) -> str:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["case"], row["skills"])].append(row)
    lines = ["| model | case | skills | runs | statuses | mutation score | independent probe: failed/total |",
             "|---|---|---|---|---|---|---|"]
    for (model, case, skills), group in sorted(groups.items()):
        statuses = ", ".join(r.get("status", "ERROR") for r in group)
        scores = [r["mutation"] for r in group if r.get("mutation") is not None]
        mutation = f"{sum(scores) / len(scores):.2f}" if scores else "-"
        probes = ", ".join(
            f"{r['probe']['failed']}/{r['probe']['total']}" if "failed" in r.get("probe", {})
            else f"err: {r.get('probe', {}).get('error') or r.get('error', '?')}" for r in group
        )
        lines.append(f"| {model} | {case} | {'yes' if skills else 'no'} | {len(group)} | {statuses} | "
                     f"{mutation} | {probes} |")
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", required=True, help="comma-separated model names")
    parser.add_argument("--cases", default="upload,ratelimit")
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    base = Settings.from_env(ROOT)
    if not base.has_llm:
        sys.exit("The ablation needs a real LLM: configure HARNESS_API_KEY/HARNESS_MODEL in .env.")
    registry = SkillRegistry(base.skills_dir)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    jsonl = base.runs_dir / f"ablation_{stamp}.jsonl"
    base.runs_dir.mkdir(exist_ok=True)

    rows = []
    for model in args.models.split(","):
        settings = dataclasses.replace(base, model=model)
        for case_id in args.cases.split(","):
            for use_skills in (True, False):
                for attempt in range(1, args.repeat + 1):
                    row = {"model": model, "case": case_id, "skills": use_skills, "attempt": attempt}
                    try:
                        row |= measure(case_id, settings, registry, use_skills)
                    except Exception as exc:  # one broken run must not stop the ablation
                        row |= {"error": f"{type(exc).__name__}: {exc}"[:200]}
                        traceback.print_exc()
                    rows.append(row)
                    with jsonl.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(json.dumps(row, ensure_ascii=False), flush=True)

    table = summarize(rows)
    (base.runs_dir / f"ablation_{stamp}.md").write_text(table + "\n", encoding="utf-8")
    print("\n" + table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
