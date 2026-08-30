"""Step 6 template bundle definition and coverage validation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from jinja2 import DictLoader, Environment, StrictUndefined, meta
from pydantic import ConfigDict

from ..schemas.ros import IRModel


class TemplateManifest(IRModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["3.0"] = "3.0"
    templates: list[dict[str, Any]]
    wiring_bindings: dict[str, dict[str, Any]]
    baseline_profile: str


def _all(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return schema["launch_arguments"] + [
        item for values in schema["node_parameters"].values() for item in values
    ]


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


def build_template_definition(
    schema: dict[str, Any], configuration_ir: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Return a manifest, templates, and baseline for every discovered value."""
    if not schema.get("complete"):
        raise ValueError("template definition requires a complete source-derived schema")
    variables = _all(schema); by_key = {item["template_key"]: item for item in variables}
    configuration_keys = set(schema["configuration_keys"])
    baseline_values = {}
    wiring_bindings: dict[str, dict[str, Any]] = {}
    yaml_targets: dict[tuple[str, str, str], str] = {}
    for key in schema.get("interface_parameter_keys", []):
        item = by_key[key]; binding = _binding_key(item)
        record = wiring_bindings.setdefault(binding, {
            "baseline_value": item.get("current_value"), "parameter_keys": [],
        })
        record["parameter_keys"].append(key)
    for key, item in by_key.items():
        if item["kind"] == "launch_argument":
            baseline_values[key] = item.get("current_value")
            root_target = next((target for target in item.get("targets", [])
                                if str(target.get("file", "")).endswith("/antrobot.launch.py")), None)
            if root_target is None:
                raise ValueError(
                    f"launch argument {key!r} is not forwarded by the root launch and cannot be rendered"
                )
        else:
            yaml_sources = [source for source in item.get("provenance", []) if source.get("source_type") == "yaml"]
            if yaml_sources:
                source = yaml_sources[-1]; file = source["source"]; selector = source.get("node_selector")
                parameter = source.get("parameter_path") or item["name"]
                baseline_values[key] = item.get("current_value")
                target = (file, str(selector), str(parameter))
                if target in yaml_targets:
                    raise ValueError(
                        f"configuration keys {yaml_targets[target]!r} and {key!r} share physical YAML target {target!r}"
                    )
                yaml_targets[target] = key
            else:
                raise ValueError(
                    f"ROS parameter {key!r} has no YAML target and cannot be rendered"
                )
    templates: dict[str, str] = {}
    launch_items = [by_key[key] for key in sorted(configuration_keys)
                    if by_key[key]["kind"] == "launch_argument"]
    launch_lines = ["from launch import LaunchDescription", "from launch.actions import IncludeLaunchDescription",
        "from launch.launch_description_sources import PythonLaunchDescriptionSource",
        "from launch.substitutions import PathJoinSubstitution",
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
            for (target_file, target_selector, parameter), key in yaml_targets.items():
                if target_file == file and target_selector == selector:
                    replacements[parameter] = f"{{{{ values[{json.dumps(key)}] | yaml_scalar }}}}"
            lines.append(f"{selector}:"); lines.append("  ros__parameters:")
            lines.extend(_render_yaml(profile["parameters"], 4, replacements))
        templates[f"config/{Path(file).name}.j2"] = "\n".join(lines) + "\n"
    template_records = []
    for path in sorted(templates):
        is_launch = path.startswith("launch/")
        record = {"path": path, "output": path[:-3],
                  "deployment_strategy": "wrapper_launch" if is_launch else "package_overlay"}
        if not is_launch:
            record["install_target"] = f"share/antrobot_ros/config/{Path(path[:-3]).name}"
        template_records.append(record)
    manifest = {"schema_version": "3.0", "templates": template_records,
        "wiring_bindings": wiring_bindings, "baseline_profile": "profiles/baseline.yaml"}
    baseline = {"profile": "baseline", "values": baseline_values}
    validation = validate_template_definition(manifest, templates, baseline, schema)
    if not validation["valid"]:
        raise ValueError(f"invalid template definition: {validation['errors']}")
    return TemplateManifest.model_validate(manifest).model_dump(mode="json"), templates, baseline


def _referenced_configuration_keys(templates: dict[str, str]) -> set[str]:
    references: set[str] = set()
    pattern = re.compile(r"values\[(\"(?:\\.|[^\"\\])*\")\]")
    for source in templates.values():
        references.update(json.loads(match) for match in pattern.findall(source))
    return references


def validate_template_definition(
    manifest: dict[str, Any], templates: dict[str, str], baseline: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    configuration_keys = set(schema["configuration_keys"])
    referenced_keys = _referenced_configuration_keys(templates)
    baseline_keys = set(baseline.get("values", {}))
    template_paths = {item.get("path") for item in manifest.get("templates", [])}
    output_paths = [item.get("output") for item in manifest.get("templates", [])]
    bound_interface_parameters = {
        key
        for binding in manifest["wiring_bindings"].values()
        for key in binding["parameter_keys"]
    }
    expected_interface_parameters = set(schema["interface_parameter_keys"])
    missing_interface_parameters = sorted(
        expected_interface_parameters - bound_interface_parameters
    )
    errors = []
    if configuration_keys != referenced_keys:
        errors.append(
            "template references differ from schema: "
            f"missing={sorted(configuration_keys - referenced_keys)}, "
            f"unknown={sorted(referenced_keys - configuration_keys)}"
        )
    if configuration_keys != baseline_keys:
        errors.append(
            "baseline keys differ from schema: "
            f"missing={sorted(configuration_keys - baseline_keys)}, "
            f"unknown={sorted(baseline_keys - configuration_keys)}"
        )
    if template_paths != set(templates):
        errors.append(
            f"manifest template paths differ from generated templates: "
            f"manifest={sorted(str(item) for item in template_paths)}, generated={sorted(templates)}"
        )
    if len(output_paths) != len(set(output_paths)):
        errors.append("manifest contains duplicate output routes")
    if missing_interface_parameters:
        errors.append(
            f"interface parameters without wiring metadata: {missing_interface_parameters}"
        )
    environment = Environment(loader=DictLoader(templates), undefined=StrictUndefined)
    environment.filters.update({"yaml_scalar": lambda value: value, "ros_launch_value": lambda value: value})
    declared_roots = {"values"}; unknown = {}
    for path, source in templates.items():
        parsed = environment.parse(source); names = meta.find_undeclared_variables(parsed) - declared_roots
        if names: unknown[path] = sorted(names)
        try: environment.get_template(path)
        except Exception as error: errors.append(f"template {path} does not compile: {error}")
    if unknown: errors.append(f"unknown Jinja roots: {unknown}")
    return {"valid": not errors, "errors": errors,
        "configuration_values": len(configuration_keys),
        "template_references": len(referenced_keys),
        "baseline_values": len(baseline_keys),
        "interface_parameters": len(expected_interface_parameters),
        "bound_interface_parameters": (
            len(expected_interface_parameters) - len(missing_interface_parameters)
        ),
        "templates": len(templates)}


def write_template_definition(manifest: dict[str, Any], templates: dict[str, str],
                              baseline: dict[str, Any], output_directory: str | Path) -> dict[str, Path]:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True); paths = {}
    for relative, source in templates.items():
        path = output / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(source, encoding="utf-8")
        paths[f"template:{relative}"] = path
    documents = {"manifest": ("manifest.json", manifest),
                 "baseline": ("profiles/baseline.yaml", baseline)}
    for name, (relative, payload) in documents.items():
        path = output / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"); paths[name] = path
    return paths
