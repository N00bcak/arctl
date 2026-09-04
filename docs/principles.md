# Research principles

This document is the normative source for arctl's role boundaries and research
values. It governs how the implementation contract should be designed,
interpreted, and reviewed; it does not by itself claim that every desirable
enforcement mechanism already exists. The other design documents describe the
current mechanisms and must state their limitations plainly.

## Roles and boundaries

**Architecture** comprises the controller, approved evaluator contract,
isolation boundaries, evidence records, and promotion machinery. It governs
search validity: it fixes which evidence is admissible, keeps evaluation outside
the researcher's control, and makes only reproducible, authorized state changes.
An approved protocol can still contain a scientific mistake, so enforcement of
the protocol is not proof that its mathematics is correct.

**Research agents** principally own search quality. They derive useful policy
behaviors, select hypotheses, design candidate interventions within the approved
validity envelope, implement frozen briefs, and interpret public evidence. They
must preserve validity-adjacent obligations, but required validity invariants
must not depend on their goodwill or self-assessment.

**The human** initiates the task, checks and authorizes the proposed architecture
and research boundaries, oversees reported evidence and exceptions, and may stop
or reauthorize the work. Once the task is running, the human is an overseer, not
a routine strategy, implementation, or experiment-approval stage. Ordinary
correctness must not depend on continuous human participation.

These responsibilities overlap at their boundaries without becoming
interchangeable. A setup specialist may propose a statistical design, the human
checks and authorizes it, and the architecture freezes and enforces it. A
research agent designs each candidate experiment inside that frozen envelope.
When an agent notices a possible validity defect, it reports the defect for
human review instead of modifying or working around the protocol.

## Architectural principles

### Human-sensible honesty

Reports must make the supported claim understandable to a diligent human. They
distinguish facts, inferences, assumptions, and unknowns; operational completion,
scientific evidence, and promotion; and package-level outcome evidence,
behavioral activation, and causal-mechanism attribution. A technically present
caveat is insufficient when surrounding language still invites a stronger
conclusion.

The architecture should make evaluator gaming unavailable rather than relying
on a request that an agent behave well. Limitations and unsupported guarantees
remain visible at approval and in normal reports.

### Reproducibility

Champion and candidate receive the same ordered public cases within a paired
comparison. Recovery reuses the exact immutable reservation and valid saved
outputs; it never redraws a failed or inconvenient batch. Calibration, primary,
suspect, and separate experiment reservations use fresh, non-overlapping seeds.
Reusing one visible or inferable seed set throughout the search would turn it
into an optimization target, not improve reproducibility.

### Simplicity

Keep the trusted core, contracts, state transitions, and ordinary audit path as
small and unambiguous as the required guarantees permit. Every added mechanism
or contract obligation should protect a named invariant or remove a concrete
ambiguity. Brevity does not justify weakening a trust boundary, while defensive
complexity does not justify an interface that a human cannot sensibly audit.

## Research-agent principles

### Substantivity

Prefer experiments that address structural policy faults or test material
mechanisms. A narrow heuristic, coefficient, or boundary change is appropriate
only when accumulated evidence has genuinely narrowed the live uncertainty to
that choice and the expected information is worth an official comparison.
Exhausting a direction is better than manufacturing a weak candidate merely to
continue the loop.

### Integrity

Optimize the policy, never the evaluator or measurement process. Research agents
must not propose or make changes to evaluator behavior, seeds, trial counts,
thresholds, private evidence, or promotion rules. A suspected evaluator defect
is a reason to report, stop, and seek reauthorization, not an opportunity to
exploit the defect or silently compensate for it in the policy.

### Contract-following

Treat the frozen experiment brief literally. Preserve every stated behavioral,
fallback, validation, and fidelity obligation; do not broaden, simplify,
reinterpret, or replace the mechanism after it is selected. When faithful
implementation cannot be established, report infeasibility rather than submit a
different experiment under the original claim.

Interpretation follows the same rule: an outcome comparison supports the exact
evaluated package. It supports behavioral activation or a causal explanation
only when the experiment collected evidence capable of establishing that
stronger claim.

## Resolving conflicts

Search validity, human-sensible honesty, and agent integrity are constraints, not
quantities to trade for more experiments or higher scores. Within those
constraints, prefer substantive search, reproducibility, simplicity, and then
speed. Human oversight is exception-driven and audit-oriented; this ordering
does not introduce approval checkpoints between ordinary experiments.
