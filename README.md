# Whatran

[![test](https://github.com/qchiujunhao/whatran/actions/workflows/test.yml/badge.svg)](https://github.com/qchiujunhao/whatran/actions/workflows/test.yml)
[![showcase](https://img.shields.io/badge/showcase-GitHub%20Pages-0969da)](https://qchiujunhao.github.io/whatran/)

Whatran is one Skill for Codex and Claude Code. It turns a completed scientific
analysis into a normal GitHub repository that:

- reruns the final reported analysis from declared inputs;
- compares regenerated outputs with committed reference results;
- shows the final steps, scripts, findings, and visualizations through GitHub
  Pages; and
- can be understood and rerun by another person or coding agent.

It captures final analysis paths, not chat history, debugging attempts, or
discarded branches. Whatran is not a new agent platform, workflow engine, registry,
or hosted execution service.

See the generated [live showcase](https://qchiujunhao.github.io/whatran/).

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

After installation, the Skill applies automatically when the coding agent
finishes a scientific or data analysis. The Claude Code plugin loads this
completion rule at session start. You can also invoke
`$package-analysis` directly in Codex.

For local development, clone this repository and replace
`qchiujunhao/whatran --ref main` with `.` in the Codex marketplace command, or
replace `qchiujunhao/whatran` with `.` in the Claude Code command.

## What the Skill produces

```text
whatran.json                    declared inputs, steps, and outputs
run.sh                        one canonical rerun command
requirements.txt             resolved environment lock for Python analyses
data/                         raw inputs or checked download instructions
steps/                        final result-affecting scripts
results/                      committed reference results
check.py                      fixed declared-output comparator
build_site.py                 analysis-specific result-page generator
.github/workflows/reproduce.yml
README.md
```

For several independent questions, each package lives under
`analyses/<slug>/`, with one generated landing page. For several defensible paths
to the same question, one package keeps the paths as branches and includes an
explicit comparison step.

## How checking works

On every push, GitHub Actions starts from a fresh clone, moves committed results
outside the analysis package, runs `run.sh`, compares the regenerated outputs,
and builds the website from the regenerated results.

The same check can be run locally:

```bash
reference_root="$(mktemp -d)"
mv results "$reference_root/results"
bash run.sh
python3 check.py results "$reference_root/results"
python3 build_site.py
```

A pass means the declared outputs regenerated and matched according to
`whatran.json`. It does not establish scientific correctness or prove that no
relevant method decision was omitted.

## Examples

- `examples/pipeline-demo/` — the smallest complete, standard-library pipeline.
- `examples/cancer-classification/` — a real dependency-heavy analysis with two
  defensible model paths, explicit comparison, numeric checks, and a generated
  [live result page](https://qchiujunhao.github.io/whatran/example/).
- `examples/multi-analysis-demo/` — two independent questions checked and shown
  in one repository website.

The installable product is entirely under `plugins/whatran/`. Examples demonstrate
what the Skill produces; they are not required after installation.

## License

MIT
