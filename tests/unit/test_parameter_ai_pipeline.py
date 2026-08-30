from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ros_config_builder import (
    CapabilitySelections, ParameterReasoner, SemanticEnricher,
    build_parameter_evidence, build_system_model, build_template_configuration_schema,
    derive_parameter_catalogue, enrichment_by_parameter_id, evaluate_parameter_reasoning,
    evidence_by_parameter_id, extract_launch_files, extract_package_metadata,
    extract_parameter_yaml, extract_ros_node_ir, load_capability_registry,
    load_parameter_reasoning_tasks,
    realize_capabilities, resolve_ros_orchestration,
    retrieve_parameters,
)


class ParameterAIPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        roots = ["src/antrobot_ros", "src/antrobot_description"]
        configuration = extract_parameter_yaml(".", source_roots=roots)
        cls.model = build_system_model(
            workspace=".", step1=extract_ros_node_ir(".", source_roots=roots),
            launch=extract_launch_files(".", source_roots=roots), configuration=configuration,
            package_metadata=extract_package_metadata(".", source_roots=["src"]),
            root_launch_files=["src/antrobot_ros/launch/antrobot.launch.py"],
        )
        cls.schema = build_template_configuration_schema(cls.model)
        cls.evidence = build_parameter_evidence(".", cls.schema, cls.model)
        cls.manifest = json.loads(Path("configuration_templates/manifest.json").read_text())
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        realization = realize_capabilities(CapabilitySelections(), registry)
        orchestration = resolve_ros_orchestration(
            realization, registry, cls.model, cls.manifest,
        )
        cls.catalogue = derive_parameter_catalogue(
            realization, cls.schema,
            evidence_by_id=evidence_by_parameter_id(cls.evidence),
        )

    def _wheel_reasoner(self, *, introduce_unselected: bool = False) -> ParameterReasoner:
        selection = json.dumps({
            "status": "valid",
            "interpretation": "The request changes the physical wheel radius.",
            "selected_parameters": [
                {"parameter_id": "nodes.rdrive_node.wheel_radius",
                 "relevance": "Used by the drive system."},
                *([] if introduce_unselected else [{
                    "parameter_id": "nodes.joint_state_estimator.wheel_radius",
                    "relevance": "The estimator uses the same physical radius.",
                }]),
            ],
        })
        value = json.dumps({
            "status": "valid", "interpretation": "Eight centimetres diameter is four centimetres radius.",
            "changes": [
                {"parameter_id": "nodes.rdrive_node.wheel_radius", "old_value": 0.03,
                 "new_value": 0.04, "reason": "Convert diameter to radius."},
                {"parameter_id": "nodes.joint_state_estimator.wheel_radius", "old_value": 0.03,
                 "new_value": 0.04, "reason": "Keep the shared geometry consistent."},
            ],
            "dependencies_considered": ["nodes.joint_state_estimator.wheel_radius"],
            "uncertainties": [],
        })
        return ParameterReasoner(
            lambda *_: selection, lambda *_: value,
            selection_prompt_template=Path("prompts/parameter_selection.txt").read_text(),
            value_prompt_template=Path("prompts/parameter_value_reasoning.txt").read_text(),
            context_variant="graph", top_k=8, graph_hops=1,
        )

    def test_evidence_packages_include_declaration_usage_and_shared_components(self) -> None:
        self.assertEqual(len(self.evidence), 109)
        wheel = next(item for item in self.evidence
                     if item.parameter_id == "nodes.rdrive_node.wheel_radius")
        self.assertIn("Radius of the wheels", wheel.declarations[0].text)
        self.assertTrue(any("max_wheel_vel_left" in item.text for item in wheel.usages))
        shared = next(item for item in self.evidence
                      if item.parameter_id == "nodes.explore.publish_rate")
        self.assertEqual(shared.affected_components, ["explore", "explore_lite_map_converter"])

    def test_semantic_enrichment_requires_real_evidence_ids(self) -> None:
        wheel = next(item for item in self.evidence
                     if item.parameter_id == "nodes.rdrive_node.wheel_radius")
        payload = {"enrichments": [{
            "parameter_id": wheel.parameter_id,
            "description": "Radius of each drive wheel.",
            "physical_quantity": "length", "unit": "metres",
            "semantic_category": "robot_geometry",
            "behavioral_effect": ["Changes wheel-speed to linear-speed conversion."],
            "constraints": ["Must be positive."], "related_parameters": [],
            "confidence": 0.95, "evidence_ids": [wheel.declarations[0].evidence_id],
        }]}
        enricher = SemanticEnricher(
            lambda *_: json.dumps(payload),
            prompt_template=Path("prompts/parameter_semantic_enrichment.txt").read_text(),
        )
        result = enricher.enrich([wheel], known_parameter_ids={wheel.parameter_id})
        self.assertEqual(enrichment_by_parameter_id(result)[wheel.parameter_id]["unit"], "metres")
        payload["enrichments"][0]["evidence_ids"] = ["evidence:invented"]
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            enricher.enrich([wheel], known_parameter_ids={wheel.parameter_id})

    def test_retrieval_and_graph_expansion_find_both_wheel_radius_parameters(self) -> None:
        result = retrieve_parameters(
            "I installed wheels with an 8 cm diameter", self.catalogue,
            variant="graph", top_k=5, graph_hops=1,
            wiring_bindings=self.manifest["wiring_bindings"],
        )
        identifiers = {item.parameter_id for item in result.candidates}
        self.assertIn("nodes.rdrive_node.wheel_radius", identifiers)
        self.assertIn("nodes.joint_state_estimator.wheel_radius", identifiers)
        self.assertLess(len(identifiers), len(self.catalogue.parameters))

    def test_selection_and_value_reasoning_are_separate_and_validated(self) -> None:
        result = self._wheel_reasoner().reason(
            "I installed wheels with an 8 cm diameter", self.catalogue,
            wiring_bindings=self.manifest["wiring_bindings"],
        )
        self.assertEqual(result.validated_values, {
            "nodes.rdrive_node.wheel_radius": 0.04,
            "nodes.joint_state_estimator.wheel_radius": 0.04,
        })
        self.assertTrue(result.selection.selected_parameters)
        self.assertTrue(result.value_interpretation.changes)

    def test_value_stage_cannot_introduce_an_unselected_parameter(self) -> None:
        with self.assertRaisesRegex(ValueError, "introduced unselected"):
            self._wheel_reasoner(introduce_unselected=True).reason(
                "I installed wheels with an 8 cm diameter", self.catalogue,
                wiring_bindings=self.manifest["wiring_bindings"],
            )

    def test_parameter_evaluation_scores_retrieval_selection_and_values(self) -> None:
        case = {
            "id": "wheel-001", "mission": "I installed wheels with an 8 cm diameter",
            "outcome": "supported", "strata": ["physical", "unit-conversion"],
            "expected": {"parameter_reasoning": {"status": "valid", "changes": [
                {"parameter_id": "nodes.rdrive_node.wheel_radius", "value": 0.04},
                {"parameter_id": "nodes.joint_state_estimator.wheel_radius", "value": 0.04},
            ]}},
        }
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "cases.jsonl"
            dataset.write_text(json.dumps(case) + "\n")
            report = evaluate_parameter_reasoning(
                dataset, self._wheel_reasoner(), self.catalogue,
                wiring_bindings=self.manifest["wiring_bindings"],
            )
        self.assertEqual(report["metrics"]["selection_exact_set_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["value_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["end_to_end_accuracy"], 1.0)

    def test_parameter_task_dataset_has_valid_gold_ids_and_types(self) -> None:
        tasks = load_parameter_reasoning_tasks(
            "data/parameter_reasoning_tasks_v1.jsonl", self.catalogue,
        )
        self.assertEqual(len(tasks), 40)
        self.assertEqual(sum(task.outcome == "supported" for task in tasks), 32)
        self.assertTrue(all(task.review_status == "pending_human_review" for task in tasks))


if __name__ == "__main__":
    unittest.main()
