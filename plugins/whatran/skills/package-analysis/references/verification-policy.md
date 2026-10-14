# Verification policy

Use one standard check:

1. Start from a fresh clone.
2. Move committed `results/` outside the analysis package.
3. Run the package's canonical `run.sh`.
4. Compare regenerated and committed outputs with the unchanged `check.py`.
5. Build the website from the regenerated results.

Moving the reference outside the package prevents accidental reuse through the
normal results path. It is not a security boundary against deliberately hostile
analysis code.

The package owns its scripts, manifest, and reference results. GitHub Actions
therefore supplies automated evidence on another clean machine, not an
independent scientific judgment.

A passing run supports only this statement:

> The declared outputs regenerated from the declared inputs and matched the
> committed references under the comparisons in `whatran.json`.

It does not establish scientific correctness, completeness of method capture, or
validity on new data.

Check the full analysis when it fits the runner. Otherwise use a larger runner or
clearly label a representative-subset run as `subset`; never present it as a
full-data reproduction.
