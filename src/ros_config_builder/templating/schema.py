"""Step 5: select and organize system-model values for Jinja2 inputs.

This module performs deterministic configuration-interface classification.  It
does not infer capabilities or mission semantics.  Every automatic decision is
labelled and can be replaced by an explicit review override.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..schemas.ros import IRModel


SCHEMA_VERSION = "1.0"
Classification = Literal["configurable", "fixed", "derived", "not_relevant"]


class TemplateVariable(IRModel):
    id: str
    name: str
    template_key: str
    kind: Literal["launch_argument", "ros_parameter"]
    group: str
    classification: Classification
    value_type: str
    default: Any = None
    current_value: Any = None
    allowed_values: list[Any] | None = None
    range: dict[str, Any] | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    classification_reason: str
    decision_source: Literal["deterministic_policy", "manual_override"]
    review_required: bool = True
    configuration_role: str = "unclassified"
    review_status: Literal["proposed", "approved", "rejected"] = "proposed"
    platform_policy: dict[str, str] = Field(default_factory=dict)


class TemplateConfigurationSchema(IRModel):
    schema_version: str = SCHEMA_VERSION
    stage: str = "template_configuration_schema"
    source_model_stage: str
    policy: dict[str, Any]
    launch_options: list[TemplateVariable] = Field(default_factory=list)
    node_parameters: dict[str, list[TemplateVariable]] = Field(default_factory=dict)
    structural_values: list[TemplateVariable] = Field(default_factory=list)
    derived_values: list[TemplateVariable] = Field(default_factory=list)
    internal_values: list[TemplateVariable] = Field(default_factory=list)
    summary: dict[str, int]
    frozen: bool = False
    policy_version: str | None = None
    frozen_inputs: list[str] = Field(default_factory=list)
    derived_bindings: list[str] = Field(default_factory=list)
    role_groups: dict[str, list[TemplateVariable]] = Field(default_factory=dict)


def _id(kind: str, *parts: Any) -> str:
    return f"template:{kind}:{hashlib.sha1('|'.join(map(str, parts)).encode()).hexdigest()[:12]}"


def _type(value: Any) -> str:
    if isinstance(value, bool): return "boolean"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    if value is None: return "unknown"
    if isinstance(value, str) and value.casefold() in {"true", "false"}: return "boolean"
    return "string"


def _coerce(value: Any, value_type: str) -> Any:
    if value_type == "boolean" and isinstance(value, str): return value.casefold() == "true"
    return value


def _launch_policy(name: str) -> tuple[Classification, str, str]:
    if name == "namespace": return "configurable", "structural", "deployment namespace"
    if name.startswith("launch_") or name in {"explore_lite", "enable_explore_lite"}:
        return "configurable", "launch_options", "launch activation control"
    if name in {"map", "params_file"}: return "configurable", "structural", "deployment resource selection"
    if name == "use_sim_time": return "configurable", "launch_options", "runtime clock selection"
    return "configurable", "launch_options", "declared launch interface"


def _parameter_policy(name: str) -> tuple[Classification, str, str]:
    lowered = name.casefold()
    if "plugin" in lowered: return "fixed", "internal", "implementation plugin selection"
    if lowered.startswith(("log_", "verbose_")) or lowered.endswith("_logging"):
        return "not_relevant", "internal", "diagnostic-only setting"
    if lowered.endswith(("_topic", "_frame", "_frame_id")) or lowered in {"odom", "topics"}:
        return "derived", "derived", "system wiring or structural ROS name"
    if lowered.endswith(("_file", "_path", "_dir")):
        return "fixed", "internal", "deployment filesystem detail"
    return "configurable", "node_parameters", "declared runtime parameter"


def _apply_override(record: dict[str, Any], override: dict[str, Any] | None) -> None:
    if not override: return
    allowed = {"classification", "group", "template_key", "allowed_values", "range",
               "classification_reason", "review_required"}
    record.update({key: value for key, value in override.items() if key in allowed})
    record["decision_source"] = "manual_override"


def build_template_configuration_schema(system_model: dict[str, Any], *,
                                        overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a thin, reviewable Jinja input schema from a deployment model."""
    overrides = overrides or {}; launch_by_name: dict[str, dict[str, Any]] = {}
    for argument in system_model.get("launch_argument_graph", []):
        name = argument["argument"]; classification, group, reason = _launch_policy(name)
        current = argument.get("effective_value"); value_type = _type(current)
        record = launch_by_name.setdefault(name, {"id": _id("launch", name), "name": name,
            "template_key": f"launch.{name}", "kind": "launch_argument", "group": group,
            "classification": classification, "value_type": value_type,
            "default": argument.get("declared_default"), "current_value": _coerce(current, value_type),
            "allowed_values": [True, False] if value_type == "boolean" else None, "range": None,
            "provenance": [], "targets": [], "classification_reason": reason,
            "decision_source": "deterministic_policy", "review_required": True})
        record["provenance"].append({key: argument.get(key) for key in
                                     ("declared_default", "inherited_value", "root_override", "effective_value")})
        record["targets"].append({"file": argument["launch_file"], "argument": name,
                                  "source": argument.get("source")})
    variables = list(launch_by_name.values())
    for instance in system_model.get("deployment_instances", []):
        node_key = str(instance.get("effective_node_name") or instance.get("executable"))
        inventory = instance.get("source_inventory") or {}
        declared = {item["name"].get("resolved_value"): item for item in inventory.get("parameters", [])
                    if isinstance(item.get("name"), dict)}
        for parameter in instance.get("effective_parameters", []):
            name = parameter["name"]; classification, group, reason = _parameter_policy(name)
            value = parameter.get("effective_value"); value_type = _type(value)
            declaration = declared.get(name, {})
            provenance = parameter.get("provenance", [])
            default_source = next((item for item in provenance if item.get("source_type") == "python_default"), None)
            record = {"id": _id("parameter", instance["instance_id"], name), "name": name,
                "template_key": f"nodes.{node_key}.{name}", "kind": "ros_parameter", "group": group,
                "classification": classification, "value_type": value_type,
                "default": default_source.get("value") if default_source else None,
                "current_value": _coerce(value, value_type),
                "allowed_values": [True, False] if value_type == "boolean" else None, "range": None,
                "provenance": provenance, "targets": [{"node_instance": instance["instance_id"],
                    "node": node_key, "package": instance.get("package"), "executable": instance.get("executable"),
                    "source_declaration": declaration.get("source"),
                    "configuration_sources": sorted({str(x.get("source")) for x in provenance if x.get("source")})}],
                "classification_reason": reason, "decision_source": "deterministic_policy",
                "review_required": True}
            _apply_override(record, overrides.get(record["id"]) or overrides.get(record["template_key"]))
            variables.append(record)
    for record in variables:
        _apply_override(record, overrides.get(record["id"]) or overrides.get(record["template_key"]))
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "stage": "template_configuration_schema",
        "source_model_stage": system_model.get("stage", "unknown"),
        "policy": {"semantic_capability_inference": False, "mission_inference": False,
                   "automatic_decisions_require_review": True,
                   "categories": {"configurable": "A", "fixed": "B", "derived": "C", "not_relevant": "D"}},
        "launch_options": [], "node_parameters": {}, "structural_values": [], "derived_values": [],
        "internal_values": [], "summary": {}}
    for record in sorted(variables, key=lambda x: x["template_key"]):
        model = TemplateVariable.model_validate(record).model_dump(mode="json")
        if model["group"] == "launch_options": result["launch_options"].append(model)
        elif model["group"] == "node_parameters":
            node = model["template_key"].split(".", 2)[1]; result["node_parameters"].setdefault(node, []).append(model)
        elif model["group"] == "structural": result["structural_values"].append(model)
        elif model["group"] == "derived": result["derived_values"].append(model)
        else: result["internal_values"].append(model)
    all_values = (result["launch_options"] + result["structural_values"] + result["derived_values"] +
                  result["internal_values"] + [x for values in result["node_parameters"].values() for x in values])
    counts = {name: sum(item["classification"] == name for item in all_values)
              for name in ("configurable", "fixed", "derived", "not_relevant")}
    result["summary"] = {"variables": len(all_values), "launch_arguments":
        sum(x["kind"] == "launch_argument" for x in all_values), "ros_parameters":
        sum(x["kind"] == "ros_parameter" for x in all_values), **counts,
        "manual_overrides": sum(x["decision_source"] == "manual_override" for x in all_values),
        "review_required": sum(x["review_required"] for x in all_values)}
    return TemplateConfigurationSchema.model_validate(result).model_dump(mode="json")


