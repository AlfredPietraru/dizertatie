# ROS Config Builder

ROS Config Builder is an AntRobot research prototype for translating natural-language robot requirements into grounded ROS 2 configuration changes.

The central research question is:

> Does structured source-code, ROS-system, and parameter-relationship evidence improve an LLM's ability to select the correct ROS configuration parameters and derive valid values?

The deterministic extractor, validator, and renderer are supporting infrastructure. The main AI problem is semantic parameter understanding, retrieval, parameter selection, and value reasoning.

## Architecture at a glance

```text
ROS source, launch files, and YAML
                |
                v
deterministic extraction and integration
                |
                +--> frozen ROS system model
                +--> physical configuration-slot schema
                +--> grounded parameter-evidence artifact
                +--> lean templates, baseline, and manifest
                |
                v
optional LLM semantic enrichment
                |
user mission --> capability interpretation and realization
                |
                v
retrieval + bounded graph-aware context
                |
                v
LLM parameter selection
                |
                v
LLM value reasoning
                |
                v
deterministic validation and rendering
```

The current generated configuration surface contains 109 writable physical slots:

- 16 launch arguments;
- 93 ROS parameter slots;
- 114 effective ROS parameter occurrences, because one wildcard YAML assignment can configure more than one component.

Every extracted physical slot is a candidate. There is no manually curated `configurable`/`fixed` permission policy.

## What “frozen” means

“Frozen” is project terminology, not a ROS feature. It means an artifact was generated from a known repository state and saved as the stable input for an experiment or application run. The application does not rediscover the system during each mission.

At startup it loads the saved ROS system model, configuration schema, renderer manifest, capability registry, and parameter evidence. Evidence and optional enrichment hashes must match the loaded system model and schema. The renderer reads the manifest-named baseline when Step 8 runs.

## Repository map

- `src/ros_config_builder/extraction/`: deterministic Python, launch, and YAML fact extraction.
- `src/ros_config_builder/integration/`: construction of the integrated ROS system model.
- `src/ros_config_builder/templating/`: physical-slot schema, template generation, validation, and rendering.
- `src/ros_config_builder/mission/`: capability reasoning, semantic evidence, retrieval, separated parameter selection/value reasoning, and evaluation.
- `configuration_templates/`: generated Jinja templates, lean renderer manifest, baseline profiles, and the human-authored capability registry.
- `artifacts/`: frozen system, configuration, evidence, and experiment outputs.
- `prompts/`: versionable prompts for the three LLM boundaries and optional semantic enrichment.
- `data/`: annotated capability tasks and parameter-reasoning tasks.
- `tests/`: unit, integration, scenario, and equivalence tests.

## Running the prototype

Install the local package, then run the test suite:

```bash
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -t .
```

Regenerate the complete deterministic artifact chain after changing ROS source, launch files, parameter YAML, extraction/integration logic, or template generation:

```bash
PYTHONPATH=src python helper_scripts/regenerate_artifacts.py --workspace .
```

This is the canonical regeneration command. It rebuilds the integrated ROS system model, physical configuration-slot schema, generated templates, baseline, lean manifest, hash-bound parameter evidence, and generated scenario bundles, then writes `artifacts/ARTIFACTS.json` with counts and hashes. It validates the human-authored capability registry against the rebuilt artifacts but does not generate that registry.

Regeneration replaces `artifacts/semantic_parameters/`. Consequently, any optional `enrichment.json` is deliberately removed because its input hashes may no longer be valid. Generate enrichment again only after deterministic regeneration. At application startup, both evidence and explicitly requested enrichment are checked against the loaded system-model and configuration-schema hashes; stale artifacts are rejected.

The canonical command runs offline scenario-equivalence analysis by default. This is artifact-generation assurance and is separate from normal mission rendering, which skips repository re-analysis. Use `--skip-scenario-equivalence` only when deliberately producing artifacts without that offline check.

