# Proposed Solution diagrams

These Mermaid diagrams accompany `proposed_solution.tex`. They are kept outside the LaTeX chapter so they can be reviewed and exported independently.

## 1. Complete proposed architecture

```mermaid
flowchart TD
    SRC[ROS 2 Python, launch, YAML, package metadata]
    SRC --> EXT[Deterministic knowledge extraction]
    EXT --> SYS[Unified deployed-system model]
    SYS --> CFG[Physical configuration representation]
    SYS --> EVD[Grounded parameter evidence]
    CFG --> TPL[Templates, complete baseline, lean manifest]

    REG[Human-authored capability registry] --> START[Startup cross-validation]
    SYS --> START
    CFG --> START
    EVD --> START
    TPL --> START

    USER[User mission] --> CAP[LLM capability interpretation]
    START --> CAP
    CAP --> REAL[Deterministic capability realization]
    REAL --> ROS[ROS connection and QoS verification]
    ROS --> RET[Deterministic lexical retrieval and graph expansion]
    USER --> RET
    RET --> SEL[LLM parameter selection]
    SEL --> VAL[LLM value reasoning]
    VAL --> PLAN[Deterministic sparse plan generation]
    PLAN --> CHECK[Configuration validation]
    CHECK --> RENDER[Deterministic launch and YAML rendering]
    RENDER --> OUT[Mission-specific ROS 2 configuration bundle]
```

## 2. Deterministic knowledge and artifact generation

```mermaid
flowchart LR
    PY[Python ROS entities] --> INT[System integration]
    LAUNCH[Launch and deployment facts] --> INT
    YAML[Parameter assignments] --> INT
    PKG[Package metadata] --> INT

    INT --> INST[Deployment instances]
    INT --> PARAM[Effective parameters and provenance]
    INT --> IFACE[Effective ROS interfaces]
    INT --> UNRES[Explicit unresolved facts]

    INST --> SYS[Versioned unified system model]
    PARAM --> SYS
    IFACE --> SYS
    UNRES --> SYS

    SYS --> SLOTS[Physical configuration slots]
    SYS --> EVIDENCE[Bounded source and interface evidence]
    SLOTS --> DEFINITION[Templates, baseline, renderer manifest]

    SYS --> HASH[Artifact freshness hashes]
    SLOTS --> HASH
    HASH --> EVIDENCE
```

## 3. Parameter retrieval and separated LLM reasoning

```mermaid
flowchart TD
    M[Mission text] --> TOK[Normalization, stop-word removal, stemming]
    CAT[Complete parameter catalogue] --> DOC[Variant-dependent searchable fields]
    TOK --> SCORE[Weighted lexical scoring]
    DOC --> SCORE
    SCORE --> TOP[Top-k candidates]
    GRAPH[Parameter relationships and wiring bindings] --> EXPAND[Bounded graph expansion]
    TOP --> EXPAND
    EXPAND --> CONTEXT[Character-budgeted selection context]

    CONTEXT --> SELECT[LLM parameter selection]
    M --> SELECT
    SYSTEM[Current realization and ROS orchestration] --> SELECT
    SELECT --> SVALID{Selection valid?}
    SVALID -- no --> TERMINAL[No change, unsupported, clarification, or rejection]
    SVALID -- yes --> VALUECTX[Selected records, current values, constraints, relationships]
    VALUECTX --> VALUE[LLM value reasoning]
    M --> VALUE
    VALUE --> VVALID{Values valid?}
    VVALID -- no --> TERMINAL
    VVALID -- yes --> CHANGES[Validated parameter changes]
```

## 4. Sparse plan validation and configuration application

```mermaid
flowchart TD
    CR[Capability realization values] --> MERGE[Sparse plan merge]
    PC[Validated parameter changes] --> MERGE
    MERGE --> CONFLICT{Claims agree?}
    CONFLICT -- no --> REJECT[Reject plan]
    CONFLICT -- yes --> OBJECTIVE[Existence, type, bounds, renderability]
    OBJECTIVE --> VALID{All checks pass?}
    VALID -- no --> REJECT
    VALID -- yes --> BASE[Overlay sparse plan on complete baseline]
    BASE --> JINJA[Strict predefined templates]
    JINJA --> STATIC[Launch syntax and ROS parameter structure checks]
    STATIC --> BUNDLE[Generated launch/YAML bundle with hashes and provenance]
```

## 5. Representative end-to-end request

```mermaid
flowchart TD
    U[Build a map using KISS-ICP and limit its range to 20 metres]
    U --> C1[Select mapping capability]
    U --> C2[Select KISS-ICP odometry implementation]
    C1 --> R[Deterministic realization]
    C2 --> R
    R --> R1[Activate mapping implementation]
    R --> R2[Activate KISS-ICP]
    R --> R3[Activate scan-to-point-cloud dependency]
    R --> R4[Displace kinematic ICP]

    R --> O[Verify required ROS interface chain]
    O --> P[Retrieve KISS-ICP range candidates]
    U --> P
    P --> S[Select maximum-range parameter]
    S --> V[Reason new value: 20 metres]
    V --> PLAN[Merge capability and parameter values]
    PLAN --> CHECK[Validate identifiers, old value, type, bounds, and conflicts]
    CHECK --> APPLY[Overlay on baseline and render predefined artifacts]
```
