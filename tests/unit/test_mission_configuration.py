from __future__ import annotations

import json
import copy
import unittest
import urllib.error
from io import BytesIO
from unittest import mock
from pathlib import Path

from pydantic import ValidationError

from ros_config_builder.mission import (
    CapabilitySelections, MissionInterpretation, ParameterChange,
    MissionInterpreter, OllamaBackend, build_interpretation_prompt,
    build_template_configuration_plan,
    derive_parameter_catalogue, load_capability_registry,
    realize_capabilities, resolve_ros_orchestration, validate_parameter_changes,
)


class MissionInterpretationTests(unittest.TestCase):
    registry = load_capability_registry("configuration_templates/capability_registry.yaml")
    system_model = json.loads(Path("artifacts/ros_system_model/ros_system_model.json").read_text())
    manifest = json.loads(Path("configuration_templates/manifest.json").read_text())

    def plan_for(self, capabilities: CapabilitySelections):
        return build_template_configuration_plan(realize_capabilities(capabilities, self.registry), {})

    def orchestration_for(self, realization):
        return resolve_ros_orchestration(
            realization, self.registry, self.system_model, self.manifest,
        )

    def test_rejects_unknown_fields_and_invalid_values(self) -> None:
        with self.assertRaises(ValidationError):
            CapabilitySelections.model_validate({"nodes": {"rdrive_node": {"wheel_radius": 0.9}}})
        with self.assertRaises(ValidationError):
            CapabilitySelections.model_validate({"mapping": {
                "enabled": True, "selection_basis": "explicit",
            }})
        with self.assertRaises(ValidationError):
            MissionInterpretation.model_validate({
                "status": "valid",
                "capabilities": {"mapping": {"enabled": False}, "exploration": {"enabled": True}},
            })

    def test_kiss_icp_has_deterministic_implications(self) -> None:
        mission = CapabilitySelections.model_validate({"odometry": {
            "enabled": True, "implementation": "kiss_icp", "selection_basis": "explicit",
        }})
        self.assertEqual(self.plan_for(mission).user_values, {
            "launch.launch_kinematic_icp": False,
            "launch.launch_kiss_icp": True,
            "launch.launch_laserscan_to_pointcloud": True,
        })

    def test_interpreter_uses_strict_json_contract(self) -> None:
        backend = lambda *_: ('{"status":"valid","capabilities":'
                              '{"navigation":{"enabled":false},"odometry":{"enabled":true,'
                              '"implementation":"kiss_icp","selection_basis":"explicit"}}}')
        result = MissionInterpreter(backend, system_prompt="test", registry=self.registry).interpret(
            "Map without navigation using KISS-ICP")
        self.assertFalse(result.capabilities.navigation.enabled)
        self.assertEqual(result.capabilities.odometry.implementation, "kiss_icp")

    def test_interpreter_rejects_prose_and_invented_keys(self) -> None:
        with self.assertRaises(ValueError):
            MissionInterpreter(lambda *_: 'Here is JSON: {"status":"valid"}',
                               system_prompt="test", registry=self.registry).interpret("baseline")
        with self.assertRaises(ValueError):
            MissionInterpreter(lambda *_: '{"unsupported":true}',
                               system_prompt="test", registry=self.registry).interpret("unsupported")

    def test_ollama_404_identifies_missing_model(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat", 404, "Not Found", {},
            BytesIO(b'{"error":"model qwen2.5-coder:7b not found"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, r"ollama pull qwen2.5-coder:7b"):
                OllamaBackend(model="qwen2.5-coder:7b")("system", "mission")

    def test_interpretation_status_invariants_are_terminal(self) -> None:
        valid = MissionInterpretation.model_validate({
            "status": "valid", "capabilities": {"navigation": {"enabled": True}},
        })
        self.assertTrue(valid.capabilities.navigation.enabled)
        MissionInterpretation.model_validate({"status": "unsupported", "reason": "not available"})
        MissionInterpretation.model_validate({
            "status": "needs_clarification", "reason": "missing value",
            "clarification_question": "What value should be used?",
        })
        invalid = (
            {"status": "valid"},
            {"status": "unsupported", "capabilities": {}},
            {"status": "needs_clarification", "reason": "missing value"},
        )
        for payload in invalid:
            with self.assertRaises(ValidationError):
                MissionInterpretation.model_validate(payload)

    def test_sparse_capability_contract_rejects_inconsistent_states(self) -> None:
        invalid = (
            {"capabilities": {"mapping": {"enabled": False, "implementation": "cartographer",
                                            "selection_basis": "explicit"}}},
            {"capabilities": {"mapping": {"enabled": True, "selection_basis": "explicit"}}},
            {"capabilities": {"mapping": {"enabled": True, "implementation": "orb_slam",
                                            "selection_basis": "explicit"}}},
        )
        for payload in invalid:
            with self.assertRaises(ValidationError):
                CapabilitySelections.model_validate(payload["capabilities"])

        selected_without_provenance = CapabilitySelections.model_validate({
            "odometry": {"enabled": True, "implementation": "kiss_icp"},
        })
        self.assertIsNone(selected_without_provenance.odometry.selection_basis)

    def test_capability_registry_defaults_and_explicit_choices_are_deterministic(self) -> None:
        default_mapping = CapabilitySelections.model_validate({"mapping": {"enabled": True}})
        self.assertEqual(self.plan_for(default_mapping).user_values, {
            "launch.launch_cartographer": True,
        })
        explicit = CapabilitySelections.model_validate({
                "mapping": {"enabled": False},
                "odometry": {"enabled": True, "implementation": "kiss_icp",
                             "selection_basis": "requirement_match"},
        })
        self.assertEqual(self.plan_for(explicit).user_values, {
            "launch.launch_cartographer": False,
            "launch.launch_kinematic_icp": False,
            "launch.launch_kiss_icp": True,
            "launch.launch_laserscan_to_pointcloud": True,
        })

    def test_unmentioned_capabilities_do_not_create_renderer_values(self) -> None:
        mission = CapabilitySelections.model_validate({})
        self.assertEqual(self.plan_for(mission).user_values, {})

    def test_capability_prompt_exposes_semantics_but_hides_renderer_realization(self) -> None:
        template = Path("prompts/mission_interpretation.txt").read_text(encoding="utf-8")
        prompt = build_interpretation_prompt(template, self.registry)
        self.assertIn('"kiss_icp"', prompt)
        self.assertIn("point-cloud", prompt)
        self.assertNotIn("launch.launch_kiss_icp", prompt)
        self.assertNotIn("{{ANTROBOT_CAPABILITY_REGISTRY}}", prompt)
        self.assertNotIn("{{MISSION_INTERPRETATION_JSON_SCHEMA}}", prompt)

    def test_capability_interpreter_validates_and_realizes_model_output(self) -> None:
        raw = json.dumps({
            "status": "valid",
            "capabilities": {
                    "odometry": {"enabled": True, "implementation": "kiss_icp",
                                 "selection_basis": "explicit"},
            },
        })
        interpreter = MissionInterpreter(lambda *_: raw, system_prompt="prompt", registry=self.registry)
        result = interpreter.interpret("Use KISS-ICP")
        self.assertEqual(result.capabilities.odometry.implementation, "kiss_icp")

    def test_realization_provenance_and_full_parameter_catalogue_are_independent(self) -> None:
        mission = CapabilitySelections.model_validate({"odometry": {
            "enabled": True, "implementation": "kiss_icp", "selection_basis": "explicit",
        }})
        realization = realize_capabilities(mission, self.registry)
        components = {item.component_id: item for item in realization.components}
        self.assertEqual(components["kiss_icp"].reason, "selected_implementation")
        self.assertEqual(components["laserscan_to_pointcloud"].reason, "required_dependency")
        self.assertEqual(components["kinematic_icp"].reason, "displaced_implementation")
        self.assertFalse(components["kinematic_icp"].enabled)

        schema = json.loads(Path(
            "artifacts/template_configuration/template_configuration_schema.json"
        ).read_text(encoding="utf-8"))
        orchestration = self.orchestration_for(realization)
        visible = derive_parameter_catalogue(realization, schema)
        identifiers = {item.parameter_id for item in visible.parameters}
        self.assertIn("nodes.kiss_icp.max_range", identifiers)
        self.assertIn("nodes.joint_state_estimator.publish_frequency", identifiers)
        self.assertIn("nodes.kinematic_icp.max_range", identifiers)
        self.assertIn("nodes.rdrive_node.wheel_radius", identifiers)

        parameters = {item.parameter_id: item for item in visible.parameters}
        wheel_radius_relationships = parameters[
            "nodes.rdrive_node.wheel_radius"
        ].relationships
        self.assertIn({
            "kind": "same_parameter_name",
            "target": "nodes.joint_state_estimator.wheel_radius",
        }, wheel_radius_relationships)

    def test_parameter_changes_are_validated_against_full_catalogue(self) -> None:
        mission = CapabilitySelections.model_validate({"odometry": {
            "enabled": True, "implementation": "kiss_icp", "selection_basis": "explicit",
        }})
        realization = realize_capabilities(mission, self.registry)
        schema = json.loads(Path(
            "artifacts/template_configuration/template_configuration_schema.json"
        ).read_text(encoding="utf-8"))
        orchestration = self.orchestration_for(realization)
        catalogue = derive_parameter_catalogue(realization, schema)
        changes = [ParameterChange.model_validate({
            "parameter_id": "nodes.kiss_icp.max_range", "value": 20.0,
            "evidence": "limit its range to 20 metres",
        })]
        self.assertEqual(validate_parameter_changes(changes, catalogue), {
            "nodes.kiss_icp.max_range": 20.0,
        })
        inactive_component_change = [ParameterChange.model_validate({
            "parameter_id": "nodes.kinematic_icp.max_range", "value": 20.0,
        })]
        self.assertEqual(validate_parameter_changes(inactive_component_change, catalogue), {
            "nodes.kinematic_icp.max_range": 20.0,
        })

    def test_required_ros_connections_are_projected_with_partial_external_status(self) -> None:
        capabilities = CapabilitySelections.model_validate({"odometry": {
            "enabled": True, "implementation": "kiss_icp", "selection_basis": "explicit",
        }})
        orchestration = self.orchestration_for(realize_capabilities(capabilities, self.registry))
        requirements = {item.connection_id: item for item in orchestration.required_connections}
        self.assertEqual(orchestration.status, "partially_verified")
        self.assertEqual(requirements["kiss_icp_pointcloud_input"].validation_status,
                         "external_unknown")
        self.assertEqual(requirements["pointcloud_converter_scan_input"].effective_name, "/scan")
        self.assertTrue(any(
            item.binding_id == "wiring.scan_pointcloud_topic"
            and "nodes.laserscan_to_pointcloud.pointcloud_topic" in item.active_targets
            for item in orchestration.wiring_bindings
        ))
        self.assertTrue(any(
            item.source_component == "rdrive_node"
            and item.target_component == "joint_state_estimator"
            and item.effective_name == "/odom_wheel"
            for item in orchestration.discovered_connections
        ))
        self.assertTrue(any(item.status == "unknown" for item in orchestration.qos_checks))

    def test_missing_local_required_endpoint_invalidates_orchestration(self) -> None:
        capabilities = CapabilitySelections.model_validate({"odometry": {
            "enabled": True, "implementation": "kiss_icp", "selection_basis": "explicit",
        }})
        model = copy.deepcopy(self.system_model)
        for instance in model["deployment_instances"]:
            if instance["effective_node_name"] in {"rplidar_node", "kiss_icp"}:
                instance["resolution_status"] = "resolved_local"
                instance["interfaces"] = []
        orchestration = resolve_ros_orchestration(
            realize_capabilities(capabilities, self.registry), self.registry, model, self.manifest,
        )
        self.assertEqual(orchestration.status, "invalid")
        self.assertTrue(orchestration.missing_required_connections)


if __name__ == "__main__":
    unittest.main()
