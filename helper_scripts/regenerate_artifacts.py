#!/usr/bin/env python3
"""Rebuild every canonical deterministic artifact from the current ROS workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ros_config_builder import (
    build_system_model,
    build_template_configuration_schema,
    build_template_definition,
    extract_launch_files,
    extract_package_metadata,
    extract_parameter_yaml,
    extract_ros_node_ir,
    render_configuration_bundle,
    validate_capability_registry,
    validate_scenario_delta,
    validate_system_model,
    write_scenario_validation,
    write_system_model_artifacts,
    write_template_configuration_schema,
    write_template_definition,
)
from ros_config_builder.mission import (
    build_parameter_evidence,
    build_parameter_evidence_artifact,
    load_capability_registry,
    write_parameter_evidence,
)


SOURCE_ROOTS = ("src/antrobot_ros", "src/antrobot_description")
ROOT_LAUNCH_FILES = ("src/antrobot_ros/launch/antrobot.launch.py",)
SCENARIOS = ("mapping", "mapping_navigation", "sensor_odometry")


def _inside(workspace: Path, path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(workspace)
    return resolved


def _replace_directory(workspace: Path, path: Path) -> Path:
    target = _inside(workspace, path)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    target.mkdir(parents=True)
    return target


def _remove_generated_template_files(workspace: Path, template_directory: Path) -> None:
    targets = (
        template_directory / "config",
        template_directory / "launch",
        template_directory / "manifest.json",
        template_directory / "profiles/baseline.yaml",
    )
    for target in targets:
        resolved = _inside(workspace, target)
        if resolved.is_dir():
            shutil.rmtree(resolved)
        elif resolved.exists():
            resolved.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_hashes(workspace: Path, paths: list[Path]) -> dict[str, str]:
    return {
        path.resolve().relative_to(workspace).as_posix(): _digest(path)
        for path in sorted(paths)
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--skip-scenario-equivalence",
        action="store_true",
        help="render scenarios without rebuilding generated system models",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    template_directory = workspace / "configuration_templates"
    system_directory = workspace / "artifacts/ros_system_model"
    schema_directory = workspace / "artifacts/template_configuration"
    semantic_directory = workspace / "artifacts/semantic_parameters"
    generated_directory = workspace / "artifacts/generated"

    configuration = extract_parameter_yaml(workspace, source_roots=SOURCE_ROOTS)
    model = build_system_model(
        workspace=workspace,
        step1=extract_ros_node_ir(workspace, source_roots=SOURCE_ROOTS),
        launch=extract_launch_files(workspace, source_roots=SOURCE_ROOTS),
        configuration=configuration,
        package_metadata=extract_package_metadata(workspace, source_roots=("src",)),
        root_launch_files=list(ROOT_LAUNCH_FILES),
    )
    validation = validate_system_model(model)
    if not validation["valid"]:
        raise ValueError(f"generated ROS system model is invalid: {validation['errors']}")

    _replace_directory(workspace, system_directory)
    system_paths = write_system_model_artifacts(model, system_directory)

    schema = build_template_configuration_schema(model)
    _replace_directory(workspace, schema_directory)
    schema_paths = write_template_configuration_schema(schema, schema_directory)

    manifest, templates, baseline = build_template_definition(schema, configuration)
    _remove_generated_template_files(workspace, template_directory)
    template_paths = write_template_definition(
        manifest, templates, baseline, template_directory,
    )

    registry = load_capability_registry(template_directory / "capability_registry.yaml")
    validate_capability_registry(registry, model, schema, manifest)

    evidence = build_parameter_evidence(workspace, schema, model)
    evidence_artifact = build_parameter_evidence_artifact(
        evidence, source_model=model, configuration_model=schema,
    )
    _replace_directory(workspace, semantic_directory)
    evidence_path = write_parameter_evidence(
        evidence_artifact, semantic_directory / "evidence.json",
    )

    _replace_directory(workspace, generated_directory)
    require_equivalence = not args.skip_scenario_equivalence
    baseline_result = render_configuration_bundle(
        workspace=workspace,
        template_directory=template_directory,
        schema=schema,
        reference_model=model,
        output_directory=generated_directory / "baseline",
        debug_contexts=True,
        require_equivalence=require_equivalence,
    )
    for scenario in SCENARIOS:
        profile = _read_json(template_directory / f"profiles/{scenario}.yaml")
        result = render_configuration_bundle(
            workspace=workspace,
            template_directory=template_directory,
            schema=schema,
            reference_model=model,
            output_directory=generated_directory / scenario,
            profile=profile,
            debug_contexts=True,
            require_equivalence=require_equivalence,
        )
        if require_equivalence:
            expectation = _read_json(
                template_directory / f"profiles/expectations/{scenario}.json"
            )
            delta = validate_scenario_delta(
                baseline_model=baseline_result["generated_model"],
                scenario_model=result["generated_model"],
                baseline_context=baseline_result["context"],
                scenario_context=result["context"],
                expectation=expectation,
            )
            if not delta["valid"]:
                raise ValueError(
                    f"generated scenario {scenario!r} violates its expectation: "
                    f"{delta['errors']}"
                )
            write_scenario_validation(delta, generated_directory / scenario)

    canonical_paths = [
        *system_paths.values(),
        *schema_paths.values(),
        *template_paths.values(),
        evidence_path,
        *[path for path in generated_directory.rglob("*") if path.is_file()],
    ]
    generation_record = {
        "schema_version": "1.0",
        "generator": "helper_scripts/regenerate_artifacts.py",
        "source_roots": list(SOURCE_ROOTS),
        "root_launch_files": list(ROOT_LAUNCH_FILES),
        "system_model_schema": model["schema_version"],
        "configuration_schema": schema["schema_version"],
        "renderer_manifest_schema": manifest["schema_version"],
        "counts": {
            **schema["summary"],
            "evidence_records": len(evidence),
            "generated_scenarios": len(SCENARIOS) + 1,
        },
        "scenario_equivalence": require_equivalence,
        "sha256": _relative_hashes(workspace, canonical_paths),
    }
    record_path = workspace / "artifacts/ARTIFACTS.json"
    record_path.write_text(
        json.dumps(generation_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "regenerated",
        "record": str(record_path),
        "counts": generation_record["counts"],
        "scenario_equivalence": require_equivalence,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
