"""YAML-configured orchestrator for analysis, validation, and missions."""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Allow ``python src/ros_config_builder/orchestrate.py ...`` without installation.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ros_config_builder.mission import (
    AntRobotCapabilityRegistry, MissionInterpretation, MissionInterpreter, OllamaBackend,
    build_interpretation_prompt, build_template_configuration_plan, derive_parameter_catalogue,
    build_parameter_selection_system_context,
    ParameterReasoner, ParameterSelectionInterpretation, ParameterValueInterpretation,
    build_parameter_evidence, enrichment_by_parameter_id, evidence_by_parameter_id,
    load_capability_registry, load_parameter_evidence_artifact,
    load_semantic_enrichment_artifact, parameter_evidence_artifact_matches,
    semantic_enrichment_artifact_matches,
    realize_capabilities, resolve_ros_orchestration,
    validate_capability_registry, validate_template_plan,
)
from ros_config_builder.integration import SystemModel
from ros_config_builder.pipeline import SemesterOnePipeline
from ros_config_builder.templating import (
    TemplateConfigurationSchema, TemplateManifest, render_configuration_bundle,
)


Renderer = Callable[..., dict[str, Any]]
DEFAULT_CONFIGURATION_PATH = Path(__file__).with_name("parameters.yaml")


class ApplicationConfiguration(BaseModel):
    """All application settings loaded from the colocated parameters file."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["mission", "analyze", "validate"] = "mission"
    workspace: Path = Path(".")
    mission: str = "Build a map using KISS-ICP"
    output_directory: Path = Path("artifacts/mission_runs/current")

    system_model: Path = Path("artifacts/ros_system_model/ros_system_model.json")
    configuration_schema: Path = Path(
        "artifacts/template_configuration/template_configuration_schema.json"
    )
    parameter_evidence: Path | None = Path("artifacts/semantic_parameters/evidence.json")
    semantic_enrichment: Path | None = None
    template_directory: Path = Path("configuration_templates")
    capability_registry: Path = Path("configuration_templates/capability_registry.yaml")

    capability_prompt: Path = Path("prompts/mission_interpretation.txt")
    parameter_selection_prompt: Path = Path("prompts/parameter_selection.txt")
    parameter_value_prompt: Path = Path("prompts/parameter_value_reasoning.txt")
    ollama_model: str = "qwen2.5-coder:7b"
    ollama_host: str | None = None
    ollama_context_window: int = Field(default=8192, ge=1024)
    parameter_selection_context_window: int = Field(default=16384, ge=1024)
    environment_file: Path = Path(".env")

    context_variant: Literal[
        "names_values", "semantic", "source", "system", "graph", "llm_semantic",
    ] = "graph"
    retrieval_top_k: int = Field(default=10, ge=1)
    graph_hops: int = Field(default=1, ge=0)

    root_launch_files: list[Path] = Field(default_factory=lambda: [
        Path("src/antrobot_ros/launch/antrobot.launch.py")
    ])
    analysis_output: Path | None = Path("artifacts/ros_system_model/analysis.json")
    model_to_validate: Path = Path("artifacts/ros_system_model/ros_system_model.json")

    @model_validator(mode="after")
    def operation_inputs_exist(self) -> "ApplicationConfiguration":
        if self.operation == "mission" and not self.mission.strip():
            raise ValueError("mission must not be empty when operation is 'mission'")
        if self.operation == "analyze" and not self.root_launch_files:
            raise ValueError("root_launch_files must not be empty when operation is 'analyze'")
        return self

    def resolve(self, path: Path | None) -> Path | None:
        """Resolve application paths relative to the configured workspace."""
        if path is None:
            return None
        return path if path.is_absolute() else self.workspace / path


def load_application_configuration(
    path: str | Path = DEFAULT_CONFIGURATION_PATH,
) -> ApplicationConfiguration:
    """Load and validate the application's single YAML configuration object."""
    configuration_path = Path(path)
    try:
        raw = json.loads(configuration_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"application configuration is missing: {configuration_path}"
        ) from error
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON-compatible YAML object in {configuration_path}")
    configuration = ApplicationConfiguration.model_validate(raw)
    workspace = configuration.workspace
    configuration.workspace = (
        workspace.resolve() if workspace.is_absolute() else (Path.cwd() / workspace).resolve()
    )
    return configuration


