# `arctl` Light MVP Spec
**Status:** build spec | **Language:** Python 3.11+ | **Platform:** Linux  
**Needs:** Git, Codex CLI, Codex sandbox | **Use:** one repo, one task, one experiment at a time

## 1. Goal
`arctl` runs a simple and honest auto-research loop for code.
For each experiment:
1. Start one fresh Codex session.
2. Give it the current champion, the public task, and safe past results.
3. Let it make one code change.
4. Save the change as a Git commit.
5. Test champion and candidate on the same private random-seed primary batch and, only when the approved evaluator flags an apparent win, one fresh suspect-test batch.
6. Let controller code decide the result.
7. Update the champion only when the candidate passes.
> Git, Codex, and a small controller should be enough to run useful code experiments without letting the model control the test, the evidence, or the winner.
`arctl` is not a general agent system, job scheduler, or new sandbox.

## 2. Core rules
1. Git tracks the champion, candidates, history, and rollback.
2. Codex provides fresh sessions and sandbox rules.
3. The private evaluator and its manifest are locked before calibration or experiments start.
4. Research and subject runs cannot read the evaluator.
5. Each experiment uses one fresh Codex session. It is never resumed.
6. Every evaluation uses one best-effort random-seed paired-trial protocol. A deterministic test is `trials: 1`.
7. Every experiment uses one primary trial batch and, only when the approved evaluator flags an apparent win as suspect, at most one fresh suspect-test batch. A batch is chosen once and each subject runs it once. Batches are never redrawn, extended, or selectively retried.
8. The model may suggest. Only `arctl` may test, decide, publish, or promote.
9. The command-line tool must be easy for a human to use and audit.

The controller uses three reusable records:
- `ProcessRun`: one exactly-once process with a saved `started` record and at most one valid output;
- `ComparisonRun`: one immutable paired batch that performs `reserve → prepare → run subjects → score → validate`;
- `Experiment`: one research session, one frozen candidate, one primary `ComparisonRun`, and at most one suspect `ComparisonRun`.

Primary and suspect comparisons use the same commands, schemas, trial count, validation, storage shape, and crash rules. Their only behavioral difference is that a primary comparison may request a suspect comparison and a suspect comparison may not.

## 3. Scope
Included: one trusted local Git repo; one active task and experiment; one approved task file, evaluator commit, and evaluator manifest; fresh Codex sessions; allowed-path checks; public tests and dev probes; controller-made candidate commits; best-effort random-seed paired-trial evaluation; optional one-time trial-count calibration; one optional evaluator-triggered suspect test before promotion; saved experiment files; safe stop, crash recovery, status, inspect, and reports.
Not included: parallel tasks or workers; subagents or resumed sessions; evaluator creation by the model; controller-defined task metrics or statistical methods; adaptive trial extension; search-wide multiple-testing correction or a harness-wide false-positive guarantee; production branch changes; custom containers, VMs, workflow graphs, or plugins; protection from a hostile OS admin or sandbox escape.

## 4. Trust and control
Trusted: user, host OS, Git, `arctl`, approved Codex setup, and the approved evaluator commit and manifest.
Not trusted: model output, candidate code, model-made repo edits, process output, evaluator output before checks, and any model-made score or decision.
Only `arctl` may:
- create the official candidate commit;
- generate the private master seed, derive trial seeds, and choose subject run order;
- start official subject and evaluator runs;
- save official evidence;
- validate standardized comparison evidence and calculate the result;
- publish public feedback;
- update the champion Git ref.

## 5. Git and evaluator
Champion ref:
```text
refs/arctl/<task-id>/champion
```
Candidate ref:
```text
refs/arctl/<task-id>/candidates/<experiment-id>
```
The candidate commit must have the starting champion as its parent.
Reject the candidate if its tree matches the champion, the same tree was already tested against the same champion and evaluator, or it changes blocked files.
Promotion uses Git compare-and-swap:
```bash
git update-ref refs/arctl/$TASK/champion $CANDIDATE $EXPECTED_CHAMPION
```
`arctl` never changes a normal production branch.
The evaluator must live outside the target repo. A separate private Git repo is the normal setup. The task stores one evaluator commit and its manifest approved by the user. Every calibration and official test uses that exact commit and manifest. Changing either means creating a new task.

The manifest contains the frozen subject, preparation, calibration, and scoring argument-vector commands; their schemas and limits; the public telemetry allowlist; the meaning of one trial and any known dependence between trials; the score statistic; the uncertainty method; the seed-to-case procedure; known uncontrolled score variation and attempted mitigations; the optional suspect-test trigger and its allowed reason codes; and, when supported, the automatic calibration policy and ceiling. The same preparation and scoring commands handle both comparison kinds. The approval screen explains these items in plain language. Evaluator commands are data fixed by the approved manifest; evaluator processes may not return new commands or launch subjects.

