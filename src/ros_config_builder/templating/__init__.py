"""Curated interfaces, template definitions, and deterministic rendering."""

from .definition import TemplateManifest, build_template_definition, validate_template_definition, write_template_definition
from .renderer import (
    compare_system_models,
    reanalyze_generated_bundle,
    render_configuration_bundle,
    resolve_render_context,
    validate_public_values,
    validate_rendered_files,
)
from .schema import (
    TemplateConfigurationSchema,
    build_template_configuration_schema,
    curate_template_configuration_schema,
    load_template_configuration_policy,
    render_template_schema_report,
    write_template_configuration_schema,
)

__all__ = [name for name in globals() if not name.startswith("_")]
