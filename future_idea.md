### Have this classification be done by an LLM -> human provided knowledge now (ofuleting)
No. In the current architecture, the capability registry is human-authored robot-design knowledge.

It was written based on understanding:

- which capabilities AntRobot supports;
- which implementations are available;
- which components belong to each implementation;
- which capabilities depend on others;
- which launch values enable or disable them;
- which ROS connections are required.

The LLM does not generate the registry during application execution.

The division is:

```text
Programmer / robot designer
    → capability_registry.yaml

Deterministic source analysis
    → ROS system model
    → configuration schema
    → parameter evidence

LLM at runtime
    → interprets which registered capabilities the user requested
    → selects relevant parameters
    → reasons about new values
```

The registry is subsequently checked deterministically against generated artifacts using `validate_capability_registry()`. That catches references to unknown components, configuration keys, implementations, or wiring bindings.

An LLM may have assisted during development—for example, by suggesting registry contents—but that would be authoring assistance, not part of the implemented pipeline. The final registry remains manually curated and must be treated as human-provided knowledge.