Every evaluator comparison must use a direction-normalized effect:
```text
effect = 0   means equal performance
effect > 0   means the candidate is better
effect < 0   means the candidate is worse
```
The effect may use any task-appropriate score or units. `arctl` does not interpret those units.

The evaluator must use each trial seed to control score-affecting task randomness directly or through evaluator-derived sub-seeds wherever the environment permits. It must give champion and candidate the same paired case and attempt to bound, reset, and mitigate uncontrolled variation. If a subject intentionally carries score-affecting state across rows in a batch, the manifest must not describe those rows as independent trials; it must identify the actual independent unit and use an uncertainty method consistent with it. Calibration may reduce observed random variation, but neither calibration nor additional trials are claimed to remove systematic bias.

Research and subject sandboxes must not read the evaluator repo, path, commit, controller master seeds, private trial identities, schedules, answers, scores, or evidence. An evaluator may place a seed in subject-visible input only when that seed is a legitimate task observation; otherwise it materializes the case and keeps the generator seed private.

## 6. Fresh session rule
Each experiment starts one new Codex session.
The session may read the public task, rules, allowed paths, a worktree of the champion, public tests, dev probes, the evaluator manifest's approved public statistic and subject-interface description, safe results from completed experiments, and the required experiment JSON format.
The session may not read evaluator code or location, hidden cases or answers, official seeds or schedules, raw subject output, private evidence, private errors, or earlier Codex chat logs.
The session ends before candidate freeze and official test choice. It is never resumed.

## 7. Sandbox profiles
`arctl` uses the sandbox already provided by Codex.
| Profile | Can use | Must not read | Network |
|---|---|---|---|
| `RESEARCH` | candidate worktree, public task data, scratch | evaluator, private results, other experiments | user-approved research access |
| `SUBJECT` | frozen code, runtime, public input, fresh output | evaluator, controller files, history | none |
| `EVALUATOR` | evaluator commit, private case data, saved subject output | writable target repo, Git refs | none |
`arctl doctor` must test file reads, file writes, network access, timeouts, and child-process cleanup before a task runs.

## 8. Task file
Use one small YAML file that a human can read:
```yaml
schema_version: 1
task_id: demo
repo: /absolute/path/to/repo
objective: Improve play across procedurally generated maps without breaking correctness.
editable_paths: [src/**, tests/**]
denied_paths: [.git/**, pyproject.toml, uv.lock]
public_checks: [[python, -m, pytest, -q]]
public_probe: [python, tools/dev_benchmark.py]
evaluator:
  repo: /private/arctl-evaluators/demo
  commit: 7a95d61
trials: auto
max_experiments: 30
```
`trials` must be exactly `auto` or a positive integer other than a Boolean. `auto` runs the approved evaluator's calibration once and freezes the selected count. An integer skips calibration and fixes that count; `1` is the deterministic special case. The fixed or calibrated count must be supported by the evaluator manifest and must not exceed its approved safety ceiling. `arctl init` uses `auto` by default.

All evaluator-manifest commands must be argument lists, not shell strings.
Allowed full-argument placeholders:
```text
subject command:   {input} {output}
evaluator command: {request} {response}
```
The controller creates each JSON request and every fresh output path. Evaluator requests identify the operation (`prepare`, `calibrate`, or `score`) and, for comparison operations, `kind: primary | suspect`. They expose only the inputs authorized for that operation. Reject unknown placeholders, partial placeholder text, path escapes, blocked programs, evaluator-returned commands, and responses written anywhere except `{response}`.

The task file deliberately has no metric direction, units, minimum delta, statistic, confidence level, calibration threshold, trial ladder, evaluator command, subject command, suspect-test rule, or telemetry declaration. These are task-specific evaluator responsibilities, fixed by the approved evaluator manifest and summarized during approval.

## 9. Human interface
These rules are required for release.

### 9.1 Setup and approval
- `arctl init` creates a starter task file with comments and safe defaults.
- Setup should need one edit step and one approval command.
- Users must not need database IDs, internal phase names, or hidden state.
- There are only two approvals: task file and evaluator commit.
- No approval is needed for each experiment.
The evaluator-commit approval includes its manifest. The approval screen must show the exact file or commit, changes from the last version, safety-related paths and commands, the Git hash, the `trials` setting, and the exact approval command. It must summarize the evaluator's statistic, what positive effect means, uncertainty method, known uncontrolled variation and mitigation, calibration policy and ceiling when `trials: auto`, subject-visible seed policy, optional suspect-test trigger and reason codes, and publishable telemetry. It must say plainly that the evaluator owns the statistical method, while `arctl` validates the approved protocol and evidence shape; neither approval nor calibration creates a harness-wide false-positive guarantee. For an integer `trials` value, it must say plainly that calibration will be skipped and the displayed count will be used for every comparison.

