# Configuration analysis pipeline — 5 August 2026

## Summary

The project direction was clarified by reviewing
`DevPlan___Automated_ROS_Infrastructure_Orchestration_via_LLM_Driven_Templating.pdf`.
The main conclusion is that the existing, operational AntRobot YAML files
should be converted into templates rather than replaced by small YAML files
generated from scratch.

The intended pipeline is:

```text
Existing working ROS configuration
    → reusable Jinja template
    → validated mission context
    → complete rendered ROS configuration
    → ROS launch and runtime validation
```

The LLM should produce a small set of mission-level decisions. It should not
be responsible for reconstructing every low-level ROS parameter on every run.
Stable values and verified defaults remain in templates or configuration
profiles.

Work also began on a deterministic source-analysis pipeline. Three stages are
now implemented:

1. Python module and import indexing;
2. scoped AST extraction;
3. configuration-source detection and immediate binding extraction.

Generated artifacts are stored in `artifacts/repo_ingestion/`.

## Interpretation of the development plan

The development plan requires converting AntRobot's real YAML files for SLAM,
localization, navigation, and perception into reusable Jinja templates. The
rendered results are expected to remain complete and launchable.

A useful baseline requirement was identified:
ction;
3. configuration-source detection and immediate binding extraction.

> Rendering a template with its default context should produce a configuration
> that is semantically equivalent to the original working YAML.

File length and textual equality are not the primary validation criteria.
Validation should instead check:

- preservation of required ROS namespaces and parameters;
- preservation of stable defaults;
- correct mission-specific changes;
- valid YAML and ROS parameter types;
- successful node and launch startup;
- successful mission behavior in simulation and, later, on hardware.

The plan also includes launch configuration templating. This does not
necessarily mean templating every launch file. Launch files should be made
dynamic where missions change node selection, sensors, namespaces, included
subsystems, parameter files, remappings, or launch arguments. Completely static
launch files may remain unchanged.

## Parameterization policy

Mechanically replacing every YAML scalar with a Jinja expression would be easy,
but it would expose too many implementation details and would not create a
useful mission interface.

Parameters were divided conceptually into these groups:

- **Structural choices:** select nodes, plugins, sensors, or operating modes.
- **Mission-tunable values:** meaningfully change the requested mission.
- **Environment-tunable values:** depend on terrain, map size, lighting, or
  similar operational conditions.
- **Hardware-specific values:** should normally come from a robot profile.
- **Runtime-derived values:** topic names, frame names, and files that may need
  validation against the deployed system.
- **Internal constants:** remain fixed in the template.
- **Safety-critical values:** require strict bounds and stronger review.
- **Unknown values:** remain unexposed until evidence is available.

The main engineering task is therefore not simply YAML-to-Jinja conversion. It
is deriving a small, meaningful, and validated configuration abstraction from
the complete operational configuration.

## Configuration schema

A schema was discussed as the contract between the LLM and the deterministic
templating system. It should describe:

- permitted context fields;
- field types;
- required and optional fields;
- numeric bounds;
- finite choices;
- rejection of unknown fields;
- conditional and cross-field requirements.

Examples include requiring a map file for localization, a scan topic when LiDAR
is selected, or plugin-specific parameter groups when a particular controller
is selected.

JSON Schema is one possible mission-facing representation. ROS parameter
descriptors and `generate_parameter_library` are also relevant because they can
represent ROS-native types, defaults, descriptions, bounds, enumerations, and
custom validators.

An important distinction was established:

```text
hard validity constraint
    is not the same as
recommended tuning range
    is not the same as
current AntRobot default
```

These forms of evidence must remain separate rather than being merged into one
invented minimum and maximum.

## Proposed evidence-discovery pipeline

An LLM-assisted pipeline was discussed for building a parameter catalog:

```text
Existing YAML files
    → deterministic parameter inventory
    → source-code, documentation, and runtime evidence
    → LLM-assisted extraction and classification
    → conflict and uncertainty report
    → reviewed parameter catalog
    → mission schema and templates
    → static, simulation, and hardware validation
```

The LLM may help extract types, defaults, ranges, choices, dependencies, and
descriptions, but it should not be treated as the authority. Findings should
include their source, confidence, and unresolved questions. The model must be
allowed to report that no authoritative constraint was found.

Relevant research areas identified for further reading include:

- configuration-option extraction from source code;
- configuration-constraint extraction from documentation;
- configurable systems and software product-line variability;
- ROS parameter tuning and runtime reconfiguration;
- LLM-based ROS orchestration;
- infrastructure-as-code generation and verification;
- schema-constrained structured generation;
- evidence-grounded retrieval and information extraction.

Representative works discussed included *Static Extraction of Program
Configuration Options*, CarpetFuzz, ROS-LLM, JSONSchemaBench-related structured
output research, and recent verifier-guided infrastructure-as-code generation
work.

## Repository-analysis scope

Only explicitly selected, workspace-local source trees are analyzed:

- `src/antrobot_ros`;
- `src/antrobot_description`;
- `src/kiss-icp`;
- `src/kinematic-icp`.

