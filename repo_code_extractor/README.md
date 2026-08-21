# Repository code extractor (compatibility package)

> Deprecated: the maintained Semester 1 implementation now lives in
> `src/ros_config_builder`. Existing imports remain supported temporarily;
> new code should import `ros_config_builder`.

`repo_code_extractor` is an importable Python package for repository ingestion,
AST extraction, configuration detection, value-flow analysis, function
chunking, and optional vector-store ingestion.

## Python API

```python
from repo_code_extractor import extract_code_information

result = extract_code_information(
    "/path/to/workspace",
    source_roots=["src/my_package"],
    output_directory="artifacts/repo_ingestion",
)

print(result.summaries)
print(result.artifacts)
```

Extract all graph-connected evidence and deduplicated source text for one exact
configuration parameter:

```python
from repo_code_extractor import extract_parameter_artifacts

evidence = extract_parameter_artifacts("wheel_radius", repo_root="/path/to/workspace")
print(evidence["llm_context"])
print(evidence["artifact_paths"])
```

Individual stages are available from their modules, while the most common
methods are exported directly from `repo_code_extractor`.

## Python ROS static extraction (Step 1)

The ROS-specific pass is deliberately limited to Python node source. It emits
both a reusable `ModuleIR` (imports, assignments, classes, functions, calls,
returns, and entrypoints) and one local `ROSNodeIR` per class derived from
`rclpy.node.Node` or `LifecycleNode`:

```python
from repo_code_extractor import extract_ros_node_ir, write_ros_node_ir

payload = extract_ros_node_ir("/path/to/workspace", source_roots=["src/my_package"])
artifact = write_ros_node_ir(payload, "/path/to/artifacts/step_1")
```

It recognizes parameters and reads, publishers, subscriptions, services,
clients, timers, QoS profiles, TF entities, publish/service/TF behavior calls,
conditions, loop context, simple expression dependencies, and source locations.
Values that cannot be resolved safely are retained in `unresolved_expressions`.
This pass does not inspect launch files or YAML, resolve namespaces/remappings,
connect nodes to one another, generate templates, or invoke an LLM.

The output contract is versioned as `1.0` and validated by the Pydantic models
in `ros_node_schema.py` before it is returned or written. The extractor also
supports basic ROS actions, local class inheritance with inherited-entity
provenance, ROS calls created in helper methods, detection of module-level ROS
factory functions, and expansion of loops over statically known lists/tuples.
Dynamic loops remain symbolic and are marked with `dynamic_multiplicity`.

## Launch and YAML extraction (Steps 2 and 3)

Launch composition and ROS parameter configuration remain separate from the
Python node model:

```python
from repo_code_extractor import (
    extract_launch_files,
    extract_parameter_yaml,
    write_deployment_artifacts,
)

launch = extract_launch_files("/path/to/workspace", source_roots=["src"])
configuration = extract_parameter_yaml("/path/to/workspace", source_roots=["src"])
paths = write_deployment_artifacts(launch, configuration, "artifacts/static_ros")
```

The launch pass extracts declarations, launched nodes, includes, substitutions,
conditions, namespaces, remappings, arguments, inline parameters, and parameter
file references. The YAML pass extracts node selectors (including `/**`), nested
ROS parameters, and a dot-flattened parameter view. Both use versioned Pydantic
contracts and retain unresolved expressions. Applying profiles, includes,
namespaces, and remappings to `ROSNodeIR` is intentionally deferred to the
integration/system-model stage.

## Deployment integration and ROS system model (Step 4)

Step 4 builds a separate deployment model and never mutates the Step 1–3
payloads. Package metadata statically maps `console_scripts` executables to
Python modules. A caller supplies one or more root launch files:

```python
from repo_code_extractor import build_system_model, extract_package_metadata

packages = extract_package_metadata(workspace, source_roots=["src"])
model = build_system_model(
    workspace=workspace,
    step1=python_ir,
    launch=launch_ir,
    configuration=config_ir,
    package_metadata=packages,
    root_launch_files=["src/antrobot_ros/launch/antrobot.launch.py"],
    root_arguments={"namespace": "robot1"},
)
```

The result contains the include and launch-argument graphs, local and external
deployment instances, ordered parameter provenance, effective names and
namespaces, remapping transformations, deployed interfaces, confirmed local
communication edges, and unresolved deployment facts. External instances are
retained with unknown interfaces; no external behavior is guessed.

Before using the model for templates, generate its acceptance artifacts:

```python
from repo_code_extractor import validate_system_model, write_system_model_artifacts

validation = validate_system_model(model)
artifacts = write_system_model_artifacts(model, "artifacts/ros_system_model")
```

