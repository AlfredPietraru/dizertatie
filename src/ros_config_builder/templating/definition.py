"""Step 6 template bundle definition and coverage validation."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from jinja2 import DictLoader, Environment, StrictUndefined, meta
from pydantic import Field

from ..schemas.ros import IRModel


class TemplateManifest(IRModel):
    schema_version: str = "1.0"
    stage: str = "template_definition"
    template_interface_version: str
    templates: list[dict[str, Any]]
    variable_targets: dict[str, list[dict[str, Any]]]
    handling_strategies: dict[str, dict[str, Any]]
    derived_bindings: dict[str, dict[str, Any]]
    platform_profile: str
    baseline_profile: str
    public_inputs: list[str]
    rejected_inputs: list[str]
    coverage: dict[str, Any]


def _all(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if schema.get("role_groups"):
        unique = {item["template_key"]: item for values in schema["role_groups"].values() for item in values}
        return list(unique.values())
    return (schema["launch_options"] + schema["structural_values"] + schema["derived_values"] +
            schema["internal_values"] + [item for values in schema["node_parameters"].values() for item in values])


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_") or "value"


def _yaml_scalar(value: Any) -> str:
    if value is None: return "null"
    if value is True: return "true"
    if value is False: return "false"
    if isinstance(value, (int, float)): return repr(value)
    if isinstance(value, str): return json.dumps(value)
    return json.dumps(value)


def _render_yaml(value: Any, indent: int, replacements: dict[str, str], prefix: str = "") -> list[str]:
    lines = []
    if not isinstance(value, dict): return lines
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key); pad = " " * indent
        if path in replacements: lines.append(f"{pad}{key}: {replacements[path]}")
        elif isinstance(child, dict):
            lines.append(f"{pad}{key}:"); lines.extend(_render_yaml(child, indent + 2, replacements, path))
        else: lines.append(f"{pad}{key}: {_yaml_scalar(child)}")
    return lines


def _binding_key(item: dict[str, Any]) -> str:
    name = item["name"].casefold(); value = item.get("current_value")
    kind = "frame" if "frame" in name else "topic"
    return f"wiring.{_slug(str(value))}_{kind}"


def build_template_definition(schema: dict[str, Any], configuration_ir: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]]:
    """Return manifest, templates, platform profile, and baseline profile."""
    if not schema.get("frozen") or schema.get("summary", {}).get("review_required"):
        raise ValueError("Step 6 requires a frozen, fully reviewed template schema")
    variables = _all(schema); by_key = {item["template_key"]: item for item in variables}
    public = set(schema["frozen_inputs"]); rejected = {item["template_key"] for item in variables
                                                       if item["review_status"] == "rejected"}
    strategies: dict[str, dict[str, Any]] = {}; targets: dict[str, list[dict[str, Any]]] = {}
    platform_values, baseline_values = {}, {}
    derived: dict[str, dict[str, Any]] = {}
    yaml_targets: dict[tuple[str, str, str], tuple[str, str]] = {}
    for key, item in by_key.items():
        if key in rejected:
            strategies[key] = {"kind": "inaccessible", "reason": item["classification_reason"]}; targets[key] = []
            continue
        if item["classification"] == "derived":
            binding = _binding_key(item); record = derived.setdefault(binding, {
                "source": {"kind": "constant", "value": item.get("current_value")}, "targets": []})
            record["targets"].append(key); strategies[key] = {"kind": "derived_binding", "binding": binding}
            targets[key] = [{"kind": "derived", "binding": binding}]; continue
        if item.get("platform_policy", {}).get("antrobot") == "fixed":
            platform_values[key] = item.get("current_value"); strategies[key] = {"kind": "platform_profile"}
            targets[key] = [{"kind": "platform", "profile": "platform/antrobot.yaml"}]
        elif item["kind"] == "launch_argument":
            baseline_values[key] = item.get("current_value")
            root_target = next((target for target in item.get("targets", [])
                                if str(target.get("file", "")).endswith("/antrobot.launch.py")), None)
            if root_target:
                strategies[key] = {"kind": "launch_argument"}
                targets[key] = [{"kind": "template", "template": "launch/antrobot_profile.launch.py.j2",
                                 "path": item["name"]}]
            else:
                strategies[key] = {"kind": "source_default", "value": item.get("current_value"),
                                   "reason": "child launch argument is not forwarded by the root launch"}
                targets[key] = [{"kind": "source_default", "source": item.get("targets", [])}]
        else:
            yaml_sources = [source for source in item.get("provenance", []) if source.get("source_type") == "yaml"]
            if yaml_sources:
                source = yaml_sources[-1]; file = source["source"]; selector = source.get("node_selector")
                parameter = source.get("parameter_path") or item["name"]
                template = f"config/{Path(file).name}.j2"; baseline_values[key] = item.get("current_value")
                strategies[key] = {"kind": "generated_yaml", "template": template}
                targets[key] = [{"kind": "template", "template": template,
                                 "path": f"{selector}.ros__parameters.{parameter}", "source_file": file}]
                yaml_targets[(file, str(selector), str(parameter))] = (key, "values")
            else:
                strategies[key] = {"kind": "source_default", "value": item.get("current_value")}
                targets[key] = [{"kind": "source_default", "source": item.get("targets", [{}])[0].get("source_declaration")}]
                baseline_values[key] = item.get("current_value")
    # Derived and platform values that live in YAML replace their original source targets.
    for key, item in by_key.items():
        if item["classification"] != "derived" and key not in platform_values: continue
        yaml_sources = [source for source in item.get("provenance", []) if source.get("source_type") == "yaml"]
        if yaml_sources:
            source = yaml_sources[-1]; yaml_targets[(source["source"], str(source.get("node_selector")),
                str(source.get("parameter_path") or item["name"]))] = (strategies[key].get("binding", key),
                                                                        "derived" if item["classification"] == "derived" else "platform")
    templates: dict[str, str] = {}
    launch_items = [by_key[key] for key in sorted(public)
                    if strategies[key]["kind"] == "launch_argument"]
    launch_lines = ["from launch import LaunchDescription", "from launch.actions import IncludeLaunchDescription",
        "from launch.launch_description_sources import PythonLaunchDescriptionSource",
        "from launch_ros.substitutions import FindPackageShare", "import os", "", "def generate_launch_description():",
        "    source = os.path.join(FindPackageShare('antrobot_ros').find('antrobot_ros'), 'launch', 'antrobot.launch.py')",
        "    include = IncludeLaunchDescription(", "        PythonLaunchDescriptionSource(source),", "        launch_arguments={"]
    for item in launch_items:
        launch_lines.append(f"            '{item['name']}': {{{{ values[{json.dumps(item['template_key'])}] | ros_launch_value }}}},")
    launch_lines += ["        }.items(),", "    )", "    return LaunchDescription([include])", ""]
    templates["launch/antrobot_profile.launch.py.j2"] = "\n".join(launch_lines)
    profiles_by_file: dict[str, list[dict[str, Any]]] = {}
    for profile in configuration_ir["profiles"]: profiles_by_file.setdefault(profile["file"], []).append(profile)
    for file in sorted({key[0] for key in yaml_targets}):
        lines = []
        for profile in profiles_by_file.get(file, []):
            selector = profile["node_selector"]; replacements = {}
            for (target_file, target_selector, parameter), (key, source_kind) in yaml_targets.items():
                if target_file == file and target_selector == selector:
                    accessor = {"values": "values", "derived": "derived", "platform": "platform"}[source_kind]
                    replacements[parameter] = f"{{{{ {accessor}[{json.dumps(key)}] | yaml_scalar }}}}"
            lines.append(f"{selector}:"); lines.append("  ros__parameters:")
            lines.extend(_render_yaml(profile["parameters"], 4, replacements))
        templates[f"config/{Path(file).name}.j2"] = "\n".join(lines) + "\n"
    template_records = []
    for path in sorted(templates):
        is_launch = path.startswith("launch/")
        record = {"path": path, "output": path[:-3], "kind": "launch" if is_launch else "yaml",
                  "deployment_strategy": "wrapper_launch" if is_launch else "package_overlay"}
        if not is_launch:
            record["install_target"] = f"share/antrobot_ros/config/{Path(path[:-3]).name}"
        template_records.append(record)
    manifest = {"schema_version": "1.0", "stage": "template_definition",
        "template_interface_version": schema.get("policy_version") or "1.0", "templates": template_records,
        "variable_targets": targets, "handling_strategies": strategies, "derived_bindings": derived,
        "platform_profile": "platform/antrobot.yaml", "baseline_profile": "profiles/baseline.yaml",
        "public_inputs": sorted(public), "rejected_inputs": sorted(rejected), "coverage": {}}
    validation = validate_template_definition(manifest, templates, schema)
    manifest["coverage"] = validation
    return TemplateManifest.model_validate(manifest).model_dump(mode="json"), templates, {
        "schema_version": "1.0", "platform": "antrobot", "fixed_values": platform_values}, {
        "schema_version": "1.0", "profile": "baseline", "values": baseline_values}


def validate_template_definition(manifest: dict[str, Any], templates: dict[str, str], schema: dict[str, Any]) -> dict[str, Any]:
    public = set(schema["frozen_inputs"]); strategies = manifest["handling_strategies"]
    missing = sorted(public - strategies.keys()); untargeted = sorted(key for key in public if not manifest["variable_targets"].get(key))
    derived_targets = {target for binding in manifest["derived_bindings"].values() for target in binding["targets"]}
    expected_derived = set(schema["derived_bindings"]); missing_derived = sorted(expected_derived - derived_targets)
    errors = []
    if missing: errors.append(f"public inputs without strategy: {missing}")
    if untargeted: errors.append(f"public inputs without target: {untargeted}")
    if missing_derived: errors.append(f"derived values without binding: {missing_derived}")
    environment = Environment(loader=DictLoader(templates), undefined=StrictUndefined)
    environment.filters.update({"yaml_scalar": lambda value: value, "ros_launch_value": lambda value: value})
    declared_roots = {"values", "derived", "platform"}; unknown = {}
    for path, source in templates.items():
        parsed = environment.parse(source); names = meta.find_undeclared_variables(parsed) - declared_roots
        if names: unknown[path] = sorted(names)
        try: environment.get_template(path)
        except Exception as error: errors.append(f"template {path} does not compile: {error}")
    if unknown: errors.append(f"unknown Jinja roots: {unknown}")
    return {"valid": not errors, "errors": errors, "public_inputs": len(public),
        "strategies": len([key for key in public if key in strategies]), "targeted_inputs": len(public) - len(untargeted),
        "derived_values": len(expected_derived), "bound_derived_values": len(expected_derived) - len(missing_derived),
        "platform_fixed_values": sum(x.get("kind") == "platform_profile" for x in strategies.values()),
        "templates": len(templates)}


def write_template_definition(manifest: dict[str, Any], templates: dict[str, str], platform: dict[str, Any],
                              baseline: dict[str, Any], output_directory: str | Path) -> dict[str, Path]:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True); paths = {}
    for relative, source in templates.items():
        path = output / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(source, encoding="utf-8")
        paths[f"template:{relative}"] = path
    documents = {"manifest": ("manifest.json", manifest), "platform": ("platform/antrobot.yaml", platform),
                 "baseline": ("profiles/baseline.yaml", baseline)}
    for name, (relative, payload) in documents.items():
        path = output / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"); paths[name] = path
    return paths
