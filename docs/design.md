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
                 │ Compare every behavior against the latest champion; │
                 │ freeze one brief, then implement only that brief     │
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
                 │ append public ledger -> loop until stop or limit     │
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
    C -->|environment snapshot only| S[Strategy model]
    C -->|champion and public history| P[Planning model]
    C -->|frozen brief and champion| X[Implementation model]
    X -->|candidate edits in whitelist| C
    C -->|read-only candidate diff| R[Independent policy reviewer]
    C -->|private seeds and scoring inputs| E[Approved evaluator]
    C -->|public batch only| P1[Champion process]
    C -->|same public batch only| P2[Candidate process]
    E -->|aggregate evidence and allowlisted telemetry| C
    C -->|candidate, champion, result, public history| F[Reflection model]
    C -->|public result, dossier, ledger| O

    classDef trusted fill:#d8f3dc,stroke:#2d6a4f,color:#000;
    classDef untrusted fill:#ffe8cc,stroke:#d9480f,color:#000;
    classDef private fill:#e7f5ff,stroke:#1864ab,color:#000;
    class C,H trusted;
    class S,P,X,R,F,P1,P2 untrusted;
    class E private;
```

- **The controller** owns state transitions, Git refs, seeds, process records,
  validation, publication, and compare-and-swap promotion.
- **The evaluator** owns the statistic, uncertainty method, calibration
  diagnostic, suspect-test rule, telemetry contract, and final decision rule.
- **Strategy** studies the environment, not the current policy. It produces
  cited environment observations and implementation-independent behaviors.
- **Planning** starts from the latest accepted champion, searches a compact public
  catalog, and gives each candidate direction one authoritative request. Selection
  references the chosen direction; it never restates or paraphrases the mechanism.
- **Implementation** receives that frozen request and current code, not the growing
  research history.
- **Review** controls pre-trial admission but cannot evaluate outcomes.
  **Reflection** interprets completed evidence but cannot alter its verdict.

## Task lifecycle

```mermaid
flowchart TD
    I[Guided init prints location-safe resume command] --> Q[Public inspection and cited question batches]
    Q --> A0[Authorize derived design]
    A0 --> B[Offline generation in isolated staging]
    B --> R[Independent static review before provisioning]
    R --> C[Dependency lock and capability-aware conformance]
    R -->|findings| B
    C -->|failure| B
    C -->|setup token accepted| D[TASK_DRAFT]
    D -->|approval preview and human token| A[APPROVED]
    A -->|fixed trials| READY[READY]
    A -->|trials: auto| CAL[CALIBRATION_REQUIRED]
    CAL -->|pilot succeeds or ceiling fallback| READY
    CAL -->|process/evidence failure| CALF[CALIBRATION_FAILED]
    READY -->|arctl run| WORK[Active search or experiment state]
    WORK -->|safe stop| STOP[STOPPED]
    WORK -->|all directions exhausted| REVISE[Revise environment strategy]
    WORK -->|recoverable downstream failure| FAIL[Inspectable failure state]
    WORK -->|experiment published| READY
    READY -->|completed equals approved maximum| LIMIT[LIMIT_REACHED]
    REVISE --> WORK
    FAIL -->|safe recovery or retry| WORK
    STOP -->|explicit later run| WORK
