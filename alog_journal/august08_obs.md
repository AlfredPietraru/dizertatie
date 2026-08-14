the important thing, in order to generate correct json entries that can be consumed by the templater for launch files: first I need to properly understand the relationship between parameters, otherwise, it will be completely impossible to render valid .launch.py files

## Questions and Issues covert the YAML files to templates:
simple conversion between the yaml files to template is most probably incorrect.
The templates should be aware of dependencies and contradictions between the parameters.
How do I do that? The LLM clearly needs deep code understanding. 

Do I have to identify the parameters from the yaml while only looking in the code:
For example for odom_eval_params.yaml -> i have to look in odom_eval_node.py and find the correct
parameters associated with it and only from that build the yaml?







### Simplest solution: Pass the entire codebase to LLM
Let the LLM undertand on it's own all the dependencies between all the variables, and can update the yaml templates with all the things it might need. 
What it needs?  
Example: Constrains (thresholds or recommended entries), default values, dependencies to other parameters.

| Source root | Files | Bytes | Estimated tokens |
|---|---:|---:|---:|
| `src/antrobot_ros` | 69 | 339,620 | 81,128 |
| `src/antrobot_description` | 20 | 52,184 | 14,478 |
| `src/kiss-icp` | 111 | 352,631 | 87,560 |
| `src/kinematic-icp` | 47 | 257,755 | 62,058 |
| **TOTAL** | **247** | **1,002,190** | **245,224** |

And the model I am currently using has a context window of 131072 tokens, which is almost half the amount. While ignoring the fact that passing all the content might be a bad solution it itself due to context rot. Also there is a limitation on the output size which could be only up to 32K tokens.
So the input should be around 90K - 110K tokens.
Some context engineering work is necessary.

### A better solution - pass to the LLM for a particular parameter only the chunks of code relevant to that parameter, greatly reduces the context.
I build the code graph -> what is next.
Need to define tools to call

## Parameter-oriented repository extraction

The repository analysis code was converted from a collection of standalone
scripts into the importable `repo_code_extractor` package. The numbered script
names were removed because Python modules cannot be imported normally when their
names begin with a number.

The package now owns the complete deterministic extraction pipeline:

1. repository and module ingestion;
2. scoped AST extraction;
3. configuration-source detection;
4. configuration value-flow construction;
5. function-level code chunk extraction;
6. parameter graph querying;
7. parameter-context compilation.

`ast_intermediate.py`, `parameter_graph_tools.py`, and
`parameter_context_compiler.py` were moved into `repo_code_extractor`. This is
important because repository extraction and parameter evidence retrieval now
have one interface instead of requiring imports from unrelated helper scripts.

The main callable interface for one exact parameter is:

```python
from repo_code_extractor import extract_parameter_artifacts

result = extract_parameter_artifacts(
    "wheel_radius",
    repo_root="/home/alf/ros_ws",
)
```

The equivalent command is:

```bash
python3 -m repo_code_extractor . --parameter wheel_radius
```

## Parameter artifact layout

Every queried parameter has a separate artifact directory:

```text
artifacts/code_parameters/<parameter_name>/
├── context.md
└── result.json
```

- `context.md` is the human-readable and LLM-facing evidence package. It
  contains detected instances, defaults, declarations, forward-flow
  relationships, validations, transformations, calls, ROS consumers,
  cross-instance relationships, and deduplicated complete source methods.
- `result.json` contains the same query result in a structured form for
  programmatic processing.

`context.md` must not be interpreted as a guaranteed list of every textual
occurrence. It contains the evidence reached by the current static value-flow
analysis. YAML, C++, documentation, dynamically constructed names, or flows the
analyzer cannot resolve can still be absent.

## First single-parameter test

The first end-to-end test used the exact parameter name `wheel_radius`.

| Parameter-context observation | Result |
|---|---:|
| Exact parameter name | `wheel_radius` |
| Detected parameter instances | 2 |
| Cross-instance pairs evaluated | 4 |
| Forward-flow nodes | 79 |
| Forward-flow edges | 105 |
| Deduplicated source chunks | 9 |
| Generated `context.md` size | 44,267 characters |
| Generated `context.md` lines | 816 |
| Structured `result.json` size | 45,586 bytes |

The two instances are declared in
`joint_state_estimator_node.py` and `rdrive_node.py`. Both have the detected
default value `0.03`. The extracted evidence includes zero-value validation,
wheel velocity and position calculations, feasible-velocity calculations, and
the call that passes the radius to the drive implementation.

This test confirms that parameter-specific retrieval reduces the input from an
estimated 245,224 tokens for all selected source roots to a focused context of
44,267 characters. The exact token count still depends on the tokenizer and
must be measured before enforcing a prompt budget.

## Practical prompt budget for Qwen2.5-Coder 7B

Qwen2.5-Coder 7B advertises a maximum context of 131,072 tokens, but its normal
configuration is 32,768 tokens. Contexts above 32K require long-context/YaRN
configuration. In addition, Ollama's allocated context can be much smaller than
the model maximum and should be checked with `ollama ps`.

For this parameter-analysis task, using the theoretical maximum is not the
objective. A smaller relevant context is more appropriate for a 7B model.

| Prompt-budget item | Recommended value | Reason |
|---|---:|---|
| Ollama `num_ctx` | 32,768 tokens | Uses the model's normal context range without requiring YaRN |
| Preferred extracted evidence | 20,000-40,000 characters | Keeps the prompt focused and leaves room for instructions and output |
| Acceptable upper evidence size | approximately 60,000 characters | Use only when the evidence cannot be reduced without losing important code |
| Reserved generated answer | 2,000-4,000 tokens | Allows a structured parameter analysis |
| Evidence above the upper size | rank or split chunks | Avoids context dilution and excessive KV-cache use |

The current `wheel_radius` context is slightly above the preferred character
range but remains reasonable with `num_ctx=32768`. The next context-engineering
step should rank evidence and separate direct parameter evidence from long
transitive flows. Character count is useful as a cheap guard, but the final
budgeting decision should use the Qwen tokenizer's actual token count.
