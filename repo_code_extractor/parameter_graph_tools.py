#!/usr/bin/env python3
"""Query parameter flows and relationships from the generated value-flow graph."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from .api import extract_code_information
from .ast_intermediate import ast_from_json
from .parameter_context_compiler import compile_parameter_context


PIPELINE_DEPENDENCIES = (
    "repo_code_extractor/ingestion.py",
    "repo_code_extractor/ast_extraction.py",
    "repo_code_extractor/config_detection.py",
    "repo_code_extractor/value_flow.py",
    "repo_code_extractor/ast_intermediate.py",
)

ARTIFACT_DIRECTORY = Path("artifacts/repo_ingestion")
MODULE_INDEX = ARTIFACT_DIRECTORY / "module_index.json"
AST_EXTRACTION = ARTIFACT_DIRECTORY / "ast_extraction.json"
CONFIGURATION_CATALOG = ARTIFACT_DIRECTORY / "configuration_sources.json"
VALUE_FLOW_GRAPH = ARTIFACT_DIRECTORY / "value_flow_graph.json"

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "install",
    "log",
    "logs",
    "site-packages",
    "venv",
}

RELATIONSHIP_NODE_KINDS = {
    "call",
    "condition",
    "program_value",
    "return_value",
    "ros_consumer",
    "transformation",
}

LOGGING_CALL_SUFFIXES = {
    "debug",
    "error",
    "exception",
    "fatal",
    "info",
    "log",
    "print",
    "warn",
    "warning",
}


def repository_root() -> Path:
    script = Path(__file__).resolve()
    for candidate in script.parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"Could not find repository root above {script}")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    # Stage 02 hashes text after Python's universal-newline normalization.
    content = path.read_text(encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def is_ignored_directory(name: str) -> bool:
    return name in IGNORED_DIRECTORIES or "pycache" in name.casefold()


def indexed_python_files(root: Path, source_roots: Iterable[str]) -> set[str]:
    files: set[str] = set()
    for configured_root in source_roots:
        source_root = (root / configured_root).resolve()
        if not source_root.is_dir():
            continue
        for directory_name, directory_names, file_names in os.walk(source_root):
            directory_names[:] = [
                name for name in directory_names if not is_ignored_directory(name)
            ]
            directory = Path(directory_name)
            for name in file_names:
                path = directory / name
                if path.suffix == ".py" and path.is_file() and not path.is_symlink():
                    files.add(path.relative_to(root).as_posix())
    return files


def artifact_staleness(root: Path) -> list[str]:
    required = [MODULE_INDEX, AST_EXTRACTION, CONFIGURATION_CATALOG, VALUE_FLOW_GRAPH]
    missing = [path.as_posix() for path in required if not (root / path).is_file()]
    if missing:
        return [f"missing artifact: {path}" for path in missing]

    index = load_json(root / MODULE_INDEX)
    catalog = load_json(root / CONFIGURATION_CATALOG)
    recorded = {item["path"]: item["source_sha256"] for item in catalog["modules"]}
    current_paths = indexed_python_files(root, index["source_roots"])
    reasons: list[str] = []

    for path in sorted(current_paths - set(recorded)):
        reasons.append(f"new source file: {path}")
    for path in sorted(set(recorded) - current_paths):
        reasons.append(f"removed source file: {path}")
    for relative_path in sorted(current_paths & set(recorded)):
        path = root / relative_path
        if sha256(path) != recorded[relative_path]:
            reasons.append(f"changed source file: {relative_path}")

    graph_time = (root / VALUE_FLOW_GRAPH).stat().st_mtime
    for script_name in PIPELINE_DEPENDENCIES:
        script = root / script_name
        if not script.is_file():
            reasons.append(f"missing pipeline script: {script_name}")
        elif script.stat().st_mtime > graph_time:
            reasons.append(f"pipeline script changed: {script_name}")
    return reasons


def regenerate_pipeline(root: Path, reasons: list[str]) -> dict[str, Any]:
    result = extract_code_information(
        root,
        output_directory=ARTIFACT_DIRECTORY,
        include_chunks=False,
        include_static_analysis=False,
    )
    return {
        "performed": True,
        "reasons": reasons,
        "stages": [
            {
                "script": "repo_code_extractor.extract_code_information",
                "return_code": 0,
                "summaries": result.summaries,
            }
        ],
    }


def ensure_current_artifacts(root: Path) -> dict[str, Any]:
    reasons = artifact_staleness(root)
    if not reasons:
        return {"performed": False, "reasons": [], "stages": []}
    result = regenerate_pipeline(root, reasons)
    remaining = artifact_staleness(root)
    if remaining:
        raise RuntimeError(
            "Artifacts are still stale after regeneration:\n"
            + json.dumps(remaining, indent=2)
        )
    return result


class GraphIndex:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.nodes = {node["id"]: node for node in payload["nodes"]}
        self.edges = payload["edges"]
        self.outgoing: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for index, edge in enumerate(self.edges):
            self.outgoing[edge["source"]].append((index, edge))

    def parameter_matches(self, name: str) -> dict[str, dict[str, Any]]:
        return {
            node_id: node
            for node_id, node in self.nodes.items()
            if node.get("kind") == "configuration_source" and node.get("name") == name
        }

    def forward_flow(self, root_id: str) -> dict[str, Any]:
        visited_nodes = {root_id}
        visited_edges: set[int] = set()
        queue = deque([root_id])
        while queue:
            current = queue.popleft()
            for edge_index, edge in self.outgoing.get(current, []):
                visited_edges.add(edge_index)
                target = edge["target"]
                if target not in visited_nodes:
                    visited_nodes.add(target)
                    queue.append(target)
        return {
            "root": root_id,
            "nodes": {
                node_id: self.nodes[node_id]
                for node_id in sorted(visited_nodes)
            },
            "edges": [
                {"edge_index": index, **self.edges[index]}
                for index in sorted(visited_edges)
            ],
        }


def module_names(module_index: dict[str, Any]) -> dict[str, str]:
    return {item["path"]: item["module"] for item in module_index["modules"]}


def scope_map(tree: ast.Module, module_name: str) -> dict[str, ast.AST]:
    result: dict[str, ast.AST] = {module_name: tree}

    def descend(parent: ast.AST, parent_scope: str) -> None:
        body = getattr(parent, "body", [])
        if not isinstance(body, list):
            return
        for child in body:
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = f"{parent_scope}.{child.name}"
                result[scope] = child
                descend(child, scope)

    descend(tree, module_name)
    return result


def source_lines(source: str, node: ast.AST) -> tuple[int, int, str]:
    decorators = getattr(node, "decorator_list", [])
    start = min(
        [getattr(node, "lineno", 1)]
        + [getattr(decorator, "lineno", getattr(node, "lineno", 1)) for decorator in decorators]
    )
    end = getattr(node, "end_lineno", start)
    lines = source.splitlines(keepends=True)
    return start, end, "".join(lines[start - 1 : end])


def enclosing_module_statement(tree: ast.Module, line: int) -> ast.AST | None:
    candidates = [
        statement
        for statement in tree.body
        if getattr(statement, "lineno", 0) <= line <= getattr(statement, "end_lineno", 0)
    ]
    return min(
        candidates,
        key=lambda item: getattr(item, "end_lineno", line) - getattr(item, "lineno", line),
        default=None,
    )


def retrieve_source(
    root: Path,
    ast_artifact: dict[str, Any],
    graph_nodes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    names = module_names(ast_artifact)
    modules = {item["path"]: item for item in ast_artifact["modules"]}
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in graph_nodes.values():
        if node.get("path") and node.get("scope"):
            by_path[node["path"]].append(node)

    methods: dict[str, dict[str, Any]] = {}
    statements: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for path, relevant_nodes in sorted(by_path.items()):
        try:
            module = modules[path]
            source = module["source"]
            tree = ast_from_json(module["ast"])
        except (KeyError, TypeError, ValueError) as error:
            unresolved.append({"path": path, "error": str(error)})
            continue
        module_name = names.get(path)
        if not module_name:
            unresolved.append({"path": path, "error": "module not found in index"})
            continue
        scopes = scope_map(tree, module_name)
        for graph_node in relevant_nodes:
            scope = graph_node["scope"]
            scope_node = scopes.get(scope)
            if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start, end, code = source_lines(source, scope_node)
                key = f"{path}:{start}:{end}"
                record = methods.setdefault(
                    key,
                    {
                        "id": key,
                        "path": path,
                        "qualified_name": scope,
                        "start_line": start,
                        "end_line": end,
                        "code": code,
                        "supports_nodes": [],
                    },
                )
                record["supports_nodes"].append(graph_node["id"])
            elif scope == module_name and graph_node.get("line"):
                statement = enclosing_module_statement(tree, graph_node["line"])
                if statement is None:
                    unresolved.append(
                        {
                            "path": path,
                            "node_id": graph_node["id"],
                            "error": "no enclosing module statement",
                        }
                    )
                    continue
                start, end, code = source_lines(source, statement)
                key = f"{path}:{start}:{end}"
                record = statements.setdefault(
                    key,
                    {
                        "id": key,
                        "path": path,
                        "start_line": start,
                        "end_line": end,
                        "code": code,
                        "supports_nodes": [],
                    },
                )
                record["supports_nodes"].append(graph_node["id"])

    for collection in (methods, statements):
        for record in collection.values():
            record["supports_nodes"] = sorted(set(record["supports_nodes"]))
    return {
        "methods": methods,
        "module_statements": statements,
        "unresolved_source": unresolved,
    }


def parameter_content(
    root: Path,
    ast_artifact: dict[str, Any],
    graph: GraphIndex,
    parameter_id: str,
) -> dict[str, Any]:
    flow = graph.forward_flow(parameter_id)
    source = retrieve_source(root, ast_artifact, flow["nodes"])
    return {
        "parameter": graph.nodes[parameter_id],
        "forward_flow": flow,
        **source,
    }


def similar_parameter_names(graph: GraphIndex, query: str) -> list[str]:
    query_folded = query.casefold()
    names = {
        str(node.get("name"))
        for node in graph.nodes.values()
        if node.get("kind") == "configuration_source" and node.get("name") is not None
    }
    return sorted(name for name in names if query_folded in name.casefold())


def get_parameter_flow(
    name: str,
    *,
    repo_root: str | Path | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else repository_root().resolve()
    regeneration = ensure_current_artifacts(root) if refresh else {
        "performed": False,
        "reasons": ["refresh disabled"],
        "stages": [],
    }
    ast_artifact = load_json(root / AST_EXTRACTION)
    graph = GraphIndex(load_json(root / VALUE_FLOW_GRAPH))
    matches = graph.parameter_matches(name)
    return {
        "tool": "get_parameter_flow",
        "query": {"name": name, "match": "exact"},
        "status": "ok" if matches else "not_found",
        "regeneration": regeneration,
        "matches": {
            parameter_id: parameter_content(root, ast_artifact, graph, parameter_id)
            for parameter_id in sorted(matches)
        },
        "similar_names": [] if matches else similar_parameter_names(graph, name),
    }


def is_logging_node(node: dict[str, Any]) -> bool:
    if node.get("kind") != "call":
        return False
    suffix = str(node.get("label", "")).split(".")[-1].casefold()
    return suffix in LOGGING_CALL_SUFFIXES or "get_logger" in str(node.get("label", ""))


def classify_shared_node(
    node: dict[str, Any],
    incoming_a: set[str],
    incoming_b: set[str],
) -> str:
    kind = node["kind"]
    if kind == "transformation":
        return "shared_transformations"
    if kind == "condition":
        if "validated_by" in incoming_a and "validated_by" in incoming_b:
            return "shared_validations"
        return "shared_conditions"
    if kind == "ros_consumer":
        return "shared_ros_consumers"
    if kind == "call":
        return "shared_calls"
    if kind == "return_value":
        return "shared_returns"
    return "shared_values"


def incoming_relations(flow: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for edge in flow["edges"]:
        result[edge["target"]].add(edge["relation"])
    return result


def pair_relationship(
    root: Path,
    ast_artifact: dict[str, Any],
    graph: GraphIndex,
    parameter_a: str,
    content_a: dict[str, Any],
    parameter_b: str,
    content_b: dict[str, Any],
) -> dict[str, Any]:
    flow_a = content_a["forward_flow"]
    flow_b = content_b["forward_flow"]
    nodes_a = set(flow_a["nodes"])
    nodes_b = set(flow_b["nodes"])
    shared_ids = nodes_a & nodes_b
    shared_ids.discard(parameter_a)
    shared_ids.discard(parameter_b)

    incoming_a = incoming_relations(flow_a)
    incoming_b = incoming_relations(flow_b)
    categories: dict[str, list[dict[str, Any]]] = {
        "shared_transformations": [],
        "shared_validations": [],
        "shared_conditions": [],
        "shared_calls": [],
        "shared_returns": [],
        "shared_ros_consumers": [],
        "shared_values": [],
    }
    evidence_nodes: dict[str, dict[str, Any]] = {}
    ignored_logging: list[dict[str, Any]] = []
    for node_id in sorted(shared_ids):
        node = graph.nodes[node_id]
        if node.get("kind") not in RELATIONSHIP_NODE_KINDS:
            continue
        if is_logging_node(node):
            ignored_logging.append(node)
            continue
        category = classify_shared_node(node, incoming_a[node_id], incoming_b[node_id])
        evidence = {
            "node": node,
            "incoming_relations_from_a": sorted(incoming_a[node_id]),
            "incoming_relations_from_b": sorted(incoming_b[node_id]),
        }
        categories[category].append(evidence)
        evidence_nodes[node_id] = node

    direct_a_to_b = parameter_b in nodes_a
    direct_b_to_a = parameter_a in nodes_b
    source = retrieve_source(root, ast_artifact, evidence_nodes)
    relationship_found = direct_a_to_b or direct_b_to_a or bool(evidence_nodes)
    return {
        "parameter_a_id": parameter_a,
        "parameter_b_id": parameter_b,
        "relationship_found": relationship_found,
        "direct_a_to_b": direct_a_to_b,
        "direct_b_to_a": direct_b_to_a,
        **categories,
        "ignored_logging_nodes": ignored_logging,
        **source,
    }


def get_parameter_relationship(
    name_a: str,
    name_b: str,
    *,
    repo_root: str | Path | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else repository_root().resolve()
    regeneration = ensure_current_artifacts(root) if refresh else {
        "performed": False,
        "reasons": ["refresh disabled"],
        "stages": [],
    }
    ast_artifact = load_json(root / AST_EXTRACTION)
    graph = GraphIndex(load_json(root / VALUE_FLOW_GRAPH))
    matches_a = graph.parameter_matches(name_a)
    matches_b = graph.parameter_matches(name_b)
    content_a = {
        parameter_id: parameter_content(root, ast_artifact, graph, parameter_id)
        for parameter_id in sorted(matches_a)
    }
    content_b = {
        parameter_id: parameter_content(root, ast_artifact, graph, parameter_id)
        for parameter_id in sorted(matches_b)
    }
    relationships: dict[str, Any] = {}
    for parameter_a, parameter_a_content in content_a.items():
        for parameter_b, parameter_b_content in content_b.items():
            key = f"{parameter_a}::{parameter_b}"
            relationships[key] = pair_relationship(
                root,
                ast_artifact,
                graph,
                parameter_a,
                parameter_a_content,
                parameter_b,
                parameter_b_content,
            )
    status = "ok" if matches_a and matches_b else "not_found"
    return {
        "tool": "get_parameter_relationship",
        "query": {
            "parameter_a": name_a,
            "parameter_b": name_b,
            "match": "exact",
        },
        "status": status,
        "regeneration": regeneration,
        "parameter_a_matches": content_a,
        "parameter_b_matches": content_b,
        "pair_relationships": relationships,
        "similar_names": {
            "parameter_a": [] if matches_a else similar_parameter_names(graph, name_a),
            "parameter_b": [] if matches_b else similar_parameter_names(graph, name_b),
        },
    }


def get_parameter_context(
    name: str,
    *,
    repo_root: str | Path | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    """Run retrieval tools and compile their verbose results for LLM consumption."""
    root = Path(repo_root).resolve() if repo_root is not None else repository_root().resolve()
    flow = get_parameter_flow(name, repo_root=root, refresh=refresh)
    relationship = None
    if flow["status"] == "ok" and len(flow.get("matches", {})) > 1:
        relationship = get_parameter_relationship(
            name, name, repo_root=root, refresh=False
        )
    compiled = compile_parameter_context(name, flow, relationship)
    return {
        "tool": "get_parameter_context",
        "query": {"name": name, "match": "exact"},
        "status": flow["status"],
        "regeneration": flow["regeneration"],
        "retrieval_summary": {
            "parameter_instances": len(flow.get("matches", {})),
            "relationship_pairs": len(
                relationship.get("pair_relationships", {}) if relationship else {}
            ),
        },
        **compiled,
        "similar_names": flow.get("similar_names", []),
    }


def safe_parameter_name(name: str) -> str:
    """Return a filesystem-safe, readable directory name for a parameter."""
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    normalized = normalized.strip("._-")
    return normalized or "unnamed_parameter"


def write_parameter_artifacts(
    name: str,
    result: dict[str, Any],
    *,
    repo_root: str | Path | None = None,
    output_root: str | Path = Path("artifacts/code_parameters"),
) -> dict[str, Path]:
    """Save structured and readable evidence under a parameter-specific folder."""
    root = Path(repo_root).resolve() if repo_root is not None else repository_root().resolve()
    base = Path(output_root)
    if not base.is_absolute():
        base = root / base
    destination = base / safe_parameter_name(name)
    destination.mkdir(parents=True, exist_ok=True)

    context_path = destination / "context.md"
    result_path = destination / "result.json"
    context_path.write_text(result.get("llm_context", ""), encoding="utf-8")
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "directory": destination,
        "context": context_path,
        "result": result_path,
    }


def extract_parameter_artifacts(
    name: str,
    *,
    repo_root: str | Path | None = None,
    output_root: str | Path = Path("artifacts/code_parameters"),
    refresh: bool = True,
) -> dict[str, Any]:
    """Extract one parameter's evidence, save it, and return result plus paths."""
    result = get_parameter_context(name, repo_root=repo_root, refresh=refresh)
    paths = write_parameter_artifacts(
        name, result, repo_root=repo_root, output_root=output_root
    )
    return {**result, "artifact_paths": paths}


