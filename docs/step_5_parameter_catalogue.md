# Step 5 — parameter catalogue and grounded evidence

Step 5 prepares the complete configuration knowledge available to retrieval and parameter reasoning.

It answers:

> What writable configuration slots exist, what deterministic evidence is known about each one, and what optional semantic hypotheses have been added?

## No permission classification

Every extracted physical configuration slot is included. Step 5 does not classify values as `configurable`, `fixed`, `derived`, `approved`, or `rejected`.

Hardware geometry, algorithm settings, launch switches, topic names, and frame names are all candidates when the deterministic schema contains a writable slot for them. Semantic categories may describe a value, but they do not authorize or forbid changes.

## Prepared inputs

Several Step 5 inputs are produced before any mission:

- the frozen integrated ROS system model;
- the schema-version-3.0 physical configuration-slot model;
- the hash-bound parameter-evidence artifact;
- optional versioned LLM semantic enrichment.

For the current mission, Step 5 also receives:

- the `SystemRealization` from Step 3;
- the `ROSOrchestrationPlan` from Step 4.

## Physical configuration slots

The authoritative configuration inventory is:

```text
artifacts/template_configuration/template_configuration_schema.json
```

Its `configuration_keys` field contains the complete set accepted by deterministic validation and represented in the templates/baseline.

Current counts are:

| Item | Count |
|---|---:|
| Launch-argument slots | 16 |
| Physical ROS-parameter slots | 93 |
| Total writable slots | 109 |
| Effective ROS-parameter occurrences | 114 |
| Interface-related slots | 22 |

There are more occurrences than slots because one wildcard YAML target may configure several deployed components. Such a location is represented once with all `affected_components` rather than exposed as contradictory aliases.

## Deterministic evidence packages

The frozen evidence artifact is:

```text
artifacts/semantic_parameters/evidence.json
```

For each parameter slot, it can provide:

- parameter ID, source name, kind, type, default, and current value;
- canonical component and every affected component;
- bounded declaration excerpts;
- parameter reads and usage excerpts;
- related publishers/subscribers or other ROS interfaces;
- source provenance;
- extracted source relationships;
- related parameter IDs.

Every excerpt and interface fact has an evidence ID. Later model outputs may cite only IDs supplied in their bounded context.

### Why evidence is frozen and hash-bound

The evidence artifact records SHA-256 hashes of both the system model and configuration schema. `MissionApplication` recomputes those hashes at startup.

If the source-derived inputs have changed, startup fails instead of silently giving the LLM evidence for a different system version. If no evidence file is configured, the same deterministic evidence can be built in memory.

## Optional semantic enrichment

An offline LLM can infer semantic hypotheses from the evidence, including:

- description;
- physical quantity;
- unit;
- semantic category;
- behavioral effects;
- likely constraints;
- related parameters;
- confidence.

This artifact is optional and loaded only when requested. It is stored separately because:

- source facts remain authoritative;
- model-generated semantics may be wrong or revised;
- experiments can compare context with and without enrichment;
- model and prompt provenance remains visible.

An enrichment cannot invent parameter IDs, relationship targets, or evidence IDs and pass validation.

The versioned enrichment also records the system-model and configuration-schema hashes from which its evidence was derived. `MissionApplication` recomputes both hashes at startup and rejects stale enrichment before catalogue construction.

The canonical deterministic regeneration command is:

```bash
PYTHONPATH=src python helper_scripts/regenerate_artifacts.py --workspace .
```

It rebuilds the source model, physical-slot schema, templates, baseline, lean manifest, and parameter evidence. It replaces `artifacts/semantic_parameters/`, so a previous optional `enrichment.json` is deliberately removed. If semantic enrichment is needed, run `helper_scripts/enrich_parameters.py` only after this deterministic workflow completes.

## Per-mission catalogue construction

`derive_parameter_catalogue()` creates an `AvailableParameter` for every schema slot. Each record includes:

- `parameter_id`;
- component and `affected_components`;
- kind and semantic display name;
- type and current value;
- discovered allowed values or numeric bounds;
- description, unit, physical quantity, category, effects, and constraints when available;
- deterministic and semantic relationships;
- deterministic evidence;
- semantic confidence when enrichment exists.

The catalogue also includes the active component IDs from the current realization and a collision map for repeated short names such as `wheel_radius` or `max_range`.

Inactive-component parameters remain present. Activity is context, not a visibility gate. This matters when a user explicitly asks to reconfigure an inactive component or when the request itself also enables that component.

## Example: wheel radius

A simplified catalogue record can look like:

```json
{
  "parameter_id": "nodes.rdrive_node.wheel_radius",
  "component_id": "rdrive_node",
  "affected_components": ["rdrive_node"],
  "value_type": "number",
  "current_value": 0.03,
  "description": "Configure wheel radius for rdrive node.",
  "relationships": [
    {
      "kind": "same_parameter_name",
      "target": "nodes.joint_state_estimator.wheel_radius"
    }
  ],
  "evidence": {
    "declarations": [],
    "usages": [],
    "ros_interfaces": []
  }
}
```

The real evidence artifact contains the available bounded source excerpts and interface facts. Optional enrichment can replace the generic fallback description with an evidence-grounded semantic description and unit.

## Relationship sources

The catalogue keeps relationship provenance conceptually separate:

- deterministic `same_parameter_name` links come from the generated schema;
- source relationships come from extraction;
- wiring groups connect topic/frame slots through the manifest;
- optional enrichment adds explicitly labelled `semantic_related_parameter` links.

Graph-aware retrieval can traverse these links, but the LLM still decides whether a related parameter is actually required by the request.

## Output boundary

The result is a complete `ParameterCatalogue`. Step 5 does not send all 109 records directly to an LLM.

Step 6 first runs deterministic retrieval and constructs a bounded context. This avoids an uncontrolled whole-model prompt and creates a measurable retrieval boundary.

## Main implementation locations

| Location | Responsibility |
|---|---|
| `src/ros_config_builder/templating/schema.py` | Physical-slot schema generation |
| `src/ros_config_builder/mission/semantics.py` | Evidence construction, hashes, optional enrichment contracts |
| `src/ros_config_builder/mission/schema.py` — `derive_parameter_catalogue()` | Per-mission catalogue assembly |
| `artifacts/template_configuration/template_configuration_schema.json` | Frozen authoritative configuration inventory |
| `artifacts/semantic_parameters/evidence.json` | Frozen deterministic grounding evidence |
| `helper_scripts/regenerate_artifacts.py` | Canonical deterministic artifact-chain regeneration and invalidation |
| `helper_scripts/enrich_parameters.py` | Optional offline semantic-enrichment generation |

Step 6 retrieves a bounded subset, asks one LLM to select parameters, and asks a second LLM to reason about values.
