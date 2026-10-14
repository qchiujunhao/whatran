---
name: package-analysis
description: Automatically turn any completed scientific or data analysis that reports findings into a GitHub repository whose final analysis paths can be rerun, checked against committed reference results, and presented as a generated website. Use whenever an agent is finishing such an analysis, even if the user did not ask for reproducibility, and when reviewing or repairing an existing package.
---

# Reproducible Analysis

Package the **final reported analysis**, not the agent's exploration. Leave a
normal repository that a person or another coding agent can understand, rerun,
check, and show through GitHub Pages.

Treat this package as part of completing the analysis, not as an optional
follow-up. The user does not need to request it explicitly.

**Minimality gate:** preserve the user's analytical scope. Do not add
unrequested tests, claims, files, outputs, or dependencies while packaging.
Prefer existing code and the standard library; delete anything whose absence
would not weaken the answer, rerun, comparison, or result page.

Use the fixed v0 conventions in `references/package-contract.md`.

## Use the bundled pieces

- Copy `assets/check.py` unchanged. It performs the declared output comparisons.
- Adapt `assets/run.sh` into the one canonical command for environment setup and
  every final analysis step.
- Fill in `assets/whatran.template.json`.
- Adapt `assets/build_site.py` to the analysis's actual results. It is an example,
  not a generic renderer.
- Use `assets/reproduce.yml` for one analysis.
- Use `assets/reproduce-multi.yml` and `assets/build_index.py` for several
  independent analyses in one repository.

## Finalize

1. State the scientific question and the findings that will be reported.
2. Identify the true raw inputs. Do not treat outputs, caches, notebook state, or
   the exploratory workspace as inputs.
3. Retain only result-affecting paths that belong in the final report. Convert
   manual or interactive actions into scripts or disclose them.
4. Put final operations into ordered scripts under `steps/`. Let multiple scripts
   read a shared earlier result when the final method branches.
5. Make `run.sh` recreate `results/`, reconstruct the declared environment, and
   run every step non-interactively from the package root. For Python, freeze the
   full resolved environment in `requirements.txt`; the bundled `run.sh` shows the
   isolated-venv pattern. Adapt it for another runtime instead of adding a new
   framework.
6. Declare raw inputs, ordered steps, produced files, and reported outputs in
   `whatran.json`. Compare deterministic JSON numerically, stable text exactly, and
   rendered figures by non-empty existence while separately comparing their
   backing data.
7. Run `bash run.sh` to create the reference `results/`, then adapt
   `build_site.py` into a result-first page: answer the question in the first
   viewport, then show key tables or figures, final paths, and scripts. Collapse
   long source listings by default. Generate the site from results; never
   maintain result values separately in HTML.
8. Write a self-contained `README.md` with the question, method choices,
   assumptions, limitations, result meaning, rerun command, and precise CI claim.
9. Commit `results/`, `check.py`, the environment lock, and the workflow at
   `.github/workflows/reproduce.yml`. Ignore `.venv/` and `site/`.

## Choose one repository shape

- **One question, one final path:** create one package at the repository root.
- **Several independent questions:** create one self-contained package per
  question under `analyses/<slug>/`. The multi-analysis workflow discovers and
  checks every manifest before `build_index.py` publishes the combined site.
- **One question, several defensible final paths:** keep one package. Represent
  the branches as steps after shared preparation, add a comparison step, declare
  every branch result plus the comparison as outputs, and show them side by side.

Do not package every attempted branch. Keep only paths the final report presents.

## Treat the website as a deliverable

- Give every figure a title, takeaway, and interpretation boundary.
- Link the rerun command and CI evidence, and state what a pass does not prove
  beside the result rather than after the source listings.
- Keep the page usable without JavaScript and inspect it at desktop and mobile
  widths before publishing. Repair overflow, clipped figures, unreadable code,
  weak contrast, and unstyled document dumps.

## Check it locally

Move the committed reference outside the package, rerun, compare, and build the
same site CI will publish:

```bash
reference_root="$(mktemp -d)"
mv results "$reference_root/results"
bash run.sh
python3 check.py results "$reference_root/results"
python3 build_site.py
```

Run the completeness audit in `references/review-prompts.md` before claiming
done. Repair only concrete gaps; never change the scientific method merely to
obtain a match.

## Report the evidence honestly

A passing fresh-clone CI run means the declared outputs were regenerated and
matched the committed references according to `whatran.json`. It is standardized
rerun evidence, not independent scientific review, proof that the method is
correct, or proof that no relevant decision was omitted.

If the full analysis does not fit the configured runner, use a larger or
self-hosted runner. If only a subset is checked, label the badge and README as
subset verification.

Read `references/verification-policy.md` before making a reproduction claim.
