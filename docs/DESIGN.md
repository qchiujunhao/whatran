# Whatran design

Whatran answers two questions about an analysis that a coding agent carried
out: *what actually ran*, and *does the reported result rerun*. It does this in
two stages that share one repository and one manifest.

| stage | skill | runs when | leaves behind |
| --- | --- | --- | --- |
| record | `record-analysis` | during the analysis | `analysis/events.jsonl`, `analysis/graph.json`, `analysis/index.html` |
| package | `package-analysis` | when findings are reported | `whatran.json`, `run.sh`, `steps/`, `results/`, `check.py`, `build_site.py`, a CI workflow |

## Problem

An agent finishes an analysis and hands over result files and a written report.
Two things are missing: which computations the results actually depend on, and
whether the agent's description of those computations is true. Workflow engines
answer this by making the steps explicit before anything runs, which
exploratory analysis does not tolerate. Chat transcripts contain the truth but
cannot be read, checked or rerun.

## Stage one: record

### The ledger is the source of truth

`analysis/events.jsonl` is append-only. One JSON object per line, never
rewritten:

- `exec`: a command ran. Command, working directory, exit code, timing, the
  head of stdout and stderr, and the files under the watched directories that
  changed while it ran, each with size and SHA-256.
- `node`: the agent declared an artifact, a run or a claim.
- `status`: a run or claim was rejected or superseded, with a reason.
- `update`: a label, note, parameter set or status was amended.
- `replay`: a packaged path reran and passed its check (stage two writes this).

`analysis/graph.json` is compiled from the ledger and can be regenerated at any
time. Nothing is edited in place, so a rejected branch cannot disappear and the
history of every node is recoverable. Append-only lines also merge cleanly in
git.

### Evidence and meaning come from different writers

Execution evidence (`exec` events) is written only by something that watched
the command run: a hook, or the `whatran.py run` wrapper. Scientific meaning
(`node` events) is declared by the agent with `whatran.py record "what it did"`.
A run links the two. The agent cannot mint evidence and the recorder cannot know what a step
means, so each stays within what it can know.

Three ways to capture evidence, in decreasing strength:

1. **Hooks** (Claude Code). Every Bash tool call is observed: `PreToolUse`
   records the start time, `PostToolUse` and `PostToolUseFailure` append the
   `exec` event. Hooks act only inside a project that has
   `analysis/config.json`; elsewhere they exit immediately. When a command
   changed files, the hook returns one line of context to the agent naming the
   execution id and the changed files, so the agent can declare the step while
   it still knows what the command was for.
2. **Wrapper**: `whatran.py run -- CMD` executes the command itself, snapshots
   the watched tree before and after, and appends the same `exec` event. This
   works in any agent or shell.
3. **Direct**: an agent without a shell writes `graph.json` in the schema. Its
   runs are `declared` by construction.

File changes are detected by comparing modification time and size (wrapper:
exact snapshot diff; hooks: files modified since the command started). Reads are
not captured; the agent declares inputs and the recorder verifies they exist and
hashes them. `outputs_observed` lists which declared outputs were actually seen
changing with a matching hash.

### Nodes

Three node types, modelled on W3C PROV:

| type | PROV | identity |
| --- | --- | --- |
| `artifact` | Entity | path + SHA-256. Same path with new content is a new node that `supersedes` the old one. |
| `run` | Activity | explicit id `run-<slug>-<n>`; one scientific step, possibly several commands |
| `claim` | derived Entity | explicit id; a conclusion and the runs or artifacts it rests on |

Edges: `used` (artifact → run), `generated` (run → artifact), `supports`
(run, artifact or claim → claim), `supersedes` (new → old). Branches need no
node type: two runs that use the same artifact are a branch.

### Evidence grades

Every run and artifact carries a grade. Nothing is reproducible by assertion.

