# arctl design

This document describes the current controller implemented in `src/arctl`. It
is an operational map for task authors, evaluator authors, human reviewers, and
AI operators. The [MVP specification](../arctl_light_mvp_spec_simple.md) remains
the normative contract; this document emphasizes how the pieces fit together.

## The loop at a glance

```text
  HUMAN + APPROVED CONFIGURATION
  ┌──────────────────────────────────────────────────────────────────────┐
  │ task.yaml + evaluator commit + manifest + environment + champion    │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  │ approve once
                                  v
  ┌──────────────┐     auto      ┌───────────────┐
  │ Freeze trials│<──────────────│ Calibrate     │
  │ (fixed/auto) │               │ champion once │
  └──────┬───────┘               └───────────────┘
         │
         v
  ┌────────────────┐   derive   ┌────────────────────────┐
  │ Understand the │───────────>│ Successful-policy      │
  │ environment    │            │ behaviors (strategy)   │
  └────────────────┘            └────────────┬───────────┘
                                             │
                 ┌───────────────────────────v──────────────────────────┐
                 │ Start from latest champion; propose one mechanism;   │
                 │ inspect public history; edit only approved paths     │
                 └───────────────────────────┬──────────────────────────┘
                                             │ candidate
                                             v
                 ┌──────────────────────────────────────────────────────┐
                 │ Static checks -> independent review -> optional      │
                 │ bounded repair -> freeze candidate commit            │
                 └───────────────────────────┬──────────────────────────┘
                                             │
                                             v
                 ┌──────────────────────────────────────────────────────┐
                 │ Public checks -> reserve paired seeds -> prepare     │
                 │ batch -> run both subjects -> score -> validate      │
                 └───────────────────────────┬──────────────────────────┘
                                             │ fixed evidence
                          ┌──────────────────┴──────────────────┐
                          v                                     v
                  ACCEPT / promote                 REJECT / ARCHIVE / INVALID
                          └──────────────────┬──────────────────┘
                                             v
                 ┌──────────────────────────────────────────────────────┐
                 │ Telemetry reflection -> publish result + dossier ->  │
                 │ append public ledger -> loop until stop/stall/limit  │
                 └──────────────────────────────────────────────────────┘
```

The central rule is simple: language models may propose and interpret, but
only the approved evaluator's frozen comparison protocol can decide whether a
candidate is promoted.

## Actors and trust boundaries

```mermaid
flowchart LR
    H[Human approver] -->|locks exact bytes and commits| C[Trusted arctl controller]
    O[Human or AI operator] -->|CLI or JSON commands| C
    C -->|public environment only| S[Strategy and reflection model]
    C -->|champion worktree and public history| X[Execution model]
    X -->|candidate edits in whitelist| C
    C -->|read-only candidate diff| R[Independent policy reviewer]
    C -->|private seeds and scoring inputs| E[Approved evaluator]
    C -->|public batch only| P1[Champion process]
    C -->|same public batch only| P2[Candidate process]
    E -->|aggregate evidence and allowlisted telemetry| C
    C -->|public result, dossier, ledger| O

    classDef trusted fill:#d8f3dc,stroke:#2d6a4f,color:#000;
    classDef untrusted fill:#ffe8cc,stroke:#d9480f,color:#000;
    classDef private fill:#e7f5ff,stroke:#1864ab,color:#000;
    class C,H trusted;
    class S,X,R,P1,P2 untrusted;
    class E private;
```

- **The controller** owns state transitions, Git refs, seeds, process records,
  validation, publication, and compare-and-swap promotion.
- **The evaluator** owns the statistic, uncertainty method, calibration
  diagnostic, suspect-test rule, telemetry contract, and final decision rule.
- **Strategy** studies the environment, not the current policy. It produces
  cited environment observations and implementation-independent behaviors.
- **Execution** starts from the latest accepted champion, reads public history,
  chooses one strategic behavior, and implements one testable mechanism.
- **Review and reflection** are advisory methodology checks. Neither can alter
  the evaluator's verdict.

## Task lifecycle

```mermaid
flowchart TD
    D[TASK_DRAFT] -->|approval preview and human token| A[APPROVED]
    A -->|fixed trials| READY[READY]
    A -->|trials: auto| CAL[CALIBRATION_REQUIRED]
    CAL -->|pilot succeeds or ceiling fallback| READY
    CAL -->|process/evidence failure| CALF[CALIBRATION_FAILED]
    READY -->|arctl run| WORK[Active search or experiment state]
    WORK -->|safe stop| STOP[STOPPED]
    WORK -->|six candidate misses| STALL[SEARCH_STALLED]
    WORK -->|recoverable downstream failure| FAIL[Inspectable failure state]
    WORK -->|experiment published| READY
    READY -->|completed equals approved maximum| LIMIT[LIMIT_REACHED]
    STALL -->|explicit later run| WORK
    FAIL -->|safe recovery or retry| WORK
    STOP -->|explicit later run| WORK
```

