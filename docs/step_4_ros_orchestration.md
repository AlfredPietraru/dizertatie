# Step 4 — deterministic ROS orchestration verification

Step 4 asks:

> Can the components selected for this mission be connected using the ROS interfaces and wiring facts extracted from the repository?

Selecting component names is not enough. A publisher and subscriber must agree on topic name and message type, and known QoS settings must be compatible.

## Inputs

Step 4 consumes:

- the `SystemRealization` produced for this mission;
- the validated capability registry;
- the frozen integrated ROS system model;
- `wiring_bindings` from the lean renderer manifest.

The integrated model is a saved deterministic snapshot produced before runtime. It contains deployment instances, effective interfaces, names, types, remappings, and source resolution status.

## What QoS means

QoS means **Quality of Service**. In ROS 2 it is the communication policy used by a publisher or subscriber, including properties such as:

- reliability — for example, best effort or reliable delivery;
- durability — for example, volatile or transient-local data;
- queue/history behavior.

A publisher and subscriber can use the same topic name and message type yet still fail to communicate if their QoS policies are incompatible.

The current Step 4 comparison checks deterministically modeled reliability and durability. Its result can be:

- `compatible` — the known policies do not conflict;
- `incompatible` — a known policy mismatch exists;
- `unknown` — one or both endpoints lack fully resolved QoS facts.

`unknown` is preserved as uncertainty. It is not silently labelled compatible or incompatible.

## Project active components onto the system model

For each enabled component in the realization, Step 4 finds a matching deployment instance by effective node name or executable.

It records whether the component is:

- `resolved_local` — local source and interfaces are available;
- `external` — the deployment is known but its implementation is outside the analyzed workspace;
- `unresolved` — a unique matching instance could not be established.

The projection exposes only active components for connection checking, while separately recording inactive components.

## Wiring bindings

The renderer manifest groups parameters that carry one logical topic or frame value. Example:

```json
{
  "wiring.scan_topic": {
    "baseline_value": "scan",
    "parameter_keys": [
      "nodes.kinematic_icp.lidar_topic",
      "nodes.laserscan_to_pointcloud.scan_topic"
    ]
  }
}
```

This tells Step 4 that those configuration slots participate in the same semantic connection. It does not make the values unchangeable.

For each binding, Step 4 records:

- effective baseline value;
- all parameter targets;
- targets belonging to currently active components;
- extracted interfaces affected by that effective name.

## Discovered connections

Step 4 scans active resolved endpoints. It records a discovered connection when:

- one endpoint is a publisher;
- the other is a subscription;
- their effective topic names are equal;
- their message types are equal.

Known QoS is then compared for that endpoint pair.

These discovered edges describe what the extracted system appears to connect. They are distinct from required connections.

## Required connections

The capability registry explicitly identifies connections essential to a selected implementation. For example, KISS-ICP requires:

```text
rplidar_node
  -- sensor_msgs/msg/LaserScan on /scan -->
laserscan_to_pointcloud
  -- sensor_msgs/msg/PointCloud2 on /scan_pointcloud -->
kiss_icp
```

For each required connection, Step 4 verifies:

- source component is active;
- target component is active;
- source has exactly one matching publisher;
- target has exactly one matching subscription;
- effective name matches the referenced wiring binding;
- message type matches the registry requirement;
- known QoS policies are compatible.

The required connection receives one of these statuses:

| Status | Meaning |
|---|---|
| `confirmed` | Both local endpoints match the required role, name, and type |
| `missing` | Locally inspectable endpoints fail the requirement |
| `external_unknown` | At least one endpoint is external or unresolved, so the connection cannot be fully proved locally |

## Overall status

`ROSOrchestrationPlan.status` is derived as follows:

- `invalid` if a required local connection is missing or known QoS is incompatible;
- `partially_verified` if no confirmed failure exists but an active component or required connection is external/unresolved;
- `valid` when required local connections are confirmed and no known QoS mismatch exists.

Normal mission processing stops only for `invalid`. A partially verified plan may continue, but the uncertainty remains explicit in the saved result.

## Running example

For KISS-ICP, Step 3 enables `laserscan_to_pointcloud` and `kiss_icp`. Step 4 uses the system model to find their effective interfaces and verifies:

```text
/scan
  rplidar publisher: LaserScan
  converter subscriber: LaserScan

/scan_pointcloud
  converter publisher: PointCloud2
  KISS-ICP subscriber: PointCloud2
```

It also records the controlling wiring bindings and known QoS results.

## Output

`ROSOrchestrationPlan` contains:

- active deployment components and their resolution status;
- inactive components;
- required connections;
- discovered connections;
- resolved wiring-binding records;
- QoS compatibility checks;
- overall status.

This output becomes both a deterministic gate and context for later parameter reasoning.

## What Step 4 does not do

Step 4 does not:

- start nodes or inspect a live ROS graph;
- ask an LLM whether two endpoints connect;
- decide the semantic meaning of arbitrary parameters;
- forbid topic or frame changes;
- render files.

Its conclusions are limited to the frozen source/deployment evidence. Live deployment verification remains a separate concern.

## Main implementation locations

| Location | Responsibility |
|---|---|
| `artifacts/ros_system_model/ros_system_model.json` | Frozen deployment and interface facts |
| `configuration_templates/capability_registry.yaml` | Required connection declarations |
| `configuration_templates/manifest.json` | Wiring-binding groups |
| `src/ros_config_builder/mission/schema.py` — `resolve_ros_orchestration()` | Projection, connection matching, QoS checks, and status |

Step 5 combines the complete physical configuration schema with source evidence and this mission's active-system context.
