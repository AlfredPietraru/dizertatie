"""Contracts for controlled, intent-first synthetic mission generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from .schema import (
    AntRobotCapabilityRegistry, CapabilitySelections, MissionModel, ParameterCatalogue,
    ParameterChange, RendererValue, build_template_configuration_plan, realize_capabilities,
    validate_parameter_changes,
)


class SeedBase(MissionModel):
    id: str = Field(pattern=r"^seed-[0-9]{3}$")
    intent_description: str = Field(min_length=8)
    strata: list[str] = Field(min_length=1)
    paraphrase_count: int = Field(ge=1, le=20)
    review_status: Literal["human_verified"] = "human_verified"


class SeedConfiguration(MissionModel):
    capabilities: CapabilitySelections = Field(default_factory=CapabilitySelections)


class ValidSeed(SeedBase):
    outcome: Literal["supported"]
    configuration: SeedConfiguration
    expected_template_configuration: dict[str, RendererValue]

    @property
    def capabilities(self) -> CapabilitySelections:
        return self.configuration.capabilities

class UnsupportedSeed(SeedBase):
    outcome: Literal["unsupported"]
    category: Literal["sensor", "algorithm", "odometry", "capability", "invalid_value"]
    reason: str = Field(min_length=8)


class ClarificationSeed(SeedBase):
    outcome: Literal["ambiguous"]
    category: Literal[
        "missing_numeric_value", "unresolved_alternative", "qualitative_choice",
        "conflicting_requirements", "ambiguous_reference",
    ]
    reason: str = Field(min_length=8)
    clarification_question: str = Field(min_length=8)


SyntheticSeed = Annotated[ValidSeed | UnsupportedSeed | ClarificationSeed, Field(discriminator="outcome")]
SEED_ADAPTER = TypeAdapter(SyntheticSeed)


class GoldParameterChange(MissionModel):
    parameter_id: str
    value: RendererValue
    absolute_tolerance: float = Field(default=1e-9, ge=0.0)


class ParameterReasoningGold(MissionModel):
    status: Literal["valid", "no_change", "unsupported", "needs_clarification"]
    changes: list[GoldParameterChange] = Field(default_factory=list)
    reason: str | None = None
    clarification_question: str | None = None

    @model_validator(mode="after")
    def check_status(self) -> "ParameterReasoningGold":
        if self.status == "valid" and not self.changes:
            raise ValueError("valid parameter-reasoning gold requires changes")
        if self.status != "valid" and self.changes:
            raise ValueError("terminal/no-change parameter gold cannot contain changes")
        if self.status in {"unsupported", "needs_clarification"} and not self.reason:
            raise ValueError("terminal parameter gold requires a reason")
        if self.status == "needs_clarification" and not self.clarification_question:
            raise ValueError("clarification gold requires a question")
        identifiers = [item.parameter_id for item in self.changes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("parameter gold contains duplicate IDs")
        return self


class ParameterTaskExpectation(MissionModel):
    parameter_reasoning: ParameterReasoningGold


class ParameterReasoningTask(MissionModel):
    id: str = Field(pattern=r"^parameter-[0-9]{3}$")
    mission: str = Field(min_length=3)
    outcome: Literal["supported", "unsupported", "ambiguous"]
    difficulty: Literal["explicit", "semantic", "physical", "behavioral", "multi_component",
                        "ambiguous", "unsupported", "no_change"]
    strata: list[str] = Field(min_length=1)
    review_status: Literal["pending_human_review", "human_verified"]
    expected: ParameterTaskExpectation

    @model_validator(mode="after")
    def align_outcome(self) -> "ParameterReasoningTask":
        expected_status = {
            "supported": {"valid", "no_change"},
            "unsupported": {"unsupported"},
            "ambiguous": {"needs_clarification"},
        }[self.outcome]
        if self.expected.parameter_reasoning.status not in expected_status:
            raise ValueError("task outcome and expected parameter status disagree")
        return self


def load_parameter_reasoning_tasks(
    path: str | Path,
    catalogue: ParameterCatalogue | None = None,
) -> list[ParameterReasoningTask]:
    tasks = [ParameterReasoningTask.model_validate_json(line)
             for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    identifiers = [task.id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("parameter reasoning task identifiers must be unique")
    if catalogue is not None:
        for task in tasks:
            if task.expected.parameter_reasoning.status != "valid":
                continue
            validate_parameter_changes([
                ParameterChange(parameter_id=item.parameter_id, value=item.value)
                for item in task.expected.parameter_reasoning.changes
            ], catalogue)
    return tasks


def load_synthetic_seeds(
    path: str | Path,
    registry: AntRobotCapabilityRegistry,
) -> list[ValidSeed | UnsupportedSeed | ClarificationSeed]:
    seeds = [SEED_ADAPTER.validate_python(json.loads(line))
             for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    identifiers = [seed.id for seed in seeds]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("synthetic seed identifiers must be unique")
    for seed in seeds:
        if isinstance(seed, ValidSeed):
            mapped = build_template_configuration_plan(
                realize_capabilities(seed.capabilities, registry), {},
            ).user_values
            mismatches = {
                key: {"mapped": value, "expected": seed.expected_template_configuration.get(key)}
                for key, value in mapped.items()
                if seed.expected_template_configuration.get(key) != value
            }
            if mismatches:
                raise ValueError(
                    f"{seed.id}: capability realization differs from expected configuration: {mismatches}"
                )
    return seeds


def summarize_synthetic_seeds(seeds: list[ValidSeed | UnsupportedSeed | ClarificationSeed]) -> dict:
    outcomes = {name: [seed for seed in seeds if seed.outcome == name]
                for name in ("supported", "unsupported", "ambiguous")}
    return {
        "seed_count": len(seeds),
        "candidate_target": sum(seed.paraphrase_count for seed in seeds),
        "outcomes": {name: {"seeds": len(values),
                             "candidates": sum(seed.paraphrase_count for seed in values)}
                     for name, values in outcomes.items()},
        "strata": sorted({stratum for seed in seeds for stratum in seed.strata}),
    }
