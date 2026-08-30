# Step 7 — deterministic validation and plan construction

Step 7 converts validated AI proposals and capability choices into one sparse renderer plan.

It answers:

> Are these exact IDs and values valid for the frozen physical configuration interface, and can they be merged without contradiction?

No LLM call occurs here.

## Inputs

Step 7 consumes:

- capability-driven renderer values from `SystemRealization`;
- parameter values validated by `ParameterReasoner`;
- the schema-version-3.0 physical configuration model.

The lean renderer manifest is passed through some compatibility signatures, but it is no longer a per-key authorization or target map. The authoritative key/type interface is the configuration schema, and the generated Jinja templates contain the actual key references.

## Parameter validation

The proposal is checked against the complete catalogue and schema. Deterministic checks cover:

- identifier exists;
- identifier is part of the generated physical interface;
- no duplicate ID appears;
- Python/JSON value type matches the discovered type;
- boolean values are not accepted accidentally as integers;
- value belongs to any discovered allowed-value set;
- numeric value respects any discovered minimum and maximum.

Earlier selection/value checks have already enforced:

- the selected ID was in the bounded LLM context;
- evidence IDs were not invented;
- value reasoning did not introduce an unselected ID;
- `old_value` matched the frozen current value.

Step 7 rechecks the renderer-facing facts at the final boundary.

## Merge order

`build_template_configuration_plan()` begins with Step 3 capability values and then adds Step 6 parameter changes:

```text
capability renderer values
          +
validated parameter changes
          |
          v
TemplateConfigurationPlan.user_values
```

The result remains sparse. It contains only mission-specific overrides; the complete baseline is merged in Step 8.

## Conflict rule

If both capability realization and parameter reasoning target the same key, their values must agree.

For example, if the mission selected KISS-ICP, Step 3 may require:

```text
launch.launch_kiss_icp = true
```

If parameter reasoning independently proposed `false`, Step 7 rejects the mission instead of allowing one stage to silently overwrite the other.

This conflict check preserves the distinction between:

- deployment intent resolved from capabilities;
- lower-level tuning requested in natural language.

## Running example

Mission:

> Map using KISS-ICP and limit its range to 20 metres.

The sparse plan is approximately:

```json
{
  "user_values": {
    "launch.launch_cartographer": true,
    "launch.launch_kinematic_icp": false,
    "launch.launch_kiss_icp": true,
    "launch.launch_laserscan_to_pointcloud": true,
    "nodes.kiss_icp.max_range": 20.0
  }
}
```

No unrelated baseline values are copied into this plan.

## Why there is no policy approval check

Catalogue/schema membership means:

> This is a physical value discovered in the analyzed system and represented by the generated renderer interface.

It does not mean a human committee approved that semantic category. Wheel geometry, topic names, frame names, algorithm settings, and launch arguments are validated by the same objective rules.

Appropriateness is the AI reasoning problem. Existence, type, range, freshness, and renderability are deterministic checks.

## Output

`TemplateConfigurationPlan` contains:

```python
user_values: dict[str, RendererValue]
```

It is saved in `mission_result.json` and handed to Step 8 as `user_values`.

## Failure conditions

Step 7 rejects:

- unknown or non-renderable keys;
- duplicate changes;
- wrong types;
- disallowed values;
- values outside discovered bounds;
- conflicts between capability and parameter values;
- incomplete or invalid plan data.

The application does not render a partial plan after such a failure.

## Main implementation locations

| Location | Responsibility |
|---|---|
| `src/ros_config_builder/mission/schema.py` — `validate_parameter_changes()` | Catalogue-facing ID/type/range validation |
| `src/ros_config_builder/mission/reasoning.py` — `validate_parameter_values()` | Selection membership and stale-old-value checks |
| `src/ros_config_builder/mission/schema.py` — `build_template_configuration_plan()` | Sparse merge and conflict detection |
| `src/ros_config_builder/mission/schema.py` — `validate_template_plan()` | Final schema-facing validation |
| `src/ros_config_builder/templating/renderer.py` — `validate_configuration_values()` | Shared renderer-interface checks |

Step 8 overlays this sparse plan on the complete generated baseline and renders files.
