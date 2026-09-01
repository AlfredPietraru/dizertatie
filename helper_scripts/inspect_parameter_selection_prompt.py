#!/usr/bin/env python3
"""Materialize the exact parameter-selection LLM input for inspection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ros_config_builder.mission import (
    CapabilitySelections,
    ParameterSelectionInterpretation,
    build_parameter_selection_prompt,
    build_parameter_selection_system_context,
    build_selection_context,
    derive_parameter_catalogue,
    realize_capabilities,
    resolve_ros_orchestration,
    retrieve_parameters,
)
from ros_config_builder.orchestrate import (
    DEFAULT_CONFIGURATION_PATH,
    build_mission_application,
    load_application_configuration,
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
        "--capabilities-json",
        help=("Optional CapabilitySelections JSON. When omitted, the configured capability "
              "interpreter is called before constructing the selection prompt."),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("artifacts/prompt_inspection/parameter_selection_input.json"),
    )
    args = parser.parse_args()

    configuration = load_application_configuration(args.configuration)
    mission = (args.mission or configuration.mission).strip()
    application = build_mission_application(configuration)
    if args.capabilities_json:
        capabilities = CapabilitySelections.model_validate(json.loads(args.capabilities_json))
        capability_source = "command_line_json"
    else:
        interpretation = application.interpreter.interpret(mission)
        if interpretation.status != "valid" or interpretation.capabilities is None:
            parser.error(
                "capability interpretation did not produce valid capabilities; "
                f"status={interpretation.status!r}"
            )
        capabilities = interpretation.capabilities
        capability_source = "configured_capability_interpreter"

    assert application.capability_registry is not None
    realization = realize_capabilities(capabilities, application.capability_registry)
    orchestration = resolve_ros_orchestration(
        realization, application.capability_registry,
        application.reference_model, application.manifest,
    )
    catalogue = derive_parameter_catalogue(
        realization, application.schema,
        evidence_by_id=application.parameter_evidence,
        semantic_enrichments=application.semantic_enrichments,
    )
    retrieval = retrieve_parameters(
        mission, catalogue,
        variant=configuration.context_variant,
        top_k=configuration.retrieval_top_k,
        graph_hops=configuration.graph_hops,
        wiring_bindings=application.manifest.get("wiring_bindings", {}),
    )
    selection_context = build_selection_context(retrieval, catalogue)
    system_context = build_parameter_selection_system_context(
        orchestration.model_dump(mode="json"),
    )
    prompt_path = configuration.resolve(configuration.parameter_selection_prompt)
    assert prompt_path is not None
    system_prompt = build_parameter_selection_prompt(
        prompt_path.read_text(encoding="utf-8"), selection_context,
        system_context=system_context,
    )
    user_prompt = mission
    request = {
        "model": configuration.ollama_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": ParameterSelectionInterpretation.model_json_schema(),
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    artifact = {
        "description": "Exact parameter-selection input before Ollama chat templating.",
        "configuration": str(args.configuration),
        "mission": mission,
        "capability_source": capability_source,
        "capabilities": capabilities.model_dump(mode="json"),
        "system_context": system_context,
        "selection_context": selection_context.model_dump(mode="json"),
        "retrieval_diagnostics": retrieval.model_dump(mode="json"),
        "ollama_request": request,
        "sizes": {
            "system_context": _size(system_context),
            "selection_context": _size(selection_context.model_dump(mode="json")),
            "system_prompt": _size(system_prompt),
            "user_prompt": _size(user_prompt),
        },
    }
    output = args.output if args.output.is_absolute() else configuration.workspace / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "sizes": artifact["sizes"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
