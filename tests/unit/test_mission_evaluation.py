from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ros_config_builder.mission import (
    MissionInterpreter, evaluate_missions, load_capability_registry, write_evaluation_report,
)


class MissionEvaluationTests(unittest.TestCase):
    registry = load_capability_registry("configuration_templates/capability_registry.yaml")

    def test_records_structured_success_and_failure_stages(self) -> None:
        responses = iter([
            '{"status":"valid","capabilities":{"navigation":{"enabled":false}}}',
            "not JSON",
        ])
        interpreter = MissionInterpreter(
            lambda *_: next(responses), system_prompt="test", registry=self.registry,
        )
        cases = [
            {"id": "one", "mission": "No navigation", "expected": {
                "capabilities": {"navigation": {"enabled": False}}},
             "expected_template_configuration": {"launch.launch_nav2": False}},
            {"id": "two", "mission": "Mapping", "expected": {
                "capabilities": {"mapping": {"enabled": True}}},
             "expected_template_configuration": {"launch.launch_cartographer": True}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "cases.jsonl"
            dataset.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
            report = evaluate_missions(dataset, interpreter, registry=self.registry)
            paths = write_evaluation_report(report, Path(temporary) / "report")
            self.assertTrue(report["predictions"][0]["end_to_end_semantic_correct"])
            self.assertEqual(report["predictions"][0]["explicit_choice_recall"]["correct"], 1)
            self.assertEqual(report["predictions"][1]["failure_stage"], "invalid_json")
            self.assertEqual(report["metrics"]["explicit_choice_recall"]["rate"], 0.5)
            self.assertTrue(all(path.exists() for path in paths.values()))

    def test_outcome_aware_terminal_results_are_classified_without_mapping(self) -> None:
        responses = iter([
            '{"status":"unsupported","reason":"not available"}',
            '{"status":"needs_clarification","reason":"missing choice",'
            '"clarification_question":"Which option?"}',
        ])
        interpreter = MissionInterpreter(
            lambda *_: next(responses), system_prompt="test", registry=self.registry,
        )
        cases = [
            {"id": "u", "mission": "unsupported", "outcome": "unsupported", "strata": ["unsupported"]},
            {"id": "a", "mission": "ambiguous", "outcome": "ambiguous", "strata": ["ambiguous"]},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "cases.jsonl"
            dataset.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
            report = evaluate_missions(dataset, interpreter, registry=self.registry)
        self.assertTrue(all(item["outcome_correct"] for item in report["predictions"]))
        self.assertEqual(report["metrics"]["outcome_classification_accuracy"]["rate"], 1.0)
        self.assertEqual(report["metrics"]["ambiguous_execution_rate"]["rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
