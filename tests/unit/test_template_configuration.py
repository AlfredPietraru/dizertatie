from __future__ import annotations

import unittest

from ros_config_builder import (
    build_system_model,
    build_template_configuration_schema,
    extract_launch_files,
    extract_package_metadata,
    extract_parameter_yaml,
    extract_ros_node_ir,
    curate_template_configuration_schema,
    load_template_configuration_policy,
)


class TemplateConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        roots = ["src/antrobot_ros", "src/antrobot_description"]
        cls.model = build_system_model(
            workspace=".", step1=extract_ros_node_ir(".", source_roots=roots),
            launch=extract_launch_files(".", source_roots=roots),
            configuration=extract_parameter_yaml(".", source_roots=roots),
            package_metadata=extract_package_metadata(".", source_roots=["src"]),
            root_launch_files=["src/antrobot_ros/launch/antrobot.launch.py"],
        )

    def test_classifies_launch_parameters_and_structural_values(self) -> None:
        schema = build_template_configuration_schema(self.model)
        cartographer = next(x for x in schema["launch_options"] if x["name"] == "launch_cartographer")
        self.assertEqual(cartographer["classification"], "configurable")
        self.assertEqual(cartographer["value_type"], "boolean")
        namespace = next(x for x in schema["structural_values"] if x["name"] == "namespace")
        self.assertEqual(namespace["template_key"], "launch.namespace")
        odom = next(x for x in schema["derived_values"]
                    if x["template_key"] == "nodes.rdrive_node.odom_topic")
        self.assertEqual(odom["classification"], "derived")
        frequency = next(x for x in schema["node_parameters"]["joint_state_estimator"]
                         if x["name"] == "publish_frequency")
        self.assertEqual(frequency["classification"], "configurable")
        self.assertTrue(frequency["provenance"])

    def test_manual_override_is_explicit_and_traceable(self) -> None:
        key = "nodes.rdrive_node.wheel_radius"
        schema = build_template_configuration_schema(self.model, overrides={key: {
            "classification": "fixed", "group": "internal",
            "classification_reason": "fixed for this physical robot", "review_required": False,
        }})
        value = next(x for x in schema["internal_values"] if x["template_key"] == key)
        self.assertEqual(value["decision_source"], "manual_override")
        self.assertFalse(value["review_required"])

    def test_curated_policy_freezes_all_decisions(self) -> None:
        candidate = build_template_configuration_schema(self.model)
        policy = load_template_configuration_policy("policies/template_configuration_policy.yaml")
        frozen = curate_template_configuration_schema(candidate, policy)
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["summary"]["review_required"], 0)
        self.assertEqual(frozen["summary"]["proposed"], 0)
        self.assertEqual(frozen["summary"]["rejected"], 1)
        self.assertEqual(len(frozen["role_groups"]["hardware"]), 8)
        self.assertTrue(all(item["platform_policy"].get("antrobot") == "fixed"
                            for item in frozen["role_groups"]["hardware"]))
        self.assertNotIn("launch.params_file", frozen["frozen_inputs"])


if __name__ == "__main__":
    unittest.main()
