#!/usr/bin/env python3
"""Local-maximum peak detection on the mean spectrum.

A bin is a peak when its mean intensity across samples is a local maximum and
exceeds threshold x median(mean spectrum). Writes a long CSV of per-sample
intensities at each detected peak: peak_id,mz,sample,group,intensity.
"""
import argparse
import csv
from collections import OrderedDict, defaultdict

ap = argparse.ArgumentParser()
ap.add_argument("--threshold", type=float, required=True, help="multiple of the median mean-intensity")
ap.add_argument("src")
ap.add_argument("dst")
args = ap.parse_args()

per_bin = OrderedDict()      # mz -> list of (sample, group, intensity)
with open(args.src, newline="") as fh:
    for row in csv.DictReader(fh):
        per_bin.setdefault(float(row["mz"]), []).append((row["sample"], row["group"], float(row["intensity"])))

mzs = list(per_bin)
means = [sum(v[2] for v in per_bin[m]) / len(per_bin[m]) for m in mzs]
median = sorted(means)[len(means) // 2]
floor = args.threshold * median

peaks = []
for i in range(1, len(mzs) - 1):
    if means[i] > floor and means[i] >= means[i - 1] and means[i] >= means[i + 1]:
        peaks.append(i)

with open(args.dst, "w", newline="") as fh:
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(["peak_id", "mz", "sample", "group", "intensity"])
    for k, i in enumerate(peaks, 1):
        for sample, group, inten in per_bin[mzs[i]]:
            w.writerow([f"P{k:03d}", mzs[i], sample, group, inten])
print(f"{len(peaks)} peaks above {args.threshold} x median ({floor:.2f}) -> {args.dst}")
