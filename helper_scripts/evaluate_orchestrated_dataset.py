#!/usr/bin/env python3
"""Evaluate the LLM stages used by production orchestration against JSONL datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ros_config_builder.mission import (
    MissionInterpretation, ParameterSelectionInterpretation,
    build_parameter_selection_system_context,
    derive_parameter_catalogue, realize_capabilities,
    resolve_ros_orchestration, retrieve_parameters,
    validate_frozen_dataset,
)
from ros_config_builder.mission.inference import extract_json_object
from ros_config_builder.orchestrate import (
    DEFAULT_CONFIGURATION_PATH, ApplicationConfiguration, build_mission_application,
    load_application_configuration,
)


DEFAULT_OUTPUT = Path("artifacts/evaluation/orchestrated")
class _RecordingBackend:
    """Transparent backend wrapper retaining the exact prompt and raw LLM response."""

    def __init__(self, backend: Any, stage: str) -> None:
        self.backend, self.stage = backend, stage
        self.calls: list[dict[str, Any]] = []
        self.call_count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        self.call_count += 1
        call_number = self.call_count
        started = time.perf_counter()
        call = {"stage": self.stage, "system_prompt": system_prompt,
                "user_prompt": user_prompt, "raw_response": None, "error": None,
                "call": call_number, "latency_seconds": None}
        print(f"  [{self.stage}] LLM call {call_number} started", flush=True)
        try:
            call["raw_response"] = self.backend(system_prompt, user_prompt)
            elapsed = time.perf_counter() - started
            call["latency_seconds"] = elapsed
            print(
                f"  [{self.stage}] LLM call {call_number} completed in {elapsed:.1f}s",
                flush=True,
            )
            return call["raw_response"]
        except Exception as error:
            call["error"] = f"{type(error).__name__}: {error}"
            elapsed = time.perf_counter() - started
            call["latency_seconds"] = elapsed
            print(
                f"  [{self.stage}] LLM call {call_number} failed after {elapsed:.1f}s: "
                f"{call['error']}", flush=True,
            )
            raise
        finally:
            self.calls.append(call)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate datasets with the exact LLM prompts configured for orchestrate.py."
    )
    parser.add_argument("--configuration", type=Path, default=DEFAULT_CONFIGURATION_PATH)
    parser.add_argument(
        "--dataset", type=Path, action="append", required=True,
        help="JSONL dataset; repeat the option to compare multiple datasets.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--allow-unfrozen-dataset", action="store_true",
                        help="Development only: evaluate datasets without frozen metadata.")
    return parser


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(not isinstance(item, dict) for item in records):
        raise ValueError(f"{path} must contain one JSON object per line")
    return records


def _run_fingerprint(path: Path, configuration: ApplicationConfiguration) -> str:
    """Identify all inputs that must remain fixed when resuming a dataset run."""
    inputs: dict[str, Any] = {
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "configuration": configuration.model_dump(mode="json"),
    }
    for name in ("capability_prompt", "parameter_selection_prompt", "parameter_value_prompt"):
        resolved = configuration.resolve(getattr(configuration, name))
        assert resolved is not None
        inputs[f"{name}_sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _load_predictions_checkpoint(path: Path) -> list[dict[str, Any]]:
    """Load complete JSONL records and discard only a crash-truncated final line."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise ValueError(f"corrupt checkpoint record {index + 1} in {path}")
            path.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
                encoding="utf-8",
            )
    return records