```

Guided setup is resumable pre-approval scaffolding, not research and not
scientific approval. Its agents cannot read evaluator-private data. Generated
public trees are hashed after review; acceptance refuses changed trees, creates
local commits, and never pushes. `init` leaves the supplied source repository
untouched and locally clones it into a non-Git shell workspace containing the
independent `subject`, `environment`, and `evaluator` repositories. The workspace
subject changes only on an `arctl/setup-*` branch. The setup agent implements typed subject, preparation,
calibration, scoring, and telemetry hooks. Controller-owned entrypoints compile
Python execution descriptors into the task-v5/manifest-v4 setup protocol and enforce
the declared telemetry wire shapes. Unsupported process-resource guarantees
remain advisory instead of becoming nullable evaluator metrics. The generated evaluator includes public hook tests,
but the human still owns the evaluator's mathematical validity at `approve`.
Human intent comes from explicit revisioned setup answers. Discovery inspects
public code offline and asks up to three related, cited decisions per batch. Every
option displays its exact persisted value. Objective, outcome, and editable boundary
require a human source; environment adapter, outcome extraction, finite horizon,
conformance, and dependencies are typed in the final design while derived values
retain their repository or controller provenance. `ARCTL_SETUP.md` is generated only
after acceptance as a readable view of the structured decision and design state.
The specialist inspects environment code and recommends the
sampling, randomness, calibration, uncertainty, and telemetry design rather
than delegating those mechanics to the user. Setup prints live discovery,
generation,
dependency, validation, check, and review stages with direct failure details.
Comparator freezing, paired-batch execution, seed non-reuse, calibration
selection, promotion mapping, and unscored operational failures are
controller-owned invariants rather than setup questions. Unsupported operational
guarantees require one explicit advisory-downgrade confirmation. Recommendations state
their assumptions and use conservative conventional choices when evidence is
incomplete.
Confirmed decisions constrain one hash-bound authorized design snapshot consumed
without lossy translation by generation and review. The builder writes disposable staging repositories and returns a
compact report rather than source code in JSON. The controller validates and
reviews the resulting files before provisioning authorized direct dependencies,
then locks the resolved graph and permits one error-directed repair. Invalid
retries preserve their immutable diagnostics.
An unfinished contract repair resumes from the saved generation instead of
rerunning the expensive generation stage.
Generated tests, protocol hooks, public checks, and the public probe run as
bounded managed process groups inside filesystem and network sandboxes. The
setup token binds the authorization bundle, staging trees, task draft,
owned-file list, subject base, and review result. A later edit clears the token; the next `arctl setup` records
the changes, reruns only their dependent checks plus setup review, and returns a
replacement token. Acceptance requires an empty Git index and stages only
reviewed owned paths.

`max_experiments` is an approval-locked lifetime ceiling or `unlimited`. The
`--max-experiments` option is only a smaller per-invocation bound. Reaching the
task ceiling produces `LIMIT_REACHED`; it does not run calibration again or
pretend that an empty invocation tested a candidate.

## Approval and automatic calibration

Approval locks the task file, resolved method, backend attestations, evaluator
commit and manifest, initial champion, and every declared environment Git
blob. Changing any semantic input requires a new approval. Promotion may move
the champion ref afterward; calibration remains bound to the approved initial
champion.

```mermaid
flowchart TD
    P[Parse task v5 or compatible v3/v4] --> V[Validate repositories, paths, method, and manifest]
    V --> ENV[Snapshot environment Git blobs and probe contracts]
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

Strategy, planning, and execution deliberately have different information. Strategy sees
the objective as a boundary plus approval-locked environment sources and
policy-free probes. It does not see the champion, evaluator statistic,
telemetry targets, or current policy. Planning sees the current champion,
strategy behaviors, and the searchable public catalog but cannot edit. Execution
receives one direction-owned frozen brief and may edit only approved paths.

```mermaid
flowchart TD
    START[Need candidate] --> STRAT{Current strategy exists?}
    STRAT -->|no| ANALYZE[Fresh read-only environment analysis]
    STRAT -->|yes| PLAN
    ANALYZE --> BEHAVIOR[Publish cited successful-policy behaviors]
    BEHAVIOR --> PLAN[Assess every behavior against latest champion]
    PLAN -->|all exhausted| REFRESH[Refresh environment strategy]
    REFRESH --> PLAN
    PLAN --> REQUEST[Freeze best evidence-backed experiment brief]
    REQUEST --> EDIT[Implement brief and audit every frozen obligation]
    EDIT --> VALIDATE{Request, links, paths, and tree valid?}
    VALIDATE -->|no| MISS[Record typed public search miss]
    VALIDATE -->|yes| GUARD[Deterministic policy checks]
    GUARD --> REVIEW[Review diff, audit coverage, and evidence]
    REVIEW -->|pass| FREEZE[Commit and freeze novel candidate]
    REVIEW -->|fail and repair available| REPAIR[Fresh bounded repair]
    REPAIR --> GUARD
    REVIEW -->|fail after bound| MISS
    MISS --> PLAN
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
Published execution failures carry a sanitized explanation. Human views label
results without valid comparison evidence as `No score` and retain the reason in
the experiment dossier.

## Storage and audit trail

Git and validated files are authoritative; no database is required. The main
task directory contains:

```text
TASK/
├── task.yaml                         # approval-locked task configuration
├── approval.json                     # hashes and initial champion
├── evaluator.commit
├── evaluator.manifest.json
├── environment/                      # approval-locked Git-blob snapshots
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
├── exploration/
│   ├── entries/                      # full immutable canonical records
│   ├── ledger.public.jsonl           # compact deterministic search catalog
│   └── direction-exhaustion.public.jsonl
└── reports/experiments/000001/        # immutable public Markdown dossier
```

Agent selections, backend attestations, and session provenance are persisted
beside their lifecycle artifacts. Prompts are immutable `prompt.public.txt`
files, limited to 32 KiB and hashed before execution. The Codex adapter streams
them over standard input; other adapters must preserve the same contract.
Growing history stays in the catalog and canonical entries instead of being
copied into each prompt.

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

- [Research methods and agent backends](research-methods.md)
- [Evaluator design boundary](evaluator-design.md)
- [Empirical validation protocols](empirical-validation.md)
- [MVP specification](../arctl_light_mvp_spec_simple.md)
