#!/usr/bin/env python3
"""Generate the simplified AntRobot mission dataset from verified seed labels."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ros_config_builder.mission import (
    CandidateReview,
    OllamaParaphraseBackend,
    automatically_validate_candidates,
    generate_paraphrases,
    load_capability_registry,
    load_candidates,
    load_synthetic_seeds,
    write_candidate_history,
)
from ros_config_builder.mission.generation import GeneratedBatch, SyntheticCandidate


@dataclass(frozen=True)
class DatasetGenerationConfig:
    model: str
    candidates_per_seed: int
    seed_path: Path
    dataset_path: Path
    metadata_path: Path
    configuration_schema_path: Path
    capability_registry_path: Path
    candidate_history_path: Path
    raw_directory: Path
    semantic_rejection_path: Path
    manual_repair_path: Path
    first_batch_styles: list[str]
    second_batch_styles: list[str]
    semantic_rejections: dict[str, str]
    additional_semantic_rejections: set[tuple[str, str]]
    manual_replacements: dict[str, str]
    start_seed_index: int


def load_generation_config(path: Path) -> DatasetGenerationConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("dataset generation config must be a YAML object")
    required = {
        "model", "seed_path", "dataset_path", "metadata_path",
        "configuration_schema_path", "capability_registry_path",
        "candidate_history_path", "raw_directory", "semantic_rejection_path",
        "manual_repair_path", "first_batch_styles", "second_batch_styles",
        "semantic_rejections", "additional_semantic_rejections", "manual_replacements",
        "start_seed_index", "candidates_per_seed",
    }
    missing = sorted(required - raw.keys())
    unknown = sorted(raw.keys() - required)
    if missing or unknown:
        raise ValueError(f"invalid config keys; missing={missing}, unknown={unknown}")
    additional = raw["additional_semantic_rejections"]
    if not isinstance(additional, list):
        raise ValueError("additional_semantic_rejections must be a list")
    candidates_per_seed = int(raw["candidates_per_seed"])
    if candidates_per_seed < 1:
        raise ValueError("candidates_per_seed must be at least 1")
    available_styles = len(raw["first_batch_styles"]) + len(raw["second_batch_styles"])
    if candidates_per_seed > available_styles:
        raise ValueError(
            f"candidates_per_seed={candidates_per_seed} exceeds the "
            f"{available_styles} configured style slots"
        )
    return DatasetGenerationConfig(
        model=str(raw["model"]),
        candidates_per_seed=candidates_per_seed,
        seed_path=Path(raw["seed_path"]),
        dataset_path=Path(raw["dataset_path"]),
        metadata_path=Path(raw["metadata_path"]),
        configuration_schema_path=Path(raw["configuration_schema_path"]),
        capability_registry_path=Path(raw["capability_registry_path"]),
        candidate_history_path=Path(raw["candidate_history_path"]),
        raw_directory=Path(raw["raw_directory"]),
        semantic_rejection_path=Path(raw["semantic_rejection_path"]),
        manual_repair_path=Path(raw["manual_repair_path"]),
        first_batch_styles=list(raw["first_batch_styles"]),
        second_batch_styles=list(raw["second_batch_styles"]),
        semantic_rejections=dict(raw["semantic_rejections"]),
        additional_semantic_rejections={
            (str(item["candidate_id"]), str(item["text"])) for item in additional
        },
        manual_replacements=dict(raw["manual_replacements"]),
        start_seed_index=int(raw["start_seed_index"]),
    )


def _load_environment(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _generation_batches(config: DatasetGenerationConfig) -> list[tuple[str, list[str]]]:
    remaining = config.candidates_per_seed
    batches = []
    for series, configured_styles in (
        ("a", config.first_batch_styles),
        ("b", config.second_batch_styles),
    ):
        styles = configured_styles[:remaining]
        if styles:
            batches.append((series, styles))
            remaining -= len(styles)
    return batches


def _candidates_from_raw(seed, config: DatasetGenerationConfig) -> list[SyntheticCandidate] | None:
    batches = []
    for series, styles in _generation_batches(config):
        path = config.raw_directory / f"{seed.id}-batch-{series}.json"
        if not path.is_file():
            return None
        batch = GeneratedBatch.model_validate_json(path.read_text(encoding="utf-8"))
        returned_styles = [item.requested_style for item in batch.candidates]
        if returned_styles[:len(styles)] != styles:
            return None
        batches.extend(batch.candidates[:len(styles)])
    candidates = [
        SyntheticCandidate(
            candidate_id=f"{seed.id}-p{index:02d}",
            seed_id=seed.id,
            outcome=seed.outcome,
            requested_style=item.requested_style,
            text=item.text.strip(),
            generator_model=config.model,
        )
        for index, item in enumerate(batches, 1)
    ]
    if len(candidates) != config.candidates_per_seed:
        return None
    return candidates


def _validate_partial_history(candidates, seeds_by_id, candidates_per_seed: int) -> set[str]:
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate checkpoint contains duplicate candidate IDs")
    unknown = sorted({candidate.seed_id for candidate in candidates} - set(seeds_by_id))
    if unknown:
        raise ValueError(f"candidate checkpoint contains unknown seed IDs: {unknown}")
    completed = set()
    for seed_id, seed in seeds_by_id.items():
        count = sum(candidate.seed_id == seed_id for candidate in candidates)
        if count not in (0, candidates_per_seed):
            raise ValueError(
                f"candidate checkpoint has partial seed {seed_id}: "
                f"{count}/{candidates_per_seed} candidates"
            )
        if count:
            completed.add(seed_id)
    return completed


def _build_dataset_record(candidate, seed) -> dict:
    interpretation: dict[str, object]
    expected_parameters: dict[str, object] = {}
    if seed.outcome == "supported":
        interpretation = {
            "status": "valid",
            "capabilities": seed.capabilities.model_dump(mode="json"),
        }
        expected_parameters = {
            key: value
            for key, value in seed.expected_template_configuration.items()
            if key.startswith("nodes.")
        }
    elif seed.outcome == "unsupported":
        interpretation = {
            "status": "unsupported",
            "reason": seed.reason,
        }
    else:
        interpretation = {
            "status": "needs_clarification",
            "reason": seed.reason,
            "clarification_question": seed.clarification_question,
        }
    return {
        "id": candidate.candidate_id,
        "source_seed": seed.id,
        "mission": candidate.text,
        "outcome": seed.outcome,
        "strata": [*seed.strata, candidate.requested_style],
        "expected_capability_interpretation": interpretation,
        "expected_parameters": expected_parameters,
    }


def _repair_rejected_candidates(candidates, seeds_by_id, backend,
                                config: DatasetGenerationConfig) -> None:
    for attempt in range(1, 6):
        rejected_indexes = [
            index
            for index, candidate in enumerate(candidates)
            if candidate.review.status == "rejected"
        ]
        if not rejected_indexes:
            return
        print(
            f"repairing {len(rejected_indexes)} rejected candidates, attempt {attempt}",
            flush=True,
        )
        for index in rejected_indexes:
            rejected = candidates[index]
            excluded = [
                candidate.text
                for candidate_index, candidate in enumerate(candidates)
                if candidate_index != index and candidate.seed_id == rejected.seed_id
            ]
            replacements, raw = generate_paraphrases(
                seeds_by_id[rejected.seed_id],
                backend,
                requested_styles=["natural"],
                candidate_series="r",
                replacement=True,
                excluded_texts=excluded,
            )
            replacement = replacements[0]
            replacement.candidate_id = rejected.candidate_id
            candidates[index] = replacement
            (config.raw_directory / f"{rejected.candidate_id}-repair-{attempt}.json").write_text(
                raw + "\n", encoding="utf-8"
            )
        automatically_validate_candidates(candidates)
        write_candidate_history(candidates, config.candidate_history_path)
    rejected_ids = [
        candidate.candidate_id
        for candidate in candidates
        if candidate.review.status == "rejected"
    ]
    raise ValueError(f"could not produce unique replacements for {rejected_ids}")


def _apply_semantic_review(candidates, config: DatasetGenerationConfig) -> None:
    rejected_records = []
    if config.semantic_rejection_path.is_file():
        rejected_records = json.loads(
            config.semantic_rejection_path.read_text(encoding="utf-8")
        )
    logged = {
        (record["candidate_id"], record["rejected_text"])
        for record in rejected_records
    }
    for candidate in candidates:
        rejected_text = config.semantic_rejections.get(candidate.candidate_id)
        rejected_pair = (candidate.candidate_id, candidate.text)
        if (candidate.text != rejected_text
                and rejected_pair not in config.additional_semantic_rejections):
            continue
        candidate.review = CandidateReview(
            status="rejected",
            semantic_equivalence=False,
            natural_language_quality="unacceptable",
            rejection_reason="semantic_drift",
            notes="Rejected during post-generation semantic-preservation review.",
            reviewer="dataset-semantic-review",
        )
        if rejected_pair not in logged:
            rejected_records.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "rejected_text": candidate.text,
                    "reason": "semantic_drift_or_clarity_failure",
                }
            )
            logged.add(rejected_pair)
    if rejected_records:
        config.semantic_rejection_path.parent.mkdir(parents=True, exist_ok=True)
        config.semantic_rejection_path.write_text(
            json.dumps(rejected_records, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _apply_manual_repairs(candidates, config: DatasetGenerationConfig) -> None:
    repairs = []
    for candidate in candidates:
        replacement = config.manual_replacements.get(candidate.candidate_id)
        if replacement is None or candidate.text == replacement:
            continue
        repairs.append(
            {
                "candidate_id": candidate.candidate_id,
                "replaced_text": candidate.text,
                "replacement_text": replacement,
                "reason": "manual_semantic_preservation_repair",
            }
        )
        candidate.text = replacement
        candidate.requested_style = "natural"
        candidate.review = CandidateReview()
    if repairs:
        config.manual_repair_path.parent.mkdir(parents=True, exist_ok=True)
        config.manual_repair_path.write_text(
            json.dumps(repairs, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an AntRobot mission dataset from an explicit seed set."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="YAML file containing all dataset-generation inputs and output paths.",
    )
    return parser


def run(config_path: Path) -> int:
    _load_environment()
    config = load_generation_config(config_path)
    registry = load_capability_registry(config.capability_registry_path)
    seeds = load_synthetic_seeds(config.seed_path, registry)
    if not seeds:
        raise ValueError("seed file must contain at least one seed")
    if not 1 <= config.start_seed_index <= len(seeds):
        raise ValueError(
            f"start_seed_index must be between 1 and {len(seeds)}, "
            f"found {config.start_seed_index}"
        )
    expected_records = len(seeds) * config.candidates_per_seed

    schema = json.loads(config.configuration_schema_path.read_text(encoding="utf-8"))
    configuration_keys = set(schema["configuration_keys"])
    expected_parameter_keys = {
        key
        for seed in seeds
        for key in getattr(seed, "expected_template_configuration", {})
        if key.startswith("nodes.")
    }
    unknown_parameter_keys = sorted(expected_parameter_keys - configuration_keys)
    if unknown_parameter_keys:
        raise ValueError(f"seed annotations contain unknown parameters: {unknown_parameter_keys}")

    backend = OllamaParaphraseBackend(
        host=os.getenv("OLLAMA_HOST_PATH", "10.0.2.2"),
        model=config.model,
    )
    seeds_by_id = {seed.id: seed for seed in seeds}
    if config.candidate_history_path.is_file():
        candidates = load_candidates(config.candidate_history_path)
        completed_seed_ids = _validate_partial_history(
            candidates, seeds_by_id, config.candidates_per_seed
        )
        print(
            f"resuming checkpoint with {len(completed_seed_ids)}/{len(seeds)} seeds "
            f"and {len(candidates)}/{expected_records} candidates",
            flush=True,
        )
    else:
        candidates = []
        completed_seed_ids = set()
    config.raw_directory.mkdir(parents=True, exist_ok=True)
    recovered_seed_count = 0
    for seed in seeds:
        if seed.id in completed_seed_ids:
            continue
        recovered = _candidates_from_raw(seed, config)
        if recovered is None:
            continue
        candidates.extend(recovered)
        completed_seed_ids.add(seed.id)
        recovered_seed_count += 1
    if recovered_seed_count:
        write_candidate_history(candidates, config.candidate_history_path)
        print(
            f"recovered {recovered_seed_count} seeds from raw responses; checkpoint now "
            f"contains {len(completed_seed_ids)}/{len(seeds)} seeds",
            flush=True,
        )
    for index, seed in enumerate(seeds, 1):
        if index < config.start_seed_index or seed.id in completed_seed_ids:
            continue
        try:
            generated = []
            for series, styles in _generation_batches(config):
                batch, raw = generate_paraphrases(
                    seed,
                    backend,
                    requested_styles=styles,
                    candidate_series=series,
                    excluded_texts=[candidate.text for candidate in generated],
                )
                generated.extend(batch)
                (config.raw_directory / f"{seed.id}-batch-{series}.json").write_text(
                    raw + "\n", encoding="utf-8"
                )
            for candidate_index, candidate in enumerate(generated, 1):
                candidate.candidate_id = f"{seed.id}-p{candidate_index:02d}"
            if len(generated) != config.candidates_per_seed:
                raise ValueError(
                    f"{seed.id} produced {len(generated)} paraphrases instead of "
                    f"{config.candidates_per_seed}"
                )
            candidates.extend(generated)
            completed_seed_ids.add(seed.id)
            write_candidate_history(candidates, config.candidate_history_path)
            print(f"generated {index}/{len(seeds)}: {seed.id}", flush=True)
        except Exception:
            print(
                f"generation stopped at seed index {index} ({seed.id}); "
                f"checkpoint contains {len(completed_seed_ids)}/{len(seeds)} completed seeds",
                flush=True,
            )
            raise

    missing_seed_ids = [seed.id for seed in seeds if seed.id not in completed_seed_ids]
    if missing_seed_ids:
        raise ValueError(
            "generation is incomplete; checkpoint was preserved. Missing seeds: "
            f"{missing_seed_ids}. Lower start_seed_index or resume from the first missing seed."
        )

    _apply_manual_repairs(candidates, config)
    automatically_validate_candidates(candidates)
    _apply_semantic_review(candidates, config)
    write_candidate_history(candidates, config.candidate_history_path)
    _repair_rejected_candidates(candidates, seeds_by_id, backend, config)
    rejected = [candidate for candidate in candidates if candidate.review.status == "rejected"]
    if rejected:
        summary = [
            f"{candidate.candidate_id}: {candidate.review.rejection_reason}"
            for candidate in rejected
        ]
        raise ValueError(f"automatic validation rejected candidates: {summary}")
    if len(candidates) != expected_records:
        raise ValueError(f"expected {expected_records} candidates, found {len(candidates)}")
    normalized = [_normalized(candidate.text) for candidate in candidates]
    if len(set(normalized)) != len(normalized):
        raise ValueError("generated paraphrases are not globally unique")

    records = [
        _build_dataset_record(candidate, seeds_by_id[candidate.seed_id])
        for candidate in candidates
    ]
    _write_jsonl(records, config.dataset_path)

    metadata = {
        "schema_version": "1.0",
        "generator_model": config.model,
        "model_generated_record_count": expected_records - len(config.manual_replacements),
        "manually_repaired_record_count": len(config.manual_replacements),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed_file": str(config.seed_path),
        "dataset_file": str(config.dataset_path),
        "seed_count": len(seeds),
        "paraphrases_per_seed": config.candidates_per_seed,
        "record_count": len(records),
        "outcome_counts": {
            outcome: sum(seed.outcome == outcome for seed in seeds)
            * config.candidates_per_seed
            for outcome in ("supported", "unsupported", "ambiguous")
        },
        "ground_truth_source": "repository-grounded seed annotations",
        "ground_truth_generated_by_model": False,
        "paraphrase_review_status": "assistant_reviewed_pending_author_review",
        "candidate_history": str(config.candidate_history_path),
        "raw_response_directory": str(config.raw_directory),
        "semantic_rejection_log": str(config.semantic_rejection_path),
        "manual_repair_log": str(config.manual_repair_path),
    }
    config.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    config.metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args().config))
