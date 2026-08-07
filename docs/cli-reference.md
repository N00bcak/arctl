# arctl command reference

This file mirrors the built-in CLI help. Regenerate it with `.venv/bin/python tools/generate_cli_reference.py`.

## `arctl -h`

```text
usage: arctl [-h] [--debug] COMMAND ...

Run faithful, statistically cautious AutoResearch loops against local Git repositories.

arctl separates untrusted research from an approval-locked evaluator, preserves every
official comparison, and promotes candidates only through the evaluator's fixed rule.

options:
  -h, --help  show this help message and exit
  --debug     show controller tracebacks instead of concise failure reports

commands:
  COMMAND
    doctor    check Git, Codex, sandbox, runtime, network, and cleanup support
    init      create a guided Python research workspace
    setup     discover, build, review, and accept a Python task workspace
    approve   preview or confirm the task, evaluator, environment, and champion lock
    run       calibrate if needed, search for candidates, and run official experiments
    status    show the task's current controller state and resumability
    stop      request a safe idempotent stop
    report    list completed experiments and public dossier paths
    history   search the public strategy and experiment exploration ledger
    inspect   inspect one experiment and its safe artifacts

Typical workflow:
  arctl doctor
  arctl init --repo /path/to/subject
  arctl setup TASK
  arctl approve TASK
  arctl approve TASK --confirm TOKEN
  arctl run TASK --max-experiments 3
  arctl status TASK

Complete command forms:
  arctl doctor [--json]
  arctl init (--repo PATH | --new-repo PATH) [--workspace PATH]
             [--task-id TASK] [--json]
  arctl setup [TASK] [--answers FILE] [--allow-network]
                     [--accept TOKEN] [--json]
  arctl approve [TASK] [--confirm TOKEN] [--json]
  arctl run [TASK] [--max-experiments N] [--retries N]
                    [--retry-delay SECONDS] [--json]
  arctl status [TASK] [--json]
  arctl stop [TASK] [--json]
  arctl report [TASK] [--json]
  arctl history [TASK] [--query TEXT] [--path GLOB]
                        [--decision VALUE] [--json]
  arctl inspect [TASK] [EXPERIMENT] [--artifacts] [--json]

Task IDs may be omitted when the current directory identifies exactly one task.
Use --json on any command for the stable AI-orchestration response. Use --debug to
show controller tracebacks. Run `arctl COMMAND -h` for command-specific details.
```

## `arctl doctor -h`

```text
usage: arctl doctor [-h] [--json]

Run non-destructive installation and sandbox capability checks required by arctl.

options:
  -h, --help  show this help message and exit
  --json      emit one stable machine-readable JSON object

Run this after installation or when a sandbox/runtime preflight fails.
```

## `arctl init -h`

```text
usage: arctl init [-h] [--json] [--repo PATH] [--new-repo PATH] [--workspace PATH] [--task-id TASK]

Create visible setup storage for an existing or new Python subject repository.

options:
  -h, --help        show this help message and exit
  --json            emit one stable machine-readable JSON object
  --repo PATH       existing clean local Git worktree containing the policy to improve
  --new-repo PATH   create a visible workspace and new subject repository at PATH
  --workspace PATH  workspace path; defaults to a visible sibling of an existing repo
  --task-id TASK    task ID; defaults to the repository directory name

Example:
  arctl init --repo . --task-id routing-policy
```

## `arctl setup -h`

```text
usage: arctl setup [-h] [--json] [--answers FILE] [--allow-network] [--accept TOKEN] [TASK]

Run resumable pre-approval setup. Discovery is read-only; generated code and evaluator
remain drafts until explicit setup acceptance and normal task approval.

positional arguments:
  TASK             task ID; omit when the current repository identifies exactly one task

options:
  -h, --help       show this help message and exit
  --json           emit one stable machine-readable JSON object
  --answers FILE   JSON object mapping every returned setup question ID to an answer
  --allow-network  allow uv to fetch declared Python dependencies during setup
  --accept TOKEN   accept the verified setup trees and create their local Git commits

AI operators should use --json, submit the returned question IDs with --answers,
and obtain permission before using --accept.
```

## `arctl approve -h`

