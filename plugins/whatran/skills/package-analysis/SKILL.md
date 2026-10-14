---
name: package-analysis
description: Turn a completed scientific or data analysis that reports findings into a repository that reruns the result path from declared inputs, checks the regenerated outputs against committed references on a fresh clone, and shows the result on a generated page. Use whenever an agent is finishing such an analysis, even if the user did not ask for reproducibility, and when reviewing or repairing an existing package. Stage two of Whatran; record-analysis is stage one.
---

# Package the result

Package the **final reported path**, not the exploration. The result is a normal
repository that a person or another agent can rerun, check, and read.

Treat this as part of completing the analysis; the user does not need to ask.

## With a recorded graph (the normal case)

```bash
python3 analysis/whatran.py package     # whatran.json, run.sh, check.py, build_site.py, .github/workflows/reproduce.yml
python3 analysis/whatran.py check       # move results/ aside, bash run.sh, compare, mark the steps replay-verified
```

`package` derives everything from the result path of the graph: inputs, the
completed steps in dependency order (rejected branches excluded), the result
files with a comparison rule each. `run.sh` is the recorded commands in order.
Before `check`:

1. Read `run.sh`. Add dependency installation under "Environment" if the steps
   need packages (for Python, freeze `requirements.txt` and use a venv as the
   template shows). Do not change what the steps compute.
2. Read `whatran.json`. Inputs must be raw inputs only. Outputs are compared
   `numeric` (JSON, with tolerance), `exact` (stable text) or `exists`
   (rendered figures; compare their backing data separately). Fix the guesses.
3. If a step has no recorded command (`declared`), write it as a script and put
   the command in `run.sh`; `check` fails loudly until you do.

`check` fails when run.sh fails or outputs differ; the committed results are put
back either way. Repair only concrete gaps. Never change the method, thresholds,
inputs or conclusion merely to obtain a match; if a correction changes the
method, say so and record it as a new step.

Then commit `results/`, `whatran.json`, `run.sh`, `check.py`, `build_site.py`,
the workflow and the environment lock. Ignore `.venv/` and `site/`. CI repeats
`check` on every push and publishes `site/` (result page plus the graph).

**Minimality:** do not add unrequested tests, claims, files, outputs or
dependencies while packaging. Delete anything whose absence would not weaken
the answer, the rerun, the comparison or the result page.

## Without a recorded graph

Use `assets/` directly: copy `check.py` unchanged, adapt `run.sh`, fill in
`whatran.template.json`, adapt `build_site.py` (an example page) or use
`build_site_from_graph.py`, add `reproduce.yml`. Several independent questions
in one repository: one package per `analyses/<slug>/`, `reproduce-multi.yml`
and `build_index.py`. `references/package-contract.md` is the full contract.

## Report the evidence honestly

A passing check means the declared outputs regenerated from the declared inputs
and matched the committed references under `whatran.json`. It is rerun
evidence, not scientific review, not proof the method is right, not proof that
nothing was omitted. If CI checks only a subset of the data, label it as a
subset. `references/verification-policy.md` has the exact wording;
`references/review-prompts.md` has an audit prompt to run before claiming done.
