# Package contract

Use these fixed v0 paths:

```text
whatran.json
run.sh
requirements.txt
README.md
check.py
build_site.py
data/
steps/
results/
.github/workflows/reproduce.yml
```

For a non-Python analysis, replace `requirements.txt` with the runtime's lock but
keep `run.sh` as the canonical entrypoint.

`whatran.json` is intentionally small:

```json
{
  "method": {
    "title": "Human title",
    "question": "Question answered by the final analysis"
  },
  "inputs": ["data/measurements.csv"],
  "run": "bash run.sh",
  "results_dir": "results",
  "steps": [
    {
      "id": "clean",
      "title": "Clean raw measurements",
      "script": "steps/01_clean.py",
      "produces": ["results/clean.csv"]
    },
    {
      "id": "summarize",
      "title": "Compute statistics",
      "script": "steps/02_summarize.py",
      "produces": ["results/summary.json"]
    }
  ],
  "outputs": [
    {"path": "summary.json", "compare": "numeric", "tolerance": 1e-9},
    {"path": "chart.svg", "compare": "exists"}
  ]
}
```

- Keep every path relative and inside the package. Do not use absolute paths or
  `..`.
- List raw inputs only. Never list outputs, caches, or intermediates as inputs.
- Order `steps` topologically. For alternative paths to one question, place both
  branches after shared preparation and put their comparison after both.
- Make every reported table, metric, and plot-backing dataset an `output`.
- Use `numeric` for JSON. Numbers use the declared absolute tolerance; keys,
  lengths, list order, strings, and booleans must match exactly.
- Use `exact` only for byte-stable files.
- Use `exists` for rendered artifacts whose bytes may vary. It requires a
  non-empty regular file and a committed reference; compare the data behind the
  rendering separately.

The README must explain all result-affecting choices, input roles, output meaning,
assumptions, limitations, and whether CI checks full data or a declared subset.
