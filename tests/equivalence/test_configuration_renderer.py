from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from ros_config_builder import (
    build_system_model, build_template_configuration_schema, build_template_definition,
    curate_template_configuration_schema, extract_launch_files, extract_package_metadata,
    extract_parameter_yaml, extract_ros_node_ir, load_template_configuration_policy,
    render_configuration_bundle, validate_public_values, write_template_definition,
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
        candidate = build_template_configuration_schema(cls.model)
        cls.schema = curate_template_configuration_schema(candidate,
            load_template_configuration_policy("policies/template_configuration_policy.yaml"))
        manifest, templates, platform, baseline = build_template_definition(cls.schema, cls.configuration)
        cls.templates_temp = tempfile.TemporaryDirectory()
        cls.template_directory = Path(cls.templates_temp.name)
        write_template_definition(manifest, templates, platform, baseline, cls.template_directory)
        cls.manifest = manifest

    @classmethod
    def tearDownClass(cls) -> None:
        cls.templates_temp.cleanup()

    def test_rejects_unknown_derived_fixed_and_inaccessible_changes(self) -> None:
        cases = [
            {"unknown.value": 1},
            {"nodes.rdrive_node.odom_topic": "broken"},
            {"nodes.rdrive_node.wheel_radius": 0.05},
            {"launch.use_sim_time": True},
            {"nodes.joint_state_estimator.publish_frequency": "fast"},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_public_values(values, self.schema, self.manifest, layer="test")

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


if __name__ == "__main__":
    unittest.main()
