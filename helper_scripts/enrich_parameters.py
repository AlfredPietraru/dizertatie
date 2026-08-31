#!/usr/bin/env python3
"""Generate a versioned LLM semantic-enrichment artifact from frozen evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ros_config_builder.integration import SystemModel
from ros_config_builder.mission import (
    OllamaBackend, SemanticEnricher, SemanticEnrichment, SemanticEnrichmentBatch,
    build_semantic_enrichment_artifact, load_parameter_evidence_artifact,
    parameter_evidence_artifact_matches, write_semantic_enrichment,
)
from ros_config_builder.templating import TemplateConfigurationSchema


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--system-model", type=Path,
                        default=Path("artifacts/ros_system_model/ros_system_model.json"))
    parser.add_argument("--configuration-model", type=Path, default=Path(
        "artifacts/template_configuration/template_configuration_schema.json"))
    parser.add_argument("--prompt", type=Path,
                        default=Path("prompts/parameter_semantic_enrichment.txt"))
    parser.add_argument("--evidence", type=Path,
                        default=Path("artifacts/semantic_parameters/evidence.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/semantic_parameters/enrichment_v2/enrichment.json"))
    parser.add_argument("--working-directory", type=Path, default=None)
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--host", default=None)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--ignore-evidence-ids", action="store_true")
    parser.add_argument("--prompt-version", default="semantic_enrichment_v2")
    return parser


def _append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load_jsonl_checkpoint(path: Path, model: type[SemanticEnrichment]) -> list:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(model.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError):
            if index != len(lines) - 1:
                raise ValueError(f"corrupt enrichment checkpoint record {index + 1}: {path}")
            path.write_text(
                "".join(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n"
                        for item in records),
                encoding="utf-8",
            )
    return records


def _fingerprint(*, evidence_path: Path, prompt_path: Path, model: str,
                 prompt_version: str, batch_size: int) -> str:
    value = {
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        "model": model, "prompt_version": prompt_version, "batch_size": batch_size,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _load_frozen_evidence(
    path: Path,
    *,
    system_model: dict,
    configuration_model: dict,
):
    artifact = load_parameter_evidence_artifact(path)
    if not parameter_evidence_artifact_matches(
        artifact,
        source_model=system_model,
        configuration_model=configuration_model,
    ):
        raise ValueError(
            f"parameter evidence hashes do not match the frozen models: {path}"
        )
    return artifact.parameters


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    workspace = args.workspace.resolve()
    system_path = args.system_model if args.system_model.is_absolute() else workspace / args.system_model
    configuration_path = (args.configuration_model if args.configuration_model.is_absolute()
                          else workspace / args.configuration_model)
    prompt_path = args.prompt if args.prompt.is_absolute() else workspace / args.prompt
    system_model = SystemModel.model_validate_json(
        system_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    configuration_model = TemplateConfigurationSchema.model_validate_json(
        configuration_path.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    evidence_path = args.evidence if args.evidence.is_absolute() else workspace / args.evidence
    try:
        evidence = _load_frozen_evidence(
            evidence_path,
            system_model=system_model,
            configuration_model=configuration_model,
        )
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    backend = OllamaBackend(
        host=args.host, model=args.model, response_model=SemanticEnrichmentBatch,
    )
    enricher = SemanticEnricher(
        backend,
        prompt_template=prompt_path.read_text(encoding="utf-8"),
        validate_evidence_ids=not args.ignore_evidence_ids,
    )
    known_ids = {item.parameter_id for item in evidence}
    output = args.output if args.output.is_absolute() else workspace / args.output
    working = args.working_directory or output.parent
    working = working if working.is_absolute() else workspace / working
    working.mkdir(parents=True, exist_ok=True)
    records_path = working / "enrichments.jsonl"
    raw_path = working / "raw_responses.jsonl"
    checkpoint_path = working / "checkpoint.json"
    fingerprint = _fingerprint(
        evidence_path=evidence_path, prompt_path=prompt_path, model=backend.model,
        prompt_version=args.prompt_version, batch_size=args.batch_size,
    )
    checkpoint = {
        "schema_version": "2.0", "status": "in_progress", "fingerprint": fingerprint,
        "model": backend.model, "prompt_version": args.prompt_version,
        "evidence_id_policy": "ignored" if args.ignore_evidence_ids else "validated",
        "batch_size": args.batch_size, "total_parameters": len(evidence),
    }
    if checkpoint_path.is_file():
        previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if previous.get("fingerprint") != fingerprint:
            raise ValueError(
                f"cannot resume enrichment because evidence, prompt, model, or batch size changed: "
                f"{checkpoint_path}"
            )
    records = _load_jsonl_checkpoint(records_path, SemanticEnrichment)
    if args.ignore_evidence_ids:
        for item in records:
            item.evidence_ids = []
            for relationship in item.relationships:
                relationship.evidence_ids = []
    completed = {item.parameter_id for item in records}
    if len(completed) != len(records):
        raise ValueError(f"enrichment checkpoint contains duplicate parameter IDs: {records_path}")
    unknown_completed = completed - known_ids
    if unknown_completed:
        raise ValueError(f"checkpoint contains unknown parameter IDs: {sorted(unknown_completed)}")
    checkpoint["completed_parameters"] = len(records)
    checkpoint_path.write_text(
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if records:
        print(f"resuming semantic enrichment with {len(records)}/{len(evidence)} saved", flush=True)
    pending = [item for item in evidence if item.parameter_id not in completed]
    for start in range(0, len(pending), args.batch_size):
        evidence_batch = pending[start:start + args.batch_size]
        batch_id = len(records) // args.batch_size + 1
        enricher.last_raw_response = None
        try:
            batch, raw = enricher.enrich_with_raw(
                evidence_batch, known_parameter_ids=known_ids,
            )
        except Exception as error:
            _append_jsonl(raw_path, {
                "batch": batch_id,
                "parameter_ids": [item.parameter_id for item in evidence_batch],
                "raw_response": enricher.last_raw_response,
                "raw_responses": enricher.last_raw_responses,
                "attempts": len(enricher.last_raw_responses),
                "error": f"{type(error).__name__}: {error}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            raise
        _append_jsonl(raw_path, {
            "batch": batch_id,
            "parameter_ids": [item.parameter_id for item in evidence_batch],
            "raw_response": raw,
            "raw_responses": enricher.last_raw_responses,
            "attempts": len(enricher.last_raw_responses),
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        for item in batch.enrichments:
            _append_jsonl(records_path, item.model_dump(mode="json"))
            records.append(item)
        checkpoint["completed_parameters"] = len(records)
        checkpoint_path.write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"enriched {len(records)}/{len(evidence)} parameters", flush=True)
    by_id = {item.parameter_id: item for item in records}
    records = [by_id[item.parameter_id] for item in evidence]
    artifact = build_semantic_enrichment_artifact(
        SemanticEnrichmentBatch(enrichments=records), source_model=system_model,
        configuration_model=configuration_model, model_identifier=backend.model,
        prompt_version=args.prompt_version,
    )
    write_semantic_enrichment(artifact, output)
    checkpoint.update(
        status="complete", completed_parameters=len(records), output=str(output),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    checkpoint_path.write_text(
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "parameters": len(records), "model": backend.model,
        "evidence": str(evidence_path), "enrichment": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
