from __future__ import annotations

import unittest

from ros_config_builder import (
    build_system_model, extract_launch_files, extract_package_metadata,
    extract_parameter_yaml, extract_ros_node_ir, validate_system_model,
)


class AntRobotSystemModelTests(unittest.TestCase):
    def test_builds_rooted_local_and_external_instances(self) -> None:
        roots = ["src/antrobot_ros", "src/antrobot_description"]
        model = build_system_model(
            workspace=".",
            step1=extract_ros_node_ir(".", source_roots=roots),
            launch=extract_launch_files(".", source_roots=roots),
            configuration=extract_parameter_yaml(".", source_roots=roots),
            package_metadata=extract_package_metadata(".", source_roots=["src"]),
            root_launch_files=["src/antrobot_ros/launch/antrobot.launch.py"],
        )
        self.assertEqual(model["summary"]["roots"], 1)
        self.assertGreaterEqual(model["summary"]["local_instances"], 4)
        self.assertGreaterEqual(model["summary"]["external_instances"], 1)
        rdrive = next(x for x in model["deployment_instances"] if x["executable"] == "rdrive_node")
        self.assertEqual(rdrive["resolution_status"], "resolved_local")
        self.assertEqual(rdrive["interface_status"], "known")
        self.assertTrue(any(x["name"] == "wheel_radius" and len(x["provenance"]) >= 2
                            for x in rdrive["effective_parameters"]))
        lidar = next(x for x in model["deployment_instances"] if x["executable"] == "rplidar_node")
        self.assertEqual(lidar["resolution_status"], "external")
        self.assertEqual(lidar["interface_status"], "unknown")
        validation = validate_system_model(model)
        self.assertTrue(validation["valid"])
        self.assertGreaterEqual(validation["coverage"]["confirmed_edges"], 1)
        edge = next(item for item in model["edges"] if item["name"] == "/odom_wheel")
        self.assertEqual(edge["confidence"], "confirmed")


if __name__ == "__main__":
    unittest.main()
