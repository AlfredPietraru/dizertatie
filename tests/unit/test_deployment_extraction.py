from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ros_config_builder import extract_launch_files, extract_parameter_yaml


LAUNCH = '''
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

def generate_launch_description():
    namespace_arg = DeclareLaunchArgument('namespace', default_value='robot')
    enabled = DeclareLaunchArgument('enabled', default_value='true')
    config = PathJoinSubstitution([FindPackageShare('demo'), 'config', 'demo.yaml'])
    node = Node(package='demo', executable='talker', name='talker',
                namespace=LaunchConfiguration('namespace'), parameters=[config, {'rate': 10}],
                remappings=[('input', 'events')],
                condition=IfCondition(LaunchConfiguration('enabled')))
    include = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution(
            [FindPackageShare('other'), 'launch', 'child.launch.py'])),
        launch_arguments={'namespace': LaunchConfiguration('namespace')}.items())
    return LaunchDescription([namespace_arg, enabled, node, include])
'''

YAML = '''
/**:
  ros__parameters:
    use_sim_time: false
    nested:
      topic: scan
    topics:
      - /front
      - /rear
    transforms:
      - parent_frame: base_link
        child_frame: base_footprint
        translation: [0.0, 0.0, -0.035]
robot/controller:
  ros__parameters:
    rate: 20.0
'''


class DeploymentExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "src" / "demo"
        (root / "launch").mkdir(parents=True)
        (root / "config").mkdir()
        (root / "launch" / "system.launch.py").write_text(LAUNCH, encoding="utf-8")
        (root / "config" / "demo.yaml").write_text(YAML, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_launch_arguments_nodes_includes_and_substitutions(self) -> None:
        payload = extract_launch_files(self.temp.name)
        self.assertEqual(payload["summary"], {"launch_files": 1, "arguments": 2, "nodes": 1,
                         "includes": 1, "unresolved_expressions": 0, "parse_failures": 0})
        node = payload["launch_files"][0]["nodes"][0]
        self.assertEqual(node["package"]["resolved_value"], "demo")
        self.assertEqual(node["namespace"]["resolved_value"]["kind"], "launch_configuration")
        self.assertEqual(node["parameters"][0]["kind"], "file")
        self.assertEqual(node["parameters"][1], {"kind": "inline", "value": {"rate": 10}})
        include = payload["launch_files"][0]["includes"][0]
        self.assertEqual(include["source_reference"]["resolved_value"]["kind"], "launch_source")

    def test_yaml_profiles_and_nested_parameter_flattening(self) -> None:
        payload = extract_parameter_yaml(self.temp.name)
        self.assertEqual(payload["summary"]["profiles"], 2)
        wildcard = next(item for item in payload["profiles"] if item["node_selector"] == "/**")
        self.assertEqual(wildcard["flattened_parameters"]["nested.topic"], "scan")
        self.assertEqual(wildcard["flattened_parameters"]["topics"], ["/front", "/rear"])
        self.assertEqual(wildcard["flattened_parameters"]["transforms"][0]["child_frame"],
                         "base_footprint")
        controller = next(item for item in payload["profiles"] if item["node_selector"] == "robot/controller")
        self.assertEqual(controller["namespace_selector"], "robot")


if __name__ == "__main__":
    unittest.main()
