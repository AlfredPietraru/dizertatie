"""Step 4: integrate immutable source facts into a deployment system model."""

from __future__ import annotations

import ast
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from pydantic import Field

from ..schemas.ros import IRModel


SCHEMA_VERSION = "1.0"


class PackageMetadata(IRModel):
    name: str
    path: str
    version: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    executables: list[dict[str, str]] = Field(default_factory=list)


class SystemModel(IRModel):
    schema_version: str = SCHEMA_VERSION
    stage: str
    roots: list[str]
    packages: list[PackageMetadata]
    include_graph: list[dict[str, Any]]
    launch_argument_graph: list[dict[str, Any]]
    deployment_instances: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    unresolved_facts: list[dict[str, Any]]
    summary: dict[str, int]


def _id(kind: str, *parts: Any) -> str:
    return f"{kind}:{hashlib.sha1('|'.join(map(str, parts)).encode()).hexdigest()[:12]}"


def _literal(node: ast.AST | None, values: dict[str, Any]) -> Any:
    if node is None: return None
    if isinstance(node, ast.Constant): return node.value
    if isinstance(node, ast.Name): return values.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(item, values) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {str(_literal(k, values)): _literal(v, values) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, values), _literal(node.right, values)
        if isinstance(left, str) and isinstance(right, str): return left + right
    return None


def extract_package_metadata(workspace: str | Path, *, source_roots: Iterable[str | Path] = ("src",)) -> dict[str, Any]:
    """Parse package.xml and console_scripts without importing setup modules."""
    workspace = Path(workspace).resolve(); packages = []
    roots = [(workspace / root).resolve() if not Path(root).is_absolute() else Path(root).resolve()
             for root in source_roots]
    for manifest in sorted({p for root in roots if root.is_dir() for p in root.rglob("package.xml")}):
        try: xml = ET.parse(manifest).getroot()
        except (OSError, ET.ParseError): continue
        package_dir = manifest.parent; name = (xml.findtext("name") or package_dir.name).strip()
        dependencies = sorted({(item.text or "").strip() for tag in ("depend", "exec_depend", "build_depend")
                               for item in xml.findall(tag) if (item.text or "").strip()})
        executables: list[dict[str, str]] = []
        setup = package_dir / "setup.py"
        if setup.is_file():
            try:
                tree = ast.parse(setup.read_text(encoding="utf-8")); values: dict[str, Any] = {}
                for item in tree.body:
                    if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                        value = _literal(item.value, values)
                        if value is not None: values[item.targets[0].id] = value
                call = next((x for x in ast.walk(tree) if isinstance(x, ast.Call) and
                             isinstance(x.func, ast.Name) and x.func.id == "setup"), None)
                if call:
                    entry_node = next((kw.value for kw in call.keywords if kw.arg == "entry_points"), None)
                    entry_points = _literal(entry_node, values)
                    if isinstance(entry_points, dict):
                        for entry in entry_points.get("console_scripts", []):
                            if isinstance(entry, str) and "=" in entry:
                                executable, target = map(str.strip, entry.split("=", 1))
                                module, _, function = target.partition(":")
                                executables.append({"name": executable, "module": module, "function": function})
            except (OSError, UnicodeError, SyntaxError): pass
        packages.append({"name": name, "path": package_dir.relative_to(workspace).as_posix(),
                         "version": xml.findtext("version"), "dependencies": dependencies,
                         "executables": sorted(executables, key=lambda x: x["name"])})
    return {"schema_version": SCHEMA_VERSION, "stage": "package_metadata_extraction",
            "packages": [PackageMetadata.model_validate(x).model_dump(mode="json") for x in packages]}


