# Using the same record from other agents

The recording protocol is three things: the policy in `SKILL.md`, the format in
`graph-format.md`, and the viewer. Nothing in it needs Claude Code. What differs
per agent is only how execution evidence gets captured.

| agent | how it records | evidence level |
| --- | --- | --- |
| Claude Code (this plugin) | hooks capture every Bash command; `whatran.py record "..."` names the steps | observed |
| Codex | this plugin's skill (`.codex-plugin`); run steps through `python3 analysis/whatran.py run -- CMD`, or add a PostToolUse hook that pipes the hook payload to `hooks/observe.py` | observed |
| Gemini CLI, other local agents | drop `SKILL.md` into `GEMINI.md` / `AGENTS.md`; use `whatran.py run -- CMD` | observed |
| ChatGPT / web agents without a shell | project instructions below; emit `analysis/graph.json` directly | declared |
| a person | the same commands | observed |

## Codex

Install the plugin (it carries `.codex-plugin/plugin.json`) or copy
`skills/record-analysis/` into the skills directory. Codex sessions do not run the
Claude Code hooks, so execute each scientific step as

```bash
python3 analysis/whatran.py run --label "<step>" --in ... --out ... --code ... --param k=v -- <command>
```

`whatran.py run` captures exit status, output and changed files itself, so the run is
`observed`. Steps run outside the wrapper can still be declared with
`whatran.py record "..." --evidence none`.

Codex hooks receive the same JSON shape as Claude Code hooks (`tool_name`,
`tool_input.command`, `cwd`, `tool_response`). If your Codex version supports
PreToolUse/PostToolUse command hooks, point them at `hooks/observe.py`; it is
agent-agnostic and only needs those fields.

## Gemini CLI and AGENTS.md agents

Append the **Recording policy** and **Commands** sections of `SKILL.md` to the
project's `GEMINI.md` or `AGENTS.md`, and use `whatran.py run` as above.

## ChatGPT and agents without file access

Add to the project instructions:

> While analysing, keep an Analysis Graph in the `analysis-graph/0.1` format:
> artifacts (files with sha256 when you can compute it), runs (one per
> scientific step with command, parameters, inputs, outputs, status) and claims
> (conclusions with `supported_by`). Mark every run's `evidence.level` as
> `declared`. Keep rejected branches with `status: rejected`. Deliver
> `analysis/graph.json` with the results.

The user commits that file next to the viewer (`whatran.py publish` from any machine,
or copy `viewer/index.html`) and gets the same interactive graph. The footer will
honestly say "0 of N runs have observed execution evidence".

## Hand-over between agents

Because the ledger is append-only and the ids are stable, one agent can finish
part of an analysis and another can continue: open the graph, pick a node, use
**Copy “branch from here” prompt**, and paste it into the next agent. The new
agent records a new run with `--supersedes <id>`; the old branch remains.
