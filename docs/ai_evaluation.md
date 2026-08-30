# AI evaluation and context ablations

This document defines how the AI contribution should be evaluated. It does not report dissertation results: the current parameter task set is a draft awaiting independent human review.

## Evaluation question

The primary question is:

> Does adding structured program evidence improve parameter retrieval, parameter selection, value reasoning, and safe abstention for natural-language ROS configuration requests?

The experiment separates the boundaries so a failure can be attributed to retrieval, selection, value generation, validation, or the final outcome rather than collapsed into one success/failure number.

## Task families

`data/parameter_reasoning_tasks_v1.jsonl` currently contains 40 tasks across six intended difficulty families:

1. explicit — the parameter or its direct meaning and target value are stated;
2. semantic — the request uses behavior or domain language rather than the exact identifier;
3. physical — unit conversion or physical interpretation is required;
4. behavioral — symptoms must be connected to plausible configuration effects;
5. multi-component — one request requires coordinated selections or deliberate exclusions;
6. ambiguous/unsupported/no-change — the correct result may be abstention rather than a configuration.

The current distribution contains 32 supported, 4 ambiguous, and 4 unsupported tasks. All are marked `pending_human_review` and must not be presented as validated dissertation ground truth yet.

## Gold annotation

For a supported parameter task, gold data records:

- the exact selected parameter-ID set;
- the expected new value for each ID;
- optional absolute numeric tolerance;
- outcome, difficulty, and strata.

For an ambiguous or unsupported task, gold data records the terminal status without a partial configuration.

The decision rules are defined independently of model prompts in `data/mission_annotation_policy.md`. Reviewers must verify:

- the source-derived parameter ID;
- current value and type;
- unit conversion;
- required related occurrences;
- whether the request is actually supported;
- whether clarification is genuinely necessary.

Review status must be changed to `human_verified` only after that check. The evaluator blocks the canonical draft dataset by default while any task remains pending.

## Context ablations

The same reasoner and prompts can be run with five context variants:

| Variant | Information supplied |
|---|---|
| `names_values` | Parameter ID, name, component, kind, type, and current value |
| `semantic` | Names/values plus descriptions, physical quantities, units, categories, effects, and constraints |
| `source` | Semantic context plus declaration and usage excerpts |
| `system` | Source context plus related ROS interfaces |
| `graph` | System context plus parameter relationships and bounded graph-neighbor expansion |

The deterministic lexical retriever is held constant except for the fields available to each variant. This makes the contribution of additional grounding observable. If a learned or hybrid retriever is later added, it should be evaluated as a separate factor rather than silently replacing the baseline.

## Separated evaluation boundaries

### Retrieval

For each expected parameter ID, evaluation records where it appeared in the ranked candidates.

Reported metrics are:

- top-1 recall;
- top-3 recall;
- top-5 recall;
- mean reciprocal rank (MRR).

A selection model cannot select an ID absent from its bounded context, so retrieval recall is a meaningful upper-bound diagnostic.

### Parameter selection

The selected ID set is compared independently of proposed values.

Reported metrics are:

- exact-set accuracy;
- micro precision;
- micro recall;
- micro F1;
- per-case missed and over-selected IDs.

Graph adjacency alone is not sufficient gold justification. A related parameter belongs in the expected set only when the request requires it.

### Value reasoning

Value evaluation is run after selection. It compares each expected value using exact equality or the annotated absolute tolerance.

Reported metrics are:

- per-value accuracy;
- complete value-set accuracy;
- extra values introduced by the prediction.

The runtime contract additionally checks that value reasoning does not add unselected IDs and that each supplied `old_value` still matches the frozen catalogue.

### Outcome and end to end

Reported metrics are:

- outcome accuracy over `valid`, `no_change`, `unsupported`, and `needs_clarification`;
- end-to-end accuracy, requiring correct outcome, exact selection, and complete values;
- unsafe execution rate for cases that should be unsupported or require clarification;
- outcome and end-to-end accuracy grouped by difficulty.

Exceptions, malformed JSON, schema failures, and mapping failures are always counted as failures. They are never reclassified as correct abstention.

## Running an ablation

Before freezing an experiment, regenerate its deterministic grounding artifacts:

```bash
PYTHONPATH=src python helper_scripts/regenerate_artifacts.py --workspace .
```

This rebuilds the system model, physical-slot schema, templates/baseline/lean manifest, evidence, and scenario artifacts as one chain. It removes any previous optional semantic enrichment because that enrichment may be tied to old input hashes. If an ablation uses enrichment, generate it again after regeneration; application startup verifies its system-model and configuration-schema hashes.

After deterministic regeneration and human verification, run all variants with:

```bash
PYTHONPATH=src python helper_scripts/evaluate_parameter_reasoning.py \
  --workspace . \
  --model qwen2.5-coder:7b \
  --output artifacts/parameter_reasoning/ablation_v1
```

