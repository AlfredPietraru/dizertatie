"""Model-independent mission inference with strict structured validation."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .schema import AntRobotCapabilityRegistry, MissionInterpretation, capability_prompt_catalogue


class ChatBackend(Protocol):
    def __call__(self, system_prompt: str, user_prompt: str) -> str: ...


INTERPRETATION_SCHEMA_PLACEHOLDER = "{{MISSION_INTERPRETATION_JSON_SCHEMA}}"
CAPABILITY_REGISTRY_PLACEHOLDER = "{{ANTROBOT_CAPABILITY_REGISTRY}}"


def build_interpretation_prompt(
    template: str,
    registry: AntRobotCapabilityRegistry,
) -> str:
    """Inject the response schema and LLM-safe capability catalogue."""
    for placeholder in (INTERPRETATION_SCHEMA_PLACEHOLDER, CAPABILITY_REGISTRY_PLACEHOLDER):
        if template.count(placeholder) != 1:
            raise ValueError(f"prompt must contain exactly one {placeholder} placeholder")
    schema = json.dumps(MissionInterpretation.model_json_schema(), indent=2, sort_keys=True)
    catalogue = json.dumps(capability_prompt_catalogue(registry), indent=2, sort_keys=True)
    return template.replace(INTERPRETATION_SCHEMA_PLACEHOLDER, schema).replace(
        CAPABILITY_REGISTRY_PLACEHOLDER, catalogue,
    ).rstrip()


def extract_json_object(raw: str) -> dict[str, Any]:
    """Accept plain JSON and fenced JSON, while rejecting surrounding prose."""
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[0] not in {"```", "```json"} or lines[-1] != "```":
            raise ValueError("model response contains an invalid code fence")
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"model response is not a single JSON object: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


class OllamaBackend:
    """Small standard-library client for Ollama's local chat endpoint."""

    def __init__(self, *, host: str | None = None, model: str | None = None, timeout: float = 300,
                 response_model: type[BaseModel] = MissionInterpretation,
                 context_window: int = 8192) -> None:
        raw_host = host or os.getenv("OLLAMA_HOST_PATH") or "127.0.0.1"
        self.host = raw_host.removeprefix("http://").removeprefix("https://").rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.timeout = timeout
        self.response_model = response_model
        if context_window < 1024:
            raise ValueError("Ollama context window must be at least 1024 tokens")
        self.context_window = context_window

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": self.response_model.model_json_schema(),
            # Some evidence-rich single-parameter prompts exceed Ollama's
            # default 4K runtime context.  Keep enough room for the largest
            # frozen evidence entry and its structured response.
            "options": {"temperature": 0, "num_ctx": self.context_window},
        }).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self.host}:11434/api/chat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = body.get("error", str(error)) if isinstance(body, dict) else str(error)
            except (UnicodeError, json.JSONDecodeError):
                detail = str(error)
            hint = f" Run `ollama pull {self.model}` or select an installed model with `--model`." \
                if error.code == 404 else ""
            raise RuntimeError(f"Ollama rejected model {self.model!r}: {detail}.{hint}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(
                f"Cannot reach Ollama at http://{self.host}:11434: {error}. "
                "Start it with `ollama serve`."
            ) from error
        try:
            return result["message"]["content"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("Ollama returned an unexpected response") from error


class MissionInterpreter:
    """Interpret capability choices, then realize them through a fixed registry."""

    response_model = MissionInterpretation

    def __init__(
        self,
        backend: ChatBackend,
        *,
        system_prompt: str,
        registry: AntRobotCapabilityRegistry,
    ) -> None:
        self.backend = backend
        self.system_prompt = system_prompt
        self.registry = registry

    def interpret(self, mission: str) -> MissionInterpretation:
        if not mission.strip():
            raise ValueError("mission must not be empty")
        raw = self.backend(self.system_prompt, mission.strip())
        try:
            return MissionInterpretation.model_validate(extract_json_object(raw))
        except ValidationError as error:
            raise ValueError(f"model output violates MissionInterpretation: {error}") from error
