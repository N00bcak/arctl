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

Normal operation uses `run`, `status`, `stop`, `report`, and `inspect`.
Interactive `run` shows the experiment FSM, comparison substages, and live
stage timing. `status` names the experiment that promoted the current champion;
`report` presents compact evidence and dossier IDs under one printed dossier
root. Every published experiment receives a public-only Markdown dossier under
the task’s `reports/experiments/` directory. Use `--json` for AI orchestration.

Task-v4 approvals lock the public environment code, interface, documentation,
policy-free probes, and research method. A strategy session reads only the
environment snapshot and derives successful-policy behaviors from cited
observations. A read-only planner compares those behaviors with the latest
accepted champion and freezes one experiment brief; a fresh implementer may
only realize that brief. The implementer publishes a requirement-by-requirement
audit and may claim completion only when every frozen obligation is verified.
Task-v3 files remain supported with their legacy single-agent assignments.

`serial-v1` assigns one agent to each agent-driven component.
`serial-hotseat-v1` instead draws from an approved component-local pool for
each stage lifecycle and persists the draw for recovery. Exhausting every
current direction refreshes the environment strategy rather than ending the
search.

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
create a Codex sandbox:

```bash
ARCTL_HOST_SANDBOX_TEST=1 python -m unittest tests.test_sandbox_host -v
```
