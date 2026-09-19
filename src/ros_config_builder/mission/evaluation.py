"""Evaluation harness for zero-shot and few-shot mission experiments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .inference import MissionInterpreter, extract_json_object
from .schema import (
    AntRobotCapabilityRegistry, CapabilitySelections, MissionInterpretation,
    build_template_configuration_plan, realize_capabilities, validate_template_plan,
)


FAILURE_CATEGORIES = (
    "model_error", "dataset_ambiguity", "schema_limitation", "prompt_limitation", "unsupported_mission",
)


def _sparse(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: cleaned for key, item in value.items()
                if (cleaned := _sparse(item)) not in ({}, [])}
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


def _fraction(correct: int, total: int) -> float | None:
    return correct / total if total else None


def _metric_summary(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(predictions)
    supported = [item for item in predictions if item["expected_outcome"] == "supported"]
    summary: dict[str, Any] = {"missions": total}
    for metric in ("json_valid", "schema_valid"):
        count = sum(item[metric] is True for item in predictions)
        summary[metric] = {"count": count, "total": total, "rate": _fraction(count, total)}
    for metric in ("mapping_accepted", "configuration_validation_accepted"):
        count = sum(item[metric] is True for item in supported)
        summary[metric] = {"count": count, "total": len(supported), "rate": _fraction(count, len(supported))}
    for metric in ("exact_mission_match", "end_to_end_semantic_correct"):
        count = sum(item[metric] is True for item in supported)
        summary[metric] = {"count": count, "total": len(supported), "rate": _fraction(count, len(supported))}
    field_correct = sum(item["field_comparison"]["correct"] for item in supported)
    field_total = sum(item["field_comparison"]["total"] for item in supported)
    summary["per_field_accuracy"] = {
        "correct": field_correct, "total": field_total, "rate": _fraction(field_correct, field_total),
    }
    explicit_correct = sum(item["explicit_choice_recall"]["correct"] for item in supported)
    explicit_total = sum(item["explicit_choice_recall"]["total"] for item in supported)
    summary["explicit_choice_recall"] = {
        "correct": explicit_correct, "total": explicit_total,
        "rate": _fraction(explicit_correct, explicit_total),
    }
    for group in ("capabilities", "odometry"):
        eligible = [item for item in supported if item["group_accuracy"][group] is not None]
        correct = sum(item["group_accuracy"][group] is True for item in eligible)
        summary[f"{group}_accuracy"] = {
            "count": correct, "total": len(eligible), "rate": _fraction(correct, len(eligible)),
        }
    unsupported = [item for item in predictions if item["expected_outcome"] == "unsupported"]
    rejected = sum(item["outcome_correct"] is True for item in unsupported)
    summary["supported_mission_success"] = {
        "count": sum(item["end_to_end_semantic_correct"] is True for item in supported),
        "total": len(supported),
        "rate": _fraction(sum(item["end_to_end_semantic_correct"] is True for item in supported), len(supported)),
    }
    summary["unsupported_request_rejection"] = {
        "count": rejected, "total": len(unsupported), "rate": _fraction(rejected, len(unsupported)),
    }
    ambiguous = [item for item in predictions if item["expected_outcome"] == "ambiguous"]
    clarified = sum(item["predicted_outcome"] == "ambiguous" for item in ambiguous)
    classifications = sum(item["predicted_outcome"] == item["expected_outcome"] for item in predictions)
    summary["outcome_classification_accuracy"] = {
        "count": classifications, "total": total, "rate": _fraction(classifications, total),
    }
    summary["unsupported_false_acceptance_rate"] = {
        "count": sum(item["predicted_outcome"] == "supported" for item in unsupported),
        "total": len(unsupported),
        "rate": _fraction(sum(item["predicted_outcome"] == "supported" for item in unsupported), len(unsupported)),
    }
    summary["ambiguous_clarification_recall"] = {
        "count": clarified, "total": len(ambiguous), "rate": _fraction(clarified, len(ambiguous)),
    }
    summary["ambiguous_execution_rate"] = {
        "count": sum(item["predicted_outcome"] == "supported" for item in ambiguous),
        "total": len(ambiguous),
        "rate": _fraction(sum(item["predicted_outcome"] == "supported" for item in ambiguous), len(ambiguous)),
    }
    for stratum in sorted({tag for item in predictions for tag in item["strata"]}):
        members = [item for item in predictions if stratum in item["strata"]]
        correct = sum(item["outcome_correct"] is True for item in members)
        summary.setdefault("strata", {})[stratum] = {
            "count": correct, "total": len(members), "rate": _fraction(correct, len(members)),
        }
    return summary


def evaluate_missions(
    dataset_path: str | Path,
    interpreter: MissionInterpreter,
    *,
    registry: AntRobotCapabilityRegistry,
    schema: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate each mission and retain evidence for every failure boundary."""
    if (schema is None) != (manifest is None):
        raise ValueError("schema and manifest must be supplied together")
    cases = [json.loads(line) for line in Path(dataset_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    predictions: list[dict[str, Any]] = []
    system_prompt = interpreter.system_prompt
    for case in cases:
        expected_outcome = case.get("outcome", "supported")
        if expected_outcome not in {"supported", "unsupported", "ambiguous"}:
            raise ValueError(f"{case['id']} has invalid outcome {expected_outcome!r}")
        expected = _sparse(CapabilitySelections.model_validate(
            case.get("expected", {}).get("capabilities", {})
        ).model_dump(mode="json")) if expected_outcome == "supported" else {}
        expected_fields = _leaves(expected)
        full_expected_configuration = case.get("expected_template_configuration", {})
        capability_configuration = {
            key: value for key, value in full_expected_configuration.items()
            if key.startswith("launch.")
        }
        record: dict[str, Any] = {
            "id": case["id"], "mission": case["mission"], "expected": expected,
            "expected_template_configuration": capability_configuration,
            "parameter_expectations_excluded": {
                key: value for key, value in full_expected_configuration.items()
                if key.startswith("nodes.")
            },
            "expected_outcome": expected_outcome, "predicted_outcome": None,
            "strata": case.get("strata", []), "outcome_correct": False,
            "raw_response": None, "parsed_json": None, "prediction": None,
            "json_valid": False, "schema_valid": False, "exact_mission_match": False,
            "mapping_accepted": False, "configuration_validation_accepted": None,
            "end_to_end_semantic_correct": False,
            "failure_stage": None, "failure_detail": None,
            "failure_category": None, "manual_review": None,
            "field_comparison": {"correct": 0, "total": len(expected_fields), "mismatches": []},
            "explicit_choice_recall": {"correct": 0, "total": len(expected_fields), "missed": []},
            "group_accuracy": {"capabilities": None, "odometry": None},
        }
        try:
            record["raw_response"] = interpreter.backend(system_prompt, case["mission"])
        except Exception as error:
            record.update(failure_stage="model_request", failure_detail=str(error), failure_category="model_error")
            record["outcome_correct"] = False
            predictions.append(record); continue
        try:
            parsed = extract_json_object(record["raw_response"])
            record.update(parsed_json=parsed, json_valid=True)
        except ValueError as error:
            record.update(failure_stage="invalid_json", failure_detail=str(error), failure_category="model_error")
            record["outcome_correct"] = False
            predictions.append(record); continue
        try:
            interpretation = MissionInterpretation.model_validate(parsed)
            record["interpretation"] = _sparse(interpretation.model_dump(mode="json"))
            record["predicted_outcome"] = {
                "valid": "supported", "unsupported": "unsupported",
                "needs_clarification": "ambiguous",
            }[interpretation.status]
            record["schema_valid"] = True
            if interpretation.status != "valid":
                record["outcome_correct"] = record["predicted_outcome"] == expected_outcome
                if not record["outcome_correct"]:
                    record.update(failure_stage="outcome_mismatch", failure_category="model_error",
                                  failure_detail=f"expected {expected_outcome}, got {record['predicted_outcome']}")
                predictions.append(record); continue
            predicted_model = interpretation.capabilities
            assert predicted_model is not None
            predicted = _sparse(predicted_model.model_dump(mode="json"))
            record.update(prediction=predicted, schema_valid=True)
        except ValidationError as error:
            record.update(failure_stage="invalid_schema", failure_detail=str(error), failure_category="model_error")
            record["outcome_correct"] = False
            predictions.append(record); continue

        predicted_fields = _leaves(predicted)
        paths = sorted(set(expected_fields) | set(predicted_fields))
        mismatches = [{"field": path, "expected": expected_fields.get(path),
                       "predicted": predicted_fields.get(path)}
                      for path in paths if expected_fields.get(path) != predicted_fields.get(path)]
        record["field_comparison"] = {"correct": len(paths) - len(mismatches),
                                      "total": len(paths), "mismatches": mismatches}
        missed = [{"field": path, "expected": value, "predicted": predicted_fields.get(path)}
                  for path, value in expected_fields.items() if predicted_fields.get(path) != value]
        record["explicit_choice_recall"] = {
            "correct": len(expected_fields) - len(missed), "total": len(expected_fields), "missed": missed,
        }
        record["exact_mission_match"] = predicted == expected
        for group, prefix in (("capabilities", ""), ("odometry", "odometry")):
            relevant = [path for path in paths if not prefix or path == prefix or path.startswith(f"{prefix}.")]
            if group == "capabilities":
                relevant = [path for path in relevant if not path.startswith("odometry")]
            if relevant:
                record["group_accuracy"][group] = all(
                    expected_fields.get(path) == predicted_fields.get(path) for path in relevant)
        try:
            plan = build_template_configuration_plan(
                realize_capabilities(predicted_model, registry), {},
            )
            record["mapped_template_configuration"] = plan.user_values
            record["mapping_accepted"] = True
        except Exception as error:
            record.update(failure_stage="mapping_rejected", failure_detail=str(error), failure_category="schema_limitation")
            record["outcome_correct"] = False
            predictions.append(record); continue
        if schema is not None and manifest is not None:
            try:
                validate_template_plan(plan, schema, manifest)
                record["configuration_validation_accepted"] = True
            except ValueError as error:
                record.update(configuration_validation_accepted=False,
                              failure_stage="configuration_validation_rejected",
                              failure_detail=str(error), failure_category="schema_limitation")
                record["outcome_correct"] = False
                predictions.append(record); continue
        record["end_to_end_semantic_correct"] = expected_outcome == "supported" and (
            record["exact_mission_match"]
            and plan.user_values == capability_configuration
            and record["configuration_validation_accepted"] is not False
        )
        record["outcome_correct"] = record["end_to_end_semantic_correct"]
        if expected_outcome != "supported":
            record.update(failure_stage="unexpected_acceptance", failure_category="unsupported_mission",
                          failure_detail=f"expected {expected_outcome}, but the model returned a valid configuration",
                          manual_review="The structured mission schema has no explicit rejection or clarification outcome.")
        elif not record["end_to_end_semantic_correct"]:
            record.update(failure_stage="semantic_mismatch", failure_category="model_error",
                          manual_review="Classify as model error, dataset ambiguity, schema limitation, "
                                        "prompt limitation, or unsupported mission.")
        predictions.append(record)
    return {"dataset": str(dataset_path), "failure_categories": list(FAILURE_CATEGORIES),
            "metrics": _metric_summary(predictions), "predictions": predictions}


def write_evaluation_report(report: dict[str, Any], output_directory: str | Path) -> dict[str, Path]:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True)
    predictions_path = output / "predictions.jsonl"
    predictions_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n"
                                        for item in report["predictions"]), encoding="utf-8")
    evaluation = {key: value for key, value in report.items() if key != "predictions"}
    evaluation_path = output / "evaluation.json"
    evaluation_path.write_text(json.dumps(evaluation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Mission evaluation", "", f"Dataset: `{report['dataset']}`", "", "## Metrics", "",
             "| Metric | Result |", "|---|---:|"]
    for name, value in report["metrics"].items():
        if name == "missions": lines.append(f"| Missions | {value} |")
        elif name == "strata": continue
        else:
            rate = "N/A" if value["rate"] is None else f"{value['rate']:.1%}"
            lines.append(f"| {name.replace('_', ' ').title()} | {value.get('count', value.get('correct'))}/{value['total']} ({rate}) |")
    if report["metrics"].get("strata"):
        lines += ["", "## Results by stratum", "", "| Stratum | Result |", "|---|---:|"]
        for name, value in report["metrics"]["strata"].items():
            rate = "N/A" if value["rate"] is None else f"{value['rate']:.1%}"
            lines.append(f"| {name} | {value['count']}/{value['total']} ({rate}) |")
    failures = [item for item in report["predictions"] if not item["outcome_correct"]]
    lines += ["", "## Failures requiring review", ""]
    if not failures: lines.append("None.")
    for item in failures:
        lines += [f"### {item['id']}", "", f"- Stage: `{item['failure_stage']}`",
                  f"- Initial category: `{item['failure_category']}`",
                  f"- Detail: {item['failure_detail'] or item['field_comparison']['mismatches']}",
                  f"- Manual review: {item['manual_review'] or 'Not required for structural failure.'}", ""]
    markdown_path = output / "evaluation.md"
    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"predictions": predictions_path, "evaluation": evaluation_path, "markdown": markdown_path}
