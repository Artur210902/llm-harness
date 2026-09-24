"""LLM transport and structured-output parsing.

The harness talks to any OpenAI-compatible Chat Completions endpoint (OpenAI,
Anthropic's compatibility endpoint, OpenRouter, vLLM, Ollama, ...). Everything
above this module is provider-agnostic and only sees `LLMClient.complete`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class LLMCall:
    agent: str  # who is calling: "dispatcher", "dispatcher.finalize" or a sub-agent name
    system: str
    user: str
    skills: tuple[str, ...] = ()
    revision: int = 0


class LLMClient(Protocol):
    name: str

    def complete(self, call: LLMCall) -> str: ...


class OpenAICompatibleClient:
    def __init__(self, *, api_key: str, model: str, base_url: str | None, temperature: float):
        from openai import OpenAI  # imported lazily: offline mode needs no SDK

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._temperature = temperature
        self.name = f"{model} @ {base_url or 'api.openai.com'}"

    def complete(self, call: LLMCall) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": call.system},
                {"role": "user", "content": call.user},
            ],
        )
        return response.choices[0].message.content or ""


class OutputParseError(ValueError):
    pass


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _extract_json(text: str) -> object:
    text = text.strip()
    candidates = [text]
    if match := _FENCED_JSON.search(text):
        candidates.append(match.group(1))
    if (start := text.find("{")) != -1 and (end := text.rfind("}")) > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise OutputParseError("reply is not a JSON object")


def parse_model(text: str, model: type[M]) -> M:
    try:
        return model.model_validate(_extract_json(text))
    except ValidationError as exc:
        raise OutputParseError(f"JSON does not match the schema: {exc}") from exc


def complete_structured(
    llm: LLMClient, call: LLMCall, model: type[M], *, repairs: int = 1
) -> M:
    """Call the model and parse its reply; on invalid output, ask it to repair once."""
    for attempt in range(repairs + 1):
        raw = llm.complete(call)
        try:
            return parse_model(raw, model)
        except OutputParseError as exc:
            if attempt == repairs:
                raise
            call = replace(
                call,
                user=f"{call.user}\n\n# Your previous reply was rejected\n{exc}\n"
                "Reply again with ONLY the corrected JSON object.",
            )
    raise AssertionError("unreachable")


def schema_json(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False)
