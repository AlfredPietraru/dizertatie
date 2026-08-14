"""High-level callable API for repository ingestion and code extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .ast_extraction import extract, write_ast_artifacts
from .chunking import (
    extract_function_chunks,
    write_chunk_artifacts,
    write_chunk_artifacts_from_roots,
)
from .config_detection import detect, write_detection_artifacts
from .ingestion import DEFAULT_SOURCE_ROOTS, index_repository, write_artifacts
from .static_analysis import analyze
from .value_flow import analyze_value_flow, write_value_flow_artifacts


@dataclass(frozen=True)
class PipelineResult:
    """Artifacts and summaries produced by a complete extraction run."""

    workspace: Path
    output_directory: Path
    artifacts: dict[str, Path | bool]
    summaries: dict[str, Any]


def _paths(values: Iterable[str | Path] | None) -> list[Path]:
    return [Path(value) for value in values] if values is not None else list(DEFAULT_SOURCE_ROOTS)


def ingest_repository(
    workspace: str | Path,
    *,
    source_roots: Iterable[str | Path] | None = None,
    output_directory: str | Path = "artifacts/repo_ingestion",
) -> dict[str, Any]:
    """Index modules, imports, and top-level definitions and write artifacts."""
    workspace = Path(workspace).resolve()
    roots = _paths(source_roots)
    output = Path(output_directory)
    if not output.is_absolute():
        output = workspace / output
    modules, failures, skipped_roots = index_repository(workspace, roots)
    write_artifacts(output, workspace, roots, modules, failures, skipped_roots)
    return {
        "modules": modules,
        "parse_failures": failures,
        "skipped_roots": skipped_roots,
        "index_path": output / "module_index.json",
        "report_path": output / "module_index.md",
    }


def extract_code_information(
    workspace: str | Path,
    *,
    source_roots: Iterable[str | Path] | None = None,
    output_directory: str | Path = "artifacts/repo_ingestion",
    include_chunks: bool = True,
    include_static_analysis: bool = True,
) -> PipelineResult:
    """Run ingestion, AST, configuration, value-flow, and optional chunk stages."""
    workspace = Path(workspace).resolve()
    output = Path(output_directory)
    if not output.is_absolute():
        output = workspace / output
    roots = _paths(source_roots)

    ingestion = ingest_repository(
        workspace, source_roots=roots, output_directory=output
    )
    ast_payload = extract(ingestion["index_path"], workspace)
    ast_json, ast_markdown = write_ast_artifacts(ast_payload, output)
    detection_payload = detect(ast_json, workspace)
    detection_json, detection_markdown = write_detection_artifacts(
        detection_payload, output
    )
    value_payload, graph, graph_roots = analyze_value_flow(
        ast_payload, detection_payload
    )
    graph_artifacts = write_value_flow_artifacts(
        value_payload, graph, graph_roots, output
    )

    artifacts: dict[str, Path | bool] = {
        "module_index": ingestion["index_path"],
        "module_report": ingestion["report_path"],
        "ast": ast_json,
        "ast_report": ast_markdown,
        "configuration_sources": detection_json,
        "configuration_report": detection_markdown,
        **{f"value_flow_{key}": value for key, value in graph_artifacts.items()},
    }

    if include_static_analysis:
        static_output = output / "static_analysis"
        summary_path, tree_path = analyze(workspace, static_output)
        artifacts.update(
            {"static_analysis": summary_path, "source_tree": tree_path}
        )

    if include_chunks:
        chunks_output = output / "function_chunks"
        chunk_roots = [workspace / root for root in roots]
        chunks_path, statistics_path = write_chunk_artifacts_from_roots(
            chunk_roots, chunks_output, project_root=workspace
        )
        artifacts.update(
            {"function_chunks": chunks_path, "chunk_statistics": statistics_path}
        )

    return PipelineResult(
        workspace=workspace,
        output_directory=output,
        artifacts=artifacts,
        summaries={
            "ingestion": {
                "modules": len(ingestion["modules"]),
                "parse_failures": len(ingestion["parse_failures"]),
                "skipped_roots": ingestion["skipped_roots"],
            },
            "ast": ast_payload["summary"],
            "configuration": detection_payload["summary"],
            "value_flow": value_payload["summary"],
        },
    )


__all__ = [
    "PipelineResult",
    "analyze",
    "extract_code_information",
    "extract_function_chunks",
    "ingest_repository",
    "write_chunk_artifacts",
    "write_chunk_artifacts_from_roots",
]
