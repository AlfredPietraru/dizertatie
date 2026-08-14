"""Repository code ingestion and static information extraction."""

from .api import (
    PipelineResult,
    analyze,
    extract_code_information,
    extract_function_chunks,
    ingest_repository,
    write_chunk_artifacts,
    write_chunk_artifacts_from_roots,
)
from .parameter_context_compiler import compile_parameter_context, render_llm_context
from .parameter_graph_tools import (
    extract_parameter_artifacts,
    get_parameter_context,
    get_parameter_flow,
    get_parameter_relationship,
    repository_root,
    write_parameter_artifacts,
)

__all__ = [
    "PipelineResult",
    "analyze",
    "extract_code_information",
    "extract_function_chunks",
    "ingest_repository",
    "compile_parameter_context",
    "get_parameter_context",
    "extract_parameter_artifacts",
    "get_parameter_flow",
    "get_parameter_relationship",
    "render_llm_context",
    "repository_root",
    "write_parameter_artifacts",
    "write_chunk_artifacts",
    "write_chunk_artifacts_from_roots",
]
