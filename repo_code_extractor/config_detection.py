#!/usr/bin/env python3
"""Detect configuration sources and their immediate bindings in indexed Python."""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .ast_intermediate import ast_from_json


ROS_PARAMETER_OPERATIONS = {
    "declare_parameter": "declare",
    "declare_parameters": "declare_multiple",
    "get_parameter": "read",
    "get_parameters": "read_multiple",
    "get_parameter_or": "read_with_fallback",
    "get_parameter_type": "read_type",
    "has_parameter": "check",
    "set_parameter": "write",
    "set_parameters": "write_multiple",
    "set_parameters_atomically": "write_multiple_atomically",
    "undeclare_parameter": "undeclare",
}

ROS_CALLBACK_OPERATIONS = {
    "add_pre_set_parameters_callback": "register_pre_set_callback",
    "add_on_set_parameters_callback": "register_on_set_callback",
    "add_post_set_parameters_callback": "register_post_set_callback",
    "remove_pre_set_parameters_callback": "remove_pre_set_callback",
    "remove_on_set_parameters_callback": "remove_on_set_callback",
    "remove_post_set_parameters_callback": "remove_post_set_callback",
}

CONFIGPARSER_READS = {
    "get": "string",
    "getint": "integer",
    "getfloat": "float",
    "getboolean": "boolean",
    "items": "items",
}

