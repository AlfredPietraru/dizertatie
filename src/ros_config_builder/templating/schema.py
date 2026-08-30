"""Build the source-derived semantic configuration model used by the LLM."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ..schemas.ros import IRModel


class TemplateVariable(IRModel):
    id: str
    name: str
    template_key: str
    kind: Literal["launch_argument", "ros_parameter"]
    value_type: str
    default: Any = None
    current_value: Any = None
    allowed_values: list[Any] | None = None
    range: dict[str, Any] | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    semantic_information: dict[str, Any] = Field(default_factory=lambda: {
        "description": None, "unit": None, "behavioral_effect": None, "constraints": [],
    })
    relationships: list[dict[str, str]] = Field(default_factory=list)
    affected_components: list[str] = Field(default_factory=list)


class TemplateConfigurationSchema(IRModel):
    stage: Literal["semantic_configuration_model"] = "semantic_configuration_model"
    schema_version: Literal["3.0"] = "3.0"
    source_model_stage: str
    launch_arguments: list[TemplateVariable] = Field(default_factory=list)
    node_parameters: dict[str, list[TemplateVariable]] = Field(default_factory=dict)
    summary: dict[str, int]
    complete: bool = True
    configuration_keys: list[str] = Field(default_factory=list)
    interface_parameter_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_physical_slots(self) -> "TemplateConfigurationSchema":
        values = self.launch_arguments + [
            item for records in self.node_parameters.values() for item in records
        ]
        keys = [item.template_key for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("physical configuration slot keys must be unique")
        if len(self.configuration_keys) != len(set(self.configuration_keys)):
            raise ValueError("configuration_keys must not contain duplicates")
        if set(keys) != set(self.configuration_keys):
            raise ValueError("configuration_keys must exactly match physical slot records")
        return self


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


def _is_wiring_parameter(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(("_topic", "_frame", "_frame_id")) or lowered in {"odom", "topics"}


def build_template_configuration_schema(system_model: dict[str, Any]) -> dict[str, Any]:
    """Represent each physical launch/YAML configuration slot without a policy gate.

    A wildcard YAML assignment can affect several deployed nodes.  It is one writable
    value, not several independent values, so its record lists every affected component.
    """
    launch_by_name: dict[str, dict[str, Any]] = {}
    for argument in system_model.get("launch_argument_graph", []):
        name = argument["argument"]
        current = argument.get("effective_value"); value_type = _type(current)
        record = launch_by_name.setdefault(name, {"id": _id("launch", name), "name": name,
            "template_key": f"launch.{name}", "kind": "launch_argument", "value_type": value_type,
            "default": argument.get("declared_default"), "current_value": _coerce(current, value_type),
            "allowed_values": [True, False] if value_type == "boolean" else None, "range": None,
            "provenance": [], "targets": [], "affected_components": []})
        record["provenance"].append({key: argument.get(key) for key in
                                     ("declared_default", "inherited_value", "root_override", "effective_value")})
        record["targets"].append({"file": argument["launch_file"], "argument": name,
                                  "source": argument.get("source")})
    variables = list(launch_by_name.values())
    parameters_by_target: dict[tuple[str, ...], dict[str, Any]] = {}
    parameter_occurrences = 0
    for instance in system_model.get("deployment_instances", []):
        node_key = str(instance.get("effective_node_name") or instance.get("executable"))
        inventory = instance.get("source_inventory") or {}
        declared = {item["name"].get("resolved_value"): item for item in inventory.get("parameters", [])
                    if isinstance(item.get("name"), dict)}
        for parameter in instance.get("effective_parameters", []):
            parameter_occurrences += 1
            name = parameter["name"]
            value = parameter.get("effective_value"); value_type = _type(value)
            declaration = declared.get(name, {})
            provenance = parameter.get("provenance", [])
            default_source = next((item for item in provenance if item.get("source_type") == "python_default"), None)
            yaml_source = next((item for item in reversed(provenance)
                                if item.get("source_type") == "yaml"), None)
            if yaml_source is not None:
                physical_target = (
                    "yaml", str(yaml_source.get("source")), str(yaml_source.get("node_selector")),
                    str(yaml_source.get("parameter_path") or name),
                )
            else:
                physical_target = ("node", str(instance["instance_id"]), name)
            record = parameters_by_target.setdefault(physical_target, {
                "id": _id("parameter", *physical_target), "name": name,
                "template_key": "", "kind": "ros_parameter", "value_type": value_type,
                "default": default_source.get("value") if default_source else None,
                "current_value": _coerce(value, value_type),
                "allowed_values": [True, False] if value_type == "boolean" else None,
                "range": None, "provenance": [], "targets": [], "affected_components": [],
            })
            if record["value_type"] != value_type or record["current_value"] != _coerce(value, value_type):
                raise ValueError(
                    f"shared configuration target {physical_target!r} has divergent effective values"
                )
            record["affected_components"].append(node_key)
            record["provenance"].extend(provenance)
            record["targets"].append({"node_instance": instance["instance_id"],
                "node": node_key, "package": instance.get("package"), "executable": instance.get("executable"),
                "source_declaration": declaration.get("source"),
                "configuration_sources": sorted({str(x.get("source")) for x in provenance if x.get("source")})})
    for record in parameters_by_target.values():
        record["affected_components"] = sorted(set(record["affected_components"]))
        record["template_key"] = f"nodes.{record['affected_components'][0]}.{record['name']}"
        unique_provenance = {}
        for item in record["provenance"]:
            unique_provenance[json.dumps(item, sort_keys=True, default=str)] = item
        record["provenance"] = list(unique_provenance.values())
        variables.append(record)
    parameters_by_name: dict[str, list[dict[str, Any]]] = {}
    for record in variables:
        if record["kind"] == "ros_parameter":
            parameters_by_name.setdefault(record["name"], []).append(record)
    for records in parameters_by_name.values():
        if len(records) < 2:
            continue
        for record in records:
            record["relationships"] = [
                {"kind": "same_parameter_name", "target": other["template_key"]}
                for other in records if other is not record
            ]
    result: dict[str, Any] = {"stage": "semantic_configuration_model", "schema_version": "3.0",
        "source_model_stage": system_model.get("stage", "unknown"),
        "launch_arguments": [], "node_parameters": {}, "summary": {}}
    for record in sorted(variables, key=lambda x: x["template_key"]):
        model = TemplateVariable.model_validate(record).model_dump(mode="json")
        if model["kind"] == "launch_argument":
            result["launch_arguments"].append(model)
        else:
            node = model["template_key"].split(".", 2)[1]; result["node_parameters"].setdefault(node, []).append(model)
    all_values = result["launch_arguments"] + [
        item for values in result["node_parameters"].values() for item in values
    ]
    configuration_keys = sorted(item["template_key"] for item in all_values)
    wiring_parameters = sorted(
        item["template_key"] for item in all_values
        if item["kind"] == "ros_parameter" and _is_wiring_parameter(item["name"])
    )
    result["complete"] = True
    result["configuration_keys"] = configuration_keys
    result["interface_parameter_keys"] = wiring_parameters
    result["summary"] = {"configuration_values": len(all_values), "launch_arguments":
        sum(x["kind"] == "launch_argument" for x in all_values), "ros_parameters":
        sum(x["kind"] == "ros_parameter" for x in all_values),
        "effective_parameter_occurrences": parameter_occurrences,
        "interface_parameters": len(wiring_parameters),
        "values_with_relationships": sum(bool(x.get("relationships")) for x in all_values),
        "values_with_descriptions": sum(
            bool((x.get("semantic_information") or {}).get("description")) for x in all_values
        )}
    return TemplateConfigurationSchema.model_validate(result).model_dump(mode="json")


def render_template_schema_report(schema: dict[str, Any]) -> str:
    lines = ["# Semantic Configuration Model", "", "Every extracted value is a configuration candidate.",
             "", "## Summary", ""]
    lines += [f"- {key.replace('_', ' ').capitalize()}: **{value}**" for key, value in schema["summary"].items()]
    groups = [("Launch arguments", schema["launch_arguments"])]
    groups += [(f"Node parameters: {node}", values) for node, values in schema["node_parameters"].items()]
    for title, values in groups:
        lines += ["", f"## {title}", "", "| Configuration key | Type | Default | Current | Relationships |",
                  "|---|---|---|---|---|"]
        for item in values:
            relationships = ", ".join(
                f"{relationship['kind']} → `{relationship['target']}`"
                for relationship in item.get("relationships", [])
            ) or "—"
            lines.append(f"| `{item['template_key']}` | `{item['value_type']}` | "
                         f"`{item.get('default')}` | `{item.get('current_value')}` | {relationships} |")
    return "\n".join(lines) + "\n"


def write_template_configuration_schema(schema: dict[str, Any], output_directory: str | Path) -> dict[str, Path]:
    validated = TemplateConfigurationSchema.model_validate(schema); output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True); paths = {"json": output / "template_configuration_schema.json",
                                                        "report": output / "template_configuration_schema.md"}
    payload = validated.model_dump(mode="json")
    paths["json"].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["report"].write_text(render_template_schema_report(payload), encoding="utf-8")
    return paths
