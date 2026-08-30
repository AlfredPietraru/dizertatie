# Step 2 — capability interpretation

Step 2 uses the first runtime LLM call to answer:

> Which supported high-level robot capabilities did the user ask to enable, disable, or implement in a particular way?

It does not choose individual ROS parameters. Retrieval, parameter selection, and value reasoning happen later.

## Inputs

Step 2 consumes:

- the original mission text;
- `prompts/mission_interpretation.txt`;
- a semantic, LLM-safe projection of the validated capability registry;
- the generated JSON schema for `MissionInterpretation`;
- the configured Ollama backend.

The complete registry also contains components, launch values, and wiring requirements, but Step 2 sees only capability descriptions, supported implementation names, characteristics, and defaults. This prevents the model from manipulating low-level deployment recipes.

## Where the registry comes from

`configuration_templates/capability_registry.yaml` is human-authored robot design knowledge, not an LLM response and not a direct output of static extraction.

It defines concepts that source code alone cannot reliably infer, such as:

- “mapping” and “exploration” are user-facing capabilities;
- Cartographer is the supported mapping implementation;
- exploration requires mapping;
- KISS-ICP is an odometry alternative and needs point-cloud conversion.

At application startup, deterministic validation checks the registry against the frozen ROS system model, physical configuration schema, and manifest wiring bindings. Step 2 therefore consumes a registry whose concrete references have already been checked.

## Prompt construction

The prompt template contains exactly one occurrence of each placeholder:

```text
{{ANTROBOT_CAPABILITY_REGISTRY}}
{{MISSION_INTERPRETATION_JSON_SCHEMA}}
```

`build_interpretation_prompt()` replaces them with:

- `capability_prompt_catalogue(registry)`;
- `MissionInterpretation.model_json_schema()`.

Missing or duplicated placeholders fail before the model is contacted.

## Output contract

The model must return one of three statuses:

| Status | Meaning |
|---|---|
| `valid` | The request has a supported, unambiguous sparse capability selection |
| `unsupported` | An explicitly requested capability or implementation is unavailable |
| `needs_clarification` | More than one materially different supported interpretation remains |

For `valid`, `capabilities` contains sparse selections. Unmentioned capabilities remain `null`; they are not automatically disabled.

Conceptually:

```json
{
  "status": "valid",
  "capabilities": {
    "mapping": {
      "enabled": true,
      "implementation": null,
      "selection_basis": null
    },
    "odometry": {
      "enabled": true,
      "implementation": "kiss_icp",
      "selection_basis": "explicit"
    }
  }
}
```

An enabled capability with no implementation means “use the validated registry default.” An explicitly named implementation requires `selection_basis="explicit"`.

## Running example

Mission:

> Map using KISS-ICP and limit its range to 20 metres.

Step 2 should infer:

```text
mapping enabled
odometry enabled with KISS-ICP explicitly selected
navigation unmentioned
exploration unmentioned
```

The full sentence is preserved. The 20-metre phrase is not discarded; it is handled by later retrieval, parameter selection, and value reasoning.

## Validation boundary

After the backend returns text:

1. the application extracts one JSON object;
2. Pydantic rejects unknown fields and wrong types;
3. status-specific invariants are checked;
4. capability and implementation names are validated;
5. a valid sparse result continues to Step 3.

Schema-valid does not necessarily mean semantically correct. Semantic capability accuracy is measured by the mission evaluation dataset. The strict contract prevents malformed or invented structure from reaching deterministic realization.

## Terminal behavior

`unsupported` and `needs_clarification` are terminal. The application writes `mission_result.json` and does not run realization, orchestration, parameter reasoning, or rendering.

## What Step 2 does not do

Step 2 does not:

- choose concrete components;
- apply launch arguments;
- prove ROS connections;
- retrieve parameter records;
- select parameter IDs;
- derive numeric values;
- write launch or YAML files.

These boundaries keep semantic interpretation separate from exact deployment and configuration operations.

## Main implementation locations

| Location | Responsibility |
|---|---|
| `prompts/mission_interpretation.txt` | Capability-interpretation instructions and examples |
| `src/ros_config_builder/mission/inference.py` | Prompt assembly, backend call, and JSON extraction |
| `src/ros_config_builder/mission/schema.py` | Strict capability and interpretation contracts |
| `configuration_templates/capability_registry.yaml` | Supported capability design knowledge |
| `src/ros_config_builder/orchestrate.py` | Runtime wiring and terminal routing |

Step 3 consumes only the validated capability selections and the already validated registry.
