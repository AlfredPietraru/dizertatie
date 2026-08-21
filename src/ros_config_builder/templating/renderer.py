"""Step 7: render, re-analyze, and semantically validate configurations."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..extraction.deployment import _load_yaml, extract_launch_files, extract_parameter_yaml
from ..extraction.python import extract_ros_node_ir
from ..integration.system_model import build_system_model, extract_package_metadata


def _all_variables(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = schema.get("role_groups", {})
    if groups: return {item["template_key"]: item for values in groups.values() for item in values}
    values = schema["launch_options"] + schema["structural_values"] + schema["derived_values"] + schema["internal_values"]
    values += [item for items in schema["node_parameters"].values() for item in items]
    return {item["template_key"]: item for item in values}


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try: value = json.loads(text)
    except json.JSONDecodeError: value = _load_yaml(text)
    if not isinstance(value, dict): raise ValueError(f"expected mapping in {path}")
    return value


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "boolean": return isinstance(value, bool)
    if expected == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string": return isinstance(value, str)
    if expected == "array": return isinstance(value, list)
    if expected == "object": return isinstance(value, dict)
    return True


def validate_public_values(values: dict[str, Any], schema: dict[str, Any], manifest: dict[str, Any],
                           *, layer: str) -> None:
    variables = _all_variables(schema); public = set(manifest["public_inputs"])
    errors = []
    for key, value in values.items():
        if key not in variables: errors.append(f"unknown key: {key}"); continue
        item = variables[key]
        if key not in public:
            reason = "derived" if item["classification"] == "derived" else "fixed/rejected"
            errors.append(f"{key} is not public ({reason})"); continue
        strategy = manifest["handling_strategies"][key]
        if strategy["kind"] == "source_default" and value != strategy.get("value"):
            errors.append(f"{key} cannot change until its child launch/source exposes an override")
        if not _matches_type(value, item["value_type"]):
            errors.append(f"{key} expects {item['value_type']}, got {type(value).__name__}")
        allowed = item.get("allowed_values")
        if allowed is not None and value not in allowed: errors.append(f"{key} must be one of {allowed}")
        bounds = item.get("range") or {}
        if "minimum" in bounds and value < bounds["minimum"]: errors.append(f"{key} is below minimum")
        if "maximum" in bounds and value > bounds["maximum"]: errors.append(f"{key} is above maximum")
    if errors: raise ValueError(f"Invalid {layer}: " + "; ".join(errors))


def resolve_render_context(schema: dict[str, Any], manifest: dict[str, Any], platform: dict[str, Any],
                           baseline: dict[str, Any], profile: dict[str, Any] | None = None,
                           user_values: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = profile or {"values": {}}; user_values = user_values or {}
    baseline_values = dict(baseline.get("values", {})); profile_values = dict(profile.get("values", {}))
    validate_public_values(profile_values, schema, manifest, layer="scenario profile")
    validate_public_values(user_values, schema, manifest, layer="user input")
    provenance = {key: [{"stage": "schema_baseline", "value": value}]
                  for key, value in baseline_values.items()}
    resolved = dict(baseline_values)
    for stage, values in (("scenario_profile", profile_values), ("explicit_user", user_values)):
        for key, value in values.items():
            resolved[key] = value; provenance.setdefault(key, []).append({"stage": stage, "value": value,
                "profile": profile.get("profile") if stage == "scenario_profile" else None})
    derived = {}
    for key, binding in manifest["derived_bindings"].items():
        source = binding["source"]
        if source["kind"] == "constant": value = source.get("value")
        elif source["kind"] == "reference":
            reference = source.get("key")
            value = resolved.get(reference, derived.get(reference))
            if value is None: raise ValueError(f"unresolved derived reference {reference}")
        else: raise ValueError(f"unsupported derived source kind: {source.get('kind')}")
        derived[key] = value
        for target in binding["targets"]: provenance.setdefault(target, []).append(
            {"stage": "derived_wiring", "binding": key, "value": value})
    platform_values = dict(platform.get("fixed_values", {}))
    for key, value in platform_values.items(): provenance.setdefault(key, []).append(
        {"stage": "platform", "profile": platform.get("platform"), "value": value})
    missing = sorted(set(manifest["public_inputs"]) - resolved.keys())
    if missing: raise ValueError(f"incomplete baseline context: {missing}")
    return {"schema_context": baseline_values, "profile_context": profile_values,
        "user_context": user_values, "derived_context": derived, "platform_context": platform_values,
        "resolved_context": resolved, "provenance": provenance}


def _yaml_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _ros_launch_value(value: Any) -> str:
    if isinstance(value, bool): value = "true" if value else "false"
    elif isinstance(value, (list, dict)): value = json.dumps(value, separators=(",", ":"))
    else: value = str(value)
    return repr(value)


def _template_variables(source: str) -> list[str]:
    return sorted(set(re.findall(r'(?:values|derived|platform)\["([^"]+)"\]', source)))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_templates(template_directory: str | Path, manifest: dict[str, Any], context: dict[str, Any],
                     output_directory: str | Path) -> list[dict[str, Any]]:
    template_directory, output = Path(template_directory), Path(output_directory)
    environment = Environment(loader=FileSystemLoader(template_directory), undefined=StrictUndefined,
                              autoescape=False, keep_trailing_newline=True)
    environment.filters.update({"yaml_scalar": _yaml_scalar, "ros_launch_value": _ros_launch_value})
    records = []
    for definition in manifest["templates"]:
        template_name = definition["path"]; source = (template_directory / template_name).read_text(encoding="utf-8")
        rendered = environment.get_template(template_name).render(
            values=context["resolved_context"], derived=context["derived_context"],
            platform=context["platform_context"])
        if definition["deployment_strategy"] == "package_overlay":
            relative = Path("overlay") / definition["install_target"]
        else: relative = Path(definition["output"])
        path = output / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        records.append({"template": template_name, "output": relative.as_posix(),
                        "deployment_strategy": definition["deployment_strategy"],
                        "variables_used": _template_variables(source), "sha256": _digest(path)})
    return records


def validate_rendered_files(bundle_directory: str | Path, render_records: list[dict[str, Any]]) -> dict[str, Any]:
    bundle = Path(bundle_directory); errors = []; checked = []
    for record in render_records:
        path = bundle / record["output"]
        try:
            if path.name.endswith(".launch.py"): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            elif path.suffix in {".yaml", ".yml"}:
                value = _load_yaml(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict): raise ValueError("top-level YAML is not a mapping")
                for selector, body in value.items():
                    if not isinstance(body, dict) or not isinstance(body.get("ros__parameters"), dict):
                        raise ValueError(f"{selector} does not contain ros__parameters mapping")
            checked.append(record["output"])
        except (SyntaxError, ValueError, OSError) as error: errors.append({"file": record["output"], "error": str(error)})
    return {"valid": not errors, "checked_files": checked, "errors": errors}


def _prepare_analysis_workspace(workspace: Path, bundle: Path, render_records: list[dict[str, Any]]) -> tuple[Path, str]:
    analysis = bundle / "analysis_workspace"; source = analysis / "src"
    for package in ("antrobot_ros", "antrobot_description"):
        shutil.copytree(workspace / "src" / package, source / package, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for record in render_records:
        generated = bundle / record["output"]
        if record["deployment_strategy"] == "package_overlay":
            target = source / "antrobot_ros" / "config" / generated.name
            shutil.copy2(generated, target)
    wrapper = next(bundle / record["output"] for record in render_records
                   if record["template"].endswith("antrobot_profile.launch.py.j2"))
    wrapper_target = source / "antrobot_ros" / "launch" / "generated_profile.launch.py"
    shutil.copy2(wrapper, wrapper_target)
    return analysis, "src/antrobot_ros/launch/generated_profile.launch.py"


def reanalyze_generated_bundle(workspace: str | Path, bundle_directory: str | Path,
                               render_records: list[dict[str, Any]]) -> dict[str, Any]:
    analysis, root = _prepare_analysis_workspace(Path(workspace).resolve(), Path(bundle_directory), render_records)
    roots = ["src/antrobot_ros", "src/antrobot_description"]
    step1 = extract_ros_node_ir(analysis, source_roots=roots)
    launch = extract_launch_files(analysis, source_roots=roots)
    configuration = extract_parameter_yaml(analysis, source_roots=roots)
    packages = extract_package_metadata(analysis, source_roots=["src"])
    return build_system_model(workspace=analysis, step1=step1, launch=launch, configuration=configuration,
                              package_metadata=packages, root_launch_files=[root])


def _semantic_projection(model: dict[str, Any]) -> dict[str, Any]:
    instances = []
    for item in model["deployment_instances"]:
        if not item.get("enabled"): continue
        parameters = {value["name"]: value.get("effective_value") for value in item.get("effective_parameters", [])}
        endpoints = sorted((value["role"], value["effective_name"], value.get("type"))
                           for value in item.get("interfaces", []))
        instances.append({"package": item.get("package"), "executable": item.get("executable"),
            "node_name": item.get("effective_node_name"), "namespace": item.get("effective_namespace"),
            "enabled": True, "parameters": parameters, "remappings": item.get("remappings", []),
            "endpoints": endpoints})
    instances.sort(key=lambda x: (str(x["package"]), str(x["executable"]), str(x["node_name"])))
    edges = sorted((edge["kind"], edge.get("name"), edge.get("type")) for edge in model.get("edges", []))
    return {"instances": instances, "edges": edges}


def compare_system_models(reference: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    left, right = _semantic_projection(reference), _semantic_projection(generated)
    differences = []
    if left["instances"] != right["instances"]: differences.append({"area": "deployment", "severity": "error",
        "reference": left["instances"], "generated": right["instances"]})
    if left["edges"] != right["edges"]: differences.append({"area": "architecture", "severity": "error",
        "reference": left["edges"], "generated": right["edges"]})
    structural = {"reference_roots": reference.get("roots"), "generated_roots": generated.get("roots"),
                  "classification": "expected", "reason": "generated wrapper and copied analysis workspace"}
    return {"semantically_equivalent": not differences, "unexpected_differences": differences,
            "structural_diff": structural, "reference_projection": left, "generated_projection": right}


def render_configuration_bundle(*, workspace: str | Path, template_directory: str | Path,
                                schema: dict[str, Any], reference_model: dict[str, Any],
                                output_directory: str | Path, profile: dict[str, Any] | None = None,
                                user_values: dict[str, Any] | None = None,
                                debug_contexts: bool = False,
                                require_equivalence: bool = True) -> dict[str, Any]:
    template_directory, output = Path(template_directory), Path(output_directory)
    manifest = _read(template_directory / "manifest.json")
    platform = _read(template_directory / manifest["platform_profile"])
    baseline = _read(template_directory / manifest["baseline_profile"])
    context = resolve_render_context(schema, manifest, platform, baseline, profile, user_values)
    output.mkdir(parents=True, exist_ok=True)
    records = render_templates(template_directory, manifest, context, output)
    static_validation = validate_rendered_files(output, records)
    if not static_validation["valid"]: raise ValueError(f"rendered artifact validation failed: {static_validation['errors']}")
    generated_model = reanalyze_generated_bundle(workspace, output, records)
    equivalence = compare_system_models(reference_model, generated_model)
    resolved = {"values": context["resolved_context"], "derived": context["derived_context"],
                "platform": context["platform_context"], "provenance": context["provenance"]}
    (output / "resolved_context.json").write_text(json.dumps(resolved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "render_manifest.json").write_text(json.dumps({"files": records}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validation = {"static": static_validation, "equivalence": equivalence,
                  "equivalence_required": require_equivalence,
                  "valid": static_validation["valid"] and
                  (equivalence["semantically_equivalent"] or not require_equivalence)}
    (output / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "generated_system_model.json").write_text(json.dumps(generated_model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if debug_contexts:
        (output / "context_stages.json").write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"output_directory": output, "render_records": records, "context": resolved,
            "generated_model": generated_model, "validation": validation}