def render_template_schema_report(schema: dict[str, Any]) -> str:
    lines = ["# Template Configuration Schema", "", "## Summary", ""]
    lines += [f"- {key.replace('_', ' ').capitalize()}: **{value}**" for key, value in schema["summary"].items()]
    if schema.get("role_groups"):
        lines += ["", "## Curated role groups", ""]
        for role, values in schema["role_groups"].items():
            approved = sum(item["review_status"] == "approved" for item in values)
            rejected = sum(item["review_status"] == "rejected" for item in values)
            platform_fixed = sum(item.get("platform_policy", {}).get("antrobot") == "fixed" for item in values)
            lines.append(f"- **{role}**: {len(values)} values; {approved} approved; "
                         f"{rejected} rejected; {platform_fixed} fixed by AntRobot profile")
    groups = [("Launch options", schema["launch_options"]), ("Structural values", schema["structural_values"]),
              ("Derived values", schema["derived_values"]), ("Internal/fixed values", schema["internal_values"])]
    groups += [(f"Node parameters: {node}", values) for node, values in schema["node_parameters"].items()]
    for title, values in groups:
        lines += ["", f"## {title}", "", "| Template key | Class | Type | Default | Current | Reason |",
                  "|---|---|---|---|---|---|"]
        for item in values:
            lines.append(f"| `{item['template_key']}` | `{item['classification']}` | `{item['value_type']}` | "
                         f"`{item.get('default')}` | `{item.get('current_value')}` | {item['classification_reason']} |")
    return "\n".join(lines) + "\n"