def load_environment(path: Path) -> None:
    """Load simple KEY=VALUE settings without overriding exported variables."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class MissionApplication:
    """Route one mission through interpretation, mapping, validation, and rendering."""

    def __init__(self, *, workspace: str | Path,
                 interpreter: MissionInterpreter,
                 parameter_reasoner: ParameterReasoner,
                 capability_registry: AntRobotCapabilityRegistry | None = None,
                 renderer: Renderer = render_configuration_bundle,
                 template_directory: str | Path = "configuration_templates",
                 schema_path: str | Path = "artifacts/template_configuration/template_configuration_schema.json",
                 reference_model_path: str | Path = "artifacts/ros_system_model/ros_system_model.json",
                 parameter_evidence_path: str | Path | None = "artifacts/semantic_parameters/evidence.json",
                 semantic_enrichment_path: str | Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.interpreter, self.renderer = interpreter, renderer
        self.parameter_reasoner = parameter_reasoner
        self.capability_registry = capability_registry
        self.template_directory = self._resolve(template_directory)
        self.schema = TemplateConfigurationSchema.model_validate(
            self._read_json(self._resolve(schema_path))
        ).model_dump(mode="json")
        self.reference_model = SystemModel.model_validate(
            self._read_json(self._resolve(reference_model_path))
        ).model_dump(mode="json")
        self.manifest = TemplateManifest.model_validate(
            self._read_json(self.template_directory / "manifest.json")
        ).model_dump(mode="json")
        evidence = None
        if parameter_evidence_path is not None:
            frozen_evidence_path = self._resolve(parameter_evidence_path)
            if not frozen_evidence_path.is_file():
                raise FileNotFoundError(
                    f"required frozen parameter evidence is missing: {frozen_evidence_path}"
                )
            artifact = load_parameter_evidence_artifact(frozen_evidence_path)
            if not parameter_evidence_artifact_matches(
                artifact, source_model=self.reference_model,
                configuration_model=self.schema,
            ):
                raise ValueError(
                    f"parameter evidence artifact is stale: {frozen_evidence_path}"
                )
            evidence = artifact.parameters
        if evidence is None:
            evidence = build_parameter_evidence(self.workspace, self.schema, self.reference_model)
        self.parameter_evidence = evidence_by_parameter_id(evidence)
        self.semantic_enrichments: dict[str, dict[str, Any]] = {}
        if semantic_enrichment_path is not None:
            enrichment = load_semantic_enrichment_artifact(
                self._resolve(semantic_enrichment_path)
            )
            if not semantic_enrichment_artifact_matches(
                enrichment, source_model=self.reference_model,
                configuration_model=self.schema,
            ):
                raise ValueError(
                    f"semantic enrichment artifact is stale: "
                    f"{self._resolve(semantic_enrichment_path)}"
                )
            self.semantic_enrichments = enrichment_by_parameter_id(enrichment)
        if self.capability_registry is not None:
            validate_capability_registry(
                self.capability_registry, self.reference_model, self.schema, self.manifest,
            )

    def _resolve(self, path: str | Path) -> Path:
        value = Path(path)
        return value if value.is_absolute() else self.workspace / value

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise FileNotFoundError(f"required frozen artifact is missing: {path}") from error
        if not isinstance(value, dict):
            raise ValueError(f"expected a JSON object in {path}")
        return value

    def _assemble_launch_system(self, bundle: Path, render_records: list[dict[str, Any]]) -> dict[str, Any]:
        """Collect the generated entrypoint and its AntRobot launch/config inputs."""
        system = bundle.parent / "ros2_launch_system"
        if system.exists():
            shutil.rmtree(system)
        launch_output, config_output = system / "launch", system / "config"
        launch_output.mkdir(parents=True)
        config_output.mkdir(parents=True)

        copied_launch = []
        for source in sorted((self.workspace / "src/antrobot_ros/launch").glob("*.launch.py")):
            target = launch_output / source.name
            shutil.copy2(source, target)
            copied_launch.append(target.name)
        copied_config = []
        for source in sorted((self.workspace / "src/antrobot_ros/config").glob("*.yaml")):
            target = config_output / source.name
            shutil.copy2(source, target)
            copied_config.append(target.name)

        generated_launch, generated_config = [], []
        for record in render_records:
            source = bundle / record["output"]
            if source.name.endswith(".launch.py"):
                target = launch_output / "mission.launch.py"
                generated_launch.append(target.name)
            elif source.suffix in {".yaml", ".yml"}:
                target = config_output / source.name
                generated_config.append(target.name)
            else:
                continue
            shutil.copy2(source, target)

        manifest = {"entrypoint": "launch/mission.launch.py",
            "generated_launch_files": generated_launch, "generated_config_files": generated_config,
            "supporting_launch_files": copied_launch, "supporting_config_files": copied_config,
            "note": "External ROS package dependencies must be installed in the target ROS 2 environment."}
        (system / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"directory": str(system.resolve()), "entrypoint": str((system / manifest["entrypoint"]).resolve()),
            "launch_files": len(set(copied_launch + generated_launch)),
            "config_files": len(set(copied_config + generated_config)),
            "manifest": str((system / "manifest.json").resolve())}

    @staticmethod
    def _write_result(output: Path, result: dict[str, Any]) -> None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "mission_result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def process(self, mission: str, *, output_directory: str | Path) -> dict[str, Any]:
        """Process one mission; terminal outcomes never reach mapping or rendering."""
        interpretation = self.interpreter.interpret(mission)
        result: dict[str, Any] = {
            "mission": mission.strip(), "status": interpretation.status,
            "capability_interpretation": interpretation.model_dump(mode="json"),
            "system_realization": None, "ros_orchestration": None, "parameter_catalogue": None,
            "parameter_retrieval": None, "parameter_selection": None,
            "parameter_value_interpretation": None, "plan": None, "render": None,
        }
        output = self._resolve(output_directory)
        if interpretation.status != "valid":
            self._write_result(output, result)
            return result
        capabilities = interpretation.capabilities
        assert capabilities is not None
        if self.capability_registry is None:
            raise RuntimeError("mission processing requires a capability registry")
        realization = realize_capabilities(capabilities, self.capability_registry)
        orchestration = resolve_ros_orchestration(
            realization, self.capability_registry, self.reference_model, self.manifest,
        )
        result["system_realization"] = realization.model_dump(mode="json")
        result["ros_orchestration"] = orchestration.model_dump(mode="json")
        if orchestration.status == "invalid":
            result["status"] = "unsupported"
            result["orchestration_error"] = "required ROS connections could not be satisfied"
            self._write_result(output, result)
            return result
        catalogue = derive_parameter_catalogue(
            realization, self.schema,
            evidence_by_id=self.parameter_evidence,
            semantic_enrichments=self.semantic_enrichments,
        )
        result["parameter_catalogue"] = catalogue.model_dump(mode="json")
        reasoning = self.parameter_reasoner.reason(
            mission, catalogue,
            system_context=build_parameter_selection_system_context(
                orchestration.model_dump(mode="json"),
            ),
            wiring_bindings=self.manifest.get("wiring_bindings", {}),
        )
        result["parameter_retrieval"] = reasoning.retrieval.model_dump(mode="json")
        result["parameter_selection"] = reasoning.selection.model_dump(mode="json")
        if reasoning.value_interpretation is not None:
            result["parameter_value_interpretation"] = (
                reasoning.value_interpretation.model_dump(mode="json")
            )
        if reasoning.status in {"unsupported", "needs_clarification"}:
            result["status"] = reasoning.status
            self._write_result(output, result)
            return result
        plan = build_template_configuration_plan(realization, reasoning.validated_values)
        validate_template_plan(plan, self.schema, self.manifest)
        result["plan"] = plan.model_dump(mode="json")
        bundle = output / "bundle"
        if bundle.exists():
            shutil.rmtree(bundle)
        rendered = self.renderer(workspace=self.workspace, template_directory=self.template_directory,
            schema=self.schema, reference_model=self.reference_model, output_directory=bundle,
            profile=None, user_values=plan.user_values,
            debug_contexts=False, require_equivalence=False)
        equivalence = rendered["validation"]["equivalence"]
        launch_system = self._assemble_launch_system(bundle, rendered["render_records"])
        result["render"] = {"output_directory": str(Path(rendered["output_directory"]).resolve()),
            "files": [record["output"] for record in rendered["render_records"]],
            "valid": rendered["validation"]["valid"],
            "baseline_equivalent": equivalence["semantically_equivalent"],
            "baseline_difference_areas": sorted({item["area"] for item in equivalence["unexpected_differences"]}),
            "validation_file": str((bundle / "validation.json").resolve()),
            "ros2_launch_system": launch_system}
        self._write_result(output, result)
        return result


def build_mission_application(configuration: ApplicationConfiguration) -> MissionApplication:
    """Build the exact mission pipeline described by an application configuration."""
    workspace = configuration.workspace
    environment_path = configuration.resolve(configuration.environment_file)
    assert environment_path is not None
    load_environment(environment_path)
    prompt_path = configuration.resolve(configuration.capability_prompt)
    selection_prompt_path = configuration.resolve(configuration.parameter_selection_prompt)
    value_prompt_path = configuration.resolve(configuration.parameter_value_prompt)
    registry_path = configuration.resolve(configuration.capability_registry)
    assert prompt_path is not None
    assert selection_prompt_path is not None
    assert value_prompt_path is not None
    assert registry_path is not None
    registry = load_capability_registry(registry_path)
    prompt = build_interpretation_prompt(
        prompt_path.read_text(encoding="utf-8"), registry,
    )
    backend = OllamaBackend(
        host=configuration.ollama_host,
        model=configuration.ollama_model,
        response_model=MissionInterpretation,
        context_window=configuration.ollama_context_window,
    )
    selection_backend = OllamaBackend(
        host=configuration.ollama_host,
        model=configuration.ollama_model,
        response_model=ParameterSelectionInterpretation,
        context_window=configuration.parameter_selection_context_window,
    )
    value_backend = OllamaBackend(
        host=configuration.ollama_host,
        model=configuration.ollama_model,
        response_model=ParameterValueInterpretation,
        context_window=configuration.ollama_context_window,
    )
    reasoner = ParameterReasoner(
        selection_backend, value_backend,
        selection_prompt_template=selection_prompt_path.read_text(encoding="utf-8"),
        value_prompt_template=value_prompt_path.read_text(encoding="utf-8"),
        context_variant=configuration.context_variant,
        top_k=configuration.retrieval_top_k,
        graph_hops=configuration.graph_hops,
    )
    return MissionApplication(
        workspace=workspace,
        interpreter=MissionInterpreter(
            backend, system_prompt=prompt, registry=registry,
        ),
        parameter_reasoner=reasoner,
        capability_registry=registry,
        template_directory=configuration.template_directory,
        schema_path=configuration.configuration_schema,
        reference_model_path=configuration.system_model,
        parameter_evidence_path=configuration.parameter_evidence,
        semantic_enrichment_path=configuration.semantic_enrichment,
    )


def run(configuration_path: str | Path = DEFAULT_CONFIGURATION_PATH) -> int:
    """Execute the operation selected in the validated configuration."""
    configuration: ApplicationConfiguration = load_application_configuration(configuration_path)
    workspace = configuration.workspace
    if configuration.operation == "analyze":
        pipeline = SemesterOnePipeline(workspace)
        model = pipeline.integrate([
            path.as_posix() for path in configuration.root_launch_files
        ])
        report = pipeline.validate(model)
        payload = {"model": model, "validation": report}
        output = configuration.resolve(configuration.analysis_output)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        else:
            print(json.dumps(payload, indent=2))
        return 0 if not report.get("errors") else 1
    if configuration.operation == "validate":
        model_path = configuration.resolve(configuration.model_to_validate)
        assert model_path is not None
        model = json.loads(model_path.read_text(encoding="utf-8"))
        report = SemesterOnePipeline(workspace).validate(model)
        print(json.dumps(report, indent=2))
        return 0 if not report.get("errors") else 1

    application = build_mission_application(configuration)
    try:
        result = application.process(
            configuration.mission,
            output_directory=configuration.output_directory,
        )
    except RuntimeError as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(run())
