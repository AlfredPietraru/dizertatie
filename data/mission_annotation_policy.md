# AntRobot mission and parameter-reasoning annotation policy v2

This policy defines gold outcomes independently of any model prompt. Annotators
must apply the decision order below and record the requested configuration only
for `valid` missions.

## Decision order

1. Label **unsupported** when the mission explicitly requires a capability,
   algorithm, sensor choice, or configuration concept that is absent from the
   extracted AntRobot system model, or a value that violates a known deterministic
   constraint.
2. Otherwise label **needs_clarification** when two or more materially different
   supported configurations satisfy the explicit request and selecting one
   requires guessing the user's intent.
3. Otherwise label **no_change** at the parameter stage when the request is a
   question, a capability-only choice, or an explicit instruction to preserve
   existing parameter values.
4. Otherwise label **valid**. A valid mission contains enough information to
   apply every explicitly requested change. Unmentioned options remain unchanged
   (`null`) and do not create ambiguity.

## Valid sparse requests

`Use KISS-ICP` is valid and changes only odometry. Mapping, navigation, and
exploration remain unspecified. `Disable navigation` is valid and changes only
navigation. The annotator must not require a complete deployment description.

Deployment words and common equivalents are interpreted as follows:

- mapping, SLAM, or building a map → `mapping=true`;
- no/disable mapping or no SLAM → `mapping=false`;
- Nav2 or navigation enabled → `navigation=true`;
- no/without/disable navigation → `navigation=false`;
- autonomous frontier exploration → `exploration=true`;
- no/disable exploration → `exploration=false`.

Mentioning an exploration parameter such as planner frequency or map publication
rate does not by itself enable exploration.

The wildcard `/**` profile used by Explore Lite is one physical configuration
slot affecting both `explore` and `explore_lite_map_converter`. Gold annotations
therefore use one key such as `nodes.explore.publish_rate`, with the affected
components recorded in the configuration model; they must not invent two
independently writable values for the same YAML assignment.

An explicit wheel-radius request is valid. The value is converted to metres and
applied consistently to both `nodes.rdrive_node.wheel_radius` and
`nodes.joint_state_estimator.wheel_radius` because both parameters describe the
same physical wheels.

An explicit exclusion applies only to its grammatical target. In `mapping only`,
`only` excludes navigation under this benchmark policy. In `use only KISS-ICP`,
it selects the odometry method and does not disable unrelated capabilities.

## Unsupported requests

Unsupported examples include visual odometry, stereo-camera mapping, object
detection, person following, and non-positive runtime frequencies. Do not
approximate an unsupported request with a supported capability. Unsupported
outcomes are terminal and contain no configuration.

## Needs clarification

Clarification is required for qualitative values without a target (for example,
`make the planner faster`), subjective alternatives (`use the better odometry`),
unresolved choices (`mapping or navigation, whichever is appropriate`), and
conflicting requirements that cannot both be applied. The clarification question
must identify the missing choice or value. Clarification outcomes are terminal
and contain no partial configuration.

## Numeric interpretation

Frequency is measured in hertz. `N times per second` means `N Hz`; `once every N
seconds` means `1/N Hz`. Joint-state publication maps to `publish_frequency`.
Exploration map/update publication maps to `publish_rate`; exploration planning
maps to `planner_frequency`. Wheel radius is measured in metres, so a request for
`4 cm` maps to `0.04` for every related wheel-radius parameter.

## Parameter-selection and value gold

Parameter tasks annotate two independently measurable outputs:

1. the exact set of relevant `parameter_id` values;
2. the expected value for each selected ID, with an absolute numeric tolerance.

Do not include a parameter merely because it is graph-adjacent. Include every
occurrence required by an explicitly shared physical property or publisher/
subscriber wiring change. A value-stage proposal may not introduce an ID absent
from the selection gold.

The draft tasks in `parameter_reasoning_tasks_v1.jsonl` use
`pending_human_review`. They may be used for development, but dissertation
results must use only tasks marked `human_verified`. Review must check the
source-derived ID, current value, unit conversion, expected dependencies,
outcome, and clarification question independently of model output.
