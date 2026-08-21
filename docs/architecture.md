# Semester 1 architecture

Dependency direction is intentionally one-way:

```text
schemas
  ↑
extraction
  ↑
integration
  ↑
templating
  ↑
validation / reporting
  ↑
pipeline facade / CLI
```

Extraction never imports integration or templating. Integration consumes
immutable extraction payloads. Rendering owns generated overlays but never
modifies the analyzed ROS repository. Validation compares semantic models,
not generated file formatting.

## Stability boundaries

- JSON/Pydantic schemas and frozen configuration artifacts are versioned.
- The public API is exported by `ros_config_builder`.
- `SemesterOnePipeline` is the application-facing facade.
- `repo_code_extractor` is deprecated compatibility surface only.
- Generated output belongs under `artifacts/generated/` and is reproducible.

Changes to extraction, integration, curation, rendering, or comparison
semantics require a focused test and a regenerated acceptance report.
