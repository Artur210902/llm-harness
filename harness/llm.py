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


class OutputParseError(ValueError):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw  # the offending reply, kept for the run folder


class TruncatedReplyError(OutputParseError):
    """The model hit its output-token limit, so the JSON is cut off."""


class OpenAICompatibleClient:
    def __init__(self, *, api_key: str, model: str, base_url: str | None, temperature: float,
                 ssl_verify: bool = True, max_tokens: int | None = None):
        import httpx
        from openai import OpenAI  # imported lazily: offline mode needs no SDK

        # ssl_verify=False is for self-hosted endpoints with self-signed certificates
        http_client = None if ssl_verify else httpx.Client(verify=False)
        self._client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.name = f"{model} @ {base_url or 'api.openai.com'}"

    def complete(self, call: LLMCall) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": call.system},
                {"role": "user", "content": call.user},
            ],
            **({"max_tokens": self._max_tokens} if self._max_tokens else {}),
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        used = getattr(response.usage, "completion_tokens", None)
        # some servers cut the reply at the limit yet report finish_reason="stop"
        if choice.finish_reason == "length" or (self._max_tokens and used and used >= self._max_tokens):
            raise TruncatedReplyError(
                "your reply was cut off at the output-token limit, so the JSON is incomplete", raw=content
            )
        return content


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
    raise OutputParseError("reply is not a JSON object", raw=text)


def parse_model(text: str, model: type[M]) -> M:
    try:
        return model.model_validate(_extract_json(text))
    except ValidationError as exc:
        raise OutputParseError(f"JSON does not match the schema: {exc}", raw=text) from exc


def complete_structured(
    llm: LLMClient, call: LLMCall, model: type[M], *, repairs: int = 2
) -> M:
    """Call the model and parse its reply; on invalid output, tell it what was wrong and retry."""
    for attempt in range(repairs + 1):
        try:
            return parse_model(llm.complete(call), model)
        except OutputParseError as exc:
            if attempt == repairs:
                raise
            hint = ("Be more concise: the same content in fewer words; for code, fewer and sharper "
                    "cases." if isinstance(exc, TruncatedReplyError)
                    else "Make sure every string is valid JSON (escape quotes, backslashes and newlines).")
            call = replace(
                call,
                user=f"{call.user}\n\n# Your previous reply was rejected\n{exc}\n{hint}\n"
                "Reply again with ONLY the corrected JSON object.",
            )
    raise AssertionError("unreachable")


def schema_json(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False)
