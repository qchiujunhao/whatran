#!/usr/bin/env python3
"""Build the result-first showcase from the regenerated two-path analysis."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

OUT_DIR = Path("site")
MODEL_HEADERS = [
    ("logistic_regression", "Logistic regression"),
    ("random_forest", "Random forest"),
]
METRICS = [
    ("Accuracy", "accuracy"),
    ("F1 score", "f1"),
    ("Test ROC AUC", "roc_auc"),
]


def score(value: object) -> str:
    return f"{float(value):.3f}"


def main() -> None:
    manifest = json.loads(Path("whatran.json").read_text(encoding="utf-8"))
    method = manifest["method"]
    results = Path(manifest["results_dir"])
    comparison = json.loads((results / "comparison.json").read_text(encoding="utf-8"))

    selected = comparison["selected_path"]
    selected_label = dict(MODEL_HEADERS)[selected]
    title = html.escape(method["title"])
    question = html.escape(method["question"])
    conclusion = html.escape(comparison["conclusion"])
    row_count = comparison["paths"]["logistic_regression"]["n_train"] + comparison["paths"]["logistic_regression"]["n_test"]
    threshold_statement = (
        "Both retained model families exceeded"
        if comparison["both_paths_reach_threshold"]
        else "At least one retained model family did not exceed"
    )

    headers = "".join(
        f"<th scope='col'>{html.escape(label)}"
        + (" <span class='selected'>Selected</span>" if key == selected else "")
        + "</th>"
        for key, label in MODEL_HEADERS
    )
    rows = "".join(
        "<tr>"
        f"<th scope='row'>{html.escape(label)}</th>"
        + "".join(
            f"<td>{score(comparison['paths'][key][metric])}</td>"
            for key, _ in MODEL_HEADERS
        )
        + "</tr>"
        for label, metric in METRICS
    )
    cv_row = (
        "<tr><th scope='row'>Five-fold CV ROC AUC</th>"
        + "".join(
            f"<td>{score(comparison['paths'][key]['cv_roc_auc_mean'])} "
            f"<span class='muted'>± {score(comparison['paths'][key]['cv_roc_auc_std'])}</span></td>"
            for key, _ in MODEL_HEADERS
        )
        + "</tr>"
    )

    step_cards = []
    for index, step in enumerate(manifest["steps"], start=1):
        source = Path(step["script"]).read_text(encoding="utf-8")
        produces = ", ".join(Path(path).name for path in step["produces"])
        step_cards.append(
            "<details class='step'>"
            "<summary>"
            f"<span class='step-number'>{index:02}</span>"
            f"<span><strong>{html.escape(step['title'])}</strong>"
            f"<small>{html.escape(step['script'])}</small></span>"
            "<span class='open-label'>View script</span>"
            "</summary>"
            f"<p class='produces'><strong>Produces:</strong> {html.escape(produces)}</p>"
            f"<pre><code>{html.escape(source)}</code></pre>"
            "</details>"
        )

    OUT_DIR.mkdir(exist_ok=True)
    for figure in ("roc_comparison.svg", "model_drivers.svg"):
        shutil.copy2(results / figure, OUT_DIR / figure)

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="A generated, fresh-clone-checked two-path analysis.">
  <meta name="theme-color" content="#102a2b">
  <title>{title} · Whatran example</title>
  <style>
    :root {{
      --paper: #f6f3ec;
      --white: #fffef9;
      --ink: #132827;
      --muted: #5a6966;
      --line: #d7d4ca;
      --teal-dark: #075c54;
      --coral: #bd432f;
      --night: #102a2b;
      --blue: #4c78a8;
      --orange: #f58518;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    a {{ color: inherit; }}
    a:focus-visible, summary:focus-visible {{ outline: 3px solid var(--coral); outline-offset: 4px; }}
    .wrap {{ width: min(1120px, calc(100% - 2.5rem)); margin: 0 auto; }}
    nav {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 1.2rem 0;
      border-bottom: 1px solid var(--line);
    }}
    .brand {{ display: inline-flex; align-items: center; gap: .6rem; font-weight: 850; text-decoration: none; }}
    .brand-mark {{
      display: grid;
      place-items: center;
      width: 1.9rem;
      aspect-ratio: 1;
      border-radius: .55rem;
      background: var(--night);
      color: #baf3e9;
      font: .8rem ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .nav-links {{ display: flex; gap: 1rem; color: var(--muted); font-size: .9rem; }}
    .nav-links a {{ text-decoration: none; }}
    .nav-links a:hover {{ color: var(--ink); }}
    header {{ padding: 4.5rem 0 3rem; }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: .5rem;
      margin: 0 0 1rem;
      color: var(--teal-dark);
      font-size: .77rem;
      font-weight: 800;
      letter-spacing: .11em;
      text-transform: uppercase;
    }}
    .eyebrow::before {{ content: ""; width: 1.7rem; height: 2px; background: var(--coral); }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{
      max-width: 900px;
      margin-bottom: 1.25rem;
      font-size: clamp(2.8rem, 7vw, 6rem);
      line-height: .98;
      letter-spacing: -.06em;
    }}
    .question {{ max-width: 850px; color: var(--muted); font-size: clamp(1.05rem, 2vw, 1.3rem); }}
    .finding {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 2rem;
      align-items: end;
      margin-top: 2.3rem;
      padding: 1.5rem;
      border: 1px solid #b8d8d1;
      border-radius: 1rem;
      background: #e7f3ef;
    }}
    .finding strong {{ display: block; max-width: 720px; font-size: clamp(1.35rem, 3vw, 2rem); line-height: 1.18; }}
    .finding p {{ margin: .6rem 0 0; color: var(--muted); }}
    .pass {{ color: var(--teal-dark); font-size: .82rem; font-weight: 800; white-space: nowrap; }}
    .pass::before {{ content: "●"; margin-right: .45rem; color: #18a27e; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      margin-top: 1rem;
      border: 1px solid var(--line);
      border-radius: 1rem;
      background: var(--white);
      overflow: hidden;
    }}
    .stat {{ padding: 1.25rem; }}
    .stat + .stat {{ border-left: 1px solid var(--line); }}
    .stat strong {{ display: block; font-size: clamp(1.45rem, 3vw, 2rem); letter-spacing: -.035em; }}
    .stat span {{ color: var(--muted); font-size: .8rem; }}
    section {{ padding: 5rem 0; }}
    section + section {{ border-top: 1px solid var(--line); }}
    .section-head {{ display: grid; grid-template-columns: .72fr 1.28fr; gap: 2.5rem; align-items: end; margin-bottom: 2.4rem; }}
    .section-head h2 {{ margin: 0; font-size: clamp(2rem, 5vw, 4rem); line-height: 1; letter-spacing: -.05em; }}
    .section-head p {{ max-width: 620px; margin-bottom: .2rem; color: var(--muted); }}
    .table-wrap {{ min-width: 0; max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 1rem; background: var(--white); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 620px; }}
    caption {{ padding: 1rem 1.2rem; text-align: left; color: var(--muted); font-size: .86rem; }}
    th, td {{ padding: .9rem 1.2rem; border-top: 1px solid var(--line); text-align: left; }}
    thead th {{ background: #eef1ec; border-top: 0; }}
    tbody th {{ font-weight: 650; }}
    .selected {{ display: inline-block; margin-left: .35rem; padding: .15rem .4rem; border-radius: 999px; background: #ccebe3; color: var(--teal-dark); font-size: .68rem; }}
    .muted {{ color: var(--muted); }}
    .comparison-note {{ margin: 1rem 0 0; color: var(--muted); font-size: .9rem; }}
    .figures {{ display: grid; grid-template-columns: .82fr 1.18fr; gap: 1rem; margin-top: 2rem; }}
    .figures > *, .reproduce-grid > * {{ min-width: 0; }}
    figure {{ margin: 0; padding: 1.2rem; border: 1px solid var(--line); border-radius: 1rem; background: var(--white); }}
    figure h3 {{ margin-bottom: .2rem; }}
    figure img {{ display: block; width: 100%; height: auto; margin: .7rem auto 0; }}
    figcaption {{ margin-top: .8rem; color: var(--muted); font-size: .83rem; }}
    .path-key {{ display: flex; flex-wrap: wrap; gap: .65rem; margin-top: 1rem; }}
    .key {{ display: inline-flex; align-items: center; gap: .4rem; color: var(--muted); font-size: .82rem; }}
    .key::before {{ content: ""; width: .7rem; height: .7rem; border-radius: .2rem; background: var(--blue); }}
    .key.forest::before {{ background: var(--orange); }}
    .steps {{ display: grid; min-width: 0; gap: .7rem; }}
    details.step {{ min-width: 0; border: 1px solid var(--line); border-radius: .9rem; background: var(--white); overflow: hidden; }}
    details.step[open] {{ border-color: #a7cfc7; }}
    summary {{
      display: grid;
      grid-template-columns: 2.2rem 1fr auto;
      gap: .9rem;
      align-items: center;
      padding: 1rem 1.1rem;
      cursor: pointer;
      list-style: none;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    summary:hover {{ background: #f0eee7; }}
    summary strong, summary small {{ display: block; }}
    summary small {{ margin-top: .18rem; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .step-number {{ color: var(--coral); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82rem; }}
    .open-label {{ color: var(--teal-dark); font-size: .78rem; font-weight: 800; }}
    details[open] .open-label {{ font-size: 0; }}
    details[open] .open-label::after {{ content: "Hide script"; font-size: .78rem; }}
    .produces {{ margin: 0; padding: .9rem 1.1rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .84rem; }}
    pre {{
      max-width: 100%;
      margin: 0;
      overflow-x: auto;
      padding: 1.2rem;
      border-top: 1px solid #294747;
      background: var(--night);
      color: #d9ece7;
      font: .78rem/1.65 ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .reproduce-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
    .reproduce-card {{ padding: 1.5rem; border: 1px solid var(--line); border-radius: 1rem; background: var(--white); }}
    .reproduce-card h3 {{ margin-bottom: .75rem; }}
    .reproduce-card ul {{ margin: 0; padding-left: 1.2rem; color: var(--muted); }}
    .reproduce-card li + li {{ margin-top: .45rem; }}
    .reproduce-card pre {{ border: 0; border-radius: .75rem; }}
    footer {{ padding: 2rem 0 3rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .83rem; }}
    .footer-row {{ display: flex; justify-content: space-between; gap: 1rem; }}
    @media (max-width: 800px) {{
      .section-head, .figures, .reproduce-grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: 1fr 1fr; }}
      .stat:nth-child(3) {{ border-left: 0; }}
      .stat:nth-child(n+3) {{ border-top: 1px solid var(--line); }}
    }}
    @media (max-width: 560px) {{
      .nav-links a:not(:last-child) {{ display: none; }}
      header {{ padding-top: 3.2rem; }}
      h1 {{ font-size: clamp(2.8rem, 15vw, 4.2rem); }}
      .finding {{ grid-template-columns: 1fr; gap: 1rem; }}
      .stats {{ grid-template-columns: 1fr; }}
      .stat + .stat {{ border-left: 0; border-top: 1px solid var(--line); }}
      section {{ padding: 4rem 0; }}
      summary {{ grid-template-columns: 2rem 1fr auto; }}
      .open-label {{ font-size: 0; }}
      .open-label::after {{ content: "›"; font-size: 1.25rem; }}
      details[open] .open-label::after {{ content: "⌄"; font-size: 1.25rem; }}
      .footer-row {{ flex-direction: column; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}
  </style>
</head>
<body>
  <header class="wrap">
    <nav aria-label="Analysis navigation">
      <a class="brand" href="../"><span class="brand-mark">H·</span>Whatran</a>
      <div class="nav-links">
        <a href="#results">Results</a>
        <a href="#method">Method</a>
        <a href="#reproduce">Reproduce</a>
        <a href="https://github.com/qchiujunhao/whatran/tree/main/examples/cancer-classification">Source ↗</a>
      </div>
    </nav>
    <p class="eyebrow">Generated example · fresh-clone checked</p>
    <h1>{title}</h1>
    <p class="question">{question}</p>
    <div class="finding">
      <div>
        <strong>{conclusion}</strong>
        <p>{html.escape(selected_label)} ranked first by {html.escape(comparison["comparison_basis"])}.</p>
      </div>
      <span class="pass">Declared outputs reproduced</span>
    </div>
    <div class="stats" aria-label="Headline statistics">
      <div class="stat"><strong>{score(comparison["paths"]["logistic_regression"]["roc_auc"])}</strong><span>Logistic test ROC AUC</span></div>
      <div class="stat"><strong>{score(comparison["paths"]["random_forest"]["roc_auc"])}</strong><span>Forest test ROC AUC</span></div>
      <div class="stat"><strong>{row_count}</strong><span>Rows in the declared input</span></div>
      <div class="stat"><strong>{len(manifest["outputs"])}</strong><span>Declared outputs checked</span></div>
    </div>
  </header>

  <main>
    <section id="results">
      <div class="wrap">
        <div class="section-head">
          <h2>Two paths, one conclusion.</h2>
          <p>{threshold_statement} the prespecified {comparison["strong_auc_threshold"]:.2f} cross-validation ROC AUC threshold. The selected path is highlighted, while both remain part of the final report.</p>
        </div>
        <div class="table-wrap">
          <table>
            <caption>Performance on the shared seeded split; selection used mean training-set cross-validation ROC AUC.</caption>
            <thead><tr><th scope="col">Metric</th>{headers}</tr></thead>
            <tbody>{rows}{cv_row}</tbody>
          </table>
        </div>
        <p class="comparison-note">Cross-validation ROC AUC range: {comparison["cv_roc_auc_range"]:.3f}; test ROC AUC range: {comparison["test_roc_auc_range"]:.3f}.</p>

        <div class="figures">
          <figure>
            <h3>Held-out ROC curves</h3>
            <div class="path-key"><span class="key">Logistic regression</span><span class="key forest">Random forest</span></div>
            <img src="roc_comparison.svg" alt="ROC curves for logistic regression and random forest on the held-out test set">
            <figcaption>Both curves approach the upper-left corner. The AUC difference is small and does not change the shared conclusion.</figcaption>
          </figure>
          <figure>
            <h3>What each path used most</h3>
            <img src="model_drivers.svg" alt="Top logistic-regression coefficient magnitudes and random-forest feature importances">
            <figcaption>These panels explain each model on its own scale. Standardized coefficient magnitude and impurity importance are not directly comparable units.</figcaption>
          </figure>
        </div>
      </div>
    </section>

    <section id="method">
      <div class="wrap">
        <div class="section-head">
          <h2>The complete final method.</h2>
          <p>Every result-affecting script is present in execution order. Source is collapsed by default so the finding stays readable, but nothing needed for the rerun is hidden.</p>
        </div>
        <div class="steps">{''.join(step_cards)}</div>
      </div>
    </section>

    <section id="reproduce">
      <div class="wrap">
        <div class="section-head">
          <h2>Reproduce the evidence.</h2>
          <p>The repository carries the raw input, pinned Python environment, reference results, comparison policy, and one canonical rerun command.</p>
        </div>
        <div class="reproduce-grid">
          <article class="reproduce-card">
            <h3>Run locally</h3>
            <pre><code>git clone https://github.com/qchiujunhao/whatran
cd whatran/examples/cancer-classification
bash run.sh</code></pre>
          </article>
          <article class="reproduce-card">
            <h3>What the check establishes</h3>
            <ul>
              <li>A fresh clone regenerated the declared outputs from the declared input.</li>
              <li>Declared outputs matched under their configured comparison rules, and declared figures were recreated.</li>
              <li><a href="https://github.com/qchiujunhao/whatran/actions/workflows/test.yml">Open the GitHub Actions evidence ↗</a></li>
              <li>It does not establish clinical validity or scientific correctness.</li>
            </ul>
          </article>
        </div>
      </div>
    </section>
  </main>

  <footer>
    <div class="wrap footer-row">
      <span>Generated from the rerun outputs by the analysis package.</span>
      <span><a href="../">About Whatran</a> · <a href="https://github.com/qchiujunhao/whatran">GitHub</a></span>
    </div>
  </footer>
</body>
</html>
"""
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    print("wrote site/index.html and figure assets")


if __name__ == "__main__":
    main()
