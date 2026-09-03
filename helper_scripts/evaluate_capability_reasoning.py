#!/usr/bin/env python3
"""Evaluate the capability-interpretation LLM independently of parameter reasoning."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ros_config_builder.mission import (
    CapabilitySelections,
    MissionInterpretation,
    MissionInterpreter,
    OllamaBackend,
    build_interpretation_prompt,
    load_capability_registry,
    validate_frozen_dataset,
)
from ros_config_builder.orchestrate import load_environment


class _RecordingBackend:
    """Retain the exact raw response while preserving the runtime interpreter path."""

    def __init__(self, backend: OllamaBackend) -> None:
        self.backend = backend
        self.raw_response: str | None = None

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        self.raw_response = self.backend(system_prompt, user_prompt)
        return self.raw_response


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
    raw = case.get("expected_capability_interpretation")
    if raw is None:
        legacy = case.get("expected", {})
        status = {
            "supported": "valid", "unsupported": "unsupported", "ambiguous": "needs_clarification",
        }.get(case.get("outcome", "supported"), "valid")
        raw = {"status": status, "capabilities": legacy.get("capabilities", {})}
    if not isinstance(raw, dict):
        raise ValueError("expected capability interpretation must be an object")
    status = raw.get("status")
    if status not in {"valid", "unsupported", "needs_clarification"}:
        raise ValueError(f"invalid expected capability status {status!r}")
    if status == "valid":
        capabilities = CapabilitySelections.model_validate(
            raw.get("capabilities", {})
        ).model_dump(mode="json")
    else:
        capabilities = {}
    return {
        "status": status,
        "capabilities": _sparse(capabilities),
    }


def _fraction(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    exact = sum(bool(item["exact"]) for item in records)
    status = sum(bool(item["status_correct"]) for item in records)
    errors = sum(item["error"] is not None for item in records)
    expected_fields = sum(len(item["expected_fields"]) for item in records)
    predicted_fields = sum(len(item["predicted_fields"]) for item in records)
    correct_fields = sum(len(item["correct_fields"]) for item in records)
    precision = _fraction(correct_fields, predicted_fields)
    recall = _fraction(correct_fields, expected_fields)
    f1 = None if precision is None or recall is None or precision + recall == 0 else (
        2 * precision * recall / (precision + recall)
    )
    by_seed: dict[str, Any] = {}
    for seed in sorted({str(item.get("source_seed") or "unseeded") for item in records}):
        members = [item for item in records if str(item.get("source_seed") or "unseeded") == seed]
        by_seed[seed] = {
            "cases": len(members),
            "exact_accuracy": _fraction(sum(bool(item["exact"]) for item in members), len(members)),
            "status_accuracy": _fraction(
                sum(bool(item["status_correct"]) for item in members), len(members),
            ),
        }
    return {
        "cases": len(records), "errors": errors,
        "status_accuracy": _fraction(status, len(records)),
        "exact_capability_accuracy": _fraction(exact, len(records)),
        "field_micro_precision": precision, "field_micro_recall": recall,
        "field_micro_f1": f1, "by_source_seed": by_seed,
    }


def _failure_text(record: dict[str, Any]) -> str:
    lines: list[str] = []
    if record["error"]:
        lines += ["Runtime or validation error:", str(record["error"]), ""]
    if not record["status_correct"]:
        lines += [
            "Status mismatch:",
            f"expected={record['expected']['status']}",
            f"predicted={record['predicted_status']}", "",
        ]
    if record["missed_fields"]:
        lines += ["Missed or incorrect expected fields:", json.dumps(
            record["missed_fields"], indent=2, sort_keys=True,
        ), ""]
    if record["extra_fields"]:
        lines += ["Unexpected fields:", json.dumps(
            record["extra_fields"], indent=2, sort_keys=True,
        ), ""]
    return "\n".join(lines).rstrip() + "\n"


def _write_failure(output: Path, record: dict[str, Any], system_prompt: str) -> None:
    directory = output / "failures" / record["id"]
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath("system_prompt.txt").write_text(system_prompt + "\n", encoding="utf-8")
    directory.joinpath("user_prompt.txt").write_text(record["mission"] + "\n", encoding="utf-8")
    directory.joinpath("expected_result.json").write_text(
        json.dumps(record["expected"], indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    directory.joinpath("model_response.txt").write_text(
        (record["raw_response"] or "No model response was produced.") + "\n", encoding="utf-8",
    )
    directory.joinpath("error.txt").write_text(_failure_text(record), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--dataset", type=Path, required=True,
                        help="Training or test JSONL dataset to evaluate.")
    parser.add_argument("--output", type=Path, default=Path("artifacts/capability_reasoning/intrinsic_v1"))
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--host", default=None)
    parser.add_argument("--prompt", type=Path, default=Path("prompts/mission_interpretation.txt"))
    parser.add_argument(
        "--capability-registry", type=Path,
        default=Path("configuration_templates/capability_registry.yaml"),
    )
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases.")
    parser.add_argument("--allow-unfrozen-dataset", action="store_true",
                        help="Development only: evaluate a dataset without frozen metadata.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = args.workspace.resolve()
    load_environment(workspace / ".env")
    resolve = lambda path: path if path.is_absolute() else workspace / path
    dataset, output, prompt_path = resolve(args.dataset), resolve(args.output), resolve(args.prompt)
    validate_frozen_dataset(dataset, allow_unfrozen=args.allow_unfrozen_dataset)
    registry_path = resolve(args.capability_registry)
    registry = load_capability_registry(registry_path)
    system_prompt = build_interpretation_prompt(prompt_path.read_text(encoding="utf-8"), registry)
    ollama = OllamaBackend(host=args.host, model=args.model, response_model=MissionInterpretation)
    backend = _RecordingBackend(ollama)
    interpreter = MissionInterpreter(backend, system_prompt=system_prompt, registry=registry)
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        cases = cases[:args.limit]
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        expected = _expected(case)
        print(f"[capability] case {index}/{len(cases)} {case['id']} started: {case['mission']}", flush=True)
        started = time.perf_counter()
        raw_response = None
        prediction = None
        predicted_status = None
        error = None
        try:
            backend.raw_response = None
            interpretation = interpreter.interpret(case["mission"])
            raw_response = backend.raw_response
            predicted_status = interpretation.status
            prediction = {
                "status": predicted_status,
                "capabilities": _sparse(interpretation.capabilities.model_dump(mode="json"))
                if interpretation.capabilities is not None else {},
            }
        except Exception as caught:
            error = f"{type(caught).__name__}: {caught}"
        expected_fields = _leaves(expected["capabilities"])
        predicted_fields = _leaves(prediction["capabilities"]) if prediction else {}
        correct_fields = sorted(
            path for path, value in expected_fields.items() if predicted_fields.get(path) == value
        )
        missed = [
            {"field": path, "expected": value, "predicted": predicted_fields.get(path)}
            for path, value in expected_fields.items() if predicted_fields.get(path) != value
        ]
        extra = [
            {"field": path, "expected": expected_fields.get(path), "predicted": value}
            for path, value in predicted_fields.items() if expected_fields.get(path) != value
        ]
        status_correct = predicted_status == expected["status"]
        exact = bool(prediction == expected)
        record = {
            "id": case["id"], "source_seed": case.get("source_seed"), "mission": case["mission"],
            "expected": expected, "prediction": prediction, "predicted_status": predicted_status,
            "raw_response": raw_response, "error": error,
            "latency_seconds": time.perf_counter() - started,
            "status_correct": status_correct, "exact": exact,
            "expected_fields": expected_fields, "predicted_fields": predicted_fields,
            "correct_fields": correct_fields, "missed_fields": missed, "extra_fields": extra,
        }
        records.append(record)
        if not exact:
            _write_failure(output, record, system_prompt)
        running_correct = sum(bool(item["exact"]) for item in records)
        running_errors = sum(item["error"] is not None for item in records)
        detail = f" error={error}" if error else ""
        print(
            f"[capability] case {index}/{len(cases)} {case['id']} finished: "
            f"exact={exact} running_correct={running_correct} running_errors={running_errors}{detail}",
            flush=True,
        )
    metrics = _metrics(records)
    output.joinpath("predictions.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8",
    )
    experiment = {
        "dataset": str(dataset), "model": args.model, "prompt": str(prompt_path),
        "hashes": {
            str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (dataset, prompt_path, registry_path)
        },
        "metrics": metrics,
    }
    output.joinpath("experiment.json").write_text(
        json.dumps(experiment, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    output.joinpath("system_prompt.txt").write_text(system_prompt + "\n", encoding="utf-8")
    print(json.dumps(experiment, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