def write_template_configuration_schema(schema: dict[str, Any], output_directory: str | Path) -> dict[str, Path]:
    validated = TemplateConfigurationSchema.model_validate(schema); output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True); paths = {"json": output / "template_configuration_schema.json",
                                                        "report": output / "template_configuration_schema.md"}
    payload = validated.model_dump(mode="json")
    paths["json"].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["report"].write_text(render_template_schema_report(payload), encoding="utf-8")
    return paths


def _curation_role(variable: dict[str, Any]) -> tuple[str, str]:
    """Assign a practical review role without capability/mission inference."""
    name = variable["name"].casefold(); key = variable["template_key"].casefold()
    if variable["kind"] == "launch_argument": return "deployment", "launch interface"
    if variable["classification"] == "derived" or name.endswith(("_topic", "_frame", "_frame_id")):
        return "wiring", "ROS name or system connection"
    if any(token in name for token in ("wheel_radius", "wheel_separation", "encoder_cpr", "no_load_rpm")):
        return "hardware", "physical robot constant"
    if any(token in key for token in ("kinematic_icp", "kiss_icp", "cartographer", "explore")):
        return "algorithm", "algorithm-specific parameter"
    if "plugin" in name or name.startswith(("log_", "verbose_")) or name.endswith(("_path", "_dir", "_file")):
        return "internal", "implementation or filesystem detail"
    return "runtime", "runtime behavior parameter"


