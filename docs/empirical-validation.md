# Empirical validation protocols

These protocols evaluate claims that controller conformance tests cannot prove.
They are plans for a later evaluation phase, not release guarantees.

They also test whether the implementation realizes the [research
principles](principles.md). In particular, search-wide false-promotion behavior
tests architectural validity beyond one paired comparison, while human audit
comprehension tests human-sensible honesty and exception-driven oversight.
These are validation obligations for the principles, not optional presentation
polish, but the protocols below remain future work rather than current
guarantees.

## Calibration and interval behavior

For each reference and real evaluator, simulate data-generating processes with
known null and positive effects. Include the declared distribution as well as
heavy tails, ties, heteroskedastic pairs, and the plausible dependence described
by the manifest.

Repeat calibration and one frozen comparison on independent seeds. Record the
selected trial count, lower-bound coverage under the null, promotion rate,
power at practically relevant effects, and runtime. Evaluate departures from
the declared assumptions separately rather than averaging them away.

## Adaptive-search false promotions

Construct null tasks where every reachable candidate has zero true effect.
Repeat complete bounded research runs, preserving their adaptive sequence.
Measure the fraction of runs with at least one promotion, promotions per tested
candidate, and how the rate changes with the experiment ceiling. This estimates
task-wide behavior that per-comparison intervals do not bound.

Repeat on tasks with known beneficial candidates to measure discovery rate and
time-to-promotion. Report null safety and discovery power together.

## Suspect-trigger value

Create evaluator-specific pathologies that the approved trigger claims to
detect, such as timeout shifts, seed-sensitive wins, or unstable resource use.
Compare complete runs with and without the suspect policy. Record trigger
frequency, caught false promotions, rejected genuine improvements, and added
cost. A trigger is useful only if it targets a demonstrated failure mode.

## Real-repository usefulness

Select external repositories with deterministic setup, meaningful public
development feedback, and an independently maintained private evaluator. For
each task record setup time, failed launches, useful-candidate rate, promotion
rate, model/runtime cost, and whether an expert judges the promoted diff
mechanistically credible.

Do not reuse a repository to tune both the harness and its final evaluation
without disclosing that reuse.

## Human audit comprehension

Give a novice only normal CLI output and the generated dossier. Ask them to
identify the hypothesis, exact code change, public checks, official statistic,
effect, uncertainty, decision, champion outcome, and stated limitations.

Success requires correct answers without opening JSON or private artifacts.
Also record time-to-answer, mistaken trust in researcher claims, and accidental
attempts to access private evidence.
