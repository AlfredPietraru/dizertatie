"""Versioned schemas for launch and ROS parameter YAML extraction."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .ros import IRModel, SourceLocation


SCHEMA_VERSION = "1.0"


class LaunchFileIR(IRModel):
    id: str
    file: str
    arguments: list[dict[str, Any]] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    includes: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_expressions: list[dict[str, Any]] = Field(default_factory=list)


class LaunchPayload(IRModel):
    schema_version: str = SCHEMA_VERSION
    stage: Literal["ros_launch_static_extraction"]
    summary: dict[str, int]
    parse_failures: list[dict[str, str]]
    launch_files: list[LaunchFileIR]


class ParameterProfileIR(IRModel):
    id: str
    file: str
    node_selector: str
    namespace_selector: str | None = None
    parameters: dict[str, Any]
    flattened_parameters: dict[str, Any]
    source: SourceLocation


class ConfigurationPayload(IRModel):
    schema_version: str = SCHEMA_VERSION
    stage: Literal["ros_yaml_configuration_extraction"]
    summary: dict[str, int]
    parse_failures: list[dict[str, str]]
    profiles: list[ParameterProfileIR]
