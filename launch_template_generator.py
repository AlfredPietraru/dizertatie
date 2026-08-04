#!/usr/bin/env python3
"""Generate a ROS 2 Python launch file from JSON data and a Jinja template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "launch_templates" / "joint_state_estimator.json"
TEMPLATE_DIRECTORY = PROJECT_ROOT / "launch_templates"
OUTPUT_DIRECTORY = PROJECT_ROOT / "artifacts" / "launch_files"
REQUIRED_FIELDS = {
    "template",
    "output_filename",
    "launch_arguments",
    "computed_paths",
    "parameter_loaders",
    "parameter_extractions",
    "nodes",
    "included_launch_files",
    "launch_description",
}


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and minimally validate a launch-file description."""
    with config_path.open(encoding="utf-8") as stream:
        config = json.load(stream)

    missing_fields = sorted(REQUIRED_FIELDS - config.keys())
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise ValueError(f"Missing required JSON fields: {missing}")

    output_filename = Path(config["output_filename"])
    if output_filename.name != str(output_filename):
        raise ValueError("output_filename must be a filename, not a path")

    for argument in config["launch_arguments"]:
        default = argument.get("default")
        if not isinstance(default, dict):
            continue
        if default.get("type") != "path_join_substitution":
            raise ValueError(
                f"Unsupported default type for {argument.get('name')}: "
                f"{default.get('type')}"
            )
        parts = default.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(
                f"Path default for {argument.get('name')} requires non-empty parts"
            )
        for part in parts:
            if isinstance(part, str):
                continue
            if (
                not isinstance(part, dict)
                or part.get("type") != "find_package_share"
                or not isinstance(part.get("package"), str)
            ):
                raise ValueError(
                    f"Invalid path part for launch argument {argument.get('name')}"
                )

    for include in config["included_launch_files"]:
        source = include.get("source")
        if source is None:
            continue
        if source.get("type") != "path_join_substitution":
            raise ValueError(
                f"Unsupported include source type for {include.get('result_name')}: "
                f"{source.get('type')}"
            )
        parts = source.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(
                f"Include source for {include.get('result_name')} requires non-empty parts"
            )
        for part in parts:
            if isinstance(part, str):
                continue
            if (
                not isinstance(part, dict)
                or part.get("type") != "find_package_share"
                or not isinstance(part.get("package"), str)
            ):
                raise ValueError(
                    f"Invalid path part for include {include.get('result_name')}"
                )

    for node in config["nodes"]:
        inline_parameters = node.get("inline_parameters")
        if inline_parameters is not None and not isinstance(inline_parameters, dict):
            raise ValueError(
                f"inline_parameters for {node.get('result_name')} must be an object"
            )
        for name, value in (inline_parameters or {}).items():
            if not isinstance(value, dict):
                continue
            if value.get("type") == "variable" and isinstance(value.get("value"), str):
                continue
            if (
                value.get("type") == "launch_configuration"
                and isinstance(value.get("name"), str)
            ):
                continue
            raise ValueError(
                f"Invalid inline parameter value for {node.get('result_name')}.{name}"
            )

    for document in config.get("xacro_documents", []):
        if not isinstance(document.get("result_name"), str) or not isinstance(
            document.get("path_variable"), str
        ):
            raise ValueError("Xacro documents require result_name and path_variable")

    return config


def generate_launch_file(config_path: Path, output_directory: Path) -> Path:
    """Render one launch file and return its output path."""
    config = load_config(config_path)
    template_name = config.get("template", "generator_launch_file.py.j2")

    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIRECTORY),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    rendered_launch_file = environment.get_template(template_name).render(**config)

    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / config["output_filename"]
    output_path.write_text(rendered_launch_file, encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a ROS 2 Python launch file using Jinja and JSON."
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"JSON configuration (default: {DEFAULT_CONFIG.relative_to(PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIRECTORY,
        help="directory for generated launch files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output_path = generate_launch_file(args.config.resolve(), args.output_dir)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Error: {error}")
        return 1

    print(f"Generated {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
