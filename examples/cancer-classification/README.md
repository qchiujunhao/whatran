# Tumor classification across two model paths

This example asks one scientific question through two defensible final analysis
paths:

> Can tumor measurements separate malignant from benign, and is that conclusion
> stable across a linear and a nonlinear model?

Both paths use the same seeded stratified train/test split:

1. standardized logistic regression;
2. a seeded 200-tree random forest.

A final comparison selects by mean five-fold training-set cross-validation ROC
AUC and calls the conclusion stable when both paths reach the declared 0.95
threshold. The held-out test set is reported but is not used for selection.

## Input and result-affecting choices

The sole raw input is `data/cancer.csv`: 569 rows, 30 numeric tumor
measurements, and a binary target from scikit-learn's bundled Wisconsin
Diagnostic Breast Cancer dataset. `export_data.py` records how that CSV was
created, but it is not part of the final pipeline; the committed CSV is the
declared input.

The shared split is stratified, uses seed 42, and assigns 25% of rows to the
held-out test set (426 training rows and 143 test rows). Target value 1, benign,
is the positive class.

- The linear path standardizes every feature and fits logistic regression with
  the `liblinear` solver, seed 42, and `max_iter=5000`.
- The nonlinear path fits a 200-tree random forest with seed 42 and one worker.
- Both paths classify at probability 0.5.
- Training-set model assessment uses the same seeded, shuffled, stratified
  five-fold cross-validation and ROC AUC. The path with the higher mean
  cross-validation ROC AUC is selected.
- Held-out accuracy, precision, recall, F1, and ROC AUC are descriptive
  evaluations of the fixed test split.

## Result

| Path | Test accuracy | Test ROC AUC | CV ROC AUC |
|---|---:|---:|---:|
| Logistic regression | 0.986 | 0.998 | 0.996 ± 0.004 |
| Random forest | 0.958 | 0.995 | 0.989 ± 0.010 |

Both model families support strong separation, and logistic regression has the
higher mean cross-validation ROC AUC for this declared comparison. The positive
class is benign.

## Final pipeline

```text
data/cancer.csv
        |
        v
seeded shared split
   |             |
   v             v
logistic      random forest
   |             |
   +------ comparison ------+
                |
                v
        tables + SVG figures
```

`whatran.json` records the same topological order. Every branch metric, ROC dataset,
driver dataset, comparison, and figure is declared as an output.

## Run

```bash
PYTHON=python3.11 bash run.sh
python3 build_site.py
```

Check it the same way as CI:

```bash
reference_root="$(mktemp -d)"
mv results "$reference_root/results"
PYTHON=python3.11 bash run.sh
python3 check.py results "$reference_root/results"
python3 build_site.py
```

`run.sh` removes and rebuilds `.venv` from the resolved package versions in
`requirements.txt`, then executes all five final steps. The generated site shows
the comparison, both figures, and every final script.

## Interpretation boundary

A passing CI run means the declared outputs regenerated and matched the committed
references. It does not make either model clinically valid, prove the 0.95
interpretation threshold is uniquely correct, or establish that these are the
only defensible model families. The example performs no external validation,
calibration assessment, uncertainty interval, subgroup analysis, or clinical
utility analysis. Feature coefficients and impurity importances describe these
fitted models and are not causal effects.