- Run `evidence.level`: `observed` (a recorder saw it run) or `declared` (only
  the agent's word).
- Run `status`: `completed`, `failed`, `rejected`, `superseded`.
- Artifact `integrity`, recomputed at compile time: `intact`, `modified`,
  `missing`, `unchecked` (no hash: absent when recorded, or above the size
  limit).
- Run `verified_by_replay`: false until stage two records a passed replay.

The viewer's footer states the counts. A graph with zero observed runs is still
a useful record, and it says so.

### Result first, everything kept

Compilation computes `main_path`: the dependency closure of the non-rejected
claims (or, with no claims, of the result artifacts of completed runs), plus the
other outputs of those runs. The viewer shows only this by default. Rejected,
failed and superseded branches stay in `nodes`, every captured command stays in
`exec_log`, and one toggle reveals them. Selective reporting is visible, not
possible.

### Viewer

`analysis/index.html` is one static file with one CDN script (dagre) and a
built-in fallback layout. It renders `graph.json` from GitHub Pages or a local
server: dependency highlighting, a detail panel per node, the execution log,
and two copy buttons that hand a node's exact inputs, code, parameters and
hashes to an agent as a prompt. It executes nothing; branching happens in the
agent, which records a new run that `supersedes` the old one.

### The tool is copied into the project

`whatran.py init` copies `whatran.py` and the viewer into `analysis/`. The
project is then self-sufficient: any agent on any machine can run
`python3 analysis/whatran.py ...` without the plugin installed. Upgrading is a
file copy.

## Stage two: package

The package is a normal repository, not a capsule. Its contract:

```text
whatran.json                  declared inputs, ordered steps, outputs and comparison rules
run.sh                        one canonical command: rebuild the environment, run every step
requirements.txt              resolved environment lock (or the runtime's equivalent)
data/                         raw inputs, or checked download instructions
steps/                        final result-affecting scripts, in order
results/                      committed reference outputs
check.py                      fixed comparator, copied unchanged from the skill
build_site.py                 analysis-specific result page, generated from results
.github/workflows/reproduce.yml
README.md                     question, method choices, assumptions, limits, rerun command, CI claim
```

Only the paths that the reported result depends on are packaged. Alternative
paths to the same question are kept as branches after shared preparation with
an explicit comparison step; independent questions become separate packages
under `analyses/<slug>/` with one generated landing page.

### What a check proves

On every push, CI starts from a fresh clone, moves the committed `results/`
outside the package, runs `run.sh`, compares the regenerated outputs with the
moved references using the rules in `whatran.json` (`numeric` with tolerance,
`exact` bytes, or `exists` for rendered figures whose data is checked
separately), and builds the site from the regenerated results.

A pass supports exactly this statement: the declared outputs regenerated from
the declared inputs and matched the committed references under the declared
comparisons. It does not establish scientific correctness, completeness of
method capture, or validity on new data. Subset runs must be labelled as such.

## How the stages join

1. `whatran.py package` derives the package from `main_path`: input artifacts
   become `inputs`, completed runs in topological order become `steps` (ids
   derived from the run ids), result artifacts become `outputs` with a
   comparison rule guessed from the file type. `run.sh` is the recorded
   commands of those runs in order; `check.py`, `build_site.py` and the CI
   workflow are written from copies embedded in `whatran.py`. Rejected and
   superseded branches are excluded by construction. Existing files are kept.
2. `whatran.py check` moves the committed results aside, runs `run.sh`, runs
   `check.py` against the moved references, puts the references back whatever
   the outcome, and only on success appends a `replay` event naming the covered
   runs. `replay-verified --ci URL` records a pointer to a passing CI run
   instead. Compilation then sets `verified_by_replay: true` on those runs and
   nowhere else.

The record stays complete; the package stays minimal; the manifest ties them.

## The meaning layer: short, uniform, editable records

Evidence is collected by machines; meaning is written by agents and people. To
keep that writing cheap and comparable across agents and languages, records
obey writing rules modelled on Simplified Technical English (ASD-STE100): a step
label starts with an approved verb and has at most 8 words; sentences in claims,
notes and reasons have at most 20 words; no pronouns, hedging, intensifiers or
passive voice; plain words over formal ones. The rules are checked when a record
is written, and a refusal names the fix. The verb list is the project's own
(STE's dictionary is copyrighted): a built-in list written to STE's rules, which
`analysis/config.json` can extend, replace from a file, or switch off.

Records can be edited and removed at any time from the agent's point of view.
Underneath, `edit` appends an `update` event and `remove` appends a `retract`
event; the graph shows the latest state and hides retracted records, while the
ledger keeps every version. An auditor can always see what was changed or
withdrawn, which is what makes the record worth trusting.

The same API exists for code in any language. `begin LABEL` snapshots the
project and returns a token; `end TOKEN` diffs it and records the step with the
files that changed as outputs. The Python API (`with w.step(...)`, `@w.step`)
and the R package call exactly this, so a step recorded from R, Python or a
shell has the same shape and the same evidence.

## The agent's interface

Everything above is driven by a handful of commands, each needing one argument
at most: `init`, `record "what it did"`, `claim "finding"`, `reject ID --reason`,
`done`, `package`, `check`, `open`. `record` reads inputs, outputs, code and
parameters from the captured command (files the command changed are outputs;
script files it names are code; other existing files it names are inputs;
`--key value` pairs are parameters) and prints what it inferred so the agent can
override with flags. Everything else is an optional refinement.

## Principles

- Grade, do not promise. Every claim about evidence is a field with a value.
- Keep everything, show the result path. Hiding is a view setting, never a deletion.
- Thin. One Python file, one HTML file, one JSON schema. No server, no database,
  no account. The repository owns the record; GitHub provides versioning, CI
  and hosting.
- Agent-agnostic. The policy, the format and the viewer need nothing from a
  particular agent; hooks, the wrapper and direct emission are adapters.

## Limits

- Hooks observe the window of a Bash call. Commands in a notebook kernel, on a
  cluster, or in a background process are not observed; they stay `declared`
  unless run through the wrapper on the machine that executed them.
- A walk of the watched directories runs after every recorded command. Narrow
  `watch` in `analysis/config.json` for very large trees.
- The exit code reported by Claude Code hooks is inferred: 0 on success, parsed
  from the error on failure.
- Files above `hash_max_bytes` (1 GiB by default) are not hashed and stay
  `unchecked`.
- `graph.json` contains commands, parameters, paths and output heads. GitHub
  Pages is public even for private repositories; review before publishing.
- Moving references aside prevents accidental reuse, not deliberate reuse by
  hostile analysis code. CI is automated rerun evidence, not an independent
  scientific judgment.
