---
name: record-analysis
description: Record what actually ran during a scientific or data analysis as a checkable, result-centric Analysis Graph, while the analysis happens. Use whenever you run computations that produce scientific results (data processing, statistics, figures, models; any Python/R/shell/HPC/Galaxy analysis), whenever the user mentions provenance, lineage, "what did you actually run", or wants to inspect, branch, replay or compare analysis steps. Stage one of Whatran; package-analysis is stage two.
---

# Record the analysis

Analyse the way you normally would. Alongside, keep a record that distinguishes
what was **observed to run** from what you merely **say** ran.

Commands are captured automatically (Claude Code hooks). You add the meaning:

```bash
python3 <plugin root>/bin/whatran.py init      # once per project, before the first step (plugin root = two directories above this skill)
python3 analysis/whatran.py record "Peak detection"       # after a step that produced something worth keeping
python3 analysis/whatran.py claim "X is higher in group B (p < 0.05)"   # a finding
python3 analysis/whatran.py reject run-peak-detection-1 --reason "called noise as peaks"
python3 analysis/whatran.py done               # at the end: compile, check files, draft the package
```

`record` needs only the label. Inputs, outputs, code and parameters are read
from the captured command; the output tells you what it inferred. If it got
something wrong: `--in`, `--out`, `--code`, `--param k=v` override, or
`update <id> --param k=v` afterwards. Outside Claude Code (Codex, a shell), run
each step as `python3 analysis/whatran.py run -- <command>` so it is captured.

## When to record

- A step **produced something a scientist would want to inspect or reuse**:
  processed data, a table, a statistic, a model, a figure, a QC report. Not
  `ls`, file peeks, environment checks, debugging reruns.
- **One record per scientific step.** Name it by what it does ("Differential
  abundance, limma"), not by the script name.
- **A changed parameter or method is a new record**, with `--supersedes <old id>`
  if it replaces the old one. Never overwrite; the old step stays.
- **Abandoned branches get `reject`**, not silence. The viewer hides them by
  default and never deletes them.
- **Findings get `claim`.** A claim attaches to the last completed step unless
  you pass `--supports <run id or output file>`.
- **Never write evidence by hand.** No hashes, exit codes or edits to
  `analysis/events.jsonl`. A step that ran where nothing could capture it is
  recorded as `declared`; say so when reporting.

When a hook says `captured x-0012; changed: results/peaks.csv`, decide right
then: scientific step or noise? If a step, `record "<what it did>"` while you
still know what the command was for.

## Joining an analysis already in progress

Steps that ran before `init` were not captured. For each one that matters,
either rerun it through `python3 analysis/whatran.py run -- <command>` (best
when it takes seconds or minutes: it becomes `observed`), or record it with
the command and its outputs:

```bash
python3 analysis/whatran.py record "Peak detection" --cmd "python scripts/detect.py --ppm 15 data/raw.mzML results/peaks.csv" --out results/peaks.csv
```

It is `declared` (nobody saw it run), inputs, code and parameters are still
read from the command, and because the command is known, `package` puts it in
`run.sh` and `check` can still replay-verify it.

## Continuing in a later session

The record is in the repository, so nothing is lost between conversations or
agents. Start with `python3 analysis/whatran.py status` (or `open`) to see what
is recorded, then carry on recording. To redo a step differently, record the
new one with `--supersedes <old id>`. From the viewer, "Copy context for an
agent" hands any node's exact inputs, code and parameters to the next session.
Do not run two sessions that record the same project at the same time.

## Finishing

`done` compiles `analysis/graph.json`, re-checks every recorded file, drafts the
package manifest, and lists anything unlinked. Then hand over to stage two:
`package` generates the rerunnable package from the result path and `check`
reruns it from clean (see the package-analysis skill). Tell the user how to open
the graph: `python3 analysis/whatran.py open`.

When reporting results, give one line per claim with its supporting step, say
how many steps are observed versus declared, and point to the graph. The graph
wins over memory.

`references/graph-format.md` describes the file format;
`references/other-agents.md` covers agents without this plugin.
