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
stage timing. Every published
experiment receives a public-only Markdown dossier under the task’s
`reports/experiments/` directory; `report` and `inspect` print its path.
Use `--json` for AI orchestration.

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

See [the evaluator design boundary](docs/evaluator-design.md) and
[empirical validation protocols](docs/empirical-validation.md).

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

The two host-sandbox boundary tests are opt-in because they require a host
that can create a Codex sandbox:

```bash
ARCTL_HOST_SANDBOX_TEST=1 python -m unittest tests.test_sandbox_host -v
```
