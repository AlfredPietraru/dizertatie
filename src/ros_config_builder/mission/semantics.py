"""Source-evidence construction and optional LLM semantic enrichment.

The extractor remains the authority for objective facts.  This module packages
those facts for an LLM and stores the model's semantic hypotheses separately so
they can be evaluated, replaced, or omitted without changing the ROS model.
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from .schema import MissionModel


SEMANTIC_EVIDENCE_PLACEHOLDER = "{{PARAMETER_EVIDENCE}}"
SEMANTIC_ENRICHMENT_SCHEMA_PLACEHOLDER = "{{SEMANTIC_ENRICHMENT_JSON_SCHEMA}}"


class SourceExcerpt(MissionModel):
    evidence_id: str
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    role: Literal["declaration", "read", "usage", "launch_declaration"]
    text: str


class InterfaceEvidence(MissionModel):
    evidence_id: str
    role: str
    effective_name: str | None = None
    interface_type: str | None = None
    controlling_parameter: str | None = None


class ParameterEvidence(MissionModel):
    parameter_id: str
    name: str
    kind: Literal["ros_parameter", "launch_argument"]
    component_id: str | None = None
    affected_components: list[str] = Field(default_factory=list)
    value_type: str
    default: Any = None
    current_value: Any = None
    declarations: list[SourceExcerpt] = Field(default_factory=list)
    usages: list[SourceExcerpt] = Field(default_factory=list)
    ros_interfaces: list[InterfaceEvidence] = Field(default_factory=list)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    source_relationships: list[dict[str, Any]] = Field(default_factory=list)
    related_parameters: list[str] = Field(default_factory=list)


class ParameterEvidenceArtifact(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    source_model_sha256: str
    configuration_model_sha256: str
    parameters: list[ParameterEvidence]


class SemanticEnrichment(MissionModel):
    """One model-generated semantic hypothesis grounded in ParameterEvidence."""

    parameter_id: str
    description: str = Field(min_length=3)
    physical_quantity: str | None = None
    unit: str | None = None
    semantic_category: str | None = None
    behavioral_effect: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    related_parameters: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(min_length=1)


class SemanticEnrichmentBatch(MissionModel):
    enrichments: list[SemanticEnrichment]

    @model_validator(mode="after")
    def unique_parameter_ids(self) -> "SemanticEnrichmentBatch":
        identifiers = [item.parameter_id for item in self.enrichments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("semantic enrichment contains duplicate parameter IDs")
        return self


class SemanticEnrichmentArtifact(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    source_model_sha256: str
    configuration_model_sha256: str
    model_identifier: str
    prompt_version: str
    enrichments: list[SemanticEnrichment]


class SemanticBackend(Protocol):
    def __call__(self, system_prompt: str, user_prompt: str) -> str: ...


def _configuration_items(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return list(schema.get("launch_arguments", [])) + [
        item
        for items in schema.get("node_parameters", {}).values()
        for item in items
    ]


def _component_id(item: dict[str, Any]) -> str | None:
    if item.get("kind") != "ros_parameter":
        return None
    parts = str(item["template_key"]).split(".", 2)
    return parts[1] if len(parts) == 3 else None


def _source_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = value.get("source") if isinstance(value.get("source"), dict) else value
    if not isinstance(source.get("file"), str) or not isinstance(source.get("line_start"), int):
        return None
    return source


def _source_path(workspace: Path, relative: str) -> Path | None:
    candidate = (workspace / relative).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _excerpt(
    workspace: Path,
    reference: dict[str, Any],
    role: Literal["declaration", "read", "usage", "launch_declaration"],
    *,
    context_lines: int,
) -> SourceExcerpt | None:
    relative = reference.get("file")
    if not isinstance(relative, str):
        return None
    path = _source_path(workspace, relative)
    if path is None:
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return None
    source_start = max(1, int(reference.get("line_start", 1)))
    source_end = max(source_start, int(reference.get("line_end", source_start)))
    start = max(1, source_start - context_lines)
    end = min(len(lines), source_end + context_lines)
    numbered = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
    identity = f"{relative}|{start}|{end}|{role}|{numbered}"
    evidence_id = f"evidence:{hashlib.sha1(identity.encode()).hexdigest()[:12]}"
    return SourceExcerpt(evidence_id=evidence_id, file=relative, line_start=start,
                         line_end=end, role=role, text=numbered)


def _instances_for_components(
    system_model: dict[str, Any], component_ids: list[str],
) -> list[dict[str, Any]]:
    identifiers = set(component_ids)
    return [
        instance for instance in system_model.get("deployment_instances", [])
        if identifiers & {instance.get("effective_node_name"), instance.get("executable")}
    ]


def _inventory_parameter(instance: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if instance is None:
        return None
    for item in (instance.get("source_inventory") or {}).get("parameters", []):
        resolved_name = item.get("name", {}).get("resolved_value") if isinstance(item.get("name"), dict) else None
        if resolved_name == name:
            return item
    return None


def _deduplicate_excerpts(values: list[SourceExcerpt]) -> list[SourceExcerpt]:
    result: list[SourceExcerpt] = []
    seen: set[tuple[str, int, int, str]] = set()
    for item in values:
        key = (item.file, item.line_start, item.line_end, item.role)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _lexical_usages(
    workspace: Path,
    files: set[str],
    terms: set[str],
    declaration_lines: set[tuple[str, int]],
    *,
    context_lines: int,
    max_usages: int,
) -> list[SourceExcerpt]:
    patterns = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
                for term in terms if term]
    usages: list[SourceExcerpt] = []
    for relative in sorted(files):
        path = _source_path(workspace, relative)
        if path is None:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, 1):
            if (relative, number) in declaration_lines or not any(pattern.search(line) for pattern in patterns):
                continue
            reference = {"file": relative, "line_start": number, "line_end": number}
            item = _excerpt(workspace, reference, "usage", context_lines=context_lines)
            if item is not None:
                usages.append(item)
            if len(usages) >= max_usages:
                return usages
    return usages


def build_parameter_evidence(
    workspace: str | Path,
    schema: dict[str, Any],
    system_model: dict[str, Any],
    *,
    context_lines: int = 2,
    max_usages: int = 8,
) -> list[ParameterEvidence]:
    """Build bounded evidence packages from existing extractor outputs and source lines."""
    root = Path(workspace).resolve()
    packages: list[ParameterEvidence] = []
    for item in sorted(_configuration_items(schema), key=lambda value: value["template_key"]):
        component_id = _component_id(item)
        affected_components = list(item.get("affected_components") or ([component_id] if component_id else []))
        instances = _instances_for_components(system_model, affected_components)
        inventories = [inventory for instance in instances
                       if (inventory := _inventory_parameter(instance, item["name"])) is not None]
        declaration_refs: list[dict[str, Any]] = []
        for target in item.get("targets", []):
            for candidate in (target.get("source_declaration"), target.get("source")):
                if (reference := _source_reference(candidate)) is not None:
                    declaration_refs.append(reference)
        for inventory in inventories:
            if (reference := _source_reference(inventory.get("source"))) is not None:
                declaration_refs.append(reference)
        declaration_role = "launch_declaration" if item["kind"] == "launch_argument" else "declaration"
        declarations = [
            excerpt for reference in declaration_refs
            if (excerpt := _excerpt(root, reference, declaration_role, context_lines=context_lines)) is not None
        ]

        reads: list[SourceExcerpt] = []
        assigned_terms: set[str] = set()
        for inventory in inventories:
            for read in inventory.get("reads", []):
                if isinstance(read.get("assigned_to"), str):
                    assigned_terms.add(read["assigned_to"])
                reference = _source_reference(read.get("source"))
                if reference is not None:
                    excerpt = _excerpt(root, reference, "read", context_lines=context_lines)
                    if excerpt is not None:
                        reads.append(excerpt)

        source_relationships = []
        for instance in instances:
            for relationship in (instance.get("source_inventory") or {}).get("relationships", []):
                if relationship.get("from") == f"parameter:{item['name']}":
                    source_relationships.append(relationship)
                    if isinstance(relationship.get("to"), str):
                        assigned_terms.add(relationship["to"])

        files = {reference["file"] for reference in declaration_refs if isinstance(reference.get("file"), str)}
        declaration_lines = {
            (reference["file"], number)
            for reference in declaration_refs if isinstance(reference.get("file"), str)
            for number in range(int(reference.get("line_start", 1)), int(reference.get("line_end", reference.get("line_start", 1))) + 1)
        }
        terms = {item["name"], *assigned_terms}
        usages = _deduplicate_excerpts(reads + _lexical_usages(
            root, files, terms, declaration_lines,
            context_lines=context_lines, max_usages=max_usages,
        ))[:max_usages]

        interfaces = []
        for instance in instances:
            interfaces = [InterfaceEvidence(
                evidence_id=f"evidence:{hashlib.sha1(json.dumps(interface, sort_keys=True, default=str).encode()).hexdigest()[:12]}",
                role=str(interface.get("role", "unknown")),
                effective_name=interface.get("effective_name"),
                interface_type=interface.get("type"),
                controlling_parameter=interface.get("controlling_parameter"),
            ) for interface in instance.get("interfaces", [])] + interfaces
        related = sorted({
            relationship["target"] for relationship in item.get("relationships", [])
            if isinstance(relationship.get("target"), str)
        })
        packages.append(ParameterEvidence(
            parameter_id=item["template_key"], name=item["name"], kind=item["kind"],
            component_id=component_id, affected_components=affected_components,
            value_type=item["value_type"], default=item.get("default"),
            current_value=item.get("current_value"), declarations=_deduplicate_excerpts(declarations),
            usages=usages, ros_interfaces=interfaces, provenance=list(item.get("provenance", [])),
            source_relationships=source_relationships, related_parameters=related,
        ))
    return packages


def evidence_by_parameter_id(evidence: list[ParameterEvidence]) -> dict[str, dict[str, Any]]:
    return {item.parameter_id: item.model_dump(mode="json") for item in evidence}


def build_semantic_enrichment_prompt(template: str, evidence: list[ParameterEvidence]) -> str:
    replacements = {
        SEMANTIC_EVIDENCE_PLACEHOLDER: [item.model_dump(mode="json") for item in evidence],
        SEMANTIC_ENRICHMENT_SCHEMA_PLACEHOLDER: SemanticEnrichmentBatch.model_json_schema(),
    }
    prompt = template
    for placeholder, value in replacements.items():
        if prompt.count(placeholder) != 1:
            raise ValueError(f"prompt must contain exactly one {placeholder} placeholder")
        prompt = prompt.replace(placeholder, json.dumps(value, indent=2, sort_keys=True))
    return prompt.rstrip()


class SemanticEnricher:
    """Obtain auditable semantic hypotheses without modifying deterministic facts."""

    def __init__(self, backend: SemanticBackend, *, prompt_template: str) -> None:
        self.backend = backend
        self.prompt_template = prompt_template

    def enrich(
        self,
        evidence: list[ParameterEvidence],
        *,
        known_parameter_ids: set[str] | None = None,
    ) -> SemanticEnrichmentBatch:
        from .inference import extract_json_object

        prompt = build_semantic_enrichment_prompt(self.prompt_template, evidence)
        raw = self.backend(prompt, "Interpret the supplied parameter evidence.")
        batch = SemanticEnrichmentBatch.model_validate(extract_json_object(raw))
        requested = {item.parameter_id for item in evidence}
        returned = {item.parameter_id for item in batch.enrichments}
        if returned != requested:
            raise ValueError(
                f"semantic enrichment IDs differ from the requested batch: "
                f"missing={sorted(requested - returned)}, unexpected={sorted(returned - requested)}"
            )
        known = known_parameter_ids or requested
        invalid_relationships = sorted({
            target for item in batch.enrichments for target in item.related_parameters
            if target not in known
        })
        if invalid_relationships:
            raise ValueError(f"semantic enrichment references unknown parameters: {invalid_relationships}")
        evidence_ids = {
            item.parameter_id: {
                reference.evidence_id for reference in [*item.declarations, *item.usages, *item.ros_interfaces]
            }
            for item in evidence
        }
        invalid_evidence = sorted({
            evidence_id
            for item in batch.enrichments
            for evidence_id in item.evidence_ids
            if evidence_id not in evidence_ids[item.parameter_id]
        })
        if invalid_evidence:
            raise ValueError(f"semantic enrichment references unknown evidence: {invalid_evidence}")
        return batch


def enrichment_by_parameter_id(
    enrichments: SemanticEnrichmentBatch | SemanticEnrichmentArtifact | list[SemanticEnrichment],
) -> dict[str, dict[str, Any]]:
    values = enrichments.enrichments if isinstance(
        enrichments, (SemanticEnrichmentBatch, SemanticEnrichmentArtifact)
    ) else enrichments
    return {item.parameter_id: item.model_dump(mode="json") for item in values}


def _payload_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def build_semantic_enrichment_artifact(
    batch: SemanticEnrichmentBatch,
    *,
    source_model: dict[str, Any],
    configuration_model: dict[str, Any],
    model_identifier: str,
    prompt_version: str,
) -> SemanticEnrichmentArtifact:
    return SemanticEnrichmentArtifact(
        source_model_sha256=_payload_digest(source_model),
        configuration_model_sha256=_payload_digest(configuration_model),
        model_identifier=model_identifier, prompt_version=prompt_version,
        enrichments=batch.enrichments,
    )


def build_parameter_evidence_artifact(
    evidence: list[ParameterEvidence],
    *,
    source_model: dict[str, Any],
    configuration_model: dict[str, Any],
) -> ParameterEvidenceArtifact:
    return ParameterEvidenceArtifact(
        source_model_sha256=_payload_digest(source_model),
        configuration_model_sha256=_payload_digest(configuration_model),
        parameters=evidence,
    )


def parameter_evidence_artifact_matches(
    artifact: ParameterEvidenceArtifact,
    *,
    source_model: dict[str, Any],
    configuration_model: dict[str, Any],
) -> bool:
    return (
        artifact.source_model_sha256 == _payload_digest(source_model)
        and artifact.configuration_model_sha256 == _payload_digest(configuration_model)
    )


def semantic_enrichment_artifact_matches(
    artifact: SemanticEnrichmentArtifact,
    *,
    source_model: dict[str, Any],
    configuration_model: dict[str, Any],
) -> bool:
    return (
        artifact.source_model_sha256 == _payload_digest(source_model)
        and artifact.configuration_model_sha256 == _payload_digest(configuration_model)
    )


def write_parameter_evidence(artifact: ParameterEvidenceArtifact, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return output


def load_parameter_evidence_artifact(path: str | Path) -> ParameterEvidenceArtifact:
    return ParameterEvidenceArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write_semantic_enrichment(
    artifact: SemanticEnrichmentArtifact,
    path: str | Path,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return output


def load_semantic_enrichment_artifact(path: str | Path) -> SemanticEnrichmentArtifact:
    return SemanticEnrichmentArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))
