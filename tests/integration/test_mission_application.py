from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

from ros_config_builder.mission import (
    MissionInterpreter, ParameterReasoner, load_capability_registry,
)
from ros_config_builder.orchestrate import MissionApplication, load_environment


class MissionApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path.cwd()
        self.registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        self.selection_prompt = Path("prompts/parameter_selection.txt").read_text(encoding="utf-8")
        self.value_prompt = Path("prompts/parameter_value_reasoning.txt").read_text(encoding="utf-8")

    def reasoner(self, selection: str | None = None, value: str | None = None) -> ParameterReasoner:
        selection_response = selection or (
            '{"status":"no_change","reason":"No parameter value change was requested."}'
        )
        value_response = value or (
            '{"status":"unsupported","reason":"The value stage should not be reached."}'
        )
        return ParameterReasoner(
            lambda *_: selection_response,
            lambda *_: value_response,
            selection_prompt_template=self.selection_prompt,
            value_prompt_template=self.value_prompt,
            top_k=12,
        )

    def test_environment_file_supplies_host_without_overriding_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OLLAMA_HOST_PATH=10.0.2.2\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                load_environment(path)
                self.assertEqual(os.environ["OLLAMA_HOST_PATH"], "10.0.2.2")
            with mock.patch.dict(os.environ, {"OLLAMA_HOST_PATH": "explicit-host"}, clear=True):
                load_environment(path)
                self.assertEqual(os.environ["OLLAMA_HOST_PATH"], "explicit-host")

    def test_configured_frozen_evidence_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-evidence.json"
            with self.assertRaisesRegex(
                FileNotFoundError, "required frozen parameter evidence is missing",
            ):
                MissionApplication(
                    workspace=self.workspace,
                    interpreter=MissionInterpreter(
                        lambda *_: '{"status":"unsupported","reason":"unused"}',
                        system_prompt="test", registry=self.registry,
                    ),
                    parameter_reasoner=self.reasoner(),
                    capability_registry=self.registry,
                    parameter_evidence_path=missing,
                )

    def test_valid_mission_maps_validates_and_reaches_renderer(self) -> None:
        backend = lambda *_: (
            '{"status":"valid","capabilities":'
            '{"mapping":"cartographer","navigation":null,'
            '"exploration":"explore_lite","odometry":"kiss_icp"}}'
        )
        calls = []

        def renderer(**kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_directory"])
            (output / "launch").mkdir(parents=True)
            (output / "launch/generated.launch.py").write_text("# generated\n", encoding="utf-8")
            return {
                "output_directory": kwargs["output_directory"],
                "render_records": [{"output": "launch/generated.launch.py"}],
                "validation": {"valid": True, "equivalence": {
                    "semantically_equivalent": True, "unexpected_differences": [],
                }},
            }

        application = MissionApplication(
            workspace=self.workspace,
            interpreter=MissionInterpreter(backend, system_prompt="test", registry=self.registry),
            parameter_reasoner=self.reasoner(),
            capability_registry=self.registry,
            renderer=renderer,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = application.process("Map without navigation using KISS-ICP", output_directory=directory)
            self.assertTrue((Path(directory) / "mission_result.json").is_file())

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["plan"]["user_values"], {
            "launch.enable_explore_lite": True,
            "launch.explore_lite": True,
            "launch.launch_cartographer": True,
            "launch.launch_nav2": False,
            "launch.launch_kinematic_icp": False,
            "launch.launch_kiss_icp": True,
            "launch.launch_laserscan_to_pointcloud": True,
        })
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["require_equivalence"])

    def test_terminal_outcome_never_reaches_renderer(self) -> None:
        backend = lambda *_: (
            '{"status":"unsupported","capabilities":null,'
            '"reason":"Stereo cameras are absent from the supported capability registry."}'
        )

        def renderer(**_kwargs):
            self.fail("terminal outcomes must not invoke the renderer")

        application = MissionApplication(
            workspace=self.workspace,
            interpreter=MissionInterpreter(backend, system_prompt="test", registry=self.registry),
            parameter_reasoner=self.reasoner(),
            capability_registry=self.registry,
            renderer=renderer,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = application.process("Use a stereo camera", output_directory=directory)
            output = Path(directory)
            self.assertTrue((output / "mission_result.json").is_file())
            self.assertFalse((output / "bundle").exists())
        self.assertEqual(result["status"], "unsupported")
        self.assertIsNone(result["plan"])

    def test_terminal_parameter_outcome_never_reaches_renderer(self) -> None:
        capability_backend = lambda *_: (
            '{"status":"valid","capabilities":'
            '{"mapping":"cartographer","navigation":"nav2",'
            '"exploration":"explore_lite","odometry":"kiss_icp"}}'
        )

        def renderer(**_kwargs):
            self.fail("terminal parameter outcomes must not invoke the renderer")

        application = MissionApplication(
            workspace=self.workspace,
            interpreter=MissionInterpreter(
                capability_backend, system_prompt="test", registry=self.registry,
            ),
            parameter_reasoner=self.reasoner(selection=(
                '{"status":"needs_clarification","reason":"missing range value",'
                '"clarification_question":"What maximum range should KISS-ICP use?"}'
            )),
            capability_registry=self.registry,
            renderer=renderer,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = application.process(
                "Use KISS-ICP with a shorter range", output_directory=directory,
            )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIsNone(result["plan"])

    def test_capability_selection_reaches_renderer_through_registry(self) -> None:
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        backend = lambda *_: (
            '{"status":"valid","capabilities":'
            '{"mapping":"cartographer","navigation":"nav2",'
            '"exploration":"explore_lite","odometry":"kiss_icp"}}'
        )
        calls = []

        def renderer(**kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_directory"])
            (output / "launch").mkdir(parents=True)
            (output / "launch/generated.launch.py").write_text("# generated\n", encoding="utf-8")
            return {
                "output_directory": kwargs["output_directory"],
                "render_records": [{"output": "launch/generated.launch.py"}],
                "validation": {"valid": True, "equivalence": {
                    "semantically_equivalent": True, "unexpected_differences": [],
                }},
            }

        application = MissionApplication(
            workspace=self.workspace,
            interpreter=MissionInterpreter(
                backend, system_prompt="test", registry=registry,
            ),
            parameter_reasoner=self.reasoner(
                selection=(
                    '{"status":"valid","selected_parameters":[{'
                    '"parameter_id":"nodes.kiss_icp.max_range",'
                    '"relevance":"The request explicitly limits KISS-ICP range."}]}'
                ),
                value=(
                    '{"status":"valid","changes":[{'
                    '"parameter_id":"nodes.kiss_icp.max_range",'
                    '"old_value":3.5,"new_value":20.0,'
                    '"reason":"The requested maximum range is 20 metres."}]}'
                ),
            ),
            capability_registry=registry,
            renderer=renderer,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = application.process(
                "Map using KISS-ICP and limit its range to 20 metres", output_directory=directory,
            )
        self.assertEqual(result["plan"]["user_values"], {
            "launch.enable_explore_lite": True,
            "launch.explore_lite": True,
            "launch.launch_cartographer": True,
            "launch.launch_nav2": True,
            "launch.launch_kinematic_icp": False,
            "launch.launch_kiss_icp": True,
            "launch.launch_laserscan_to_pointcloud": True,
            "nodes.kiss_icp.max_range": 20.0,
        })
        self.assertIn("nodes.kiss_icp.max_range", {
            item["parameter_id"] for item in result["parameter_catalogue"]["parameters"]
        })
        self.assertEqual(result["parameter_selection"]["status"], "valid")
        self.assertEqual(result["parameter_value_interpretation"]["status"], "valid")
        self.assertEqual(len(calls), 1)

    def test_retrieval_selection_and_value_stages_reach_renderer_separately(self) -> None:
        reasoner = ParameterReasoner(
            lambda *_: (
                '{"status":"valid","selected_parameters":[{'
                '"parameter_id":"nodes.joint_state_estimator.publish_frequency",'
                '"relevance":"The request names joint-state publishing."}]}'
            ),
            lambda *_: (
                '{"status":"valid","changes":[{'
                '"parameter_id":"nodes.joint_state_estimator.publish_frequency",'
                '"old_value":20.0,"new_value":30.0,'
                '"reason":"The user requested 30 Hz."}]}'
            ),
            selection_prompt_template=Path("prompts/parameter_selection.txt").read_text(),
            value_prompt_template=Path("prompts/parameter_value_reasoning.txt").read_text(),
            top_k=8,
        )
        calls = []

        def renderer(**kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_directory"])
            (output / "launch").mkdir(parents=True)
            (output / "launch/generated.launch.py").write_text("# generated\n")
            return {
                "output_directory": output,
                "render_records": [{"output": "launch/generated.launch.py"}],
                "validation": {"valid": True, "equivalence": {
                    "semantically_equivalent": None, "unexpected_differences": [],
                }},
            }

        application = MissionApplication(
            workspace=self.workspace,
            interpreter=MissionInterpreter(
                lambda *_: ('{"status":"valid","capabilities":{"mapping":"cartographer",'
                           '"navigation":"nav2","exploration":"explore_lite",'
                           '"odometry":"kinematic_icp"}}'),
                system_prompt="test", registry=self.registry,
            ),
            parameter_reasoner=reasoner, capability_registry=self.registry, renderer=renderer,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = application.process(
                "Set joint-state publishing to 30 Hz", output_directory=directory,
            )
        self.assertEqual(result["parameter_selection"]["status"], "valid")
        self.assertEqual(result["parameter_value_interpretation"]["status"], "valid")
        self.assertEqual(result["plan"]["user_values"], {
            "launch.enable_explore_lite": True,
            "launch.explore_lite": True,
            "launch.launch_cartographer": True,
            "launch.launch_nav2": True,
            "launch.launch_kinematic_icp": True,
            "launch.launch_kiss_icp": False,
            "launch.launch_laserscan_to_pointcloud": False,
            "nodes.joint_state_estimator.publish_frequency": 30.0,
        })
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
