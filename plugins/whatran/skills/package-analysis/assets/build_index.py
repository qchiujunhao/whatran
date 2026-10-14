#!/usr/bin/env python3
"""Build the landing page for a multi-analysis repo. Copy verbatim; it is generic.

Scans analyses/*/whatran.json, runs each analysis's own build_site.py, collects the
per-analysis showcases under site/<slug>/, and writes site/index.html linking
them. Run from the repo root.
"""
from __future__ import annotations

import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

ANALYSES = Path("analyses")
SITE = Path("site")


def main() -> None:
    SITE.mkdir(exist_ok=True)
    cards = []
    for manifest_path in sorted(ANALYSES.glob("*/whatran.json")):
        analysis = manifest_path.parent
        slug = analysis.name
        method = json.loads(manifest_path.read_text(encoding="utf-8")).get("method", {})

        subprocess.run([sys.executable, "build_site.py"], cwd=analysis, check=True)
        destination = SITE / slug
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(analysis / "site", destination)
        cards.append((slug, method.get("title", slug), method.get("question", "")))

    items = "".join(
        f'<a class="card" href="{html.escape(slug)}/index.html">'
        f'<span class="status">Reproduced</span><h2>{html.escape(title)}</h2>'
        f"<p>{html.escape(question)}</p><strong>Open analysis →</strong></a>"
        for slug, title, question in cards
    )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reproducible analyses</title>
<style>
:root{{--paper:#f6f3ec;--white:#fffef9;--ink:#132827;--muted:#5a6966;--line:#d7d4ca;--teal:#087f70}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,sans-serif;line-height:1.55}}
header,main,footer{{width:min(1040px,calc(100% - 2rem));margin:auto}}header{{padding:5rem 0 3rem;border-bottom:1px solid var(--line)}}
.eyebrow{{color:var(--teal);font-size:.78rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}}h1{{max-width:760px;margin:.6rem 0 1rem;font-size:clamp(3rem,8vw,6rem);line-height:.95;letter-spacing:-.06em}}
header p,footer{{color:var(--muted)}}main{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem;padding:3rem 0 5rem}}
.card{{display:flex;min-height:280px;flex-direction:column;padding:1.7rem;border:1px solid var(--line);border-radius:1rem;background:var(--white);color:inherit;text-decoration:none}}.card:hover{{border-color:var(--teal);transform:translateY(-2px)}}
.card h2{{margin:auto 0 .65rem;font-size:1.65rem;line-height:1.15}}.card p{{color:var(--muted)}}.card strong{{color:var(--teal)}}.status{{color:var(--teal);font-size:.75rem;font-weight:800;text-transform:uppercase}}.status::before{{content:"●";margin-right:.4rem}}
footer{{padding:2rem 0 3rem;border-top:1px solid var(--line);font-size:.83rem}}@media(max-width:650px){{main{{grid-template-columns:1fr}}}}
</style></head><body>
<header><p class="eyebrow">Generated from checked runs</p><h1>Reproducible analyses</h1>
<p>Each question has its own inputs, final scripts, outputs, and result page.</p></header>
<main>{items}</main>
<footer>Fresh-clone CI regenerates each package before publication. A pass is rerun evidence, not scientific review.</footer>
</body></html>
"""
    (SITE / "index.html").write_text(page, encoding="utf-8")
    print(f"built site/ landing with {len(cards)} analyses")


if __name__ == "__main__":
    main()
