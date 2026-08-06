# Research methods

arctl fixes the semantic loop but version-controls its implementation:

`STRATEGIZE → (PLAN → EXECUTE → EVALUATE → REFLECT)`

Each stage is a component with a strict input/output contract. A method profile
selects one compatible implementation and an agent pool per agent-driven
component. `serial-v1` uses one agent per pool. `serial-hotseat-v1` uniformly
selects one agent, with replacement, for each component lifecycle.

The selected agent owns that whole lifecycle and the choice is persisted before
the session starts. Recovery reuses the choice but starts a fresh process
attempt. STRATEGIZE, PLAN, and REFLECT draw once per pass. EXECUTE draws
independently for implementation, each review round, and each repair attempt.
SEARCH and EVALUATE remain controller-owned. Hotseating never crosses a
component boundary.

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
of components. `codex-cli-v1` is the verified adapter in this release. Other
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

This release is serial and has exactly one official champion. Its contracts
reserve generation and branch provenance, but do not run work concurrently. A
future beam method must retain one official champion per generation. One width
`K` limits both evaluated branches and retained alternatives; at most `K`
positive-mean non-winners are retained in descending order of effect estimate,
lower bound, then stable branch ID. A retained policy may seed a later branch,
but descendants are evaluated against the current official champion.

EVALUATE may select another approved protocol implementation in the future,
while the controller continues to own private seeds and data, evidence
validation, budgets, and promotion.
