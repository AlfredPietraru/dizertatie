"""Intrinsic evaluation for retrieval, parameter selection, and value reasoning."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .reasoning import ParameterReasoningResult
from .schema import ParameterCatalogue


class ParameterReasoningSystem(Protocol):
    context_variant: str

    def reason(
        self, mission: str, catalogue: ParameterCatalogue, *,
        system_context: dict[str, Any] | None = None,
        wiring_bindings: dict[str, dict[str, Any]] | None = None,
    ) -> ParameterReasoningResult: ...


def _rate(numerator: int | float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _expected_changes(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parameter_reasoning = (case.get("expected") or {}).get("parameter_reasoning")
    if isinstance(parameter_reasoning, dict):
        return {
            item["parameter_id"]: {
                "value": item["value"],
                "absolute_tolerance": item.get("absolute_tolerance", 1e-9),
            }
            for item in parameter_reasoning.get("changes", [])
        }
    return {
        key: {"value": value, "absolute_tolerance": 1e-9}
        for key, value in case.get("expected_template_configuration", {}).items()
        if key.startswith("nodes.")
    }


def _expected_status(case: dict[str, Any], changes: dict[str, Any]) -> str:
    outcome = case.get("outcome", "supported")
    if outcome == "unsupported":
        return "unsupported"
    if outcome == "ambiguous":
        return "needs_clarification"
    return "valid" if changes else "no_change"


def _is_parameter_case(case: dict[str, Any], *, include_no_change: bool) -> bool:
    if _expected_changes(case):
        return True
    if (case.get("expected") or {}).get("parameter_reasoning") is not None:
        return True
    strata = set(case.get("strata", []))
    if case.get("outcome") in {"unsupported", "ambiguous"} and strata & {
        "parameter-reasoning", "runtime-override", "invalid-value", "missing-value",
        "ambiguous-reference", "wheel-radius", "publish-frequency", "publish-rate",
        "planner-frequency",
    }:
        return True
    return include_no_change and case.get("outcome", "supported") == "supported"


def _value_equal(expected: Any, predicted: Any, tolerance: float) -> bool:
    if (isinstance(expected, (int, float)) and not isinstance(expected, bool)
            and isinstance(predicted, (int, float)) and not isinstance(predicted, bool)):
        return abs(float(expected) - float(predicted)) <= tolerance
    return expected == predicted


def _selection_scores(expected: set[str], predicted: set[str]) -> dict[str, Any]:
    true_positive = len(expected & predicted)
    precision = _rate(true_positive, len(predicted))
    recall = _rate(true_positive, len(expected))
    if precision is None and recall is None:
        f1 = 1.0
    elif not precision or not recall:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "true_positive": true_positive, "predicted": len(predicted), "expected": len(expected),
        "precision": 1.0 if precision is None and not expected else precision,
        "recall": 1.0 if recall is None and not predicted else recall,
        "f1": f1, "exact": expected == predicted,
        "missed": sorted(expected - predicted), "over_selected": sorted(predicted - expected),
    }


def evaluate_parameter_reasoning(
    dataset_path: str | Path,
    reasoner: ParameterReasoningSystem,
    catalogue: ParameterCatalogue,
    *,
    system_context: dict[str, Any] | None = None,
    wiring_bindings: dict[str, dict[str, Any]] | None = None,
    include_no_change: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Score each AI boundary independently; execution errors are always failures."""
    all_cases = [json.loads(line) for line in Path(dataset_path).read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    cases = [case for case in all_cases if _is_parameter_case(case, include_no_change=include_no_change)]
    records: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases, 1):
        expected = _expected_changes(case)
        expected_ids = set(expected)
        expected_status = _expected_status(case, expected)
        record: dict[str, Any] = {
            "id": case["id"], "mission": case["mission"], "strata": case.get("strata", []),
            "difficulty": case.get("difficulty"),
            "expected_status": expected_status, "expected_changes": expected,
            "predicted_status": "error", "error": None, "retrieval": None,
            "selection": _selection_scores(expected_ids, set()), "value_comparison": None,
            "outcome_correct": False, "end_to_end_correct": False,
        }
        if progress is not None:
            progress({
                "event": "case_started", "index": case_index,
                "total": len(cases), "id": case["id"], "mission": case["mission"],
                "expected_result": {"status": expected_status, "changes": expected},
            })
        try:
            result = reasoner.reason(
                case["mission"], catalogue, system_context=system_context,
                wiring_bindings=wiring_bindings,
            )
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            records.append(record)
            if progress is not None:
                progress({
                    "event": "case_finished", "index": case_index,
                    "total": len(cases), "id": case["id"], "status": "error",
                    "error": record["error"], "end_to_end_correct": False,
                    "record": record,
                })
            continue
        record["predicted_status"] = result.status
        ranked = [item.parameter_id for item in result.retrieval.candidates]
        ranks = {identifier: index for index, identifier in enumerate(ranked, 1)}
        record["retrieval"] = {
            "ranked_parameter_ids": ranked,
            "top_1_recall": _rate(sum(identifier in ranked[:1] for identifier in expected_ids), len(expected_ids)),
            "top_3_recall": _rate(sum(identifier in ranked[:3] for identifier in expected_ids), len(expected_ids)),
            "top_5_recall": _rate(sum(identifier in ranked[:5] for identifier in expected_ids), len(expected_ids)),
            "reciprocal_ranks": {
                identifier: (1 / ranks[identifier] if identifier in ranks else 0.0)
                for identifier in sorted(expected_ids)
            },
        }
        predicted_ids = {
            item.parameter_id for item in result.selection.selected_parameters
            if result.selection.status == "valid"
        }
        record["selection"] = _selection_scores(expected_ids, predicted_ids)
        predicted_values = result.validated_values
        comparisons = []
        for identifier, expectation in expected.items():
            predicted = predicted_values.get(identifier)
            correct = identifier in predicted_values and _value_equal(
                expectation["value"], predicted, expectation["absolute_tolerance"],
            )
            comparisons.append({
                "parameter_id": identifier, "expected": expectation["value"],
                "predicted": predicted, "absolute_tolerance": expectation["absolute_tolerance"],
                "correct": correct,
            })
        extra_values = sorted(set(predicted_values) - expected_ids)
        record["value_comparison"] = {
            "correct": sum(item["correct"] for item in comparisons),
            "total": len(comparisons), "items": comparisons, "extra_values": extra_values,
            "exact": all(item["correct"] for item in comparisons) and not extra_values,
        }
        record["outcome_correct"] = result.status == expected_status
        record["end_to_end_correct"] = (
            record["outcome_correct"]
            and record["selection"]["exact"]
            and record["value_comparison"]["exact"]
        )
        records.append(record)
        if progress is not None:
            progress({
                "event": "case_finished", "index": case_index,
                "total": len(cases), "id": case["id"], "status": result.status,
                "error": None, "end_to_end_correct": record["end_to_end_correct"],
                "record": record,
            })

    expected_parameter_records = [item for item in records if item["expected_changes"]]
    retrieval_records = [item for item in expected_parameter_records if item["retrieval"] is not None]
    selection_tp = sum(item["selection"]["true_positive"] for item in expected_parameter_records)
    selection_predicted = sum(item["selection"]["predicted"] for item in expected_parameter_records)
    selection_expected = sum(item["selection"]["expected"] for item in expected_parameter_records)
    precision = _rate(selection_tp, selection_predicted)
    recall = _rate(selection_tp, selection_expected)
    micro_f1 = (2 * precision * recall / (precision + recall)
                if precision is not None and recall is not None and precision + recall else 0.0)
    value_correct = sum(item["value_comparison"]["correct"] for item in expected_parameter_records
                        if item["value_comparison"] is not None)
    value_total = sum(item["value_comparison"]["total"] for item in expected_parameter_records
                      if item["value_comparison"] is not None)
    metrics = {
        "cases": len(records),
        "errors": sum(item["error"] is not None for item in records),
        "retrieval_top_1_recall": _rate(sum((item["retrieval"]["top_1_recall"] or 0)
                                             for item in retrieval_records), len(retrieval_records)),
        "retrieval_top_3_recall": _rate(sum((item["retrieval"]["top_3_recall"] or 0)
                                             for item in retrieval_records), len(retrieval_records)),
        "retrieval_top_5_recall": _rate(sum((item["retrieval"]["top_5_recall"] or 0)
                                             for item in retrieval_records), len(retrieval_records)),
        "retrieval_mrr": _rate(sum(
            sum(item["retrieval"]["reciprocal_ranks"].values())
            / len(item["retrieval"]["reciprocal_ranks"])
            for item in retrieval_records
        ), len(retrieval_records)),
        "selection_exact_set_accuracy": _rate(
            sum(item["selection"]["exact"] for item in expected_parameter_records),
            len(expected_parameter_records),
        ),
        "selection_micro_precision": precision,
        "selection_micro_recall": recall,
        "selection_micro_f1": micro_f1,
        "value_accuracy": _rate(value_correct, value_total),
        "complete_value_set_accuracy": _rate(
            sum(item["value_comparison"]["exact"] for item in expected_parameter_records
                if item["value_comparison"] is not None), len(expected_parameter_records),
        ),
        "outcome_accuracy": _rate(sum(item["outcome_correct"] for item in records), len(records)),
        "end_to_end_accuracy": _rate(sum(item["end_to_end_correct"] for item in records), len(records)),
        "unsafe_execution_rate": _rate(sum(
            item["predicted_status"] == "valid"
            for item in records if item["expected_status"] in {"unsupported", "needs_clarification"}
        ), sum(item["expected_status"] in {"unsupported", "needs_clarification"} for item in records)),
    }
    metrics["by_difficulty"] = {
        difficulty: {
            "cases": len(members),
            "outcome_accuracy": _rate(sum(item["outcome_correct"] for item in members), len(members)),
            "end_to_end_accuracy": _rate(sum(item["end_to_end_correct"] for item in members), len(members)),
        }
        for difficulty in sorted({str(item["difficulty"]) for item in records})
        if (members := [item for item in records if str(item["difficulty"]) == difficulty])
    }
    return {
        "dataset": str(dataset_path), "context_variant": reasoner.context_variant,
        "include_no_change": include_no_change, "metrics": metrics, "predictions": records,
    }


def write_parameter_evaluation_report(
    report: dict[str, Any], output_directory: str | Path,
) -> dict[str, Path]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    predictions = output / "parameter_predictions.jsonl"
    predictions.write_text("".join(json.dumps(item, sort_keys=True) + "\n"
                                   for item in report["predictions"]), encoding="utf-8")
    summary = output / "parameter_evaluation.json"
    summary.write_text(json.dumps({key: value for key, value in report.items() if key != "predictions"},
                                  indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = output / "parameter_evaluation.md"
    lines = ["# Parameter reasoning evaluation", "",
             f"Context variant: `{report['context_variant']}`", "", "| Metric | Value |", "|---|---:|"]
    for name, value in report["metrics"].items():
        if isinstance(value, dict):
            continue
        rendered = "N/A" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)
        lines.append(f"| {name.replace('_', ' ').title()} | {rendered} |")
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"predictions": predictions, "evaluation": summary, "report": markdown}