Configure the application in `src/ros_config_builder/parameters.yaml`. The file contains the
operation, mission, artifact paths, model settings, retrieval settings, and the inputs used by
the analyze and validate operations. For example:

```json
{
  "operation": "mission",
  "workspace": ".",
  "mission": "I installed wheels with a diameter of 8 cm",
  "output_directory": "artifacts/mission_runs/wheel_change"
}
```

The `.yaml` file uses JSON-compatible YAML so it can be parsed without another dependency.

Then run the application without command-line arguments:

```bash
PYTHONPATH=src python src/ros_config_builder/orchestrate.py
```

Set `operation` to `analyze` or `validate` in the same YAML file to run those operations. A
single flat `ApplicationConfiguration` validates the complete YAML object and rejects unknown
or invalid settings before any operation starts.

Generate optional LLM semantic enrichment from the frozen source evidence:

```bash
PYTHONPATH=src python helper_scripts/enrich_parameters.py --workspace .
```

The enrichment is stored separately from deterministic facts and records model, prompt, and input hashes. It is optional; the runtime can use source evidence without it. When supplied to the application, its system-model and configuration-schema hashes are verified at startup.

Run parameter-context ablations only after the task file has been human reviewed:

```bash
PYTHONPATH=src python helper_scripts/evaluate_parameter_reasoning.py --workspace .
```

The current 40-task parameter dataset is marked `pending_human_review`; the evaluator refuses to treat it as dissertation evidence unless that state is deliberately overridden.

## Documentation

- [End-to-end architecture](docs/architecture.md)
- [How deterministic artifacts are produced](docs/code_interpretation_architecture.md)
- [AI evaluation and ablations](docs/ai_evaluation.md)
- [Step 2: capability interpretation](docs/step_2_capability_interpretation.md)
- [Step 3: capability realization](docs/step_3_capability_realization.md)
- [Step 4: ROS orchestration](docs/step_4_ros_orchestration.md)
- [Step 5: parameter catalogue and evidence](docs/step_5_parameter_catalogue.md)
- [Step 6: retrieval, selection, and value reasoning](docs/step_6_parameter_reasoning.md)
- [Step 7: validation and plan construction](docs/step_7_parameter_validation_and_plan.md)
- [Step 8: rendering](docs/step_8_rendering.md)

## The entire pipeline:
PYTHONPATH=src python helper_scripts/evaluate_orchestrated_dataset.py \
  --dataset data/antrobot_train_mission_dataset_v1.jsonl \
  --output artifacts/evaluation/train

PYTHONPATH=src python helper_scripts/evaluate_orchestrated_dataset.py \
  --dataset data/antrobot_test_mission_dataset_v1.jsonl \
  --output artifacts/evaluation/test


## Only the capability reasoning:
PYTHONPATH=src python helper_scripts/evaluate_capability_reasoning.py \
  --dataset data/antrobot_train_mission_dataset_v1.jsonl \
  --model qwen2.5-coder:7b \
  --output artifacts/capability_reasoning/train_fourth

PYTHONPATH=src python helper_scripts/evaluate_capability_reasoning.py \
  --dataset data/antrobot_test_mission_dataset_v1.jsonl \
  --model qwen2.5-coder:7b \
  --output artifacts/capability_reasoning/test_initial


Parameter evaluation and reasoning:
PYTHONPATH=src python helper_scripts/evaluate_parameter_reasoning.py \
  --dataset data/antrobot_train_mission_dataset_v1.jsonl \
  --model qwen2.5-coder:7b \
  --context-variant names_values \
  --output artifacts/parameter_reasoning/train_qwen_names_values


PYTHONPATH=src python helper_scripts/evaluate_parameter_reasoning.py \
  --dataset data/antrobot_test_mission_dataset_v1.jsonl \
  --model qwen2.5-coder:7b \
  --context-variant names_values \
  --output artifacts/parameter_reasoning/test_qwen_names_values
