"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str | None
    model: str | None
    ssl_verify: bool
    temperature: float
    max_revisions: int
    sandbox_timeout_s: int
    skills_dir: Path
    runs_dir: Path

    @property
    def has_llm(self) -> bool:
        return bool(self.api_key and self.model)

    @classmethod
    def from_env(cls, root: Path) -> Settings:
        load_dotenv(root / ".env")
        env = os.environ.get
        return cls(
            api_key=env("HARNESS_API_KEY") or env("OPENAI_API_KEY") or None,
            base_url=env("HARNESS_BASE_URL") or None,
            model=env("HARNESS_MODEL") or None,
            ssl_verify=env("HARNESS_SSL_VERIFY", "true").lower() not in ("0", "false", "no"),
            temperature=float(env("HARNESS_TEMPERATURE", "0.2")),
            max_revisions=int(env("HARNESS_MAX_REVISIONS", "2")),
            sandbox_timeout_s=int(env("HARNESS_SANDBOX_TIMEOUT", "60")),
            skills_dir=root / "skills",
            runs_dir=root / "runs",
        )
