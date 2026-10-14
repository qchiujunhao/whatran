#!/usr/bin/env python3
"""Welch t-test per detected peak between groups A and B. Pure Python.

Writes stats.json (one record per peak) and a volcano plot as hand-written SVG.
"""
import csv
import json
import math
import sys
from collections import defaultdict

src, dst_json, dst_svg = sys.argv[1:4]
ALPHA = 0.05


def betacf(a, b, x, max_iter=200, eps=3e-14):
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c; c = c if abs(c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c; c = c if abs(c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * betacf(a, b, x) / a
    return 1.0 - front * betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t, df):
    x = df / (df + t * t)
    return betainc(df / 2.0, 0.5, x)


def welch(a, b):
    na, nb = len(a), len(b)
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((v - ma) ** 2 for v in a) / (na - 1)
    vb = sum((v - mb) ** 2 for v in b) / (nb - 1)
    se2 = va / na + vb / nb
    t = (mb - ma) / math.sqrt(se2) if se2 > 0 else 0.0
    df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)) if se2 > 0 else na + nb - 2
    return ma, mb, t, df, t_two_sided_p(abs(t), df)


groups = defaultdict(lambda: defaultdict(list))
mz_of = {}
with open(src, newline="") as fh:
    for row in csv.DictReader(fh):
        groups[row["peak_id"]][row["group"]].append(float(row["intensity"]))
        mz_of[row["peak_id"]] = float(row["mz"])

features = []
for pid in sorted(groups):
    a, b = groups[pid]["A"], groups[pid]["B"]
    if len(a) < 2 or len(b) < 2:
        continue
    ma, mb, t, df, p = welch(a, b)
    features.append({"peak_id": pid, "mz": mz_of[pid], "mean_a": round(ma, 4), "mean_b": round(mb, 4),
                     "log2fc": round(math.log2(mb / ma), 4), "t": round(t, 4), "df": round(df, 2),
                     "p": p, "significant": p < ALPHA})

n_a = len(next(iter(groups.values()))["A"]) if groups else 0
n_b = len(next(iter(groups.values()))["B"]) if groups else 0
with open(dst_json, "w") as fh:
    json.dump({"test": "welch", "alpha": ALPHA, "n_a": n_a, "n_b": n_b, "features": features}, fh, indent=2)

# volcano plot ---------------------------------------------------------------
W, H, L, T = 520, 360, 56, 24
xs = [f["log2fc"] for f in features] or [0.0]
ys = [-math.log10(max(f["p"], 1e-12)) for f in features] or [0.0]
xmax = max(1.0, max(abs(v) for v in xs) * 1.15)
ymax = max(1.5, max(ys) * 1.15)
sx = lambda v: L + (v + xmax) / (2 * xmax) * (W - L - 16)
sy = lambda v: H - 40 - v / ymax * (H - 40 - T)
parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif" font-size="11">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<line x1="{L}" y1="{T}" x2="{L}" y2="{H - 40}" stroke="#444"/>',
         f'<line x1="{L}" y1="{H - 40}" x2="{W - 16}" y2="{H - 40}" stroke="#444"/>',
         f'<line x1="{sx(0)}" y1="{T}" x2="{sx(0)}" y2="{H - 40}" stroke="#bbb" stroke-dasharray="3 3"/>',
         f'<line x1="{L}" y1="{sy(-math.log10(ALPHA))}" x2="{W - 16}" y2="{sy(-math.log10(ALPHA))}" stroke="#bbb" stroke-dasharray="3 3"/>',
         f'<text x="{(L + W) / 2}" y="{H - 10}" text-anchor="middle">log2 fold change (B / A)</text>',
         f'<text x="14" y="{(T + H - 40) / 2}" text-anchor="middle" transform="rotate(-90 14 {(T + H - 40) / 2})">-log10 p</text>',
         f'<text x="{sx(-xmax)}" y="{H - 26}" text-anchor="middle">{-xmax:.1f}</text>',
         f'<text x="{sx(xmax)}" y="{H - 26}" text-anchor="middle">{xmax:.1f}</text>',
         f'<text x="{L - 6}" y="{sy(ymax) + 4}" text-anchor="end">{ymax:.1f}</text>',
         f'<text x="{L - 6}" y="{sy(0) + 4}" text-anchor="end">0</text>']
for f, x, y in zip(features, xs, ys):
    color = "#b91c1c" if f["significant"] else "#9ca3af"
    parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5" fill="{color}" fill-opacity="0.85"/>')
    if f["significant"]:
        parts.append(f'<text x="{sx(x) + 8:.1f}" y="{sy(y) + 4:.1f}">m/z {f["mz"]}</text>')
parts.append("</svg>")
with open(dst_svg, "w") as fh:
    fh.write("\n".join(parts) + "\n")

sig = [f for f in features if f["significant"]]
print(f"{len(features)} peaks tested, {len(sig)} significant at alpha={ALPHA}: " +
      ", ".join(f'm/z {f["mz"]} (log2fc {f["log2fc"]:+.2f}, p={f["p"]:.2g})' for f in sig))
