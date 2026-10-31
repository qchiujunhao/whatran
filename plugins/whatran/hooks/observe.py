#!/usr/bin/env python3
"""Claude Code hook: turn every Bash tool call into execution evidence.

Reads the hook payload on stdin. Acts only when the command ran inside a project
that has ``analysis/config.json`` (created by ``whatran.py init``); otherwise exits
silently, so installing the plugin changes nothing for ordinary projects.

* PreToolUse          - remembers when the command started
* PostToolUse         - appends an ``exec`` event: command, exit status, output
                        head, and the files under the watched directories that
                        changed while the command ran (with SHA-256)
* PostToolUseFailure  - same, with the non-zero exit code parsed from the error
* Stop                - end of a turn: executions become steps, the graph is compiled,
                        the package is refreshed, the agent's last message is saved as
                        ``analysis/report.md``
* SessionStart        - tells the agent the project is being recorded

The hook never blocks a command and never raises: any internal failure exits 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bin"))

try:
    import whatran as ag  # noqa: E402
except Exception:  # pragma: no cover - plugin layout broken
    sys.exit(0)


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def state_file(p: "ag.Project", key: str) -> Path:
    p.state_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
    return p.state_dir / f"{safe}.json"


def emit_context(text: str, event: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


COMPLETION_RULE = (
    "Whatran: for tasks that report scientific or data-analysis findings, completion includes "
    "`python3 analysis/whatran.py check` (rerun from clean and compare; see /whatran:package-analysis). "
    "Recording is automatic. Other tasks are unaffected."
)


def handle_session_start(payload: dict) -> None:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or "<plugin root>"
    root = ag.find_root(payload.get("cwd"))
    if root is None:
        emit_context(
            COMPLETION_RULE + f" To record an analysis in this project: `python3 {plugin_root}/bin/whatran.py init` "
            "once, then work as usual.",
            "SessionStart",
        )
        return
    p = ag.Project(root)
    if not p.config.get("observe", {}).get("enabled", True):
        emit_context(COMPLETION_RULE, "SessionStart")
        return
    n_exec = sum(1 for e in p.events() if e.get("event") == "exec")
    emit_context(
        COMPLETION_RULE + f" This project records itself (analysis/, {n_exec} executions captured): every command "
        "is captured, and when a turn ends the steps, analysis/graph.json and the package (whatran.json, run.sh, "
        "check.py, CI workflow) are refreshed. Optional, one line each: `python3 analysis/whatran.py claim \"<finding>\"` "
        "for a conclusion, `reject <run id> --reason \"...\"` for an abandoned branch, `record \"<Verb> <object>\"` "
        "to name a step yourself. `status` shows the record.",
        "SessionStart",
    )


def handle_stop(payload: dict) -> None:
    """End of a turn. Does nothing until something ran in the project."""
    root = ag.find_root(payload.get("cwd"))
    if root is None:
        return
    p = ag.Project(root)
    obs = p.config.get("observe", {})
    if not obs.get("enabled", True):
        return
    events = p.events()
    if not any(e.get("event") == "exec" for e in events):
        return
    sf = state_file(p, "finish")
    last = {}
    if sf.is_file():
        try:
            last = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            last = {}
    size = p.events_path.stat().st_size if p.events_path.is_file() else 0
    grew = size != last.get("events_size")
    report = None
    if obs.get("report", True) and payload.get("transcript_path"):
        text = ag.last_assistant_text(payload["transcript_path"])
        if text and (grew or ag.mentions_record(text, ag.fold_nodes(events))):
            report = text
    if not grew and report is None:
        return
    had_package = (p.root / ag.PACKAGE_MANIFEST).is_file()
    res = ag.finish(p, report=report) if grew else {"report": ag.write_report(p, report), "package": {"written": []}}
    size = p.events_path.stat().st_size if p.events_path.is_file() else 0
    sf.write_text(json.dumps({"events_size": size, "ts": time.time()}), encoding="utf-8")
    written = res.get("package", {}).get("written") or []
    if not had_package and "whatran.json" in written and "run.sh" in written:     # first time only
        print(json.dumps({"systemMessage": "whatran: package written (whatran.json, run.sh, check.py, CI workflow). "
                          "`python3 analysis/whatran.py check` reruns it from clean."}))


def exit_code_from_failure(payload: dict) -> int | None:
    err = payload.get("error")
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err)
    if not isinstance(err, str):
        return None
    m = re.search(r"[Ee]xit code[:\s]+(\d+)", err)
    return int(m.group(1)) if m else 1


def handle_tool(payload: dict) -> None:
    if payload.get("tool_name") != "Bash":
        return
    root = ag.find_root(payload.get("cwd"))
    if root is None:
        return
    p = ag.Project(root)
    obs = p.config.get("observe", {})
    if not obs.get("enabled", True):
        return

    event = payload.get("hook_event_name")
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    key = payload.get("tool_use_id") or f"session-{payload.get('session_id', 'unknown')}"
    sf = state_file(p, key)

    if event == "PreToolUse":
        if ag.is_ignored_command(command, p.config):
            return
        before = p.snapshot()                     # exact diff after the command, no mtime window
        state = {"start": time.time(), "command": command}
        if len(before) <= ag.SNAPSHOT_MAX_FILES:
            state["snapshot"] = before
        sf.write_text(json.dumps(state), encoding="utf-8")
        return

    if event not in ("PostToolUse", "PostToolUseFailure"):
        return

    start = None
    before = None
    if sf.is_file():
        try:
            state = json.loads(sf.read_text(encoding="utf-8"))
            start = float(state.get("start"))
            if isinstance(state.get("snapshot"), dict):
                before = {k: tuple(v) for k, v in state["snapshot"].items()}
        except Exception:
            start, before = None, None
        try:
            sf.unlink()
        except OSError:
            pass
    if ag.is_ignored_command(command, p.config):
        return

    now = time.time()
    duration_ms = payload.get("duration_ms")
    slack = ag.MTIME_SLACK_PRECISE
    if start is None and isinstance(duration_ms, (int, float)):
        start = now - float(duration_ms) / 1000.0
        slack = ag.MTIME_SLACK_ESTIMATED

    resp = payload.get("tool_response")
    stdout = stderr = ""
    interrupted = False
    extra_changed: list[str] = []
    if isinstance(resp, dict):
        stdout = str(resp.get("stdout") or "")
        stderr = str(resp.get("stderr") or "")
        interrupted = bool(resp.get("interrupted"))
        diff = resp.get("bashEditDiff") or {}
        if isinstance(diff, dict):
            for f in diff.get("changedFiles") or diff.get("files") or []:
                path = f.get("path") if isinstance(f, dict) else f
                if isinstance(path, str):
                    extra_changed.append(path)
    elif isinstance(resp, str):
        stdout = resp

    if event == "PostToolUse":
        exit_code: int | None = 0
        m = re.search(r"^Exit code (\d+)", stderr, re.M) or re.search(r"^Exit code (\d+)", stdout, re.M)
        if m:
            exit_code = int(m.group(1))
    else:
        exit_code = exit_code_from_failure(payload)
        err = payload.get("error")
        if isinstance(err, str) and not stderr:
            stderr = err

    files = None
    truncated = False
    if before is not None:
        files, truncated = p.changed_since_snapshot(before)
    elif start is not None:
        files, truncated = p.changed_files(start - slack)
    if extra_changed:
        files = files or []
        known = {f["path"] for f in files}
        max_bytes = int(p.config.get("hash_max_bytes", 1 << 30))
        for raw in extra_changed:
            fp = Path(raw)
            if not fp.is_absolute():
                fp = Path(payload.get("cwd") or root) / fp
            rel = p.rel(fp)
            if rel in known or not fp.is_file():
                continue
            st = fp.stat()
            files.append({"path": rel, "size": st.st_size, "mtime": ag.iso_from_ts(st.st_mtime),
                          "sha256": ag.sha256_file(fp, max_bytes)})
            known.add(rel)
        files.sort(key=lambda f: f["path"])

    intent = None
    if obs.get("intent", True) and payload.get("transcript_path") and payload.get("tool_use_id"):
        intent = ag.intent_before_tool_use(payload["transcript_path"], payload["tool_use_id"],
                                           int(obs.get("intent_chars", 300)))
    events = p.events()
    ev = p.make_exec_event(
        command, source="claude-code-hook", exit_code=exit_code, started=start, ended=now,
        stdout=stdout, stderr=stderr, cwd=payload.get("cwd"), files_changed=files, truncated=truncated,
        session=payload.get("session_id"), tool_use_id=payload.get("tool_use_id"),
        interrupted=interrupted, events=events,
        description=tool_input.get("description"), intent=intent,
    )
    p.append(ev)

    if obs.get("nudge", False) and files:
        shown = ", ".join(f["path"] for f in files[:4]) + (" ..." if len(files) > 4 else "")
        emit_context(f"whatran: captured {ev['id']} (exit {exit_code}); changed: {shown}.", event)


def main() -> int:
    try:
        payload = read_payload()
        if "--session-start" in sys.argv or payload.get("hook_event_name") == "SessionStart":
            handle_session_start(payload)
        elif "--stop" in sys.argv or payload.get("hook_event_name") == "Stop":
            handle_stop(payload)
        else:
            handle_tool(payload)
    except Exception as exc:  # never break the agent's tool call
        try:
            sys.stderr.write(f"whatran hook: {exc}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
