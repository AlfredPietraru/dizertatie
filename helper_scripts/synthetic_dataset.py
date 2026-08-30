#!/usr/bin/env python3
"""Generate, review, and freeze controlled synthetic mission data."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ros_config_builder.mission import (
    OllamaParaphraseBackend, automatically_validate_candidates, dataset_quality_report, freeze_accepted_dataset,
    generate_paraphrases, load_candidates, load_synthetic_seeds, review_candidate,
    load_capability_registry, write_candidate_history, write_quality_report, write_review_queue,
)


DATASET_GENERATION_DIRECTORY = Path("artifacts/dataset_generation/synthetic_missions_v1")


def _load_env(path: Path) -> None:
    if not path.exists(): return
    for line in path.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1); os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--seeds", type=Path, default=Path("data/synthetic_seed_intents_v1.jsonl"))
    generate.add_argument("--output", type=Path, default=Path("data/synthetic_candidates_v1.jsonl"))
    generate.add_argument(
        "--raw-directory", type=Path,
        default=DATASET_GENERATION_DIRECTORY / "raw",
    )
    generate.add_argument("--model", default="qwen2.5-coder:7b")
    review = commands.add_parser("review")
    review.add_argument("candidate_id"); review.add_argument("decision", choices=("accept", "reject"))
    review.add_argument("--candidates", type=Path, default=Path("data/synthetic_candidates_v1.jsonl"))
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", choices=("semantic_drift", "added_requirement", "removed_requirement",
        "unnatural_wording", "unnatural_language", "wrong_outcome", "duplicate",
        "annotation_policy_violation", "malformed_generation", "other"))
    review.add_argument("--notes")
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--seeds", type=Path, default=Path("data/synthetic_seed_intents_v1.jsonl"))
    freeze.add_argument("--candidates", type=Path, default=Path("data/synthetic_candidates_v1.jsonl"))
    freeze.add_argument("--output", type=Path, default=Path("data/synthetic_dataset_v1.jsonl"))
    freeze.add_argument("--report-directory", type=Path,
                        default=DATASET_GENERATION_DIRECTORY / "quality")
    queue = commands.add_parser("queue")
    queue.add_argument("--seeds", type=Path, default=Path("data/synthetic_seed_intents_v1.jsonl"))
    queue.add_argument("--candidates", type=Path, default=Path("data/synthetic_candidates_v1.jsonl"))
    queue.add_argument("--output", type=Path,
                       default=DATASET_GENERATION_DIRECTORY / "review_queue.md")
    return parser


def main() -> int:
    args = _parser().parse_args(); _load_env(Path(".env"))
    registry = load_capability_registry("configuration_templates/capability_registry.yaml")
    if args.command == "generate":
        host = os.getenv("OLLAMA_HOST_PATH", "127.0.0.1")
        backend = OllamaParaphraseBackend(host=host, model=args.model)
        seeds = load_synthetic_seeds(args.seeds, registry); candidates = []; args.raw_directory.mkdir(parents=True, exist_ok=True)
        for seed in seeds:
            generated, raw = generate_paraphrases(seed, backend); candidates.extend(generated)
            (args.raw_directory / f"{seed.id}.json").write_text(raw + "\n", encoding="utf-8")
        automatically_validate_candidates(candidates); write_candidate_history(candidates, args.output)
        counts = {status: sum(item.review.status == status for item in candidates)
                  for status in ("pending", "accepted", "rejected")}
        print(json.dumps({"generated": len(candidates), "review": counts, "output": str(args.output)}, indent=2))
        return 0
    if args.command == "review":
        candidates = load_candidates(args.candidates)
        selected = next((item for item in candidates if item.candidate_id == args.candidate_id), None)
        if selected is None: raise SystemExit(f"unknown candidate: {args.candidate_id}")
        if args.decision == "reject" and not args.reason: raise SystemExit("--reason is required when rejecting")
        review_candidate(selected, accepted=args.decision == "accept", reviewer=args.reviewer,
                         rejection_reason=args.reason, notes=args.notes)
        write_candidate_history(candidates, args.candidates); return 0
    if args.command == "queue":
        candidates = load_candidates(args.candidates); seeds = load_synthetic_seeds(args.seeds, registry)
        print(json.dumps({"pending": write_review_queue(candidates, seeds, args.output),
                          "output": str(args.output)}, indent=2)); return 0
    candidates = load_candidates(args.candidates); seeds = load_synthetic_seeds(args.seeds, registry)
    pending = sum(item.review.status == "pending" for item in candidates)
    if pending: raise SystemExit(f"cannot freeze dataset: {pending} candidates remain pending")
    accepted = freeze_accepted_dataset(candidates, seeds, args.output)
    reports = write_quality_report(dataset_quality_report(candidates, seeds), args.report_directory)
    print(json.dumps({"accepted": accepted, "output": str(args.output),
                      "reports": {key: str(value) for key, value in reports.items()}}, indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
