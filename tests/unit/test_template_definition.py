from __future__ import annotations

import unittest

from ros_config_builder import (
    build_system_model,
    build_template_configuration_schema,
    build_template_definition,
    curate_template_configuration_schema,
    extract_launch_files,
    extract_package_metadata,
    extract_parameter_yaml,
    extract_ros_node_ir,
    load_template_configuration_policy,
    validate_template_definition,
)


class TemplateDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        roots = ["src/antrobot_ros", "src/antrobot_description"]
        cls.configuration = extract_parameter_yaml(".", source_roots=roots)
        model = build_system_model(workspace=".",
            step1=extract_ros_node_ir(".", source_roots=roots),
            launch=extract_launch_files(".", source_roots=roots), configuration=cls.configuration,
            package_metadata=extract_package_metadata(".", source_roots=["src"]),
            root_launch_files=["src/antrobot_ros/launch/antrobot.launch.py"])
        candidate = build_template_configuration_schema(model)
        cls.schema = curate_template_configuration_schema(candidate,
            load_template_configuration_policy("policies/template_configuration_policy.yaml"))

    def test_frozen_bundle_has_complete_coverage(self) -> None:
        manifest, templates, platform, baseline = build_template_definition(self.schema, self.configuration)
        coverage = validate_template_definition(manifest, templates, self.schema)
        self.assertTrue(coverage["valid"])
        self.assertEqual(coverage["public_inputs"], 93)
        self.assertEqual(coverage["targeted_inputs"], 93)
        self.assertEqual(coverage["derived_values"], 30)
        self.assertEqual(coverage["bound_derived_values"], 30)
        self.assertEqual(coverage["platform_fixed_values"], 8)
        self.assertEqual(len(platform["fixed_values"]), 8)
        self.assertEqual(len(baseline["values"]), 93)
        self.assertIn("launch/antrobot_profile.launch.py.j2", templates)
        self.assertNotIn("launch.params_file", manifest["public_inputs"])

    def test_unreviewed_schema_cannot_define_templates(self) -> None:
        candidate = dict(self.schema); candidate["frozen"] = False
        with self.assertRaises(ValueError):
            build_template_definition(candidate, {"profiles": []})


if __name__ == "__main__":
    unittest.main()