This writes the validated JSON model, a machine-readable validation result, a
Markdown robotics inventory, and a Graphviz DOT topology. Checks cover model
cross-references, local source evidence, external-interface honesty, parameter
provenance, duplicate identities, and confirmed edge integrity.

## Template configuration model (Step 5)

The thin Step 5 bridge inventories reachable launch arguments and effective ROS
parameters, then classifies each as configurable, fixed, derived, or not
relevant. It retains types, defaults, current values, provenance, and render
targets. No capability or mission semantics are inferred:

```python
from repo_code_extractor import (
    build_template_configuration_schema,
    write_template_configuration_schema,
)

schema = build_template_configuration_schema(model, overrides={
    "nodes.rdrive_node.wheel_radius": {
        "classification": "fixed",
        "group": "internal",
        "classification_reason": "fixed for this physical robot",
        "review_required": False,
    }
})
write_template_configuration_schema(schema, "artifacts/template_configuration")
```

Automatic lexical classifications remain explicitly marked for review. Manual
decisions can address either a stable variable ID or its readable template key,
and are recorded as `decision_source: manual_override`.

The checked-in curation policy at `policies/template_configuration_policy.yaml`
groups candidates into deployment, hardware, runtime, wiring, algorithm, and
internal roles. It also separates generic configurability from the physical
AntRobot profile. `curate_template_configuration_schema()` refuses to freeze a
policy with any remaining `proposed` decisions when `require_complete` is true.

## Template definition (Step 6)

Step 6 converts the frozen interface into a non-executable template bundle:

```python
from repo_code_extractor import build_template_definition, write_template_definition

manifest, templates, platform, baseline = build_template_definition(
    frozen_schema,
    configuration_ir,
)
write_template_definition(
    manifest, templates, platform, baseline, "configuration_templates"
)
```

The manifest assigns every public input a handling strategy: root launch
argument, generated YAML, validated source default, derived binding, or platform
profile. Child-only launch arguments are not incorrectly passed to the root
wrapper. Generated YAML declares a package-overlay install target for the Step 7
renderer. All derived wiring uses constant/reference-ready records rather than
arbitrary Python expressions. Coverage validation requires all 93 public inputs,
all 30 derived targets, and all eight platform-fixed values to be accounted for,
and compiles every template with `StrictUndefined`.

## Rendering and semantic validation (Step 7)

The renderer validates sparse public input, resolves baseline/profile/user/
derived/platform stages, renders with `StrictUndefined`, creates a disposable
package overlay, and re-runs Steps 1–4 in an isolated copied analysis workspace:

```python
from repo_code_extractor import render_configuration_bundle

result = render_configuration_bundle(
    workspace=workspace,
    template_directory="configuration_templates",
    schema=frozen_schema,
    reference_model=accepted_system_model,
    output_directory="artifacts/generated/baseline",
    profile={"profile": "baseline", "values": {}},
    user_values={},
)
```

Unknown, derived, rejected, platform-fixed, wrongly typed, and inaccessible
source-default overrides are rejected before rendering. The bundle records its
resolved context and provenance, variables consumed per template, file hashes,
static validation, generated system model, semantic comparison, and expected
structural/provenance differences. Equivalence compares normalized deployment
instances, parameters, ROS endpoints/remappings, and confirmed local edges—not
rendered text or source locations.

## Semester 1 demonstration profiles

The frozen profiles `mapping`, `mapping_navigation`, and `sensor_odometry` live
under `configuration_templates/profiles/`. Each has a checked-in expected delta.
`validate_scenario_delta()` requires the exact declared public-input and
enabled/disabled executable changes, no undeclared parameter changes, stable
derived wiring and platform values, and preservation of required local edges.
The Nav2 source remains external, so its mapping/navigation distinction is
validated at the public configuration boundary and reported as a known
observability limitation rather than inferred as local nodes.

## Command line

Run the complete pipeline without installing the package:

```bash
python3 -m repo_code_extractor /path/to/workspace \
  --source-root src/my_package \
  --output artifacts/repo_ingestion
```

Use `--no-chunks` or `--no-static-analysis` to skip those optional stages.

The parameter evidence CLI remains available as a package module:

```bash
python3 -m repo_code_extractor.parameter_graph_tools \
  --repo-root /path/to/workspace \
  context wheel_radius
```

Parameter-specific results are saved by default under
`artifacts/code_parameters/<parameter_name>/context.md` and `result.json`.

Vector ingestion is intentionally separate. Importing the package does not
require ChromaDB, Hugging Face Hub, or python-dotenv; those dependencies are
checked only when `repo_code_extractor.vector_store.ingest_chunks()` is called.
