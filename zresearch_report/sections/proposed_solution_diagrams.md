# Proposed Solution diagrams

These Mermaid diagrams accompany `proposed_solution.tex`. They are kept outside the LaTeX chapter so they can be reviewed and exported independently.

## 1. Complete proposed architecture

```mermaid
flowchart LR
    SRC[ROS 2 repository] --> MODEL[Deterministic system model<br/>and configuration knowledge]
    REG[Capability registry] --> REASON[Three-stage LLM reasoning]
    MODEL --> REASON
    USER[User mission] --> REASON
    REASON --> VALIDATE[Deterministic realization<br/>and validation]
    VALIDATE --> RENDER[Template-based rendering]
    RENDER --> OUT[ROS 2 configuration bundle]
```

## 2. Deterministic knowledge and artifact generation

```mermaid
flowchart LR
    SRC[Python, launch, YAML,<br/>and package files] --> EXTRACT[Static extraction<br/>and integration]
    EXTRACT --> SYS[Versioned deployed-system model]
    SYS --> CFG[Physical configuration<br/>and deterministic evidence]
    CFG --> SEM[Validated semantic metadata]
    CFG --> GEN[Baseline, templates,<br/>and renderer definition]
```

## 3. Three-call language-model pipeline

Suggested placement: immediately after the opening explanation in the `Natural-Language Configuration Reasoning` section.

```mermaid
flowchart TD
    M[User mission] --> C1[LLM call 1<br/>Capability interpretation]
    C1 --> D1[Realize capabilities and<br/>build bounded context]
    D1 --> C2[LLM call 2<br/>Parameter selection]
    C2 --> D2[Validate selection<br/>and retry once if needed]
    D2 --> C3[LLM call 3<br/>Value reasoning]
    C3 --> D3[Validate values<br/>and retry once if needed]
    D3 --> OUT[Validated configuration decisions]
```

## 4. Parameter retrieval and separated LLM reasoning

```mermaid
flowchart TD
    INPUT[Mission, active components,<br/>and parameter catalogue] --> RET[Semantic retrieval<br/>of top 10 candidates]
    RET --> CONTEXT[Bounded model context<br/>without ranks or scores]
    CONTEXT --> SELECT[LLM selects parameter IDs]
    SELECT --> CHECK[Deterministic validation<br/>with one retry]
    CHECK --> VALUE[LLM generates values<br/>for selected IDs only]
    VALUE --> OUT[Validated parameter changes]
```

## 5. Sparse plan validation and configuration application

```mermaid
flowchart TD
    CAP[Capability realization] --> MERGE[Sparse configuration plan]
    PARAM[Validated parameter changes] --> MERGE
    MERGE --> VALIDATE[Conflict, type, bounds,<br/>and renderability checks]
    VALIDATE --> APPLY[Overlay on complete baseline]
    APPLY --> RENDER[Render and structurally validate<br/>launch and YAML files]
    RENDER --> BUNDLE[Configuration bundle<br/>with hashes and provenance]
```

## 6. Representative end-to-end request

```mermaid
flowchart TD
    U[Build a map using KISS-ICP and limit its range to 20 metres]
    U --> CAP[Select Cartographer mapping<br/>and KISS-ICP odometry]
    CAP --> REAL[Activate required components<br/>and dependencies]
    REAL --> PARAM[Select KISS-ICP maximum range<br/>and assign 20 metres]
    PARAM --> CHECK[Validate and merge<br/>the sparse plan]
    CHECK --> APPLY[Render the ROS 2<br/>configuration bundle]
```
