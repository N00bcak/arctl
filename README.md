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
Interactive `run` narrates safe experiment transitions. Every published
experiment receives a public-only Markdown dossier under the task’s
`reports/experiments/` directory; `report` and `inspect` print its path.
Use `--json` for AI orchestration.

Research sessions are offline. They may read the runtimes named by approved
public checks and probes, but cannot read evaluator or task-private artifacts.

See [the evaluator design boundary](docs/evaluator-design.md) and
[empirical validation protocols](docs/empirical-validation.md).
