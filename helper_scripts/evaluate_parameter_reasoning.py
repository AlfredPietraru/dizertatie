#!/usr/bin/env python3
"""Run intrinsic parameter-retrieval/selection/value experiments with context ablations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ros_config_builder.integration import SystemModel
from ros_config_builder.mission import (
    CapabilitySelections, OllamaBackend, ParameterReasoner,
    ParameterSelectionInterpretation, ParameterValueInterpretation,
    derive_parameter_catalogue, enrichment_by_parameter_id, evaluate_parameter_reasoning,
    evidence_by_parameter_id, load_capability_registry, load_parameter_evidence_artifact,
    load_parameter_reasoning_tasks, load_semantic_enrichment_artifact,
    parameter_evidence_artifact_matches,
    realize_capabilities,
    resolve_ros_orchestration, write_parameter_evaluation_report,
    semantic_enrichment_artifact_matches,
    validate_capability_registry,
)
from ros_config_builder.orchestrate import load_environment
from ros_config_builder.templating import TemplateConfigurationSchema, TemplateManifest


VARIANTS = ("names_values", "semantic", "source", "system", "graph")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--dataset", type=Path,
                        default=Path("data/parameter_reasoning_tasks_v1.jsonl"))
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/parameter_reasoning/ablation_v1"))
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--host", default=None)
    parser.add_argument("--selection-prompt", type=Path,
                        default=Path("prompts/parameter_selection.txt"))
    parser.add_argument("--value-prompt", type=Path,
                        default=Path("prompts/parameter_value_reasoning.txt"))
    parser.add_argument("--semantic-enrichment", type=Path, default=None)
    parser.add_argument("--context-variant", action="append", choices=VARIANTS)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--graph-hops", type=int, default=1)
    parser.add_argument("--include-no-change", action="store_true")
    parser.add_argument("--allow-pending-review", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    load_environment(workspace / ".env")
    resolve = lambda path: path if path.is_absolute() else workspace / path
    dataset = resolve(args.dataset)
    output = resolve(args.output)
    selection_prompt = resolve(args.selection_prompt)
    value_prompt = resolve(args.value_prompt)
    system_path = workspace / "artifacts/ros_system_model/ros_system_model.json"
    schema_path = workspace / "artifacts/template_configuration/template_configuration_schema.json"
    manifest_path = workspace / "configuration_templates/manifest.json"
    registry_path = workspace / "configuration_templates/capability_registry.yaml"
    evidence_path = workspace / "artifacts/semantic_parameters/evidence.json"
    system_model = SystemModel.model_validate_json(
        system_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    schema = TemplateConfigurationSchema.model_validate_json(
        schema_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    manifest = TemplateManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    registry = load_capability_registry(registry_path)
    validate_capability_registry(registry, system_model, schema, manifest)
    realization = realize_capabilities(CapabilitySelections(), registry)
    orchestration = resolve_ros_orchestration(realization, registry, system_model, manifest)
    evidence_artifact = load_parameter_evidence_artifact(evidence_path)
    if not parameter_evidence_artifact_matches(
        evidence_artifact, source_model=system_model, configuration_model=schema,
    ):
        parser.error("parameter evidence hashes do not match the system model and schema")
    evidence = evidence_artifact.parameters
    enrichments = {}
    if args.semantic_enrichment is not None:
        enrichment = load_semantic_enrichment_artifact(resolve(args.semantic_enrichment))
        if not semantic_enrichment_artifact_matches(
            enrichment, source_model=system_model, configuration_model=schema,
        ):
            parser.error("semantic enrichment hashes do not match the system model and schema")
        enrichments = enrichment_by_parameter_id(enrichment)
    catalogue = derive_parameter_catalogue(
        realization, schema, evidence_by_id=evidence_by_parameter_id(evidence),
        semantic_enrichments=enrichments,
    )
    if dataset.name == "parameter_reasoning_tasks_v1.jsonl":
        tasks = load_parameter_reasoning_tasks(dataset, catalogue)
        pending = [task.id for task in tasks if task.review_status != "human_verified"]
        if pending and not args.allow_pending_review:
            parser.error(
                f"{len(pending)} parameter tasks still require human review; "
                "review them or pass --allow-pending-review for a development-only run"
            )
    selection_backend = OllamaBackend(
        host=args.host, model=args.model, response_model=ParameterSelectionInterpretation,
    )
    value_backend = OllamaBackend(
        host=args.host, model=args.model, response_model=ParameterValueInterpretation,
    )
    variants = args.context_variant or list(VARIANTS)
    summaries = {}
    for variant in variants:
        reasoner = ParameterReasoner(
            selection_backend, value_backend,
            selection_prompt_template=selection_prompt.read_text(encoding="utf-8"),
            value_prompt_template=value_prompt.read_text(encoding="utf-8"),
            context_variant=variant, top_k=args.top_k, graph_hops=args.graph_hops,
        )
        report = evaluate_parameter_reasoning(
            dataset, reasoner, catalogue,
            system_context={
                "system_realization": realization.model_dump(mode="json"),
                "ros_orchestration": orchestration.model_dump(mode="json"),
            },
            wiring_bindings=manifest.get("wiring_bindings", {}),
            include_no_change=args.include_no_change,
        )
        write_parameter_evaluation_report(report, output / variant)
        summaries[variant] = report["metrics"]
    metadata = {
        "model": args.model, "context_variants": variants, "top_k": args.top_k,
        "graph_hops": args.graph_hops, "include_no_change": args.include_no_change,
        "hashes": {str(path.relative_to(workspace)): _digest(path) for path in (
            dataset, selection_prompt, value_prompt, system_path, schema_path,
            manifest_path, evidence_path,
            *([resolve(args.semantic_enrichment)] if args.semantic_enrichment is not None else []),
        )},
        "metrics": summaries,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "experiment.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
