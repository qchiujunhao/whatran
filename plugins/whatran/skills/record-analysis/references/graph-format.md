# Graph format (analysis-graph/0.1)

`analysis/graph.json` is compiled from `analysis/events.jsonl`. It is a view;
regenerate it with `whatran.py done` (or `compile`) instead of editing it. The JSON Schema is in
`schema/analysis-graph.schema.json` at the plugin root.

## Three node types

| type | PROV analogue | what it is | identity |
| --- | --- | --- | --- |
| `artifact` | Entity | a file: raw data, intermediate, result, figure | `path` + `sha256`; same path with new content is a new node that `supersedes` the old one |
| `run` | Activity | one scientific step that actually executed | explicit `id` (`run-<slug>-<n>`) |
| `claim` | Entity (derived) | a conclusion and what it rests on | explicit `id` (`claim-<slug>-<n>`) |

Edges: `used` (artifact → run), `generated` (run → artifact), `supports`
(run | artifact | claim → claim), `supersedes` (new → old). Branches are not a
node type: two runs that use the same input are a branch.

## Run node

```json
{
  "id": "run-peak-detection-2",
  "type": "run",
  "label": "Peak detection",
  "command": "python scripts/detect_peaks.py --ppm 15 data/raw.mzML results/peaks.csv",
  "code": [{"path": "scripts/detect_peaks.py", "sha256": "…"}],
  "params": {"ppm": 15, "peakwidth": [5, 30]},
  "inputs": ["art-raw-mzml-1"],
  "outputs": ["art-peaks-csv-2"],
  "status": "completed",
  "evidence": {
    "level": "observed",
    "exec_ids": ["x-0007"],
    "source": "claude-code-hook",
    "exit_code": 0,
    "started": "2026-10-14T18:02:11Z",
    "duration_ms": 8130,
    "outputs_observed": ["results/peaks.csv"]
  },
  "environment": {"python": "3.12.4", "platform": "macOS-15.6-arm64"},
  "git": {"commit": "3f1c…", "dirty": true},
  "verified_by_replay": false,
  "supersedes": "run-peak-detection-1",
  "recorded_at": "2026-10-14T18:03:40Z",
  "recorded_by": "claude-code"
}
```

`status`: `completed` | `failed` | `rejected` | `superseded`.
`evidence.level`: `observed` (a recorder saw it run) | `declared` (asserted).

## Artifact node

```json
{
  "id": "art-peaks-csv-2",
  "type": "artifact",
  "label": "peaks.csv",
  "path": "results/peaks.csv",
  "sha256": "…",
  "size": 48213,
  "version": 2,
  "supersedes": "art-peaks-csv-1",
  "role": "intermediate",
  "integrity": "intact",
  "generated_by": "run-peak-detection-2",
  "used_by": ["run-feature-matrix-1"]
}
```

`role` and `integrity` are computed at compile time. `integrity` compares the
file on disk with the recorded hash: `intact` | `modified` | `missing` |
`unchecked` (no hash: file too large or absent when recorded).

## Claim node

```json
{
  "id": "claim-metabolite-x-elevated-1",
  "type": "claim",
  "label": "Metabolite X is elevated in group B (FDR 0.01)",
  "supported_by": ["run-differential-abundance-1", "art-volcano-svg-1"],
  "status": "tentative"
}
```

`status`: `tentative` | `supported` | `rejected`. A claim is the terminal of the
result path; it never means the conclusion was independently validated.

## Main path

`main_path` lists what the viewer shows by default: the dependency closure of the
non-rejected claims (or, with no claims, of the result artifacts of completed
runs), plus the other outputs of those runs. Rejected, failed and superseded
branches are off the path but stay in `nodes`, and every captured command stays
in `exec_log` whether or not a node links to it.

## Ledger events (`events.jsonl`)

One JSON object per line, never rewritten:

- `exec` - a command ran (written by hooks or `whatran.py run`): command, cwd,
  exit code, timing, output head, files changed under the watched directories
  with their hashes.
- `node` - a node declaration (artifact / run / claim).
- `status` - a status change with a reason (reject, supersede).
- `update` - amended label, note, params or status.

Agents that cannot run `whatran.py` can emit a `graph.json` directly in this format;
their runs are `declared` by construction.
