# Repository code extractor

`repo_code_extractor` is an importable Python package for repository ingestion,
AST extraction, configuration detection, value-flow analysis, function
chunking, and optional vector-store ingestion.

## Python API

```python
from repo_code_extractor import extract_code_information

result = extract_code_information(
    "/path/to/workspace",
    source_roots=["src/my_package"],
    output_directory="artifacts/repo_ingestion",
)

print(result.summaries)
print(result.artifacts)
```

Extract all graph-connected evidence and deduplicated source text for one exact
configuration parameter:

```python
from repo_code_extractor import extract_parameter_artifacts

evidence = extract_parameter_artifacts("wheel_radius", repo_root="/path/to/workspace")
print(evidence["llm_context"])
print(evidence["artifact_paths"])
```

Individual stages are available from their modules, while the most common
methods are exported directly from `repo_code_extractor`.

## Command line

Run the complete pipeline without installing the package:

```bash
python3 -m repo_code_extractor /path/to/workspace \
  --source-root src/my_package \
  --output artifacts/repo_ingestion
```

Use `--no-chunks` or `--no-static-analysis` to skip those optional stages.

The parameter evidence CLI remains available as a package module:

```bash
python3 -m repo_code_extractor.parameter_graph_tools \
  --repo-root /path/to/workspace \
  context wheel_radius
```

Parameter-specific results are saved by default under
`artifacts/code_parameters/<parameter_name>/context.md` and `result.json`.

Vector ingestion is intentionally separate. Importing the package does not
require ChromaDB, Hugging Face Hub, or python-dotenv; those dependencies are
checked only when `repo_code_extractor.vector_store.ingest_chunks()` is called.
