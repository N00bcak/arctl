# Evaluator design boundary

An arctl evaluator defines the task-specific experiment. arctl freezes and
enforces that definition; it does not certify that the definition is
scientifically correct.

Under the [research principles](principles.md), these are separate
responsibilities. A statistical and experimental-design specialist may propose
the protocol from the objective, repository evidence, risk tolerance, and
resource ceiling. The human checks and authorizes that proposal. The resulting
architecture then freezes and enforces it without requiring the human to
participate in each experiment. Authorization accepts the evaluator's explicit
assumptions and limitations; it is not independent proof of its mathematics.

Research agents optimize policies inside this approved envelope. If one finds a
possible evaluator defect, the valid response is to surface it, stop affected
research, and seek human review or reauthorization. Exploiting the defect,
changing the evaluator, or compensating for it inside a candidate is not a
research experiment.

## What arctl enforces

- The approved evaluator commit, manifest, commands, schemas, and limits do not
  change during a task.
- Champion and candidate receive the same ordered batch exactly once.
- Calibration, primary, and suspect seeds do not overlap.
- Subject results match the approved schema and declared trial count.
- Evidence contains finite, direction-normalized effects and an internally
  coherent one-sided lower bound.
- Every declared primary telemetry metric is present with its approved semantic
  role and arm shape; paired deltas are controller-derived.
- Only controller code decides and promotes.

Passing these checks means the approved protocol was followed. It does not mean
the uncertainty method has nominal coverage or that uncontrolled variation is
absent.

## Optional candidate methodology review

A task may impose a candidate-review contract before an experiment or official
seed reservation exists. Approval-locked deterministic commands catch obvious
capability or interface violations, then a fresh read-only execution session
reviews the uncommitted candidate diff. One configured fresh repair may address
cited findings before checks and semantic review repeat. A repeated violation
is recorded as a research miss and never reaches the evaluator.

The reviewer reports only a summary and actionable findings. The controller
derives `pass` from an empty findings set and `fail` otherwise, so the response
cannot contain a contradictory verdict.

This gate is deliberately outside the evaluator's mathematics. It helps govern
cooperative research agents and provides auditable admission reasoning, but
static and model review do not make arbitrary executable policy code safe
against an adversarial submitter.

## What the evaluator author must justify

The manifest must describe the real independent unit. Rows are not independent
trials when a subject carries score-affecting state across them. The uncertainty
method must match the declared dependence, score distribution, and statistic.

The author must also explain:

- how a controller seed becomes a case and which randomness remains;
- how the subject interface keeps the controller seed hidden;
- the direction-normalized effect and what a positive value means;
- known uncontrolled variation and concrete mitigations;
- why the fixed trial count or calibration criterion is adequate;
- any suspect trigger, its approved reason codes, and the pathology it targets;
- each public diagnostic's unit, scope, favorable direction, and whether it
  measures outcome, mechanism, safety, implementation, or uncertainty.

Manifest v3 requires semantic telemetry descriptors. A paired metric reports
champion and candidate values; a comparison metric reports one aggregate
value. Missing declared metrics, nulls, and undeclared aggregates invalidate
the evidence. An evaluator may deliberately declare no telemetry, but arctl
then warns that it cannot perform causal post-trial reflection.

For new automatic tasks, arctl runs the frozen champion once at the approved
ladder ceiling. The evaluator reports one non-negative scalar diagnostic for
every nested prefix; arctl applies the approved maximum and freezes the
smallest stable passing suffix. If the ceiling misses the target, arctl uses it
only with a persistent warning.

This champion-only pilot can size an approved baseline-precision property. It
does not know the paired-difference variance of future candidates and therefore
does not prove candidate power, remove systematic bias, or prove coverage. A
suspect test can investigate one declared warning sign. It is not a general
multiple-testing correction.

After a valid verdict, a separate read-only model session interprets declared
telemetry against the selected environment-derived behavior, precommitted
policy mechanism, viability argument, prior-evidence review, and falsifiers.
It also records whether the implementation expressed the behavior and any
policy-specific findings that later executors should consider. Its reflection is
advisory: it can recommend retention, refinement, later review, implementation
audit, or abandonment, but cannot change the verdict or promotion. If that
required session fails, valid evidence is preserved and later research stops
until a fresh reflection attempt succeeds.

Reflection schema v4 fixes the selected behavior and known history IDs while
recording only material telemetry interpretations, concrete implementation
concerns, and a concise disposition. Older published reflection versions remain
readable and render unchanged.

## Reference conformance fixtures

`tests/fixtures/evaluators/` contains three contract examples:

- paired arithmetic-mean difference with a one-sided standard-error bound;
- paired binary win rate among discordant cases with an exact one-sided
  Clopper–Pearson bound;
- median paired difference with a distribution-free sign-test order-statistic
  bound.

They demonstrate that the common evidence contract is statistic-agnostic.
They are teaching and regression fixtures, not universal recommendations.

## Pre-approval review

Before approval, a human should be able to answer:

1. What is one independent trial?
2. What exact event or quantity is compared?
3. What does the lower bound claim, and under which assumptions?
4. Which randomness is paired, controlled, or still uncontrolled?
5. What would make an apparent improvement untrustworthy?
6. Which aggregates may later research sessions see?

If those answers require reading evaluator source, the manifest explanation is
not yet adequate.
