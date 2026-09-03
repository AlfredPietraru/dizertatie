#!/usr/bin/env python3
"""Run the frozen zero-shot mission evaluation against local Ollama."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ros_config_builder.integration import SystemModel
from ros_config_builder.mission import (
    MissionInterpretation, MissionInterpreter, OllamaBackend, build_interpretation_prompt,
    evaluate_missions, load_capability_registry, validate_capability_registry,
    write_evaluation_report,
)
from ros_config_builder.templating import TemplateConfigurationSchema, TemplateManifest


DEFAULT_EVALUATION_OUTPUT = Path(
    "artifacts/evaluation/capability_interpretation/zero_shot"
)


def _load_local_environment(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True,
                        help="Training or test JSONL dataset to evaluate.")
    parser.add_argument("--output", type=Path, default=DEFAULT_EVALUATION_OUTPUT)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt", type=Path, default=Path("prompts/mission_interpretation.txt"))
    parser.add_argument("--capability-registry", type=Path,
                        default=Path("configuration_templates/capability_registry.yaml"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = Path.cwd().resolve()
    _load_local_environment(workspace / ".env")
    system_model = SystemModel.model_validate(_read_json(
        workspace / "artifacts/ros_system_model/ros_system_model.json"
    )).model_dump(mode="json")
    schema = TemplateConfigurationSchema.model_validate(_read_json(
        workspace / "artifacts/template_configuration/template_configuration_schema.json"
    )).model_dump(mode="json")
    manifest = TemplateManifest.model_validate(_read_json(
        workspace / "configuration_templates/manifest.json"
    )).model_dump(mode="json")
    prompt_template = args.prompt.read_text(encoding="utf-8")
    registry = load_capability_registry(args.capability_registry)
    validate_capability_registry(registry, system_model, schema, manifest)
    interpreter = MissionInterpreter(
        OllamaBackend(model=args.model, response_model=MissionInterpretation),
        system_prompt=build_interpretation_prompt(prompt_template, registry),
        registry=registry,
    )
    report = evaluate_missions(
        args.dataset, interpreter, registry=registry,
        schema=schema, manifest=manifest,
    )
    paths = write_evaluation_report(report, args.output)
    (args.output / "system_prompt.txt").write_text(interpreter.system_prompt + "\n", encoding="utf-8")
    print(json.dumps({"model": interpreter.backend.model, "prompt": str(args.prompt), "metrics": report["metrics"],
                      "artifacts": {key: str(value) for key, value in paths.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
