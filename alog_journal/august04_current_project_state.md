# Current project state — 4 August 2026

## Summary

The launch-template experiment can now describe and generate every non-empty
ROS 2 Python launch file examined in `src/antrobot_ros/launch`.

- 17 JSON launch descriptions exist in `launch_templates/`.
- 17 corresponding Python launch files are generated in
  `artifacts/launch_files/`.
- All generated JSON files pass JSON parsing and generator validation.
- All generated Python launch files pass Python compilation.
- The Xacro expansion used by `robot_state.launch.py` was also tested
  successfully after sourcing the workspace and using the system ROS Python
  environment.
- `map_converter_explore.launch.py` remains excluded because it is empty and
  contains no launch description to reproduce.

This means the current result is **17 of 17 non-empty launch files covered**.

## Main design decision

The generator does not try to reproduce every piece of arbitrary Python found
in the original launch files. Configuration that is intended to be fixed when
the system starts is represented directly in JSON or YAML instead.

`LaunchConfiguration` was not removed completely. It remains appropriate for
real launch-time choices, especially:

- the robot namespace;
- enabling or disabling a subsystem in the top-level launch orchestrator;
- passing the namespace to an included launch file;
- setting `frame_prefix` from the selected namespace.

It is no longer used for ordinary node settings that have been classified as
deterministic startup configuration. Those settings now come from YAML or JSON.
As a consequence, some values that were previously exposed as command-line
launch arguments can no longer be overridden when calling `ros2 launch`.

The intended separation is:

- JSON describes launch structure, nodes, includes, fixed arguments, and
  template-specific operations.
- YAML contains node configuration that is naturally maintained as ROS
  parameters.
- `LaunchConfiguration` is reserved for genuine launch-time choices.
- Defaults declared in node code remain fallbacks when a YAML value is absent.

## Supported template concepts

The template system currently covers:

- launch-argument declarations;
- ROS node creation;
- namespaces through `LaunchConfiguration`;
- YAML parameter-section loading through `load_node_params`;
- direct ROS parameter-file paths;
- literal inline parameters, including strings, numbers, and booleans;
- inline parameter values referencing generated variables;
- inline parameter values referencing a `LaunchConfiguration`;
- node command-line arguments containing literals and generated variables;
- remappings derived from YAML values;
- `emulate_tty` and process prefixes;
- package-relative computed paths;
- conditional and unconditional launch-file includes;
- include sources built with `FindPackageShare` and
  `PathJoinSubstitution`;
- forwarding launch arguments to included files;
- multiple explicitly described nodes;
- Xacro processing for a generated `robot_description`.

## Coverage by launch file

| Launch file | Current representation |
|---|---|
| `antrobot.launch.py` | Arguments and conditional child-launch includes |
| `cartographer.launch.py` | Fixed Cartographer filename, resolution, and publish period in JSON |
| `explore_lite.launch.py` | Both nodes load the shared YAML file; no runtime behavior override |
| `joint_state_estimator.launch.py` | Loaded YAML parameter section and one node |
| `keyboard_teleoperation.launch.py` | Direct YAML path, `emulate_tty`, and terminal prefix |
| `kinematic_icp.launch.py` | YAML parameters plus an extracted remapping target |
| `kiss_icp.launch.py` | Two YAML-section extractions and two remappings |
| `kiss_icp_rviz.launch.py` | Computed RViz path and node arguments |
| `laserscan_to_pointcloud.launch.py` | Loaded YAML parameter section and one node |
| `nav2.launch.py` | Package-relative defaults and an external package launch include |
| `odom_eval.launch.py` | Direct `odom_eval_params.yaml` loading with no runtime overrides |
| `odom_monitor.launch.py` | Deterministic topics and thresholds stored as inline JSON parameters |
| `rdrive.launch.py` | Raw YAML section passed directly to the node without reconstructed casts |
| `robot_state.launch.py` | Xacro processing and structured robot-state-publisher parameters |
| `rplidar.launch.py` | Loaded YAML parameter section and one node |
| `rviz.launch.py` | Computed RViz path and node arguments |
| `tf_static_link.launch.py` | Two transforms expanded into two explicit JSON node descriptions |

## Important caveats

### Behavioral equivalence is intentional, but not always textual equivalence

The generated files are not expected to be textually identical to the original
launch files. They may differ in formatting, variable names, action placement,
construction order, comments, and imports. The goal is equivalent launch
behavior under the revised deterministic-configuration policy.

Some generated files still contain generic imports that they do not use. This
does not prevent execution, but the main template could later make its imports
conditional to produce cleaner output.

### Some original runtime overrides were deliberately removed

The following changes are design decisions rather than perfect reproductions of
the old interfaces:

