### Initial ideas:
create separate components for each block in the launch files, and it can be done because the 
number of blocks are limited -> up to this point only created 4 blocks:
one for node creation (Node)
one for including the launch description (IncludeLaunchDescription)
one for parameter declarion (DeclareLaunchArgument)
one for parameter loading: load_node_params



### Difficult launch files to cover -> most probably a re writting of those elements is necessary, an LLM based rewritting?
kiss_icp.launch.py -> remappings in nodes not covered
robot_state.launch.py -> behaves wierd
rdrive.launch.py -> difficult to work with:
tf_static_link.launch.py -> this one complicated too

Maybe the direction is wrong, i am looking at the launch files -> then building the structure.   
i should go from the node code and from the config.yaml file associated 
with it, and based on that build the launch file, then compare with the prexisting launch file,
Need to build a dataset to experiment on. A curriculum-learning approach, starting
with simple examples and introducing one new launch concept at a time, might be best.

### Launch-file curriculum: easiest to hardest

The order is approximate because launch files can be complex in different ways:
number of nodes, substitutions, parameter transformations, remappings, includes,
external processing, or runtime control flow.

#### 0. Empty placeholder

- `src/antrobot_ros/launch/map_converter_explore.launch.py`

The file is currently empty, so there is nothing to generate or compare.


Observation -> for none of these i can build the json structure that populates the template, so it is still work in progress. 
#### 1. Simple, single named node 
Observation: This can be handled well by the template generator, completely covered.

- `src/antrobot_ros/launch/joint_state_estimator.launch.py`
- `src/antrobot_ros/launch/rplidar.launch.py`

Pattern: declare a namespace, load one YAML section, create one named node, and
return the argument and node.

#### 2. Simple node using command-line arguments


- `src/antrobot_ros/launch/rviz.launch.py`
- `src/antrobot_ros/launch/kiss_icp_rviz.launch.py`

These use node `arguments`, such as `['-d', rviz_config_file]`, instead of normal
ROS parameters. The node macro therefore needs optional command-line arguments.

#### 3. Nodes declared inline inside `LaunchDescription`

- `src/antrobot_ros/launch/keyboard_teleoperation.launch.py`
- `src/antrobot_ros/launch/laserscan_to_pointcloud.launch.py`

The nodes do not have intermediate Python result variables. This is mainly a
Python representation difference; a normalized model can still assign generated
result names internally.

#### 4. Launch arguments and substitution-based configuration

- `src/antrobot_ros/launch/odom_eval.launch.py`
- `src/antrobot_ros/launch/nav2.launch.py`

`odom_eval` combines `FindPackageShare`, `PathJoinSubstitution`, launch arguments,
a YAML parameter file, inline parameter overrides, and an inline node.

`nav2` is an include-wrapper rather than a direct node launch file. It declares
arguments, constructs paths with substitutions, includes the Nav2 bringup launch
file, and forwards several launch arguments.

#### 5. Parameter transformation before node creation

- `src/antrobot_ros/launch/odom_monitor.launch.py`
- `src/antrobot_ros/launch/rdrive.launch.py`

`odom_monitor` builds an inline parameter dictionary from multiple
`LaunchConfiguration` values.

`rdrive` loads YAML, casts values with `float`, `int`, and `bool`, supplies fallback
values, and constructs a new parameter dictionary before creating the node.

#### 6. Remappings derived from YAML values

- `src/antrobot_ros/launch/kinematic_icp.launch.py`
- `src/antrobot_ros/launch/kiss_icp.launch.py`

`kinematic_icp` loads one parameter section and extracts one remapping target.

`kiss_icp` loads two different YAML sections, extracts values from both, and
creates two remappings for one node.

#### 7. Multiple nodes and computed arguments

- `src/antrobot_ros/launch/cartographer.launch.py`

This launch file loads one YAML section, computes a configuration path, creates
two nodes, supplies separate inline parameter dictionaries, and derives
command-line arguments with `os.path.dirname`, `os.path.basename`, and `str`.

