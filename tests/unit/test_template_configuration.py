from __future__ import annotations

import unittest

from ros_config_builder import (
    TemplateConfigurationSchema,
    build_system_model,
    build_template_configuration_schema,
    extract_launch_files,
    extract_package_metadata,
    extract_parameter_yaml,
    extract_ros_node_ir,
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

    def test_exposes_launch_arguments_and_all_ros_parameters(self) -> None:
        schema = build_template_configuration_schema(self.model)
        cartographer = next(
            x for x in schema["launch_arguments"] if x["name"] == "launch_cartographer"
        )
        self.assertEqual(cartographer["value_type"], "boolean")
        namespace = next(x for x in schema["launch_arguments"] if x["name"] == "namespace")
        self.assertEqual(namespace["template_key"], "launch.namespace")
        odom = next(x for x in schema["node_parameters"]["rdrive_node"]
                    if x["template_key"] == "nodes.rdrive_node.odom_topic")
        self.assertIn(odom["template_key"], schema["configuration_keys"])
        frequency = next(x for x in schema["node_parameters"]["joint_state_estimator"]
                         if x["name"] == "publish_frequency")
        self.assertTrue(frequency["provenance"])

    def test_schema_is_complete_without_an_allow_deny_policy(self) -> None:
        schema = build_template_configuration_schema(self.model)
        self.assertTrue(schema["complete"])
        self.assertEqual(schema["stage"], "semantic_configuration_model")
        self.assertEqual(
            schema["summary"]["configuration_values"], len(schema["configuration_keys"])
        )
        wheel = next(x for x in schema["node_parameters"]["rdrive_node"]
                     if x["name"] == "wheel_radius")
        self.assertIn("nodes.joint_state_estimator.wheel_radius",
                      {item["target"] for item in wheel["relationships"]})
        self.assertIn(wheel["template_key"], schema["configuration_keys"])
        self.assertNotIn("classification", wheel)
        self.assertNotIn("review_status", wheel)

    def test_wildcard_yaml_is_one_physical_slot_with_all_affected_components(self) -> None:
        schema = build_template_configuration_schema(self.model)
        publish_rate = next(
            item for item in schema["node_parameters"]["explore"]
            if item["name"] == "publish_rate"
        )
        self.assertEqual(
            publish_rate["affected_components"],
            ["explore", "explore_lite_map_converter"],
        )
        self.assertNotIn("explore_lite_map_converter", schema["node_parameters"])
        self.assertEqual(schema["summary"]["configuration_values"], 109)
        self.assertEqual(schema["summary"]["effective_parameter_occurrences"], 114)

    def test_schema_rejects_duplicate_configuration_keys(self) -> None:
        schema = build_template_configuration_schema(self.model)
        schema["configuration_keys"] = [
            *schema["configuration_keys"], schema["configuration_keys"][0],
        ]

        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            TemplateConfigurationSchema.model_validate(schema)


if __name__ == "__main__":
    unittest.main()
