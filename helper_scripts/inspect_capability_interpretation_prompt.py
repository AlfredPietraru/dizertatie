#!/usr/bin/env python3
"""Materialize the exact capability-interpretation LLM input for inspection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ros_config_builder.mission import (
    MissionInterpretation, build_interpretation_prompt, load_capability_registry,
)
from ros_config_builder.orchestrate import (
    DEFAULT_CONFIGURATION_PATH, load_application_configuration,
)


def _size(value: Any) -> dict[str, int]:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
    return {
        "characters": len(text),
        "utf8_bytes": len(text.encode("utf-8")),
        "estimated_tokens_chars_div_4": round(len(text) / 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, default=DEFAULT_CONFIGURATION_PATH)
    parser.add_argument("--mission", help="Override the mission in the application configuration")
    parser.add_argument(
        "--output", type=Path,
        default=Path("artifacts/prompt_inspection/capability_interpretation_input.json"),
    )
    args = parser.parse_args()

    configuration = load_application_configuration(args.configuration)
    mission = (args.mission or configuration.mission).strip()
    registry_path = configuration.resolve(configuration.capability_registry)
    prompt_path = configuration.resolve(configuration.capability_prompt)
    assert registry_path is not None and prompt_path is not None
    registry = load_capability_registry(registry_path)
    system_prompt = build_interpretation_prompt(
        prompt_path.read_text(encoding="utf-8"), registry,
    )
    request = {
        "model": configuration.ollama_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mission},
        ],
        "stream": False,
        "format": MissionInterpretation.model_json_schema(),
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    artifact = {
        "description": "Exact capability-interpretation input before Ollama chat templating.",
        "configuration": str(args.configuration),
        "mission": mission,
        "ollama_request": request,
        "sizes": {
            "system_prompt": _size(system_prompt),
            "user_prompt": _size(mission),
        },
    }
    output = args.output if args.output.is_absolute() else configuration.workspace / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "sizes": artifact["sizes"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