#### 8. Multiple nodes sharing configuration with overrides

- `src/antrobot_ros/launch/explore_lite.launch.py`

This file creates two nodes from different packages. Both receive the same YAML
file, while one node also receives an inline `LaunchConfiguration` override. It
also uses `emulate_tty`.

#### 9. External file processing and runtime parameter construction

- `src/antrobot_ros/launch/robot_state.launch.py`

This file locates and processes a Xacro file, converts it to URDF XML,
conditionally modifies the frame prefix, constructs a new parameter dictionary,
and then creates the robot-state-publisher node.

#### 10. Composition and orchestration stress test

- `src/antrobot_ros/launch/antrobot.launch.py`

This is primarily a launch-file orchestrator rather than a node launch file. It
declares many launch arguments, conditionally includes many child launch files,
and forwards arguments to those files. It is long but structurally repetitive.

#### 11. Dynamic, configuration-driven node generation

- `src/antrobot_ros/launch/tf_static_link.launch.py`

This is a node factory. It reads a `transforms` list from YAML and creates one
`static_transform_publisher` node for every transform. The number of nodes is not
known until launch execution. It also conditionally returns either the namespace
argument plus the generated nodes or an empty `LaunchDescription`.

### Generation experiment with the evolving generator

The first pass kept `launch_template_generator.py`, `generator_launch_file.py.j2`,
and the component macros fixed. A subsequent extension added structured node
command-line arguments and reusable package-relative paths so the two RViz cases
could also be represented without arbitrary Python in JSON. Optional
`emulate_tty` and `prefix` node properties were then added for interactive
processes such as keyboard teleoperation.

The current schema can represent 9 / 17 non-empty launch files without inserting
arbitrary Python expressions into JSON:

| Category | Launch file | Result | Reason |
|---|---|---|---|
| Simple named node | `joint_state_estimator.launch.py` | Generated | One launch argument, one parameter loader, and one node |
| Simple named node | `rplidar.launch.py` | Generated | One launch argument, one parameter loader, and one node |
| Node command-line arguments | `rviz.launch.py` | Generated | One computed config path and one node with `['-d', rviz_config_file]` |
| Node command-line arguments | `kiss_icp_rviz.launch.py` | Generated | One computed config path and one node with `['-d', rviz_config_file]` |
| Interactive inline node | `keyboard_teleoperation.launch.py` | Generated | Raw YAML path plus `emulate_tty=True` and `prefix='xterm -e'` |
| Inline node | `laserscan_to_pointcloud.launch.py` | Generated | The generated node is assigned to a variable instead of being inline, but the ROS actions and values are equivalent |
| YAML-derived remapping | `kinematic_icp.launch.py` | Generated | One loader, one extraction, and one remapping |
| YAML-derived remapping | `kiss_icp.launch.py` | Generated | Two loaders, two extractions, and two remappings |
| Include orchestrator | `antrobot.launch.py` | Generated | Twelve launch arguments and eleven conditional launch includes |

JSON descriptions used:

- `launch_templates/joint_state_estimator.json`
- `launch_templates/rplidar.json`
- `launch_templates/rviz.json`
- `launch_templates/kiss_icp_rviz.json`
- `launch_templates/keyboard_teleoperation.json`
- `launch_templates/laserscan_to_pointcloud.json`
- `launch_templates/kinematic_icp.json`
- `launch_templates/kiss_icp.json`
- `launch_templates/antrobot.json`

Generated artifacts:

- `artifacts/launch_files/joint_state_estimator.launch.py`
- `artifacts/launch_files/rplidar.launch.py`
- `artifacts/launch_files/rviz.launch.py`
- `artifacts/launch_files/kiss_icp_rviz.launch.py`
- `artifacts/launch_files/keyboard_teleoperation.launch.py`
- `artifacts/launch_files/laserscan_to_pointcloud.launch.py`
- `artifacts/launch_files/kinematic_icp.launch.py`
- `artifacts/launch_files/kiss_icp.launch.py`
- `artifacts/launch_files/antrobot.launch.py`

