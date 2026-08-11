#!/usr/bin/env python3
"""Extract statically declared ROS 2 parameters from a Python node into YAML."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "launch_templates" / "yaml_files"


@dataclass(frozen=True)
class ExtractedParameter:
    """One parameter declaration found in the source file."""

    name: str
    default: Any
    line: int


class ParameterVisitor(ast.NodeVisitor):
    """Collect ROS node names and parameter declarations in source order."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.node_names: list[str] = []
        self.parameters: list[ExtractedParameter] = []
        self.warnings: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        self._extract_node_name(node)

        method_name = self._method_name(node.func)
        if method_name == "declare_parameter":
            self._extract_single_parameter(node)
        elif method_name == "declare_parameters":
            self._extract_parameter_group(node)

        self.generic_visit(node)

    @staticmethod
    def _method_name(function: ast.expr) -> str | None:
        if isinstance(function, ast.Attribute):
            return function.attr
        return None

    def _extract_node_name(self, node: ast.Call) -> None:
        is_super_init = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
        )
        if not is_super_init or not node.args:
            return

        value = self._literal(node.args[0], "node name", node.lineno)
        if isinstance(value, str) and value not in self.node_names:
            self.node_names.append(value)

    def _extract_single_parameter(self, node: ast.Call) -> None:
        name_node = self._argument(node, 0, "name")
        default_node = self._argument(node, 1, "value")
        if name_node is None:
            self.warnings.append(
                f"line {node.lineno}: declare_parameter has no parameter name"
            )
            return

        name = self._literal(name_node, "parameter name", node.lineno)
        if not isinstance(name, str):
            self.warnings.append(
                f"line {node.lineno}: skipped non-literal parameter name"
            )
            return

        default = None
        if default_node is None:
            self.warnings.append(
                f"line {node.lineno}: {name!r} has no statically declared default"
            )
        else:
            default = self._literal(default_node, f"default for {name!r}", node.lineno)

        self.parameters.append(ExtractedParameter(name, default, node.lineno))

    def _extract_parameter_group(self, node: ast.Call) -> None:
        namespace_node = self._argument(node, 0, "namespace")
        parameters_node = self._argument(node, 1, "parameters")

        namespace = ""
        if namespace_node is not None:
            value = self._literal(namespace_node, "parameter namespace", node.lineno)
            if isinstance(value, str):
                namespace = value
            else:
                self.warnings.append(
                    f"line {node.lineno}: non-literal parameter namespace was ignored"
                )

        if not isinstance(parameters_node, (ast.List, ast.Tuple)):
            self.warnings.append(
                f"line {node.lineno}: declare_parameters list is not statically readable"
            )
            return

        for entry in parameters_node.elts:
            if not isinstance(entry, (ast.List, ast.Tuple)) or len(entry.elts) < 2:
                self.warnings.append(
                    f"line {getattr(entry, 'lineno', node.lineno)}: skipped invalid "
                    "declare_parameters entry"
                )
                continue

            entry_line = getattr(entry, "lineno", node.lineno)
            name = self._literal(entry.elts[0], "parameter name", entry_line)
            if not isinstance(name, str):
                self.warnings.append(
                    f"line {entry_line}: skipped non-literal parameter name"
                )
                continue

            full_name = f"{namespace}.{name}" if namespace else name
            default = self._literal(
                entry.elts[1], f"default for {full_name!r}", entry_line
            )
            self.parameters.append(ExtractedParameter(full_name, default, entry_line))

    @staticmethod
    def _argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
        if len(node.args) > position:
            return node.args[position]
        for item in node.keywords:
            if item.arg == keyword:
                return item.value
        return None

    def _literal(self, node: ast.expr, label: str, line: int) -> Any:
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            expression = ast.get_source_segment(self.source, node) or ast.dump(node)
            self.warnings.append(
                f"line {line}: {label} is dynamic and was emitted as null: {expression}"
            )
            return None


def extract_parameters(source_path: Path) -> tuple[list[str], list[ExtractedParameter], list[str]]:
    """Read and parse an entire Python source file."""
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    visitor = ParameterVisitor(source)
    visitor.visit(tree)
    return visitor.node_names, visitor.parameters, visitor.warnings


def yaml_scalar(value: Any) -> str:
    """Render literal Python data using the YAML-compatible JSON flow syntax."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(yaml_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = (
            f"{json.dumps(str(key), ensure_ascii=False)}: {yaml_scalar(item)}"
            for key, item in value.items()
        )
        return "{" + ", ".join(entries) + "}"
    raise TypeError(f"Unsupported parameter value: {value!r}")


def render_ros_yaml(node_name: str, parameters: list[ExtractedParameter]) -> str:
    """Render extracted declarations as a ROS 2 parameter YAML document."""
    unique_parameters: dict[str, ExtractedParameter] = {}
    for parameter in parameters:
        unique_parameters[parameter.name] = parameter

    lines = [f"{json.dumps(node_name)}:", "  ros__parameters:"]
    if not unique_parameters:
        lines.append("    {}")
    else:
        for parameter in unique_parameters.values():
            lines.append(
                f"    {json.dumps(parameter.name, ensure_ascii=False)}: "
                f"{yaml_scalar(parameter.default)}"
            )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract declared ROS 2 node parameters into ROS YAML."
    )
    parser.add_argument("node_file", type=Path, help="path to the Python ROS node")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "override the default output path "
            "launch_templates/yaml_files/{node_name}.yaml"
        ),
    )
    parser.add_argument(
        "--node-name",
        help="override the node name inferred from super().__init__(...)"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_path = args.node_file.resolve()
    if not source_path.is_file():
        print(f"Error: node file does not exist: {source_path}", file=sys.stderr)
        return 1

    try:
        node_names, parameters, warnings = extract_parameters(source_path)
    except (OSError, UnicodeError, SyntaxError) as error:
        print(f"Error: could not parse {source_path}: {error}", file=sys.stderr)
        return 1

    node_name = args.node_name or (node_names[0] if node_names else source_path.stem)
    if len(node_names) > 1 and args.node_name is None:
        warnings.append(
            f"multiple node names found ({', '.join(node_names)}); using {node_name!r}"
        )

    output = render_ros_yaml(node_name, parameters)
    safe_node_name = node_name.strip("/").replace("/", "__")
    if not safe_node_name or safe_node_name in {".", ".."}:
        print(f"Error: invalid node name for output filename: {node_name!r}", file=sys.stderr)
        return 1

    output_path = args.output or DEFAULT_OUTPUT_DIRECTORY / f"{safe_node_name}.yaml"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
    except OSError as error:
        print(f"Error: could not write {output_path}: {error}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(
        f"Extracted {len(parameters)} parameter declaration(s) from {source_path}"
    )
    print(f"Generated {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
