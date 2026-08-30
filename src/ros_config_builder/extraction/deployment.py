"""Deterministic ROS 2 launch-file and parameter-YAML extraction.

These are Step 2 and Step 3.  They preserve substitutions and references but
do not instantiate a launch graph or apply configuration to source nodes.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ..schemas.deployment import ConfigurationPayload, LaunchPayload


IGNORED = {".git", "build", "install", "log", "__pycache__", ".venv", "venv"}


def _text(source: str, node: ast.AST | None) -> str | None:
    if node is None: return None
    return (ast.get_source_segment(source, node) or ast.unparse(node)).strip()


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call): return _name(node.func)
    return ""


def _loc(path: str, node: ast.AST) -> dict[str, Any]:
    return {"file": path, "line_start": getattr(node, "lineno", None),
            "column_start": getattr(node, "col_offset", None),
            "line_end": getattr(node, "end_lineno", None),
            "column_end": getattr(node, "end_col_offset", None)}


def _stable(kind: str, path: str, node: ast.AST | str) -> str:
    position = node if isinstance(node, str) else f"{node.lineno}:{node.col_offset}"
    return f"{kind}:{hashlib.sha1(f'{path}|{position}'.encode()).hexdigest()[:12]}"


class LaunchResolver:
    def __init__(self, source: str) -> None:
        self.source = source
        self.values: dict[str, Any] = {}

    def value(self, node: ast.AST | None) -> tuple[bool, Any]:
        if node is None: return True, None
        raw = _text(self.source, node)
        if raw in self.values: return True, self.values[raw]
        if isinstance(node, ast.Constant): return True, node.value
        if isinstance(node, ast.Name) and node.id in self.values: return True, self.values[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self.value(item) for item in node.elts]
            if all(ok for ok, _ in values): return True, [value for _, value in values]
        if isinstance(node, ast.Dict):
            keys, vals = [self.value(x) for x in node.keys], [self.value(x) for x in node.values]
            if all(ok for ok, _ in keys + vals):
                return True, {str(key): value for (_, key), (_, value) in zip(keys, vals)}
        if isinstance(node, ast.Subscript):
            owner_ok, owner = self.value(node.value); key_ok, key = self.value(node.slice)
            if owner_ok and key_ok and isinstance(owner, dict) and owner.get("kind") == "yaml_profile":
                return True, {"kind": "yaml_parameter", "profile": owner, "name": key}
        if isinstance(node, ast.Call):
            suffix = _name(node.func).split(".")[-1]
            args = [self.value(item)[1] for item in node.args]
            kws = {kw.arg: self.value(kw.value)[1] for kw in node.keywords if kw.arg}
            if suffix == "LaunchConfiguration":
                return True, {"kind": "launch_configuration", "name": args[0] if args else None,
                              "default": kws.get("default")}
            if suffix == "load_node_params":
                return True, {"kind": "yaml_profile", "path": args[0] if args else None,
                              "selector": args[1] if len(args) > 1 else None}
            if suffix in {"float", "int", "bool", "str"} and args:
                return True, args[0]
            if suffix in {"FindPackageShare", "get_package_share_directory"}:
                return True, {"kind": "package_share", "package": args[0] if args else None}
            if suffix == "find" and isinstance(node.func, ast.Attribute):
                owner_ok, owner = self.value(node.func.value)
                if owner_ok and isinstance(owner, dict) and owner.get("kind") == "package_share":
                    return True, owner
            if suffix == "get" and isinstance(node.func, ast.Attribute):
                owner_ok, owner = self.value(node.func.value)
                if owner_ok and isinstance(owner, dict) and owner.get("kind") == "yaml_profile" and args:
                    return True, {"kind": "yaml_parameter", "profile": owner, "name": args[0],
                                  "fallback": args[1] if len(args) > 1 else None}
            if suffix in {"PathJoinSubstitution", "PythonLaunchDescriptionSource"}:
                return True, {"kind": "path" if suffix == "PathJoinSubstitution" else "launch_source",
                              "value": args[0] if args else None}
            if suffix == "join" and all(value is not None for value in args):
                return True, {"kind": "path_join", "parts": args}
            if suffix in {"IfCondition", "UnlessCondition"}:
                return True, {"kind": "if" if suffix == "IfCondition" else "unless",
                              "expression": args[0] if args else None}
            if suffix == "items" and isinstance(node.func, ast.Attribute):
                return self.value(node.func.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.value(node.left), self.value(node.right)
            if left[0] and right[0]: return True, {"kind": "path_join", "parts": [left[1], right[1]]}
        return False, None

    def expression(self, node: ast.AST | None) -> dict[str, Any]:
        ok, value = self.value(node)
        return {"expression": _text(self.source, node), "resolved_value": value if ok else None,
                "resolved": ok, "resolution": {"status": "resolved" if ok else "unresolved",
                "method": "static_launch_evaluation" if ok else "symbolic",
                "reason": None if ok else "dynamic_launch_expression"}}


def _args(call: ast.Call, names: tuple[str, ...]) -> dict[str, ast.AST]:
    result = dict(zip(names, call.args))
    result.update({kw.arg: kw.value for kw in call.keywords if kw.arg})
    return result


def _assignment_target(tree: ast.AST, call: ast.Call, source: str) -> str | None:
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Assign) and parent.value is call and parent.targets:
            return _text(source, parent.targets[0])
    return None


def _parameter_sources(node: ast.AST | None, resolver: LaunchResolver) -> list[dict[str, Any]]:
    ok, value = resolver.value(node)
    entries = value if ok and isinstance(value, list) else [value] if ok else []
    result = []
    for entry in entries:
        kind = "inline" if isinstance(entry, dict) and "kind" not in entry else "file"
        result.append({"kind": kind, "value": entry})
    if not ok:
        result.append({"kind": "unresolved", "expression": _text(resolver.source, node)})
    return result


def extract_launch_files(workspace: str | Path, *, source_roots: Iterable[str | Path] = ("src",)) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    roots = [(workspace / root).resolve() if not Path(root).is_absolute() else Path(root).resolve()
             for root in source_roots]
    paths = sorted({path for root in roots if root.is_dir() for path in root.rglob("*.launch.py")
                    if not any(part in IGNORED for part in path.parts)})
    records, failures = [], []
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        try:
            source = path.read_text(encoding="utf-8"); tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as error:
            failures.append({"file": relative, "error": str(error)}); continue
        resolver = LaunchResolver(source)
        assignments = sorted((x for x in ast.walk(tree) if isinstance(x, (ast.Assign, ast.AnnAssign))),
                             key=lambda x: (x.lineno, x.col_offset))
        for _ in range(len(assignments) + 1):
            changed = False
            for item in assignments:
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                ok, value = resolver.value(item.value)
                if ok:
                    for target in targets:
                        name = _text(source, target)
                        if name and resolver.values.get(name) != value:
                            resolver.values[name] = value; changed = True
            if not changed: break
        record = {"id": _stable("launch", relative, relative), "file": relative,
                  "arguments": [], "nodes": [], "includes": [], "unresolved_expressions": []}
        calls = sorted((x for x in ast.walk(tree) if isinstance(x, ast.Call)), key=lambda x: (x.lineno, x.col_offset))
        for call in calls:
            suffix = _name(call.func).split(".")[-1]
            if suffix == "DeclareLaunchArgument":
                values = _args(call, ("name", "default_value", "description", "choices"))
                record["arguments"].append({"id": _stable("launch_argument", relative, call),
                    "variable": _assignment_target(tree, call, source),
                    **{key: resolver.expression(values.get(key)) for key in ("name", "default_value", "description", "choices")},
                    "source": _loc(relative, call)})
            elif suffix == "Node":
                values = _args(call, ("package", "executable", "name", "namespace"))
                node_record = {"id": _stable("launch_node", relative, call),
                    "variable": _assignment_target(tree, call, source),
                    **{key: resolver.expression(values.get(key)) for key in ("package", "executable", "name", "namespace")},
                    "parameters": _parameter_sources(values.get("parameters"), resolver),
                    "remappings": resolver.expression(values.get("remappings")),
                    "arguments": resolver.expression(values.get("arguments")),
                    "condition": resolver.expression(values.get("condition")),
                    "output": resolver.expression(values.get("output")), "source": _loc(relative, call)}
                record["nodes"].append(node_record)
            elif suffix == "IncludeLaunchDescription":
                values = _args(call, ("launch_description_source",))
                record["includes"].append({"id": _stable("launch_include", relative, call),
                    "variable": _assignment_target(tree, call, source),
                    "source_reference": resolver.expression(values.get("launch_description_source")),
                    "launch_arguments": resolver.expression(values.get("launch_arguments")),
                    "condition": resolver.expression(values.get("condition")), "source": _loc(relative, call)})
        for collection in ("arguments", "nodes", "includes"):
            for entity in record[collection]:
                for field, value in entity.items():
                    if isinstance(value, dict) and value.get("resolved") is False:
                        record["unresolved_expressions"].append({"owner_id": entity["id"], "field": field,
                                                               **value, "source": entity["source"]})
        records.append(record)
    summary = {"launch_files": len(records), "arguments": sum(len(x["arguments"]) for x in records),
               "nodes": sum(len(x["nodes"]) for x in records), "includes": sum(len(x["includes"]) for x in records),
               "unresolved_expressions": sum(len(x["unresolved_expressions"]) for x in records),
               "parse_failures": len(failures)}
    payload = {"stage": "ros_launch_static_extraction",
               "summary": summary, "parse_failures": failures, "launch_files": records}
    return LaunchPayload.model_validate(payload).model_dump(mode="json")


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result = {}
    for key, child in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict): result.update(_flatten(child, name))
        else: result[name] = child
    return result


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("!!float"): value = value[7:].strip()
    if not value: return {}
    if value.casefold() in {"true", "false"}: return value.casefold() == "true"
    if value.casefold() in {"null", "none", "~"}: return None
    try: return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        try: return float(value) if any(x in value.lower() for x in (".", "e")) else int(value)
        except ValueError: return value.strip("'\"")


def _strip_comment(line: str) -> str:
    quote = None
    for index, character in enumerate(line):
        if character in {"'", '"'}: quote = None if quote == character else character if quote is None else quote
        elif character == "#" and quote is None: return line[:index]
    return line


def _simple_yaml(text: str) -> dict[str, Any]:
    """Parse ROS' indentation-based mapping/list subset without PyYAML."""
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        content = _strip_comment(raw_line).rstrip()
        if content.strip():
            lines.append((len(content) - len(content.lstrip(" ")), content.strip()))

    def block(index: int, indent: int) -> tuple[Any, int]:
        is_list = lines[index][1].startswith("-")
        result: Any = [] if is_list else {}
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent: break
            if current_indent > indent:
                index += 1; continue
            if is_list:
                if not content.startswith("-"): break
                raw = content[1:].strip()
                if not raw and index + 1 < len(lines) and lines[index + 1][0] > indent:
                    value, index = block(index + 1, lines[index + 1][0]); result.append(value); continue
                if ":" in raw:
                    key, raw_value = raw.split(":", 1)
                    item: dict[str, Any] = {key.strip().strip("'\""): _yaml_scalar(raw_value)}
                    index += 1
                    if index < len(lines) and lines[index][0] > indent:
                        continuation, index = block(index, lines[index][0])
                        if isinstance(continuation, dict): item.update(continuation)
                    result.append(item); continue
                result.append(_yaml_scalar(raw)); index += 1; continue
            if content.startswith("-") or ":" not in content: break
            key, raw = content.split(":", 1); key = key.strip().strip("'\""); raw = raw.strip()
            if raw:
                result[key] = _yaml_scalar(raw); index += 1; continue
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = block(index + 1, lines[index + 1][0]); result[key] = value; continue
            result[key] = {}; index += 1
        return result, index

    return block(0, lines[0][0])[0] if lines else {}


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # Optional; the ROS mapping subset has a dependency-free fallback.
    except ImportError:
        return _simple_yaml(text)
    return yaml.safe_load(text)


