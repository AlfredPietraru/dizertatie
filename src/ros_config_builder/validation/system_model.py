"""Acceptance checks and readable artifacts for the AntRobot system model."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def validate_system_model(model: dict[str, Any]) -> dict[str, Any]:
    """Validate cross-references and report coverage without guessing behavior."""
    errors, warnings = [], []
    if model.get("schema_version") != "1.0":
        errors.append(
            "unsupported system model schema version "
            f"{model.get('schema_version')!r}; expected '1.0'"
        )
    instances = model.get("deployment_instances", [])
    ids = [item.get("instance_id") for item in instances]
    if len(ids) != len(set(ids)): errors.append("deployment instance IDs are not unique")
    interfaces = {endpoint["id"] for item in instances for endpoint in item.get("interfaces", [])}
    for edge in model.get("edges", []):
        if edge.get("source_interface") not in interfaces: errors.append(f"edge {edge.get('id')} has missing source")
        if edge.get("target_interface") not in interfaces: errors.append(f"edge {edge.get('id')} has missing target")
    for item in instances:
        status = item.get("resolution_status")
        if status == "resolved_local" and (not item.get("source_node_ref") or not item.get("source_inventory")):
            errors.append(f"local instance {item.get('instance_id')} lacks source evidence")
        if status == "external" and item.get("interface_status") != "unknown":
            errors.append(f"external instance {item.get('instance_id')} claims known interfaces")
        names = [parameter.get("name") for parameter in item.get("effective_parameters", [])]
        if len(names) != len(set(names)): errors.append(f"instance {item.get('instance_id')} has duplicate parameters")
        for parameter in item.get("effective_parameters", []):
            if not parameter.get("provenance"): errors.append(f"parameter {parameter.get('name')} lacks provenance")
    if not model.get("edges"): warnings.append("no confirmed local communication edges were reconstructed")
    if model.get("unresolved_facts"): warnings.append(
        f"{len(model['unresolved_facts'])} deployment facts remain unresolved"
    )
    external = sum(item.get("resolution_status") == "external" for item in instances)
    if external: warnings.append(f"{external} external instances have unknown interfaces by design")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "coverage": {"instances": len(instances),
                "enabled_instances": sum(item.get("enabled", True) for item in instances),
                "local_instances": sum(item.get("resolution_status") == "resolved_local" for item in instances),
                "external_instances": external,
                "known_interfaces": len(interfaces), "confirmed_edges": len(model.get("edges", [])),
                "effective_parameters": sum(len(item.get("effective_parameters", [])) for item in instances),
                "unresolved_facts": len(model.get("unresolved_facts", []))}}


def render_system_model_report(model: dict[str, Any], validation: dict[str, Any] | None = None) -> str:
    validation = validation or validate_system_model(model)
    lines = ["# AntRobot ROS System Model", "", "## Acceptance status", "",
             f"- Structurally valid: **{'yes' if validation['valid'] else 'no'}**"]
    for key, value in validation["coverage"].items(): lines.append(f"- {key.replace('_', ' ').capitalize()}: **{value}**")
    if validation["errors"]:
        lines += ["", "### Errors", ""] + [f"- {value}" for value in validation["errors"]]
    if validation["warnings"]:
        lines += ["", "### Known limitations", ""] + [f"- {value}" for value in validation["warnings"]]
    lines += ["", "## Deployment inventory", "",
              "| Node | Package / executable | Status | Enabled | Namespace | Interfaces | Parameters |",
              "|---|---|---|---:|---|---:|---:|"]
    for item in model["deployment_instances"]:
        lines.append(f"| `{item.get('effective_node_name')}` | `{item.get('package')}/{item.get('executable')}` | "
                     f"`{item.get('resolution_status')}` | {'yes' if item.get('enabled') else 'no'} | "
                     f"`{item.get('effective_namespace') or '/'}` | {len(item.get('interfaces', []))} | "
                     f"{len(item.get('effective_parameters', []))} |")
    for item in model["deployment_instances"]:
        if item.get("resolution_status") != "resolved_local": continue
        inventory = item.get("source_inventory") or {}; name = item.get("effective_node_name")
        lines += ["", f"## Local node: `{name}`", "",
                  f"Source model: `{item.get('source_node_ref')}`", "",
                  f"Launch source: `{item.get('launch_source', {}).get('file')}:{item.get('launch_source', {}).get('line_start')}`", "",
                  "### Effective endpoints", ""]
        if item.get("interfaces"):
            for endpoint in item["interfaces"]:
                lines.append(f"- `{endpoint['role']}` `{endpoint['effective_name']}` (`{endpoint.get('type')}`)")
        else: lines.append("- None resolved")
        counts = {key: len(inventory.get(key, [])) for key in
                  ("services", "clients", "actions", "timers", "qos_profiles", "tf_entities")}
        lines += ["", "### ROS construct counts", ""] + [f"- {key.replace('_', ' ')}: {value}" for key, value in counts.items()]
        lines += ["", "### Effective parameters", ""]
        for parameter in item.get("effective_parameters", []):
            sources = " → ".join(str(x.get("source_type")) for x in parameter["provenance"])
            lines.append(f"- `{parameter['name']}` = `{parameter.get('effective_value')}` ({sources})")
    lines += ["", "## Confirmed local connections", ""]
    if model.get("edges"):
        for edge in model["edges"]: lines.append(f"- `{edge['name']}` (`{edge.get('type')}`), confidence: `{edge['confidence']}`")
    else: lines.append("- None. External interfaces are deliberately not inferred.")
    lines += ["", "## Unresolved deployment facts", ""]
    if model.get("unresolved_facts"):
        for fact in model["unresolved_facts"]: lines.append(f"- `{fact.get('kind')}`: `{json.dumps(fact, sort_keys=True)}`")
    else: lines.append("- None")
    return "\n".join(lines) + "\n"


def render_system_model_dot(model: dict[str, Any]) -> str:
    lines = ["digraph ros_system_model {", "  rankdir=LR;", '  graph [label="AntRobot deployment", labelloc=t];']
    interface_owner = {}
    for item in model["deployment_instances"]:
        node_id = "n_" + item["instance_id"].replace(":", "_")
        style = "solid" if item.get("enabled") else "dashed"
        color = "#2e7d32" if item.get("resolution_status") == "resolved_local" else "#757575"
        label = f"{item.get('effective_node_name')}\\n{item.get('package')}/{item.get('executable')}"
        lines.append(f'  {node_id} [label="{label}", shape=box, style="{style}", color="{color}"];')
        for endpoint in item.get("interfaces", []): interface_owner[endpoint["id"]] = node_id
    for edge in model.get("edges", []):
        source, target = interface_owner.get(edge["source_interface"]), interface_owner.get(edge["target_interface"])
        if source and target: lines.append(f'  {source} -> {target} [label="{edge["name"]}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_system_model_artifacts(model: dict[str, Any], output_directory: str | Path) -> dict[str, Path]:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True)
    validation = validate_system_model(model)
    paths = {"model": output / "ros_system_model.json", "validation": output / "validation.json",
             "report": output / "ros_system_model.md", "dot": output / "ros_system_model.dot"}
    paths["model"].write_text(json.dumps(model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["validation"].write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["report"].write_text(render_system_model_report(model, validation), encoding="utf-8")
    paths["dot"].write_text(render_system_model_dot(model), encoding="utf-8")
    return paths
