# arctl

A small, faithful AutoResearch controller for local Git repositories. Research
may edit approved paths and use approved public tools, but controller-owned
paired evaluation decides what wins.

Install locally:

```bash
./install.sh
. .venv/bin/activate
arctl doctor
```

## Supported hosts

Arctl supports Linux and macOS on the architectures supported by Python 3.11+
and the installed Codex CLI. Windows and WSL are intentionally unsupported.
The host must provide Git, `uv`, and Codex CLI. Linux online dependency
provisioning additionally requires Bubblewrap; macOS uses its built-in Seatbelt
sandbox and `libproc` process metadata.

On macOS, run arctl from a normal Terminal, iTerm, or unsandboxed CI process.
A parent application sandbox can prevent Codex from creating the nested
Seatbelt profile arctl requires. `arctl doctor --json` reports this as a profile
failure with the remediation instead of treating macOS as unsupported.

Create a visible guided workspace around an existing Python repository:

```bash
arctl init .
arctl setup
arctl approve
arctl run
```

`init` locally clones the supplied clean repository into
`<name>-research/subject`, creates independent `environment` and `evaluator`
repositories beside it, and leaves the source repository untouched. The shell
workspace is not itself a Git repository. It prints an absolute, location-safe
`setup` command. `setup` inspects only public repository material and presents
up to three related, cited decisions at a time. Each decision uses explicit
numbered choices (or a custom answer); silence never promotes an agent proposal.
Only consequential choices are recorded as human-confirmed. Arctl derives the
remaining setup with provenance, shows one compact complete-design summary, and
requires authorization before generation. Every option displays the exact value
that selecting it will persist.

The builder writes normal files inside isolated task-owned staging repositories
with network disabled and returns only a compact path/dependency report. A fresh
reviewer covers intent fidelity, editable boundaries, dependencies, trial
independence, scoring, seeds, and runtime before package provisioning. New direct
dependencies are shown in the authorized design; transitive dependencies inherit
that authorization. Direct URLs, VCS/local sources, and alternate sources require
a distinct human decision. Provisioning uses a configured package index and the
resolved lock is bound into setup acceptance. Authorization is completed before
installation, but installation is still confined: Bubblewrap provides the Linux
online sandbox and Codex Seatbelt provides the macOS online sandbox. Offline
provisioning uses the Codex sandbox with networking disabled on both systems.

Controller conformance checks exercise seed-zero and same-reservation repeatability,
sequential state isolation, identity scoring, evidence shape, and unscored failures.
Declared conformance selectively enables different-seed variation and outcome-scoped
arm-label effect reversal; environment roles or move order are never assumed to
be symmetric. Only setup-token acceptance creates or switches to the local setup
branch, copies reviewed owned files, and commits; setup never pushes. It then
generates workspace-level `ARCTL_SETUP.md` as a readable output, not an input.
Normal `approve` remains the separate scientific trust boundary. The setup
specialist owns recommendations for sampling, seeds, calibration, uncertainty,
and telemetry.
Controller-owned comparator freezing, seed non-reuse, decision mapping, and
unscored operational failures are shown as fixed rules, never delegated back to
the human. Unsupported technical choices receive a conservative, explained
recommendation rather than a statistics questionnaire.
The decision record and byte-exact authorized design snapshot are the canonical
setup contracts. Objective, editable paths, environment adapter, outcome statistic,
and finite trial horizon are typed rather than rediscovered by the builder. Requested
guarantees that arctl cannot enforce require an explicit decision. Generated
hooks and typed task/evaluator designs are validated and independently reviewed
before dependency installation. Builders describe Python modules or relative
scripts while arctl supplies the interpreter and working directory.
Telemetry results must match their declared `{champion, candidate}` or `{value}`
shape. One automatic repair receives all contract findings together. A
ceiling-sized setup batch must then pass the real
prepare, subject, calibration, scoring, evidence, and telemetry protocol. A
failed repair or conformance run makes the next invocation generate afresh.
When static review proves that two authorized guarantees conflict, the next
`setup` invocation reopens one cited human decision instead of blindly rebuilding.
Authorizing the revised design archives the previous signed design and
authorization pair, clears only derived build/review state, and preserves every
failed attempt as evidence.
The acceptance token also covers the authorization bundle, task draft, and
owned-file list. If reviewed artifacts change, `arctl setup` records the edit,
reruns the affected checks and setup review, and issues a new token. Acceptance
stages only reviewed owned paths; unrelated unstaged files remain uncommitted
and a non-empty index is rejected.
Use `--answers FILE --json` for the revisioned non-interactive state machine.
Inspection, building, and setup review are offline; `--offline` additionally
requires cached dependencies. Manual task authoring remains available through
`arctl task create SOURCE`.

