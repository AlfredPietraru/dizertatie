"""Bounded retrieval over semantic parameter records and their local graph.

The default implementation is deliberately dependency-free and reproducible. It
provides a lexical baseline for experiments; embedding or hybrid retrievers can
implement the same result contract without changing the reasoning stages.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict, deque
from typing import Any, Literal

from pydantic import Field

from .schema import AvailableParameter, MissionModel, ParameterCatalogue


ContextVariant = Literal[
    "names_values", "semantic", "source", "system", "graph", "llm_semantic",
]
RetrievalOrigin = Literal["exact", "lexical", "graph"]


class RetrievalCandidate(MissionModel):
    parameter_id: str
    score: float
    rank: int
    matched_fields: list[str] = Field(default_factory=list)
    retrieval_origin: RetrievalOrigin
    graph_distance: int = Field(default=0, ge=0)
    parent_parameter_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ParameterRetrieval(MissionModel):
    query: str
    variant: ContextVariant
    top_k: int
    graph_hops: int
    candidates: list[RetrievalCandidate]
    catalogue_size: int
    retriever: str = "deterministic_lexical_v1"


class SelectionContext(MissionModel):
    query: str
    variant: ContextVariant
    records: list[dict[str, Any]]
    graph_edges: list[dict[str, Any]] = Field(default_factory=list)
    included_parameter_ids: list[str]
    omitted_candidates: int
    character_count: int
    character_budget: int


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "i", "in",
    "is", "it", "its", "of", "on", "or", "please", "robot", "set", "that", "the",
    "this", "to", "use", "value", "with",
}


def _normalize_text(value: Any) -> str:
    text = str(value).replace("_", " ").replace(".", " ").replace("/", " ")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _stem(token: str) -> str:
    for suffix in ("ations", "ation", "ments", "ment", "ing", "ies", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[:-len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def _tokens(value: Any) -> list[str]:
    return [_stem(token) for token in _normalize_text(value).split()
            if token not in _STOP_WORDS and len(token) > 1]


def _evidence_ids(parameter: AvailableParameter) -> list[str]:
    if not parameter.evidence:
        return []
    identifiers = []
    for field in ("declarations", "usages", "ros_interfaces"):
        for item in parameter.evidence.get(field, []):
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                identifiers.append(item["evidence_id"])
    return identifiers


def _search_fields(parameter: AvailableParameter, variant: ContextVariant) -> dict[str, str]:
    if variant == "llm_semantic":
        metadata = parameter.llm_semantic_metadata
        if metadata is None:
            return {}
        return {
            "llm_description": metadata.description,
            "llm_aliases": " ".join(metadata.aliases),
            "llm_user_expressions": " ".join(metadata.user_expressions),
            "llm_physical_quantity": metadata.physical_quantity or "",
            "llm_unit": metadata.unit or "",
            "llm_semantic_category": metadata.semantic_category or "",
            "llm_behavioral_effect": " ".join(metadata.behavioral_effect),
            "llm_constraints": " ".join(metadata.constraints),
            "llm_relationships": " ".join(
                f"{item.relation} {item.target_parameter_id} {item.reason} {item.confidence}"
                for item in metadata.relationships
            ),
        }
    fields = {
        "identifier": parameter.parameter_id,
        "name": parameter.semantic_name,
        "component": " ".join(parameter.affected_components) or parameter.component_id or "",
        "value": f"{parameter.current_value} {parameter.value_type}",
    }
    if variant in {"semantic", "source", "system", "graph"}:
        fields.update({
            "description": parameter.description,
            "physical_quantity": parameter.physical_quantity or "",
            "unit": parameter.unit or "",
            "semantic_category": parameter.semantic_category or "",
            "behavioral_effect": parameter.behavioral_effect or "",
            "constraints": " ".join(parameter.constraints),
        })
    evidence = parameter.evidence or {}
    if variant in {"source", "system", "graph"}:
        fields["source"] = " ".join(
            str(item.get("text", ""))
            for group in ("declarations", "usages")
            for item in evidence.get(group, []) if isinstance(item, dict)
        )
    if variant in {"system", "graph"}:
        fields["interfaces"] = " ".join(
            " ".join(str(item.get(key, "")) for key in
                     ("role", "effective_name", "interface_type", "controlling_parameter"))
            for item in evidence.get("ros_interfaces", []) if isinstance(item, dict)
        )
    if variant == "graph":
        fields["relationships"] = " ".join(
            f"{item.get('kind', '')} {item.get('target', '')}" for item in parameter.relationships
        )
    return fields


def _graph(
    catalogue: ParameterCatalogue,
    wiring_bindings: dict[str, dict[str, Any]] | None,
    *,
    llm_semantic_only: bool = False,
) -> dict[str, set[str]]:
    identifiers = {item.parameter_id for item in catalogue.parameters}
    graph: dict[str, set[str]] = {identifier: set() for identifier in identifiers}
    for parameter in catalogue.parameters:
        relationships = (
            parameter.llm_semantic_metadata.relationships
            if llm_semantic_only and parameter.llm_semantic_metadata else []
        )
        if not llm_semantic_only:
            relationships = parameter.relationships
        for relationship in relationships:
            target = (relationship.target_parameter_id if llm_semantic_only
                      else relationship.get("target"))
            if target in identifiers:
                graph[parameter.parameter_id].add(target)
                graph[target].add(parameter.parameter_id)
    if llm_semantic_only:
        return graph
    for binding in (wiring_bindings or {}).values():
        targets = [item for item in binding.get("parameter_keys", []) if item in identifiers]
        for left in targets:
            graph[left].update(right for right in targets if right != left)
    return graph


def retrieve_parameters(
    query: str,
    catalogue: ParameterCatalogue,
    *,
    variant: ContextVariant = "graph",
    top_k: int = 10,
    graph_hops: int = 1,
    max_candidates: int = 24,
    wiring_bindings: dict[str, dict[str, Any]] | None = None,
) -> ParameterRetrieval:
    """Rank candidates lexically, then include bounded local graph neighbors."""
    if top_k < 1 or graph_hops < 0 or max_candidates < top_k:
        raise ValueError("retrieval requires top_k >= 1, graph_hops >= 0, and max_candidates >= top_k")
    parameters = sorted(catalogue.parameters, key=lambda item: item.parameter_id)
    if variant == "llm_semantic" and not any(
        item.llm_semantic_metadata is not None for item in parameters
    ):
        raise ValueError(
            "llm_semantic retrieval requires a semantic-enrichment artifact"
        )
    documents = {item.parameter_id: _search_fields(item, variant) for item in parameters}
    tokenized = {
        identifier: {field: _tokens(text) for field, text in fields.items()}
        for identifier, fields in documents.items()
    }
    document_tokens = {
        identifier: set(token for values in fields.values() for token in values)
        for identifier, fields in tokenized.items()
    }
    document_frequency = Counter(
        token for values in document_tokens.values() for token in values
    )
    query_tokens = _tokens(query)
    normalized_query = _normalize_text(query)
    total = max(1, len(parameters))
    scored: list[tuple[float, str, list[str], RetrievalOrigin]] = []
    for parameter in parameters:
        identifier = parameter.parameter_id
        field_tokens = tokenized[identifier]
        matched_fields: set[str] = set()
        score = 0.0
        for token in query_tokens:
            for field, values in field_tokens.items():
                count = values.count(token)
                if not count:
                    continue
                matched_fields.add(field)
                inverse_frequency = math.log((total + 1) / (document_frequency[token] + 0.5)) + 1.0
                field_weight = {"identifier": 3.0, "name": 2.5, "description": 1.8,
                                "behavioral_effect": 1.6, "source": 1.2}.get(field, 1.0)
                score += inverse_frequency * field_weight * (1.0 + math.log(count))
        short_name = identifier.rsplit(".", 1)[-1]
        exact = variant != "llm_semantic" and (
            _normalize_text(identifier) in normalized_query
            or _normalize_text(short_name) in normalized_query
            or _normalize_text(parameter.semantic_name) in normalized_query
        )
        if exact:
            score += 100.0
            matched_fields.add("exact_name")
        if score > 0:
            scored.append((score, identifier, sorted(matched_fields), "exact" if exact else "lexical"))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = scored[:top_k]
    by_id = {item.parameter_id: item for item in parameters}
    candidates: list[RetrievalCandidate] = [
        RetrievalCandidate(parameter_id=identifier, score=round(score, 8), rank=index,
                           matched_fields=matched, retrieval_origin=origin,
                           evidence_ids=([] if variant == "llm_semantic"
                                         else _evidence_ids(by_id[identifier])))
        for index, (score, identifier, matched, origin) in enumerate(selected, 1)
    ]
    if variant in {"graph", "llm_semantic"} and graph_hops and candidates:
        adjacency = _graph(
            catalogue, wiring_bindings, llm_semantic_only=variant == "llm_semantic",
        )
        known = {item.parameter_id for item in candidates}
        queue = deque((item.parameter_id, 0, item.parameter_id) for item in candidates)
        while queue and len(candidates) < max_candidates:
            current, distance, seed = queue.popleft()
            if distance >= graph_hops:
                continue
            for neighbor in sorted(adjacency[current]):
                if neighbor in known:
                    continue
                next_distance = distance + 1
                parent_score = next(item.score for item in candidates if item.parameter_id == seed)
                candidates.append(RetrievalCandidate(
                    parameter_id=neighbor, score=round(parent_score / (10 * next_distance), 8),
                    rank=len(candidates) + 1, matched_fields=["relationship"],
                    retrieval_origin="graph", graph_distance=next_distance,
                    parent_parameter_id=current,
                    evidence_ids=([] if variant == "llm_semantic"
                                  else _evidence_ids(by_id[neighbor])),
                ))
                known.add(neighbor)
                queue.append((neighbor, next_distance, seed))
                if len(candidates) >= max_candidates:
                    break
    return ParameterRetrieval(
        query=query, variant=variant, top_k=top_k, graph_hops=graph_hops,
        candidates=candidates, catalogue_size=len(parameters),
    )


def _context_record(parameter: AvailableParameter, variant: ContextVariant) -> dict[str, Any]:
    if variant == "llm_semantic":
        metadata = parameter.llm_semantic_metadata
        return {
            "parameter_id": parameter.parameter_id,
            "kind": parameter.kind,
            "value_type": parameter.value_type,
            "current_value": parameter.current_value,
            "minimum": parameter.minimum,
            "maximum": parameter.maximum,
            "allowed_values": parameter.allowed_values,
            "semantic_metadata": (
                metadata.model_dump(mode="json") if metadata is not None else None
            ),
        }
    record: dict[str, Any] = {
        "parameter_id": parameter.parameter_id,
        "component_id": parameter.component_id,
        "affected_components": parameter.affected_components,
        "kind": parameter.kind,
        "semantic_name": parameter.semantic_name,
        "value_type": parameter.value_type,
        "current_value": parameter.current_value,
        "minimum": parameter.minimum,
        "maximum": parameter.maximum,
        "allowed_values": parameter.allowed_values,
    }
    if variant in {"semantic", "source", "system", "graph"}:
        record.update({
            "description": parameter.description,
            "physical_quantity": parameter.physical_quantity,
            "unit": parameter.unit,
            "semantic_category": parameter.semantic_category,
            "behavioral_effect": parameter.behavioral_effect,
            "constraints": parameter.constraints,
            "semantic_confidence": parameter.semantic_confidence,
        })
    evidence = parameter.evidence or {}
    if variant in {"source", "system", "graph"}:
        record["source_evidence"] = {
            "declarations": evidence.get("declarations", []),
            "usages": evidence.get("usages", []),
        }
    if variant in {"system", "graph"}:
        record["ros_interfaces"] = evidence.get("ros_interfaces", [])
    if variant == "graph":
        record["relationships"] = parameter.relationships
    return record


def build_selection_context(
    retrieval: ParameterRetrieval,
    catalogue: ParameterCatalogue,
    *,
    character_budget: int = 40000,
) -> SelectionContext:
    """Serialize ranked records without exceeding a reproducible character budget."""
    if character_budget < 1000:
        raise ValueError("selection context character budget must be at least 1000")
    by_id = {item.parameter_id: item for item in catalogue.parameters}
    records: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []
    used = 2
    for candidate in retrieval.candidates:
        parameter = by_id[candidate.parameter_id]
        record = _context_record(parameter, retrieval.variant)
        serialized = json.dumps(record, sort_keys=True, default=str)
        if records and used + len(serialized) > character_budget:
            break
        if not records and len(serialized) > character_budget:
            record.pop("source_evidence", None)
            record.pop("ros_interfaces", None)
            serialized = json.dumps(record, sort_keys=True, default=str)
        records.append(record)
        used += len(serialized)
        if retrieval.variant == "graph":
            for relationship in parameter.relationships:
                if isinstance(relationship.get("target"), str):
                    graph_edges.append({"source": parameter.parameter_id,
                                        "kind": relationship.get("kind", "related"),
                                        "target": relationship["target"]})
        elif retrieval.variant == "llm_semantic" and parameter.llm_semantic_metadata:
            for relationship in parameter.llm_semantic_metadata.relationships:
                graph_edges.append({
                    "source": parameter.parameter_id,
                    "kind": relationship.relation,
                    "target": relationship.target_parameter_id,
                    "reason": relationship.reason,
                    "confidence": relationship.confidence,
                })
    # Retrieval rank decides shortlist membership, but must not bias the LLM's
    # independent semantic decision through scores, ranks, or record position.
    records.sort(key=lambda record: record["parameter_id"])
    graph_edges.sort(key=lambda edge: (
        str(edge.get("source", "")), str(edge.get("target", "")),
        str(edge.get("kind", "")),
    ))
    included = [record["parameter_id"] for record in records]
    return SelectionContext(
        query=retrieval.query, variant=retrieval.variant, records=records,
        graph_edges=graph_edges, included_parameter_ids=included,
        omitted_candidates=len(retrieval.candidates) - len(records),
        character_count=used, character_budget=character_budget,
    )


def catalogue_subset(catalogue: ParameterCatalogue, parameter_ids: set[str]) -> ParameterCatalogue:
    parameters = [item for item in catalogue.parameters if item.parameter_id in parameter_ids]
    collisions = {
        name: [identifier for identifier in identifiers if identifier in parameter_ids]
        for name, identifiers in catalogue.collisions.items()
    }
    return ParameterCatalogue(
        active_components=catalogue.active_components, parameters=parameters,
        collisions={name: identifiers for name, identifiers in collisions.items() if len(identifiers) > 1},
    )
