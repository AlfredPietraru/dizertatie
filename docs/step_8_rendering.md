# Step 8 — deterministic rendering

Step 8 applies the sparse mission plan to the generated baseline and writes ROS 2 launch/YAML files.

The LLM never generates Python launch source, YAML structure, Jinja expressions, output paths, or installation targets.

## Inputs

Step 8 consumes:

- the schema-version-3.0 physical configuration model;
- `configuration_templates/manifest.json`;
- the complete baseline profile named by the manifest;
- generated Jinja templates;
- sparse `TemplateConfigurationPlan.user_values` from Step 7;
- the frozen ROS system model only when offline equivalence analysis is requested.

## The lean renderer manifest

The manifest is operational. Its current top-level structure is:

```json
{
  "schema_version": "3.0",
  "templates": [],
  "wiring_bindings": {},
  "baseline_profile": "profiles/baseline.yaml"
}
```

Step 8 uses:

- `schema_version` to reject incompatible definitions;
- `templates` to locate Jinja sources and determine output/deployment routes;
- `baseline_profile` to load all default values.

`wiring_bindings` are consumed earlier by orchestration and graph retrieval; rendering preserves them in the same manifest because they are generated alongside the interface templates.

The manifest does not duplicate the full key inventory or a per-key handling map. The schema owns IDs/types/constraints, and every generated Jinja reference identifies the concrete writable location.

## Exact interface coverage

Template-definition validation requires three key sets to be identical:

```text
schema configuration_keys
       = baseline values
       = Jinja values[...] references
```

It also verifies:

- manifest template paths equal generated template paths;
- output routes are unique;
- every interface-related parameter occurs in wiring metadata;
- templates compile with strict undefined-variable handling;
- no unknown Jinja root is used.

The current exact interface has 109 physical slots. A wildcard YAML slot is emitted once, even when it affects multiple deployed components.

## Layer merge

`resolve_render_context()` merges values in this order:

```text
complete generated baseline
          -> optional scenario profile
          -> explicit per-mission user values
```

Later layers override earlier layers. Provenance records every applied stage and value.

Before merging, profile and mission values are validated against the schema. After merging, every schema key must have a resolved value.

The normal mission pipeline passes no scenario profile, so its effective order is simply:

```text
baseline -> mission plan
```

## Rendering

Jinja uses `StrictUndefined`; a missing value cannot silently render as an empty string.

Two output strategies are supported:

| Strategy | Purpose |
|---|---|
| `wrapper_launch` | Generate the root mission launch wrapper |
| `package_overlay` | Generate ROS parameter YAML at its AntRobot package install path |

Serialization filters convert values safely:

- `yaml_scalar` uses JSON-compatible scalar/list/object syntax accepted by YAML;
- `ros_launch_value` converts booleans, arrays/objects, strings, and known package-share substitutions into valid launch expressions.

Every render record contains the template, output path, deployment strategy, referenced keys, and SHA-256 digest of the generated file.

## Static generated-file validation

Every normal mission render performs inexpensive structural validation:

- Python launch files must parse with `ast.parse()`;
- YAML must parse to a top-level mapping;
- each YAML selector must contain a `ros__parameters` mapping.

An invalid generated file raises an error before the result is declared valid.

## Runtime versus offline equivalence

Earlier runtime behavior copied the ROS tree, re-extracted the generated launch system, rebuilt a system model, and compared it with the reference during every mission. That is useful as an offline acceptance test but unnecessarily expensive as a normal rendering step.

The current application calls the renderer with:

```python
require_equivalence=False
debug_contexts=False
```

Normal runtime therefore performs schema and static file validation but does not reanalyze the repository. `validation.json` records that equivalence was skipped deliberately.

Tests and offline diagnostics can use `require_equivalence=True`. In that mode, Step 8:

1. constructs an isolated analysis workspace;
2. installs the generated overlay and wrapper;
3. re-runs deterministic extraction and integration;
4. compares active deployment/interface projections with the frozen reference;
5. writes `generated_system_model.json`.

This keeps expensive assurance available without making it part of every user request.

The canonical deterministic artifact workflow runs that offline scenario-equivalence assurance by default:

```bash
PYTHONPATH=src python helper_scripts/regenerate_artifacts.py --workspace .
```

This does not change the normal runtime setting above. `--skip-scenario-equivalence` may be used for a deliberately faster regeneration, but the resulting `artifacts/ARTIFACTS.json` records that equivalence was not performed.

## Bundle outputs

A render bundle contains generated files plus diagnostics, conceptually:

```text
bundle/
├── launch/
│   └── antrobot_profile.launch.py
├── overlay/
│   └── share/antrobot_ros/config/
│       ├── antrobot_params.yaml
│       └── explore_lite_params.yaml
├── resolved_context.json
├── render_manifest.json
└── validation.json
```

Optional files are:

- `generated_system_model.json` when equivalence was requested;
- `context_stages.json` when debug contexts were requested.

### `resolved_context.json`

Contains every resolved key and its merge provenance.

### `render_manifest.json`

Contains output-file records, variables used, deployment strategy, and output hashes. This is a per-render diagnostic and should not be confused with the static `configuration_templates/manifest.json`.

### `validation.json`

Contains static validation, equivalence status, whether equivalence was required, and final validity.

## ROS launch-system assembly

After Step 8, `MissionApplication` assembles a convenience directory named `ros2_launch_system` by combining:

- the generated mission wrapper;
- generated parameter YAML;
- supporting AntRobot launch and configuration files.

Its local `manifest.json` identifies the entry point and copied files. This assembly manifest is different from both the static renderer manifest and the per-render diagnostic manifest.

The application still does not start ROS. The operator must deploy/build the package overlay and launch the entry point in a suitable ROS 2 environment with external dependencies installed.

## Running example

For a plan containing:

```text
launch.launch_kiss_icp = true
launch.launch_kinematic_icp = false
nodes.kiss_icp.max_range = 20.0
```

Step 8:

1. starts with all 109 baseline slot values;
2. overlays those three mission values;
3. renders the launch wrapper and YAML templates;
4. validates their syntax and ROS parameter shape;
5. records hashes and provenance;
6. assembles the convenience launch system.

Unrelated baseline values remain unchanged.

## Failure conditions

Rendering rejects:

- an unsupported manifest schema version;
- missing baseline or template files;
- unknown keys or wrong value types;
- values outside allowed sets/ranges;
- an incomplete baseline;
- unresolved Jinja variables;
- invalid generated Python or YAML;
- unexpected system-model differences when offline equivalence is required.

## Main implementation locations

| Location | Responsibility |
|---|---|
| `src/ros_config_builder/templating/definition.py` | Template/baseline/manifest generation and exact coverage checks |
| `src/ros_config_builder/templating/renderer.py` | Merge, validation, rendering, optional re-analysis, and diagnostics |
| `configuration_templates/manifest.json` | Static operational renderer definition and wiring metadata |
| `configuration_templates/profiles/baseline.yaml` | Complete baseline values |
| `src/ros_config_builder/orchestrate.py` | YAML configuration loading, operation dispatch, and launch-system assembly |
| `src/ros_config_builder/parameters.yaml` | Complete flat application configuration |
