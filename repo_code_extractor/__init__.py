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
from .ros_node_ir import build_module_ir, extract_ros_node_ir, write_ros_node_ir
from .ros_node_schema import Step1Payload, validate_step1_payload
from .deployment_extraction import (
    extract_launch_files,
    extract_parameter_yaml,
    write_deployment_artifacts,
)
from .system_model import build_system_model, extract_package_metadata, write_system_model
from .system_model_validation import (
    render_system_model_dot,
    render_system_model_report,
    validate_system_model,
    write_system_model_artifacts,
)
from .template_configuration import (
    TemplateConfigurationSchema,
    build_template_configuration_schema,
    curate_template_configuration_schema,
    load_template_configuration_policy,
    render_template_schema_report,
    write_template_configuration_schema,
)
from .template_definition import (
    TemplateManifest,
    build_template_definition,
    validate_template_definition,
    write_template_definition,
)
from .configuration_renderer import (
    compare_system_models,
    reanalyze_generated_bundle,
    render_configuration_bundle,
    resolve_render_context,
    validate_public_values,
    validate_rendered_files,
)
from .scenario_validation import validate_scenario_delta, write_scenario_validation

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
    "build_module_ir",
    "extract_ros_node_ir",
    "write_ros_node_ir",
    "Step1Payload",
    "validate_step1_payload",
    "extract_launch_files",
    "extract_parameter_yaml",
    "write_deployment_artifacts",
    "build_system_model",
    "extract_package_metadata",
    "write_system_model",
    "render_system_model_dot",
    "render_system_model_report",
    "validate_system_model",
    "write_system_model_artifacts",
    "TemplateConfigurationSchema",
    "build_template_configuration_schema",
    "curate_template_configuration_schema",
    "load_template_configuration_policy",
    "render_template_schema_report",
    "write_template_configuration_schema",
    "TemplateManifest",
    "build_template_definition",
    "validate_template_definition",
    "write_template_definition",
    "compare_system_models",
    "reanalyze_generated_bundle",
    "render_configuration_bundle",
    "resolve_render_context",
    "validate_public_values",
    "validate_rendered_files",
    "validate_scenario_delta",
    "write_scenario_validation",
]