Normal operation uses `run`, `status`, `stop`, `report`, and `inspect`.
Interactive `run` shows the experiment FSM, comparison substages, and live
stage timing. `status` names the experiment that promoted the current champion;
`report` presents compact evidence and dossier IDs under one printed dossier
root. Every published experiment receives a public-only Markdown dossier under
the task’s `reports/experiments/` directory. Use `--json` for AI orchestration.

Task-v5 approvals lock the public environment code, interface, documentation,
policy-free probes, and research method. A strategy session reads only the
environment snapshot and derives successful-policy behaviors from cited
observations. A read-only planner compares those behaviors with the latest
accepted champion and freezes one experiment brief; a fresh implementer may
only realize that brief. The implementer publishes a requirement-by-requirement
audit and may claim completion only when every frozen obligation is verified.
Its declared probe size also gives an advisory official-runtime projection
before review. Task-v3/v4 files remain supported.

`serial-v1` assigns one agent to each agent-driven component.
`serial-hotseat-v1` instead draws from an approved component-local pool for
each stage lifecycle and persists the draw for recovery. Exhausting every
current direction refreshes the environment strategy rather than ending the
search.

Evaluation keeps experiments and the champion/candidate arms serial, while each
arm partitions its ordered cases across isolated subject workers. Select a cap
from 1 to 16 with `arctl run TASK --workers N`; the default is 16. The controller
reassembles worker results in original case order before scoring, so changing the
worker cap changes scheduling rather than the scientific comparison.

Tasks may also require a pre-trial candidate review contract. Approval-locked
deterministic checks catch obvious violations, then a fresh read-only execution
session reviews the candidate diff and implementation audit. One configured
repair re-audits the complete brief before a second review; repeated failure is
a research miss and consumes no experiment or official trial seeds. This is
cooperative methodology enforcement, not hostile-code isolation.

Manifest-v3 evaluators describe publishable telemetry by meaning, arm scope,
role, unit, and direction. After each valid verdict, a fresh read-only session
uses those aggregates to assess the proposed mechanism and implementation and
to recommend later research. This reflection is public and advisory; only the
fixed statistical rule can decide or promote.

Results retain the promotion decision and separately report operational and
scientific status, so a scoreless implementation failure remains visibly
untested rather than looking like negative evidence.

Automatic trial sizing is a one-time controller-run champion pilot over an
approved ladder. The evaluator calculates the task-specific diagnostic; arctl
selects and freezes the smallest stable passing rung.

Research sessions are offline. They may read the runtimes named by approved
public checks and probes, but cannot read evaluator or task-private artifacts.

Documentation:

- [Current design and workflow diagrams](docs/design.md)
- [Versioned research methods and agent backends](docs/research-methods.md)
- [Complete CLI command reference](docs/cli-reference.md)
- [Evaluator design boundary](docs/evaluator-design.md)
- [Empirical validation protocols](docs/empirical-validation.md)
- [Normative MVP specification](arctl_light_mvp_spec_simple.md)

## Development

Run the portable test suite from the repository root:

```bash
python -W error -m unittest discover -v
python -m compileall -q src tests
bash -n install.sh
```

The suite creates temporary subject repositories and uses portable evaluator
fixtures from `tests/fixtures/`. `test_tris/` is an ignored local lab and is
not required to build, test, or install arctl.

Host-sandbox boundary tests are opt-in because they require a host that can
create a Codex sandbox. Run them from an unsandboxed host shell; the suite also
performs a small network-enabled `uv sync` inside the dependency profile:

```bash
ARCTL_HOST_SANDBOX_TEST=1 python -m unittest tests.test_sandbox_host -v
```

The macOS host merge gate additionally requires `arctl doctor --json` to pass.
If doctor reports `sandbox_apply: Operation not permitted`, leave the parent
sandbox and rerun it from Terminal. Missing `uv`, Codex, or Bubblewrap is
reported as a prerequisite/backend failure before setup creates a new attempt.
