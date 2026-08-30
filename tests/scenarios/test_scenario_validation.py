from __future__ import annotations

import unittest

from ros_config_builder import validate_scenario_delta


def model(enabled: set[str]) -> dict:
    return {"deployment_instances": [
        {"executable": name, "enabled": name in enabled, "effective_parameters": [], "interfaces": []}
        for name in ("rdrive", "kinematic", "kiss")
    ], "edges": [{"kind": "topic", "name": "/odom_wheel", "type": "Odometry"}]}


class ScenarioValidationTests(unittest.TestCase):
    def test_accepts_only_declared_delta(self) -> None:
        baseline_context = {"values": {"launch.kiss": False}}
        scenario_context = {"values": {"launch.kiss": True}}
        result = validate_scenario_delta(
            baseline_model=model({"rdrive", "kinematic"}), scenario_model=model({"rdrive", "kiss"}),
            baseline_context=baseline_context, scenario_context=scenario_context,
            expectation={"scenario": "sensor", "changed_values": {"launch.kiss": True},
                         "enabled_executables": ["kiss"], "disabled_executables": ["kinematic"],
                         "parameter_changes": {}, "required_edges": [["topic", "/odom_wheel", "Odometry"]]},
        )
        self.assertTrue(result["valid"])

    def test_rejects_unexpected_change(self) -> None:
        result = validate_scenario_delta(
            baseline_model=model({"rdrive"}), scenario_model=model(set()),
            baseline_context={"values": {"nodes.rdrive.radius": 0.03}},
            scenario_context={"values": {"nodes.rdrive.radius": 0.04}},
            expectation={"changed_values": {}, "enabled_executables": [],
                         "disabled_executables": [], "parameter_changes": {}},
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any(error["area"] == "configuration" for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
