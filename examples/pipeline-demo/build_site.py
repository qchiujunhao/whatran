#!/usr/bin/env python3
"""Build the result-first showcase from the regenerated pipeline."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

OUT_DIR = Path("site")


def main() -> None:
    manifest = json.loads(Path("whatran.json").read_text(encoding="utf-8"))
    method = manifest.get("method", {})
    results = Path(manifest.get("results_dir", "results"))
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    best = max(summary["groups"], key=lambda group: float(group["mean"]))

    steps = []
    for index, step in enumerate(manifest["steps"], start=1):
        source = Path(step["script"]).read_text(encoding="utf-8")
        steps.append(
            "<details class='step'><summary>"
            f"<span>{index:02}</span><strong>{html.escape(step['title'])}</strong>"
            f"<small>{html.escape(step['script'])}</small></summary>"
            f"<pre><code>{html.escape(source)}</code></pre></details>"
        )

    rows = "".join(
        f"<tr><td>{html.escape(str(group['group']))}</td>"
        f"<td>{group['n']}</td><td>{group['mean']}</td></tr>"
        for group in summary["groups"]
    )
    title = html.escape(method.get("title", "Analysis"))
    question = html.escape(method.get("question", ""))

    OUT_DIR.mkdir(exist_ok=True)
    shutil.copy2(results / "chart.svg", OUT_DIR / "chart.svg")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--paper:#f6f3ec;--white:#fffef9;--ink:#132827;--muted:#5a6966;--line:#d7d4ca;--teal:#087f70;--coral:#bd432f;--night:#102a2b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,sans-serif;line-height:1.55}}
header,main,footer{{width:min(960px,calc(100% - 2rem));margin:auto}}header{{padding:4rem 0 2rem}}h1{{font-size:clamp(2.5rem,7vw,5rem);line-height:1;letter-spacing:-.05em;margin:.5rem 0 1rem}}
.eyebrow{{color:var(--teal);font-size:.78rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}}.question,.note{{color:var(--muted)}}
.finding{{margin:2rem 0;padding:1.25rem;border-left:5px solid var(--teal);background:#e5f1ed;font-size:1.15rem}}
section{{padding:3.5rem 0;border-top:1px solid var(--line)}}h2{{font-size:clamp(1.8rem,4vw,3rem);letter-spacing:-.04em}}
.result-grid{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}.result-grid>*,.steps,details{{min-width:0}}figure,.table-wrap{{margin:0;padding:1rem;border:1px solid var(--line);border-radius:1rem;background:var(--white);overflow:auto}}
figure img{{display:block;width:100%;height:auto}}figcaption{{color:var(--muted);font-size:.84rem}}table{{width:100%;border-collapse:collapse}}th,td{{padding:.7rem;border-bottom:1px solid var(--line);text-align:left}}
.steps{{display:grid;gap:.65rem}}details{{border:1px solid var(--line);border-radius:.8rem;background:var(--white);overflow:hidden}}summary{{display:grid;grid-template-columns:2rem 1fr auto;gap:.7rem;padding:1rem;cursor:pointer}}summary span{{color:var(--coral);font-family:monospace}}summary small{{color:var(--muted);font-family:monospace}}
pre{{max-width:100%;margin:0;padding:1rem;overflow:auto;background:var(--night);color:#d9ece7;font:.8rem/1.6 monospace}}footer{{padding:2rem 0 3rem;border-top:1px solid var(--line);color:var(--muted);font-size:.82rem}}
@media(max-width:700px){{.result-grid{{grid-template-columns:1fr}}summary{{grid-template-columns:2rem 1fr}}summary small{{grid-column:2}}}}
</style></head><body>
<header>
<p class="eyebrow">Reproducible analysis · generated from rerun outputs</p>
<h1>{title}</h1>
<p class="question">{question}</p>
<p class="finding"><strong>Result:</strong> {html.escape(str(best['group']))} has the highest mean at {best['mean']}.</p>
<p class="note">Fresh-clone CI regenerates the declared outputs and compares them with committed references. This is rerun evidence, not scientific review.</p>
</header>
<main>
<section>
<h2>Result evidence</h2>
<div class="result-grid">
<figure><img src="chart.svg" alt="Result chart"><figcaption>Generated from the checked summary values.</figcaption></figure>
<div class="table-wrap"><table><thead><tr><th>Group</th><th>n</th><th>Mean</th></tr></thead><tbody>{rows}</tbody></table></div>
</div>
</section>
<section>
<h2>Final method</h2>
<p class="note">Open any step to inspect the complete result-affecting script.</p>
<div class="steps">{''.join(steps)}</div>
</section>
</main>
<footer>Generated from the rerun outputs; a passing check does not establish scientific correctness.</footer>
</body></html>
"""
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    print("wrote site/index.html and chart.svg")


if __name__ == "__main__":
    main()