- `odom_eval` no longer exposes `params_file`, `output_dir`, or
  `reference_frame` as launch-time overrides. The values come from
  `odom_eval_params.yaml`.
- `odom_monitor` no longer exposes its jump thresholds as launch arguments. The
  selected values are stored in `odom_monitor.json`.
- `explore_lite` no longer accepts a child-level `enable_explore_lite` launch
  argument. The top-level `explore_lite` argument only controls whether the
  complete child launch is included.
- Cartographer's configuration filename, map resolution, and publish period are
  stored directly in `cartographer.json` rather than extracted from
  `antrobot_params.yaml`.

This is correct only while those values are intended to be deterministic at
startup. If users later need command-line overrides, the schema will need to
represent that distinction explicitly again.

### Explore Lite currently has a configuration decision to confirm

`src/antrobot_ros/config/explore_lite_params.yaml` currently contains:

```yaml
enable_explore_lite: false
```

Because the runtime override was removed, this YAML value is now authoritative.
It must be changed to the intended startup value if the map converter should be
enabled whenever the Explore Lite subsystem is launched.

The YAML file uses the `/**` wildcard and is supplied to both the Antrobot map
converter and the external Explore Lite node. This should be runtime-tested to
confirm both nodes accept the shared wildcard parameter file without warnings
or undeclared-parameter issues.

### RDrive relies on YAML typing and node defaults

The generated RDrive launch file passes the loaded `rdrive` YAML section directly
to the node. It no longer applies `float`, `int`, or `bool` conversions, and it
does not insert fallback values in the launch file.

Therefore:

- YAML values must have the correct ROS-compatible scalar types.
- Missing values fall back to defaults declared by `rdrive_node.py`.
- YAML defaults and node-code defaults should be kept consistent.
- Care is required with YAML boolean syntax and values that could otherwise be
  interpreted as integers instead of floating-point numbers.

### Static transforms are now expanded at generation time

`tf_static_link.json` contains two explicit static-transform-publisher nodes.
The generated launch file no longer reads the transforms list or
`publish_transforms` from `antrobot_params.yaml`.

The top-level `launch_tf_static_link` argument determines whether this child
launch file is included. If the transforms change in YAML, the JSON will not
update automatically. The JSON entries must be updated as well, or the project
should later choose one authoritative source and generate the other from it.

This also changes the previous double-enable behavior: enabling the child include
now starts the explicit transform nodes without consulting the old
`publish_transforms` YAML flag.

### Configuration can drift between JSON and YAML

Some values that originally lived in `antrobot_params.yaml` now live in JSON,
especially Cartographer settings and static transforms. Keeping the same value
in both places creates a risk of silent divergence.

A follow-up cleanup should either:

1. remove obsolete duplicate values from YAML;
2. declare JSON as authoritative and generate the YAML where appropriate; or
3. add validation that reports conflicting duplicated values.

### Robot state has real runtime dependencies

`robot_state.launch.py` still performs a genuine preprocessing operation. The
template locates `antrobot_description/urdf/antrobot.xacro`, processes it with
Xacro, and passes the resulting XML as `robot_description`.

Successful runtime use requires:

- the workspace to be built and sourced so `antrobot_description` can be found;
- the ROS Xacro package and its Python dependencies, including PyYAML;
- the Xacro file and all of its included resources to be installed correctly.

The default Miniconda Python in the current shell can import ROS Xacro but lacks
PyYAML. Xacro expansion passed with `/usr/bin/python3` after sourcing
`install/setup.bash`. The runtime environment used by ROS launch must therefore
be checked carefully.

### Validation is mostly static

The current validation proves that:

- every JSON document is parseable;
- the generator accepts every description;
- every generated Python file is syntactically valid;
- Xacro expansion works in the sourced system ROS environment.

It does not yet prove full runtime equivalence. The launch files have not all
been executed because several start hardware drivers or the complete robot
stack. Runtime tests should eventually cover parameter types, topic remappings,
included package availability, namespace behavior, external node CLI arguments,
and clean shutdown.

### Repository state

The repository still has no Git commits. The generator, templates, journal, and
supporting scripts are untracked. The current working state should be committed
before further refactoring so there is a reproducible baseline and changes can
be reviewed safely.

## Current conclusion

The JSON-and-Jinja approach is sufficient for all non-empty launch files in the
current dataset, provided deterministic startup configuration is accepted as a
design constraint. The successful coverage does not mean arbitrary ROS 2 Python
launch programs can be represented. It means the current launch files were
either declarative already, simplified into deterministic descriptions, or
covered by one explicit specialized operation for Xacro processing.

The next most valuable work is runtime validation, removal or validation of
duplicated JSON/YAML configuration, confirmation of the Explore Lite YAML value,
and creation of the first version-controlled baseline.
