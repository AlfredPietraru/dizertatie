# Step 6 — retrieval, parameter selection, and value reasoning

Step 6 contains the main per-mission AI configuration work. It deliberately separates:

1. deterministic retrieval and context construction;
2. LLM parameter selection;
3. LLM value reasoning.

This makes retrieval accuracy, selection accuracy, and value accuracy independently measurable.

## Inputs

Step 6 consumes:

- the original mission text;
- the complete evidence-backed `ParameterCatalogue` from Step 5;
- the Step 3 realization and Step 4 orchestration as system context;
- manifest wiring bindings for graph construction;
- `prompts/parameter_selection.txt`;
- `prompts/parameter_value_reasoning.txt`;
- retrieval limits and a selected context variant.

## Stage 6A — deterministic retrieval

The baseline retriever is dependency-free and reproducible. It normalizes and tokenizes the mission, scores catalogue fields with deterministic lexical weights, gives a large bonus to exact identifier/name matches, and sorts ties by stable parameter ID.

The five context variants are:

| Variant | Search/context fields |
|---|---|
| `names_values` | IDs, names, components, types, and current values |
| `semantic` | Adds descriptions, physical quantities, units, categories, effects, and constraints |
| `source` | Adds declaration and usage excerpts |
| `system` | Adds related ROS interfaces |
| `graph` | Adds relationships and bounded neighbor expansion |

Default runtime settings retrieve the top 12 lexical candidates, then the graph variant may expand one hop up to a bounded maximum.

### Graph construction

The in-memory graph connects parameters through:

- deterministic schema relationships;
- optional labelled semantic relationships;
- shared topic/frame `wiring_bindings`.

Graph expansion records the seed/parent and graph distance. A neighbor is additional context, not automatically a selected change.

### Bounded selection context

`build_selection_context()` serializes ranked records under a character budget. It records:

- included parameter IDs;
- omitted candidate count;
- character count and budget;
- evidence and graph edges allowed by the selected variant.

The default budget is 40,000 characters. If even the first record is too large, bulky source/interface fields are removed from that record rather than exceeding the declared budget without notice.

## Stage 6B — LLM parameter selection

The selection prompt receives:

- the bounded retrieval context;
- active realization/orchestration context;
- the generated `ParameterSelectionInterpretation` JSON schema;
- the original mission as user input.

The model returns one of:

| Status | Required behavior |
|---|---|
| `valid` | Return one or more unique selected parameter IDs |
| `no_change` | Return no IDs and explain why no parameter change was requested |
| `unsupported` | Return no IDs and explain why the known configuration cannot express the request |
| `needs_clarification` | Return no IDs, a reason, and one clarification question |

A valid selection record includes:

- `parameter_id`;
- concise relevance justification;
- grounding evidence IDs;
- optional confidence.

### Deterministic selection checks

After parsing, deterministic code requires:

- every selected ID appeared in the supplied bounded context;
- selected IDs are unique;
- every cited evidence ID appeared in that context;
- the status-specific shape is valid.

The model therefore cannot retrieve one record but silently select an unseen parameter from prior knowledge.

## Stage 6C — LLM value reasoning

Only a valid non-empty selection reaches the value model. Its prompt includes:

- the validated selection;
- full context records for the selected IDs;
- graph edges touching the selection;
- the generated `ParameterValueInterpretation` JSON schema;
- the original mission.

A proposed change contains:

```json
{
  "parameter_id": "nodes.kiss_icp.max_range",
  "old_value": 3.5,
  "new_value": 20.0,
  "reason": "The user explicitly limited the sensor range to 20 metres.",
  "request_evidence": "limit its range to 20 metres",
  "grounding_evidence_ids": []
}
```

The output also supports concise `dependencies_considered` and `uncertainties`. These are externally inspectable justifications, not a request for hidden chain-of-thought.

### Deterministic value checks

Before Step 7, code enforces:

- the value stage cannot introduce an ID absent from the validated selection;
- every `old_value` exactly matches the frozen current catalogue value;
- IDs are unique;
- new values satisfy the existing identifier, type, allowed-value, and numeric-bound checks.

A stale old value prevents a proposal derived from an outdated context from being applied.

## Running examples

### Explicit range

Mission:

> Limit KISS-ICP range to 20 metres.

Expected stages:

```text
retrieval -> nodes.kiss_icp.max_range appears near the top
selection -> {nodes.kiss_icp.max_range}
value     -> old value from catalogue, new value 20.0
```

### Physical multi-parameter reasoning

Mission:

> I installed wheels with a diameter of 8 cm.

Expected reasoning:

```text
diameter 8 cm -> radius 4 cm -> 0.04 m
```

The relationship context should expose both independently writable wheel-radius slots:

```text
nodes.rdrive_node.wheel_radius
nodes.joint_state_estimator.wheel_radius
```

The selection stage is evaluated on choosing both IDs; the value stage is evaluated on deriving `0.04` for each.

### Ambiguous request

Mission:

> Make localization much better.

The correct result may be `needs_clarification` because no measurable target or symptom identifies a defensible configuration change. Safe abstention is part of the evaluation.

## Terminal behavior

`no_change`, `unsupported`, and `needs_clarification` stop before plan construction and rendering. Their retrieval and selection records remain in `mission_result.json` for diagnosis.

A value-stage `unsupported` or `needs_clarification` result also stops safely.

## Main implementation locations

| Location | Responsibility |
|---|---|
| `src/ros_config_builder/mission/retrieval.py` | Lexical ranking, graph expansion, and bounded context |
| `src/ros_config_builder/mission/reasoning.py` | Selection/value contracts, prompts, validation, and orchestration |
| `prompts/parameter_selection.txt` | Selection instructions |
| `prompts/parameter_value_reasoning.txt` | Value reasoning instructions |
| `src/ros_config_builder/orchestrate.py` | Backend creation and result recording |
| `src/ros_config_builder/mission/parameter_evaluation.py` | Boundary-specific evaluation metrics |

Step 7 receives only validated parameter-to-value changes from the separated reasoner.
