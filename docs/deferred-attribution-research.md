# Deferred research: attribution of improvements

## Status

**DEFERRED — insufficient product and protocol maturity.**

This work is not part of the current MVP. It changes no runtime behavior,
research contract, evaluator schema, promotion rule, or experiment budget.

The deferral is governed by the [research principles](principles.md), especially
human-sensible honesty and contract-following. Current reports must preserve the
distinction between package-level outcome evidence, evidence that the intended
behavior activated, and evidence for a causal mechanism. Naming those stronger
standards does not claim that the current runtime implements them.

## Problem

A paired candidate-versus-champion comparison can establish that an exact
candidate package improved the approved outcome. It does not necessarily show
which component produced the gain or establish the candidate's proposed causal
explanation. Fixed task telemetry may also be unable to observe mechanisms that
were not anticipated when the evaluator was approved.

Future design work should distinguish three strengths of attribution:

1. **Intervention attribution:** one coherent candidate intervention caused the
   measured package difference under the approved comparison.
2. **Behavioral attribution:** approved diagnostics also show that the intended
   policy behavior activated.
3. **Scientific-mechanism attribution:** a control or ablation distinguishes the
   proposed causal mechanism from other changes.

The MVP currently guarantees none of these beyond faithfully recording the
frozen candidate, its diff, and its aggregate outcome evidence. Reflection may
discuss mechanisms, but must treat unsupported causal explanations as
hypotheses.

## Candidate future design

- Freeze one coherent intervention boundary and activation condition in the
  original research request. A large implementation may remain coherent; the
  boundary concerns independently testable ideas, not line count.
- Permit a tightly bounded policy-diagnostic channel whose field names,
  meanings, types, and limits are frozen before execution. Prefer values
  derived by trusted task code; label policy-reported values as untrusted
  observations that cannot affect promotion.
- Have independent review check the candidate against the champion for bundled
  interventions, diagnostic fidelity, RNG perturbation, and undeclared coupled
  behavior.
- When stronger causal evidence is warranted, group an independently frozen and
  evaluated control or ablation as a subexperiment under the original
  experiment's dossier. Do not merge distinct estimands into one opaque result.
- Give every official comparison its own reservation, seeds, review, evidence,
  and recovery record. Grouping must not make additional evaluation free or
  invisible to resource limits.
- Support direction-aware evidence before interpreting ablations: removing a
  useful mechanism should produce a confidently negative candidate-minus-
  champion effect, which the current lower-bound-only decision interface cannot
  establish rigorously.
- Report outcome status separately from attribution status. Statistical
  promotion must never imply that the proposed mechanism was demonstrated.

Ideas discussed but not adopted include promoting a proven package before an
immediate explanatory follow-up, grouping that follow-up with its parent, and
charging every official comparison to a reserved budget slot. These are design
directions, not current product commitments.

## Risks and unresolved questions

- Policy-emitted diagnostics can be inaccurate, strategically misleading, or
  behavior-changing; bounded output and semantic review reduce but do not
  eliminate this risk.
- Instrumentation can consume randomness or alter timing and control flow.
- Requiring causal ablations for every improvement could sharply reduce search
  throughput and encourage performative explanations.
- Complex algorithmic changes do not always have one defensible causal unit.
- Multi-arm, sequential, and retrospective attribution designs have different
  statistical and operational costs.
- The appropriate default standard—intervention, behavioral, or scientific
  mechanism attribution—has not been chosen.

## Revisit criteria

Reconsider this feature only after:

- the strategy, planning, implementation, review, evaluation, and reflection
  loop is stable on MNIST and at least one other substantive domain;
- real traces show that missing attribution materially harms later research
  decisions;
- the evidence protocol can rigorously interpret negative-direction ablations;
  and
- a safe bounded diagnostic interface has been prototyped and empirically
  checked for fidelity and usefulness.

Until then, arctl should preserve the narrower honest claim: an accepted result
supports the evaluated candidate package, not an untested explanation of why it
worked.