def _effective(value: Any, arguments: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind == "launch_configuration": return arguments.get(str(value.get("name")), value.get("default"))
        if kind == "package_share": return value
        if kind in {"path", "launch_source"}: return _effective(value.get("value"), arguments)
        if kind in {"path_join"}: return [_effective(x, arguments) for x in value.get("parts", [])]
        if kind in {"if", "unless"}:
            selected = _effective(value.get("expression"), arguments)
            truth = str(selected).casefold() in {"1", "true", "yes", "on"}
            return truth if kind == "if" else not truth
        return {key: _effective(child, arguments) for key, child in value.items()}
    if isinstance(value, list): return [_effective(x, arguments) for x in value]
    return value


def _expr(field: dict[str, Any] | None, arguments: dict[str, Any]) -> Any:
    return _effective(field.get("resolved_value"), arguments) if isinstance(field, dict) else None


def _package_and_file(value: Any) -> tuple[str | None, str | None]:
    package = None; filename = None
    def visit(item: Any) -> None:
        nonlocal package, filename
        if isinstance(item, dict):
            if item.get("kind") == "package_share": package = item.get("package")
            for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
        elif isinstance(item, str) and item.endswith((".launch.py", ".yaml", ".yml")):
            filename = Path(item).name
    visit(value); return package, filename


def _yaml_references(value: Any) -> list[dict[str, Any]]:
    found = []
    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("kind") == "yaml_parameter" and isinstance(item.get("profile"), dict):
                found.append(item["profile"])
            elif item.get("kind") == "yaml_profile": found.append(item)
            for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    unique = {}
    for item in found: unique[json.dumps(item, sort_keys=True)] = item
    return list(unique.values())


def _normalize_ns(value: Any) -> str:
    if not isinstance(value, str) or not value.strip("/"): return ""
    return "/" + value.strip("/")


def _ros_name(source: str, namespace: str, node_name: str, remappings: list[Any]) -> tuple[str, dict[str, Any]]:
    if source.startswith("/"): expanded = source
    elif source.startswith("~/"): expanded = "/".join(x for x in (namespace, node_name, source[2:]) if x).replace("//", "/")
    else: expanded = "/".join(x for x in (namespace, source) if x).replace("//", "/")
    if not expanded.startswith("/"): expanded = "/" + expanded
    final = expanded; applied = None
    for pair in remappings if isinstance(remappings, list) else []:
        if isinstance(pair, list) and len(pair) == 2:
            old, new = pair
            old_expanded = _ros_name(str(old), namespace, node_name, [])[0]
            if expanded == old_expanded:
                final = _ros_name(str(new), namespace, node_name, [])[0]; applied = pair
    return final, {"source_name": source, "namespace_expanded": expanded,
                   "applied_remapping": applied, "effective_name": final}


def build_system_model(*, workspace: str | Path, step1: dict[str, Any], launch: dict[str, Any],
                       configuration: dict[str, Any], package_metadata: dict[str, Any] | None = None,
                       root_launch_files: Iterable[str], root_arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a deployment model without mutating any input IR."""
    workspace = Path(workspace).resolve(); roots = list(root_launch_files); root_arguments = root_arguments or {}
    metadata = package_metadata or extract_package_metadata(workspace)
    packages = {x["name"]: x for x in metadata["packages"]}
    launch_by_path = {x["file"]: x for x in launch["launch_files"]}
    launch_by_package_name: dict[tuple[str, str], str] = {}
    for path in launch_by_path:
        for package in packages.values():
            if path.startswith(package["path"] + "/"):
                launch_by_package_name[(package["name"], Path(path).name)] = path
    executable_map = {(p["name"], e["name"]): e for p in packages.values() for e in p["executables"]}
    node_by_module = {n["qualified_class"].rsplit(".", 1)[0]: n for n in step1["nodes"]}
    profiles_by_file: dict[str, list[dict[str, Any]]] = {}
    for profile in configuration["profiles"]: profiles_by_file.setdefault(profile["file"], []).append(profile)
    include_graph, argument_graph, instances, unresolved = [], [], [], []

    def visit(path: str, inherited: dict[str, Any], parent_condition: list[Any], stack: tuple[str, ...]) -> None:
        if path in stack:
            unresolved.append({"kind": "include_cycle", "path": path, "stack": list(stack)}); return
        record = launch_by_path.get(path)
        if not record:
            unresolved.append({"kind": "missing_launch_file", "path": path}); return
        arguments = dict(inherited)
        for declaration in record["arguments"]:
            name = _expr(declaration["name"], arguments)
            default = _expr(declaration["default_value"], arguments)
            if isinstance(name, str):
                effective = arguments.get(name, root_arguments.get(name, default)); arguments[name] = effective
                argument_graph.append({"launch_file": path, "argument": name, "declared_default": default,
                                       "inherited_value": inherited.get(name), "root_override": root_arguments.get(name),
                                       "effective_value": effective, "source": declaration["source"]})
        for launch_node in record["nodes"]:
            package, executable = _expr(launch_node["package"], arguments), _expr(launch_node["executable"], arguments)
            declared_name = _expr(launch_node["name"], arguments); declared_ns = _expr(launch_node["namespace"], arguments)
            condition = _expr(launch_node["condition"], arguments)
            conditions = parent_condition + ([condition] if condition is not None else [])
            mapping = executable_map.get((package, executable)); source_node = None
            if mapping:
                candidates = [node for module, node in node_by_module.items()
                              if module == mapping["module"] or module.endswith(mapping["module"])]
                source_node = candidates[0] if len(candidates) == 1 else None
            status = "resolved_local" if source_node else "external" if package not in packages or not mapping else "unresolved"
            node_name = declared_name or (source_node["node_name"]["resolved_value"] if source_node else executable)
            namespace = _normalize_ns(declared_ns); remappings = _expr(launch_node["remappings"], arguments) or []
            parameter_sources, effective_parameters = [], {}
            if source_node:
                for parameter in source_node["parameters"]:
                    name = parameter["name"]["resolved_value"]
                    if isinstance(name, str):
                        value = parameter["default"]["resolved_value"]
                        effective_parameters[name] = {"name": name, "effective_value": value,
                            "provenance": [{"source_type": "python_default", "value": value,
                                            "source": parameter["source"]}]}
            for order, source in enumerate(launch_node["parameters"]):
                value = _effective(source.get("value"), arguments); selected_profiles = []
                if source["kind"] == "inline" and isinstance(value, dict):
                    references = _yaml_references(value)
                    for reference in references:
                        pkg, filename = _package_and_file(reference.get("path"))
                        candidates = [file for file in profiles_by_file if Path(file).name == filename]
                        if pkg in packages: candidates = [file for file in candidates if file.startswith(packages[pkg]["path"])]
                        selected_profiles.extend(profile for file in candidates for profile in profiles_by_file[file]
                                                 if profile["node_selector"] == reference.get("selector"))
                    literal_values = {key: configured for key, configured in value.items()
                                      if not (isinstance(configured, dict) and configured.get("kind") == "yaml_parameter")}
                    if literal_values:
                        selected_profiles.append({"id": None, "node_selector": "<inline>",
                            "flattened_parameters": literal_values, "file": path})
                else:
                    pkg, filename = _package_and_file(value)
                    candidates = [file for file in profiles_by_file if Path(file).name == filename]
                    if pkg in packages: candidates = [file for file in candidates if file.startswith(packages[pkg]["path"])]
                    for file in candidates:
                        selected_profiles.extend(x for x in profiles_by_file[file]
                                                 if x["node_selector"] in {"/**", node_name, executable, f"/{node_name}"})
                parameter_sources.append({"order": order, "declared": source, "matched_profiles":
                                          [x.get("id") for x in selected_profiles]})
                for profile in selected_profiles:
                    for name, configured in profile["flattened_parameters"].items():
                        entry = effective_parameters.setdefault(name, {"name": name, "effective_value": None, "provenance": []})
                        entry["effective_value"] = configured
                        entry["provenance"].append({"source_type": "inline" if profile["node_selector"] == "<inline>" else "yaml",
                                                    "source": profile["file"], "profile_id": profile.get("id"),
                                                    "node_selector": profile.get("node_selector"),
                                                    "parameter_path": name, "value": configured, "order": order})
            instance_id = _id("instance", path, launch_node["id"], len(instances))
            interfaces = []
            if source_node:
                bindings = {relationship.get("to"): str(relationship.get("from", ""))[10:]
                            for relationship in source_node.get("relationships", [])
                            if relationship.get("kind") == "assigned_to" and
                            str(relationship.get("from", "")).startswith("parameter:")}
                for collection, role in (("publishers", "publisher"), ("subscriptions", "subscription"),
                                         ("services", "service_server"), ("clients", "service_client")):
                    for endpoint in source_node[collection]:
                        field = endpoint.get("topic") or endpoint.get("name")
                        raw = field.get("resolved_value") if isinstance(field, dict) else None
                        parameter_name = bindings.get(field.get("expression")) if isinstance(field, dict) else None
                        if parameter_name in effective_parameters:
                            configured_name = effective_parameters[parameter_name]["effective_value"]
                            if isinstance(configured_name, str): raw = configured_name
                        if isinstance(raw, str):
                            effective_name, transform = _ros_name(raw, namespace, str(node_name), remappings)
                            interfaces.append({"id": _id("deployed_interface", instance_id, endpoint["id"]),
                                "source_ref": endpoint["id"], "role": role, "effective_name": effective_name,
                                "name_transformation": transform,
                                "type": (endpoint.get("message_type") or endpoint.get("service_type") or {}).get("resolved")})
            instances.append({"instance_id": instance_id, "package": package, "executable": executable,
                "declared_node_name": declared_name, "effective_node_name": node_name,
                "declared_namespace": declared_ns, "effective_namespace": namespace,
                "source_node_ref": source_node["id"] if source_node else None, "resolution_status": status,
                "interface_status": "known" if source_node else "unknown", "launch_source": launch_node["source"],
                "conditions": conditions, "enabled": all(x is not False for x in conditions),
                "parameter_sources": parameter_sources,
                "effective_parameters": sorted(effective_parameters.values(), key=lambda x: x["name"]),
                "remappings": remappings, "interfaces": interfaces, "unresolved_facts": []})
            if source_node:
                instances[-1]["source_inventory"] = {
                    key: source_node[key] for key in (
                        "parameters", "publishers", "subscriptions", "services", "clients",
                        "actions", "timers", "qos_profiles", "tf_entities", "relationships"
                    )
                }
            else:
                instances[-1]["source_inventory"] = None
        for include in record["includes"]:
            source_value = _expr(include["source_reference"], arguments); package, filename = _package_and_file(source_value)
            child = launch_by_package_name.get((package, filename)) if package and filename else None
            overrides = _expr(include["launch_arguments"], arguments) or {}
            condition = _expr(include["condition"], arguments); child_conditions = parent_condition + ([condition] if condition is not None else [])
            include_graph.append({"source": path, "target": child, "package": package, "filename": filename,
                                  "arguments": overrides, "condition": condition, "source_location": include["source"]})
            if child: visit(child, {**arguments, **(overrides if isinstance(overrides, dict) else {})}, child_conditions, stack + (path,))
            else: unresolved.append({"kind": "unresolved_include", "launch_file": path,
                                     "package": package, "filename": filename, "source": include["source"]})

    for root in roots: visit(root, dict(root_arguments), [], ())
    edges = []
    endpoints = [item for instance in instances if instance["enabled"] for item in instance["interfaces"]]
    for publisher in (x for x in endpoints if x["role"] == "publisher"):
        for subscription in (x for x in endpoints if x["role"] == "subscription"):
            if (publisher["effective_name"], publisher["type"]) == (subscription["effective_name"], subscription["type"]):
                edges.append({"id": _id("edge", publisher["id"], subscription["id"]), "kind": "topic",
                              "name": publisher["effective_name"], "type": publisher["type"],
                              "source_interface": publisher["id"], "target_interface": subscription["id"],
                              "confidence": "confirmed"})
    payload = {"schema_version": SCHEMA_VERSION, "stage": "deployment_integration",
        "roots": roots, "packages": metadata["packages"], "include_graph": include_graph,
        "launch_argument_graph": argument_graph, "deployment_instances": instances, "edges": edges,
        "unresolved_facts": unresolved, "summary": {"roots": len(roots), "packages": len(packages),
        "includes": len(include_graph), "instances": len(instances),
        "local_instances": sum(x["resolution_status"] == "resolved_local" for x in instances),
        "external_instances": sum(x["resolution_status"] == "external" for x in instances),
        "confirmed_edges": len(edges), "unresolved_facts": len(unresolved)}}
    return SystemModel.model_validate(payload).model_dump(mode="json")


def write_system_model(model: dict[str, Any], output_directory: str | Path) -> Path:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True); path = output / "ros_system_model.json"
    path.write_text(SystemModel.model_validate(model).model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