```text
usage: arctl approve [-h] [--json] [--confirm TOKEN] [TASK]

Without --confirm, validate the draft and print the human approval table and token.
With --confirm, lock the exact task, evaluator commit, environment sources, and initial champion.

positional arguments:
  TASK             task ID; omit when the current repository identifies exactly one task

options:
  -h, --help       show this help message and exit
  --json           emit one stable machine-readable JSON object
  --confirm TOKEN  confirmation token printed by the approval preview

Approval is a human trust boundary. An AI operator must not confirm without explicit permission.
```

## `arctl run -h`

```text
usage: arctl run [-h] [--json] [--max-experiments N] [--retries N] [--retry-delay SECONDS] [TASK]

Resume TASK safely and run a bounded number of new experiments. Existing process records,
comparisons, reflections, and failed candidate reviews are recovered before new work starts.

positional arguments:
  TASK                   task ID; omit when the current repository identifies exactly one task

options:
  -h, --help             show this help message and exit
  --json                 emit one stable machine-readable JSON object
  --max-experiments N    maximum experiments for this invocation; cannot exceed the approved task
                         limit
  --retries N            additional consecutive transient attempts (default: 0)
  --retry-delay SECONDS  fixed interruptible delay between transient retries (default: 60)

Retries apply only to recognized transient Codex and pre-trial public-process failures.
Started calibration and official comparison commands are never retried.

Example:
  arctl run TASK --max-experiments 3 --retries 2 --retry-delay 60
```

## `arctl status -h`

```text
usage: arctl status [-h] [--json] [TASK]

Show approval, calibration, frozen trial count, champion, active work, latest result,
search progress, stop state, experiment-limit state, and the relevant log path.

positional arguments:
  TASK        task ID; omit when the current repository identifies exactly one task

options:
  -h, --help  show this help message and exit
  --json      emit one stable machine-readable JSON object
```

## `arctl stop -h`

```text
usage: arctl stop [-h] [--json] [TASK]

Create TASK's persistent stop request. The active managed process is terminated at a safe
boundary; reserved evidence is preserved and never silently redrawn.

positional arguments:
  TASK        task ID; omit when the current repository identifies exactly one task

options:
  -h, --help  show this help message and exit
  --json      emit one stable machine-readable JSON object

Calling stop repeatedly is safe. Ctrl-C during `arctl run` requests the same stop.
```

## `arctl report -h`

```text
usage: arctl report [-h] [--json] [TASK]

Show the completed experiment history, decisions, aggregate effects, and immutable public
Markdown dossier paths. Private cases, seeds, and raw outputs are not disclosed.

positional arguments:
  TASK        task ID; omit when the current repository identifies exactly one task

options:
  -h, --help  show this help message and exit
  --json      emit one stable machine-readable JSON object
```

## `arctl history -h`

```text
usage: arctl history [-h] [--json] [--query TEXT] [--path GLOB] [--decision VALUE] [TASK]

Search immutable public exploration entries used by later candidate executors.

positional arguments:
  TASK              task ID; omit when the current repository identifies exactly one task

options:
  -h, --help        show this help message and exit
  --json            emit one stable machine-readable JSON object
  --query TEXT      require all words in the entry's searchable public text
  --path GLOB       match at least one candidate changed path
  --decision VALUE  match an exact decision such as ACCEPT, REJECT, ARCHIVE, or INVALID

Filters combine with AND. Text matching is case-insensitive; --path accepts shell-style globs.
Example:
  arctl history TASK --query lookahead --decision REJECT
```

## `arctl inspect -h`

```text
usage: arctl inspect [-h] [--json] [--artifacts] [TASK] [EXPERIMENT]

Show one experiment's hypothesis, decision, aggregate comparisons, commits, and public dossier.
When TASK is inferable, a lone numeric positional argument is treated as EXPERIMENT.

positional arguments:
  TASK         task ID; omit when the current repository identifies exactly one task
  EXPERIMENT   positive experiment number; defaults to the latest experiment

options:
  -h, --help   show this help message and exit
  --json       emit one stable machine-readable JSON object
  --artifacts  include the safe public/private artifact inventory

Examples:
  arctl inspect TASK 4
  arctl inspect 4 --artifacts
```