def write_result(result: dict[str, Any], output: Path | None, root: Path) -> None:
    rendered = json.dumps(result, indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    destination = output if output.is_absolute() else root / output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    print(destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--output", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    flow = subparsers.add_parser("flow", help="Return all forward flows for an exact parameter name")
    flow.add_argument("parameter")
    relationship = subparsers.add_parser(
        "relationship", help="Return relationships for every matching parameter pair"
    )
    relationship.add_argument("parameter_a")
    relationship.add_argument("parameter_b")
    context = subparsers.add_parser(
        "context", help="Write LLM-facing Markdown for one parameter"
    )
    context.add_argument("parameter")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.repo_root.resolve()
    refresh = not args.no_refresh
    if args.command == "flow":
        result = get_parameter_flow(args.parameter, repo_root=root, refresh=refresh)
    elif args.command == "relationship":
        result = get_parameter_relationship(
            args.parameter_a,
            args.parameter_b,
            repo_root=root,
            refresh=refresh,
        )
    else:
        result = extract_parameter_artifacts(
            args.parameter, repo_root=root, refresh=refresh
        )
        if args.output is None:
            print(result["artifact_paths"]["directory"])
        else:
            destination = args.output if args.output.is_absolute() else root / args.output
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(result["llm_context"], encoding="utf-8")
            print(destination)
        return 0 if result["status"] == "ok" else 2
    write_result(result, args.output, root)
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
