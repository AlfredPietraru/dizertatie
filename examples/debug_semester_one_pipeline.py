"""Debugger-friendly walkthrough of Semester 1, from extraction to rendering.

Each ``step_*`` function is intentionally small: put a breakpoint on its return
statement, inspect the result, then use Step Into to enter the production code.
Generated diagnostics are written only below ``debug_output``.
"""

from __future__ import annotations

import argparse
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
    validate_system_model,
    validate_template_definition,
    write_template_definition,
)

SOURCE_ROOTS = ["src/antrobot_ros", "src/antrobot_description"]
ROOT_LAUNCH = ["src/antrobot_ros/launch/antrobot.launch.py"]


def write_snapshot(output: Path, number: str, name: str, value: Any) -> Path:
    path = output / f"{number}_{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    return path


def step_1_extract_python(workspace: Path) -> dict[str, Any]:
    result = extract_ros_node_ir(workspace, source_roots=SOURCE_ROOTS)
    return result


def step_2_extract_launch(workspace: Path) -> dict[str, Any]:
    result = extract_launch_files(workspace, source_roots=SOURCE_ROOTS)
    return result


def step_3_extract_configuration(workspace: Path) -> dict[str, Any]:
    result = extract_parameter_yaml(workspace, source_roots=SOURCE_ROOTS)
    return result


def step_3b_extract_packages(workspace: Path) -> dict[str, Any]:
    result = extract_package_metadata(workspace, source_roots=["src"])
    return result


def step_4_integrate(workspace: Path, facts: dict[str, Any]) -> dict[str, Any]:
    result = build_system_model(
        workspace=workspace,
        step1=facts["python"],
        launch=facts["launch"],
        configuration=facts["configuration"],
        package_metadata=facts["packages"],
        root_launch_files=ROOT_LAUNCH,
    )
    return result


def step_4b_validate(model: dict[str, Any]) -> dict[str, Any]:
    result = validate_system_model(model)
    return result


def step_5_build_semantic_schema(model: dict[str, Any]) -> dict[str, Any]:
    return build_template_configuration_schema(model)


def step_6_define_templates(schema: dict[str, Any], configuration: dict[str, Any], output: Path) -> dict[str, Any]:
    manifest, templates, baseline = build_template_definition(schema, configuration)
    coverage = validate_template_definition(manifest, templates, baseline, schema)
    paths = write_template_definition(manifest, templates, baseline, output / "templates")
    return {"manifest": manifest, "templates": templates,
            "baseline": baseline, "coverage": coverage, "paths": paths}


def step_7_render(workspace: Path, schema: dict[str, Any], model: dict[str, Any], output: Path) -> dict[str, Any]:
    result = render_configuration_bundle(
        workspace=workspace,
        template_directory=output / "templates",
        schema=schema,
        reference_model=model,
        output_directory=output / "rendered",
        debug_contexts=True,
        require_equivalence=True,
    )
    return result


def run(workspace: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    # Recreate products owned by this walkthrough so repeated runs are safe.
    for generated_directory in (output / "templates", output / "rendered"):
        if generated_directory.exists():
            shutil.rmtree(generated_directory)
    facts = {
        "python": step_1_extract_python(workspace),
        "launch": step_2_extract_launch(workspace),
        "configuration": step_3_extract_configuration(workspace),
        "packages": step_3b_extract_packages(workspace),
    }
    write_snapshot(output, "01", "python_extraction", facts["python"])
    write_snapshot(output, "02", "launch_extraction", facts["launch"])
    write_snapshot(output, "03", "configuration_extraction", facts["configuration"])
    write_snapshot(output, "03b", "package_extraction", facts["packages"])

    model = step_4_integrate(workspace, facts)
    validation = step_4b_validate(model)
    write_snapshot(output, "04", "system_model", model)
    write_snapshot(output, "04b", "system_validation", validation)

    schema = step_5_build_semantic_schema(model)
    write_snapshot(output, "05", "semantic_configuration_schema", schema)

    definition = step_6_define_templates(schema, facts["configuration"], output)
    write_snapshot(output, "06", "template_coverage", definition["coverage"])
    rendered = step_7_render(workspace, schema, model, output)

    summary = {
        "output_directory": str(output.resolve()),
        "python_nodes": len(facts["python"]["nodes"]),
        "launch_files": len(facts["launch"]["launch_files"]),
        "parameter_profiles": len(facts["configuration"]["profiles"]),
        "packages": len(facts["packages"]["packages"]),
        "deployment_instances": len(model["deployment_instances"]),
        "configuration_values": definition["coverage"]["configuration_values"],
        "rendered_files": len(rendered["render_records"]),
        "render_valid": rendered["validation"]["valid"],
        "semantically_equivalent": rendered["validation"]["equivalence"]["semantically_equivalent"],
    }
    write_snapshot(output, "07", "walkthrough_summary", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path,
                        default=Path("debug_output/semester_one_walkthrough"))
    args = parser.parse_args()
    print(json.dumps(run(args.workspace.resolve(), args.output), indent=2))


if __name__ == "__main__":
    main()