Approval must also warn that it establishes a trust boundary. An AI operator must present the approval and obtain explicit human permission before confirming it. The MVP does not implement delegated authority.

### 9.2 Normal use
Normal use should need only:
```text
run
status
stop
report
inspect
```
`arctl status` must show current work, calibration state and frozen trial count, experiment number, champion commit, whether a candidate is provisional or a suspect test is running, the last final result and effect estimate, whether the user must act, how to stop safely, and log paths.

Every command uses the same interaction contract:
- infer the task from the current repo when exactly one task matches;
- require an explicit task ID only when inference is impossible or ambiguous;
- say what happened, whether saved evidence remains valid, and whether the user must act;
- finish with exactly one recommended next command.

The same eight commands accept `--json` as the sanctioned AI-operation route. JSON output contains a schema version, success or failure, task and experiment IDs, stable state, action requirement, allowed actions, safe artifact metadata, and the next command. It never contains private seeds, cases, raw subject output, or private evidence. The configured operator AI is trusted to orchestrate through this route; research sessions remain untrusted and separately sandboxed. Human-readable and JSON output describe the same permitted operational facts.

### 9.3 Errors and stop
Each error must say what failed, whether the result is still valid, whether work can continue, the next safe command, and the log path. Show stack traces only with `--debug`.
`Ctrl-C` and `arctl stop` must do the same thing.
Before the primary comparison is reserved: stop, discard the unfinished experiment, and do not count an experiment.
After any comparison is reserved: stop active processes, keep valid saved output, publish `INVALID`, and never rerun or redraw that comparison.
Running `stop` more than once must be safe.

### 9.4 Audit
Each completed experiment must have one small folder with all key files. A human should be able to audit it with normal file tools and Git commands. A database must not be required.

## 10. Experiment workflow

### 10.1 Fix the trial count
When `trials` is a positive integer, `arctl` freezes that value for every comparison and skips calibration.

With `trials: auto`, calibration happens once after approval and before research. It freezes the initial champion and manifest, reserves non-reusable calibration seeds, runs the approved calibration protocol, validates the recommended positive count against the manifest and its ceiling, saves private evidence, and freezes the count for the task. Calibration reuses the controller's seed allocator and `ProcessRun` rules.

The evaluator owns and explains its calibration criterion. The starter template uses `32, 64, 128, 256`, an evaluator-defined 95% uncertainty statement, and a 10% relative-uncertainty target, but these are not universal assumptions. Calibration means only that the approved criterion was met on its evidence; it does not prove that all variation is controlled or that uncertainty has nominal coverage. Failure blocks research and is never silently retried or given replacement seeds.

### 10.2 Develop and freeze
`arctl` creates a clean champion worktree and starts one `RESEARCH` session with the approved public packet. The session may use public probes and must write:
```json
{
  "schema_version": 1,
  "claim": "Prefer routes with recoverable resources.",
  "mechanism": "Penalize branches with no safe retreat.",
  "expected_effect": "Complete more procedurally generated maps.",
  "expected_telemetry": {"dead_end_entries": "decrease"},
  "falsifiers": ["The paired effect is not positive", "Correctness tests fail"]
}
```
After the session ends, `arctl` validates this record, changed and blocked paths, and public checks; creates the candidate commit and ref; and rejects blocked, empty, or repeated candidates. No code changes after candidate freeze, and public probes never decide promotion.

### 10.3 Run one comparison
`ComparisonRun(kind)` is the only official evaluation workflow:
1. **Reserve:** create a cryptographically strong private master seed and derive exactly the frozen number of domain-separated trial seeds. Save the comparison kind; experiment, champion, candidate, evaluator, and manifest IDs; trial count and seed-derivation version; private master seed; schedule hash; randomized subject order; exact commands; and process IDs. Nothing in the reservation may change.
2. **Prepare:** run the approved evaluator once under `EVALUATOR`. It writes one public batch for both subjects and one private scoring file. The public batch has the reserved number of cases and exposes no hidden answers, private identities, schedules, evaluator details, or illegitimate generator seeds.
3. **Run subjects:** run champion and candidate once each under `SUBJECT`, in the reserved order, on the identical ordered public batch. Each writes exactly one result per declared trial to a controller-created folder. After its complete process group stops, `arctl` copies, validates, and parses the output. Results are never omitted, replaced, or retried because they are missing, noisy, or unfavorable.
4. **Score:** run the same approved scoring command once with `kind`, the private scoring file, and both saved subject outputs. It returns the common evidence schema below.
5. **Validate:** reject evidence as `INVALID` when required values are missing or non-finite, identities or trial count differ from the reservation, unapproved telemetry appears, the lower bound exceeds the estimate, or suspect-test fields violate the approved manifest.

