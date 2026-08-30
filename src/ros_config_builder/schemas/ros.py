"""Pydantic contract for the versioned Python ROS Step 1 output.

Entity models intentionally allow extension fields: the schema fixes the
shared contract while individual ROS entity kinds retain their specific data.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class IRModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourceLocation(IRModel):
    file: str
    line_start: int | None
    column_start: int | None = None
    line_end: int | None = None
    column_end: int | None = None


class Resolution(IRModel):
    status: Literal["resolved", "partial", "unresolved"]
    method: str
    reason: str | None = None


class ExpressionValue(IRModel):
    expression: str | None
    resolved_value: Any = None
    resolved: bool
    dependencies: list[str] = Field(default_factory=list)
    resolution: Resolution


class ImportIR(IRModel):
    qualified_name: str
    kind: Literal["module", "symbol"]


class ModuleMetadata(IRModel):
    file_path: str
    module_name: str
    package: str | None = None


class ModuleIR(IRModel):
    module: ModuleMetadata
    imports: dict[str, ImportIR]
    global_assignments: list[dict[str, Any]] = Field(default_factory=list)
    classes: list[dict[str, Any]] = Field(default_factory=list)
    functions: list[dict[str, Any]] = Field(default_factory=list)
    entrypoints: list[dict[str, Any]] = Field(default_factory=list)
    ros_factories: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_symbols: list[dict[str, Any]] = Field(default_factory=list)


class ROSNodeIR(IRModel):
    id: str
    class_name: str
    qualified_class: str
    ros_node_status: Literal["confirmed", "possible"] = "confirmed"
    node_name: ExpressionValue
    source: SourceLocation
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    publishers: list[dict[str, Any]] = Field(default_factory=list)
    subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    services: list[dict[str, Any]] = Field(default_factory=list)
    clients: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    timers: list[dict[str, Any]] = Field(default_factory=list)
    qos_profiles: list[dict[str, Any]] = Field(default_factory=list)
    tf_entities: list[dict[str, Any]] = Field(default_factory=list)
    functions: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_expressions: list[dict[str, Any]] = Field(default_factory=list)


class Step1Payload(IRModel):
    stage: Literal["python_ros_static_extraction"]
    boundary: dict[str, bool]
    summary: dict[str, int]
    parse_failures: list[dict[str, str]]
    modules: list[ModuleIR]
    nodes: list[ROSNodeIR]


def validate_step1_payload(payload: dict[str, Any]) -> Step1Payload:
    """Validate and normalize a complete Step 1 payload."""
    return Step1Payload.model_validate(payload)
