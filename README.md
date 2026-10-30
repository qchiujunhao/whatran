# Whatran

[![test](https://github.com/qchiujunhao/whatran/actions/workflows/test.yml/badge.svg)](https://github.com/qchiujunhao/whatran/actions/workflows/test.yml)
[![showcase](https://img.shields.io/badge/showcase-GitHub%20Pages-0969da)](https://qchiujunhao.github.io/whatran/)

**What ran?** Install Whatran, then use Codex or Claude Code exactly as before.
When the agent does a scientific or data analysis, you get two things without
asking:

1. **A graph of what actually ran.** `analysis/graph.json` plus a viewer:
   every step with its command, parameters, inputs and outputs (hashed), which
   steps were observed to run, which branches were tried and dropped, and which
   results the findings rest on. Next to it, `analysis/report.md` keeps what the
   agent said, so the two can be compared.
2. **A package that reruns.** `run.sh`, `whatran.json`, `check.py` and a CI
   workflow that reruns the result path on a fresh clone, compares the outputs
   with the committed ones, and publishes a result page. A run is marked
   replay-verified only after that check passes.

Live showcase: [a recorded exploration](https://qchiujunhao.github.io/whatran/graph/)
and [a verified analysis](https://qchiujunhao.github.io/whatran/example/).

## Install

Codex:

```bash
codex plugin marketplace add qchiujunhao/whatran --ref main
codex plugin add whatran@whatran
```

Claude Code:

```bash
claude plugin marketplace add qchiujunhao/whatran
claude plugin install whatran@whatran
```

Any shell, script or notebook, with or without an agent:

```bash
pip install whatran
```

That is the whole setup. Nothing is recorded in projects the agent has not
initialised, and the agent initialises a project only when it starts an analysis
there.

## What you do

Nothing, normally. Two commands are useful to know:

```bash
python3 analysis/whatran.py open     # the graph, in your browser
python3 analysis/whatran.py check    # rerun from clean and compare, like CI does
```

## What the agent does

In Claude Code, almost nothing. The agent runs `init` once when it starts an
analysis in a project. From then on hooks capture every command it runs, and
when each of its turns ends:

- every captured command that produced result files becomes a step, named from
  the agent's own description of the command, else from what it said before
  running it, else from the command;
- a result file made again by a different attempt marks the earlier attempt
  superseded; an exact repeat is attached to the step it repeats;
- `analysis/graph.json` is compiled, and the package (`whatran.json`,
  `run.sh`, `check.py`, `build_site.py`, the CI workflow) is written or
  refreshed, files you edited by hand are left alone;
- the agent's last message is saved as `analysis/report.md`.

What remains for the agent is `check`, which reruns the package from clean. A
few one-line commands improve the record and are optional:

```bash
whatran claim "Feature 301.1 is higher in group B (p < 0.05)"     # a finding, attached to the last step
whatran reject run-detect-peaks-1 --reason "Threshold 1.1 calls noise as peaks"
whatran record "Detect peaks"                 # name a step yourself; details are read from the captured command
whatran done                                  # what the turn end does, for agents without hooks
whatran check                                 # rerun from clean, compare, mark replay-verified
```

`whatran` is the command after `pip install whatran`; without pip it is
`python3 analysis/whatran.py`, the copy that `init` leaves in the project.
Outside Claude Code, run steps as `whatran run -- CMD` so they are captured,
and finish with `done`.

`run.sh` is the recorded commands in dependency order; review it, add
dependency installation if the steps need packages, and `check` does the rest.
A step that the agent only talks about, with no captured command, stays
`declared`; a step whose label came from the command rather than from words is
marked so in the graph.

### In code: Python and R

Scripts and notebooks can record their own steps. Files that change inside a
step become its outputs; the script is recorded as the code.

```python
import whatran as w

with w.step("Detect peaks", params={"threshold": 5}, inputs=["data/raw.csv"]) as s:
    peaks = detect(raw, threshold=5)
    peaks.to_csv("results/peaks.csv")

@w.step("Fit model", params=["method"])          # captures the named arguments
def fit(table, method="welch"): ...

w.claim("Feature 301.1 is higher in group B (p < 0.05)", supports=[s])
w.reject(s, "Threshold 1.1 calls noise as peaks")
w.edit(s, params={"peakwidth": [5, 30]})
w.remove(s)                                      # retracts from the graph; the ledger keeps it
```

```r
source("r/whatran/R/whatran.R")                  # or install the package under r/whatran
id <- whatran_step("Detect peaks", {
  peaks <- detect(raw, threshold = 5)
  write.csv(peaks, "results/peaks.csv")
}, params = list(threshold = 5), inputs = "data/raw.csv")
whatran_claim("Feature 301.1 is higher in group B (p < 0.05)", supports = id)
```

Both go through the same command (`whatran begin` / `whatran end`), so any
language with a shell can do the same, and every record obeys the same rules.

### Writing rules

Records are short so that they cost few tokens and read the same from every
agent. The rules follow Simplified Technical English (ASD-STE100): a step label
starts with an approved verb and has at most 8 words (`Detect peaks`, not
`Peak detection`); a claim, note or reason has sentences of at most 20 words;
no pronouns, hedging, intensifiers or passive voice. A record that breaks a rule
is kept, with a warning that spells out the fix; `"style": {"strict": true}`
refuses instead. `--no-style` silences the warnings for one record;
`analysis/config.json` can add verbs (`"style": {"verbs": [...]}`), replace the
list from a file, or disable the rules. Labels made automatically from the
agent's words follow the rules when the words allow it and are kept either way.

Everything else (`log`, `status`, `update`, `publish`, …) is listed by
`whatran -h`.

## Three situations

**Starting fresh.** Ask the agent to analyse. It runs `init`, works, and
finishes with `check`; the record and the package grow at the end of each turn.
Commit and push; CI reruns the package on a fresh clone and publishes the result
page with the graph.

**Installing halfway through.** Nothing before `init` was captured. The agent
reruns the earlier steps through `whatran.py run -- CMD` when that is cheap, or
records them with `record "..." --cmd "<command>" --out <files>`. Those steps
are `declared` rather than `observed`, but they still go into `run.sh`, so
`check` can replay-verify them.

**Across several conversations or agents.** The record lives in the repository.
A new session (Codex, Claude Code, a person) starts from
`python3 analysis/whatran.py status` or `open`, then keeps recording; a redone
step gets `--supersedes <old id>`. The viewer's "Copy context for an agent"
hands a node's exact inputs, code and parameters to the next session.

## What the record can and cannot say

| mark | meaning |
| --- | --- |
| run `observed` | a recorder saw the command run, its exit status, and the files it changed |
| run `declared` | only the agent's word; no execution was captured |
| run `auto` | the step was made from a captured execution without the agent naming it; `label_from` says whether the label came from the agent's description of the command, its words before running it, or the command |
| artifact `intact` / `modified` / `missing` | whether the file on disk still matches the hash taken when it was recorded |
| run `replay-verified` | `check` (or CI) reran the packaged path from clean and the outputs matched |

A passed check means the declared outputs regenerated from the declared inputs
and matched the committed references under the rules in `whatran.json`. It does
not establish scientific correctness or that every method decision was
captured. Rejected branches and every captured command stay in the record.

## Examples

- `examples/recorded-demo/` — the whole flow on a synthetic peak-detection
  analysis: captured steps, a rejected branch, claims, package, check.
  `bash make_demo.sh` rebuilds it from scratch.
- `examples/pipeline-demo/`, `examples/cancer-classification/`,
  `examples/multi-analysis-demo/` — hand-built packages: the smallest one, a
  real two-path analysis with a [live result page](https://qchiujunhao.github.io/whatran/example/),
  and several questions in one repository.

## Layout

```text
plugins/whatran/
├── bin/whatran.py                  the tool: one file, no dependencies, Python 3.9+
├── hooks/                          session rule, Bash capture, turn end (Claude Code)
├── skills/record-analysis/         what to record, and when
├── skills/package-analysis/        how to package, and what a pass means
├── schema/analysis-graph.schema.json
└── viewer/index.html               the graph viewer (static)
r/whatran/                          the R interface (calls the same command)
pyproject.toml                      pip install whatran -> the `whatran` command and the Python API
```

Design: [docs/DESIGN.md](docs/DESIGN.md). Tests: `python3 -m unittest discover -v`.

## License

MIT
