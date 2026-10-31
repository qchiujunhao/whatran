# Whatran design

Whatran answers two questions about an analysis that a coding agent carried
out: *what actually ran*, and *does the reported result rerun*. It does this in
two stages that share one repository and one manifest.

| stage | skill | runs when | leaves behind |
| --- | --- | --- | --- |
| record | `record-analysis` | during the analysis | `analysis/events.jsonl`, `analysis/graph.json`, `analysis/report.md`, `analysis/index.html` |
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
(`node` events) is declared by the agent, or derived for it from the evidence
and from its own words (below). A run links the two. The agent cannot mint
evidence, and a derived step is marked as derived, so each record says how it
came to be.

Three ways to capture evidence, in decreasing strength:

1. **Hooks** (Claude Code). Every Bash tool call is observed: `PreToolUse`
   records the start time and a snapshot of the watched tree, `PostToolUse`
   and `PostToolUseFailure` append the `exec` event. Hooks act only inside a project that has
   `analysis/config.json`; elsewhere they exit immediately. The hook is silent
   by default (`observe.nudge` turns on a one-line notice per captured command).
2. **Wrapper**: `whatran.py run -- CMD` executes the command itself, snapshots
   the watched tree before and after, and appends the same `exec` event. This
   works in any agent or shell.
3. **Direct**: an agent without a shell writes `graph.json` in the schema. Its
   runs are `declared` by construction.

File changes are detected by comparing each file's modification time and size
with a snapshot taken just before the command, by hooks and wrapper alike. Only
when a hook learns of a command after it ran, or the tree is too large to
snapshot, does it fall back to files modified since the estimated start. Reads are
not captured; inputs are read off the command line (existing files it names)
or declared, and the recorder verifies they exist and hashes them.
`outputs_observed` lists which declared outputs were actually seen changing
with a matching hash.
The package's own files (`whatran.json`, `run.sh`, `check.py`, `build_site.py`,
the workflow, and the root `README.md` and `.gitignore`) are never evidence:
whatran writes them, so no step can claim them, however close in time.

### The agent's words

A captured execution also carries what the agent said about it, when the agent
runs in Claude Code: the one-line `description` it gave the command, and the
`intent`, its last text before the call within the same turn, read from the
session transcript and clipped to a few hundred characters. Only assistant
text is read; the person's messages are never copied. Both are stored on the
`exec` event and copied to the run that links it. `observe.intent` switches
this off. This is the cheapest source of the "why" layer: words written before
the command ran, bound to the execution that followed.

### Steps without the agent's help

At the end of every turn (and on `whatran.py done`), every captured execution
that is not yet linked to a run becomes one, if it succeeded and produced
result files. Files that are code or configuration do not count as results, so
writing a script is not a step; commands that only move, copy, delete or
install things are skipped; failed commands stay in the log unlinked.

The label is taken, in order, from the command's description, from the intent
(the last sentence that starts with an action, filler such as "now let me"
removed, the verb normalised), or from the command (`Run detect_peaks.py`).
The run records which source it came from (`auto.label_from`), and the viewer
marks it, so an auto-named step is never mistaken for a named one. Inputs,
outputs, code and parameters are read from the command as for `record`.

Two cases are not new steps. An execution whose command equals a recorded
run's command and whose changed files have the same content as recorded is a
repeat: it is attached to that run. An execution that only regenerates files
already in the record under some other command (a rerun of `run.sh`, for
example) is left alone.

A run that makes a result file that an earlier completed run made before
supersedes that run, unless it reads that run's output as an input (a step
that rewrites a table in place is a new step, not a redo). The same rule
applies to steps the agent records itself. Rejecting a branch with a reason
remains the agent's call; without it, the record still shows which attempt is
current.

### The end of a turn

A `Stop` hook runs when the agent's turn ends: promote executions to steps,
compile `graph.json`, write or refresh the package, and save the agent's last
message as `analysis/report.md`. The report is replaced only when the turn did
analysis work (the ledger grew) or when the message names a recorded file or
step, so small talk does not overwrite it. The report is the agent's claim
about the analysis; the graph is what ran. An auditor compares the two.

Package files that whatran generates (`whatran.json`, `run.sh`) carry a
fingerprint of their own content. While the file is exactly what whatran wrote
it is regenerated as the record grows; once someone edits it, it is kept.
`check.py`, `build_site.py` and the workflow are written only when missing.
The hook says one line when the package is first created and is silent after
that. Nothing happens in a project where nothing has run.

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

1. The package is derived from `main_path` (by the turn-end hook, `done`, or
   `package`): input artifacts become `inputs`, completed runs in topological
   order become `steps` (ids derived from the run ids), result artifacts become
   `outputs` with a comparison rule guessed from the file type. `run.sh` is the
   recorded commands of those runs in order; `check.py`, `build_site.py` and the
   CI workflow are written from copies embedded in `whatran.py`. Rejected and
   superseded branches are excluded by construction. Generated files are
   refreshed until someone edits them; edited files are kept.
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
is written; a warning names the fix and the record is kept (`style.strict`
refuses instead). Labels derived from the agent's words follow the rules when
the words allow it. The verb list is the project's own
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

In Claude Code the agent runs `init` once and `check` at the end; everything
between is captured and compiled without it. A handful of one-line commands
refine the record and are optional: `claim "finding"`, `reject ID --reason`,
`record "what it did"` (to name a step better than the derived label; inputs,
outputs, code and parameters are read from the captured command), `update`,
`open`. Agents without hooks run steps through `run -- CMD` and call `done`,
which does what the turn-end hook does.

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
- `graph.json` contains commands, parameters, paths and output heads, and,
  with the transcript features on, the agent's descriptions, intents and
  report. These are the agent's words, not the person's, but they go into the
  repository; `observe.intent` and `observe.report` switch them off. GitHub
  Pages is public even for private repositories; review before publishing.
- Inference cannot tell a file that a command both reads and writes from an
  output, so an in-place rewrite recorded automatically supersedes the earlier
  step; recording it with `--in` keeps both. A label taken from the command
  (`Run detect_peaks.py`) says what ran, not what it meant.
- The Stop hook compiles and hashes the whole watched tree at the end of every
  turn; narrow `watch` for large data directories.
- Moving references aside prevents accidental reuse, not deliberate reuse by
  hostile analysis code. CI is automated rerun evidence, not an independent
  scientific judgment.
