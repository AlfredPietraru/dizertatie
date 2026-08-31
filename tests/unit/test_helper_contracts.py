from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helper_scripts.enrich_parameters import _load_frozen_evidence
from helper_scripts.evaluate_missions import DEFAULT_EVALUATION_OUTPUT, _parser as evaluation_parser
from helper_scripts.evaluate_orchestrated_dataset import (
    DEFAULT_DATASETS, DEFAULT_OUTPUT as ORCHESTRATED_OUTPUT,
    _append_checkpoint, _load_predictions_checkpoint, _parser as orchestrated_parser,
)
from helper_scripts.synthetic_dataset import DATASET_GENERATION_DIRECTORY, _parser as dataset_parser
from ros_config_builder.mission import (
    build_parameter_evidence_artifact,
    write_parameter_evidence,
)


class HelperContractTests(unittest.TestCase):
    def test_active_helper_defaults_do_not_write_into_historical_results(self) -> None:
        evaluation = evaluation_parser().parse_args([])
        orchestrated = orchestrated_parser().parse_args([])
        generation = dataset_parser().parse_args(["generate"])
        freezing = dataset_parser().parse_args(["freeze"])
        queue = dataset_parser().parse_args(["queue"])

        self.assertEqual(evaluation.output, DEFAULT_EVALUATION_OUTPUT)
        self.assertEqual(orchestrated.output, ORCHESTRATED_OUTPUT)
        self.assertIsNone(orchestrated.dataset)
        self.assertEqual(len(DEFAULT_DATASETS), 2)
        self.assertEqual(generation.raw_directory, DATASET_GENERATION_DIRECTORY / "raw")
        self.assertEqual(freezing.report_directory, DATASET_GENERATION_DIRECTORY / "quality")
        self.assertEqual(queue.output, DATASET_GENERATION_DIRECTORY / "review_queue.md")
        for path in (
            evaluation.output,
            orchestrated.output,
            generation.raw_directory,
            freezing.report_directory,
            queue.output,
        ):
            self.assertNotIn("semester2", path.parts)

    def test_enrichment_requires_hash_matching_frozen_evidence(self) -> None:
        source_model = {"schema_version": "1.0", "deployment_instances": []}
        configuration_model = {"schema_version": "3.0", "configuration_keys": []}
        artifact = build_parameter_evidence_artifact(
            [], source_model=source_model, configuration_model=configuration_model,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_parameter_evidence(artifact, Path(directory) / "evidence.json")
            self.assertEqual(
                _load_frozen_evidence(
                    path,
                    system_model=source_model,
                    configuration_model=configuration_model,
                ),
                [],
            )
            with self.assertRaisesRegex(ValueError, "hashes do not match"):
                _load_frozen_evidence(
                    path,
                    system_model={**source_model, "stage": "changed"},
                    configuration_model=configuration_model,
                )

    def test_version_registry_covers_current_generated_contracts(self) -> None:
        versions = json.loads(
            Path("configuration_templates/VERSIONS.json").read_text(encoding="utf-8")
        )
        self.assertEqual(versions["artifact_generator"], "1.0")
        self.assertEqual(versions["parameter_evidence"], "1.0")
        self.assertEqual(versions["semantic_enrichment"], "2.0")
        self.assertEqual(versions["parameter_reasoning_dataset"], "1.0")

    def test_evaluation_checkpoint_survives_a_truncated_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "predictions.jsonl"
            _append_checkpoint(path, {"id": "case-1", "mission": "first"})
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"id":"case-2"')
            self.assertEqual(
                _load_predictions_checkpoint(path),
                [{"id": "case-1", "mission": "first"}],
            )
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))


if __name__ == "__main__":
    unittest.main()
