# Pipeline demo — a reproducible, shareable, self-showcasing analysis

This is the reference example for the `package-analysis` skill. It shows the
whole shape in miniature: a multi-step analysis pipeline whose declared outputs
regenerate from the raw input, checked by CI on a fresh clone, with a generated
showcase website.

## Question and result

The question is: **What is the mean value in each group?**

| Group | Included rows | Mean |
| --- | ---: | ---: |
| A | 3 | 11.0 |
| B | 3 | 21.0 |
| C | 2 | 31.0 |

The only raw input is `data/measurements.csv`, a small hand-authored
demonstration dataset rather than a sample from a defined population. Cleaning
trims whitespace, ignores blank rows, and excludes rows with a missing group,
missing value, or non-numeric value. Consequently, the `C,not-a-number` row is
not included. Valid rows are sorted by group and value before each arithmetic
mean is calculated.

These means are descriptive summaries of the supplied rows. They carry no
sampling uncertainty, support no causal claim, and should not be generalized
beyond this demonstration input.

## Layout

```text
pipeline-demo/
├── whatran.json        # lightweight manifest: inputs, steps, outputs + comparisons
├── run.sh            # orchestrator (runs the steps in order)
├── requirements.txt  # environment lock (stdlib only here)
├── data/
│   └── measurements.csv   # raw input
├── steps/
│   ├── 01_clean.py        # raw -> results/clean.csv
│   ├── 02_summarize.py    # clean -> results/summary.json  (the reported result)
│   └── 03_visualize.py    # summary -> results/chart.svg   (a regenerated plot)
├── results/          # reference outputs (committed; moved aside before re-run)
├── check.py          # compares regenerated outputs vs reference (numeric tolerance)
├── build_site.py     # builds site/index.html from the verified run
└── reproduce.yml     # -> copy to .github/workflows/reproduce.yml
```

## Run it locally

```bash
bash run.sh            # regenerate results/
python3 build_site.py  # regenerate the showcase into site/index.html
```

## Reproduce it the way CI does

Move the committed answer outside the package, then re-run:

```bash
reference_root="$(mktemp -d)"
mv results "$reference_root/results"
bash run.sh
python3 check.py results "$reference_root/results"
```

A pass means `results/summary.json` regenerated within tolerance and the chart
was rebuilt. In a real repo this runs on every push (`reproduce.yml`) in a fresh
GitHub Actions clone.

## What the badge does and does not mean

- ✅ The declared outputs regenerate from the declared raw inputs, in a
  reconstructed environment, on a clean machine.
- ❌ It does **not** mean the method is scientifically correct, nor that every
  step of the original process was captured. Those are best-effort and disclosed,
  not proven by CI.

## Implementation notes

- **Steps are explicit** so the site can show the process, the scripts, and the
  visualization — all generated from the verified run, so they never go stale.
- **The plot is compared as data, not bytes.** `summary.json` is compared with
  numeric tolerance; `chart.svg` only has to be regenerated (`exists`), because
  image bytes differ across machines for spurious rendering reasons.
- **Intermediates are regenerated.** Moving the committed `results/` outside the
  package prevents accidental reuse through the normal result paths.
