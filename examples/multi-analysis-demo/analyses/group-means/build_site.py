#!/usr/bin/env python3
"""Build the static showcase site from the verified run.

The site is generated from the regenerated outputs, so it cannot drift from what
actually reproduces. It shows the question, the ordered pipeline steps with their
source, and the result visualization.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

OUT_DIR = Path("site")


def main() -> None:
    manifest = json.loads(Path("whatran.json").read_text(encoding="utf-8"))
    method = manifest.get("method", {})
    results_dir = Path(manifest.get("results_dir", "results"))
    summary = json.loads((results_dir / "summary.json").read_text(encoding="utf-8"))
    chart = (results_dir / "chart.svg").read_text(encoding="utf-8")

    steps_html = []
    for step in manifest["steps"]:
        source = Path(step["script"]).read_text(encoding="utf-8")
        steps_html.append(
            f"<section class='step'><h3>{html.escape(step['title'])}</h3>"
            f"<p class='path'>{html.escape(step['script'])}</p>"
            f"<pre><code>{html.escape(source)}</code></pre></section>"
        )

    rows = "".join(
        f"<tr><td>{html.escape(str(g['group']))}</td>"
        f"<td>{g['n']}</td><td>{g['mean']}</td></tr>"
        for g in summary["groups"]
    )

    title = html.escape(method.get("title", "Analysis"))
    question = html.escape(method.get("question", ""))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;line-height:1.5}}
pre{{background:#f6f8fa;padding:1rem;overflow-x:auto;border-radius:6px}}
.path{{color:#666;font-family:monospace;margin:.2rem 0}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:.3rem .6rem}}
.step{{margin:1.5rem 0}}
</style></head><body>
<h1>{title}</h1>
<p><strong>Question:</strong> {question}</p>
<h2>Result</h2>
<div>{chart}</div>
<table><tr><th>group</th><th>n</th><th>mean</th></tr>{rows}</table>
<h2>Pipeline</h2>
{''.join(steps_html)}
<hr>
<p><small>Generated from the verified run. Reproduction is checked by CI on a
fresh clone; a passing badge means the declared outputs regenerated from the raw
inputs, not that the method is scientifically correct.</small></p>
</body></html>
"""
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    print("wrote site/index.html")


if __name__ == "__main__":
    main()