def curate_template_configuration_schema(candidate_schema: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Apply a review policy and freeze only when every candidate has a decision."""
    payload = TemplateConfigurationSchema.model_validate(candidate_schema).model_dump(mode="json")
    role_policy = policy.get("roles", {}); overrides = policy.get("overrides", {})
    collections = [payload["launch_options"], payload["structural_values"], payload["derived_values"],
                   payload["internal_values"], *payload["node_parameters"].values()]
    all_values = [item for collection in collections for item in collection]
    for item in all_values:
        role, role_reason = _curation_role(item); rules = role_policy.get(role, {})
        item["configuration_role"] = role
        if rules:
            item["classification"] = rules.get("classification", item["classification"])
            item["review_status"] = rules.get("review_status", "approved")
            item["review_required"] = item["review_status"] == "proposed"
            item["classification_reason"] = rules.get("reason", role_reason)
            item["platform_policy"] = dict(rules.get("platform_policy", {}))
            item["decision_source"] = "manual_override"
        override = overrides.get(item["template_key"], {})
        if override:
            for field in ("classification", "group", "configuration_role", "review_status",
                          "classification_reason", "platform_policy", "allowed_values", "range"):
                if field in override: item[field] = override[field]
            item["review_required"] = item["review_status"] == "proposed"
            item["decision_source"] = "manual_override"
    pending = [item for item in all_values if item["review_status"] == "proposed"]
    rejected = [item for item in all_values if item["review_status"] == "rejected"]
    approved = [item for item in all_values if item["review_status"] == "approved"]
    generic_inputs = [item["template_key"] for item in approved if item["classification"] == "configurable"]
    platform = policy.get("platform", "antrobot")
    frozen_inputs = [item["template_key"] for item in approved if item["classification"] == "configurable"
                     and item.get("platform_policy", {}).get(platform) != "fixed"]
    payload["frozen"] = not pending
    payload["policy_version"] = str(policy.get("version", "unversioned"))
    payload["frozen_inputs"] = sorted(frozen_inputs)
    payload["derived_bindings"] = sorted(item["template_key"] for item in approved
                                         if item["classification"] == "derived")
    payload["role_groups"] = {role: sorted(
        (item for item in all_values if item["configuration_role"] == role),
        key=lambda item: item["template_key"],
    ) for role in ("deployment", "hardware", "runtime", "wiring", "algorithm", "internal")}
    payload["policy"] = {**payload["policy"], "curation_policy": policy,
                         "generic_configurable_inputs": sorted(generic_inputs)}
    payload["summary"].update({"approved": len(approved), "rejected": len(rejected),
        "proposed": len(pending), "review_required": len(pending), "frozen_inputs": len(frozen_inputs),
        "generic_inputs": len(generic_inputs), "derived_bindings": len(payload["derived_bindings"]),
        "configurable": sum(item["classification"] == "configurable" for item in all_values),
        "fixed": sum(item["classification"] == "fixed" for item in all_values),
        "derived": sum(item["classification"] == "derived" for item in all_values),
        "not_relevant": sum(item["classification"] == "not_relevant" for item in all_values),
        "manual_overrides": sum(item["decision_source"] == "manual_override" for item in all_values)})
    if pending and policy.get("require_complete", True):
        names = ", ".join(item["template_key"] for item in pending[:5])
        raise ValueError(f"Template interface cannot be frozen: {len(pending)} proposed values remain ({names})")
    return TemplateConfigurationSchema.model_validate(payload).model_dump(mode="json")


def load_template_configuration_policy(path: str | Path) -> dict[str, Any]:
    """Load a JSON or YAML policy; PyYAML remains optional."""
    path = Path(path); text = path.read_text(encoding="utf-8")
    try: value = json.loads(text)
    except json.JSONDecodeError:
        from ..extraction.deployment import _load_yaml
        value = _load_yaml(text)
    if not isinstance(value, dict): raise ValueError("template configuration policy must be a mapping")
    return value
