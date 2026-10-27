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
python3 analysis/whatran.py record "Detect peaks"         # after a step that produced something worth keeping
python3 analysis/whatran.py claim "Feature 301.1 is higher in group B (p < 0.05)"   # a finding
python3 analysis/whatran.py reject run-detect-peaks-1 --reason "Threshold 1.1 calls noise as peaks"
python3 analysis/whatran.py done               # at the end: compile, check files, draft the package
```

`record` needs only the label. Inputs, outputs, code and parameters are read
from the captured command; the output tells you what it inferred. If it got
something wrong: `--in`, `--out`, `--code`, `--param k=v` override, or
`update <id> --param k=v` afterwards. Outside Claude Code (Codex, a shell), run
each step as `python3 analysis/whatran.py run -- <command>` so it is captured.

## Writing rules (enforced)

Every label, claim, note and reason must follow these rules; a record that
breaks them is refused and the error names the fix. Keep to them and records
stay short, uniform and cheap.

- A step label starts with an approved verb and has at most 8 words:
  `Detect peaks`, `Compare groups with Welch t-test`, `Fit linear model`.
  Not `Peak detection`, not `Statistics`.
- A claim, note or reason uses sentences of at most 20 words. State the
  measured thing and the comparison: `Feature 301.1 is higher in group B (p < 0.05)`.
- No pronouns (I, we, you), no hedging (maybe, seems, might, should), no
  intensifiers (very, really), no passive voice (was, were, been).
- Prefer the plain word: do, use, start, stop, before, after.
- Technical words are fine as nouns (m/z, limma, FDR). If a verb you need is
  missing, add it once to `analysis/config.json` under `"style": {"verbs": []}`.

## Recording from inside code

When you write the analysis as a script or notebook, record the steps in it
instead of from the shell. Files that change inside a step become its outputs;
the script becomes the code.

```python
import whatran as w                                   # pip install whatran, or sys.path to analysis/
with w.step("Detect peaks", params={"threshold": 5}, inputs=["data/raw.csv"]) as s:
    ...
w.claim("Feature 301.1 is higher in group B (p < 0.05)", supports=[s])
```

```r
source("analysis/whatran.R")   # from the plugin's r/whatran/R/whatran.R, or install the package
id <- whatran_step("Detect peaks", { ... }, params = list(threshold = 5), inputs = "data/raw.csv")
```

Any language can do the same with `whatran.py begin "Label"` (prints a token)
and `whatran.py end TOKEN --param k=v` around the code.

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