def _append_checkpoint(path: Path, record: dict[str, Any]) -> None:
    """Durably append one completed prediction before continuing inference."""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _pretty_prompt_for_log(prompt: str) -> str:
    """Pretty-print embedded JSON in persisted prompts without changing runtime input."""
    decoder = json.JSONDecoder()
    for marker in (
        "Stable system context:\n", "Bounded selection context:\n",
        "Selected parameter context:\n", "Required JSON schema:\n",
        "AntRobot capability catalogue:\n",
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


def _failure_detail(record: dict[str, Any]) -> str:
    lines: list[str] = []
    if record.get("error"):
        lines += ["Runtime or validation error:", str(record["error"]), ""]
    if not record.get("outcome_correct"):
        lines += [
            "Outcome mismatch:",
            f"expected={record['expected']['status']}",
            f"predicted={record.get('predicted_status')}", "",
        ]
    comparisons = (
        ("Capability mismatch", record["expected"]["capabilities"],
         record.get("predicted_capabilities", {}), record.get("capability_exact_match")),
        ("Parameter-selection mismatch", sorted(record["expected"]["parameters"]),
         sorted(record.get("selected_parameter_ids", [])),
         record.get("parameter_selection_exact_match")),
        ("Parameter-value mismatch", record["expected"]["parameters"],
         record.get("predicted_parameters", {}), record.get("parameter_values_exact_match")),
    )
    for title, expected, predicted, exact in comparisons:
        if not exact:
            lines += [title + ":", "expected=" + json.dumps(expected, indent=2, sort_keys=True),
                      "predicted=" + json.dumps(predicted, indent=2, sort_keys=True), ""]
    return "\n".join(lines).rstrip() + "\n"


def _write_failure_artifacts(output: Path, record: dict[str, Any]) -> None:
    failure = output / "failures" / str(record["id"])
    failure.mkdir(parents=True, exist_ok=True)
    failure.joinpath("expected_result.json").write_text(
        json.dumps(record["expected"], indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    actual_result = {
        "status": record.get("predicted_status"),
        "capabilities": record.get("predicted_capabilities", {}),
        "selected_parameter_ids": record.get("selected_parameter_ids", []),
        "parameters": record.get("predicted_parameters", {}),
        "retrieval": record.get("retrieval"),
        "selection": record.get("selection"),
        "parameter_status": record.get("parameter_predicted_status"),
        "llm_outputs": record.get("llm_outputs", {}),
        "failure_stage": record.get("failure_stage"),
        "error": record.get("error"),
    }
    failure.joinpath("actual_result.json").write_text(
        json.dumps(actual_result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    failure.joinpath("error.txt").write_text(_failure_detail(record), encoding="utf-8")
    calls = record.get("llm_calls", [])
    failure.joinpath("llm_calls.json").write_text(
        json.dumps(calls, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    if not calls:
        failure.joinpath("system_prompt.txt").write_text(
            "No LLM call was reached.\n", encoding="utf-8",
        )
        failure.joinpath("user_prompt.txt").write_text(
            record["mission"] + "\n", encoding="utf-8",
        )
        failure.joinpath("model_response.txt").write_text(
            "No model response was produced.\n", encoding="utf-8",
        )
        return
    final_call = calls[-1]
    failure.joinpath("system_prompt.txt").write_text(
        _pretty_prompt_for_log(str(final_call["system_prompt"])) + "\n", encoding="utf-8",
    )
    failure.joinpath("user_prompt.txt").write_text(
        str(final_call["user_prompt"]) + "\n", encoding="utf-8",
    )
    failure.joinpath("model_response.txt").write_text(
        str(final_call.get("raw_response") or "No model response was produced.") + "\n",
        encoding="utf-8",
    )
    for index, call in enumerate(calls, start=1):
        stem = f"{index:02d}_{call['stage']}"
        failure.joinpath(stem + "_system_prompt.txt").write_text(
            _pretty_prompt_for_log(str(call["system_prompt"])) + "\n", encoding="utf-8",
        )
        failure.joinpath(stem + "_user_prompt.txt").write_text(
            str(call["user_prompt"]) + "\n", encoding="utf-8",
        )
        failure.joinpath(stem + "_model_response.txt").write_text(
            str(call.get("raw_response") or "No model response was produced.") + "\n",
            encoding="utf-8",
        )


def _sparse(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned for key, item in value.items()
            if (cleaned := _sparse(item)) not in ({}, [])
        }
    if isinstance(value, list):
        return [_sparse(item) for item in value]
    return value


def _leaves(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(_leaves(item, path))
        else:
            result[path] = item
    return result


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    if "expected_capability_interpretation" in case:
        interpretation = MissionInterpretation.model_validate(
            case["expected_capability_interpretation"]
        )
    else:
        outcome = case.get("outcome", "supported")
        status = {"supported": "valid", "unsupported": "unsupported",
                  "ambiguous": "needs_clarification"}[outcome]
        payload: dict[str, Any] = {"status": status}
        if status == "valid":
            payload["capabilities"] = case.get("expected", {}).get("capabilities", {})
        elif status == "unsupported":
            payload["reason"] = "gold unsupported outcome"
        else:
            payload.update(reason="gold ambiguous outcome", clarification_question="required")
        interpretation = MissionInterpretation.model_validate(payload)

    parameters = dict(case.get("expected_parameters", {}))
    supplied_configuration = dict(case.get("expected_template_configuration", {}))
    parameters.update({key: value for key, value in supplied_configuration.items()
                       if key.startswith("nodes.")})
    return {
        "status": interpretation.status,
        "capabilities": _sparse(
            interpretation.capabilities.model_dump(mode="json")
            if interpretation.capabilities is not None else {}
        ),
        "parameters": parameters,
    }


def _prf(expected: dict[str, Any], predicted: dict[str, Any]) -> tuple[int, int, int]:
    gold, guess = set(expected.items()), set(predicted.items())
    return len(gold & guess), len(guess - gold), len(gold - guess)


def _selection_scores(expected: set[str], predicted: set[str]) -> dict[str, Any]:
    true_positive = len(expected & predicted)
    precision = true_positive / len(predicted) if predicted else (1.0 if not expected else None)
    recall = true_positive / len(expected) if expected else (1.0 if not predicted else None)
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else 0.0)
    return {
        "true_positive": true_positive, "predicted": len(predicted), "expected": len(expected),
        "precision": precision, "recall": recall, "f1": f1,
        "exact": expected == predicted, "missed": sorted(expected - predicted),
        "over_selected": sorted(predicted - expected),
    }


def _rate(count: int, total: int) -> dict[str, int | float | None]:
    return {"count": count, "total": total, "rate": count / total if total else None}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    valid_gold = [item for item in records if item["expected"]["status"] == "valid"]
    metrics: dict[str, Any] = {
        "missions": total,
        "errors": sum(item["error"] is not None for item in records),
        "pipeline_completed": _rate(sum(item["error"] is None for item in records), total),
        "outcome_accuracy": _rate(sum(item["outcome_correct"] for item in records), total),
        "capability_exact_match": _rate(
            sum(item["capability_exact_match"] for item in valid_gold), len(valid_gold)
        ),
        "parameter_selection_exact_match": _rate(
            sum(item["parameter_selection_exact_match"] for item in valid_gold), len(valid_gold)
        ),
        "parameter_values_exact_match": _rate(
            sum(item["parameter_values_exact_match"] for item in valid_gold), len(valid_gold)
        ),
        "llm_end_to_end_exact_match": _rate(
            sum(item["llm_end_to_end_exact_match"] for item in valid_gold), len(valid_gold)
        ),
    }
    for family in ("capability", "parameter_selection", "parameter_value"):
        tp = sum(item[f"{family}_counts"]["tp"] for item in valid_gold)
        fp = sum(item[f"{family}_counts"]["fp"] for item in valid_gold)
        fn = sum(item[f"{family}_counts"]["fn"] for item in valid_gold)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        metrics[f"{family}_micro"] = {
            "tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": (2 * precision * recall / (precision + recall)
                   if precision is not None and recall is not None and precision + recall else None),
        }
    unsafe = [item for item in records if item["expected"]["status"] != "valid"]
    metrics["unsafe_execution_rate"] = _rate(
        sum(item["predicted_status"] == "valid" for item in unsafe), len(unsafe)
    )
    latencies = [item["latency_seconds"] for item in records]
    metrics["latency_seconds"] = {
        "mean": sum(latencies) / len(latencies) if latencies else None,
        "p50": _percentile(latencies, .5), "p95": _percentile(latencies, .95),
    }
    parameter_cases = [item for item in valid_gold if item["expected"]["parameters"]]
    retrieval_cases = [item for item in parameter_cases if item.get("retrieval") is not None]
    selection_tp = sum(item.get("selection", {}).get("true_positive", 0)
                       for item in parameter_cases)
    selection_predicted = sum(item.get("selection", {}).get("predicted", 0)
                              for item in parameter_cases)
    selection_expected = sum(item.get("selection", {}).get(
        "expected", len(item["expected"]["parameters"]),
    ) for item in parameter_cases)
    selection_precision = selection_tp / selection_predicted if selection_predicted else None
    selection_recall = selection_tp / selection_expected if selection_expected else None
    value_total = sum(len(item["expected"]["parameters"]) for item in parameter_cases)
    value_correct = sum(
        sum(item.get("predicted_parameters", {}).get(key) == value
            for key, value in item["expected"]["parameters"].items())
        for item in parameter_cases
    )
    metrics["parameter_reasoning"] = {
        "cases": len(valid_gold),
        "parameter_change_cases": len(parameter_cases),
        "errors": sum(item["error"] is not None for item in valid_gold),
        "retrieval_top_1_recall": (
            sum(item["retrieval"]["top_1_recall"] or 0 for item in retrieval_cases)
            / len(retrieval_cases) if retrieval_cases else None
        ),
        "retrieval_top_3_recall": (
            sum(item["retrieval"]["top_3_recall"] or 0 for item in retrieval_cases)
            / len(retrieval_cases) if retrieval_cases else None
        ),
        "retrieval_top_5_recall": (
            sum(item["retrieval"]["top_5_recall"] or 0 for item in retrieval_cases)
            / len(retrieval_cases) if retrieval_cases else None
        ),
        "retrieval_mrr": (
            sum(
                sum(item["retrieval"]["reciprocal_ranks"].values())
                / len(item["retrieval"]["reciprocal_ranks"])
                for item in retrieval_cases
            ) / len(retrieval_cases) if retrieval_cases else None
        ),
        "selection_exact_set_accuracy": (
            sum(bool(item.get("selection", {}).get("exact")) for item in parameter_cases)
            / len(parameter_cases) if parameter_cases else None
        ),
        "selection_micro_precision": selection_precision,
        "selection_micro_recall": selection_recall,
        "selection_micro_f1": (
            2 * selection_precision * selection_recall / (selection_precision + selection_recall)
            if selection_precision is not None and selection_recall is not None
            and selection_precision + selection_recall else 0.0
        ),
        "value_accuracy": value_correct / value_total if value_total else None,
        "complete_value_set_accuracy": (
            sum(item["parameter_values_exact_match"] for item in parameter_cases)
            / len(parameter_cases) if parameter_cases else None
        ),
        "outcome_accuracy": (
            sum(bool(item.get("parameter_outcome_correct")) for item in valid_gold)
            / len(valid_gold) if valid_gold else None
        ),
    }
    capability_tp = sum(item["capability_counts"]["tp"] for item in valid_gold)
    capability_fp = sum(item["capability_counts"]["fp"] for item in valid_gold)
    capability_fn = sum(item["capability_counts"]["fn"] for item in valid_gold)
    capability_precision = (
        capability_tp / (capability_tp + capability_fp)
        if capability_tp + capability_fp else None
    )
    capability_recall = (
        capability_tp / (capability_tp + capability_fn)
        if capability_tp + capability_fn else None
    )
    metrics["capability_reasoning"] = {
        "cases": total,
        "errors": sum(
            item["error"] is not None and item.get("failure_stage") == "capability_interpretation"
            for item in records
        ),
        "status_accuracy": (
            sum(bool(item.get("capability_status_correct")) for item in records) / total
            if total else None
        ),
        "exact_capability_accuracy": (
            sum(item["capability_exact_match"] for item in valid_gold) / len(valid_gold)
            if valid_gold else None
        ),
        "field_micro_precision": capability_precision,
        "field_micro_recall": capability_recall,
        "field_micro_f1": (
            2 * capability_precision * capability_recall / (capability_precision + capability_recall)
            if capability_precision is not None and capability_recall is not None
            and capability_precision + capability_recall else 0.0
        ),
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        if item.get("source_seed"):
            groups[item["source_seed"]].append(item)
    if groups:
        consistent = sum(
            len({json.dumps(item.get("llm_outputs"), sort_keys=True)
                 for item in members}) == 1
            for members in groups.values()
        )
        robust = sum(all(item["llm_end_to_end_exact_match"] for item in members)
                     for members in groups.values())
        metrics["paraphrase_prediction_consistency"] = _rate(consistent, len(groups))
        metrics["paraphrase_group_robustness"] = _rate(robust, len(groups))
    return metrics


def evaluate_dataset(path: Path, configuration: ApplicationConfiguration,
                     output: Path, limit: int | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    predictions_path = output / "predictions.jsonl"
    checkpoint_path = output / "checkpoint.json"
    fingerprint = _run_fingerprint(path, configuration)
    checkpoint = {
        "dataset": str(path), "fingerprint": fingerprint,
        "limit": limit, "status": "in_progress",
    }
    checkpoint_path.write_text(
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    application = build_mission_application(configuration)
    capability_backend = _RecordingBackend(
        application.interpreter.backend, "capability_interpretation"
    )
    selection_backend = _RecordingBackend(
        application.parameter_reasoner.selection_backend, "parameter_selection"
    )
    value_backend = _RecordingBackend(
        application.parameter_reasoner.value_backend, "parameter_value_reasoning"
    )
    application.interpreter.backend = capability_backend
    application.parameter_reasoner.selection_backend = selection_backend
    application.parameter_reasoner.value_backend = value_backend
    cases = _read_jsonl(path)
    if limit is not None:
        cases = cases[:limit]
    records = _load_predictions_checkpoint(predictions_path)
    case_by_id = {
        str(case.get("id", f"case-{index + 1:04d}")): case
        for index, case in enumerate(cases)
    }
    completed = {item["id"] for item in records}
    if len(completed) != len(records):
        raise ValueError(f"checkpoint contains duplicate case IDs: {predictions_path}")
    unknown = completed - set(case_by_id)
    if unknown:
        raise ValueError(f"checkpoint contains IDs absent from dataset: {sorted(unknown)}")
    for item in records:
        if item["mission"] != case_by_id[item["id"]]["mission"]:
            raise ValueError(f"checkpoint mission changed for {item['id']}")
    if records:
        print(f"[{path.name}] resuming with {len(records)}/{len(cases)} cases saved", flush=True)
        for saved_record in records:
            if not saved_record.get("llm_end_to_end_exact_match"):
                _write_failure_artifacts(output, saved_record)
    running_correct = sum(bool(item["llm_end_to_end_exact_match"]) for item in records)
    running_errors = sum(item.get("error") is not None for item in records)
    for index, case in enumerate(cases):
        case_id = str(case.get("id", f"case-{index + 1:04d}"))
        if case_id in completed:
            continue
        expected = _expected(case)
        print(
            f"[{path.name}] case {index + 1}/{len(cases)} {case_id} started: "
            f"{case['mission']}", flush=True,
        )
        call_offsets = [len(backend.calls) for backend in
                        (capability_backend, selection_backend, value_backend)]
        started = time.perf_counter()
        error = None
        failure_stage = "capability_interpretation"
        interpretation = None
        reasoning = None
        retrieval = None
        recovered_selection = None
        try:
            interpretation = application.interpreter.interpret(case["mission"])
            if interpretation.status == "valid":
                capabilities = interpretation.capabilities
                assert capabilities is not None
                realization = realize_capabilities(capabilities, application.capability_registry)
                orchestration = resolve_ros_orchestration(
                    realization, application.capability_registry,
                    application.reference_model, application.manifest,
                )
                if orchestration.status == "invalid":
                    raise ValueError("predicted capabilities have unsatisfied ROS connections")
                catalogue = derive_parameter_catalogue(
                    realization, application.schema,
                    evidence_by_id=application.parameter_evidence,
                    semantic_enrichments=application.semantic_enrichments,
                )
                retrieval = retrieve_parameters(
                    case["mission"], catalogue,
                    variant=application.parameter_reasoner.context_variant,
                    top_k=application.parameter_reasoner.top_k,
                    graph_hops=application.parameter_reasoner.graph_hops,
                    max_candidates=application.parameter_reasoner.max_candidates,
                    wiring_bindings=application.manifest.get("wiring_bindings", {}),
                )
                failure_stage = "parameter_reasoning"
                reasoning = application.parameter_reasoner.reason(
                    case["mission"], catalogue,
                    system_context=build_parameter_selection_system_context(
                        orchestration.model_dump(mode="json"),
                    ),
                    wiring_bindings=application.manifest.get("wiring_bindings", {}),
                )
        except Exception as caught:  # Every failed inference/validation remains an evaluation failure.
            error = f"{type(caught).__name__}: {caught}"
        latency = time.perf_counter() - started
        new_calls = [call for backend, offset in zip(
            (capability_backend, selection_backend, value_backend), call_offsets
        ) for call in backend.calls[offset:]]
        if reasoning is not None:
            recovered_selection = reasoning.selection
            retrieval = reasoning.retrieval
        else:
            selection_calls = [
                call for call in new_calls
                if call["stage"] == "parameter_selection" and call.get("raw_response")
            ]
            if selection_calls:
                try:
                    recovered_selection = ParameterSelectionInterpretation.model_validate(
                        extract_json_object(selection_calls[-1]["raw_response"])
                    )
                except ValueError:
                    recovered_selection = None
        if interpretation is None:
            predicted_status = "error"
        elif interpretation.status != "valid":
            predicted_status = interpretation.status
        elif reasoning is None:
            predicted_status = "error"
        else:
            predicted_status = (
                "valid" if reasoning.status in {"valid", "no_change"} else reasoning.status
            )
        predicted_capabilities = _sparse(
            interpretation.capabilities.model_dump(mode="json")
        ) if interpretation is not None and interpretation.capabilities is not None else {}
        selected = {
            item.parameter_id: True
            for item in (recovered_selection.selected_parameters if recovered_selection else [])
        }
        predicted_parameters = dict(reasoning.validated_values) if reasoning else {}
        capability_counts = _prf(_leaves(expected["capabilities"]), _leaves(predicted_capabilities))
        expected_selection = {key: True for key in expected["parameters"]}
        selection_counts = _prf(expected_selection, selected)
        value_counts = _prf(expected["parameters"], predicted_parameters)
        capability_exact = predicted_capabilities == expected["capabilities"]
        selection_exact = selected == expected_selection
        values_exact = error is None and predicted_parameters == expected["parameters"]
        expected_parameter_ids = set(expected["parameters"])
        ranked = [item.parameter_id for item in retrieval.candidates] if retrieval else []
        ranks = {identifier: rank for rank, identifier in enumerate(ranked, start=1)}
        parameter_expected_status = "valid" if expected_parameter_ids else "no_change"
        parameter_predicted_status = (
            reasoning.status if reasoning is not None
            else recovered_selection.status if recovered_selection is not None and error is None
            else "error"
        )
        record = {
            "id": case_id, "source_seed": case.get("source_seed"), "mission": case["mission"],
            "expected": expected, "predicted_status": predicted_status,
            "capability_predicted_status": (
                interpretation.status if interpretation is not None else "error"
            ),
            "capability_status_correct": (
                interpretation is not None and interpretation.status == expected["status"]
            ),
            "predicted_capabilities": predicted_capabilities,
            "selected_parameter_ids": sorted(selected),
            "predicted_parameters": predicted_parameters,
            "retrieval": {
                "ranked_parameter_ids": ranked,
                "top_1_recall": (sum(identifier in ranked[:1] for identifier in expected_parameter_ids)
                                 / len(expected_parameter_ids) if expected_parameter_ids else None),
                "top_3_recall": (sum(identifier in ranked[:3] for identifier in expected_parameter_ids)
                                 / len(expected_parameter_ids) if expected_parameter_ids else None),
                "top_5_recall": (sum(identifier in ranked[:5] for identifier in expected_parameter_ids)
                                 / len(expected_parameter_ids) if expected_parameter_ids else None),
                "reciprocal_ranks": {
                    identifier: (1 / ranks[identifier] if identifier in ranks else 0.0)
                    for identifier in sorted(expected_parameter_ids)
                },
            } if retrieval is not None else None,
            "selection": _selection_scores(expected_parameter_ids, set(selected)),
            "parameter_expected_status": parameter_expected_status,
            "parameter_predicted_status": parameter_predicted_status,
            "parameter_outcome_correct": parameter_predicted_status == parameter_expected_status,
            "outcome_correct": predicted_status == expected["status"],
            "capability_exact_match": capability_exact,
            "parameter_selection_exact_match": selection_exact,
            "parameter_values_exact_match": values_exact,
            "llm_end_to_end_exact_match": (
                predicted_status == expected["status"] and capability_exact
                and selection_exact and values_exact
            ),
            "llm_outputs": {
                "capability_interpretation": (
                    interpretation.model_dump(mode="json") if interpretation else None
                ),
                "parameter_selection": (
                    recovered_selection.model_dump(mode="json")
                    if recovered_selection is not None else None
                ),
                "parameter_value_interpretation": (
                    reasoning.value_interpretation.model_dump(mode="json")
                    if reasoning and reasoning.value_interpretation else None
                ),
            },
            "llm_calls": new_calls,
            "capability_counts": dict(zip(("tp", "fp", "fn"), capability_counts)),
            "parameter_selection_counts": dict(zip(("tp", "fp", "fn"), selection_counts)),
            "parameter_value_counts": dict(zip(("tp", "fp", "fn"), value_counts)),
            "latency_seconds": latency, "failure_stage": failure_stage if error else None,
            "error": error,
        }
        records.append(record)
        completed.add(case_id)
        _append_checkpoint(predictions_path, record)
        if not record["llm_end_to_end_exact_match"]:
            _write_failure_artifacts(output, record)
        running_correct += int(bool(record["llm_end_to_end_exact_match"]))
        running_errors += int(error is not None)
        detail = f" error={error}" if error else ""
        print(
            f"[{path.name}] case {index + 1}/{len(cases)} {case_id} finished: "
            f"end_to_end_correct={record['llm_end_to_end_exact_match']} "
            f"running_completed={len(completed)} running_correct={running_correct} "
            f"running_errors={running_errors}{detail}", flush=True,
        )
    records.sort(key=lambda item: list(case_by_id).index(item["id"]))
    predictions_path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8"
    )
    report = {"dataset": str(path), "metrics": _summarize(records)}
    (output / "evaluation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checkpoint["status"] = "complete"
    checkpoint["completed_cases"] = len(records)
    checkpoint_path.write_text(
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _metric_rate(value: Any) -> float | None:
    if isinstance(value, dict):
        if isinstance(value.get("rate"), (int, float)): return value["rate"]
        if isinstance(value.get("f1"), (int, float)): return value["f1"]
    return None


def _write_comparison(reports: list[dict[str, Any]], output: Path,
                      configuration_path: Path, configuration: ApplicationConfiguration,
                      datasets: list[Path]) -> None:
    names = [path.stem for path in datasets]
    metrics = sorted(set.intersection(*(set(report["metrics"]) for report in reports)))
    frozen_inputs = {}
    for name, configured_path in {
        "capability_prompt": configuration.capability_prompt,
        "parameter_selection_prompt": configuration.parameter_selection_prompt,
        "parameter_value_prompt": configuration.parameter_value_prompt,
        "system_model": configuration.system_model,
        "configuration_schema": configuration.configuration_schema,
        "parameter_evidence": configuration.parameter_evidence,
        "capability_registry": configuration.capability_registry,
    }.items():
        resolved = configuration.resolve(configured_path)
        if resolved is not None and resolved.is_file():
            frozen_inputs[name] = {
                "path": str(resolved),
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            }
    comparison = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "dataset-zero-shot; fixed orchestrator prompt (contains hand-written in-context examples)",
        "configuration": str(configuration_path),
        "configuration_sha256": hashlib.sha256(configuration_path.read_bytes()).hexdigest(),
        "model": configuration.ollama_model,
        "context_variant": configuration.context_variant,
        "retrieval_top_k": configuration.retrieval_top_k,
        "graph_hops": configuration.graph_hops,
        "frozen_inputs": frozen_inputs,
        "datasets": [{"name": name, "path": str(path),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                     for name, path in zip(names, datasets)],
        "metrics": {metric: {name: _metric_rate(report["metrics"][metric])
                             for name, report in zip(names, reports)}
                    for metric in metrics if any(_metric_rate(report["metrics"][metric]) is not None
                                                 for report in reports)},
    }
    (output / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = ["# LLM-output dataset comparison", "", f"Model: `{configuration.ollama_model}`",
             "", "| Metric | " + " | ".join(names) + " |",
             "|---|" + "---:|" * len(names)]
    for metric, values in comparison["metrics"].items():
        rendered = ["N/A" if values[name] is None else f"{values[name]:.1%}" for name in names]
        lines.append(f"| {metric.replace('_', ' ').title()} | " + " | ".join(rendered) + " |")
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = _parser().parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    configuration = load_application_configuration(args.configuration)
    if configuration.operation != "mission":
        raise SystemExit("evaluation requires operation='mission' in the configuration")
    datasets = args.dataset
    for dataset in datasets:
        validate_frozen_dataset(dataset, allow_unfrozen=args.allow_unfrozen_dataset)
    reports = []
    for path in datasets:
        reports.append(evaluate_dataset(
            path, configuration, args.output / path.stem, args.limit,
        ))
    args.output.mkdir(parents=True, exist_ok=True)
    _write_comparison(reports, args.output, args.configuration, configuration, datasets)
    print(json.dumps({"output": str(args.output), "datasets": [str(path) for path in datasets]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
