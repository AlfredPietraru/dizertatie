#!/usr/bin/env python3
"""Generate a versioned LLM semantic-enrichment artifact from frozen evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ros_config_builder.integration import SystemModel
from ros_config_builder.mission import (
    OllamaBackend, SemanticEnricher, SemanticEnrichmentBatch,
    build_semantic_enrichment_artifact, load_parameter_evidence_artifact,
    parameter_evidence_artifact_matches, write_semantic_enrichment,
)
from ros_config_builder.templating import TemplateConfigurationSchema


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--system-model", type=Path,
                        default=Path("artifacts/ros_system_model/ros_system_model.json"))
    parser.add_argument("--configuration-model", type=Path, default=Path(
        "artifacts/template_configuration/template_configuration_schema.json"))
    parser.add_argument("--prompt", type=Path,
                        default=Path("prompts/parameter_semantic_enrichment.txt"))
    parser.add_argument("--evidence", type=Path,
                        default=Path("artifacts/semantic_parameters/evidence.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/semantic_parameters/enrichment.json"))
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--host", default=None)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--prompt-version", default="semantic_enrichment_v1")
    return parser


def _load_frozen_evidence(
    path: Path,
    *,
    system_model: dict,
    configuration_model: dict,
):
    artifact = load_parameter_evidence_artifact(path)
    if not parameter_evidence_artifact_matches(
        artifact,
        source_model=system_model,
        configuration_model=configuration_model,
    ):
        raise ValueError(
            f"parameter evidence hashes do not match the frozen models: {path}"
        )
    return artifact.parameters


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    workspace = args.workspace.resolve()
    system_path = args.system_model if args.system_model.is_absolute() else workspace / args.system_model
    configuration_path = (args.configuration_model if args.configuration_model.is_absolute()
                          else workspace / args.configuration_model)
    prompt_path = args.prompt if args.prompt.is_absolute() else workspace / args.prompt
    system_model = SystemModel.model_validate_json(
        system_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    configuration_model = TemplateConfigurationSchema.model_validate_json(
        configuration_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    evidence_path = args.evidence if args.evidence.is_absolute() else workspace / args.evidence
    try:
        evidence = _load_frozen_evidence(
            evidence_path,
            system_model=system_model,
            configuration_model=configuration_model,
        )
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    backend = OllamaBackend(
        host=args.host, model=args.model, response_model=SemanticEnrichmentBatch,
    )
    enricher = SemanticEnricher(
        backend, prompt_template=prompt_path.read_text(encoding="utf-8"),
    )
    known_ids = {item.parameter_id for item in evidence}
    records = []
    for start in range(0, len(evidence), args.batch_size):
        batch = enricher.enrich(
            evidence[start:start + args.batch_size], known_parameter_ids=known_ids,
        )
        records.extend(batch.enrichments)
    artifact = build_semantic_enrichment_artifact(
        SemanticEnrichmentBatch(enrichments=records), source_model=system_model,
        configuration_model=configuration_model, model_identifier=backend.model,
        prompt_version=args.prompt_version,
    )
    output = args.output if args.output.is_absolute() else workspace / args.output
    write_semantic_enrichment(artifact, output)
    print(json.dumps({
        "parameters": len(records), "model": backend.model,
        "evidence": str(evidence_path), "enrichment": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
