#!/usr/bin/env python3
"""Simulate a tiny LC-MS-like dataset: n samples x 250 m/z bins, two groups, three true features.

Pure Python, deterministic for a given seed. Output is a long CSV: sample,group,mz,intensity.
"""
import argparse
import csv
import random

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, default=7)
ap.add_argument("--n", type=int, default=12, help="number of samples (half per group)")
ap.add_argument("out")
args = ap.parse_args()

rng = random.Random(args.seed)
bins = 250
mz = [round(100.1 + i, 1) for i in range(bins)]
# bin index -> (group with the higher level or None, peak height)
true_peaks = {51: ("A", 8.0), 201: ("B", 12.0), 230: (None, 10.0)}

with open(args.out, "w", newline="") as fh:
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(["sample", "group", "mz", "intensity"])
    for s in range(args.n):
        group = "A" if s < args.n // 2 else "B"
        signal = [abs(rng.gauss(1.0, 0.3)) for _ in range(bins)]
        for idx, (hi, height) in true_peaks.items():
            h = height * (1.7 if hi == group else 1.0) * rng.uniform(0.85, 1.15)
            signal[idx] += h
            signal[idx - 1] += h * 0.45
            signal[idx + 1] += h * 0.45
        for i in range(bins):
            w.writerow([f"S{s + 1:02d}", group, mz[i], round(signal[i], 4)])
print(f"wrote {args.out}: {args.n} samples x {bins} bins")
