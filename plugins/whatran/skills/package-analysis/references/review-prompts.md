# Review and repair prompts

## Method audit

```text
Act as an adversarial scientific method reviewer. You do not grant pass status.
Find concrete information that a context-free reproducer would need but cannot
obtain from the package. Compare README.md, whatran.json, the step scripts,
declared inputs, and generated outputs. Search specifically for
implicit filtering, manual or visual decisions, result-dependent thresholds,
missing-value and duplicate handling, database/reference drift, hidden caches,
hard-coded current-data details, and claims without output evidence.

Return findings only. For each finding provide severity, artifact evidence,
method consequence, and the smallest correction that preserves the method.
```

## Repair

```text
Repair only the supplied validation, execution, comparison, or review findings.
Do not change the scientific question, method semantics, thresholds, inputs, or
reported conclusion merely to obtain a pass. If a correction requires a semantic
method change, stop and identify it as a new Method version. Treat declared
scientific input bytes as immutable. Never delete records, rewrite data, or swap
an input merely to make execution succeed; a justified input change requires a
new run and explicit user approval. After editing, move the reference results
outside the package, run `bash run.sh`, compare with `python3 check.py`, and
report that evidence separately from your own assessment.
```

## Context-free reproduction

```text
You have no access to the originating conversation or exploratory workspace.
Using only this package and its declared inputs, explain the method briefly and
run its canonical implementation. Do not use reference outputs as computational
inputs and do not silently invent missing method decisions. Report whether you
could regenerate the declared outputs, which information was missing, and any
difference between the documented and executed method.
```
