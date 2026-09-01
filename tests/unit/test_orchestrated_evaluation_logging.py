import json
import tempfile
import unittest
from pathlib import Path

from helper_scripts.evaluate_orchestrated_dataset import _expected, _summarize, _write_failure_artifacts


class OrchestratedEvaluationLoggingTests(unittest.TestCase):
    def test_summary_preserves_correct_selection_when_value_stage_fails(self) -> None:
        record = {
            "expected": {"status": "valid", "capabilities": {}, "parameters": {"nodes.a.x": 1}},
            "error": "ValueError: invalid value response", "failure_stage": "parameter_reasoning",
            "outcome_correct": False, "capability_status_correct": True,
            "capability_exact_match": True, "parameter_selection_exact_match": True,
            "parameter_values_exact_match": False, "llm_end_to_end_exact_match": False,
            "capability_counts": {"tp": 0, "fp": 0, "fn": 0},
            "parameter_selection_counts": {"tp": 1, "fp": 0, "fn": 0},
            "parameter_value_counts": {"tp": 0, "fp": 0, "fn": 1},
            "selection": {"true_positive": 1, "predicted": 1, "expected": 1, "exact": True},
            "retrieval": {"top_1_recall": 1.0, "top_3_recall": 1.0, "top_5_recall": 1.0,
                          "reciprocal_ranks": {"nodes.a.x": 1.0}},
            "predicted_parameters": {}, "parameter_outcome_correct": False,
            "predicted_status": "error", "latency_seconds": 1.0, "source_seed": None,
        }
        metrics = _summarize([record])
        self.assertEqual(metrics["parameter_reasoning"]["retrieval_top_1_recall"], 1.0)
        self.assertEqual(metrics["parameter_reasoning"]["selection_exact_set_accuracy"], 1.0)
        self.assertEqual(metrics["parameter_reasoning"]["value_accuracy"], 0.0)
        self.assertEqual(metrics["llm_end_to_end_exact_match"]["rate"], 0.0)

    def test_capability_comparison_ignores_selection_basis(self) -> None:
        expected = _expected({
            "expected_capability_interpretation": {
                "status": "valid",
                "capabilities": {
                    "mapping": {
                        "enabled": True,
                        "implementation": "cartographer",
                        "selection_basis": "explicit",
                    },
                },
            },
        })
        self.assertEqual(expected["capabilities"], {
            "mapping": {"enabled": True, "implementation": "cartographer"},
        })

    def test_failure_artifacts_include_every_llm_stage(self) -> None:
        record = {
            "id": "case-001", "mission": "Set a value", "error": None,
            "expected": {"status": "valid", "capabilities": {}, "parameters": {"nodes.a.x": 1}},
            "predicted_status": "valid", "outcome_correct": True,
            "predicted_capabilities": {}, "capability_exact_match": True,
            "selected_parameter_ids": [], "parameter_selection_exact_match": False,
            "predicted_parameters": {}, "parameter_values_exact_match": False,
            "llm_calls": [
                {"stage": "capability_interpretation", "system_prompt": "capability",
                 "user_prompt": "Set a value", "raw_response": "{}", "error": None},
                {"stage": "parameter_selection", "system_prompt": "selection",
                 "user_prompt": "Set a value", "raw_response": "{}", "error": None},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            _write_failure_artifacts(output, record)
            failure = output / "failures" / "case-001"
            self.assertTrue((failure / "expected_result.json").is_file())
            actual = json.loads((failure / "actual_result.json").read_text())
            self.assertEqual(actual["status"], "valid")
            self.assertEqual(actual["selected_parameter_ids"], [])
            self.assertTrue((failure / "error.txt").is_file())
            self.assertTrue((failure / "01_capability_interpretation_system_prompt.txt").is_file())
            self.assertTrue((failure / "02_parameter_selection_model_response.txt").is_file())
            calls = json.loads((failure / "llm_calls.json").read_text())
            self.assertEqual([item["stage"] for item in calls], [
                "capability_interpretation", "parameter_selection",
            ])


if __name__ == "__main__":
    unittest.main()