`max_experiments` is an approval-locked lifetime ceiling. The
`--max-experiments` option is only a smaller per-invocation bound. Reaching the
task ceiling produces `LIMIT_REACHED`; it does not run calibration again or
pretend that an empty invocation tested a candidate.

## Approval and automatic calibration

Approval locks the task file hash, evaluator commit, evaluator manifest hash,
initial champion, and hashes of every declared environment source. Changing
any of those inputs requires a new approval. Promotion may move the champion
ref afterward; calibration remains bound to the initial approved champion.

```mermaid
flowchart TD
    P[Parse task v3] --> V[Validate repositories, paths, commands, models, and manifest]
    V --> ENV[Hash declared environment files and probe contracts]
    ENV --> PREVIEW[Show models, paths, seeds, trials, telemetry, risks, token]
    PREVIEW -->|explicit human confirmation| LOCK[Write immutable approval records]
    LOCK --> T{trials}
    T -->|positive integer| FIX[Freeze fixed count]
    T -->|auto| RES[Reserve private calibration seeds]
    RES --> PREP[Evaluator prepares one ceiling-sized batch]
    PREP --> CHAMP[Run approved champion once]
    CHAMP --> ASSESS[Evaluator reports diagnostic at every ladder prefix]
    ASSESS --> SELECT[Controller selects smallest stable passing rung]
    SELECT -->|none pass| CEIL[Freeze approved ceiling and persist warning]
    SELECT -->|one passes| FREEZE[Freeze selected count]
    CEIL --> READY[READY]
    FREEZE --> READY
```

Calibration estimates an approved baseline property; it is not a power
guarantee for every future effect. A ceiling fallback remains visible in
`status`, `report`, `inspect`, and run progress.

## Strategy and candidate search

Strategy and execution deliberately have different information. Strategy sees
the objective as a boundary plus approval-locked environment sources and
policy-free probes. It does not see the champion, evaluator statistic,
telemetry targets, or exploration ledger. Execution sees the current champion,
strategy behaviors, editable-path contract, public probes, and public history.

```mermaid
flowchart TD
    START[Need candidate] --> STRAT{Current strategy exists?}
    STRAT -->|no| ANALYZE[Fresh read-only environment analysis]
    STRAT -->|yes| ATTEMPT
    ANALYZE --> BEHAVIOR[Publish cited successful-policy behaviors]
    BEHAVIOR --> ATTEMPT[Fresh execution attempt from latest champion]
    ATTEMPT --> REQUEST[Select behavior; propose mechanism; review ledger evidence]
    REQUEST --> EDIT[Edit only approved paths and run public development tools]
    EDIT --> VALIDATE{Request, links, paths, and tree valid?}
    VALIDATE -->|no| MISS[Record typed public search miss]
    VALIDATE -->|yes| GUARD[Deterministic policy checks]
    GUARD --> REVIEW[Fresh read-only semantic review]
    REVIEW -->|pass| FREEZE[Commit and freeze novel candidate]
    REVIEW -->|fail and repair available| REPAIR[Fresh bounded repair]
    REPAIR --> GUARD
    REVIEW -->|fail after bound| MISS
    MISS --> COUNT{Three or six misses?}
    COUNT -->|fewer than three| ATTEMPT
    COUNT -->|three| REFRESH[Refresh environment strategy once]
    REFRESH --> ATTEMPT
    COUNT -->|six| STALL[SEARCH_STALLED; no experiment or seeds consumed]
```

Exact duplicate Git trees are hard misses. Semantic similarity is only ledger
guidance, allowing deliberate refinements rather than crashing when a related
hypothesis reappears.

## Experiment and comparison

An official experiment is allocated only after a candidate has passed the
search and review gates. Public checks can reject it before private seeds are
reserved. After reservation, comparison work is exactly once.

```mermaid
flowchart TD
    FROZEN[CANDIDATE_FROZEN] --> CHECKS[Run approval-locked public checks]
    CHECKS -->|ordinary failure| REJECT[Publish REJECT without comparison]
    CHECKS -->|pass| RESERVE[PRIMARY_RESERVED]
    RESERVE --> PREP[Evaluator prepare: public batch plus private scoring]
    PREP --> ORDER[Use reserved randomized subject order]
    ORDER --> CHAMP[Run champion once]
    CHAMP --> CAND[Run candidate once on identical ordered batch]
    CAND --> SCORE[Evaluator score once]
    SCORE --> VALID[Validate identities, trials, bounds, rules, and telemetry]
    VALID -->|invalid process or evidence| INVALID[Publish INVALID; never redraw]
    VALID --> PRIMARY[Fixed primary evidence]
    PRIMARY -->|approved suspect trigger| SUSPECT[SUSPECT_RESERVED: one fresh paired comparison]
    PRIMARY -->|no trigger| DECIDE[Resolve evaluator-owned decision]
    SUSPECT --> DECIDE
    DECIDE -->|ACCEPT| PROMOTE[Compare-and-swap champion ref]
    DECIDE -->|REJECT or ARCHIVE| KEEP[Keep current champion]
    PROMOTE --> REFLECT
    KEEP --> REFLECT[REFLECTING]
    INVALID --> PUBLISH
    REFLECT --> PUBLISH[Publish result, dossier, and exploration entry]
```

