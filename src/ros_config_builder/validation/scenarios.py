"""Validate deliberate Semester 1 scenario changes against expected deltas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _enabled(model: dict[str, Any]) -> set[str]:
    return {str(item.get("executable")) for item in model["deployment_instances"] if item.get("enabled")}


def _parameters(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("executable")): {value["name"]: value.get("effective_value")
            for value in item.get("effective_parameters", [])}
            for item in model["deployment_instances"] if item.get("enabled")}


def _edges(model: dict[str, Any]) -> set[tuple[Any, ...]]:
    return {(item.get("kind"), item.get("name"), item.get("type")) for item in model.get("edges", [])}


def validate_scenario_delta(*, baseline_model: dict[str, Any], scenario_model: dict[str, Any],
                            baseline_context: dict[str, Any], scenario_context: dict[str, Any],
                            expectation: dict[str, Any]) -> dict[str, Any]:
    """Require exactly the declared configuration and deployment changes."""
    errors, observations = [], []
    baseline_values, scenario_values = baseline_context["values"], scenario_context["values"]
    actual_changes = {key: {"from": baseline_values.get(key), "to": scenario_values.get(key)}
                      for key in sorted(set(baseline_values) | set(scenario_values))
                      if baseline_values.get(key) != scenario_values.get(key)}
    expected_changes = expectation.get("changed_values", {})
    if set(actual_changes) != set(expected_changes):
        errors.append({"area": "configuration", "message": "changed keys differ",
                       "expected": sorted(expected_changes), "actual": sorted(actual_changes)})
    for key, expected in expected_changes.items():
        if scenario_values.get(key) != expected:
            errors.append({"area": "configuration", "key": key, "expected": expected,
                           "actual": scenario_values.get(key)})
    before, after = _enabled(baseline_model), _enabled(scenario_model)
    added, removed = after - before, before - after
    expected_added = set(expectation.get("enabled_executables", []))
    expected_removed = set(expectation.get("disabled_executables", []))
    if added != expected_added: errors.append({"area": "deployment", "message": "unexpected enabled delta",
                                               "expected": sorted(expected_added), "actual": sorted(added)})
    if removed != expected_removed: errors.append({"area": "deployment", "message": "unexpected disabled delta",
                                                   "expected": sorted(expected_removed), "actual": sorted(removed)})
    expected_parameters = expectation.get("parameter_changes", {})
    before_params, after_params = _parameters(baseline_model), _parameters(scenario_model)
    actual_parameter_changes = {}
    for executable in set(before_params) & set(after_params):
        for name in set(before_params[executable]) | set(after_params[executable]):
            if before_params[executable].get(name) != after_params[executable].get(name):
                actual_parameter_changes[f"{executable}.{name}"] = after_params[executable].get(name)
    if actual_parameter_changes != expected_parameters:
        errors.append({"area": "parameters", "expected": expected_parameters,
                       "actual": actual_parameter_changes})
    required_edges = {tuple(item) for item in expectation.get("required_edges", [])}
    missing_edges = required_edges - _edges(scenario_model)
    if missing_edges: errors.append({"area": "architecture", "message": "required edges missing",
                                     "missing": sorted(missing_edges)})
    observations.append({"enabled_before": sorted(before), "enabled_after": sorted(after),
                         "added": sorted(added), "removed": sorted(removed)})
    if expectation.get("known_limitations"):
        observations.append({"known_limitations": expectation["known_limitations"]})
    return {"valid": not errors, "scenario": expectation.get("scenario"), "errors": errors,
            "actual_configuration_changes": actual_changes, "observations": observations}


def write_scenario_validation(result: dict[str, Any], output_directory: str | Path) -> Path:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True)
    path = output / "scenario_delta_validation.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
