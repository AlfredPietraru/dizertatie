#!/usr/bin/env python3
"""Extract scoped AST facts from modules selected by 00_repo_ingestion.py."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from helper_scripts.ast_intermediate import ast_to_json


SCOPE_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def location(node: ast.AST) -> dict[str, int | None]:
    return {
        "line": getattr(node, "lineno", None),
        "column": getattr(node, "col_offset", None),
        "end_line": getattr(node, "end_lineno", None),
        "end_column": getattr(node, "end_col_offset", None),
    }


def source_segment(source: str, node: ast.AST) -> str | None:
    segment = ast.get_source_segment(source, node)
    return segment.strip() if segment else None


def expression(source: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    segment = source_segment(source, node)
    if segment is not None:
        return segment
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return None


def assignment_targets(node: ast.AST, source: str) -> list[str]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        targets = [node.target]
    else:
        return []
    return [value for target in targets if (value := expression(source, target))]


def callable_name(node: ast.Call, source: str) -> str:
    return expression(source, node.func) or "<unknown>"


def definition_kind(node: ast.AST, parent_kind: str) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_method" if parent_kind == "class" else "async_function"
    if isinstance(node, ast.FunctionDef):
        return "method" if parent_kind == "class" else "function"
    return "lambda"


def function_signature(node: ast.AST, source: str) -> str | None:
    if not isinstance(node, FUNCTION_NODES):
        return None
    args = expression(source, node.args)
    if isinstance(node, ast.Lambda):
        return args
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = expression(source, node.returns)
    suffix = f" -> {returns}" if returns else ""
    return f"{prefix} {node.name}({args or ''}){suffix}"


class LocalFactVisitor(ast.NodeVisitor):
    """Collect facts in one lexical scope without entering child scopes."""

    def __init__(self, source: str, root: ast.AST) -> None:
        self.source = source
        self.root = root
        self.assignments: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.conditions: list[dict[str, Any]] = []
        self.returns: list[dict[str, Any]] = []

    def visit_scope_body(self) -> None:
        body = getattr(self.root, "body", [])
        if isinstance(body, list):
            for statement in body:
                self.visit(statement)
        elif isinstance(body, ast.AST):
            self.visit(body)

    def generic_visit(self, node: ast.AST) -> None:
        if node is not self.root and isinstance(node, SCOPE_NODES):
            return
        super().generic_visit(node)

    def record_assignment(self, node: ast.AST, value: ast.AST | None) -> None:
        record = {
            "kind": type(node).__name__,
            "targets": assignment_targets(node, self.source),
            "value": expression(self.source, value),
            **location(node),
        }
        if isinstance(node, ast.AnnAssign):
            record["annotation"] = expression(self.source, node.annotation)
        if isinstance(node, ast.AugAssign):
            record["operator"] = type(node.op).__name__
        self.assignments.append(record)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.record_assignment(node, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.record_assignment(node, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.record_assignment(node, node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.record_assignment(node, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(
            {
                "callable": callable_name(node, self.source),
                "arguments": [expression(self.source, arg) for arg in node.args],
                "keywords": [
                    {
                        "name": keyword.arg,
                        "value": expression(self.source, keyword.value),
                    }
                    for keyword in node.keywords
                ],
                **location(node),
            }
        )
        self.generic_visit(node)

    def record_condition(self, node: ast.AST, kind: str, test: ast.AST) -> None:
        self.conditions.append(
            {"kind": kind, "test": expression(self.source, test), **location(node)}
        )

    def visit_If(self, node: ast.If) -> None:
        self.record_condition(node, "if", node.test)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.record_condition(node, "while", node.test)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.record_condition(node, "conditional_expression", node.test)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.record_condition(node, "assert", node.test)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        for test in node.ifs:
            self.record_condition(test, "comprehension_filter", test)
        self.generic_visit(node)

    def visit_match_case(self, node: ast.match_case) -> None:
        if node.guard is not None:
            self.record_condition(node.guard, "match_guard", node.guard)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(
            {"value": expression(self.source, node.value), **location(node)}
        )
        self.generic_visit(node)

    def result(self) -> dict[str, list[dict[str, Any]]]:
        key = lambda item: (item.get("line") or 0, item.get("column") or 0)
        return {
            "assignments": sorted(self.assignments, key=key),
            "calls": sorted(self.calls, key=key),
            "conditions": sorted(self.conditions, key=key),
            "returns": sorted(self.returns, key=key),
        }


def child_definitions(node: ast.AST) -> Iterable[ast.AST]:
    body = getattr(node, "body", [])
    if not isinstance(body, list):
        return
    for statement in body:
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield statement


def decorators(node: ast.AST, source: str) -> list[str]:
    return [
        value
        for decorator in getattr(node, "decorator_list", [])
        if (value := expression(source, decorator))
    ]


def bases(node: ast.AST, source: str) -> list[str]:
    if not isinstance(node, ast.ClassDef):
        return []
    return [value for base in node.bases if (value := expression(source, base))]


def scope_record(
    node: ast.AST,
    source: str,
    qualified_name: str,
    kind: str,
    parent: str | None,
) -> dict[str, Any]:
    visitor = LocalFactVisitor(source, node)
    visitor.visit_scope_body()
    name = "<module>" if isinstance(node, ast.Module) else getattr(node, "name", "<lambda>")
    return {
        "name": name,
        "qualified_name": qualified_name,
        "kind": kind,
        "parent": parent,
        "signature": function_signature(node, source),
        "decorators": decorators(node, source),
        "bases": bases(node, source),
        **location(node),
        **visitor.result(),
    }


def extract_scopes(tree: ast.Module, source: str, module_name: str) -> list[dict[str, Any]]:
    scopes = [scope_record(tree, source, module_name, "module", None)]

    def descend(parent_node: ast.AST, parent_name: str, parent_kind: str) -> None:
        for child in child_definitions(parent_node):
            child_name = getattr(child, "name")
            qualified_name = f"{parent_name}.{child_name}"
            kind = definition_kind(child, parent_kind)
            scopes.append(
                scope_record(child, source, qualified_name, kind, parent_name)
            )
            descend(child, qualified_name, "class" if kind == "class" else "function")

    descend(tree, module_name, "module")
    return scopes


def extract(index_path: Path, workspace: Path) -> dict[str, Any]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    modules: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    totals: Counter[str] = Counter()

    for indexed_module in index["modules"]:
        relative_path = indexed_module["path"]
        path = workspace / relative_path
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative_path)
            scopes = extract_scopes(tree, source, indexed_module["module"])
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append({"path": relative_path, "error": str(error)})
            continue
        for scope in scopes:
            totals["scopes"] += 1
            totals[scope["kind"]] += 1
            for fact_name in ("assignments", "calls", "conditions", "returns"):
                totals[fact_name] += len(scope[fact_name])
        modules.append(
            {
                "package_root": indexed_module["package_root"],
                "path": relative_path,
                "module": indexed_module["module"],
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "ast": ast_to_json(tree),
                "scopes": scopes,
            }
        )

    return {
        "schema_version": 1,
        "stage": "scoped_ast_extraction",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_index": index_path.relative_to(workspace).as_posix(),
        "scope_policy": {
            "input_modules_from_previous_index": True,
            "follow_imports": False,
            "include_library_internals": False,
            "child_scope_facts_are_not_attributed_to_parent": True,
            "lossless_ast_intermediate": True,
            "source_embedded_for_exact_source_segments": True,
        },
        "summary": {
            "modules": len(modules),
            **dict(sorted(totals.items())),
            "parse_failures": len(failures),
        },
        "parse_failures": failures,
        "modules": modules,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Scoped AST extraction", ""]
    for module in payload["modules"]:
        lines.extend([f"## `{module['path']}`", ""])
        for scope in module["scopes"]:
            lines.extend(
                [
                    f"### `{scope['qualified_name']}`",
                    "",
                    f"Kind: `{scope['kind']}`",
                    "",
                ]
            )
            if scope["signature"]:
                lines.extend([f"Signature: `{scope['signature']}`", ""])
            for label in ("assignments", "calls", "conditions", "returns"):
                values = scope[label]
                lines.append(f"{label.capitalize()}: {len(values)}")
                for value in values:
                    if label == "assignments":
                        detail = f"{', '.join(value['targets'])} = {value['value']}"
                    elif label == "calls":
                        detail = value["callable"]
                    elif label == "conditions":
                        detail = f"{value['kind']}: {value['test']}"
                    else:
                        detail = value["value"] or "<bare return>"
                    lines.append(f"- line {value['line']}: `{detail}`")
                lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("artifacts/repo_ingestion/module_index.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/repo_ingestion"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    index_path = args.index if args.index.is_absolute() else workspace / args.index
    output_dir = args.output_dir if args.output_dir.is_absolute() else workspace / args.output_dir
    payload = extract(index_path, workspace)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ast_extraction.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "ast_extraction.md").write_text(
        render_markdown(payload), encoding="utf-8"
    )
    return 1 if payload["parse_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
