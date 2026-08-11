# Repository-analysis pipeline — 8 August 2026

## Purpose

This diagram documents the pipeline as it is implemented now. It separates:

1. repository-wide artifact generation;
2. parameter-specific graph retrieval;
3. context compilation;
4. optional Ollama analysis.

The labels on arrows describe the information transferred between stages,
even when the receiving script reads that information from an artifact on disk
rather than receiving it as a direct function argument.

## Complete implemented pipeline

```mermaid
flowchart TD
    SRC["Selected Python source roots<br/><code>src/antrobot_ros</code><br/><code>src/antrobot_description</code><br/><code>src/kiss-icp</code><br/><code>src/kinematic-icp</code><br/><br/>Repository source code only;<br/>ignored directories and installed libraries excluded"]

    S00["Stage 00: module and import indexing<br/><code>00_repo_ingestion.py</code><br/><br/>Find Python files; parse imports;<br/>record modules and top-level definitions"]
    A00["Module index artifacts<br/><code>artifacts/repo_ingestion/module_index.json</code><br/><code>module_index.md</code>"]

    S01["Stage 01: shared code representation<br/><code>01_repo_ast_extraction.py</code><br/><br/>Parse each indexed Python file once;<br/>record scopes, definitions, expressions,<br/>control structures, and source text"]
    S02["Stage 02: configuration-source detection<br/><code>02_repo_config_detection.py</code><br/><br/>Deserialize Stage 01 AST; detect ROS parameters,<br/>environment/config APIs, dictionaries,<br/>launch parameters, and wrapper candidates"]
    S03["Stage 03: value-flow graph construction<br/><code>03_repo_value_flow.py</code><br/><br/>Deserialize Stage 01 AST; apply Stage 02 roots;<br/>build assignments, transformations, validations,<br/>calls, returns, controls, and ROS consumers"]
    QUERY["Parameter query<br/>Example: <code>wheel_radius</code>"]
    TOOL["Parameter graph tools<br/><code>helper_scripts/parameter_graph_tools.py</code><br/><code>helper_scripts/parameter_context_compiler.py</code><br/><br/>Extract the exact prompt evidence<br/>for the requested parameter"]

    RUNNER["LLM analysis through Ollama<br/><code>helper_scripts/analyze_parameter_with_llm.py</code><br/><br/>Send the extracted evidence to the local model<br/>through Ollama and receive the analysis"]
    FINAL["Analysis artifact<br/><code>artifacts/repo_ingestion/<br/>llm_analysis_&lt;parameter&gt;.json</code><br/><br/>Tool summary, compiled evidence,<br/>and model analysis"]

    SRC -->|"Python file paths, imports,<br/>top-level classes/functions"| S00
    S00 -->|"Indexed module records"| A00

    A00 -->|"Ordered files, module names,<br/>and repository paths"| S01
    SRC -->|"Original source parsed once"| S01
    S01 -->|"<code>ast_extraction.json</code><br/>Repository structure and expressions<br/>used to identify configuration sources"| S02
    S01 -->|"<code>ast_extraction.json</code><br/>Repository structure and expressions<br/>used to trace how values move"| S03
    S02 -->|"<code>configuration_sources.json</code><br/>Detected records, defaults, and bindings<br/>used as graph traversal roots"| S03
    QUERY --> TOOL
    S01 -->|"<code>ast_extraction.json</code><br/>Scopes and source text used<br/>to recover relevant methods"| TOOL
    S03 -->|"<code>value_flow_graph.json</code><br/>Graph nodes and typed edges searched<br/>and traversed for the requested parameter"| TOOL
    TOOL -->|"Exact prompt evidence"| RUNNER
    RUNNER -->|"Generated parameter analysis"| FINAL
```

## Important implementation details exposed by the diagram

### Stages 00–03 are now sequential data dependencies

`01_repo_ast_extraction.py` is the only analysis stage that parses the selected
Python source. It stores a lossless JSON representation of the complete Python
AST together with the exact source snapshot and the readable scope inventory.
Stage 02 deserializes that AST to detect configuration sources. Stage 03
deserializes the same AST and combines it with Stage 02's catalog to build the
value-flow graph. Neither Stage 02 nor Stage 03 calls `ast.parse` or rereads the
repository source.

### The parameter tool reuses the Stage 01 representation

The value-flow graph contains locations and scopes, not complete source files.
After graph traversal, `parameter_graph_tools.py` uses Stage 01's serialized AST
and embedded source snapshot to recover complete enclosing functions and
methods. It does not create another parser path or reread source for extraction.
