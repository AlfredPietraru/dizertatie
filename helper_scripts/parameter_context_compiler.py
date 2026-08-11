#!/usr/bin/env python3
"""Compile verbose parameter graph results into compact, code-first LLM context."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


RELATIONSHIP_CATEGORIES = (
    "shared_transformations",
    "shared_validations",
    "shared_conditions",
    "shared_calls",
    "shared_returns",
    "shared_ros_consumers",
    "shared_values",
)


def _location(item: dict[str, Any]) -> str:
    path = item.get("path", "unknown")
    line = item.get("line") or item.get("start_line")
    return f"{path}:{line}" if line else str(path)


def _node_fact(node: dict[str, Any]) -> dict[str, Any]:
    """Keep semantic facts and discard graph hashes and AST bookkeeping."""
    fact = {
        "kind": node.get("kind"),
        "description": node.get("label") or node.get("name"),
    }
    for key in ("configuration_kind", "name", "default", "value", "path", "scope", "line"):
        if node.get(key) is not None:
            fact[key] = node[key]
    return fact


def _collect_chunks(
    flow_matches: dict[str, Any], relationship: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, str]]:
    records: dict[str, dict[str, Any]] = {}

    def add_source(source: dict[str, Any]) -> None:
        for collection_name in ("methods", "module_statements"):
            for source_key, record in source.get(collection_name, {}).items():
                stable_key = (
                    f"{record.get('path')}:{record.get('start_line')}:"
                    f"{record.get('end_line')}"
                )
                records.setdefault(stable_key, record)

    for content in flow_matches.values():
        add_source(content)
    if relationship:
        for pair in relationship.get("pair_relationships", {}).values():
            add_source(pair)

    chunks: dict[str, Any] = {}
    source_key_to_chunk: dict[str, str] = {}
    for number, (source_key, record) in enumerate(sorted(records.items()), start=1):
        chunk_id = f"code_{number:03d}"
        source_key_to_chunk[source_key] = chunk_id
        chunks[chunk_id] = {
            "path": record.get("path"),
            "qualified_name": record.get("qualified_name", "module statement"),
            "start_line": record.get("start_line"),
            "end_line": record.get("end_line"),
            "code": record.get("code", ""),
        }
    return chunks, source_key_to_chunk


def _chunk_refs(content: dict[str, Any], source_key_to_chunk: dict[str, str]) -> list[str]:
    refs: set[str] = set()
    for collection_name in ("methods", "module_statements"):
        for record in content.get(collection_name, {}).values():
            key = f"{record.get('path')}:{record.get('start_line')}:{record.get('end_line')}"
            if key in source_key_to_chunk:
                refs.add(source_key_to_chunk[key])
    return sorted(refs)


def compile_parameter_context(
    parameter: str,
    flow: dict[str, Any],
    relationship: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return compilation metrics and the Markdown context intended for an LLM."""
    chunks, source_key_to_chunk = _collect_chunks(flow.get("matches", {}), relationship)
    graph_id_to_instance: dict[str, str] = {}
    instances: dict[str, Any] = {}

    for number, (graph_id, content) in enumerate(sorted(flow.get("matches", {}).items()), start=1):
        instance_id = f"instance_{number}"
        graph_id_to_instance[graph_id] = instance_id
        parameter_node = content["parameter"]
        graph_flow = content["forward_flow"]
        nodes = graph_flow.get("nodes", {})
        facts = {_id: _node_fact(node) for _id, node in nodes.items()}
        transitions = []
        for edge in graph_flow.get("edges", []):
            source = facts.get(edge.get("source"), {"description": "unknown"})
            target = facts.get(edge.get("target"), {"description": "unknown"})
            transitions.append(
                {
                    "relation": edge.get("relation"),
                    "source": source.get("description"),
                    "target": target.get("description"),
                    "location": _location(target),
                    "confidence": edge.get("confidence"),
                }
            )
        instances[instance_id] = {
            "graph_parameter_id": graph_id,
            "source": _node_fact(parameter_node),
            "flow_summary": transitions,
            "code_chunk_ids": _chunk_refs(content, source_key_to_chunk),
            "unresolved_source": content.get("unresolved_source", []),
        }

    pairs = []
    if relationship:
        for pair in relationship.get("pair_relationships", {}).values():
            left_graph_id = pair.get("parameter_a_id")
            right_graph_id = pair.get("parameter_b_id")
            # Self-pairs carry no cross-instance information.
            if left_graph_id == right_graph_id:
                continue
            shared: dict[str, list[dict[str, Any]]] = {}
            for category in RELATIONSHIP_CATEGORIES:
                values = []
                for evidence in pair.get(category, []):
                    node = evidence.get("node", {})
                    values.append(
                        {
                            "description": node.get("label") or node.get("name"),
                            "kind": node.get("kind"),
                            "location": _location(node),
                            "relations_from_left": evidence.get("incoming_relations_from_a", []),
                            "relations_from_right": evidence.get("incoming_relations_from_b", []),
                        }
                    )
                if values:
                    shared[category] = values
            pairs.append(
                {
                    "left": graph_id_to_instance.get(left_graph_id, left_graph_id),
                    "right": graph_id_to_instance.get(right_graph_id, right_graph_id),
                    "relationship_found": pair.get("relationship_found", False),
                    "direct_left_to_right": pair.get("direct_a_to_b", False),
                    "direct_right_to_left": pair.get("direct_b_to_a", False),
                    "shared_evidence": shared,
                    "code_chunk_ids": _chunk_refs(pair, source_key_to_chunk),
                }
            )

    context = {
        "parameter": parameter,
        "status": flow.get("status"),
        "instances": instances,
        "cross_instance_relationships": pairs,
        "code_chunks": chunks,
        "compilation": {
            "input_flow_nodes": sum(
                len(item.get("forward_flow", {}).get("nodes", {}))
                for item in flow.get("matches", {}).values()
            ),
            "input_flow_edges": sum(
                len(item.get("forward_flow", {}).get("edges", []))
                for item in flow.get("matches", {}).values()
            ),
            "deduplicated_code_chunks": len(chunks),
        },
    }
    return {
        "compilation": context["compilation"],
        "llm_context": render_llm_context(context),
    }


