# Research methods

arctl fixes the semantic loop but version-controls its implementation:

`STRATEGIZE → (PLAN → EXECUTE → EVALUATE → REFLECT)`

The [research principles](principles.md) make research agents principally
responsible for search quality while architecture governs validity. Planning
therefore owns hypothesis selection, experiment substantivity, review of prior
evidence, expected effects, and falsifiers. Execution owns literal realization
of the selected frozen brief. It may not substitute an easier mechanism or
reinterpret the claim; if fidelity cannot be established, it reports
infeasibility.

Research agents design candidate interventions only within the approved
validity envelope. Evaluator behavior, trial counts, seeds, calibration,
statistical thresholds, and promotion rules are outside their role. A possible
defect in one of those controls must be reported for oversight and
reauthorization, never treated as a policy-search opportunity. It is valid to
exhaust every current direction, and preferable to inventing an experiment
whose specificity is not justified by structural analysis or accumulated
evidence.

Parameter-only and narrow local refinements include coefficients, thresholds,
search depths or widths, horizons, boundaries, and feature variants. Planning
must cite the prior experiment being refined, explain why accumulated evidence
has narrowed the live uncertainty to that specificity, and explain why a
broader structural repair is not presently better. Untested and inconclusive
outcomes remain unresolved regardless of a favorable point estimate.

Simplicity is an experiment-admission judgment, not a post-result correction.
Planning compares expected information or improvement with implementation size,
runtime cost, and human audit burden. Execution rechecks that the realized diff
is proportionate to the frozen mechanism and reports infeasibility if faithful
realization is materially more complex than planned. Once an approved
experiment produces valid statistical evidence, simplicity cannot change its
interpretation, promotion decision, or score.

Each stage is a component with a strict input/output contract. A method profile
selects one compatible implementation and an agent pool per agent-driven
component. `serial` uses one agent per pool. `serial-hotseat` uniformly
selects one agent, with replacement, for each component lifecycle.

Installed component IDs resolve through an internal registry that binds each
ID to one stage, contract version, and trusted handler. Unknown or cross-stage
IDs fail during task parsing. This is not third-party plugin execution.

A lifecycle is one complete stage pass, or one EXECUTE substage. Its selected
agent is persisted before the session starts; recovery reuses that choice in a
fresh process attempt. STRATEGIZE, PLAN, and REFLECT draw once per pass.
Implementation, each review round, and each repair attempt draw independently
from the EXECUTE pool. SEARCH and EVALUATE remain controller-owned. A draw
never crosses a component boundary.

## Environment evidence

For STRATEGIZE, the environment is an approval-locked, read-only codebase
reference. Approval records its repository commit, hashes selected files, and
materializes them under the task's `environment/` directory. The strategist may
inspect that implementation, public interfaces and rules, documentation, and
approved probes. It does not receive the champion, candidate history, or private
evaluator. It derives desired policy behavior from environment observations;
policy-specific diagnosis belongs to EXECUTE or post-trial REFLECT.

## Agent backends and certification

Agent definitions select a backend adapter, model, and settings independently
of components. `codex-cli` is the verified adapter in this release. Other
providers can be added without changing component contracts.

Certification belongs to an adapter version and capability suite, not to a
provider forever. An unverified adapter may be used for development only when
the approved method explicitly accepts weaker isolation. Approval preserves its
then-current attestation without placing mutable certification in the semantic
method hash. If that adapter later passes conformance, new sessions are
`verified`; old artifacts keep the certification recorded when they ran.

Conformance covers fresh sessions, strict structured output, workspace
boundaries, tool execution, user-configuration suppression, network mediation,
and provenance. arctl must not claim a future Claude Code, Pi, Cursor, OpenClaw,
or other adapter is verified until that concrete adapter and version pass it.

## Search evolution

This release has exactly one official champion and does not run experiments,
branches, or the champion/candidate arms concurrently. Within one arm, the
controller partitions the ordered case batch across at most 16 isolated subject
workers and restores original case order before scoring. A future beam method
must retain one official champion per generation. One width `K` limits both
evaluated branches and retained alternatives; at most `K` positive-mean
non-winners are retained in descending order of effect estimate, lower bound,
then stable branch ID. A retained policy may seed a later branch, but descendants
are evaluated against the current official champion.

EVALUATE may select another approved protocol implementation in the future,
while the controller continues to own private seeds and data, evidence
validation, budgets, and promotion.

Public probes declare how many complete paired-trial equivalents one
probe represents. Direct setup generates the probe as a denied, frozen harness
over fixed synthetic public inputs and review checks that it exercises the
public subject path. Compilation and import checks remain useful public checks,
but are not runtime evidence. Before candidate review, arctl times the approved
probe and projects official runtime. A compile-only probe is reported as
unavailable rather than projected. Likely overruns are visible to the
implementer, reviewer, and operator but do not reject the candidate or alter
approved trials.

Implementation reports retain a structured, agent-reported verification
disclosure. Each frozen requirement has a stable identifier and may cite
targeted probe records containing the probe's purpose, exact command, observed
outcome, and concise evidence. Candidate repairs use the same contract so the
final disclosure describes post-repair verification rather than relying on the
original implementation report. arctl validates the references and preserves
the raw execution transcript, but does not rerun these commands in the
controller or require candidate-owned permanent tests. This record supports
human audit and replay; it is not independent evidence that the proposed
mechanism caused an official result.

Outside a frozen experiment, managed Python processes disable bytecode writes.
After an experiment request is frozen, its checks, evaluator stages, subject
workers, arms, and reflection share one cache under
`experiments/<ID>/runtime/python-bytecode/<cache-tag>`. Separate experiments
never share a namespace. Before candidate validation, arctl invalidates only
the mutable worktree's mirrored cache subtree. After durable publication, a
scoped transactional cleanup removes the experiment cache and recognized
scratch debris. Canonical untracked `__pycache__/*.pyc` files that bypass these
controls are discarded at writable lifecycle boundaries and recorded by stage
in `runtime-artifacts.public.json`; manual `arctl gc` removes only canonical,
recognized cache targets.

Public results retain the promotion `decision` and add independent status axes.
`operational_status` records whether execution completed; `scientific_status`
records supported, contradicted, inconclusive, or untested evidence.
Deterministic operational assessments explain scoreless failures without asking
a model to interpret absent evidence.
