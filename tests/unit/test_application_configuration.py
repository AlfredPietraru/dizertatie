from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import json
from pydantic import ValidationError

from ros_config_builder.orchestrate import (
    ApplicationConfiguration,
    DEFAULT_CONFIGURATION_PATH,
    load_application_configuration,
)


class ApplicationConfigurationTests(unittest.TestCase):
    def test_colocated_yaml_contains_every_application_setting(self) -> None:
        raw = json.loads(DEFAULT_CONFIGURATION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(ApplicationConfiguration.model_fields), set(raw))
        configuration = load_application_configuration()
        self.assertEqual(configuration.operation, "mission")
        self.assertEqual(configuration.workspace, Path.cwd().resolve())
        self.assertEqual(configuration.context_variant, "llm_semantic")
        self.assertEqual(configuration.retrieval_top_k, 10)
        self.assertEqual(
            configuration.semantic_enrichment,
            Path("artifacts/semantic_parameters/enrichment_v2/enrichment.json"),
        )

    def test_unknown_and_invalid_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parameters.yaml"
            path.write_text(
                json.dumps({
                    "operation": "mission",
                    "mission": "test",
                    "retrieval_top_k": 0,
                    "unknown": True,
                }),
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                load_application_configuration(path)

    def test_operation_specific_required_values_are_checked(self) -> None:
        with self.assertRaisesRegex(ValidationError, "mission must not be empty"):
            ApplicationConfiguration(operation="mission", mission="  ")
        with self.assertRaisesRegex(ValidationError, "root_launch_files must not be empty"):
            ApplicationConfiguration(operation="analyze", root_launch_files=[])


if __name__ == "__main__":
    unittest.main()