def render_llm_context(context: dict[str, Any]) -> str:
    """Render code once and retain only human-readable graph relationships."""
    lines = [f"# Parameter analysis evidence: `{context['parameter']}`", ""]
    for instance_id, instance in context["instances"].items():
        source = instance["source"]
        lines.extend(
            [
                f"## {instance_id}",
                "",
                f"- Kind: {source.get('configuration_kind', source.get('kind', 'unknown'))}",
                f"- Default: `{source.get('default', 'unknown')}`",
                f"- Declared at: `{_location(source)}`",
                f"- Scope: `{source.get('scope', 'unknown')}`",
                f"- Code chunks: {', '.join(instance['code_chunk_ids']) or 'none'}",
                "",
                "### Detected forward flow",
                "",
            ]
        )
        seen = set()
        for transition in instance["flow_summary"]:
            key = tuple(str(transition.get(k)) for k in ("source", "relation", "target", "location"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"- {transition['source']} --{transition['relation']}--> "
                f"{transition['target']} (`{transition['location']}`)"
            )
        lines.append("")

    lines.extend(["## Cross-instance relationships", ""])
    if not context["cross_instance_relationships"]:
        lines.append("No cross-instance relationship was detected.")
    for pair in context["cross_instance_relationships"]:
        lines.append(
            f"### {pair['left']} → {pair['right']} "
            f"(relationship detected: {pair['relationship_found']})"
        )
        lines.append("")
        for category, evidence_items in pair["shared_evidence"].items():
            lines.append(f"- {category}:")
            for item in evidence_items:
                lines.append(f"  - {item['description']} (`{item['location']}`)")
        if not pair["shared_evidence"]:
            lines.append("- No shared semantic nodes detected.")
        lines.append("")

    lines.extend(["## Deduplicated source code", ""])
    for chunk_id, chunk in context["code_chunks"].items():
        lines.extend(
            [
                f"### {chunk_id}: `{chunk['qualified_name']}`",
                "",
                f"Source: `{chunk['path']}:{chunk['start_line']}`",
                "",
                "```python",
                chunk["code"].rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
