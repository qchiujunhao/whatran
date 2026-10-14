# Multi-analysis demo — several analyses, one repo, one website

A session that answered more than one question becomes a repo of **independent,
self-contained analysis packages** plus one landing page. Each analysis under
`analyses/<slug>/` reproduces on its own; one clean CI job discovers and checks
every manifest before building the combined website.

```text
multi-analysis-demo/
├── analyses/
│   ├── group-means/     # "What is the mean value in each group?"
│   └── pass-rates/      # "What fraction pass (score >= 60) per method?"
├── build_index.py       # builds site/: runs each analysis's build_site.py + a landing page
└── reproduce.yml        # -> .github/workflows/reproduce.yml
```

Each `analyses/<slug>/` is a complete package (`whatran.json`, `steps/`, `run.sh`,
`results/`, `check.py`, `build_site.py`, and `README.md`) — exactly the
single-analysis shape, composed.

## Reproduce every analysis the CI way

```bash
reference_root="$(mktemp -d)"
for a in analyses/*/; do
  slug="$(basename "$a")"
  ( cd "$a" && mv results "$reference_root/$slug-results" && bash run.sh \
    && python3 check.py results "$reference_root/$slug-results" )
done
```

## Build the combined website

```bash
python3 build_index.py     # -> site/index.html (landing) + site/<slug>/index.html
```

## When to use this vs one package

- **Separate questions** → separate packages here (one per `analyses/<slug>/`).
- **Alternative paths for the same question** → a single package with a comparison
  step, not multiple packages.

Because checking and site discovery use the same manifests, an unverified
package cannot silently appear on the site.
