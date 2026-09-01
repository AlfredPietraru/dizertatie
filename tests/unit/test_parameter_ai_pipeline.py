from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ros_config_builder import (
    CapabilitySelections, ParameterReasoner, ParameterSelectionInterpretation, SemanticEnricher,
    build_parameter_evidence, build_system_model, build_template_configuration_schema,
    derive_parameter_catalogue, enrichment_by_parameter_id, evaluate_parameter_reasoning,
    evidence_by_parameter_id, extract_launch_files, extract_package_metadata,
    extract_parameter_yaml, extract_ros_node_ir, load_capability_registry,
    load_parameter_reasoning_tasks,
    realize_capabilities, resolve_ros_orchestration,
    retrieve_parameters,
)
from ros_config_builder.mission import (
    build_parameter_selection_prompt, build_parameter_selection_system_context,
    build_selection_context,
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
        realization = realize_capabilities(CapabilitySelections.defaults(), registry)
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

    def test_catalogue_excludes_parameters_from_inactive_components(self) -> None:
        identifiers = {item.parameter_id for item in self.catalogue.parameters}
        self.assertIn("nodes.kinematic_icp.max_range", identifiers)
        self.assertNotIn("nodes.kiss_icp.max_range", identifiers)
        self.assertTrue(all(
            relationship.get("target") in identifiers
            for parameter in self.catalogue.parameters
            for relationship in parameter.relationships
        ))

    def test_semantic_enrichment_requires_real_evidence_ids(self) -> None:
        wheel = next(item for item in self.evidence
                     if item.parameter_id == "nodes.rdrive_node.wheel_radius")
        payload = {"enrichments": [{
            "parameter_id": wheel.parameter_id,
            "description": "Radius of each drive wheel.",
            "aliases": ["drive wheel radius", "physical wheel radius"],
            "user_expressions": ["make the wheels larger"],
            "physical_quantity": "length", "unit": "metres",
            "semantic_category": "robot_geometry",
            "behavioral_effect": ["Changes wheel-speed to linear-speed conversion."],
            "constraints": ["Must be positive."],
            "relationships": [{
                "target_parameter_id": "nodes.joint_state_estimator.wheel_radius",
                "relation": "same_physical_property", "requires_joint_update": True,
                "reason": "Both components describe the same physical wheels.",
                "confidence": 0.95, "evidence_ids": [wheel.declarations[0].evidence_id],
            }],
            "related_parameters": [],
            "confidence": 0.95, "evidence_ids": [wheel.declarations[0].evidence_id],
        }]}
        enricher = SemanticEnricher(
            lambda *_: json.dumps(payload),
            prompt_template=Path("prompts/parameter_semantic_enrichment.txt").read_text(),
        )
        result = enricher.enrich([
            wheel,
        ], known_parameter_ids={
            wheel.parameter_id, "nodes.joint_state_estimator.wheel_radius",
        })
        enriched = enrichment_by_parameter_id(result)[wheel.parameter_id]
        self.assertEqual(enriched["unit"], "metres")
        self.assertEqual(enriched["aliases"][0], "drive wheel radius")
        self.assertEqual(
            enriched["related_parameters"],
            ["nodes.joint_state_estimator.wheel_radius"],
        )
        payload["enrichments"][0]["evidence_ids"] = ["evidence:invented"]
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            enricher.enrich([wheel], known_parameter_ids={
                wheel.parameter_id, "nodes.joint_state_estimator.wheel_radius",
            })

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

    def test_llm_semantic_variant_uses_only_complete_generated_metadata(self) -> None:
        wheel_id = "nodes.rdrive_node.wheel_radius"
        estimator_id = "nodes.joint_state_estimator.wheel_radius"
        enrichment = {
            wheel_id: {
                "description": "Controls the physical radius of each drive wheel.",
                "aliases": ["traction circumference setting"],
                "user_expressions": ["make the wheels effectively larger"],
                "physical_quantity": "length",
                "unit": "metres",
                "semantic_category": "robot_geometry",
                "behavioral_effect": ["Changes linear distance inferred from wheel rotation."],
                "constraints": ["Must be positive."],
                "relationships": [{
                    "target_parameter_id": estimator_id,
                    "relation": "same_physical_property",
                    "requires_joint_update": True,
                    "reason": "Both values represent the same physical wheels.",
                    "confidence": 0.97,
                    "evidence_ids": ["evidence:not-forwarded"],
                }],
                "related_parameters": [estimator_id],
                "confidence": 0.96,
            },
        }
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        catalogue = derive_parameter_catalogue(
            realize_capabilities(CapabilitySelections.defaults(), registry), self.schema,
            evidence_by_id=evidence_by_parameter_id(self.evidence),
            semantic_enrichments=enrichment,
        )
        retrieval = retrieve_parameters(
            "adjust the traction circumference setting", catalogue,
            variant="llm_semantic", top_k=1, graph_hops=1,
            wiring_bindings=self.manifest["wiring_bindings"],
        )
        self.assertEqual(retrieval.candidates[0].parameter_id, wheel_id)
        self.assertIn("llm_aliases", retrieval.candidates[0].matched_fields)
        self.assertEqual(retrieval.candidates[0].evidence_ids, [])
        self.assertEqual(retrieval.candidates[1].parameter_id, estimator_id)
        self.assertEqual(retrieval.candidates[1].evidence_ids, [])

        context = build_selection_context(retrieval, catalogue)
        record = next(
            item for item in context.records if item["parameter_id"] == wheel_id
        )
        self.assertNotIn("component_id", record)
        self.assertNotIn("affected_components", record)
        self.assertNotIn("semantic_name", record)
        self.assertNotIn("source_evidence", record)
        self.assertNotIn("ros_interfaces", record)
        self.assertNotIn("retrieval", record)
        metadata = record["semantic_metadata"]
        self.assertEqual(metadata["aliases"], ["traction circumference setting"])
        self.assertEqual(metadata["user_expressions"], ["make the wheels effectively larger"])
        self.assertEqual(metadata["relationships"][0]["relation"], "same_physical_property")
        self.assertEqual(metadata["relationships"][0]["reason"],
                         "Both values represent the same physical wheels.")
        self.assertEqual(metadata["relationships"][0]["confidence"], 0.97)
        self.assertNotIn("evidence_ids", metadata["relationships"][0])
        self.assertEqual(context.graph_edges[0]["confidence"], 0.97)
        self.assertEqual(
            context.included_parameter_ids,
            sorted(context.included_parameter_ids),
        )
        prompt = build_parameter_selection_prompt(
            Path("prompts/parameter_selection.txt").read_text(), context,
            system_context={"active_components": ["rdrive_node"]},
        )
        self.assertIn('Stable system context:\n{"active_components":["rdrive_node"]}', prompt)
        self.assertIn('"semantic_metadata":{"aliases":', prompt)
        self.assertNotIn("{{PARAMETER_SELECTION_JSON_SCHEMA}}", prompt)
        self.assertNotIn('"$defs"', prompt)

    def test_llm_semantic_variant_requires_enrichment(self) -> None:
        with self.assertRaisesRegex(ValueError, "semantic-enrichment artifact"):
            retrieve_parameters(
                "change a parameter", self.catalogue,
                variant="llm_semantic", top_k=5,
            )

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

    def test_selection_validation_retries_once_and_returns_only_final_result(self) -> None:
        responses = [
            json.dumps({
                "status": "valid",
                "selected_parameters": [{
                    "parameter_id": "not.in.context", "relevance": "Invalid candidate.",
                }],
            }),
            json.dumps({"status": "no_change", "reason": "No parameter change requested."}),
        ]
        user_prompts = []

        def selection_backend(_system_prompt: str, user_prompt: str) -> str:
            user_prompts.append(user_prompt)
            return responses.pop(0)

        reasoner = ParameterReasoner(
            selection_backend, lambda *_: self.fail("value stage must not run"),
            selection_prompt_template=Path("prompts/parameter_selection.txt").read_text(),
            value_prompt_template=Path("prompts/parameter_value_reasoning.txt").read_text(),
            context_variant="graph", top_k=8,
        )
        result = reasoner.reason("Use the current settings", self.catalogue)
        self.assertEqual(result.status, "no_change")
        self.assertEqual(len(user_prompts), 2)
        self.assertIn("validation_error", user_prompts[1])
        self.assertIn("not.in.context", user_prompts[1])

    def test_valid_selection_tolerates_terminal_explanation_fields(self) -> None:
        selection = ParameterSelectionInterpretation.model_validate({
            "status": "valid",
            "selected_parameters": [{
                "parameter_id": "nodes.rdrive_node.wheel_radius",
                "relevance": "The requested wheel geometry parameter.",
            }],
            "reason": "The request identifies a configurable parameter.",
            "clarification_question": "This field is ignored for a valid selection.",
        })
        self.assertEqual(selection.status, "valid")
        self.assertEqual(len(selection.selected_parameters), 1)

    def test_value_validation_retries_once_and_returns_only_final_result(self) -> None:
        selection = json.dumps({
            "status": "valid",
            "selected_parameters": [{
                "parameter_id": "nodes.rdrive_node.wheel_radius",
                "relevance": "The drive wheel radius changes.",
            }],
        })
        responses = [
            json.dumps({
                "status": "valid",
                "changes": [{
                    "parameter_id": "nodes.joint_state_estimator.wheel_radius",
                    "old_value": 0.03, "new_value": 0.04,
                    "reason": "Invalid unselected change.",
                }],
            }),
            json.dumps({
                "status": "valid",
                "changes": [{
                    "parameter_id": "nodes.rdrive_node.wheel_radius",
                    "old_value": 0.03, "new_value": 0.04,
                    "reason": "Apply the requested radius.",
                }],
            }),
        ]
        user_prompts = []

        def value_backend(_system_prompt: str, user_prompt: str) -> str:
            user_prompts.append(user_prompt)
            return responses.pop(0)

        reasoner = ParameterReasoner(
            lambda *_: selection, value_backend,
            selection_prompt_template=Path("prompts/parameter_selection.txt").read_text(),
            value_prompt_template=Path("prompts/parameter_value_reasoning.txt").read_text(),
            context_variant="graph", top_k=8,
        )
        result = reasoner.reason("Set the wheel radius to 4 cm", self.catalogue)
        self.assertEqual(result.validated_values, {
            "nodes.rdrive_node.wheel_radius": 0.04,
        })
        self.assertEqual(len(user_prompts), 2)
        self.assertIn("introduced unselected parameters", user_prompts[1])

    def test_parameter_evaluation_scores_retrieval_selection_and_values(self) -> None:
        case = {
            "id": "wheel-001", "mission": "I installed wheels with an 8 cm diameter",
            "outcome": "supported", "strata": ["physical", "unit-conversion"],
            "expected": {"parameter_reasoning": {"status": "valid", "changes": [
                {"parameter_id": "nodes.rdrive_node.wheel_radius", "value": 0.04},
                {"parameter_id": "nodes.joint_state_estimator.wheel_radius", "value": 0.04},
            ]}},
        }
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "cases.jsonl"
            dataset.write_text(json.dumps(case) + "\n")
            report = evaluate_parameter_reasoning(
                dataset, self._wheel_reasoner(), self.catalogue,
                wiring_bindings=self.manifest["wiring_bindings"],
                progress=progress.append,
            )
        self.assertEqual(report["metrics"]["selection_exact_set_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["value_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["end_to_end_accuracy"], 1.0)
        self.assertEqual(
            [event["event"] for event in progress],
            ["case_started", "case_finished"],
        )

    def test_parameter_task_dataset_has_valid_gold_ids_and_types(self) -> None:
        tasks = load_parameter_reasoning_tasks(
            "data/parameter_reasoning_tasks_v1.jsonl",
        )
        self.assertEqual(len(tasks), 40)
        self.assertEqual(sum(task.outcome == "supported" for task in tasks), 32)
        self.assertTrue(all(task.review_status == "pending_human_review" for task in tasks))

    def test_parameter_selection_system_context_omits_orchestration_detail(self) -> None:
        context = build_parameter_selection_system_context(
            {
                "status": "valid",
                "active_components": [{
                    "component_id": "kiss_icp", "interfaces": [{"large": "detail"}],
                }],
                "inactive_components": ["kinematic_icp"],
                "wiring_bindings": [{"large": "detail"}],
            },
        )
        self.assertEqual(context, {
            "active_components": ["kiss_icp"],
            "inactive_components": ["kinematic_icp"],
        })


if __name__ == "__main__":
    unittest.main()
