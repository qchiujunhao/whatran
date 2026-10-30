---
name: record-analysis
description: Record what actually ran during a scientific or data analysis as a checkable, result-centric Analysis Graph, while the analysis happens. Use whenever you run computations that produce scientific results (data processing, statistics, figures, models; any Python/R/shell/HPC/Galaxy analysis), whenever the user mentions provenance, lineage, "what did you actually run", or wants to inspect, branch, replay or compare analysis steps. Stage one of Whatran; package-analysis is stage two.
---

# Record the analysis

Analyse the way you normally would. In Claude Code the record keeps itself:
hooks capture every command you run, and when your turn ends every captured
command that produced result files becomes a step, `analysis/graph.json` is
compiled, the package (`whatran.json`, `run.sh`, `check.py`, CI workflow) is
written or refreshed, and your last message is saved as `analysis/report.md`.

What you do:

```bash
python3 <plugin root>/bin/whatran.py init      # once per project, before the first step (plugin root = two directories above this skill)
python3 analysis/whatran.py check              # when the analysis is finished: rerun from clean and compare (package-analysis skill)
```

Steps are named from the `description` you give each Bash command, so write
it as what the step does: `Detect peaks at threshold 5`, not `Run script`.
Without a description, the last thing you said before the command is used;
without that, the command (`Run detect_peaks.py`).

Three optional one-liners make the record better. Use them when they are cheap:

```bash
python3 analysis/whatran.py claim "Feature 301.1 is higher in group B (p < 0.05)"   # a finding; attaches to the last step
python3 analysis/whatran.py reject run-detect-peaks-loose-1 --reason "Threshold 1.1 calls noise as peaks"
python3 analysis/whatran.py record "Detect peaks" --param threshold=5      # name a step yourself, or fix a guess
python3 analysis/whatran.py status                                          # what is recorded, what is unlinked
```

`record` needs only the label. Inputs, outputs, code and parameters are read
from the captured command; `--in`, `--out`, `--code`, `--param k=v` override,
and `update <id> --param k=v` amends afterwards. Outside Claude Code (Codex, a
shell), run each step as `python3 analysis/whatran.py run -- <command>` so it
is captured, and call `python3 analysis/whatran.py done` at the end; it does
what the turn end does in Claude Code.

## Writing rules

Labels, claims, notes and reasons that follow these rules stay short, uniform
and cheap. A record that breaks one is kept, with a warning that names the fix.

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

## What becomes a step, and what does not

- A captured command becomes a step when it succeeded and changed at least one
  file that is not code or configuration. Writing a script, installing
  packages, moving or copying files, and failed commands do not become steps;
  they stay in the execution log.
- **A changed parameter or method is a new step.** When it remakes the same
  result file, the earlier step is marked superseded automatically. An exact
  repeat (same command, same output bytes) is attached to the step it repeats.
- **Abandoned branches deserve `reject`** with a reason. Without it the record
  still shows which attempt is current, but not why.
- **Findings deserve `claim`.** A claim attaches to the last completed step
  unless you pass `--supports <run id or output file>`.
- **Never write evidence by hand.** No hashes, exit codes or edits to
  `analysis/events.jsonl`. A step that ran where nothing could capture it is
  recorded as `declared`; say so when reporting.

## Joining an analysis already in progress

Steps that ran before `init` were not captured. For each one that matters,
either rerun it through `python3 analysis/whatran.py run -- <command>` (best
when it takes seconds or minutes: it becomes `observed`), or record it with
the command and its outputs:

```bash
python3 analysis/whatran.py record "Detect peaks" --cmd "python scripts/detect.py --ppm 15 data/raw.mzML results/peaks.csv" --out results/peaks.csv
```

It is `declared` (nobody saw it run), inputs, code and parameters are still
read from the command, and because the command is known, the package puts it
in `run.sh` and `check` can still replay-verify it.

## Continuing in a later session

The record is in the repository, so nothing is lost between conversations or
agents. Start with `python3 analysis/whatran.py status` (or `open`) to see what
is recorded, then carry on. From the viewer, "Copy context for an agent" hands
any node's exact inputs, code and parameters to the next session. Do not run
two sessions that record the same project at the same time.

## Finishing

When the findings are ready, read `run.sh` (the recorded commands in order),
add dependency installation if the steps need packages, and run
`python3 analysis/whatran.py check`: it reruns the package from clean, compares
the outputs with the committed ones and marks the steps replay-verified (see
the package-analysis skill). Tell the user how to open the graph:
`python3 analysis/whatran.py open`.

When reporting results, give one line per claim with its supporting step, say
how many steps are observed versus declared, and point to the graph. The graph
wins over memory.

`references/graph-format.md` describes the file format;
`references/other-agents.md` covers agents without this plugin.
