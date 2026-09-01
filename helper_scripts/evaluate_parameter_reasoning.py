#!/usr/bin/env python3
"""Run intrinsic parameter-retrieval/selection/value experiments with context ablations."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from ros_config_builder.integration import SystemModel
from ros_config_builder.mission import (
    CapabilitySelections, MissionInterpretation, MissionInterpreter, OllamaBackend, ParameterReasoner,
    ParameterSelectionInterpretation, ParameterValueInterpretation,
    RealizedComponent, SystemRealization, build_interpretation_prompt, derive_parameter_catalogue,
    enrichment_by_parameter_id, evaluate_parameter_reasoning,
    build_parameter_selection_system_context,
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


VARIANTS = ("names_values", "semantic", "source", "system", "graph", "llm_semantic")


class _LoggingBackend:
    """Report each model call, including an internal validation-repair call."""

    def __init__(self, backend: OllamaBackend, stage: str) -> None:
        self.backend = backend
        self.stage = stage
        self.calls = 0
        self.records: list[dict[str, object]] = []

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        started = time.perf_counter()
        record: dict[str, object] = {
            "stage": self.stage, "call": self.calls, "started_at": started,
            "system_prompt": system_prompt, "user_prompt": user_prompt,
            "raw_response": None, "backend_error": None,
        }
        print(f"  [{self.stage}] LLM call {self.calls} started", flush=True)
        try:
            result = self.backend(system_prompt, user_prompt)
        except Exception as error:
            record["backend_error"] = f"{type(error).__name__}: {error}"
            self.records.append(record)
            elapsed = time.perf_counter() - started
            print(
                f"  [{self.stage}] LLM call {self.calls} failed after {elapsed:.1f}s: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
            raise
        record["raw_response"] = result
        self.records.append(record)
        elapsed = time.perf_counter() - started
        print(f"  [{self.stage}] LLM call {self.calls} completed in {elapsed:.1f}s", flush=True)
        return result


def _failure_explanation(record: dict[str, object]) -> str:
    lines = []
    if record.get("error"):
        lines.extend(["Runtime or validation error:", str(record["error"])])
    if not record.get("outcome_correct"):
        lines.extend([
            "Outcome mismatch:",
            f"expected_status={record.get('expected_status')}",
            f"predicted_status={record.get('predicted_status')}",
        ])
    selection = record.get("selection")
    if isinstance(selection, dict) and not selection.get("exact"):
        lines.extend([
            "Selection mismatch:",
            f"missed={json.dumps(selection.get('missed', []), sort_keys=True)}",
            f"over_selected={json.dumps(selection.get('over_selected', []), sort_keys=True)}",
        ])
    values = record.get("value_comparison")
    if isinstance(values, dict) and not values.get("exact"):
        incorrect = [item for item in values.get("items", []) if not item.get("correct")]
        lines.extend([
            "Value mismatch:",
            f"incorrect={json.dumps(incorrect, sort_keys=True)}",
            f"extra_values={json.dumps(values.get('extra_values', []), sort_keys=True)}",
        ])
    return "\n".join(lines).strip() + "\n"


def _pretty_prompt_for_log(prompt: str) -> str:
    """Pretty-print embedded JSON blocks in persisted prompts, never runtime prompts."""
    decoder = json.JSONDecoder()
    for marker in (
        "Stable system context:\n",
        "Bounded selection context:\n",
        "Selected parameter context:\n",
        "Required JSON schema:\n",
    ):
        search_from = 0
        while True:
            marker_index = prompt.find(marker, search_from)
            if marker_index < 0:
                break
            value_start = marker_index + len(marker)
            try:
                value, consumed = decoder.raw_decode(prompt[value_start:])
            except json.JSONDecodeError:
                search_from = value_start
                continue
            rendered = json.dumps(value, indent=2, sort_keys=True)
            prompt = prompt[:value_start] + rendered + prompt[value_start + consumed:]
            search_from = value_start + len(rendered)
    return prompt


def _progress_logger(
    variant: str,
    output: Path,
    selection_backend: _LoggingBackend,
    value_backend: _LoggingBackend,
    capability_backend: _LoggingBackend | None = None,
):
    counts = {"completed": 0, "errors": 0, "correct": 0}
    call_offsets = {"parameter_selection": 0, "parameter_value": 0}
    capability_offset = 0
    expected_result: dict[str, object] = {}

    def log(event: dict[str, object]) -> None:
        nonlocal capability_offset
        if event["event"] == "case_started":
            capability_offset = len(capability_backend.records) if capability_backend else 0
            call_offsets["parameter_selection"] = len(selection_backend.records)
            call_offsets["parameter_value"] = len(value_backend.records)
            expected_result.clear()
            expected_result.update(event["expected_result"])
            print(
                f"[{variant}] case {event['index']}/{event['total']} "
                f"{event['id']} started: {event['mission']}",
                flush=True,
            )
            return
        counts["completed"] += 1
        counts["errors"] += int(event["status"] == "error")
        counts["correct"] += int(bool(event["end_to_end_correct"]))
        if not event["end_to_end_correct"]:
            record = event["record"]
            selection_calls = selection_backend.records[call_offsets["parameter_selection"]:]
            value_calls = value_backend.records[call_offsets["parameter_value"]:]
            selection_failed = (
                not bool(record.get("outcome_correct"))
                or not bool(record.get("selection", {}).get("exact"))
            )
            relevant_calls = selection_calls if selection_failed or not value_calls else value_calls
            if not relevant_calls:
                capability_calls = (
                    capability_backend.records[capability_offset:] if capability_backend else []
                )
                relevant_calls = selection_calls + value_calls + capability_calls
            failure = output / "failures" / str(event["id"])
            failure.mkdir(parents=True, exist_ok=True)
            if relevant_calls:
                final_call = relevant_calls[-1]
                failure.joinpath("system_prompt.txt").write_text(
                    _pretty_prompt_for_log(str(final_call["system_prompt"])), encoding="utf-8",
                )
                failure.joinpath("user_prompt.txt").write_text(
                    str(final_call["user_prompt"]), encoding="utf-8",
                )
            else:
                failure.joinpath("system_prompt.txt").write_text(
                    "No LLM call was reached.\n", encoding="utf-8",
                )
                failure.joinpath("user_prompt.txt").write_text(
                    "No LLM call was reached.\n", encoding="utf-8",
                )
            failure.joinpath("expected_result.json").write_text(
                json.dumps(expected_result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            failure.joinpath("error.txt").write_text(
                _failure_explanation(record), encoding="utf-8",
            )
        detail = f" error={event['error']}" if event["error"] else ""
        print(
            f"[{variant}] case {event['index']}/{event['total']} {event['id']} finished: "
            f"status={event['status']} end_to_end_correct={event['end_to_end_correct']} "
            f"running_completed={counts['completed']} running_correct={counts['correct']} "
            f"running_errors={counts['errors']}{detail}",
            flush=True,
        )

    return log


class _CapabilityConditionedReasoner:
    """Build a task-specific active catalogue before parameter reasoning."""

    def __init__(
        self, reasoner: ParameterReasoner, interpreter: MissionInterpreter, *,
        registry, schema, evidence_by_id, enrichments, system_model, manifest,
    ) -> None:
        self.reasoner = reasoner
        self.interpreter = interpreter
        self.registry = registry
        self.schema = schema
        self.evidence_by_id = evidence_by_id
        self.enrichments = enrichments
        self.system_model = system_model
        self.manifest = manifest
        self.context_variant = reasoner.context_variant

    def reason(self, mission: str, _catalogue, **_kwargs):
        interpretation = self.interpreter.interpret(mission)
        capabilities = (
            interpretation.capabilities
            if interpretation.status == "valid" and interpretation.capabilities is not None
            else CapabilitySelections.defaults()
        )
        realization = realize_capabilities(capabilities, self.registry)
        orchestration = resolve_ros_orchestration(
            realization, self.registry, self.system_model, self.manifest,
        )
        if orchestration.status == "invalid":
            raise ValueError("task capability realization has unsatisfied ROS connections")
        catalogue = derive_parameter_catalogue(
            realization, self.schema, evidence_by_id=self.evidence_by_id,
            semantic_enrichments=self.enrichments,
        )
        return self.reasoner.reason(
            mission, catalogue,
            system_context=build_parameter_selection_system_context(
                orchestration.model_dump(mode="json"),
            ),
            wiring_bindings=self.manifest.get("wiring_bindings", {}),
        )


class _GroundTruthConditionedReasoner:
    """Build each intrinsic catalogue from the task's frozen active-component scope."""

    def __init__(
        self, reasoner: ParameterReasoner, *, active_components_by_mission,
        registry, schema, evidence_by_id, enrichments, system_model, manifest,
    ) -> None:
        self.reasoner = reasoner
        self.active_components_by_mission = active_components_by_mission
        self.registry = registry
        self.schema = schema
        self.evidence_by_id = evidence_by_id
        self.enrichments = enrichments
        self.system_model = system_model
        self.manifest = manifest
        self.context_variant = reasoner.context_variant

    def reason(self, mission: str, _catalogue, **_kwargs):
        active_components = self.active_components_by_mission.get(mission)
        if active_components is None:
            raise ValueError("parameter task has no frozen ground-truth active-component scope")
        realization = SystemRealization(components=[
            RealizedComponent(
                component_id=component_id, enabled=True, reason="selected_implementation",
            )
            for component_id in active_components
        ])
        orchestration = resolve_ros_orchestration(
            realization, self.registry, self.system_model, self.manifest,
        )
        catalogue = derive_parameter_catalogue(
            realization, self.schema, evidence_by_id=self.evidence_by_id,
            semantic_enrichments=self.enrichments,
        )
        return self.reasoner.reason(
            mission, catalogue,
            system_context=build_parameter_selection_system_context(
                orchestration.model_dump(mode="json"),
            ),
            wiring_bindings=self.manifest.get("wiring_bindings", {}),
        )


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
    parser.add_argument("--capability-prompt", type=Path,
                        default=Path("prompts/mission_interpretation.txt"))
    parser.add_argument("--value-prompt", type=Path,
                        default=Path("prompts/parameter_value_reasoning.txt"))
    parser.add_argument("--semantic-enrichment", type=Path, default=None)
    parser.add_argument("--context-variant", action="append", choices=VARIANTS)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--graph-hops", type=int, default=1)
    parser.add_argument("--include-no-change", action="store_true")
    parser.add_argument("--allow-pending-review", action="store_true")
    parser.add_argument(
        "--infer-capabilities", action="store_true",
        help=("Use the capability LLM instead of each task's frozen active_components. "
              "Not recommended for intrinsic parameter evaluation."),
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    load_environment(workspace / ".env")
    resolve = lambda path: path if path.is_absolute() else workspace / path
    dataset = resolve(args.dataset)
    output = resolve(args.output)
    selection_prompt = resolve(args.selection_prompt)
    capability_prompt = resolve(args.capability_prompt)
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
    realization = realize_capabilities(CapabilitySelections.defaults(), registry)
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
        tasks = load_parameter_reasoning_tasks(dataset)
        pending = [task.id for task in tasks if task.review_status != "human_verified"]
        if pending and not args.allow_pending_review:
            parser.error(
                f"{len(pending)} parameter tasks still require human review; "
                "review them or pass --allow-pending-review for a development-only run"
            )
    selection_backend = _LoggingBackend(OllamaBackend(
        host=args.host, model=args.model, response_model=ParameterSelectionInterpretation,
    ), "parameter_selection")
    value_backend = _LoggingBackend(OllamaBackend(
        host=args.host, model=args.model, response_model=ParameterValueInterpretation,
    ), "parameter_value")
    capability_backend = None
    capability_interpreter = None
    if args.infer_capabilities:
        capability_backend = _LoggingBackend(OllamaBackend(
            host=args.host, model=args.model, response_model=MissionInterpretation,
        ), "capability_interpretation")
        capability_interpreter = MissionInterpreter(
            capability_backend,
            system_prompt=build_interpretation_prompt(
                capability_prompt.read_text(encoding="utf-8"), registry,
            ),
            registry=registry,
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
        if args.infer_capabilities:
            assert capability_interpreter is not None
            conditioned_reasoner = _CapabilityConditionedReasoner(
                reasoner, capability_interpreter, registry=registry, schema=schema,
                evidence_by_id=evidence_by_parameter_id(evidence), enrichments=enrichments,
                system_model=system_model, manifest=manifest,
            )
        else:
            if dataset.name != "parameter_reasoning_tasks_v1.jsonl":
                parser.error("ground-truth component scope requires parameter_reasoning_tasks_v1.jsonl")
            conditioned_reasoner = _GroundTruthConditionedReasoner(
                reasoner,
                active_components_by_mission={
                    task.mission: task.expected.active_components for task in tasks
                },
                registry=registry, schema=schema,
                evidence_by_id=evidence_by_parameter_id(evidence), enrichments=enrichments,
                system_model=system_model, manifest=manifest,
            )
        report = evaluate_parameter_reasoning(
            dataset, conditioned_reasoner, catalogue,
            include_no_change=args.include_no_change,
            progress=_progress_logger(
                variant, output, selection_backend, value_backend, capability_backend,
            ),
        )
        write_parameter_evaluation_report(report, output / variant)
        summaries[variant] = report["metrics"]
    metadata = {
        "model": args.model, "context_variants": variants, "top_k": args.top_k,
        "graph_hops": args.graph_hops, "include_no_change": args.include_no_change,
        "capability_source": "llm" if args.infer_capabilities else "ground_truth",
        "hashes": {str(path.relative_to(workspace)): _digest(path) for path in (
            dataset, selection_prompt, value_prompt, system_path, schema_path,
            manifest_path, evidence_path,
            *([capability_prompt] if args.infer_capabilities else []),
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
