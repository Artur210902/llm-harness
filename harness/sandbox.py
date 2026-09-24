"""Code-execution tool: runs generated tests against generated code.

Isolation is process-level only (temp dir, separate interpreter, timeout, scrubbed env).
It protects the harness from crashes and hangs, not from malicious code - use a
container or VM for untrusted generations in production.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .schemas import CodeArtifact, SandboxReport, TestSuite

_COUNT = re.compile(r"(\d+) (passed|failed|errors?)")
_ENV_KEEP = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")


class PytestSandbox:
    def __init__(self, timeout_s: int = 60, tail_chars: int = 3000):
        self.timeout_s = timeout_s
        self.tail_chars = tail_chars

    def run(self, code: CodeArtifact, tests: TestSuite, *, extra_args: tuple[str, ...] = (),
            timeout_s: int | None = None) -> SandboxReport:
        timeout_s = timeout_s or self.timeout_s
        env = {k: v for k in _ENV_KEEP if (v := os.environ.get(k)) is not None}
        env |= {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory(prefix="harness_sandbox_") as tmp:
            workdir = Path(tmp)
            # module_name is schema-validated as a Python identifier, so no path tricks here
            (workdir / f"{code.module_name}.py").write_text(code.code, encoding="utf-8")
            (workdir / f"test_{tests.module_name}.py").write_text(tests.test_code, encoding="utf-8")
            cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *extra_args]
            started = time.perf_counter()
            try:
                proc = subprocess.run(
                    cmd, cwd=workdir, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                return SandboxReport(status="timeout", duration_s=timeout_s,
                                     output_tail=f"killed after {timeout_s}s")
            duration = time.perf_counter() - started
        output = (proc.stdout + proc.stderr).strip()
        counts = {"passed": 0, "failed": 0, "errors": 0}
        summary = next((ln for ln in reversed(output.splitlines()) if _COUNT.search(ln)), "")
        for number, kind in _COUNT.findall(summary):
            counts["errors" if kind.startswith("error") else kind] = int(number)
        if proc.returncode == 0:
            status = "passed"
        elif counts["failed"] or counts["errors"]:
            status = "failed"
        else:
            status = "error"  # collection/usage error, no tests ran
        return SandboxReport(
            status=status, duration_s=round(duration, 2),
            output_tail=output[-self.tail_chars:], **counts,
        )