def extract_parameter_yaml(workspace: str | Path, *, source_roots: Iterable[str | Path] = ("src",)) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    roots = [(workspace / root).resolve() if not Path(root).is_absolute() else Path(root).resolve()
             for root in source_roots]
    paths = sorted({path for root in roots if root.is_dir() for path in root.rglob("*.yaml")
                    if not any(part in IGNORED for part in path.parts)})
    profiles, failures = [], []
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        try:
            document = _load_yaml(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            failures.append({"file": relative, "error": str(error)}); continue
        if document is None: continue
        if not isinstance(document, dict):
            failures.append({"file": relative, "error": "top-level YAML value is not a mapping"}); continue
        for selector, body in document.items():
            if not isinstance(body, dict) or "ros__parameters" not in body: continue
            parameters = body["ros__parameters"]
            if not isinstance(parameters, dict):
                failures.append({"file": relative, "error": f"{selector}.ros__parameters is not a mapping"}); continue
            selector = str(selector)
            selections = parameters.items() if selector == "/**" and parameters and all(
                isinstance(value, dict) for value in parameters.values()
            ) else [(selector, parameters)]
            for profile_selector, profile_parameters in selections:
                profile_selector = str(profile_selector)
                namespace = profile_selector.rsplit("/", 1)[0] or "/" if "/" in profile_selector and profile_selector != "/**" else None
                profiles.append({"id": _stable("parameter_profile", relative, profile_selector), "file": relative,
                    "node_selector": profile_selector, "namespace_selector": namespace,
                    "parameters": profile_parameters, "flattened_parameters": _flatten(profile_parameters),
                    "source": {"file": relative, "line_start": None, "column_start": None,
                               "line_end": None, "column_end": None}})
    summary = {"yaml_files": len(paths), "profiles": len(profiles),
               "parameters": sum(len(x["flattened_parameters"]) for x in profiles),
               "parse_failures": len(failures)}
    payload = {"stage": "ros_yaml_configuration_extraction",
               "summary": summary, "parse_failures": failures, "profiles": profiles}
    return ConfigurationPayload.model_validate(payload).model_dump(mode="json")


def write_deployment_artifacts(launch: dict[str, Any], configuration: dict[str, Any],
                               output_directory: str | Path) -> dict[str, Path]:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True)
    launch_path, config_path = output / "launch_ir.json", output / "configuration_ir.json"
    launch_path.write_text(LaunchPayload.model_validate(launch).model_dump_json(indent=2) + "\n", encoding="utf-8")
    config_path.write_text(ConfigurationPayload.model_validate(configuration).model_dump_json(indent=2) + "\n", encoding="utf-8")
    return {"launch": launch_path, "configuration": config_path}