During dataset development only, pending tasks can be exercised explicitly:

```bash
PYTHONPATH=src python helper_scripts/evaluate_parameter_reasoning.py \
  --workspace . \
  --allow-pending-review \
  --output artifacts/parameter_reasoning/development_only
```

That override does not make the annotations valid research ground truth.

To run selected variants:

```bash
PYTHONPATH=src python helper_scripts/evaluate_parameter_reasoning.py \
  --workspace . \
  --context-variant names_values \
  --context-variant graph
```

The default lexical retrieval settings are `top_k=12` and one graph hop. These must be recorded or held constant when comparing variants.

## Output artifacts

Each variant directory contains:

- `parameter_predictions.jsonl` — case-level predictions and diagnostics;
- `parameter_evaluation.json` — machine-readable metrics;
- `parameter_evaluation.md` — compact human-readable summary.

The experiment root also contains `experiment.json` with:

- model identifier;
- context variants;
- retrieval settings;
- hashes of the dataset, prompts, system model, schema, and manifest;
- metrics for every variant.

Those hashes make prompt or context drift visible. If optional semantic enrichment is used, its artifact already records model/prompt/input provenance and the experiment directory should be named or extended to preserve that choice clearly.

## Comparing runs

Compatible reports can be compared with:

```bash
PYTHONPATH=src python helper_scripts/compare_mission_evaluations.py \
  artifacts/parameter_reasoning/baseline/graph \
  artifacts/parameter_reasoning/candidate/graph \
  --output artifacts/parameter_reasoning/comparison
```

Comparison requires the same case IDs and dataset. It reports metric deltas and per-case pass/fail transitions.

## LLM-output evaluation using orchestration prompts

To evaluate the LLM stages used by `orchestrate.py`—capability interpretation, parameter selection, and parameter
value reasoning—run:

```bash
PYTHONPATH=src python helper_scripts/evaluate_orchestrated_dataset.py \
  --configuration src/ros_config_builder/parameters.yaml
```

By default this compares `data/evaluation_missions.jsonl` with the generated
`data/antrobot_mission_dataset_v1.jsonl`. Supply `--dataset PATH` repeatedly to select other JSONL datasets and
use `--limit N` for a smoke run. The evaluator constructs the inference components through the same factory as
`orchestrate.py`; therefore model, host, all three prompts and response schemas, registry, evidence, context
variant, retrieval limits, and graph expansion are shared rather than reimplemented. Deterministic capability
realization and ROS orchestration are used only to construct the parameter prompts. The evaluator deliberately
stops before configuration-plan construction, YAML rendering, launch-file assembly, or render validation.

The report contains the following recommended metrics:

- outcome accuracy and unsafe execution rate for terminal outcomes;
- exact capability match and capability leaf-level micro precision, recall, and F1;
- exact parameter-selection match and selection micro precision, recall, and F1;
- exact parameter-value match and value micro precision, recall, and F1;
- joint exact match across all applicable LLM outputs as the primary end-to-end correctness measure;
- inference-pipeline completion rate as a reliability measure;
- mean, median, and p95 end-to-end latency;
- paraphrase prediction consistency and all-paraphrases-correct group robustness for generated datasets.

Each dataset receives `predictions.jsonl` and `evaluation.json`. Every prediction retains the exact system/user
prompts, raw model responses, parsed structured outputs, and validation errors for all calls that occurred. The
experiment root receives `comparison.json` and `comparison.md`, including hashes for the application configuration
and datasets. No configuration YAML or launch file is generated or evaluated.

## Experimental discipline

Use a development/held-out split before prompt tuning. Once errors from a dataset have influenced prompts, retrieval weights, examples, or context construction, that dataset is development data.

For each reported run, freeze at least:

- dataset version and review state;
- source/system/schema/evidence versions;
- model identifier and runtime settings;
- selection and value prompts;
- context variant;
- retrieval limits and graph hops;
- semantic-enrichment artifact, if any;
- raw predictions and errors.

Report confidence intervals or repeated-run variability where model inference is not deterministic. Temperature zero improves repeatability but does not guarantee identical generation across model/runtime/hardware versions.

## Recommended experiment sequence

1. Independently review and correct all parameter tasks.
2. Freeze a development and untouched held-out split.
3. Run the five context variants with one fixed model.
4. Analyze retrieval, selection, and value errors separately.
5. Tune only on development data.
6. Freeze the final candidate configuration.
7. Evaluate the held-out split once.
8. Optionally compare local models or learned retrieval in clearly separated experiments.

This sequence keeps the thesis claim focused: whether structured ROS program evidence improves LLM configuration reasoning, not whether a particular prompt was overfit to a small benchmark.