All nine JSON files passed JSON parsing and generator validation. All nine generated
Python files passed `py_compile`.

A static comparison found matching ROS action counts:

| Launch file | Original actions | Generated actions |
|---|---:|---:|
| `joint_state_estimator.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `rplidar.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `rviz.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `kiss_icp_rviz.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `keyboard_teleoperation.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `laserscan_to_pointcloud.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `kinematic_icp.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `kiss_icp.launch.py` | 1 argument, 1 node | 1 argument, 1 node |
| `antrobot.launch.py` | 12 arguments, 11 includes | 12 arguments, 11 includes |

The generated files are not textually identical. Differences include formatting,
unused generic imports, multiline parameter loading, named versus inline nodes,
and reordered Python object construction in `antrobot.launch.py`. Static
inspection shows the same relevant packages, executables, node names,
namespaces, parameter sections, remapping targets, conditions, forwarded
arguments, and `LaunchDescription` actions.

This establishes expected behavioral equivalence, but not a full runtime proof.
Launching the files was intentionally not used as validation because several
files start hardware drivers or the complete robot stack.

### Current result for every category

| Category | Files | Current generator result |
|---|---|---|
| Empty placeholder | `map_converter_explore.launch.py` | Not meaningful to generate: the source file has no launch description |
| Simple named node | `joint_state_estimator.launch.py`, `rplidar.launch.py` | Both generated |
| Node command-line arguments | `rviz.launch.py`, `kiss_icp_rviz.launch.py` | Both generated using structured node arguments and reusable computed package paths |
| Inline node | `keyboard_teleoperation.launch.py` | Generated as an equivalent named node using a raw YAML path, `emulate_tty`, and `prefix` |
| Inline node | `laserscan_to_pointcloud.launch.py` | Generated as an equivalent named node |
| Substitution configuration | `odom_eval.launch.py` | Not representable: requires mixed parameter sources, inline overrides, and `emulate_tty` |
| Include wrapper | `nav2.launch.py` | Not representable: requires an unconditional include and a substitution-based include source |
| Parameter transformation | `odom_monitor.launch.py` | Not representable: requires an inline dictionary of `LaunchConfiguration` values |
| Parameter transformation | `rdrive.launch.py` | Not representable: requires casts, fallback values, and construction of a new parameter dictionary |
| YAML-derived remapping | `kinematic_icp.launch.py`, `kiss_icp.launch.py` | Both generated |
| Computed arguments and multiple nodes | `cartographer.launch.py` | Not representable: requires computed paths, node arguments, inline parameter dictionaries, and two differently configured nodes |
| Shared configuration with overrides | `explore_lite.launch.py` | Not representable: requires a raw YAML path, a per-node inline override, and `emulate_tty` |
| External preprocessing | `robot_state.launch.py` | Not representable: requires Xacro processing, conditional mutation, and a computed parameter dictionary |
| Include orchestrator | `antrobot.launch.py` | Generated |
| Dynamic node factory | `tf_static_link.launch.py` | Not representable: requires a YAML-driven loop, conditional control flow, and a variable-size node list |

Result: **9 of the 17 non-empty launch files** can be represented cleanly by the
current JSON schema and macros. The zero-byte placeholder is excluded from the
denominator.

### Concepts not yet covered by the generator

- Mixed parameter sources: YAML files, dictionaries, and substitutions
- Inline parameter dictionaries
- Parameter casting, fallback values, and transformations
- Multiple parameter sections or files for one node
- Computed paths and intermediate Python variables
- External preprocessing such as Xacro
- Inline versus named launch actions
- Dynamically generated node collections
- Python control flow and conditional returns

The JSON-and-macro approach is suitable for declarative launch files. Files such
as `robot_state.launch.py` and `tf_static_link.launch.py` may require specialized
components or a controlled custom-Python escape hatch instead of representing
arbitrary Python behavior entirely in JSON.
