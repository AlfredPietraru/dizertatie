"""Mission contracts, capability realization, and semantic configuration mapping.

Capability interpretation stays sparse, while parameter interpretation uses the
complete source-derived configuration catalogue. All structured model outputs
reject unknown fields instead of silently ignoring them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from ..templating import validate_configuration_values


class MissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


MappingSelection = Literal["cartographer"] | None
NavigationSelection = Literal["nav2"] | None
ExplorationSelection = Literal["explore_lite"] | None
OdometrySelection = Literal["kinematic_icp", "kiss_icp"] | None


class CapabilitySelections(MissionModel):
    """Complete capability state: implementation string means active; null means disabled."""

    mapping: MappingSelection
    navigation: NavigationSelection
    exploration: ExplorationSelection
    odometry: OdometrySelection

    @classmethod
    def defaults(cls) -> "CapabilitySelections":
        return cls(
            mapping="cartographer", navigation="nav2",
            exploration="explore_lite", odometry="kinematic_icp",
        )


class MissionInterpretation(MissionModel):
    """Outcome-aware capability interpretation boundary."""

    status: Literal["valid", "unsupported", "needs_clarification"] = Field(
        description=(
            "Capability-stage outcome. Parameter-only requests are valid; unsupported is "
            "reserved for an explicitly requested capability or implementation absent from "
            "the capability registry."
        ),
    )
    capabilities: CapabilitySelections | None = Field(
        default=None,
        description=(
            "Sparse high-level capability changes. Use an empty object for a valid "
            "parameter-only request that does not change capabilities."
        ),
    )
    reason: str | None = None
    clarification_question: str | None = None

    @model_validator(mode="after")
    def check_status_contract(self) -> "MissionInterpretation":
        if self.status == "valid":
            if self.capabilities is None:
                raise ValueError("valid interpretations require capabilities")
            required = {"mapping", "navigation", "exploration", "odometry"}
            if self.capabilities.model_fields_set != required:
                raise ValueError("valid interpretations require all four capability fields")
            if self.clarification_question is not None:
                raise ValueError("valid interpretations cannot ask a clarification question")
        elif self.status == "unsupported":
            if self.capabilities is not None:
                raise ValueError("unsupported interpretations cannot contain capabilities")
            if not self.reason:
                raise ValueError("unsupported interpretations require a reason")
            if self.clarification_question is not None:
                raise ValueError("unsupported interpretations cannot ask a clarification question")
        else:
            if self.capabilities is not None:
                raise ValueError("needs_clarification interpretations cannot contain capabilities")
            if not self.reason or not self.clarification_question:
                raise ValueError("needs_clarification requires a reason and clarification question")
        if self.capabilities is not None:
            if self.capabilities.exploration is not None and self.capabilities.mapping is None:
                raise ValueError("exploration requires mapping; mapping cannot be explicitly disabled")
        return self


RendererValue = bool | int | float | str | list[Any] | dict[str, Any]
ComponentReason = Literal[
    "selected_implementation", "required_dependency", "explicit_disable", "default_realization",
    "displaced_implementation", "required_capability",
]
CapabilityReason = Literal[
    "explicit_selection", "explicit_disable", "baseline_preserved", "required_capability",
]
RendererReason = Literal["implementation_enable", "explicit_disable", "required_capability"]


class ImplementationComponent(MissionModel):
    component_id: str
    dependency: bool = False


class RequiredConnection(MissionModel):
    connection_id: str
    source_component: str
    target_component: str
    wiring_binding: str
    interface_type: str
    source_role: Literal["publisher"] = "publisher"
    target_role: Literal["subscription"] = "subscription"
    semantic_interface: str


class ImplementationReference(MissionModel):
    capability: str
    implementation: str


class CapabilityImplementation(MissionModel):
    description: str
    characteristics: dict[str, str | list[str]] = Field(default_factory=dict)
    components: list[ImplementationComponent]
    required_connections: list[RequiredConnection] = Field(default_factory=list)
    realization: dict[str, RendererValue]
    conflicts_with: list[ImplementationReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_unique_members(self) -> "CapabilityImplementation":
        component_ids = [item.component_id for item in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("implementation component IDs must be unique")
        connection_ids = [item.connection_id for item in self.required_connections]
        if len(connection_ids) != len(set(connection_ids)):
            raise ValueError("implementation connection IDs must be unique")
        return self


class CapabilityDefinition(MissionModel):
    description: str
    default_implementation: str
    disable_overrides: dict[str, RendererValue]
    component_family: list[str]
    implementations: dict[str, CapabilityImplementation]
    requires_capabilities: list[str] = Field(default_factory=list)
    conflicts_with_capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_default_exists(self) -> "CapabilityDefinition":
        if self.default_implementation not in self.implementations:
            raise ValueError(f"unknown default implementation {self.default_implementation!r}")
        if len(self.component_family) != len(set(self.component_family)):
            raise ValueError("capability component family IDs must be unique")
        family = set(self.component_family)
        for name, implementation in self.implementations.items():
            undeclared = {item.component_id for item in implementation.components} - family
            if undeclared:
                raise ValueError(
                    f"implementation {name!r} uses components outside its family: {sorted(undeclared)}"
                )
        return self


class AntRobotCapabilityRegistry(MissionModel):
    schema_version: Literal["2.0"] = "2.0"
    robot: Literal["antrobot"] = "antrobot"
    baseline_components: list[str]
    capabilities: dict[str, CapabilityDefinition]

    @model_validator(mode="after")
    def check_capability_contract(self) -> "AntRobotCapabilityRegistry":
        capability_names = set(self.capabilities)
        if not capability_names:
            raise ValueError("registry must define at least one capability")
        if len(self.baseline_components) != len(set(self.baseline_components)):
            raise ValueError("baseline component IDs must be unique")
        connection_owners: dict[str, str] = {}
        for capability_name, definition in self.capabilities.items():
            unknown_constraints = (
                set(definition.requires_capabilities)
                | set(definition.conflicts_with_capabilities)
            ) - capability_names
            if unknown_constraints:
                raise ValueError(
                    f"capability {capability_name!r} references unknown capabilities: "
                    f"{sorted(unknown_constraints)}"
                )
            if capability_name in definition.requires_capabilities:
                raise ValueError(f"capability {capability_name!r} cannot require itself")
            if capability_name in definition.conflicts_with_capabilities:
                raise ValueError(f"capability {capability_name!r} cannot conflict with itself")
            for implementation_name, implementation in definition.implementations.items():
                for reference in implementation.conflicts_with:
                    target = self.capabilities.get(reference.capability)
                    if target is None or reference.implementation not in target.implementations:
                        raise ValueError(
                            f"{capability_name}.{implementation_name} conflicts with unknown "
                            f"implementation {reference.capability}.{reference.implementation}"
                        )
                for connection in implementation.required_connections:
                    owner = f"{capability_name}.{implementation_name}"
                    previous = connection_owners.setdefault(connection.connection_id, owner)
                    if previous != owner:
                        raise ValueError(
                            f"connection ID {connection.connection_id!r} is shared by "
                            f"{previous} and {owner}"
                        )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"capability dependency cycle includes {name!r}")
            if name in visited:
                return
            visiting.add(name)
            for required in self.capabilities[name].requires_capabilities:
                visit(required)
            visiting.remove(name)
            visited.add(name)

        for capability_name in sorted(capability_names):
            visit(capability_name)
        return self


def load_capability_registry(path: str | Path) -> AntRobotCapabilityRegistry:
    """Load the JSON-compatible registry artifact and validate its complete contract."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    if version != "2.0":
        raise ValueError(f"unsupported capability registry schema version {version!r}")
    return AntRobotCapabilityRegistry.model_validate(raw)


