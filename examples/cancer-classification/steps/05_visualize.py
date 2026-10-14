#!/usr/bin/env python3
"""Visualize both retained model paths from their checked backing data."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "whatran"
import matplotlib.pyplot as plt  # noqa: E402


def read_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_svg(fig: object, path: str) -> None:
    fig.savefig(path, metadata={"Date": None})
    output = Path(path)
    output.write_text(
        "\n".join(line.rstrip() for line in output.read_text(encoding="utf-8").splitlines()) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    comparison = read_json("results/comparison.json")
    logistic_roc = read_json("results/logistic_roc.json")
    forest_roc = read_json("results/forest_roc.json")

    fig, ax = plt.subplots(figsize=(6, 4.5))
    for label, points, color in (
        ("Logistic regression", logistic_roc, "#4c78a8"),
        ("Random forest", forest_roc, "#f58518"),
    ):
        metrics = comparison["paths"][
            "logistic_regression" if label.startswith("Logistic") else "random_forest"
        ]
        ax.plot(
            [point["false_positive_rate"] for point in points],
            [point["true_positive_rate"] for point in points],
            label=f"{label} (AUC {metrics['roc_auc']:.3f})",
            color=color,
        )
    ax.plot([0, 1], [0, 1], "--", color="gray")
    ax.set(xlabel="False positive rate", ylabel="True positive rate", title="Test ROC curves")
    ax.legend(loc="lower right")
    fig.tight_layout()
    save_svg(fig, "results/roc_comparison.svg")
    plt.close(fig)

    logistic = read_json("results/logistic_coefficients.json")[:10][::-1]
    forest = read_json("results/forest_importance.json")[:10][::-1]
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    axes[0].barh(
        [row["feature"] for row in logistic],
        [row["magnitude"] for row in logistic],
        color="#4c78a8",
    )
    axes[0].set(title="Logistic coefficient magnitude", xlabel="Absolute standardized coefficient")
    axes[1].barh(
        [row["feature"] for row in forest],
        [row["importance"] for row in forest],
        color="#f58518",
    )
    axes[1].set(title="Random-forest importance", xlabel="Impurity importance")
    fig.tight_layout()
    save_svg(fig, "results/model_drivers.svg")
    plt.close(fig)

    print("wrote results/roc_comparison.svg and results/model_drivers.svg")


if __name__ == "__main__":
    main()
