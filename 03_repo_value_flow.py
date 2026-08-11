#!/usr/bin/env python3
"""Build a proof-of-concept configuration value-flow graph from Python ASTs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from helper_scripts.ast_intermediate import ast_from_json


ROS_CONSUMERS = {
    "create_publisher": {1: "used_as_topic"},
    "create_subscription": {1: "used_as_topic"},
    "create_service": {1: "used_as_service_name"},
    "create_client": {1: "used_as_service_name"},
    "create_timer": {0: "used_as_timer_period"},
    "create_rate": {0: "used_as_rate"},
}

CONFIG_RECORD_KINDS = {
    "argparse",
    "configuration_dictionary",
    "configparser",
    "environment_variable",
    "pydantic_settings_field",
    "ros_launch_argument",
    "ros_launch_node_parameter",
    "ros_parameter",
    "yaml",
}

SCOPE_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def text(source: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    segment = ast.get_source_segment(source, node)
    if segment:
        return segment.strip()
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return None


def position(node: ast.AST) -> dict[str, int | None]:
    return {
        "line": getattr(node, "lineno", None),
        "column": getattr(node, "col_offset", None),
        "end_line": getattr(node, "end_lineno", None),
        "end_column": getattr(node, "end_col_offset", None),
    }


def final_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return "<dynamic>"


def scope_class(scope: str) -> str | None:
    parts = scope.split(".")
    if len(parts) < 2:
        return None
    # Classes in this repository use conventional capitalized names.
    for index in range(len(parts) - 1, -1, -1):
        if parts[index][:1].isupper():
            return ".".join(parts[: index + 1])
    return None


def normalize_symbol(expression: str, path: str, scope: str) -> tuple[str, str]:
    class_scope = scope_class(scope)
    if expression == "self" or expression.startswith("self."):
        owner = class_scope or scope
    else:
        owner = scope
    node_id = "symbol:" + hashlib.sha1(
        f"{path}|{owner}|{expression}".encode("utf-8")
    ).hexdigest()
    return node_id, expression


class ReferenceVisitor(ast.NodeVisitor):
    """Collect top-level value references while skipping call function names."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.values: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        for argument in node.args:
            self.visit(argument)
        for item in node.keywords:
            self.visit(item.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and not any(
            isinstance(child, ast.Call) for child in ast.walk(node)
        ):
            value = text(self.source, node)
            if value:
                self.values.append(value)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id not in {"self", "True", "False", "None"}:
            self.values.append(node.id)


def references(source: str, node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    visitor = ReferenceVisitor(source)
    visitor.visit(node)
    return list(dict.fromkeys(visitor.values))


def targets(source: str, node: ast.AST) -> list[str]:
    result: list[str] = []

    def collect(value: ast.AST) -> None:
        if isinstance(value, (ast.Tuple, ast.List)):
            for item in value.elts:
                collect(item)
        else:
            expression = text(source, value)
            if expression:
                result.append(expression)

    collect(node)
    return result


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self._edge_keys: set[tuple[str, str, str, int | None]] = set()

    def node(self, node_id: str, kind: str, label: str, **attributes: Any) -> str:
        existing = self.nodes.setdefault(node_id, {"id": node_id, "kind": kind, "label": label})
        for key, value in attributes.items():
            if value is not None and key not in existing:
                existing[key] = value
        return node_id

    def edge(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        line: int | None = None,
        confidence: str = "exact",
        **attributes: Any,
    ) -> None:
        key = (source, target, relation, line)
        if source == target or key in self._edge_keys:
            return
        self._edge_keys.add(key)
        record = {
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": confidence,
        }
        if line is not None:
            record["line"] = line
        record.update({key: value for key, value in attributes.items() if value is not None})
        self.edges.append(record)

    def symbol(self, expression: str, path: str, scope: str, **attributes: Any) -> str:
        node_id, label = normalize_symbol(expression, path, scope)
        return self.node(node_id, "program_value", label, path=path, scope=scope, **attributes)

    def reachable(self, roots: set[str]) -> "Graph":
        outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.edges:
            outgoing[edge["source"]].append(edge)
        visited = set(roots)
        queue = deque(roots)
        selected_edges: list[dict[str, Any]] = []
        while queue:
            current = queue.popleft()
            for edge in outgoing.get(current, []):
                selected_edges.append(edge)
                target = edge["target"]
                if target not in visited:
                    visited.add(target)
                    queue.append(target)
        result = Graph()
        result.nodes = {node_id: self.nodes[node_id] for node_id in visited if node_id in self.nodes}
        result.edges = selected_edges
        result._edge_keys = {
            (edge["source"], edge["target"], edge["relation"], edge.get("line"))
            for edge in selected_edges
        }
        return result


def config_identity(record: dict[str, Any]) -> tuple[str, str]:
    kind = record["kind"]
    name = record.get("name")
    if name is None:
        name = record.get("destination") or record.get("flags") or record["id"]
    if isinstance(name, (dict, list)):
        name_label = json.dumps(name, sort_keys=True)
    else:
        name_label = str(name)
    owner = scope_class(record.get("scope", "")) or record.get("path", "")
    identity = f"{kind}|{owner}|{name_label}"
    return "config:" + hashlib.sha1(identity.encode("utf-8")).hexdigest(), name_label


def seed_configuration_graph(
    graph: Graph, catalog: dict[str, Any]
) -> tuple[set[str], dict[tuple[str, int, int], str]]:
    roots: set[str] = set()
    location_index: dict[tuple[str, int, int], str] = {}
    for record in catalog["records"]:
        if record["kind"] not in CONFIG_RECORD_KINDS:
            continue
        config_id, name = config_identity(record)
        roots.add(
            graph.node(
                config_id,
                "configuration_source",
                f"{record['kind']}: {name}",
                configuration_kind=record["kind"],
                name=name,
                path=record.get("path"),
                scope=record.get("scope"),
                default=record.get("default"),
            )
        )
        line = record.get("line")
        column = record.get("column")
        if line is not None and column is not None:
            location_index[(record["path"], line, column)] = config_id
        for destination in record.get("assigned_to", []):
            symbol = graph.symbol(destination, record["path"], record["scope"])
            graph.edge(config_id, symbol, "assigned_to", line=line)
        if record["kind"] == "pydantic_settings_field":
            symbol = graph.symbol(
                f"{record['settings_class']}.{record['name']}",
                record["path"],
                record["scope"],
            )
            graph.edge(config_id, symbol, "defines_field", line=line)
        if record["kind"] == "ros_launch_node_parameter":
            consumer_id = "consumer:" + hashlib.sha1(
                f"{record.get('path')}|{record.get('node_call_line')}|{record.get('executable')}".encode()
            ).hexdigest()
            graph.node(
                consumer_id,
                "ros_consumer",
                f"launch node: {record.get('executable')}",
                consumer_kind="launch_node",
                package=record.get("package"),
                executable=record.get("executable"),
                path=record.get("path"),
            )
            graph.edge(config_id, consumer_id, "passed_to_node", line=line)
    return roots, location_index


def scope_inventory(
    tree: ast.Module, module: str, source: str
) -> tuple[list[tuple[ast.AST, str]], dict[str, tuple[str, list[str]]]]:
    scopes: list[tuple[ast.AST, str]] = [(tree, module)]
    functions: dict[str, tuple[str, list[str]]] = {}

    def descend(parent: ast.AST, parent_scope: str) -> None:
        body = getattr(parent, "body", [])
        if not isinstance(body, list):
            return
        for child in body:
            if isinstance(child, ast.ClassDef):
                scope = f"{parent_scope}.{child.name}"
                scopes.append((child, scope))
                descend(child, scope)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = f"{parent_scope}.{child.name}"
                scopes.append((child, scope))
                arguments = [argument.arg for argument in child.args.posonlyargs + child.args.args]
                functions[scope] = (child.name, arguments)
                descend(child, scope)

    descend(tree, module)
    return scopes, functions


def child_statements(scope_node: ast.AST) -> Iterable[ast.AST]:
    """Walk statements in one scope without entering nested definitions."""
    stack = list(reversed(getattr(scope_node, "body", [])))
    while stack:
        node = stack.pop()
        if isinstance(node, SCOPE_NODES):
            continue
        yield node
        children = list(ast.iter_child_nodes(node))
        stack.extend(reversed(children))


def function_lookup(
    displayed_name: str,
    current_scope: str,
    functions: dict[str, tuple[str, list[str]]],
) -> str | None:
    if displayed_name.startswith("self."):
        method = displayed_name.split(".")[-1]
        class_name = scope_class(current_scope)
        candidate = f"{class_name}.{method}" if class_name else None
        return candidate if candidate in functions else None
    simple = displayed_name.split(".")[-1]
    candidates = [scope for scope, (name, _) in functions.items() if name == simple]
    return candidates[0] if len(candidates) == 1 else None


def failure_action(node: ast.If) -> str | None:
    for descendant in node.body:
        for item in ast.walk(descendant):
            if isinstance(item, ast.Raise):
                return "raise"
            if isinstance(item, ast.Return):
                return "return"
    return None


def validation_test(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare):
        return True
    return any(
        isinstance(item, ast.Call) and final_name(item) in {"isnan", "isfinite", "isinf"}
        for item in ast.walk(node)
    )


def analyze_scope(
    graph: Graph,
    source: str,
    path: str,
    scope_node: ast.AST,
    scope: str,
    functions: dict[str, tuple[str, list[str]]],
) -> None:
    return_id = graph.node(
        "return:" + hashlib.sha1(f"{path}|{scope}".encode()).hexdigest(),
        "return_value",
        f"return from {scope}",
        path=path,
        scope=scope,
    )

    for node in child_statements(scope_node):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            target_nodes = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            refs = references(source, value)
            target_values = [item for target in target_nodes for item in targets(source, target)]
            is_direct = isinstance(value, (ast.Name, ast.Attribute))
            operation_id: str | None = None
            if refs and not is_direct:
                operation_id = graph.node(
                    "operation:" + hashlib.sha1(
                        f"{path}|{scope}|{node.lineno}|{node.col_offset}|{text(source, value)}".encode()
                    ).hexdigest(),
                    "transformation",
                    text(source, value) or "expression",
                    path=path,
                    scope=scope,
                    **position(node),
                )
                for ref in refs:
                    graph.edge(
                        graph.symbol(ref, path, scope),
                        operation_id,
                        "transformed_by",
                        line=node.lineno,
                    )
            for target_value in target_values:
                target_id = graph.symbol(target_value, path, scope)
                if operation_id:
                    graph.edge(operation_id, target_id, "assigned_to", line=node.lineno)
                else:
                    for ref in refs:
                        graph.edge(
                            graph.symbol(ref, path, scope),
                            target_id,
                            "assigned_to",
                            line=node.lineno,
                        )
                if isinstance(value, ast.Call):
                    displayed = text(source, value.func) or "<dynamic>"
                    callee = function_lookup(displayed, scope, functions)
                    if callee:
                        callee_return = graph.node(
                            "return:" + hashlib.sha1(f"{path}|{callee}".encode()).hexdigest(),
                            "return_value",
                            f"return from {callee}",
                            path=path,
                            scope=callee,
                        )
                        graph.edge(callee_return, target_id, "assigned_to", line=node.lineno)

        elif isinstance(node, ast.Call):
            displayed = text(source, node.func) or "<dynamic>"
            method = final_name(node)
            callee = function_lookup(displayed, scope, functions)
            call_id = graph.node(
                "call:" + hashlib.sha1(
                    f"{path}|{scope}|{node.lineno}|{node.col_offset}".encode()
                ).hexdigest(),
                "call",
                displayed,
                path=path,
                scope=scope,
                **position(node),
            )
            for index, argument in enumerate(node.args):
                for ref in references(source, argument):
                    source_id = graph.symbol(ref, path, scope)
                    graph.edge(source_id, call_id, "passed_to", line=node.lineno, argument_index=index)
                    if callee:
                        parameters = functions[callee][1]
                        if index < len(parameters):
                            parameter_id = graph.symbol(parameters[index], path, callee)
                            graph.edge(
                                source_id,
                                parameter_id,
                                "passed_to_parameter",
                                line=node.lineno,
                                callee=callee,
                            )
            if method in ROS_CONSUMERS:
                consumer_id = graph.node(
                    "consumer:" + hashlib.sha1(
                        f"{path}|{scope}|{node.lineno}|{node.col_offset}|{method}".encode()
                    ).hexdigest(),
                    "ros_consumer",
                    displayed,
                    consumer_kind=method,
                    path=path,
                    scope=scope,
                    **position(node),
                )
                for argument_index, relation in ROS_CONSUMERS[method].items():
                    if argument_index < len(node.args):
                        for ref in references(source, node.args[argument_index]):
                            graph.edge(
                                graph.symbol(ref, path, scope),
                                consumer_id,
                                relation,
                                line=node.lineno,
                            )
            elif method == "publish" and isinstance(node.func, ast.Attribute):
                publisher = text(source, node.func.value)
                consumer_id = graph.node(
                    "consumer:" + hashlib.sha1(
                        f"{path}|{scope}|publisher|{publisher}".encode()
                    ).hexdigest(),
                    "ros_consumer",
                    f"publish through {publisher}",
                    consumer_kind="publish",
                    path=path,
                    scope=scope,
                )
                for argument in node.args:
                    for ref in references(source, argument):
                        graph.edge(
                            graph.symbol(ref, path, scope),
                            consumer_id,
                            "published_by",
                            line=node.lineno,
                        )

        elif isinstance(node, ast.Return):
            for ref in references(source, node.value):
                graph.edge(
                    graph.symbol(ref, path, scope),
                    return_id,
                    "returned_from",
                    line=node.lineno,
                )

        elif isinstance(node, ast.If):
            test = text(source, node.test) or "condition"
            condition_id = graph.node(
                "condition:" + hashlib.sha1(
                    f"{path}|{scope}|{node.lineno}|{node.col_offset}".encode()
                ).hexdigest(),
                "condition",
                test,
                path=path,
                scope=scope,
                failure_action=failure_action(node),
                **position(node),
            )
            relation = (
                "validated_by"
                if failure_action(node) and validation_test(node.test)
                else "controls"
            )
            for ref in references(source, node.test):
                graph.edge(
                    graph.symbol(ref, path, scope),
                    condition_id,
                    relation,
                    line=node.lineno,
                    test=test,
                )


def build_graph(extracted: dict[str, Any], catalog: dict[str, Any]) -> tuple[Graph, set[str], list[dict[str, str]]]:
    graph = Graph()
    roots, _ = seed_configuration_graph(graph, catalog)
    failures: list[dict[str, str]] = []
    for module in extracted["modules"]:
        try:
            source = module["source"]
            tree = ast_from_json(module["ast"])
        except (KeyError, TypeError, ValueError) as error:
            failures.append({"path": module["path"], "error": str(error)})
            continue
        scopes, functions = scope_inventory(tree, module["module"], source)
        for scope_node, scope in scopes:
            analyze_scope(graph, source, module["path"], scope_node, scope, functions)
    return graph.reachable(roots), roots, failures


def consumers_for(graph: Graph, root: str) -> list[str]:
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        outgoing[edge["source"]].append(edge["target"])
    visited = {root}
    queue = deque([root])
    consumers: list[str] = []
    while queue:
        current = queue.popleft()
        for target in outgoing.get(current, []):
            if target in visited:
                continue
            visited.add(target)
            queue.append(target)
            if graph.nodes.get(target, {}).get("kind") == "ros_consumer":
                consumers.append(graph.nodes[target]["label"])
    return sorted(set(consumers))


def render_markdown(graph: Graph, roots: set[str], summary: dict[str, Any]) -> str:
    lines = [
        "# Configuration value-flow graph",
        "",
        "This is a static proof of concept. Dynamic dispatch and container mutation may remain unresolved.",
        "",
        "## Summary",
        "",
        f"- Nodes: **{summary['nodes']}**",
        f"- Edges: **{summary['edges']}**",
        f"- Configuration roots: **{summary['configuration_roots']}**",
        f"- ROS consumers: **{summary['ros_consumers']}**",
        f"- Validations: **{summary['validations']}**",
        "",
        "## Configuration roots and discovered consumers",
        "",
    ]
    for root in sorted(roots, key=lambda item: graph.nodes.get(item, {}).get("label", item)):
        node = graph.nodes.get(root)
        if not node:
            continue
        consumers = consumers_for(graph, root)
        lines.append(f"### `{node['label']}`")
        lines.append("")
        if consumers:
            lines.extend(f"- `{consumer}`" for consumer in consumers)
        else:
            lines.append("- No ROS consumer reached by the current static analysis.")
        lines.append("")
    return "\n".join(lines)


def networkx_export(graph: Graph, output_path: Path) -> bool:
    try:
        import networkx as nx
    except ImportError:
        return False
    nx_graph = nx.MultiDiGraph()
    for node_id, attributes in graph.nodes.items():
        nx_graph.add_node(node_id, **{
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in attributes.items() if key != "id" and value is not None
        })
    for edge in graph.edges:
        attributes = {key: value for key, value in edge.items() if key not in {"source", "target"}}
        nx_graph.add_edge(edge["source"], edge["target"], **attributes)
    nx.write_graphml(nx_graph, output_path)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--ast-artifact", type=Path, default=Path("artifacts/repo_ingestion/ast_extraction.json"))
    parser.add_argument("--catalog", type=Path, default=Path("artifacts/repo_ingestion/configuration_sources.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/repo_ingestion"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    ast_path = args.ast_artifact if args.ast_artifact.is_absolute() else workspace / args.ast_artifact
    catalog_path = args.catalog if args.catalog.is_absolute() else workspace / args.catalog
    output_dir = args.output_dir if args.output_dir.is_absolute() else workspace / args.output_dir
    extracted = json.loads(ast_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    graph, roots, failures = build_graph(extracted, catalog)
    relation_counts = Counter(edge["relation"] for edge in graph.edges)
    kind_counts = Counter(node["kind"] for node in graph.nodes.values())
    summary = {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "configuration_roots": sum(node["kind"] == "configuration_source" for node in graph.nodes.values()),
        "ros_consumers": kind_counts.get("ros_consumer", 0),
        "validations": relation_counts.get("validated_by", 0),
        "by_node_kind": dict(sorted(kind_counts.items())),
        "by_relation": dict(sorted(relation_counts.items())),
        "parse_failures": len(failures),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "stage": "configuration_value_flow_graph",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "analysis_scope": {
            "static_proof_of_concept": True,
            "transitive_assignments": True,
            "simple_call_and_return_flow": True,
            "dynamic_dispatch": False,
            "arbitrary_container_mutation": False,
            "follow_imports": False,
            "input_ast_from_stage_01": True,
            "reparse_source": False,
        },
        "summary": summary,
        "parse_failures": failures,
        "nodes": sorted(graph.nodes.values(), key=lambda item: item["id"]),
        "edges": graph.edges,
    }
    (output_dir / "value_flow_graph.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "value_flow_graph.md").write_text(
        render_markdown(graph, roots, summary), encoding="utf-8"
    )
    graphml_written = networkx_export(graph, output_dir / "value_flow_graph.graphml")
    (output_dir / "value_flow_graph_status.json").write_text(
        json.dumps({"graphml_written": graphml_written, "networkx_available": graphml_written}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