def validate_capability_registry(
    registry: AntRobotCapabilityRegistry,
    system_model: dict[str, Any],
    renderer_schema: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Validate registry references against the frozen system and renderer artifacts."""
    known_components = {
        value
        for instance in system_model.get("deployment_instances", [])
        for value in (instance.get("effective_node_name"), instance.get("executable"))
        if isinstance(value, str) and value
    }
    renderer_keys = set(renderer_schema.get("configuration_keys", {}))
    known_components.update(
        key.removeprefix("launch.launch_").removeprefix("launch.enable_")
        for key in renderer_keys
        if key.startswith(("launch.launch_", "launch.enable_"))
    )
    declared_components = set(registry.baseline_components)
    for definition in registry.capabilities.values():
        declared_components.update(definition.component_family)
    unknown_components = declared_components - known_components
    if unknown_components:
        raise ValueError(f"registry references unknown system components: {sorted(unknown_components)}")

    wiring_bindings = set(manifest.get("wiring_bindings", {}))
    for capability_name, definition in registry.capabilities.items():
        sources = [("disable overrides", definition.disable_overrides)] + [
            (implementation_name, implementation.realization)
            for implementation_name, implementation in definition.implementations.items()
        ]
        for source, overrides in sources:
            unknown_keys = set(overrides) - renderer_keys
            if unknown_keys:
                raise ValueError(
                    f"{capability_name}.{source} references unknown renderer keys: {sorted(unknown_keys)}"
                )
        for implementation_name, implementation in definition.implementations.items():
            for connection in implementation.required_connections:
                unknown_endpoints = {
                    connection.source_component, connection.target_component,
                } - known_components
                if unknown_endpoints:
                    raise ValueError(
                        f"{capability_name}.{implementation_name} connection "
                        f"{connection.connection_id!r} references unknown components: "
                        f"{sorted(unknown_endpoints)}"
                    )
                if connection.wiring_binding not in wiring_bindings:
                    raise ValueError(
                        f"{capability_name}.{implementation_name} connection "
                        f"{connection.connection_id!r} references unknown wiring binding "
                        f"{connection.wiring_binding!r}"
                    )


def capability_prompt_catalogue(registry: AntRobotCapabilityRegistry) -> dict[str, Any]:
    """Return only semantic catalogue data safe to expose to the LLM."""
    return {
        "robot": registry.robot,
        "capabilities": {
            capability_name: {
                "description": capability.description,
                "default_implementation": capability.default_implementation,
                "implementations": {
                    name: {
                        "description": implementation.description,
                        "characteristics": implementation.characteristics,
                    }
                    for name, implementation in capability.implementations.items()
                },
            }
            for capability_name, capability in registry.capabilities.items()
        },
    }


class TemplateConfigurationPlan(MissionModel):
    """Output passed to the existing user-value renderer layer."""

    user_values: dict[str, RendererValue] = Field(default_factory=dict)


class RealizedComponent(MissionModel):
    component_id: str
    enabled: bool
    reason: ComponentReason
    source_capability: str | None = None
    source_implementation: str | None = None
    claims: list["ComponentClaim"] = Field(default_factory=list)


class ComponentClaim(MissionModel):
    component_id: str
    enabled: bool
    reason: ComponentReason
    source_capability: str | None = None
    source_implementation: str | None = None


class RendererClaim(MissionModel):
    key: str
    value: RendererValue
    source_capability: str
    source_implementation: str | None = None
    reason: RendererReason


class RealizedRendererValue(MissionModel):
    key: str
    value: RendererValue
    sources: list[RendererClaim]


class RealizedCapability(MissionModel):
    capability: str
    implementation: str | None
    enabled: bool
    reason: CapabilityReason = "explicit_selection"
    user_mentioned: bool = True


class SystemRealization(MissionModel):
    capabilities: list[RealizedCapability] = Field(default_factory=list)
    components: list[RealizedComponent] = Field(default_factory=list)
    renderer_values: dict[str, RendererValue] = Field(default_factory=dict)
    renderer_overrides: list[RealizedRendererValue] = Field(default_factory=list)

    @computed_field
    @property
    def active_capabilities(self) -> list[RealizedCapability]:
        return [capability for capability in self.capabilities if capability.enabled]

    @property
    def active_component_ids(self) -> set[str]:
        return {component.component_id for component in self.components if component.enabled}


class ActiveDeploymentComponent(MissionModel):
    component_id: str
    resolution_status: Literal["resolved_local", "external", "unresolved"]
    instance_id: str | None = None
    interfaces: list[dict[str, Any]] = Field(default_factory=list)


class ResolvedWiringBinding(MissionModel):
    binding_id: str
    semantic_role: str
    effective_value: RendererValue
    targets: list[str]
    active_targets: list[str]
    affected_interfaces: list[str]


class ROSConnection(MissionModel):
    connection_id: str
    source_component: str
    source_interface_id: str | None = None
    target_component: str
    target_interface_id: str | None = None
    effective_name: str
    interface_type: str
    controlling_bindings: list[str] = Field(default_factory=list)
    required: bool
    requirement_source: str | None = None
    validation_status: Literal["confirmed", "missing", "external_unknown"]


class QoSCompatibilityResult(MissionModel):
    connection_id: str
    status: Literal["compatible", "incompatible", "unknown"]
    detail: str


class ROSOrchestrationPlan(MissionModel):
    status: Literal["valid", "invalid", "partially_verified"]
    active_components: list[ActiveDeploymentComponent]
    inactive_components: list[str]
    required_connections: list[ROSConnection]
    discovered_connections: list[ROSConnection]
    wiring_bindings: list[ResolvedWiringBinding]
    qos_checks: list[QoSCompatibilityResult]

    @property
    def missing_required_connections(self) -> list[ROSConnection]:
        return [item for item in self.required_connections if item.validation_status == "missing"]

    @property
    def unresolved_external_requirements(self) -> list[ROSConnection]:
        return [item for item in self.required_connections if item.validation_status == "external_unknown"]


class LLMSemanticRelationship(MissionModel):
    target_parameter_id: str
    relation: Literal[
        "same_physical_property", "interface_consistency",
        "behaviorally_related", "semantic_alternative",
    ]
    requires_joint_update: bool = False
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list, exclude=True)


class LLMParameterMetadata(MissionModel):
    """Semantic parameter metadata generated by the enrichment LLM."""

    description: str
    aliases: list[str] = Field(default_factory=list)
    user_expressions: list[str] = Field(default_factory=list)
    physical_quantity: str | None = None
    unit: str | None = None
    semantic_category: str | None = None
    behavioral_effect: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    relationships: list[LLMSemanticRelationship] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class AvailableParameter(MissionModel):
    parameter_id: str
    component_id: str | None = None
    affected_components: list[str] = Field(default_factory=list)
    kind: Literal["ros_parameter", "launch_argument"] = "ros_parameter"
    semantic_name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    user_expressions: list[str] = Field(default_factory=list)
    value_type: Literal["boolean", "integer", "number", "string", "array", "object", "unknown"]
    current_value: RendererValue
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: list[RendererValue] | None = None
    unit: str | None = None
    physical_quantity: str | None = None
    semantic_category: str | None = None
    behavioral_effect: str | None = None
    constraints: list[str] = Field(default_factory=list)
    relationships: list[dict[str, str]] = Field(default_factory=list)
    semantic_relationships: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] | None = None
    semantic_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    llm_semantic_metadata: LLMParameterMetadata | None = None


class ParameterCatalogue(MissionModel):
    active_components: list[str]
    parameters: list[AvailableParameter]
    collisions: dict[str, list[str]] = Field(default_factory=dict)


class ParameterChange(MissionModel):
    parameter_id: str
    value: RendererValue
    evidence: str | None = None


def realize_capabilities(
    selections: CapabilitySelections,
    registry: AntRobotCapabilityRegistry,
) -> SystemRealization:
    """Resolve complete nullable implementation choices into components and renderer inputs."""
    component_claims: dict[str, list[ComponentClaim]] = {}
    renderer_claims: dict[str, list[RendererClaim]] = {}
    baseline = set(registry.baseline_components)

    def claim_component(claim: ComponentClaim) -> None:
        component_claims.setdefault(claim.component_id, []).append(claim)

    def claim_renderer(claim: RendererClaim) -> None:
        renderer_claims.setdefault(claim.key, []).append(claim)

    for component_id in sorted(baseline):
        claim_component(ComponentClaim(
            component_id=component_id, enabled=True, reason="default_realization",
        ))

    states: dict[str, dict[str, Any]] = {}
    for capability_name, definition in registry.capabilities.items():
        selection = getattr(selections, capability_name, None)
        enabled = selection is not None
        implementation_name = selection
        states[capability_name] = {
            "enabled": enabled,
            "implementation": implementation_name,
            "user_mentioned": True,
            "reason": "explicit_selection" if enabled else "explicit_disable",
            "derived": False,
        }

    def require_dependencies(capability_name: str) -> None:
        state = states[capability_name]
        if not state["enabled"]:
            return
        for required in registry.capabilities[capability_name].requires_capabilities:
            required_state = states[required]
            if required_state["user_mentioned"] and not required_state["enabled"]:
                raise ValueError(
                    f"capability {capability_name!r} requires explicitly disabled capability {required!r}"
                )
            if not required_state["enabled"]:
                required_state.update(
                    enabled=True,
                    implementation=registry.capabilities[required].default_implementation,
                    reason="required_capability",
                    derived=True,
                )
            require_dependencies(required)

    for capability_name in sorted(registry.capabilities):
        require_dependencies(capability_name)

    active = {name for name, state in states.items() if state["enabled"]}
    for capability_name in sorted(active):
        definition = registry.capabilities[capability_name]
        conflicts = active & set(definition.conflicts_with_capabilities)
        if conflicts:
            raise ValueError(
                f"enabled capability {capability_name!r} conflicts with {sorted(conflicts)}"
            )
        implementation_name = states[capability_name]["implementation"]
        if implementation_name is None:
            continue
        implementation = definition.implementations[implementation_name]
        for reference in implementation.conflicts_with:
            target = states[reference.capability]
            if target["enabled"] and target["implementation"] == reference.implementation:
                raise ValueError(
                    f"implementation {capability_name}.{implementation_name} conflicts with "
                    f"{reference.capability}.{reference.implementation}"
                )

    capabilities: list[RealizedCapability] = []
    for capability_name in sorted(registry.capabilities):
        definition = registry.capabilities[capability_name]
        state = states[capability_name]
        capabilities.append(RealizedCapability(
            capability=capability_name,
            implementation=state["implementation"] if state["enabled"] else None,
            enabled=state["enabled"], reason=state["reason"],
            user_mentioned=state["user_mentioned"],
        ))
        if not state["user_mentioned"] and not state["derived"]:
            continue
        if not state["enabled"]:
            for key, value in definition.disable_overrides.items():
                claim_renderer(RendererClaim(
                    key=key, value=value, source_capability=capability_name,
                    reason="explicit_disable",
                ))
            for component_id in definition.component_family:
                claim_component(ComponentClaim(
                    component_id=component_id, enabled=False, reason="explicit_disable",
                    source_capability=capability_name,
                ))
            continue
        implementation_name = state["implementation"]
        implementation = definition.implementations[implementation_name]
        renderer_reason: RendererReason = (
            "required_capability" if state["derived"] else "implementation_enable"
        )
        for key, value in implementation.realization.items():
            claim_renderer(RendererClaim(
                key=key, value=value, source_capability=capability_name,
                source_implementation=implementation_name, reason=renderer_reason,
            ))
        active_ids = {component.component_id for component in implementation.components}
        for component_id in definition.component_family:
            if component_id not in active_ids:
                claim_component(ComponentClaim(
                    component_id=component_id, enabled=False,
                    reason="displaced_implementation", source_capability=capability_name,
                    source_implementation=implementation_name,
                ))
        for component in implementation.components:
            reason: ComponentReason = (
                "required_capability" if state["derived"] else
                "required_dependency" if component.dependency else
                "selected_implementation" if state["user_mentioned"] else "default_realization"
            )
            claim_component(ComponentClaim(
                component_id=component.component_id, enabled=True, reason=reason,
                source_capability=capability_name, source_implementation=implementation_name,
            ))

    components: list[RealizedComponent] = []
    for component_id, claims in sorted(component_claims.items()):
        mission_claims = [claim for claim in claims if claim.source_capability is not None]
        effective_claims = mission_claims or claims
        states_for_component = {claim.enabled for claim in effective_claims}
        if len(states_for_component) != 1:
            detail = "; ".join(
                f"{claim.enabled} from {claim.source_capability or 'baseline'}"
                for claim in effective_claims
            )
            raise ValueError(f"component conflict for {component_id!r}: {detail}")
        representative = effective_claims[0]
        components.append(RealizedComponent(
            component_id=component_id, enabled=representative.enabled,
            reason=representative.reason, source_capability=representative.source_capability,
            source_implementation=representative.source_implementation, claims=claims,
        ))

    renderer_overrides: list[RealizedRendererValue] = []
    values: dict[str, RendererValue] = {}
    for key, claims in sorted(renderer_claims.items()):
        distinct = {json.dumps(claim.value, sort_keys=True) for claim in claims}
        if len(distinct) != 1:
            detail = "; ".join(
                f"{claim.value!r} from {claim.source_capability}"
                + (f".{claim.source_implementation}" if claim.source_implementation else "")
                + f" ({claim.reason})"
                for claim in claims
            )
            raise ValueError(f"renderer conflict for {key!r}: {detail}")
        values[key] = claims[0].value
        renderer_overrides.append(RealizedRendererValue(
            key=key, value=claims[0].value, sources=claims,
        ))
    return SystemRealization(
        capabilities=capabilities,
        components=components, renderer_values=values,
        renderer_overrides=renderer_overrides,
    )


def _component_for_target(target: str) -> str | None:
    parts = target.split(".", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "nodes" else None


def _qos_compatibility(connection_id: str, source: dict[str, Any], target: dict[str, Any]) -> QoSCompatibilityResult:
    source_qos, target_qos = source.get("qos"), target.get("qos")
    if not isinstance(source_qos, dict) or not isinstance(target_qos, dict):
        return QoSCompatibilityResult(
            connection_id=connection_id, status="unknown", detail="QoS is not modeled for both endpoints",
        )
    source_value = source_qos.get("resolved_value") if source_qos.get("resolved") else None
    target_value = target_qos.get("resolved_value") if target_qos.get("resolved") else None
    if not isinstance(source_value, dict) or not isinstance(target_value, dict):
        return QoSCompatibilityResult(
            connection_id=connection_id, status="unknown", detail="QoS expressions are unresolved",
        )
    mismatches = [
        policy for policy in ("reliability", "durability")
        if source_value.get(policy) is not None and target_value.get(policy) is not None
        and source_value[policy] != target_value[policy]
    ]
    return QoSCompatibilityResult(
        connection_id=connection_id,
        status="incompatible" if mismatches else "compatible",
        detail=f"incompatible policies: {', '.join(mismatches)}" if mismatches else "known policies are compatible",
    )


def resolve_ros_orchestration(
    realization: SystemRealization,
    registry: AntRobotCapabilityRegistry,
    system_model: dict[str, Any],
    manifest: dict[str, Any],
) -> ROSOrchestrationPlan:
    """Project the frozen ROS model onto active components and verify required topic wiring."""
    instances = system_model.get("deployment_instances", [])
    active_components: list[ActiveDeploymentComponent] = []
    active_instances: dict[str, dict[str, Any]] = {}
    for component_id in sorted(realization.active_component_ids):
        matches = [item for item in instances if component_id in {
            item.get("effective_node_name"), item.get("executable"),
        }]
        instance = matches[0] if len(matches) == 1 else None
        if instance is None:
            active_components.append(ActiveDeploymentComponent(
                component_id=component_id, resolution_status="unresolved",
            ))
            continue
        active_instances[component_id] = instance
        active_components.append(ActiveDeploymentComponent(
            component_id=component_id,
            resolution_status=instance.get("resolution_status", "unresolved"),
            instance_id=instance.get("instance_id"), interfaces=instance.get("interfaces", []),
        ))

    binding_values: dict[str, RendererValue] = {}
    wiring: list[ResolvedWiringBinding] = []
    for binding_id, binding in sorted(manifest.get("wiring_bindings", {}).items()):
        value = binding.get("baseline_value")
        binding_values[binding_id] = value
        targets = binding.get("parameter_keys", [])
        active_targets = [
            target for target in targets if _component_for_target(target) in realization.active_component_ids
        ]
        effective_name = f"/{str(value).lstrip('/')}"
        affected = [
            interface["id"]
            for component_id, instance in active_instances.items()
            for interface in instance.get("interfaces", [])
            if interface.get("effective_name") == effective_name
        ]
        wiring.append(ResolvedWiringBinding(
            binding_id=binding_id, semantic_role=binding_id.removeprefix("wiring."),
            effective_value=value, targets=targets, active_targets=active_targets,
            affected_interfaces=sorted(affected),
        ))

    discovered: list[ROSConnection] = []
    qos_checks: list[QoSCompatibilityResult] = []
    endpoints = [
        (component_id, interface)
        for component_id, instance in active_instances.items()
        for interface in instance.get("interfaces", [])
    ]
    for source_component, source in (item for item in endpoints if item[1].get("role") == "publisher"):
        for target_component, target in (item for item in endpoints if item[1].get("role") == "subscription"):
            if (source.get("effective_name"), source.get("type")) != (
                target.get("effective_name"), target.get("type"),
            ):
                continue
            connection_id = f"discovered:{source['id']}:{target['id']}"
            bindings = [
                item.binding_id for item in wiring
                if source["id"] in item.affected_interfaces or target["id"] in item.affected_interfaces
            ]
            discovered.append(ROSConnection(
                connection_id=connection_id, source_component=source_component,
                source_interface_id=source["id"], target_component=target_component,
                target_interface_id=target["id"], effective_name=source["effective_name"],
                interface_type=source["type"], controlling_bindings=sorted(set(bindings)),
                required=False, validation_status="confirmed",
            ))
            qos_checks.append(_qos_compatibility(connection_id, source, target))

    requirements: list[tuple[str, RequiredConnection]] = []
    for capability in realization.active_capabilities:
        if not capability.enabled or capability.implementation is None:
            continue
        implementation = registry.capabilities[capability.capability].implementations[capability.implementation]
        requirements.extend((f"{capability.capability}.{capability.implementation}", item)
                            for item in implementation.required_connections)

    required_connections: list[ROSConnection] = []
    for requirement_source, requirement in requirements:
        value = binding_values.get(requirement.wiring_binding)
        effective_name = f"/{str(value).lstrip('/')}" if value is not None else ""
        source_instance = active_instances.get(requirement.source_component)
        target_instance = active_instances.get(requirement.target_component)
        external = any(
            instance is None or instance.get("resolution_status") != "resolved_local"
            for instance in (source_instance, target_instance)
        )
        source_candidates = [] if source_instance is None else [
            item for item in source_instance.get("interfaces", [])
            if item.get("role") == requirement.source_role
            and item.get("effective_name") == effective_name
            and item.get("type") == requirement.interface_type
        ]
        target_candidates = [] if target_instance is None else [
            item for item in target_instance.get("interfaces", [])
            if item.get("role") == requirement.target_role
            and item.get("effective_name") == effective_name
            and item.get("type") == requirement.interface_type
        ]
        status = "external_unknown" if external else (
            "confirmed" if len(source_candidates) == 1 and len(target_candidates) == 1 else "missing"
        )
        connection = ROSConnection(
            connection_id=requirement.connection_id,
            source_component=requirement.source_component,
            source_interface_id=source_candidates[0]["id"] if len(source_candidates) == 1 else None,
            target_component=requirement.target_component,
            target_interface_id=target_candidates[0]["id"] if len(target_candidates) == 1 else None,
            effective_name=effective_name, interface_type=requirement.interface_type,
            controlling_bindings=[requirement.wiring_binding], required=True,
            requirement_source=requirement_source, validation_status=status,
        )
        required_connections.append(connection)
        if status == "confirmed":
            qos_checks.append(_qos_compatibility(
                requirement.connection_id, source_candidates[0], target_candidates[0],
            ))

    missing = any(item.validation_status == "missing" for item in required_connections)
    partial = any(item.validation_status == "external_unknown" for item in required_connections) or any(
        item.resolution_status != "resolved_local" for item in active_components
    )
    qos_invalid = any(item.status == "incompatible" for item in qos_checks)
    status = "invalid" if missing or qos_invalid else "partially_verified" if partial else "valid"
    return ROSOrchestrationPlan(
        status=status, active_components=active_components,
        inactive_components=sorted(
            item.component_id for item in realization.components if not item.enabled
        ),
        required_connections=required_connections, discovered_connections=discovered,
        wiring_bindings=wiring, qos_checks=qos_checks,
    )


def _merge_parameter_relationships(
    discovered: list[dict[str, str]],
    semantic_targets: list[str],
) -> list[dict[str, str]]:
    """Preserve source-derived links and add separately labelled LLM hypotheses."""
    relationships = [dict(item) for item in discovered]
    known = {(item.get("kind"), item.get("target")) for item in relationships}
    for target in semantic_targets:
        edge = ("semantic_related_parameter", target)
        if edge not in known:
            relationships.append({"kind": edge[0], "target": edge[1]})
            known.add(edge)
    return relationships


def derive_parameter_catalogue(
    realization: SystemRealization,
    template_schema: dict[str, Any],
    *,
    evidence_by_id: dict[str, dict[str, Any]] | None = None,
    semantic_enrichments: dict[str, dict[str, Any]] | None = None,
    include_inactive_components: bool = False,
    include_launch_arguments: bool = False,
) -> ParameterCatalogue:
    """Expose runtime parameters while leaving deployment toggles to capability realization."""
    evidence_by_id = evidence_by_id or {}
    semantic_enrichments = semantic_enrichments or {}
    active_components = realization.active_component_ids
    parameters: list[AvailableParameter] = []

    def llm_metadata(enrichment: dict[str, Any]) -> LLMParameterMetadata | None:
        if not enrichment:
            return None
        return LLMParameterMetadata.model_validate({
            "description": enrichment["description"],
            "aliases": enrichment.get("aliases", []),
            "user_expressions": enrichment.get("user_expressions", []),
            "physical_quantity": enrichment.get("physical_quantity"),
            "unit": enrichment.get("unit"),
            "semantic_category": enrichment.get("semantic_category"),
            "behavioral_effect": enrichment.get("behavioral_effect", []),
            "constraints": enrichment.get("constraints", []),
            "relationships": enrichment.get("relationships", []),
            "confidence": enrichment["confidence"],
        })
    launch_items = template_schema.get("launch_arguments", [])
    for item in launch_items:
        if item.get("value_type") == "boolean":
            # Boolean launch arguments enable or disable deployment components.
            # The capability stage owns those decisions and realizes their values
            # deterministically; exposing them again invites the parameter LLM to
            # duplicate or contradict an already-resolved capability selection.
            continue
        affected_components = set(item.get("affected_components", []))
        if affected_components and not affected_components & realization.active_component_ids:
            continue
        semantics = item.get("semantic_information") or {}
        value_range = item.get("range") or {}
        name = item["name"].replace("_", " ")
        enrichment = semantic_enrichments.get(item["template_key"], {})
        parameters.append(AvailableParameter(
            parameter_id=item["template_key"], kind="launch_argument",
            affected_components=list(item.get("affected_components", [])),
            semantic_name=f"launch {name}",
            description=enrichment.get("description") or semantics.get("description") or f"Launch argument {name}.",
            aliases=list(enrichment.get("aliases") or []),
            user_expressions=list(enrichment.get("user_expressions") or []),
            value_type=item["value_type"], current_value=item["current_value"],
            minimum=value_range.get("minimum"), maximum=value_range.get("maximum"),
            allowed_values=item.get("allowed_values"),
            unit=enrichment.get("unit") or semantics.get("unit"),
            physical_quantity=enrichment.get("physical_quantity"),
            semantic_category=enrichment.get("semantic_category"),
            behavioral_effect=("; ".join(enrichment.get("behavioral_effect", []))
                               or semantics.get("behavioral_effect")),
            constraints=list(enrichment.get("constraints") or semantics.get("constraints") or []),
            relationships=_merge_parameter_relationships(
                item.get("relationships", []), enrichment.get("related_parameters", []),
            ),
            semantic_relationships=list(enrichment.get("relationships") or []),
            evidence=evidence_by_id.get(item["template_key"]),
            semantic_confidence=enrichment.get("confidence"),
            llm_semantic_metadata=llm_metadata(enrichment),
        ))
    for component_id, items in sorted(template_schema.get("node_parameters", {}).items()):
        if component_id not in realization.active_component_ids:
            continue
        for item in items:
            affected_components = set(item.get("affected_components") or [component_id])
            if not include_inactive_components and not affected_components & active_components:
                continue
            semantics = item.get("semantic_information") or {}
            value_range = item.get("range") or {}
            name = item["name"].replace("_", " ")
            component_name = component_id.replace("_", " ")
            enrichment = semantic_enrichments.get(item["template_key"], {})
            parameters.append(AvailableParameter(
                parameter_id=item["template_key"], component_id=component_id,
                affected_components=sorted(affected_components),
                semantic_name=f"{component_name} {name}",
                description=(enrichment.get("description") or semantics.get("description")
                             or f"Configure {name} for {component_name}."),
                aliases=list(enrichment.get("aliases") or []),
                user_expressions=list(enrichment.get("user_expressions") or []),
                value_type=item["value_type"], current_value=item["current_value"],
                minimum=value_range.get("minimum"), maximum=value_range.get("maximum"),
                allowed_values=item.get("allowed_values"),
                unit=enrichment.get("unit") or semantics.get("unit"),
                physical_quantity=enrichment.get("physical_quantity"),
                semantic_category=enrichment.get("semantic_category"),
                behavioral_effect=("; ".join(enrichment.get("behavioral_effect", []))
                                   or semantics.get("behavioral_effect")),
                constraints=list(enrichment.get("constraints") or semantics.get("constraints") or []),
                relationships=_merge_parameter_relationships(
                    item.get("relationships", []), enrichment.get("related_parameters", []),
                ),
                semantic_relationships=list(enrichment.get("relationships") or []),
                evidence=evidence_by_id.get(item["template_key"]),
                semantic_confidence=enrichment.get("confidence"),
                llm_semantic_metadata=llm_metadata(enrichment),
            ))
    available_ids = {parameter.parameter_id for parameter in parameters}
    for parameter in parameters:
        parameter.relationships = [
            relationship for relationship in parameter.relationships
            if relationship.get("target") in available_ids
        ]
        if parameter.llm_semantic_metadata is not None:
            parameter.llm_semantic_metadata.relationships = [
                relationship for relationship in parameter.llm_semantic_metadata.relationships
                if relationship.target_parameter_id in available_ids
            ]

    by_name: dict[str, list[str]] = {}
    for parameter in parameters:
        short_name = parameter.parameter_id.rsplit(".", 1)[-1]
        by_name.setdefault(short_name, []).append(parameter.parameter_id)
    collisions = {name: ids for name, ids in by_name.items() if len(ids) > 1}
    return ParameterCatalogue(
        active_components=sorted(active_components),
        parameters=parameters, collisions=collisions,
    )


def validate_parameter_changes(
    changes: list[ParameterChange],
    catalogue: ParameterCatalogue,
) -> dict[str, RendererValue]:
    """Validate identifiers, types, ranges, and duplicates against the extracted catalogue."""
    available = {parameter.parameter_id: parameter for parameter in catalogue.parameters}
    values: dict[str, RendererValue] = {}
    for change in changes:
        if change.parameter_id in values:
            raise ValueError(f"duplicate parameter change {change.parameter_id!r}")
        parameter = available.get(change.parameter_id)
        if parameter is None:
            raise ValueError(f"configuration value is not available: {change.parameter_id!r}")
        value = change.value
        valid_type = {
            "boolean": type(value) is bool,
            "integer": type(value) is int,
            "number": type(value) in {int, float},
            "string": type(value) is str,
            "array": type(value) is list,
            "object": type(value) is dict,
            "unknown": True,
        }[parameter.value_type]
        if not valid_type:
            raise ValueError(f"wrong value type for {change.parameter_id!r}")
        if parameter.allowed_values is not None and value not in parameter.allowed_values:
            raise ValueError(f"value is not allowed for {change.parameter_id!r}")
        if parameter.minimum is not None and value < parameter.minimum:
            raise ValueError(f"value is below the minimum for {change.parameter_id!r}")
        if parameter.maximum is not None and value > parameter.maximum:
            raise ValueError(f"value is above the maximum for {change.parameter_id!r}")
        values[change.parameter_id] = value
    return values


def build_template_configuration_plan(
    realization: SystemRealization,
    parameter_values: dict[str, RendererValue],
) -> TemplateConfigurationPlan:
    values = dict(realization.renderer_values)
    for key, value in parameter_values.items():
        if key in values and values[key] != value:
            raise ValueError(f"parameter change conflicts with capability realization for {key!r}")
        values[key] = value
    return TemplateConfigurationPlan(user_values=values)


def validate_template_plan(
    plan: TemplateConfigurationPlan,
    schema: dict,
    manifest: dict | None = None,
) -> None:
    """Validate mapped values against the complete discovered configuration model."""
    validate_configuration_values(
        plan.user_values, schema, layer="LLM mission mapping"
    )
