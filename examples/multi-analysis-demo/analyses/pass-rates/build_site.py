#!/usr/bin/env python3
"""Showcase builder adapted for the pass-rate analysis (method/pass_rate + SVG)."""
from __future__ import annotations

import html
import json
from pathlib import Path

OUT_DIR = Path("site")


def main() -> None:
    manifest = json.loads(Path("whatran.json").read_text(encoding="utf-8"))
    method = manifest.get("method", {})
    results = Path(manifest.get("results_dir", "results"))
    data = json.loads((results / "passrate.json").read_text(encoding="utf-8"))
    chart = (results / "chart.svg").read_text(encoding="utf-8")

    rows = "".join(
        f"<tr><td>{html.escape(str(m['method']))}</td>"
        f"<td>{m['n']}</td><td>{m['pass_rate']}</td></tr>"
        for m in data["methods"]
    )
    steps_html = []
    for step in manifest["steps"]:
        source = Path(step["script"]).read_text(encoding="utf-8")
        steps_html.append(
            f"<section class='step'><h3>{html.escape(step['title'])}</h3>"
            f"<p class='path'>{html.escape(step['script'])}</p>"
            f"<pre><code>{html.escape(source)}</code></pre></section>"
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
<p><strong>Pass threshold:</strong> score &ge; {data.get("threshold")}</p>
<h2>Result</h2>
<div>{chart}</div>
<table><tr><th>method</th><th>n</th><th>pass_rate</th></tr>{rows}</table>
<h2>Pipeline</h2>
{''.join(steps_html)}
</body></html>
"""
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    print("wrote site/index.html")


if __name__ == "__main__":
    main()
