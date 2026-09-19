"""Separated LLM parameter selection and value-generation stages."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from .retrieval import (
    ContextVariant, ParameterRetrieval, SelectionContext, build_selection_context,
    retrieve_parameters,
)
from .schema import (
    MissionModel, ParameterCatalogue, ParameterChange, RendererValue,
    ROSOrchestrationPlan, SystemRealization,
    validate_parameter_changes,
)


SELECTION_CONTEXT_PLACEHOLDER = "{{SELECTION_CONTEXT}}"
SYSTEM_CONTEXT_PLACEHOLDER = "{{SYSTEM_CONTEXT}}"
VALUE_CONTEXT_PLACEHOLDER = "{{VALUE_CONTEXT}}"
VALUE_SCHEMA_PLACEHOLDER = "{{PARAMETER_VALUE_JSON_SCHEMA}}"


class ReasoningBackend(Protocol):
    def __call__(self, system_prompt: str, user_prompt: str) -> str: ...


class SelectedParameter(MissionModel):
    parameter_id: str
    relevance: str = Field(min_length=3)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ParameterSelectionInterpretation(MissionModel):
    status: Literal["valid", "no_change", "unsupported", "needs_clarification"]
    interpretation: str | None = None
    selected_parameters: list[SelectedParameter] = Field(default_factory=list)
    reason: str | None = None
    clarification_question: str | None = None

    @model_validator(mode="after")
    def check_status(self) -> "ParameterSelectionInterpretation":
        if self.status == "valid":
            if not self.selected_parameters:
                raise ValueError("valid parameter selection requires at least one selected parameter")
        elif self.status == "no_change":
            if self.selected_parameters or not self.reason or self.clarification_question is not None:
                raise ValueError("no_change requires a reason and no selected parameters")
        elif self.status == "unsupported":
            if self.selected_parameters or not self.reason or self.clarification_question is not None:
                raise ValueError("unsupported selection requires a reason and no selected parameters")
        elif self.selected_parameters or not self.reason or not self.clarification_question:
            raise ValueError("needs_clarification requires a reason, question, and no selection")
        identifiers = [item.parameter_id for item in self.selected_parameters]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("parameter selection contains duplicate IDs")
        return self


class ProposedParameterChange(MissionModel):
    parameter_id: str
    old_value: RendererValue
    new_value: RendererValue
    reason: str = Field(min_length=3)
    request_evidence: str | None = None


class ParameterValueInterpretation(MissionModel):
    status: Literal["valid", "unsupported", "needs_clarification"]
    interpretation: str | None = None
    changes: list[ProposedParameterChange] = Field(default_factory=list)
    dependencies_considered: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    reason: str | None = None
    clarification_question: str | None = None

    @model_validator(mode="after")
    def check_status(self) -> "ParameterValueInterpretation":
        if self.status == "valid":
            if not self.changes:
                raise ValueError("valid value interpretation requires at least one change")
            if self.reason is not None or self.clarification_question is not None:
                raise ValueError("valid value interpretation cannot contain terminal fields")
        elif self.status == "unsupported":
            if self.changes or not self.reason or self.clarification_question is not None:
                raise ValueError("unsupported value interpretation requires a reason and no changes")
        elif self.changes or not self.reason or not self.clarification_question:
            raise ValueError("needs_clarification requires a reason, question, and no changes")
        identifiers = [item.parameter_id for item in self.changes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("value interpretation contains duplicate parameter IDs")
        return self


class ParameterReasoningResult(MissionModel):
    status: Literal["valid", "no_change", "unsupported", "needs_clarification"]
    retrieval: ParameterRetrieval
    selection_context: SelectionContext
    selection: ParameterSelectionInterpretation
    value_interpretation: ParameterValueInterpretation | None = None
    validated_values: dict[str, RendererValue] = Field(default_factory=dict)


def _replace_json_placeholders(template: str, replacements: dict[str, Any]) -> str:
    prompt = template
    for placeholder, value in replacements.items():
        if prompt.count(placeholder) != 1:
            raise ValueError(f"prompt must contain exactly one {placeholder} placeholder")
        prompt = prompt.replace(
            placeholder,
            json.dumps(value, sort_keys=True, separators=(",", ":")),
        )
    return prompt.rstrip()


def build_parameter_selection_prompt(
    template: str,
    context: SelectionContext,
    *,
    system_context: dict[str, Any] | None = None,
) -> str:
    return _replace_json_placeholders(template, {
        SELECTION_CONTEXT_PLACEHOLDER: context.model_dump(mode="json"),
        SYSTEM_CONTEXT_PLACEHOLDER: system_context or {},
    })


def build_parameter_selection_system_context(
    ros_orchestration: dict[str, Any],
) -> dict[str, Any]:
    """Keep only deployment eligibility facts needed during parameter selection."""
    active_components = sorted({
        item["component_id"]
        for item in ros_orchestration.get("active_components", [])
        if isinstance(item, dict) and isinstance(item.get("component_id"), str)
    })
    return {
        "active_components": active_components,
        "inactive_components": sorted(ros_orchestration.get("inactive_components", [])),
    }


def build_parameter_value_prompt(
    template: str,
    selection: ParameterSelectionInterpretation,
    context: SelectionContext,
) -> str:
    selected = {item.parameter_id for item in selection.selected_parameters}
    records = [record for record in context.records if record["parameter_id"] in selected]
    neighbors = [edge for edge in context.graph_edges
                 if edge["source"] in selected or edge["target"] in selected]
    value_context = {
        "selection": selection.model_dump(mode="json"),
        "selected_records": records,
        "relevant_relationships": neighbors,
    }
    return _replace_json_placeholders(template, {
        VALUE_CONTEXT_PLACEHOLDER: value_context,
        VALUE_SCHEMA_PLACEHOLDER: ParameterValueInterpretation.model_json_schema(),
    })


def _validation_repair_user_prompt(
    mission: str,
    previous_response: str,
    error: ValueError,
    *,
    stage: Literal["parameter_selection", "parameter_value"],
) -> str:
    """Ask once for a corrected result while preserving the original system context."""
    repair = {
        "instruction": (
            "Correct the previous response so it passes deterministic validation. "
            "Return only the corrected JSON object required by the system prompt."
        ),
        "stage": stage,
        "original_mission": mission,
        "validation_error": str(error),
        "previous_response": previous_response,
    }
    return "Deterministic validation rejected the previous response:\n" + json.dumps(
        repair, sort_keys=True, separators=(",", ":"),
    )


def validate_parameter_selection(
    selection: ParameterSelectionInterpretation,
    context: SelectionContext,
) -> None:
    supplied = set(context.included_parameter_ids)
    unknown = sorted({item.parameter_id for item in selection.selected_parameters} - supplied)
    if unknown:
        raise ValueError(f"parameter selection contains IDs absent from its context: {unknown}")


def validate_parameter_values(
    interpretation: ParameterValueInterpretation,
    selection: ParameterSelectionInterpretation,
    catalogue: ParameterCatalogue,
) -> dict[str, RendererValue]:
    if interpretation.status != "valid":
        return {}
    selected = {item.parameter_id for item in selection.selected_parameters}
    changed = {item.parameter_id for item in interpretation.changes}
    introduced = sorted(changed - selected)
    if introduced:
        raise ValueError(f"value reasoning introduced unselected parameters: {introduced}")
    by_id = {item.parameter_id: item for item in catalogue.parameters}
    changes: list[ParameterChange] = []
    for proposal in interpretation.changes:
        current = by_id[proposal.parameter_id].current_value
        if proposal.old_value != current:
            raise ValueError(
                f"stale old_value for {proposal.parameter_id!r}: "
                f"expected {current!r}, got {proposal.old_value!r}"
            )
        changes.append(ParameterChange(
            parameter_id=proposal.parameter_id, value=proposal.new_value,
            evidence=proposal.request_evidence or proposal.reason,
        ))
    return validate_parameter_changes(changes, catalogue)


class ParameterReasoner:
    """Retrieve context, select parameters, and generate values in distinct calls."""

    def __init__(
        self,
        selection_backend: ReasoningBackend,
        value_backend: ReasoningBackend,
        *,
        selection_prompt_template: str,
        value_prompt_template: str,
        context_variant: ContextVariant = "graph",
        top_k: int = 10,
        graph_hops: int = 1,
        max_candidates: int = 24,
        character_budget: int = 40000,
    ) -> None:
        self.selection_backend = selection_backend
        self.value_backend = value_backend
        self.selection_prompt_template = selection_prompt_template
        self.value_prompt_template = value_prompt_template
        self.context_variant = context_variant
        self.top_k = top_k
        self.graph_hops = graph_hops
        self.max_candidates = max_candidates
        self.character_budget = character_budget

    def reason(
        self,
        mission: str,
        catalogue: ParameterCatalogue,
        *,
        system_context: dict[str, Any] | None = None,
        wiring_bindings: dict[str, dict[str, Any]] | None = None,
    ) -> ParameterReasoningResult:
        from .inference import extract_json_object

        if not mission.strip():
            raise ValueError("mission must not be empty")
        retrieval = retrieve_parameters(
            mission, catalogue, variant=self.context_variant, top_k=self.top_k,
            graph_hops=self.graph_hops, max_candidates=self.max_candidates,
            wiring_bindings=wiring_bindings,
        )
        context = build_selection_context(
            retrieval, catalogue, character_budget=self.character_budget,
        )
        selection_prompt = build_parameter_selection_prompt(
            self.selection_prompt_template, context, system_context=system_context,
        )
        selection_raw = self.selection_backend(selection_prompt, mission.strip())
        try:
            selection = ParameterSelectionInterpretation.model_validate(
                extract_json_object(selection_raw)
            )
            validate_parameter_selection(selection, context)
        except ValueError as error:
            repair_prompt = _validation_repair_user_prompt(
                mission.strip(), selection_raw, error, stage="parameter_selection",
            )
            selection_raw = self.selection_backend(selection_prompt, repair_prompt)
            selection = ParameterSelectionInterpretation.model_validate(
                extract_json_object(selection_raw)
            )
            validate_parameter_selection(selection, context)
        if selection.status != "valid":
            return ParameterReasoningResult(
                status=selection.status, retrieval=retrieval, selection_context=context,
                selection=selection,
            )
        value_prompt = build_parameter_value_prompt(
            self.value_prompt_template, selection, context,
        )
        value_raw = self.value_backend(value_prompt, mission.strip())
        try:
            value_interpretation = ParameterValueInterpretation.model_validate(
                extract_json_object(value_raw)
            )
            values = validate_parameter_values(value_interpretation, selection, catalogue)
        except ValueError as error:
            repair_prompt = _validation_repair_user_prompt(
                mission.strip(), value_raw, error, stage="parameter_value",
            )
            value_raw = self.value_backend(value_prompt, repair_prompt)
            value_interpretation = ParameterValueInterpretation.model_validate(
                extract_json_object(value_raw)
            )
            values = validate_parameter_values(value_interpretation, selection, catalogue)
        if value_interpretation.status != "valid":
            return ParameterReasoningResult(
                status=value_interpretation.status, retrieval=retrieval,
                selection_context=context, selection=selection,
                value_interpretation=value_interpretation,
            )
        return ParameterReasoningResult(
            status="valid", retrieval=retrieval, selection_context=context,
            selection=selection, value_interpretation=value_interpretation,
            validated_values=values,
        )
