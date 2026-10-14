# Whatran

[![test](https://github.com/qchiujunhao/whatran/actions/workflows/test.yml/badge.svg)](https://github.com/qchiujunhao/whatran/actions/workflows/test.yml)
[![showcase](https://img.shields.io/badge/showcase-GitHub%20Pages-0969da)](https://qchiujunhao.github.io/whatran/)

**What ran?** Install Whatran, then use Codex or Claude Code exactly as before.
When the agent does a scientific or data analysis, you get two things without
asking:

1. **A graph of what actually ran.** `analysis/graph.json` plus a viewer:
   every step with its command, parameters, inputs and outputs (hashed), which
   steps were observed to run, which branches were tried and dropped, and which
   results the findings rest on.
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

The skill tells the agent to run these as it works; you can run them too.

```bash
python3 <plugin>/bin/whatran.py init          # once per project; copies whatran.py into analysis/
python3 analysis/whatran.py record "Peak detection"      # after a meaningful step
python3 analysis/whatran.py claim "X is higher in group B (p < 0.05)"
python3 analysis/whatran.py reject run-peak-detection-1 --reason "called noise as peaks"
python3 analysis/whatran.py done              # compile, check files, draft the package
python3 analysis/whatran.py package           # whatran.json, run.sh, check.py, build_site.py, CI workflow
python3 analysis/whatran.py check             # rerun from clean, compare, mark replay-verified
```

`record` needs only the label. The command that ran was captured already (by
the Claude Code hooks, or by `whatran.py run -- CMD` in any other agent), so
inputs, outputs, code and parameters are read from it; flags such as `--in`,
`--out`, `--param k=v` override when the guess is wrong. `claim` attaches to
the last completed run unless told otherwise. `run.sh` is the recorded commands
in dependency order; review it, add dependency installation if the steps need
packages, and `check` does the rest.

Everything else (`log`, `status`, `update`, `publish`, …) is listed by
`python3 analysis/whatran.py -h`.

## Three situations

**Starting fresh.** Ask the agent to analyse. It runs `init`, works, records,
claims, and finishes with `done`, `package`, `check`. Commit and push; CI reruns
the package on a fresh clone and publishes the result page with the graph.

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
| artifact `intact` / `modified` / `missing` | whether the file on disk still matches the hash taken when it was recorded |
| run `replay-verified` | `check` (or CI) reran the packaged path from clean and the outputs matched |

A passed check means the declared outputs regenerated from the declared inputs
and matched the committed references under the rules in `whatran.json`. It does
not establish scientific correctness or that every method decision was
captured. Rejected branches and every captured command stay in the record.

## Examples

- `examples/recorded-demo/` — the whole flow on a synthetic peak-detection
  analysis: record, a rejected branch, claims, package, check. `bash make_demo.sh`
  rebuilds it from scratch.
- `examples/pipeline-demo/`, `examples/cancer-classification/`,
  `examples/multi-analysis-demo/` — hand-built packages: the smallest one, a
  real two-path analysis with a [live result page](https://qchiujunhao.github.io/whatran/example/),
  and several questions in one repository.

## Layout

```text
plugins/whatran/
├── bin/whatran.py                  the tool: one file, no dependencies, Python 3.9+
├── hooks/                          session rule + Bash capture (Claude Code)
├── skills/record-analysis/         what to record, and when
├── skills/package-analysis/        how to package, and what a pass means
├── schema/analysis-graph.schema.json
└── viewer/index.html               the graph viewer (static)
```

Design: [docs/DESIGN.md](docs/DESIGN.md). Tests: `python3 -m unittest discover -v`.

## License

MIT
