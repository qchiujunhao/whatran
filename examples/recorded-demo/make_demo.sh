#!/usr/bin/env bash
# Rebuild the demo from scratch with the everyday commands only.
# Steps run through `whatran.py run -- CMD` (what the Claude Code hooks do automatically),
# then `record "<what it did>"` names each one; everything else is read from the command.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf analysis data results site whatran.json run.sh check.py build_site.py .github
mkdir -p data results
python3 ../../plugins/whatran/bin/whatran.py init --name "Synthetic LC-MS pilot" \
  --description "Which features differ between group A and group B in a simulated 12-sample experiment?"
W="python3 analysis/whatran.py"
export AG_AGENT="make_demo.sh"

$W run -- python3 scripts/simulate.py --seed 7 --n 12 data/raw_signal.csv
$W record "Simulate raw signal"
$W update art-raw-signal-csv-1 --label "Raw signal (synthetic)" \
  --note "Stand-in for an mzML export. Three planted features at m/z 151.1 (A high), 301.1 (B high), 330.1 (equal)."

# first attempt: too permissive; recorded, then rejected (it stays in the record)
$W run -- python3 scripts/detect_peaks.py --threshold 1.1 data/raw_signal.csv results/peaks_loose.csv
$W record "Peak detection (loose)"
$W reject run-peak-detection-loose-1 --reason "threshold=1.1 called 27 bins as peaks, most of them noise; the mean spectrum shows them at the noise floor"

# chosen attempt
$W run -- python3 scripts/detect_peaks.py --threshold 5 data/raw_signal.csv results/peaks.csv
$W record "Peak detection"

$W run -- python3 scripts/stats.py results/peaks.csv results/stats.json results/volcano.svg
$W record "Group comparison, Welch t-test" --param test=welch --param alpha=0.05

# a debugging command: captured, never recorded as a step
$W run --quiet -- "python3 -c \"import json; d=json.load(open('results/stats.json')); print(len(d['features']), 'features')\""

$W claim "Feature at m/z 301.1 is higher in group B (Welch p < 0.05)" --supports results/volcano.svg
$W claim "Feature at m/z 151.1 is higher in group A (Welch p < 0.05)"

$W done
$W package
$W check