```json
{
  "schema_version": 1,
  "kind": "primary",
  "trial_count": 128,
  "hard_rules_pass": true,
  "comparison": {
    "effect_estimate": 0.037,
    "one_sided_lower_bound": 0.011
  },
  "suspect_test": {
    "required": false,
    "reason": null
  },
  "telemetry": {}
}
```

Effects use the evaluator's approved units and direction-normalized meaning from Section 5. For an exact evaluation, the lower bound equals the estimate. The evaluator never returns a final decision.

Only primary evidence may request a suspect test. Such a request is valid only when the normal result would be `ACCEPT`; its reason must be approved in the manifest and may use only primary evidence and task-specific diagnostics. Suspect evidence must set `required: false` and `reason: null` and returns no telemetry.

### 10.4 Decide and publish
The controller applies the same decision rule to either comparison:
- `ACCEPT`: hard rules pass and `one_sided_lower_bound > 0`;
- `ARCHIVE`: hard rules pass, the estimate is positive, and the lower bound is not above zero;
- `REJECT`: candidate checks, execution, or hard rules fail, or a valid estimate is non-positive;
- `INVALID`: champion, evaluator, evidence, sandbox, controller, host, stop, or crash fails.

After a primary `ACCEPT` that requests a suspect test, the experiment is internally `PROVISIONAL`: keep the champion unchanged and run exactly one fresh `ComparisonRun(kind="suspect")` with non-overlapping seeds. Its decision becomes final and it cannot request another run. Otherwise the primary decision is final.

Only a final `ACCEPT` may update the champion ref. `arctl` then saves private evidence for every comparison, writes aggregate public feedback, marks the experiment complete, removes temporary worktrees, and gives only the final public result to later sessions. There is no controller-defined metric direction, minimum delta, or unit-specific threshold.

## 11. Public feedback
Allowed example:
```json
{
  "experiment_id": 17,
  "hypothesis": "Prefer routes with recoverable resources.",
  "champion_before": "abc123",
  "candidate": "def456",
  "champion_after": "def456",
  "decision": "ACCEPT",
  "evaluation": {
    "statistic": "expected_completed_levels",
    "comparisons": [{
      "kind": "primary",
      "trials": 128,
      "effect_estimate": 0.037,
      "one_sided_lower_bound": 0.011,
      "suspect_test_required": false,
      "suspect_test_reason": null
    }]
  },
  "constraints": {"tests": "PASS"},
  "telemetry": {"dead_end_entries": 47}
}
```
Public feedback must not include hidden cases, seeds, schedules, evaluator code or paths, raw subject output, per-trial scores, stdout, stderr, error text, private notes, or telemetry not allowlisted by the evaluator manifest.
Public telemetry may contain only finite numbers, Booleans, or `null`.

When a suspect test ran, `evaluation.comparisons` contains a second item with `kind: suspect` and the same aggregate shape; the primary item identifies the approved reason. Public feedback never exposes either comparison's private identities. Every task report must state that uncertainty is calculated by the approved evaluator for one candidate comparison, that `arctl` validates the protocol and evidence shape rather than the evaluator's mathematics, and that calibration and suspect testing are best-effort mitigations. Because the MVP adaptively evaluates multiple candidates without alpha spending or another task-wide correction, it does not promise nominal coverage or bound the chance of at least one false promotion across the complete research run.

## 12. Process and crash rules
Every calibration or comparison process is a `ProcessRun`:
- without a `started` record it may run;
- with valid saved output the controller uses that output;
- with `started` but no valid output it never runs again.
Candidate public-test, subject, or hard-rule failure gives `REJECT`. Champion, evaluator, sandbox, controller, or host failure gives `INVALID`. Calibration failure blocks the task before research and never silently redraws calibration seeds.
Failure before the primary comparison is reserved discards the unfinished experiment. Failure after any comparison reservation publishes the experiment without a replacement seed or retry. A saved valid primary `PROVISIONAL` result may continue only into its one not-yet-started suspect comparison.
Every process starts in its own process group. On exit, timeout, stop, or too much output, `arctl` must stop and clean up all child processes.

