"""Source-grounded semantic retrieval, reasoning, evaluation, and mission contracts."""

from .inference import MissionInterpreter, OllamaBackend, build_interpretation_prompt, extract_json_object
from .reasoning import (
    ParameterReasoner, ParameterReasoningResult, ParameterSelectionInterpretation,
    ParameterValueInterpretation, ProposedParameterChange, SelectedParameter,
    build_parameter_selection_prompt, build_parameter_selection_system_context,
    build_parameter_value_prompt,
    validate_parameter_selection, validate_parameter_values,
)
from .retrieval import (
    ParameterRetrieval, RetrievalCandidate, SelectionContext, build_selection_context,
    catalogue_subset, retrieve_parameters,
)
from .semantics import (
    InterfaceEvidence, ParameterEvidence, ParameterEvidenceArtifact,
    SemanticEnricher, SemanticEnrichment, SemanticEnrichmentArtifact,
    SemanticEnrichmentBatch, SemanticParameterRelationship, SourceExcerpt,
    build_parameter_evidence,
    build_parameter_evidence_artifact, build_semantic_enrichment_artifact,
    build_semantic_enrichment_prompt, enrichment_by_parameter_id,
    evidence_by_parameter_id, load_parameter_evidence_artifact,
    load_semantic_enrichment_artifact, parameter_evidence_artifact_matches,
    semantic_enrichment_artifact_matches, write_parameter_evidence,
    write_semantic_enrichment,
)
from .evaluation import evaluate_missions, write_evaluation_report
from .parameter_evaluation import (
    evaluate_parameter_reasoning, write_parameter_evaluation_report,
)
from .dataset import (
    ClarificationSeed, GoldParameterChange, ParameterReasoningGold,
    ParameterReasoningTask, ParameterTaskExpectation, SeedConfiguration,
    SyntheticSeed, UnsupportedSeed, ValidSeed, load_parameter_reasoning_tasks,
    load_synthetic_seeds, summarize_synthetic_seeds,
)
from .generation import (
    CandidateReview, OllamaParaphraseBackend, SyntheticCandidate,
    automatically_validate_candidates, dataset_quality_report, freeze_accepted_dataset,
    generate_paraphrases, load_candidates, review_candidate, write_candidate_history,
    write_quality_report, write_review_queue,
)
from .schema import (
    AntRobotCapabilityRegistry,
    ActiveDeploymentComponent,
    AvailableParameter,
    CapabilitySelections,
    ExplorationSelection,
    MappingSelection,
    MissionInterpretation,
    NavigationSelection,
    OdometrySelection,
    ParameterCatalogue,
    ParameterChange,
    QoSCompatibilityResult,
    RealizedCapability,
    RealizedComponent,
    ResolvedWiringBinding,
    ROSConnection,
    ROSOrchestrationPlan,
    SystemRealization,
    TemplateConfigurationPlan,
    capability_prompt_catalogue,
    build_template_configuration_plan,
    derive_parameter_catalogue,
    load_capability_registry,
    realize_capabilities,
    resolve_ros_orchestration,
    validate_parameter_changes,
    validate_capability_registry,
    validate_template_plan,
)

__all__ = [name for name in globals() if not name.startswith("_")]
