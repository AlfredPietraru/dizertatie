#!/usr/bin/env python3
"""Generate the simplified AntRobot mission dataset from verified seed labels."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

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


MODEL = "qwen2.5-coder:7b"
SEED_PATH = Path("data/antrobot_seed_missions_v1.jsonl")
DATASET_PATH = Path("data/antrobot_mission_dataset_v1.jsonl")
METADATA_PATH = Path("data/antrobot_mission_dataset_v1.metadata.json")
CONFIGURATION_SCHEMA_PATH = Path(
    "artifacts/template_configuration/template_configuration_schema.json"
)
GENERATION_DIRECTORY = Path("artifacts/dataset_generation/antrobot_missions_v1")
CANDIDATE_HISTORY_PATH = GENERATION_DIRECTORY / "candidates.jsonl"
RAW_DIRECTORY = GENERATION_DIRECTORY / "raw"
SEMANTIC_REJECTION_PATH = GENERATION_DIRECTORY / "semantic_rejections.json"
MANUAL_REPAIR_PATH = GENERATION_DIRECTORY / "manual_repairs.json"
EXPECTED_SEEDS = 15
EXPECTED_PARAPHRASES_PER_SEED = 10
EXPECTED_RECORDS = EXPECTED_SEEDS * EXPECTED_PARAPHRASES_PER_SEED
FIRST_BATCH_STYLES = [
    "canonical",
    "natural",
    "shorthand",
    "imperative",
    "negative_phrasing",
    "multi_clause",
]
SECOND_BATCH_STYLES = ["natural", "shorthand", "imperative", "multi_clause"]
SEMANTIC_REJECTIONS = {
    "seed-101-p05": "Do not use SLAM for mapping. Use Cartographer and default kinematic ICP odometry only.",
    "seed-101-p06": "Use Cartographer for mapping and the standard kinematic ICP odometry method for navigation.",
    "seed-101-p07": "Generate a map using Cartographer and its default kinematic ICP odometry approach.",
    "seed-101-p10": "Use Cartographer for mapping, employing the standard kinematic ICP odometry method for navigation.",
    "seed-102-p05": "Disable mapping and autonomous navigation separately.",
    "seed-102-p10": "First, enable mapping; then, enable autonomous navigation for seamless operation together.",
    "seed-103-p07": "Navigate through new areas, automatically creating a detailed map as you go.",
    "seed-106-p05": "Do not enable autonomous exploration, keep all other capabilities the same.",
    "seed-107-p05": "Disable everything except mapping, navigation, autonomous exploration, and KISS-ICP odometry.",
    "seed-109-p05": "Disable manual exploration and set the frontier planner frequency to 0.25 hertz.",
    "seed-110-p05": "Disable autonomous exploration and set the map publish rate to a non-zero value.",
    "seed-110-p10": "Begin enabling autonomous exploration, followed by configuring the map publish rate at 2 Hz.",
    "seed-111-p06": "For odometry, we will utilize KISS-ICP and limit the sensor range to no more than 5 meters.",
    "seed-111-p07": "Enable KISS-ICP odometry with a maximum sensor range of 5 meters for navigation.",
    "seed-113-p03": "Update wheel separation to 25 cm in all using components.",
    "seed-113-p05": "Remove any current setting of wheel separation and set it to 25 centimetres.",
    "seed-113-p10": "For all components utilizing wheel separation, set the distance to 25 centimetres, removing any previous settings.",
    "seed-114-p05": "Do not set both wheel encoders to produce more than 4096 counts per revolution.",
    "seed-115-p05": "Create a map with KISS-ICP, limiting the sensor range to no more than 20 metres, and ensure navigation is not used.",
}
ADDITIONAL_SEMANTIC_REJECTIONS = {
    ("seed-101-p05", "Disable autonomous exploration and keep all other capabilities intact."),
    ("seed-101-p06", "Enable mapping, autonomous exploration, and use the default kinematic ICP odometry pipeline with Cartographer."),
    ("seed-101-p10", "Enable mapping with Cartographer and use the default kinematic ICP odometry pipeline for autonomous exploration."),
    ("seed-102-p05", "Disable mapping, disable autonomous navigation."),
    ("seed-102-p10", "Enable mapping and autonomous navigation simultaneously, utilizing the default kinematic ICP odometry pipeline with Cartographer."),
    ("seed-103-p07", "Explore an unmapped environment autonomously while building its map using Cartographer and the default kinematic ICP odometry pipeline."),
    ("seed-107-p05", "Disable autonomous exploration, keep all other capabilities intact."),
    ("seed-109-p05", "Disable autonomous exploration and keep other capabilities active."),
    ("seed-110-p05", "Disable autonomous exploration and maintain other capabilities as they are."),
    ("seed-111-p06", "Enable mapping and use KISS-ICP odometry with a maximum sensor range of 5 metres while keeping navigation disabled."),
    ("seed-113-p05", "Disable autonomous exploration and keep all other capabilities active."),
    ("seed-113-p10", "Set the distance between the wheel centres to 25 centimetres in all components that use this parameter, and disable autonomous exploration while keeping other capabilities intact."),
    ("seed-114-p05", "Disable both mapping and autonomous exploration while configuring the encoders for 4096 counts per revolution."),
    ("seed-115-p05", "Build a map using KISS-ICP, but do not enable autonomous exploration or navigation. Limit the sensor range to 20 meters."),
}
MANUAL_REPLACEMENTS = {
    "seed-101-p05": "Create a map with Cartographer while using the default kinematic ICP odometry pipeline.",
    "seed-101-p06": "Use Cartographer to build the map and use kinematic ICP for odometry.",
    "seed-101-p10": "Configure Cartographer mapping together with the default kinematic ICP odometry pipeline.",
    "seed-102-p05": "Run the robot with both mapping and autonomous navigation enabled.",
    "seed-102-p10": "Activate autonomous navigation alongside the mapping capability.",
    "seed-103-p07": "Let the robot autonomously explore unfamiliar space and construct a map as it moves.",
    "seed-107-p05": "Turn on mapping, navigation, autonomous exploration, and KISS-ICP-based odometry.",
    "seed-109-p05": "Start autonomous exploration with the frontier planner operating at 0.25 hertz.",
    "seed-110-p05": "Run autonomous exploration and publish its map twice per second.",
    "seed-111-p06": "Select KISS-ICP odometry and set its maximum sensor range to exactly 5 metres.",
    "seed-113-p05": "Apply a 25-centimetre wheel separation to every component that uses this setting.",
    "seed-113-p10": "Use the same 25-centimetre distance between wheel centres in all components that consume wheel separation.",
    "seed-114-p05": "Set each of the two wheel encoders to exactly 4096 counts per revolution.",
    "seed-115-p05": "Use KISS-ICP to build the map, set the maximum sensor range to exactly 20 metres, and leave navigation disabled.",
}


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


def _repair_rejected_candidates(candidates, seeds_by_id, backend) -> None:
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
            (RAW_DIRECTORY / f"{rejected.candidate_id}-repair-{attempt}.json").write_text(
                raw + "\n", encoding="utf-8"
            )
        automatically_validate_candidates(candidates)
        write_candidate_history(candidates, CANDIDATE_HISTORY_PATH)
    rejected_ids = [
        candidate.candidate_id
        for candidate in candidates
        if candidate.review.status == "rejected"
    ]
    raise ValueError(f"could not produce unique replacements for {rejected_ids}")


def _apply_semantic_review(candidates) -> None:
    rejected_records = []
    if SEMANTIC_REJECTION_PATH.is_file():
        rejected_records = json.loads(SEMANTIC_REJECTION_PATH.read_text(encoding="utf-8"))
    logged = {
        (record["candidate_id"], record["rejected_text"])
        for record in rejected_records
    }
    for candidate in candidates:
        rejected_text = SEMANTIC_REJECTIONS.get(candidate.candidate_id)
        rejected_pair = (candidate.candidate_id, candidate.text)
        if candidate.text != rejected_text and rejected_pair not in ADDITIONAL_SEMANTIC_REJECTIONS:
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
        SEMANTIC_REJECTION_PATH.write_text(
            json.dumps(rejected_records, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _apply_manual_repairs(candidates) -> None:
    repairs = []
    for candidate in candidates:
        replacement = MANUAL_REPLACEMENTS.get(candidate.candidate_id)
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
        MANUAL_REPAIR_PATH.write_text(
            json.dumps(repairs, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def run() -> int:
    _load_environment()
    registry = load_capability_registry("configuration_templates/capability_registry.yaml")
    seeds = load_synthetic_seeds(SEED_PATH, registry)
    if len(seeds) != EXPECTED_SEEDS:
        raise ValueError(f"expected {EXPECTED_SEEDS} seeds, found {len(seeds)}")
    if any(seed.paraphrase_count != EXPECTED_PARAPHRASES_PER_SEED for seed in seeds):
        raise ValueError("every seed must request exactly ten paraphrases")

    schema = json.loads(CONFIGURATION_SCHEMA_PATH.read_text(encoding="utf-8"))
    configuration_keys = set(schema["configuration_keys"])
    expected_parameter_keys = {
        key
        for seed in seeds
        for key in seed.expected_template_configuration
        if key.startswith("nodes.")
    }
    unknown_parameter_keys = sorted(expected_parameter_keys - configuration_keys)
    if unknown_parameter_keys:
        raise ValueError(f"seed annotations contain unknown parameters: {unknown_parameter_keys}")

    backend = OllamaParaphraseBackend(
        host=os.getenv("OLLAMA_HOST_PATH", "10.0.2.2"),
        model=MODEL,
    )
    seeds_by_id = {seed.id: seed for seed in seeds}
    if CANDIDATE_HISTORY_PATH.is_file():
        candidates = load_candidates(CANDIDATE_HISTORY_PATH)
        expected_seed_ids = set(seeds_by_id)
        if len(candidates) != EXPECTED_RECORDS or {
            candidate.seed_id for candidate in candidates
        } != expected_seed_ids:
            raise ValueError("existing candidate history does not match the configured seed set")
        print(f"resuming {len(candidates)} generated candidates", flush=True)
    else:
        candidates = []
    RAW_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if not candidates:
        for index, seed in enumerate(seeds, 1):
            first_batch, first_raw = generate_paraphrases(
                seed,
                backend,
                requested_styles=FIRST_BATCH_STYLES,
                candidate_series="a",
            )
            second_batch, second_raw = generate_paraphrases(
                seed,
                backend,
                requested_styles=SECOND_BATCH_STYLES,
                candidate_series="b",
                excluded_texts=[candidate.text for candidate in first_batch],
            )
            generated = [*first_batch, *second_batch]
            for candidate_index, candidate in enumerate(generated, 1):
                candidate.candidate_id = f"{seed.id}-p{candidate_index:02d}"
            if len(generated) != EXPECTED_PARAPHRASES_PER_SEED:
                raise ValueError(
                    f"{seed.id} produced {len(generated)} paraphrases instead of "
                    f"{EXPECTED_PARAPHRASES_PER_SEED}"
                )
            candidates.extend(generated)
            (RAW_DIRECTORY / f"{seed.id}-batch-a.json").write_text(
                first_raw + "\n", encoding="utf-8"
            )
            (RAW_DIRECTORY / f"{seed.id}-batch-b.json").write_text(
                second_raw + "\n", encoding="utf-8"
            )
            print(f"generated {index}/{EXPECTED_SEEDS}: {seed.id}", flush=True)

    _apply_manual_repairs(candidates)
    automatically_validate_candidates(candidates)
    _apply_semantic_review(candidates)
    write_candidate_history(candidates, CANDIDATE_HISTORY_PATH)
    _repair_rejected_candidates(candidates, seeds_by_id, backend)
    rejected = [candidate for candidate in candidates if candidate.review.status == "rejected"]
    if rejected:
        summary = [
            f"{candidate.candidate_id}: {candidate.review.rejection_reason}"
            for candidate in rejected
        ]
        raise ValueError(f"automatic validation rejected candidates: {summary}")
    if len(candidates) != EXPECTED_RECORDS:
        raise ValueError(f"expected {EXPECTED_RECORDS} candidates, found {len(candidates)}")
    normalized = [_normalized(candidate.text) for candidate in candidates]
    if len(set(normalized)) != len(normalized):
        raise ValueError("generated paraphrases are not globally unique")

    records = []
    for candidate in candidates:
        seed = seeds_by_id[candidate.seed_id]
        expected_parameters = {
            key: value
            for key, value in seed.expected_template_configuration.items()
            if key.startswith("nodes.")
        }
        records.append(
            {
                "id": candidate.candidate_id,
                "source_seed": seed.id,
                "mission": candidate.text,
                "expected_capability_interpretation": {
                    "status": "valid",
                    "capabilities": seed.capabilities.model_dump(mode="json"),
                },
                "expected_parameters": expected_parameters,
            }
        )
    _write_jsonl(records, DATASET_PATH)

    metadata = {
        "schema_version": "1.0",
        "generator_model": MODEL,
        "model_generated_record_count": EXPECTED_RECORDS - len(MANUAL_REPLACEMENTS),
        "manually_repaired_record_count": len(MANUAL_REPLACEMENTS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed_file": str(SEED_PATH),
        "dataset_file": str(DATASET_PATH),
        "seed_count": len(seeds),
        "paraphrases_per_seed": EXPECTED_PARAPHRASES_PER_SEED,
        "record_count": len(records),
        "ground_truth_source": "repository-grounded seed annotations",
        "ground_truth_generated_by_model": False,
        "paraphrase_review_status": "assistant_reviewed_pending_author_review",
        "candidate_history": str(CANDIDATE_HISTORY_PATH),
        "raw_response_directory": str(RAW_DIRECTORY),
        "semantic_rejection_log": str(SEMANTIC_REJECTION_PATH),
        "manual_repair_log": str(MANUAL_REPAIR_PATH),
    }
    METADATA_PATH.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