CONFIGPARSER_LOADS = {"read", "read_file", "read_string", "read_dict"}
YAML_LOADERS = {"yaml.safe_load", "yaml.load", "ruamel.yaml.YAML.load"}
SCOPE_TYPES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def source_text(source: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    segment = ast.get_source_segment(source, node)
    if segment:
        return segment.strip()
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return None


def static_value(source: str, node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return {"expression": source_text(source, node)}


def position(node: ast.AST) -> dict[str, int | None]:
    return {
        "line": getattr(node, "lineno", None),
        "column": getattr(node, "col_offset", None),
        "end_line": getattr(node, "end_lineno", None),
        "end_column": getattr(node, "end_col_offset", None),
    }


def call_name(node: ast.Call, source: str) -> str:
    return source_text(source, node.func) or "<unknown>"


def final_attribute(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def assignment_names(node: ast.AST, source: str) -> list[str]:
    targets: list[ast.AST]
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        targets = [node.target]
    else:
        return []
    return [text for target in targets if (text := source_text(source, target))]


def keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def positional_or_keyword(call: ast.Call, index: int, name: str) -> ast.AST | None:
    named = keyword(call, name)
    if named is not None:
        return named
    return call.args[index] if len(call.args) > index else None


def dotted_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            for item in node.names:
                aliases[item.asname or item.name] = f"{prefix}.{item.name}".lstrip(".")
    return aliases


def canonical_name(expression: str, aliases: dict[str, str]) -> str:
    head, separator, tail = expression.partition(".")
    resolved = aliases.get(head, head)
    return resolved + (separator + tail if separator else "")


def containing_assignment(node: ast.AST, parents: dict[ast.AST, ast.AST], source: str) -> list[str]:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            return assignment_names(current, source)
        if isinstance(current, (ast.Return, ast.Expr, ast.keyword)):
            break
    return []


class ScopeDetector(ast.NodeVisitor):
    """Detect known configuration patterns within a single lexical scope."""

    def __init__(
        self,
        source: str,
        path: str,
        scope: str,
        root: ast.AST,
        aliases: dict[str, str],
        parents: dict[ast.AST, ast.AST],
        ros_node_scope: bool,
    ) -> None:
        self.source = source
        self.path = path
        self.scope = scope
        self.root = root
        self.aliases = aliases
        self.parents = parents
        self.ros_node_scope = ros_node_scope
        self.records: list[dict[str, Any]] = []
        self.object_roles: dict[str, str] = {}

    def base_record(self, node: ast.AST, kind: str, operation: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "operation": operation,
            "path": self.path,
            "scope": self.scope,
            "assigned_to": containing_assignment(node, self.parents, self.source),
            **position(node),
        }

    def visit_scope_body(self) -> None:
        body = getattr(self.root, "body", [])
        if isinstance(body, list):
            # Constructors need to be registered before their later method calls.
            for statement in body:
                self._register_object_role(statement)
            for statement in body:
                self.visit(statement)
        elif isinstance(body, ast.AST):
            self.visit(body)

    def generic_visit(self, node: ast.AST) -> None:
        if node is not self.root and isinstance(node, SCOPE_TYPES):
            return
        super().generic_visit(node)

    def _register_object_role(self, node: ast.AST) -> None:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            return
        value = node.value
        if not isinstance(value, ast.Call):
            return
        name = canonical_name(call_name(value, self.source), self.aliases)
        role = None
        if name == "argparse.ArgumentParser":
            role = "argparse_parser"
        elif name == "configparser.ConfigParser":
            role = "configparser"
        if role:
            for target in assignment_names(node, self.source):
                self.object_roles[target] = role

    def visit_Call(self, node: ast.Call) -> None:
        displayed = call_name(node, self.source)
        canonical = canonical_name(displayed, self.aliases)
        attribute = final_attribute(node)

        if attribute in ROS_PARAMETER_OPERATIONS:
            self._ros_parameter(node, attribute)
        elif attribute in ROS_CALLBACK_OPERATIONS:
            record = self.base_record(node, "ros_parameter_callback", ROS_CALLBACK_OPERATIONS[attribute])
            callback_node = node.args[0] if node.args else keyword(node, "callback")
            record.update(
                {
                    "callback": source_text(self.source, callback_node),
                    "confidence": "high" if self.ros_node_scope else "candidate",
                }
            )
            self.records.append(record)

        if canonical in {"os.getenv", "os.environ.get"}:
            record = self.base_record(node, "environment_variable", "read")
            record.update(
                {
                    "name": static_value(self.source, node.args[0] if node.args else None),
                    "default": static_value(self.source, node.args[1] if len(node.args) > 1 else keyword(node, "default")),
                    "api": canonical,
                }
            )
            self.records.append(record)

        receiver = displayed.rsplit(".", 1)[0] if "." in displayed else ""
        role = self.object_roles.get(receiver)
        if canonical == "argparse.ArgumentParser":
            record = self.base_record(node, "argparse", "create_parser")
            record["description"] = static_value(self.source, keyword(node, "description"))
            self.records.append(record)
        elif role == "argparse_parser" and attribute == "add_argument":
            self._argparse_argument(node)
        elif role == "argparse_parser" and attribute in {"parse_args", "parse_known_args"}:
            record = self.base_record(node, "argparse", attribute)
            record["parser"] = receiver
            self.records.append(record)
        elif canonical == "configparser.ConfigParser":
            self.records.append(self.base_record(node, "configparser", "create_parser"))
        elif role == "configparser" and attribute in CONFIGPARSER_LOADS:
            record = self.base_record(node, "configparser", "load")
            record.update({"parser": receiver, "api": attribute, "source": static_value(self.source, node.args[0] if node.args else None)})
            self.records.append(record)
        elif role == "configparser" and attribute in CONFIGPARSER_READS:
            record = self.base_record(node, "configparser", "read_value")
            record.update(
                {
                    "parser": receiver,
                    "api": attribute,
                    "section": static_value(self.source, node.args[0] if node.args else None),
                    "name": static_value(self.source, node.args[1] if len(node.args) > 1 else None),
                    "inferred_type": CONFIGPARSER_READS[attribute],
                    "fallback": static_value(self.source, keyword(node, "fallback")),
                }
            )
            self.records.append(record)

        if canonical in YAML_LOADERS:
            record = self.base_record(node, "yaml", "load")
            record.update({"api": canonical, "source": source_text(self.source, node.args[0] if node.args else None)})
            self.records.append(record)

        if canonical.endswith("DeclareLaunchArgument"):
            self._launch_argument(node)
        elif canonical.endswith("LaunchConfiguration"):
            record = self.base_record(node, "ros_launch_argument", "read")
            record.update(
                {
                    "name": static_value(self.source, positional_or_keyword(node, 0, "variable_name")),
                    "default": static_value(self.source, keyword(node, "default")),
                }
            )
            self.records.append(record)
        elif canonical == "launch_ros.actions.Node" or canonical.endswith("launch_ros.actions.Node"):
            self._launch_node(node)

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        owner = canonical_name(source_text(self.source, node.value) or "", self.aliases)
        if owner == "os.environ":
            record = self.base_record(node, "environment_variable", "read")
            record.update({"name": static_value(self.source, node.slice), "api": "os.environ[]"})
            self.records.append(record)
        self.generic_visit(node)

    def _ros_parameter(self, node: ast.Call, attribute: str) -> None:
        operation = ROS_PARAMETER_OPERATIONS[attribute]
        confidence = "high" if self.ros_node_scope else "candidate"
        if attribute == "declare_parameters":
            namespace_node = positional_or_keyword(node, 0, "namespace")
            parameters_node = positional_or_keyword(node, 1, "parameters")
            if isinstance(parameters_node, (ast.List, ast.Tuple)):
                for item in parameters_node.elts:
                    if isinstance(item, (ast.Tuple, ast.List)) and item.elts:
                        record = self.base_record(item, "ros_parameter", "declare")
                        record.update(
                            {
                                "name": static_value(self.source, item.elts[0]),
                                "default": static_value(self.source, item.elts[1] if len(item.elts) > 1 else None),
                                "descriptor": source_text(self.source, item.elts[2] if len(item.elts) > 2 else None),
                                "namespace": static_value(self.source, namespace_node),
                                "confidence": confidence,
                                "group_call": source_text(self.source, node.func),
                            }
                        )
                        self.records.append(record)
                return
            record = self.base_record(node, "ros_parameter", operation)
            record.update(
                {
                    "namespace": static_value(self.source, namespace_node),
                    "parameters": source_text(self.source, parameters_node),
                    "requires_resolution": True,
                    "confidence": confidence,
                }
            )
            self.records.append(record)
            return

        record = self.base_record(node, "ros_parameter", operation)
        name_node = positional_or_keyword(node, 0, "name")
        record.update(
            {
                "name": static_value(self.source, name_node),
                "confidence": confidence,
                "receiver": source_text(self.source, node.func.value) if isinstance(node.func, ast.Attribute) else None,
            }
        )
        if attribute == "declare_parameter":
            record["default"] = static_value(self.source, positional_or_keyword(node, 1, "value"))
            record["descriptor"] = source_text(self.source, positional_or_keyword(node, 2, "descriptor"))
        elif attribute == "get_parameter_or":
            record["fallback"] = static_value(self.source, positional_or_keyword(node, 1, "alternative_value"))
        elif attribute in {"get_parameters", "set_parameters", "set_parameters_atomically"}:
            record["values"] = source_text(self.source, name_node)
        self.records.append(record)

    def _argparse_argument(self, node: ast.Call) -> None:
        record = self.base_record(node, "argparse", "declare_argument")
        flags = [static_value(self.source, item) for item in node.args]
        record.update(
            {
                "parser": source_text(self.source, node.func.value) if isinstance(node.func, ast.Attribute) else None,
                "flags": flags,
                "destination": static_value(self.source, keyword(node, "dest")),
                "default": static_value(self.source, keyword(node, "default")),
                "type": source_text(self.source, keyword(node, "type")),
                "choices": static_value(self.source, keyword(node, "choices")),
                "required": static_value(self.source, keyword(node, "required")),
                "action": static_value(self.source, keyword(node, "action")),
                "help": static_value(self.source, keyword(node, "help")),
            }
        )
        if record["destination"] is None and flags:
            first_option = next((item for item in flags if isinstance(item, str) and item.startswith("--")), None)
            if first_option:
                record["destination"] = first_option[2:].replace("-", "_")
            elif isinstance(flags[0], str):
                record["destination"] = flags[0]
        self.records.append(record)

    def _launch_argument(self, node: ast.Call) -> None:
        record = self.base_record(node, "ros_launch_argument", "declare")
        record.update(
            {
                "name": static_value(self.source, positional_or_keyword(node, 0, "name")),
                "default": static_value(self.source, keyword(node, "default_value")),
                "description": static_value(self.source, keyword(node, "description")),
                "choices": static_value(self.source, keyword(node, "choices")),
            }
        )
        self.records.append(record)

    def _launch_node(self, node: ast.Call) -> None:
        package = static_value(self.source, keyword(node, "package"))
        executable = static_value(self.source, keyword(node, "executable"))
        parameters = keyword(node, "parameters")
        if parameters is None:
            return
        parameter_containers = (
            parameters.elts if isinstance(parameters, (ast.List, ast.Tuple)) else [parameters]
        )
        for container in parameter_containers:
            if not isinstance(container, ast.Dict):
                continue
            dictionary_record = self.base_record(
                container, "configuration_dictionary", "pass_to_node"
            )
            dictionary_record.update(
                {
                    "entries": [
                        {
                            "name": static_value(self.source, key_node),
                            "value": static_value(self.source, value_node),
                        }
                        for key_node, value_node in zip(container.keys, container.values)
                    ],
                    "package": package,
                    "executable": executable,
                    "evidence": "dictionary passed to launch_ros.actions.Node(parameters=...)",
                    "confidence": "high",
                    "node_call_line": node.lineno,
                }
            )
            self.records.append(dictionary_record)
        for parameter_name, value_node, container in launch_parameter_entries(parameters, self.source):
            record = self.base_record(value_node or container, "ros_launch_node_parameter", "pass_to_node")
            record.update(
                {
                    "name": parameter_name,
                    "value": static_value(self.source, value_node) if value_node else None,
                    "parameter_container": source_text(self.source, container),
                    "package": package,
                    "executable": executable,
                    "node_call_line": node.lineno,
                }
            )
            self.records.append(record)


def launch_parameter_entries(node: ast.AST, source: str) -> Iterable[tuple[Any, ast.AST | None, ast.AST]]:
    """Yield literal dictionary entries and unresolved parameter containers."""
    values = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    for value in values:
        if isinstance(value, ast.Dict):
            for key_node, value_node in zip(value.keys, value.values):
                yield static_value(source, key_node), value_node, value
        else:
            yield None, None, value


def class_is_ros_node(node: ast.ClassDef, source: str, aliases: dict[str, str]) -> bool:
    for base in node.bases:
        name = canonical_name(source_text(source, base) or "", aliases)
        if name in {"rclpy.node.Node", "Node"} or name.endswith(".Node"):
            return True
    return False


def pydantic_records(
    tree: ast.Module, source: str, path: str, module: str, aliases: dict[str, str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        resolved_bases = [canonical_name(source_text(source, base) or "", aliases) for base in node.bases]
        if not any(base.endswith("BaseSettings") for base in resolved_bases):
            continue
        class_scope = f"{module}.{node.name}"
        settings: dict[str, Any] = {}
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                names = assignment_names(statement, source)
                if "model_config" in names and isinstance(statement.value, ast.Call):
                    settings = {
                        item.arg: static_value(source, item.value)
                        for item in statement.value.keywords
                        if item.arg
                    }
        records.append(
            {
                "kind": "pydantic_settings_class",
                "operation": "declare",
                "name": node.name,
                "bases": resolved_bases,
                "settings": settings,
                "path": path,
                "scope": class_scope,
                "assigned_to": [],
                **position(node),
            }
        )
        for statement in node.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            names = assignment_names(statement, source)
            if not names or names == ["model_config"]:
                continue
            annotation = source_text(source, statement.annotation) if isinstance(statement, ast.AnnAssign) else None
            default_node = statement.value
            records.append(
                {
                    "kind": "pydantic_settings_field",
                    "operation": "declare",
                    "settings_class": node.name,
                    "name": names[0],
                    "type": annotation,
                    "default": static_value(source, default_node),
                    "environment_prefix": settings.get("env_prefix"),
                    "path": path,
                    "scope": class_scope,
                    "assigned_to": names,
                    **position(statement),
                }
            )
    return records


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def scope_records(
    tree: ast.Module,
    source: str,
    path: str,
    module: str,
    aliases: dict[str, str],
) -> list[dict[str, Any]]:
    parents = parent_map(tree)
    records: list[dict[str, Any]] = []

    def analyze(node: ast.AST, scope: str, ros_node_scope: bool) -> None:
        detector = ScopeDetector(source, path, scope, node, aliases, parents, ros_node_scope)
        detector.visit_scope_body()
        records.extend(detector.records)

    analyze(tree, module, False)

    def descend(parent: ast.AST, parent_scope: str, inherited_ros: bool) -> None:
        body = getattr(parent, "body", [])
        if not isinstance(body, list):
            return
        for child in body:
            if isinstance(child, ast.ClassDef):
                scope = f"{parent_scope}.{child.name}"
                is_ros = inherited_ros or class_is_ros_node(child, source, aliases)
                analyze(child, scope, is_ros)
                descend(child, scope, is_ros)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = f"{parent_scope}.{child.name}"
                analyze(child, scope, inherited_ros)
                descend(child, scope, inherited_ros)

    descend(tree, module, False)
    records.extend(pydantic_records(tree, source, path, module, aliases))
    return records


def wrapper_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        by_scope.setdefault((record["path"], record["scope"]), []).append(record)
    # Restrict this first pass to functions that contain explicit file/config
    # loaders.  Merely reading an environment variable inside a launch factory
    # does not make that function a configuration-loader wrapper.
    source_kinds = {"yaml", "configparser"}
    for (path, scope), values in sorted(by_scope.items()):
        if not any(value["kind"] in source_kinds for value in values):
            continue
        if scope.count(".") < 1 or scope.endswith(".<module>"):
            continue
        candidates.append(
            {
                "kind": "project_wrapper_candidate",
                "operation": "contains_configuration_source",
                "name": scope,
                "wrapped_kinds": sorted({value["kind"] for value in values if value["kind"] in source_kinds}),
                "path": path,
                "scope": scope,
                "confidence": "candidate",
                "note": "Requires value-flow confirmation that configuration is returned or exposed.",
            }
        )
    return candidates


def detect(ast_path: Path, workspace: Path) -> dict[str, Any]:
    extracted = json.loads(ast_path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for indexed in extracted["modules"]:
        try:
            source = indexed["source"]
            tree = ast_from_json(indexed["ast"])
        except (KeyError, TypeError, ValueError) as error:
            failures.append({"path": indexed["path"], "error": str(error)})
            continue
        module_records = scope_records(
            tree, source, indexed["path"], indexed["module"], dotted_aliases(tree)
        )
        for number, record in enumerate(module_records, start=1):
            record["id"] = f"config:{indexed['path']}:{record.get('line', 0)}:{record.get('column', 0)}:{number}"
        records.extend(module_records)
        modules.append(
            {
                "path": indexed["path"],
                "module": indexed["module"],
                "source_sha256": indexed["source_sha256"],
                "detections": len(module_records),
            }
        )
    wrappers = wrapper_candidates(records)
    for number, wrapper in enumerate(wrappers, start=1):
        wrapper["id"] = f"config-wrapper:{wrapper['path']}:{number}"
    records.extend(wrappers)
    kinds = Counter(record["kind"] for record in records)
    operations = Counter(record["operation"] for record in records)
    return {
        "schema_version": 1,
        "stage": "configuration_source_detection",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_ast": (
            ast_path.relative_to(workspace).as_posix()
            if ast_path.is_relative_to(workspace)
            else ast_path.as_posix()
        ),
        "scope_policy": {
            "input_modules_from_stage_01_ast": True,
            "reparse_source": False,
            "follow_imports": False,
            "transitive_value_flow": False,
            "dictionary_detection": "only dictionaries passed to launch Node parameters",
        },
        "summary": {
            "modules": len(modules),
            "records": len(records),
            "parse_failures": len(failures),
            "by_kind": dict(sorted(kinds.items())),
            "by_operation": dict(sorted(operations.items())),
        },
        "parse_failures": failures,
        "modules": modules,
        "records": records,
    }


def display(value: Any) -> str:
    if isinstance(value, dict) and set(value) == {"expression"}:
        return value["expression"] or "<unknown expression>"
    return json.dumps(value, ensure_ascii=False)


def render_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Configuration-source catalog", ""]
    current_path: str | None = None
    for record in payload["records"]:
        if record["path"] != current_path:
            current_path = record["path"]
            lines.extend([f"## `{current_path}`", ""])
        title = record.get("name") or record.get("flags") or record["kind"]
        lines.extend(
            [
                f"### {record['kind']}: `{display(title)}`",
                "",
                f"- Operation: `{record['operation']}`",
                f"- Scope: `{record['scope']}`",
            ]
        )
        if record.get("line") is not None:
            lines.append(f"- Line: {record['line']}")
        if record.get("default") is not None:
            lines.append(f"- Default: `{display(record['default'])}`")
        if record.get("assigned_to"):
            lines.append(f"- Assigned to: `{', '.join(record['assigned_to'])}`")
        if record.get("package") is not None or record.get("executable") is not None:
            lines.append(
                f"- Target node: package `{display(record.get('package'))}`, executable `{display(record.get('executable'))}`"
            )
        if record.get("confidence"):
            lines.append(f"- Confidence: `{record['confidence']}`")
        lines.append("")
    return "\n".join(lines)


def write_detection_artifacts(payload: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    """Write configuration-detection artifacts and return their paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "configuration_sources.json"
    markdown_path = output_dir / "configuration_sources.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--ast-artifact", type=Path, default=Path("artifacts/repo_ingestion/ast_extraction.json")
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/repo_ingestion")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    ast_path = args.ast_artifact if args.ast_artifact.is_absolute() else workspace / args.ast_artifact
    output_dir = args.output_dir if args.output_dir.is_absolute() else workspace / args.output_dir
    payload = detect(ast_path, workspace)
    write_detection_artifacts(payload, output_dir)
    return 1 if payload["parse_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
