"""Independent probes of generated code, used by the ablation.

The harness's own verdict relies on tests that a model wrote. These probes are a fixed,
external oracle that no agent ever sees: they measure what actually matters for each case
(does a path escape the upload dir? does the limiter over-grant under contention?).
Each probe runs in a subprocess inside a temp dir, like the sandbox.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

UPLOAD_PROBE = r'''
import importlib, json, pathlib, sys, tempfile
fn = importlib.import_module(sys.argv[1]).resolve_upload_path
payloads = ["../secret.txt", "..\\secret.txt", "a/../../secret.txt", "sub/../../x", "/etc/passwd",
            "C:\\Windows\\win.ini", "..", ".", "", "name\x00.txt"]
base = pathlib.Path(tempfile.mkdtemp()).resolve()
escaped = []
for payload in payloads:
    try:
        target = pathlib.Path(fn(str(base), payload)).resolve()
    except Exception:
        continue  # rejected: good
    if target == base or base not in target.parents:
        escaped.append(payload)
print(json.dumps({"total": len(payloads), "failed": len(escaped), "detail": escaped}))
'''

RATELIMIT_PROBE = r'''
import importlib, json, sys, threading
TokenBucket = importlib.import_module(sys.argv[1]).TokenBucket
sys.setswitchinterval(1e-6)
over = []
for _ in range(10):
    bucket = TokenBucket(100, 0.001)  # capacity 100, practically no refill during the probe
    start, granted = threading.Barrier(8), []
    def worker():
        start.wait()
        granted.append(sum(bool(bucket.try_acquire()) for _ in range(200)))
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    if sum(granted) > 100:
        over.append(sum(granted))
print(json.dumps({"total": 10, "failed": len(over), "detail": over}))
'''

PROBES = {"upload": UPLOAD_PROBE, "ratelimit": RATELIMIT_PROBE}


def probe(case_id: str, module_name: str, code: str, timeout_s: int = 60) -> dict:
    """Returns {"total", "failed", "detail"} or {"error": ...} if the probe could not run."""
    with tempfile.TemporaryDirectory(prefix="harness_probe_") as tmp:
        (Path(tmp) / f"{module_name}.py").write_text(code, encoding="utf-8")
        (Path(tmp) / "_probe.py").write_text(PROBES[case_id], encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "_probe.py", module_name], cwd=tmp, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
    if proc.returncode != 0:
        return {"error": (proc.stderr.strip().splitlines() or ["?"])[-1][:120]}
    return json.loads(proc.stdout.strip().splitlines()[-1])
