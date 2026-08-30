from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from ros_config_builder import (
    build_system_model, build_template_configuration_schema, build_template_definition,
    extract_launch_files, extract_package_metadata,
    extract_parameter_yaml, extract_ros_node_ir,
    reanalyze_generated_bundle, render_configuration_bundle,
    validate_configuration_values, write_template_definition,
)


class ConfigurationRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        roots = ["src/antrobot_ros", "src/antrobot_description"]
        cls.configuration = extract_parameter_yaml(".", source_roots=roots)
        cls.model = build_system_model(workspace=".",
            step1=extract_ros_node_ir(".", source_roots=roots),
            launch=extract_launch_files(".", source_roots=roots), configuration=cls.configuration,
            package_metadata=extract_package_metadata(".", source_roots=["src"]),
            root_launch_files=["src/antrobot_ros/launch/antrobot.launch.py"])
        cls.schema = build_template_configuration_schema(cls.model)
        manifest, templates, baseline = build_template_definition(cls.schema, cls.configuration)
        cls.templates_temp = tempfile.TemporaryDirectory()
        cls.template_directory = Path(cls.templates_temp.name)
        write_template_definition(manifest, templates, baseline, cls.template_directory)
        cls.manifest = manifest

    @classmethod
    def tearDownClass(cls) -> None:
        cls.templates_temp.cleanup()

    def test_accepts_known_values_and_rejects_unknown_or_invalid_values(self) -> None:
        validate_configuration_values({
            "nodes.rdrive_node.odom_topic": "changed_odom",
            "nodes.rdrive_node.wheel_radius": 0.05,
        }, self.schema, layer="test")
        cases = [{"unknown.value": 1},
                 {"nodes.joint_state_estimator.publish_frequency": "fast"}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_configuration_values(values, self.schema, layer="test")

    def test_baseline_is_deterministic_equivalent_and_non_mutating(self) -> None:
        source = Path("src/antrobot_ros/config/antrobot_params.yaml")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = render_configuration_bundle(workspace=".", template_directory=self.template_directory,
                schema=self.schema, reference_model=self.model, output_directory=first)
            two = render_configuration_bundle(workspace=".", template_directory=self.template_directory,
                schema=self.schema, reference_model=self.model, output_directory=second)
            self.assertTrue(one["validation"]["valid"])
            self.assertTrue(one["validation"]["equivalence"]["semantically_equivalent"])
            self.assertEqual([x["sha256"] for x in one["render_records"]],
                             [x["sha256"] for x in two["render_records"]])
            self.assertEqual(one["validation"]["equivalence"]["reference_projection"],
                             two["validation"]["equivalence"]["reference_projection"])
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_related_wheel_radius_values_are_rendered_together(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            result = render_configuration_bundle(
                workspace=".", template_directory=self.template_directory,
                schema=self.schema, reference_model=self.model, output_directory=output,
                user_values={
                    "nodes.rdrive_node.wheel_radius": 0.04,
                    "nodes.joint_state_estimator.wheel_radius": 0.04,
                },
                require_equivalence=False,
            )
            generated_model = reanalyze_generated_bundle(".", output, result["render_records"])
        self.assertTrue(result["validation"]["valid"])
        parameters = {
            instance["effective_node_name"]: {
                item["name"]: item["effective_value"]
                for item in instance.get("effective_parameters", [])
            }
            for instance in generated_model["deployment_instances"]
        }
        self.assertEqual(parameters["rdrive_node"]["wheel_radius"], 0.04)
        self.assertEqual(parameters["joint_state_estimator"]["wheel_radius"], 0.04)

    def test_normal_rendering_skips_offline_system_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            result = render_configuration_bundle(
                workspace=".", template_directory=self.template_directory,
                schema=self.schema, reference_model=self.model, output_directory=output,
                require_equivalence=False,
            )
            self.assertIsNone(result["generated_model"])
            self.assertTrue(result["validation"]["equivalence"]["skipped"])
            self.assertFalse((Path(output) / "generated_system_model.json").exists())


if __name__ == "__main__":
    unittest.main()
