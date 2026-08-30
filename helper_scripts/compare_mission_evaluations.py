#!/usr/bin/env python3
"""Compare two compatible mission or parameter-reasoning evaluation runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _artifact(directory: Path) -> tuple[dict, dict[str, dict]]:
    parameter = directory / "parameter_evaluation.json"
    if parameter.is_file():
        evaluation_path, predictions_path = parameter, directory / "parameter_predictions.jsonl"
    else:
        evaluation_path, predictions_path = directory / "evaluation.json", directory / "predictions.jsonl"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    predictions = {item["id"]: item for item in
                   (json.loads(line) for line in predictions_path.read_text(encoding="utf-8").splitlines())}
    return evaluation, predictions


def _leaves(value: dict, prefix: str = "") -> dict[str, object]:
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict): result.update(_leaves(item, path))
        else: result[path] = item
    return result


def _explicit_recall(predictions: dict[str, dict]) -> dict[str, float | int]:
    correct = total = 0
    for item in predictions.values():
        expected, predicted = _leaves(item["expected"]), _leaves(item.get("prediction") or {})
        total += len(expected)
        correct += sum(predicted.get(path) == value for path, value in expected.items())
    return {"correct": correct, "total": total, "rate": correct / total if total else 1.0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline, before = _artifact(args.baseline)
    candidate, after = _artifact(args.candidate)
    if set(before) != set(after):
        raise ValueError(
            f"evaluation case sets differ: only baseline={sorted(set(before) - set(after))}, "
            f"only candidate={sorted(set(after) - set(before))}"
        )
    if baseline.get("dataset") != candidate.get("dataset"):
        raise ValueError("evaluation datasets differ")
    if "explicit_choice_recall" in baseline["metrics"] or "explicit_choice_recall" in candidate["metrics"]:
        baseline["metrics"].setdefault("explicit_choice_recall", _explicit_recall(before))
        candidate["metrics"].setdefault("explicit_choice_recall", _explicit_recall(after))
    metrics = {}
    for name in sorted(set(baseline["metrics"]) & set(candidate["metrics"])):
        left_value, right_value = baseline["metrics"][name], candidate["metrics"][name]
        left = left_value.get("rate") if isinstance(left_value, dict) else left_value
        right = right_value.get("rate") if isinstance(right_value, dict) else right_value
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            continue
        metrics[name] = {"baseline": left, "candidate": right, "delta": right - left}
    correctness_key = ("end_to_end_correct" if all("end_to_end_correct" in item for item in before.values())
                       else "end_to_end_semantic_correct")
    cases = [{"id": case_id,
              "baseline_correct": before[case_id][correctness_key],
              "candidate_correct": after[case_id][correctness_key]}
             for case_id in sorted(before)]
    report = {"baseline": str(args.baseline), "candidate": str(args.candidate),
              "metrics": metrics, "cases": cases}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Evaluation comparison", "", "| Metric | Baseline | Candidate | Delta |",
             "|---|---:|---:|---:|"]
    for name, values in metrics.items():
        lines.append(f"| {name.replace('_', ' ').title()} | {values['baseline']:.1%} | "
                     f"{values['candidate']:.1%} | {values['delta']:+.1%} |")
    lines += ["", "## Case transitions", "", "| Case | Baseline | Candidate |", "|---|---:|---:|"]
    for case in cases:
        lines.append(f"| {case['id']} | {'pass' if case['baseline_correct'] else 'fail'} | "
                     f"{'pass' if case['candidate_correct'] else 'fail'} |")
    (args.output / "comparison.md").write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