## 13. Install, files, and CLI
The MVP is installed from its local source checkout, not PyPI. It ships one `install.sh` that verifies Python 3.11+, creates or reuses `<arctl-source>/.venv`, runs:
```bash
<arctl-source>/.venv/bin/python -m pip install --editable <arctl-source>
```
and prints the activation command. The script must be safe to rerun and must not use `sudo`, modify the system Python, install target-repo dependencies, or create experiment data inside the source checkout.

```text
<arctl-data>/tasks/<task-id>/
  task.yaml
  evaluator.commit
  evaluator.manifest.json
  trial-count.json
  calibration.private.json
  lock
  experiments/000001/
    request.public.json
    experiment.json
    champion.commit
    candidate.commit
    comparisons/
      primary/
        reservation.private.json
        process/
        evidence.private.json
      suspect/
        reservation.private.json
        process/
        evidence.private.json
    result.public.json
    published
  worktrees/
  reports/
```
`comparisons/suspect/` is absent unless the primary result required it. `calibration.private.json` is absent when `trials` is an integer. `trial-count.json` records whether the frozen count was automatic or fixed and contains no private seeds. Each comparison uses the same artifact shape. Private reservations and evidence contain complete trial identities; public results contain only aggregates.

Write files through a temporary file and rename so half-written files are not used. Only one `arctl` process may hold the task lock. SQLite may help with search and reports, but Git and these files stay the source of truth.
```bash
arctl doctor
arctl init --repo PATH
arctl approve TASK_ID
arctl run TASK_ID [--max-experiments N]
arctl status TASK_ID
arctl stop TASK_ID
arctl report TASK_ID
arctl inspect TASK_ID EXPERIMENT_ID
```
`TASK_ID` and `EXPERIMENT_ID` may be omitted when the current repo and context identify exactly one target. `doctor` checks Git, Codex, sandbox rules, and runtime needs. `init` creates task storage and starter YAML with `trials: auto`. `approve` checks and locks the task file, evaluator commit, and manifest, then requires the trust-boundary confirmation from Section 9. `run` first calibrates when required, then runs experiments until stopped, blocked, done, or failed. `status` shows current work. `stop` stops safely. `report` shows progress and aggregate results. `inspect` shows the safe artifact inventory and aggregate record; trusted humans audit private files with normal file tools.

The novice setup path is:
```text
arctl doctor
arctl init --repo .
# edit the generated task file
arctl approve
arctl run
```

## 14. Done when
The MVP is done when one toy repo and one real repo show:
1. One approved external evaluator commit and manifest stay locked, and research and subject sandboxes cannot read evaluator or private files.
2. Each experiment uses one fresh session; `arctl` checks allowed paths and public tests, freezes a nonempty candidate commit, and never lets the model test, decide, or promote.
3. Best-effort paired trials support exact `trials: 1`, a fixed positive count, and one-time manifest-approved `auto` calibration with disclosed limitations.
4. Primary and suspect evaluations use the same `ComparisonRun` commands, evidence schema, validation, storage, decision, and recovery path.
5. Candidates freeze before reservation; calibration and comparison seeds never overlap; each subject runs the identical ordered batch once and returns one result per declared trial.
6. An approved trigger can hold an apparent win as `PROVISIONAL`; exactly one fresh suspect comparison runs before promotion and cannot trigger another.
7. `ProcessRun` recovery never silently repeats, extends, selectively retries, or redraws an official process or batch.
8. The common evidence schema supports at least a mean, binary win-rate, and non-mean statistic with disclosed dependence and uncontrolled variation.
9. Only `arctl` decides: positive lower bound accepts, flagged acceptance waits for its suspect result, positive uncertainty archives, and non-positive effect rejects.
10. Candidate failure gives `REJECT`, system or incoherent-evidence failure gives `INVALID`, and only final `ACCEPT` changes the champion.
11. Later research sessions see only checked aggregate final results; private per-trial evidence remains auditable from the experiment folder and Git history.
12. All eight commands infer unambiguous tasks, explain validity and the next command, and provide schema-valid sanctioned `--json` output without private evidence.
13. AI-operated approval reports that human permission is required; approval, status, stop, errors, and the novice setup path require no source reading.
14. A real task runs several experiments without repeated approval, preserves failed ideas, and reports the best-effort and non-search-wide limits.
15. `install.sh` creates a working local editable installation and exposes `arctl` without PyPI, `sudo`, or system-Python changes.

## 15. Final rule
When rules conflict:
```text
1. Honest experiments
2. Evaluator and session safety
3. Easy human use
4. Keep valid evidence
5. Simple code
6. Speed
```
Do not add a feature unless it clearly helps one of these goals.