Champion and candidate receive the same ordered public cases but never receive
private scoring data, evaluator code, controller storage, or trial identities.
The controller validates the evaluator's protocol and evidence shape; it does
not independently prove the evaluator's mathematics.

## Decisions and telemetry reflection

The primary evidence contains the effect estimate, one-sided lower bound, hard
rule result, suspect-test request, and every manifest-declared public telemetry
metric. The approved evaluator maps that evidence to the final decision.

```mermaid
flowchart LR
    E[Validated aggregate evidence] --> H{Hard rules pass?}
    H -->|no| J[REJECT]
    H -->|yes| S{Suspect test required?}
    S -->|yes| SR[Run exactly one suspect comparison]
    S -->|no| R[Resolve approved decision]
    SR --> R
    R --> A[ACCEPT and promote]
    R --> J[REJECT]
    R --> C[ARCHIVE]
    A --> T[Telemetry reflection]
    J --> T
    C --> T
    T --> Q[Assess mechanism realization, metric movements, causal limits, and implementation concerns]
    Q --> L[Publish advisory next action to dossier and ledger]
```

Reflection cannot turn a rejection into an acceptance. Its job is to explain
what the aggregates do and do not support: for example, whether a positive mean
with a negative lower bound appears driven by variance, whether telemetry
supports the proposed mechanism, or whether the implementation failed to
express the intended strategic behavior.

## Persistence, recovery, and retries

Every managed process records its identity before its command is released. A
saved valid result is reused. A process that started without a valid result is
not silently rerun in the same process record.

```mermaid
flowchart TD
    CALL[Need downstream process] --> STARTED{started.json exists?}
    STARTED -->|no| RECORD[Record command, cwd, environment, PID, start time]
    RECORD --> GATE[Release gated child process]
    GATE --> RESULT{Valid result.json?}
    STARTED -->|yes, valid result| REUSE[Validate reservation and reuse result]
    STARTED -->|yes, no valid result| HALT[Stop exact process group; require recovery path]
    RESULT -->|yes| REUSE
    RESULT -->|recognized transient before official reservation| BUDGET{Retry budget available?}
    BUDGET -->|yes| WAIT[Interruptible fixed delay]
    WAIT --> FRESH[Create fresh numbered attempt; preserve failed artifacts]
    FRESH --> CALL
    BUDGET -->|no| SURFACE[Show primary downstream error and artifact path]
    RESULT -->|started calibration or comparison failure| INVALID[Preserve evidence; never retry or redraw]
```

`arctl run --retries N --retry-delay SECONDS` opts into retries for recognized
Codex capacity/rate-limit/network/timeouts and transient network failures in
pre-trial public processes. The count is consecutive and resets after a
successful downstream stage. Ctrl-C or `arctl stop` interrupts the delay.

## Storage and audit trail

Git and validated files are authoritative; no database is required. The main
task directory contains:

```text
TASK/
├── task.yaml                         # approval-locked task configuration
├── approval.json                     # hashes and initial champion
├── evaluator.commit
├── evaluator.manifest.json
├── trial-count.json                  # fixed count and calibration summary
├── calibration/                      # private requests, process records, outputs
├── strategy/
│   ├── 000001.public.json            # environment observations and behaviors
│   └── 000001/                       # process and failure artifacts
├── searches/000001/attempts/01/      # executor and candidate-review attempts
├── experiments/000001/
│   ├── request.public.json
│   ├── candidate.commit
│   ├── comparisons/primary/          # private reservation and evidence
│   ├── result.public.json            # allowlisted aggregate result
│   ├── reflection.public.json
│   └── published
├── exploration/ledger.public.jsonl   # strategy, misses, results, reflections
└── reports/experiments/000001/        # immutable public Markdown dossier
```

The dossier is derived, public-only, and readable by a human. Private
reservations, seeds, cases, schedules, evaluator outputs, raw subject outputs,
commands, and environments are never linked into it.

## Operator interface

Humans normally use `run`, `status`, `stop`, `report`, `history`, and
`inspect`. AI operators use the same commands with `--json`; research agents
never receive operator authority. Human `status` and `report` output uses
bounded-width tables; champion displays identify the promoting experiment and
hypothesis, while machine JSON retains exact evidence values. See the exact [command
reference](cli-reference.md), which is mechanically mirrored from `arctl -h`
and every `arctl COMMAND -h` screen.

Related documents:

- [Evaluator design boundary](evaluator-design.md)
- [Empirical validation protocols](empirical-validation.md)
- [MVP specification](../arctl_light_mvp_spec_simple.md)