The tools do not follow imports into installed libraries. Build outputs,
installation outputs, logs, caches, virtual environments, and `site-packages`
are excluded. Nav2 imports can be observed, but Nav2 implementation code is not
ingested because its source is not checked into the workspace.

## Stage 1: Module and import indexing

`00_repo_ingestion.py` creates a shallow inventory of Python files. For every
module it records:

- the selected package root;
- repository-relative file path;
- dotted module label;
- imports and aliases;
- top-level functions and classes;
- source locations.

The generated files are:

- `artifacts/repo_ingestion/module_index.json`;
- `artifacts/repo_ingestion/module_index.md`.

Current results:

- 86 Python modules;
- 465 import statements;
- 115 top-level definitions;
- zero parse failures.

## Stage 2: Scoped AST extraction

`01_repo_ast_extraction.py` expands each module into lexical scopes. A class is
separated from its methods, and a module-level function remains separate from
the class.

For example:

```text
keyboard_teleoperation_node
├── module scope
├── KeyboardTeleoperation
│   ├── __init__
│   ├── read_keys
│   ├── __get_key__
│   ├── process_key
│   └── other methods
└── main
```

Each scope records:

- definitions and signatures;
- assignments;
- calls;
- conditions;
- returns;
- line and column ranges;
- relevant source expressions.

Facts inside a method are not duplicated into the surrounding class or module.
The generated files are:

- `artifacts/repo_ingestion/ast_extraction.json`;
- `artifacts/repo_ingestion/ast_extraction.md`.

Current results:

- 488 scopes;
- 43 classes;
- 274 methods;
- 85 functions;
- 1,568 assignments;
- 2,864 calls;
- 367 conditions;
- 263 returns;
- zero parse failures.

The standard Python AST currently provides source positions and source
segments. `asttokens` was not installed and was not required for this stage.
It may be useful later if exact token-level formatting or comments need to be
retained.

## Stage 3: Configuration-source detection

`02_repo_config_detection.py` labels known configuration operations in the
previously indexed files and records their immediate destinations.

For example:

```python
self.max_linear_velocity = self.get_parameter(
    "max_linear_velocity"
).value
```

is represented conceptually as:

```text
ROS parameter max_linear_velocity
    → read in KeyboardTeleoperation.__init__
    → assigned to self.max_linear_velocity
```

The detector currently supports:

- ROS parameter declaration, reading, writing, removal, and callbacks;
- `os.getenv`, `os.environ.get`, and `os.environ[...]`;
- `argparse` parser creation, argument declaration, and parsing;
- `configparser` construction, file loading, and typed value access;
- Pydantic `BaseSettings` classes and fields;
- ROS `DeclareLaunchArgument` and `LaunchConfiguration`;
- dictionaries passed to `launch_ros.actions.Node(parameters=...)`;
- launch arguments passed as node parameters;
- YAML loading calls;
- first-pass candidates for project-specific configuration wrappers.

The generated files are:

- `artifacts/repo_ingestion/configuration_sources.json`;
- `artifacts/repo_ingestion/configuration_sources.md`.

Current results:

- 339 total configuration records;
- 122 ROS parameter operations;
- 130 ROS launch-argument declarations or reads;
- 60 node parameter bindings;
- 11 configuration dictionaries;
- 5 `argparse` records;
- one environment-variable read;
- one Pydantic settings class and five fields;
- two YAML loads;
- two project-wrapper candidates;
- zero parse failures.

The wrapper candidates are:

- `antrobot_ros.utils.load_node_params`;
- `python.kiss_icp.config.parser._yaml_source`.

## Important analysis boundary

Configuration-source detection records only where a configuration value enters
the program and where it is immediately stored or passed.

It does not yet follow a value through the entire program. For example, the
current stage can establish:

```text
max_linear_velocity parameter
    → self.max_linear_velocity
```

The future value-flow stage should extend that relationship:

```text
max_linear_velocity parameter
    → self.max_linear_velocity
    → velocity calculation
    → Twist.linear.x
    → publisher
```

ROS publishers, subscribers, services, actions, and timers should be treated as
architectural consumers, not as configuration declarations. Configuration,
program flow, and ROS architecture should remain separate graph domains and be
connected with explicit relationships.

## Reproduction commands

The implemented stages can be regenerated in order with:

```bash
python3 00_repo_ingestion.py
python3 01_repo_ast_extraction.py
python3 02_repo_config_detection.py
python3 03_repo_value_flow.py
```

## Next planned stage

The next major step is a lightweight value-flow and call analysis. It should
connect configuration sources to assignments, function arguments, returned
values, conditions, validators, and eventual ROS consumers.

The resulting relationships are expected to include:

```text
assigned_to
passed_to
returned_from
validated_by
controls
```

NetworkX can then represent these relations as a dependency graph. LLM-facing
tools can query small, deterministic graph slices such as parameter sources,
forward uses, validations, consumers, and related parameters. The LLM should
interpret those facts rather than scan or invent facts about the entire codebase
directly.
