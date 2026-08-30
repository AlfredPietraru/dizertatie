from __future__ import annotations

import unittest

from ros_config_builder import (
    build_system_model,
    build_template_configuration_schema,
    build_template_definition,
    extract_launch_files,
    extract_package_metadata,
    extract_parameter_yaml,
    extract_ros_node_ir,
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
        cls.schema = build_template_configuration_schema(model)

    def test_source_derived_bundle_has_complete_coverage(self) -> None:
        manifest, templates, baseline = build_template_definition(self.schema, self.configuration)
        coverage = validate_template_definition(manifest, templates, baseline, self.schema)
        self.assertTrue(coverage["valid"])
        self.assertEqual(
            coverage["configuration_values"], self.schema["summary"]["configuration_values"]
        )
        self.assertEqual(
            coverage["template_references"], self.schema["summary"]["configuration_values"]
        )
        self.assertEqual(
            coverage["interface_parameters"], len(self.schema["interface_parameter_keys"])
        )
        self.assertEqual(
            coverage["bound_interface_parameters"],
            len(self.schema["interface_parameter_keys"]),
        )
        self.assertEqual(
            len(baseline["values"]), self.schema["summary"]["configuration_values"]
        )
        self.assertIn("launch/antrobot_profile.launch.py.j2", templates)
        self.assertIn("launch.params_file", baseline["values"])
        self.assertEqual(
            set(manifest),
            {"schema_version", "templates", "wiring_bindings", "baseline_profile"},
        )

    def test_incomplete_schema_cannot_define_templates(self) -> None:
        candidate = dict(self.schema); candidate["complete"] = False
        with self.assertRaises(ValueError):
            build_template_definition(candidate, {"profiles": []})


if __name__ == "__main__":
    unittest.main()
