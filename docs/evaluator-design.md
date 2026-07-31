# Evaluator design boundary

An arctl evaluator defines the task-specific experiment. arctl freezes and
enforces that definition; it does not certify that the definition is
scientifically correct.

## What arctl enforces

- The approved evaluator commit, manifest, commands, schemas, and limits do not
  change during a task.
- Champion and candidate receive the same ordered batch exactly once.
- Calibration, primary, and suspect seeds do not overlap.
- Subject results match the approved schema and declared trial count.
- Evidence contains finite, direction-normalized effects and an internally
  coherent one-sided lower bound.
- Only approved aggregate telemetry is published.
- Only controller code decides and promotes.

Passing these checks means the approved protocol was followed. It does not mean
the uncertainty method has nominal coverage or that uncontrolled variation is
absent.

## What the evaluator author must justify

The manifest must describe the real independent unit. Rows are not independent
trials when a subject carries score-affecting state across them. The uncertainty
method must match the declared dependence, score distribution, and statistic.

The author must also explain:

- how a controller seed becomes a case and which randomness remains;
- whether the seed is a legitimate subject observation;
- the direction-normalized effect and what a positive value means;
- known uncontrolled variation and concrete mitigations;
- why the fixed trial count or calibration criterion is adequate;
- any suspect trigger, its approved reason codes, and the pathology it targets.

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
