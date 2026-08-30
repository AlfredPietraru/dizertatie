from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ros_config_builder.extraction.python import build_module_ir, extract_ros_node_ir


SOURCE = '''
from rclpy.node import Node as ROSNode
from rclpy.qos import QoSProfile as QP
from sensor_msgs.msg import JointState
from nav_msgs.msg import Odometry
from example_interfaces.srv import SetBool
from rclpy.action import ActionServer, ActionClient
from nav2_msgs.action import NavigateToPose
import tf2_ros as tf

DEFAULT_TOPIC = "odom"

class Helper:
    pass

def make_subscription(node, topic):
    return node.create_subscription(Odometry, topic, node.on_msg, 10)

class BaseDemo(ROSNode):
    def __init__(self):
        super().__init__('base_demo')
        self.base_pub = self.create_publisher(JointState, 'base_events', 10)

class Demo(BaseDemo):
    RATE = 10.0

    def __init__(self):
        super().__init__('demo')
        self.declare_parameter('input', DEFAULT_TOPIC)
        self.declare_parameter('rate', 10.0)
        self.input_topic = self.get_parameter('input').value
        self.rate = self.get_parameter('rate').value
        period = 1.0 / self.rate
        qos = QP(depth=10)
        self.sub = self.create_subscription(
            msg_type=Odometry, topic=self.input_topic,
            callback=self.on_msg, qos_profile=qos)
        self.pub = self.create_publisher(JointState, 'joints', qos)
        self.timer = self.create_timer(period, self.tick)
        self.service = self.create_service(SetBool, '~/enable', self.enable)
        self.action_server = ActionServer(
            self, NavigateToPose, 'navigate', self.execute,
            goal_callback=self.goal, cancel_callback=self.cancel)
        self.action_client = ActionClient(self, NavigateToPose, 'navigate')
        self.buffer = tf.Buffer()
        if self.get_parameter('broadcast').value:
            self.broadcaster = tf.TransformBroadcaster(self)

        topics = ['/front', '/rear']
        for topic in topics:
            self.create_subscription(Odometry, topic, self.on_msg, 10)

    def tick(self):
        self.pub.publish(JointState())

def main():
    node = Demo()
'''


class ModuleIRTests(unittest.TestCase):
    def test_preserves_python_facts_and_aliases(self) -> None:
        module, _ = build_module_ir(SOURCE, file_path="demo/nodes.py", module_name="demo.nodes", package="demo")
        self.assertEqual(module["imports"]["ROSNode"]["qualified_name"], "rclpy.node.Node")
        self.assertEqual(module["imports"]["tf"]["qualified_name"], "tf2_ros")
        self.assertEqual(module["global_assignments"][0]["value"]["resolved_value"], "odom")
        self.assertEqual([item["name"] for item in module["classes"]], ["Helper", "BaseDemo", "Demo"])
        self.assertTrue(module["ros_factories"][0]["ros_factory"])
        demo_entrypoint = next(item for item in module["entrypoints"][0]["node_instantiations"]
                               if item["class"] == "Demo")
        self.assertEqual(demo_entrypoint["class"], "Demo")

    def test_ros_interpretation_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "demo"
            package.mkdir(parents=True)
            (package / "nodes.py").write_text(SOURCE, encoding="utf-8")
            payload = extract_ros_node_ir(root)
        self.assertEqual(payload["summary"]["nodes"], 2)
        node = next(item for item in payload["nodes"] if item["class_name"] == "Demo")
        self.assertEqual(node["node_name"]["resolved_value"], "demo")
        self.assertEqual(len(node["parameters"]), 2)
        direct_sub = next(item for item in node["subscriptions"] if item["variable"] == "self.sub")
        self.assertEqual(direct_sub["message_type"]["resolved"], "nav_msgs.msg.Odometry")
        self.assertEqual(direct_sub["topic"]["resolved_value"], "odom")
        self.assertEqual(node["timers"][0]["period"]["resolved_value"], 0.1)
        self.assertEqual(node["services"][0]["name"]["resolved_value"], "~/enable")
        self.assertEqual({item["kind"] for item in node["tf_entities"]}, {"buffer", "broadcaster"})
        broadcaster = next(item for item in node["tf_entities"] if item["kind"] == "broadcaster")
        self.assertEqual(broadcaster["conditions"][0]["expression"],
                         "self.get_parameter('broadcast').value")
        self.assertEqual(broadcaster["conditions"][0]["resolution"]["status"], "partial")
        self.assertTrue(any(item["kind"] == "publishes_via" for item in node["relationships"]))
        self.assertTrue(any(item["inherited"] and item["origin_class"] == "BaseDemo"
                            for item in node["publishers"]))
        loop_topics = {item["topic"]["resolved_value"] for item in node["subscriptions"]
                       if item["generated_from_loop"]}
        self.assertEqual(loop_topics, {"/front", "/rear"})
        self.assertEqual({item["kind"] for item in node["actions"]}, {"server", "client"})
        server = next(item for item in node["actions"] if item["kind"] == "server")
        self.assertEqual(server["action_type"]["resolved"], "nav2_msgs.action.NavigateToPose")
        self.assertEqual(set(server["callbacks"]), {"execute_callback", "goal_callback", "cancel_callback"})


if __name__ == "__main__":
    unittest.main()
