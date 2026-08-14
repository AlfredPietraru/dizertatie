#!/usr/bin/env python3
"""Create a shallow Python module/import index for selected repository roots.

This is intentionally only the first ingestion pass.  It records modules,
top-level imports, and top-level function/class definitions without analyzing
function bodies, library internals, or value flow.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_SOURCE_ROOTS = (
    Path("src/antrobot_ros"),
    Path("src/antrobot_description"),
    Path("src/kiss-icp"),
    Path("src/kinematic-icp"),
)

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


@dataclass(frozen=True)
class ImportRecord:
    kind: str
    module: str | None
    names: list[str]
    level: int
    line: int


@dataclass(frozen=True)
class DefinitionRecord:
    kind: str
    name: str
    line: int
    end_line: int | None


@dataclass(frozen=True)
class ModuleRecord:
    package_root: str
    path: str
    module: str
    imports: list[ImportRecord]
    definitions: list[DefinitionRecord]


@dataclass(frozen=True)
class ParseFailure:
    path: str
    error: str


def python_files(source_root: Path) -> Iterable[Path]:
    """Yield Python files below an explicitly selected repository root."""
    for path in sorted(source_root.rglob("*.py")):
        relative_parts = path.relative_to(source_root).parts
        if not any(part in IGNORED_DIRECTORIES for part in relative_parts):
            yield path


def module_name(path: Path, source_root: Path) -> str:
    """Return a stable dotted label relative to the selected package root."""
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or source_root.name.replace("-", "_")


def imports_from(tree: ast.Module) -> list[ImportRecord]:
    """Collect imports without following or parsing imported libraries."""
    imports: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.append(
                ImportRecord(
                    kind="import",
                    module=None,
                    names=[
                        f"{alias.name} as {alias.asname}"
                        if alias.asname
                        else alias.name
                        for alias in node.names
                    ],
                    level=0,
                    line=node.lineno,
                )
            )
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                ImportRecord(
                    kind="from",
                    module=node.module,
                    names=[
                        f"{alias.name} as {alias.asname}"
                        if alias.asname
                        else alias.name
                        for alias in node.names
                    ],
                    level=node.level,
                    line=node.lineno,
                )
            )
    return sorted(imports, key=lambda item: (item.line, item.kind, item.module or ""))


def definitions_from(tree: ast.Module) -> list[DefinitionRecord]:
    """Collect only definitions directly owned by the module."""
    definitions: list[DefinitionRecord] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
        elif isinstance(node, ast.ClassDef):
            kind = "class"
        else:
            continue
        definitions.append(
            DefinitionRecord(
                kind=kind,
                name=node.name,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", None),
            )
        )
    return definitions


def index_repository(
    workspace: Path, source_roots: Iterable[Path]
) -> tuple[list[ModuleRecord], list[ParseFailure], list[str]]:
    """Index Python files under selected, workspace-local source roots."""
    modules: list[ModuleRecord] = []
    failures: list[ParseFailure] = []
    skipped_roots: list[str] = []

    for configured_root in source_roots:
        source_root = (workspace / configured_root).resolve()
        try:
            source_root.relative_to(workspace)
        except ValueError as error:
            raise ValueError(f"Source root is outside workspace: {source_root}") from error
        if not source_root.is_dir():
            skipped_roots.append(configured_root.as_posix())
            continue

        for path in python_files(source_root):
            relative_path = path.relative_to(workspace).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
            except (OSError, SyntaxError, UnicodeError) as error:
                failures.append(ParseFailure(path=relative_path, error=str(error)))
                continue
            modules.append(
                ModuleRecord(
                    package_root=configured_root.as_posix(),
                    path=relative_path,
                    module=module_name(path, source_root),
                    imports=imports_from(tree),
                    definitions=definitions_from(tree),
                )
            )

    modules.sort(key=lambda item: item.path)
    failures.sort(key=lambda item: item.path)
    return modules, failures, skipped_roots


def render_markdown(modules: list[ModuleRecord]) -> str:
    """Render the compact human-readable module index requested for pass one."""
    lines = ["# Python module index", ""]
    current_root: str | None = None
    for module in modules:
        if module.package_root != current_root:
            current_root = module.package_root
            lines.extend([f"## `{current_root}`", ""])
        lines.extend([f"### `{module.path}`", "", f"Module: `{module.module}`", ""])
        lines.append("Defines:")
        if module.definitions:
            lines.extend(
                f"- `{definition.name}` ({definition.kind}, line {definition.line})"
                for definition in module.definitions
            )
        else:
            lines.append("- None")
        lines.extend(["", "Imports:"])
        if module.imports:
            for imported in module.imports:
                if imported.kind == "from":
                    prefix = "." * imported.level
                    source = prefix + (imported.module or "")
                    statement = f"from {source} import {', '.join(imported.names)}"
                else:
                    statement = f"import {', '.join(imported.names)}"
                lines.append(f"- `{statement}` (line {imported.line})")
        else:
            lines.append("- None")
        lines.append("")
    return "\n".join(lines)


def write_artifacts(
    output_dir: Path,
    workspace: Path,
    source_roots: list[Path],
    modules: list[ModuleRecord],
    failures: list[ParseFailure],
    skipped_roots: list[str],
) -> None:
    """Write machine-readable and human-readable ingestion artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "schema_version": 1,
        "stage": "module_and_import_indexing",
        "generated_at": generated_at,
        "workspace": workspace.as_posix(),
        "source_roots": [root.as_posix() for root in source_roots],
        "scope_policy": {
            "python_only": True,
            "follow_imports": False,
            "include_library_internals": False,
            "definitions": "top-level functions and classes only",
        },
        "summary": {
            "modules": len(modules),
            "imports": sum(len(module.imports) for module in modules),
            "definitions": sum(len(module.definitions) for module in modules),
            "parse_failures": len(failures),
        },
        "skipped_roots": skipped_roots,
        "parse_failures": [asdict(failure) for failure in failures],
        "modules": [asdict(module) for module in modules],
    }
    (output_dir / "module_index.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "module_index.md").write_text(
        render_markdown(modules), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace containing the selected source roots (default: cwd)",
    )
    parser.add_argument(
        "--source-root",
        action="append",
        type=Path,
        dest="source_roots",
        help="Workspace-relative source root; may be repeated",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/repo_ingestion"),
        help="Workspace-relative output directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    source_roots = args.source_roots or list(DEFAULT_SOURCE_ROOTS)
    modules, failures, skipped_roots = index_repository(workspace, source_roots)
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = workspace / output_dir
    write_artifacts(
        output_dir,
        workspace,
        source_roots,
        modules,
        failures,
        skipped_roots,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
