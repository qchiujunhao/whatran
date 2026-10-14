#!/usr/bin/env python3
"""whatran.py - Analysis Graph recorder (format analysis-graph/0.1).

Single file, no dependencies, Python 3.9+.

An agent (or a person) keeps analysing however it likes. This tool keeps an
append-only ledger of what actually ran (``analysis/events.jsonl``) and compiles
it into a result-centric, portable ``analysis/graph.json`` that the bundled
viewer renders as an interactive graph.

Two kinds of writers append to the ledger:

* execution evidence - ``exec`` events, written automatically by agent hooks or
  by the ``whatran.py run`` wrapper, never by hand;
* scientific meaning - ``node`` events (artifact / run / claim), declared by the
  agent with ``whatran.py record``.

The graph is always a *view* derived from the ledger. Nothing is edited in place,
so rejected branches and failed attempts stay in the record even when the viewer
hides them by default.

Everyday commands::

    whatran.py init                 once per project: create analysis/ and start recording
    whatran.py record "what it did" after a meaningful step; inputs, outputs, code and
                                    parameters are read from the captured command
    whatran.py claim "conclusion"   a finding and what it rests on (default: the last run)
    whatran.py reject ID --reason   abandon a branch; it stays in the record
    whatran.py done                 compile the graph, check files, draft the package
    whatran.py open                 view the graph in the browser
    whatran.py package              generate whatran.json, run.sh, check.py, build_site.py, CI
    whatran.py check                rerun from clean, compare, mark the runs replay-verified
    whatran.py run -- CMD           run a command and capture evidence yourself (no hooks)

More (``whatran.py COMMAND -h``): artifact, update, log, status, compile, verify,
export, replay-verified, publish, serve.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.1.0"
FORMAT = "analysis-graph/0.1"

AG_DIRNAME = "analysis"
CONFIG_NAME = "config.json"
EVENTS_NAME = "events.jsonl"
GRAPH_NAME = "graph.json"
GRAPH_MD_NAME = "graph.md"
VIEWER_NAME = "index.html"
STATE_DIRNAME = ".state"

# how far before a command's start time a file's mtime may lie and still count
# as changed by it: small when the start was measured, larger when estimated
MTIME_SLACK_PRECISE = 0.2
MTIME_SLACK_ESTIMATED = 1.5

NODE_TYPES = ("artifact", "run", "claim")
RUN_STATUSES = ("completed", "failed", "rejected", "superseded")
CLAIM_STATUSES = ("tentative", "supported", "rejected")
ACTIVE_RUN_STATUSES = ("completed",)

DEFAULT_CONFIG: Dict[str, Any] = {
    "format": FORMAT,
    "project": {"name": "", "description": ""},
    "watch": ["."],
    "exclude": [
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
        "__pycache__", ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".tox", ".DS_Store", "site", ".cache",
    ],
    "hash_max_bytes": 1 << 30,
    "observe": {
        "enabled": True,
        "nudge": True,
        "ignore_commands": [
            "ls", "ll", "cat", "head", "tail", "pwd", "echo", "printf", "which",
            "type", "wc", "grep", "rg", "egrep", "fgrep", "find", "fd", "tree",
            "stat", "file", "less", "more", "cd", "env", "printenv", "man",
            "help", "true", "false", "test", "date", "whoami", "uname", "du",
            "df", "sleep", "diff", "sort", "uniq", "cut", "awk", "sed", "jq",
            "column", "nl", "tac", "basename", "dirname", "realpath", "md5sum",
            "shasum", "sha256sum",
        ],
        "ignore_patterns": [
            r"(^|[\s/])whatran\.py\b",
            r"^\s*git\s+(status|log|diff|show|branch|remote|rev-parse|ls-files|blame)\b",
        ],
        "stdout_head_bytes": 2000,
        "max_files_changed": 200,
    },
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path, max_bytes: int) -> Optional[str]:
    """Hex digest, or None when the file is missing or larger than max_bytes."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-")) or "node"


def parse_param(raw: str) -> Tuple[str, Any]:
    if "=" not in raw:
        raise ValueError(f"parameter must look like key=value, got {raw!r}")
    key, val = raw.split("=", 1)
    key = key.strip()
    val = val.strip()
    if val.lower() in ("true", "false"):
        return key, val.lower() == "true"
    if val.lower() in ("null", "none"):
        return key, None
    try:
        return key, int(val)
    except ValueError:
        pass
    try:
        return key, float(val)
    except ValueError:
        pass
    if val[:1] in "[{":
        try:
            return key, json.loads(val)
        except json.JSONDecodeError:
            pass
    if "," in val:
        parts = [p.strip() for p in val.split(",")]
        try:
            return key, [int(p) for p in parts]
        except ValueError:
            try:
                return key, [float(p) for p in parts]
            except ValueError:
                return key, parts
    return key, val


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def git_info(root: Path) -> Optional[Dict[str, Any]]:
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        if head.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5)
        return {"commit": head.stdout.strip(), "dirty": bool(dirty.stdout.strip())}
    except Exception:
        return None


def env_info() -> Dict[str, Any]:
    return {"python": platform.python_version(), "platform": platform.platform()}


def detect_agent() -> str:
    if os.environ.get("AG_AGENT"):
        return os.environ["AG_AGENT"]
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_HOME"):
        return "codex"
    if os.environ.get("GEMINI_CLI"):
        return "gemini-cli"
    return "unknown"


def find_root(start: Optional[str] = None) -> Optional[Path]:
    """Walk up from start (default: cwd) to the directory holding analysis/config.json."""
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / AG_DIRNAME / CONFIG_NAME).is_file():
            return d
    return None


def is_ignored_command(command: str, config: Dict[str, Any]) -> bool:
    """True for commands that only read (ls, cat, grep ...) and for whatran.py itself."""
    c = command.strip()
    if not c:
        return True
    obs = config.get("observe", {})
    for pat in obs.get("ignore_patterns", []):
        try:
            if re.search(pat, c):
                return True
        except re.error:
            continue
    if ">" in c:
        return False
    ignored = set(obs.get("ignore_commands", []))
    segments = [s for s in re.split(r"\s*(?:&&|\|\||;|\|)\s*", c) if s.strip()]
    if not segments:
        return True
    for seg in segments:
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        toks = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if not toks:
            continue
        name = os.path.basename(toks[0])
        if name not in ignored:
            return False
    return True


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #

class AgError(Exception):
    pass


class Project:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.ag_dir = self.root / AG_DIRNAME
        self.config = self._load_config()

    # paths -----------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.ag_dir / CONFIG_NAME

    @property
    def events_path(self) -> Path:
        return self.ag_dir / EVENTS_NAME

    @property
    def graph_path(self) -> Path:
        return self.ag_dir / GRAPH_NAME

    @property
    def state_dir(self) -> Path:
        return self.ag_dir / STATE_DIRNAME

    def _load_config(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {}
        if self.config_path.is_file():
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AgError(f"{self.config_path} is not valid JSON: {exc}")
        return deep_merge(DEFAULT_CONFIG, cfg)

    def rel(self, path: Any) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        p = p.resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def abs(self, relpath: str) -> Path:
        p = Path(relpath)
        return p if p.is_absolute() else self.root / p

    # ledger -----------------------------------------------------------------
    def events(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.events_path.is_file():
            return out
        with open(self.events_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    sys.stderr.write(f"ag: skipping malformed line {lineno} in {self.events_path}\n")
        return out

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event.setdefault("ts", now_iso())
        self.ag_dir.mkdir(parents=True, exist_ok=True)
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def next_exec_id(self, events: Optional[List[Dict[str, Any]]] = None) -> str:
        events = self.events() if events is None else events
        n = sum(1 for e in events if e.get("event") == "exec")
        return f"x-{n + 1:04d}"

    def next_node_id(self, prefix: str, label: str, nodes: "OrderedDict[str, Dict[str, Any]]") -> str:
        base = f"{prefix}-{slugify(label)}"
        n = 1
        while f"{base}-{n}" in nodes:
            n += 1
        return f"{base}-{n}"

    # evidence ---------------------------------------------------------------
    def _walk(self):
        """Yield (relpath, abspath, stat) for every file under the watched dirs."""
        cfg = self.config
        excl = set(cfg.get("exclude", []))
        seen = set()
        for w in cfg.get("watch", ["."]):
            base = (self.root / w).resolve()
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dp = Path(dirpath)
                dirnames[:] = [d for d in dirnames if d not in excl and (dp / d).resolve() != self.ag_dir]
                for fn in filenames:
                    if fn in excl:
                        continue
                    fp = dp / fn
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    r = self.rel(fp)
                    if r in seen:
                        continue
                    seen.add(r)
                    yield r, fp, st

    def _file_record(self, r: str, fp: Path, st: os.stat_result) -> Dict[str, Any]:
        return {
            "path": r,
            "size": st.st_size,
            "mtime": iso_from_ts(st.st_mtime),
            "sha256": sha256_file(fp, int(self.config.get("hash_max_bytes", 1 << 30))),
        }

    def changed_files(self, since: float, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], bool]:
        """Files under the watched dirs whose mtime is >= since (used by hooks, which only know
        when the command started). Returns (files, truncated)."""
        limit = limit or int(self.config["observe"].get("max_files_changed", 200))
        out: List[Dict[str, Any]] = []
        for r, fp, st in self._walk():
            if st.st_mtime < since:
                continue
            out.append(self._file_record(r, fp, st))
            if len(out) >= limit:
                out.sort(key=lambda f: f["path"])
                return out, True
        out.sort(key=lambda f: f["path"])
        return out, False

    def snapshot(self) -> Dict[str, Tuple[int, int]]:
        """relpath -> (mtime_ns, size) for every watched file. Cheap: no hashing."""
        return {r: (st.st_mtime_ns, st.st_size) for r, fp, st in self._walk()}

    def changed_since_snapshot(self, before: Dict[str, Tuple[int, int]],
                               limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], bool]:
        """Exact diff against a snapshot taken before the command (used by `whatran.py run`)."""
        limit = limit or int(self.config["observe"].get("max_files_changed", 200))
        out: List[Dict[str, Any]] = []
        for r, fp, st in self._walk():
            if before.get(r) == (st.st_mtime_ns, st.st_size):
                continue
            out.append(self._file_record(r, fp, st))
            if len(out) >= limit:
                out.sort(key=lambda f: f["path"])
                return out, True
        out.sort(key=lambda f: f["path"])
        return out, False

    def make_exec_event(self, command: str, *, source: str, exit_code: Optional[int],
                        started: Optional[float], ended: Optional[float],
                        stdout: str = "", stderr: str = "", cwd: Optional[str] = None,
                        files_changed: Optional[List[Dict[str, Any]]] = None,
                        truncated: bool = False, session: Optional[str] = None,
                        tool_use_id: Optional[str] = None, interrupted: bool = False,
                        events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        head = int(self.config["observe"].get("stdout_head_bytes", 2000))
        ev: Dict[str, Any] = {
            "event": "exec",
            "id": self.next_exec_id(events),
            "ts": now_iso(),
            "source": source,
            "command": command,
            "cwd": self.rel(cwd) if cwd else ".",
            "exit_code": exit_code,
            "started": iso_from_ts(started) if started else None,
            "ended": iso_from_ts(ended) if ended else None,
            "duration_ms": int((ended - started) * 1000) if started and ended else None,
            "stdout_head": (stdout or "")[:head],
            "stderr_head": (stderr or "")[:head],
            "files_changed": files_changed if files_changed is not None else None,
            "files_changed_truncated": truncated,
            "interrupted": interrupted,
        }
        if session:
            ev["session"] = session
        if tool_use_id:
            ev["tool_use_id"] = tool_use_id
        return ev

    def integrity(self, node: Dict[str, Any]) -> str:
        p = self.abs(node.get("path", ""))
        if not p.is_file():
            return "missing"
        if not node.get("sha256"):
            return "unchecked"
        digest = sha256_file(p, int(self.config.get("hash_max_bytes", 1 << 30)))
        if digest is None:
            return "unchecked"
        return "intact" if digest == node["sha256"] else "modified"


# --------------------------------------------------------------------------- #
# folding the ledger into nodes
# --------------------------------------------------------------------------- #

def fold_nodes(events: List[Dict[str, Any]]) -> "OrderedDict[str, Dict[str, Any]]":
    """Apply node / status / update events in order. Returns id -> node."""
    nodes: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for ev in events:
        kind = ev.get("event")
        if kind == "node":
            node = dict(ev.get("node") or {})
            if node.get("id"):
                nodes[node["id"]] = node
        elif kind == "status":
            n = nodes.get(ev.get("id"))
            if n is not None:
                n["status"] = ev.get("status")
                if ev.get("reason"):
                    n["status_reason"] = ev["reason"]
                n["status_changed_at"] = ev.get("ts")
        elif kind == "update":
            n = nodes.get(ev.get("id"))
            if n is not None:
                for k, v in (ev.get("fields") or {}).items():
                    if k in ("id", "type"):
                        continue
                    n[k] = v
    return nodes


def exec_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("event") == "exec"]


def linked_exec_ids(nodes: "OrderedDict[str, Dict[str, Any]]") -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in nodes.values():
        if n.get("type") == "run":
            for x in (n.get("evidence") or {}).get("exec_ids", []) or []:
                out[x] = n["id"]
    return out


def resolve_evidence(events: List[Dict[str, Any]], nodes: "OrderedDict[str, Dict[str, Any]]",
                     mode: str, outputs: List[str], code: List[str], command: Optional[str]) -> Optional[Dict[str, Any]]:
    """Pick the exec event that backs a run. mode: auto | none | last | x-NNNN."""
    execs = exec_events(events)
    if mode == "none" or not execs:
        return None
    if mode == "last":
        return execs[-1]
    if mode.startswith("x-"):
        for e in execs:
            if e.get("id") == mode:
                return e
        raise AgError(f"no execution evidence with id {mode}; see `whatran.py log`")
    if mode != "auto":
        raise AgError(f"unknown --evidence value {mode!r}")
    linked = linked_exec_ids(nodes)
    outs = set(outputs)
    # 0. nothing declared: the most recent unlinked execution that changed a file
    if not outputs and not code and not command:
        for e in reversed(execs):
            if e["id"] not in linked and (e.get("files_changed") or []):
                return e
        return None
    # 1. an unlinked execution that visibly produced one of the declared outputs
    for e in reversed(execs):
        if e["id"] in linked:
            continue
        changed = {f.get("path") for f in (e.get("files_changed") or [])}
        if changed & outs:
            return e
    # 2. an unlinked execution whose command matches the declared command / code
    code_names = [os.path.basename(c) for c in code if c]
    for e in reversed(execs):
        if e["id"] in linked:
            continue
        cmd = (e.get("command") or "")
        if command and cmd.strip() == command.strip():
            return e
        if any(name and name in cmd for name in code_names):
            return e
    return None


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #

def ensure_artifact(p: Project, nodes: "OrderedDict[str, Dict[str, Any]]", relpath: str, *,
                    label: Optional[str] = None, origin: Optional[str] = None,
                    note: Optional[str] = None, require_exists: bool = False) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return (artifact id, new node or None). Same path + same content reuses the node."""
    ap = p.abs(relpath)
    exists = ap.is_file()
    if require_exists and not exists:
        raise AgError(f"input does not exist: {relpath}")
    max_bytes = int(p.config.get("hash_max_bytes", 1 << 30))
    digest = sha256_file(ap, max_bytes) if exists else None
    size = ap.stat().st_size if exists else None
    existing = [n for n in nodes.values() if n.get("type") == "artifact" and n.get("path") == relpath]
    if existing:
        last = existing[-1]
        if not exists:
            return last["id"], None
        if digest and last.get("sha256") == digest:
            return last["id"], None
    base_label = label or os.path.basename(relpath) or relpath
    nid = p.next_node_id("art", base_label, nodes)
    node: Dict[str, Any] = {
        "id": nid,
        "type": "artifact",
        "label": base_label,
        "path": relpath,
        "sha256": digest,
        "size": size,
        "exists": exists,
        "recorded_at": now_iso(),
        "recorded_by": detect_agent(),
    }
    if existing:
        node["supersedes"] = existing[-1]["id"]
        node["version"] = int(existing[-1].get("version", 1)) + 1
    if origin:
        node["origin"] = origin
    if note:
        node["note"] = note
    nodes[nid] = node
    return nid, node


def record_artifact(p: Project, path: str, label: Optional[str], origin: Optional[str],
                    note: Optional[str]) -> Dict[str, Any]:
    events = p.events()
    nodes = fold_nodes(events)
    rel = p.rel(path)
    nid, node = ensure_artifact(p, nodes, rel, label=label, origin=origin, note=note, require_exists=True)
    if node is None:
        return {"id": nid, "created": False, "path": rel}
    p.append({"event": "node", "node": node})
    return {"id": nid, "created": True, "path": rel}


SHELL_OPERATORS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "2>", "2>>", "2>&1", "1>", "1>&2"}
SCRIPT_SUFFIXES = (".py", ".r", ".sh", ".bash", ".zsh", ".jl", ".ipynb", ".pl", ".rb", ".sql",
                   ".nf", ".smk", ".m", ".js", ".ts", ".scala", ".java", ".groovy", ".wdl", ".cwl")


def infer_from_exec(p: Project, ev: Dict[str, Any]) -> Dict[str, Any]:
    """Read outputs, inputs, code and parameters off a captured execution.

    outputs: every file the execution changed; code: script files named on the
    command line; inputs: other existing files named on the command line;
    params: ``--key value``, ``--key=value`` and ``-k value`` pairs.
    """
    cmd = ev.get("command") or ""
    cwd_abs = p.abs(ev.get("cwd") or ".")
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    outputs = [f["path"] for f in (ev.get("files_changed") or []) if f.get("path")]
    changed = set(outputs)
    code: List[str] = []
    inputs: List[str] = []
    params: Dict[str, Any] = {}
    pending: Optional[str] = None

    def as_file(token: str) -> Optional[str]:
        if not token or token.startswith("-"):
            return None
        fp = Path(token) if os.path.isabs(token) else cwd_abs / token
        if not fp.is_file():
            return None
        rel = p.rel(fp)
        return None if rel.startswith("/") else rel   # outside the project: interpreters, system files

    def classify(rel: str) -> None:
        if rel in changed:
            return
        if rel.lower().endswith(SCRIPT_SUFFIXES):
            if rel not in code:
                code.append(rel)
        elif rel not in inputs:
            inputs.append(rel)

    def set_param(key: str, raw: str) -> None:
        if not key or len(raw) > 80 or "\n" in raw or " " in raw:
            return
        try:
            k, v = parse_param(f"{key}={raw}")
        except ValueError:
            return
        params.setdefault(k, v)

    for tok in toks:
        if tok in SHELL_OPERATORS:
            pending = None
            continue
        if tok.startswith("-") and len(tok) > 1 and not re.match(r"^-\d", tok):
            pending = None
            key, has_eq, val = tok.lstrip("-").partition("=")
            if has_eq:
                rel = as_file(val)
                if rel is not None:
                    classify(rel)
                else:
                    set_param(key, val)
            else:
                pending = key
            continue
        rel = as_file(tok)
        if rel is not None:
            classify(rel)
            pending = None
            continue
        if pending is not None:
            set_param(pending, tok)
            pending = None
    return {"outputs": outputs, "inputs": inputs, "code": code, "params": params}


def record_run(p: Project, label: str, *, inputs: List[str], outputs: List[str], code: List[str],
               params: Dict[str, Any], command: Optional[str], evidence_mode: str,
               note: Optional[str], status: str, supersedes: Optional[str],
               node_id: Optional[str] = None, exec_event: Optional[Dict[str, Any]] = None,
               infer: bool = True) -> Dict[str, Any]:
    events = p.events()
    nodes = fold_nodes(events)
    rel_in = [p.rel(x) for x in inputs]
    rel_out = [p.rel(x) for x in outputs]
    rel_code = [p.rel(x) for x in code]
    params = dict(params or {})
    new_nodes: List[Dict[str, Any]] = []
    warnings: List[str] = []

    ev = exec_event or resolve_evidence(events, nodes, evidence_mode, rel_out, rel_code, command)

    inferred: Dict[str, Any] = {}
    source = ev
    if source is None and command and infer:
        # a step that ran before recording started: read what we can off the command the
        # agent gives us; the declared outputs stand in for the files it changed
        source = {"command": command, "cwd": ".", "files_changed": [{"path": r} for r in rel_out]}
    if source is not None and infer:
        guess = infer_from_exec(p, source)
        if not rel_out and guess["outputs"]:
            rel_out = guess["outputs"]
            inferred["outputs"] = rel_out
        if not rel_code and guess["code"]:
            rel_code = guess["code"]
            inferred["code"] = rel_code
        if not rel_in and guess["inputs"]:
            rel_in = guess["inputs"]
            inferred["inputs"] = rel_in
        if not params and guess["params"]:
            params = guess["params"]
            inferred["params"] = params

    input_ids: List[str] = []
    for r in rel_in:
        nid, node = ensure_artifact(p, nodes, r, require_exists=True)
        input_ids.append(nid)
        if node:
            new_nodes.append(node)

    code_refs: List[Dict[str, Any]] = []
    max_bytes = int(p.config.get("hash_max_bytes", 1 << 30))
    for r in rel_code:
        ap = p.abs(r)
        if not ap.is_file():
            warnings.append(f"code file not found: {r}")
            code_refs.append({"path": r, "sha256": None})
        else:
            code_refs.append({"path": r, "sha256": sha256_file(ap, max_bytes)})

    output_ids: List[str] = []
    for r in rel_out:
        nid, node = ensure_artifact(p, nodes, r)
        output_ids.append(nid)
        if node:
            new_nodes.append(node)
        if not p.abs(r).is_file():
            warnings.append(f"declared output does not exist (yet): {r}")

    if ev is not None:
        observed_paths = {f.get("path"): f.get("sha256") for f in (ev.get("files_changed") or [])}
        outputs_observed = []
        for r in rel_out:
            if r in observed_paths:
                current = sha256_file(p.abs(r), max_bytes)
                if observed_paths[r] and current == observed_paths[r]:
                    outputs_observed.append(r)
        evidence = {
            "level": "observed",
            "exec_ids": [ev["id"]],
            "source": ev.get("source"),
            "exit_code": ev.get("exit_code"),
            "started": ev.get("started"),
            "ended": ev.get("ended"),
            "duration_ms": ev.get("duration_ms"),
            "cwd": ev.get("cwd"),
            "stdout_head": ev.get("stdout_head"),
            "stderr_head": ev.get("stderr_head"),
            "outputs_observed": outputs_observed,
        }
        if not command:
            command = ev.get("command")
        if status == "completed" and isinstance(ev.get("exit_code"), int) and ev["exit_code"] != 0:
            status = "failed"
            warnings.append(f"linked execution {ev['id']} exited with code {ev['exit_code']}; status set to failed")
    else:
        evidence = {"level": "declared", "exec_ids": [], "outputs_observed": []}
        if evidence_mode == "auto":
            warnings.append("no captured execution matched; recorded as declared "
                            "(see `whatran.py log`, then --evidence x-NNNN; or run the step through `whatran.py run -- CMD`)")

    if node_id:
        if node_id in nodes:
            raise AgError(f"node id already exists: {node_id}")
        rid = node_id
    else:
        rid = p.next_node_id("run", label, nodes)
    if supersedes and supersedes not in nodes:
        raise AgError(f"--supersedes refers to unknown node {supersedes}")

    run: Dict[str, Any] = {
        "id": rid,
        "type": "run",
        "label": label,
        "command": command,
        "code": code_refs,
        "params": params,
        "inputs": input_ids,
        "outputs": output_ids,
        "status": status,
        "evidence": evidence,
        "environment": env_info(),
        "git": git_info(p.root),
        "verified_by_replay": False,
        "recorded_at": now_iso(),
        "recorded_by": detect_agent(),
    }
    if note:
        run["note"] = note
    if supersedes:
        run["supersedes"] = supersedes

    for n in new_nodes:
        p.append({"event": "node", "node": n})
    p.append({"event": "node", "node": run})
    if supersedes:
        p.append({"event": "status", "id": supersedes, "status": "superseded",
                  "reason": f"superseded by {rid}"})
    return {"id": rid, "evidence": evidence["level"], "exec_ids": evidence["exec_ids"],
            "inputs": input_ids, "outputs": output_ids, "status": status, "warnings": warnings,
            "inferred": inferred, "paths": {"inputs": rel_in, "outputs": rel_out, "code": rel_code},
            "params": params}


def resolve_support(p: Project, nodes: "OrderedDict[str, Dict[str, Any]]", ref: str) -> str:
    if ref in nodes:
        return ref
    rel = p.rel(ref)
    matches = [n for n in nodes.values() if n.get("type") == "artifact" and n.get("path") == rel]
    if matches:
        return matches[-1]["id"]
    raise AgError(f"--supports {ref!r} is neither a node id nor a recorded artifact path "
                  f"(record it first with `whatran.py artifact {ref}` or as the output of a recorded step)")


def record_claim(p: Project, text: str, supports: List[str], status: str, note: Optional[str],
                 node_id: Optional[str] = None) -> Dict[str, Any]:
    events = p.events()
    nodes = fold_nodes(events)
    ids = [resolve_support(p, nodes, s) for s in supports]
    auto = False
    if not ids:
        completed = [n for n in nodes.values() if n.get("type") == "run" and n.get("status") == "completed"]
        if completed:
            ids = [completed[-1]["id"]]
            auto = True
    if node_id:
        if node_id in nodes:
            raise AgError(f"node id already exists: {node_id}")
        cid = node_id
    else:
        cid = p.next_node_id("claim", text, nodes)
    claim: Dict[str, Any] = {
        "id": cid,
        "type": "claim",
        "label": text,
        "supported_by": ids,
        "status": status,
        "recorded_at": now_iso(),
        "recorded_by": detect_agent(),
    }
    if note:
        claim["note"] = note
    p.append({"event": "node", "node": claim})
    warnings = [] if ids else ["claim has no supporting evidence; add --supports <run id | artifact path>"]
    return {"id": cid, "supported_by": ids, "auto_supports": auto, "warnings": warnings}


# --------------------------------------------------------------------------- #
# compiling the graph
# --------------------------------------------------------------------------- #

def build_graph(p: Project, verify: bool = True) -> Dict[str, Any]:
    events = p.events()
    nodes = fold_nodes(events)
    execs = exec_events(events)

    runs = [n for n in nodes.values() if n.get("type") == "run"]
    artifacts = [n for n in nodes.values() if n.get("type") == "artifact"]
    claims = [n for n in nodes.values() if n.get("type") == "claim"]

    generated_by: Dict[str, str] = {}
    used_by: Dict[str, List[str]] = defaultdict(list)
    for r in runs:
        for o in r.get("outputs", []) or []:
            generated_by[o] = r["id"]
        for i in r.get("inputs", []) or []:
            used_by[i].append(r["id"])

    for a in artifacts:
        if a["id"] not in generated_by:
            a["role"] = "input"
        elif used_by.get(a["id"]):
            a["role"] = "intermediate"
        else:
            a["role"] = "result"
        a["generated_by"] = generated_by.get(a["id"])
        a["used_by"] = list(used_by.get(a["id"], []))
        a["integrity"] = p.integrity(a) if verify else "unchecked"

    edges: List[Dict[str, str]] = []
    for r in runs:
        for i in r.get("inputs", []) or []:
            if i in nodes:
                edges.append({"from": i, "to": r["id"], "kind": "used"})
        for o in r.get("outputs", []) or []:
            if o in nodes:
                edges.append({"from": r["id"], "to": o, "kind": "generated"})
        if r.get("supersedes") in nodes:
            edges.append({"from": r["id"], "to": r["supersedes"], "kind": "supersedes"})
    for c in claims:
        for s in c.get("supported_by", []) or []:
            if s in nodes:
                edges.append({"from": s, "to": c["id"], "kind": "supports"})
    for a in artifacts:
        if a.get("supersedes") in nodes:
            edges.append({"from": a["id"], "to": a["supersedes"], "kind": "supersedes"})

    # dependency closure ---------------------------------------------------
    deps: Dict[str, set] = defaultdict(set)
    for e in edges:
        if e["kind"] in ("used", "generated", "supports"):
            deps[e["to"]].add(e["from"])

    def run_active(r: Dict[str, Any]) -> bool:
        return r.get("status", "completed") in ACTIVE_RUN_STATUSES

    active_claims = [c for c in claims if c.get("status") != "rejected"]
    if active_claims:
        terminals = [c["id"] for c in active_claims]
    else:
        terminals = [a["id"] for a in artifacts
                     if a.get("role") == "result" and a.get("generated_by") in nodes
                     and run_active(nodes[a["generated_by"]])]
        if not terminals:
            terminals = [r["id"] for r in runs if run_active(r)]
    main: set = set()
    stack = list(terminals)
    while stack:
        nid = stack.pop()
        if nid in main:
            continue
        main.add(nid)
        stack.extend(deps.get(nid, ()))
    # side products of main-path runs are part of the same computation
    for r in runs:
        if r["id"] in main:
            for o in r.get("outputs", []) or []:
                main.add(o)
    for n in nodes.values():
        n["on_main_path"] = n["id"] in main

    # replay evidence: only a passed check (or a pointed-at CI run) sets verified_by_replay
    for ev in events:
        if ev.get("event") != "replay":
            continue
        for rid in ev.get("runs", []) or []:
            n = nodes.get(rid)
            if n is not None and n.get("type") == "run":
                n["verified_by_replay"] = True
                n["replay"] = {k: ev.get(k) for k in ("ts", "source", "ref", "manifest") if ev.get(k)}

    linked = linked_exec_ids(nodes)
    exec_log = []
    for e in execs:
        item = dict(e)
        item.pop("event", None)
        item["linked_run"] = linked.get(e["id"])
        exec_log.append(item)

    summary = {
        "runs": {
            "total": len(runs),
            "observed": sum(1 for r in runs if (r.get("evidence") or {}).get("level") == "observed"),
            "declared": sum(1 for r in runs if (r.get("evidence") or {}).get("level") != "observed"),
            "rejected": sum(1 for r in runs if r.get("status") == "rejected"),
            "failed": sum(1 for r in runs if r.get("status") == "failed"),
            "superseded": sum(1 for r in runs if r.get("status") == "superseded"),
            "replay_verified": sum(1 for r in runs if r.get("verified_by_replay")),
        },
        "artifacts": {
            "total": len(artifacts),
            **{k: sum(1 for a in artifacts if a.get("integrity") == k)
               for k in ("intact", "modified", "missing", "unchecked")},
        },
        "claims": {
            "total": len(claims),
            "rejected": sum(1 for c in claims if c.get("status") == "rejected"),
        },
        "executions": {"total": len(execs), "linked": sum(1 for e in execs if e["id"] in linked)},
        "main_path_nodes": len(main),
    }

    return {
        "format": FORMAT,
        "generator": f"whatran.py/{__version__}",
        "generated_at": now_iso(),
        "project": p.config.get("project", {}),
        "nodes": list(nodes.values()),
        "edges": edges,
        "main_path": [nid for nid in nodes if nid in main],
        "summary": summary,
        "exec_log": exec_log,
    }


def graph_to_mermaid(graph: Dict[str, Any], main_only: bool = True) -> str:
    nodes = {n["id"]: n for n in graph["nodes"]}
    keep = set(graph["main_path"]) if main_only else set(nodes)
    ident = {nid: f"n{i}" for i, nid in enumerate(nodes)}

    def esc(s: str) -> str:
        return s.replace('"', "'").replace("\n", " ")[:60]

    lines = ["flowchart TD"]
    for nid, n in nodes.items():
        if nid not in keep:
            continue
        label = esc(n.get("label", nid))
        t = n.get("type")
        if t == "artifact":
            lines.append(f'  {ident[nid]}[/"{label}"/]')
        elif t == "claim":
            lines.append(f'  {ident[nid]}(["{label}"])')
        else:
            lines.append(f'  {ident[nid]}["{label}"]')
    for e in graph["edges"]:
        if e["from"] in keep and e["to"] in keep and e["kind"] != "supersedes":
            arrow = "-.->" if e["kind"] == "supports" else "-->"
            lines.append(f"  {ident[e['from']]} {arrow} {ident[e['to']]}")
    return "\n".join(lines) + "\n"


def write_graph(p: Project, graph: Dict[str, Any]) -> None:
    p.graph_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = ["# Analysis graph", "",
          f"Generated {graph['generated_at']} by {graph['generator']}. "
          "Main path only; rejected branches and unlinked executions are kept in `graph.json`.",
          "", "```mermaid", graph_to_mermaid(graph).rstrip(), "```", ""]
    (p.ag_dir / GRAPH_MD_NAME).write_text("\n".join(md), encoding="utf-8")


def topo_sort_runs(graph: Dict[str, Any], ids: set) -> List[Dict[str, Any]]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    preds: Dict[str, set] = defaultdict(set)
    for e in graph["edges"]:
        if e["kind"] == "used" and e["to"] in ids:
            gen = nodes.get(e["from"], {}).get("generated_by")
            if gen and gen in ids:
                preds[e["to"]].add(gen)
    done: List[str] = []
    remaining = set(ids)
    while remaining:
        ready = sorted(r for r in remaining if preds[r] <= set(done))
        if not ready:
            ready = sorted(remaining)  # cycle guard
        done.append(ready[0])
        remaining.discard(ready[0])
    return [nodes[i] for i in done]


PACKAGE_MANIFEST = "whatran.json"


def export_package(p: Project, graph: Dict[str, Any]) -> Dict[str, Any]:
    """Draft a package manifest (the package-analysis contract) from the main path.

    inputs and step.produces are project-relative; outputs are relative to results_dir,
    as check.py expects.
    """
    nodes = {n["id"]: n for n in graph["nodes"]}
    main = set(graph["main_path"])
    run_ids = {nid for nid in main if nodes[nid]["type"] == "run" and nodes[nid].get("status") == "completed"}
    runs = topo_sort_runs(graph, run_ids)
    inputs = sorted({nodes[a]["path"] for a in main
                     if nodes[a]["type"] == "artifact" and nodes[a].get("role") == "input"})
    result_paths = sorted(nodes[a]["path"] for a in main
                          if nodes[a]["type"] == "artifact" and nodes[a].get("role") == "result")
    first_dirs = {path.split("/", 1)[0] for path in result_paths if "/" in path}
    results_dir = first_dirs.pop() if len(first_dirs) == 1 else "results"
    steps = []
    for r in runs:
        script = (r.get("code") or [{}])[0].get("path") or r.get("command") or ""
        steps.append({
            "id": r["id"][len("run-"):] if r["id"].startswith("run-") else r["id"],
            "title": r.get("label", r["id"]),
            "script": script,
            "produces": [nodes[o]["path"] for o in r.get("outputs", []) if o in nodes],
        })
    outputs = []
    for path in result_paths:
        rel = path[len(results_dir) + 1:] if path.startswith(results_dir + "/") else path
        if rel.endswith(".json"):
            outputs.append({"path": rel, "compare": "numeric", "tolerance": 1e-9})
        elif rel.endswith((".csv", ".tsv", ".txt", ".md")):
            outputs.append({"path": rel, "compare": "exact"})
        else:
            outputs.append({"path": rel, "compare": "exists"})
    claims = [nodes[c]["label"] for c in main if nodes[c]["type"] == "claim"]
    return {
        "method": {
            "title": p.config.get("project", {}).get("name") or p.root.name,
            "question": p.config.get("project", {}).get("description") or "",
            "findings": claims,
        },
        "inputs": inputs,
        "run": "bash run.sh",
        "results_dir": results_dir,
        "steps": steps,
        "outputs": outputs,
    }


def runs_covered_by_manifest(manifest: Dict[str, Any], nodes: "OrderedDict[str, Dict[str, Any]]") -> List[str]:
    """Map package steps back to recorded runs, by id first, then by script path."""
    covered: List[str] = []
    for step in manifest.get("steps", []) or []:
        sid = str(step.get("id", ""))
        rid = sid if sid in nodes else f"run-{sid}"
        if rid in nodes and nodes[rid].get("type") == "run":
            covered.append(rid)
            continue
        script = step.get("script")
        for n in nodes.values():
            if n.get("type") == "run" and script and any(c.get("path") == script for c in n.get("code") or []):
                covered.append(n["id"])
                break
    return list(dict.fromkeys(covered))


# --------------------------------------------------------------------------- #
# package assets, embedded so a copied whatran.py can generate a package anywhere.
# Canonical copies live in skills/package-analysis/assets/; tests keep them in sync.
# --------------------------------------------------------------------------- #

CHECK_PY = r'''#!/usr/bin/env python3
"""Compare regenerated outputs against the reference outputs.

Output declarations are read from whatran.json. The reference directory holds the
committed outputs that were moved aside before the pipeline re-ran, so a passing
comparison means the outputs were regenerated from raw inputs, not reused.

Usage: python3 check.py <generated_dir> <reference_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_TOLERANCE = 1e-9


def relative_path(value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"output path must stay inside results_dir: {value!r}")
    return path


def numbers_match(a: object, b: object, tol: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(numbers_match(a[k], b[k], tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(numbers_match(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    return a == b


def main() -> int:
    generated_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results")
    reference_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("reference-outputs")
    manifest = json.loads(Path("whatran.json").read_text(encoding="utf-8"))

    failures: list[str] = []
    for output in manifest["outputs"]:
        try:
            rel = relative_path(output["path"])
        except (KeyError, ValueError) as exc:
            failures.append(str(exc))
            continue
        compare = output.get("compare", "exact")
        generated = generated_dir / rel
        reference = reference_dir / rel

        if not generated.is_file():
            failures.append(f"{rel}: not regenerated as a regular file")
            continue
        if not reference.is_file():
            failures.append(f"{rel}: no committed regular-file reference")
            continue
        if compare == "exists":
            if generated.stat().st_size > 0:
                print(f"[ok] {rel}: regenerated (non-empty file)")
            else:
                failures.append(f"{rel}: regenerated output is not a non-empty file")
            continue
        if compare == "numeric":
            tol = float(output.get("tolerance", DEFAULT_TOLERANCE))
            try:
                gen_value = json.loads(generated.read_text(encoding="utf-8"))
                ref_value = json.loads(reference.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                failures.append(f"{rel}: could not read JSON ({exc})")
                continue
            if numbers_match(gen_value, ref_value, tol):
                print(f"[ok] {rel}: numeric match (tol={tol})")
            else:
                failures.append(f"{rel}: numeric mismatch vs reference")
            continue
        if compare == "exact" and generated.read_bytes() == reference.read_bytes():
            print(f"[ok] {rel}: byte-identical")
        elif compare == "exact":
            failures.append(f"{rel}: bytes differ from reference")
        else:
            failures.append(f"{rel}: unsupported comparison type {compare!r}")

    if failures:
        print("\nREPRODUCTION FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nREPRODUCTION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

REPRODUCE_YML = r'''# GitHub Actions workflow template.
# Copy this file to  .github/workflows/reproduce.yml  at your repository root.
#
# On every push, a blank GitHub-hosted runner checks out only what is committed,
# re-runs the pipeline from the raw inputs, compares the declared outputs, rebuilds
# the showcase, and publishes it.
#
# Free runners are limited (~2-4 vCPU, ~7-16 GB RAM, ~14 GB disk, no GPU, 6 h/job).
# If the full analysis does not fit, point `runs-on` at a self-hosted or larger
# runner, or reproduce a declared representative subset and say so in the badge.
name: reproduce

on:
  push:
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  reproduce:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Move reference outputs aside
        # Remove the committed answer from the package before re-running.
        run: mv results "$RUNNER_TEMP/reference-outputs"

      - name: Reconstruct environment and re-run from raw inputs
        # run.sh owns dependency installation as well as the ordered analysis
        # steps, so local and CI reproduction use exactly the same command.
        run: bash run.sh

      - name: Compare regenerated outputs against reference
        run: python3 check.py results "$RUNNER_TEMP/reference-outputs"

      - name: Build showcase site
        # build_site.py is the package's own analysis-specific showcase builder
        # (adapted from the skill example), not a fixed skill asset.
        run: python3 build_site.py

      - name: Configure GitHub Pages
        uses: actions/configure-pages@v5

      - name: Upload site
        uses: actions/upload-pages-artifact@v4
        with:
          path: site

  deploy-pages:
    needs: reproduce
    runs-on: ubuntu-latest
    # Publishes the showcase to GitHub Pages. Enable Pages for the repo
    # (Settings -> Pages -> Source: GitHub Actions) for this to take effect.
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
'''

BUILD_SITE_PY = r'''#!/usr/bin/env python3
"""Build a result-first page from the package manifest and the recorded analysis graph.

Generated by `whatran.py package`. Generic on purpose: findings, outputs (figures
inline, tables and JSON previewed), the final steps with their commands and
scripts, and a link to the interactive analysis graph. Edit freely; it is yours.
Run from the package root: python3 build_site.py  ->  site/index.html
"""
from __future__ import annotations

import csv
import html
import json
import shutil
from pathlib import Path

MANIFEST = Path("whatran.json")
GRAPH = Path("analysis/graph.json")
VIEWER = Path("analysis/index.html")
SITE = Path("site")
MAX_ROWS = 20
MAX_JSON_CHARS = 3000


def esc(value: object) -> str:
    return html.escape(str(value))


def render_output(results: Path, output: dict) -> str:
    rel = str(output["path"])
    path = results / rel
    name = esc(rel)
    if not path.is_file():
        return f"<figure><figcaption>{name}: missing</figcaption></figure>"
    suffix = path.suffix.lower()
    if suffix in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
        dest = SITE / path.name
        shutil.copy2(path, dest)
        return f'<figure><img src="{esc(path.name)}" alt="{name}"><figcaption>{name}</figcaption></figure>'
    if suffix == ".json":
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            text = json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            pass
        cut = text[:MAX_JSON_CHARS] + ("\n…" if len(text) > MAX_JSON_CHARS else "")
        return f"<figure><figcaption>{name}</figcaption><pre>{esc(cut)}</pre></figure>"
    if suffix in (".csv", ".tsv"):
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.reader(fh, delimiter="\t" if suffix == ".tsv" else ","))
        if not rows:
            return f"<figure><figcaption>{name}: empty</figcaption></figure>"
        head = "".join(f"<th>{esc(c)}</th>" for c in rows[0])
        body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows[1:MAX_ROWS + 1])
        more = f"<p class='note'>{len(rows) - 1 - MAX_ROWS} more rows</p>" if len(rows) - 1 > MAX_ROWS else ""
        return (f"<figure><figcaption>{name}</figcaption><div class='table-wrap'><table><thead><tr>{head}</tr></thead>"
                f"<tbody>{body}</tbody></table></div>{more}</figure>")
    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_JSON_CHARS]
    return f"<figure><figcaption>{name}</figcaption><pre>{esc(text)}</pre></figure>"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    method = manifest.get("method", {})
    results = Path(manifest.get("results_dir", "results"))
    graph = json.loads(GRAPH.read_text(encoding="utf-8")) if GRAPH.is_file() else {"nodes": []}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    summary = graph.get("summary", {}).get("runs", {})

    SITE.mkdir(exist_ok=True)
    findings = method.get("findings") or [n["label"] for n in nodes.values()
                                           if n.get("type") == "claim" and n.get("status") != "rejected"]
    findings_html = "".join(f"<li>{esc(f)}</li>" for f in findings) or "<li>No findings declared.</li>"
    outputs_html = "".join(render_output(results, o) for o in manifest.get("outputs", []))

    steps_html = []
    for index, step in enumerate(manifest.get("steps", []), start=1):
        run = nodes.get("run-" + str(step.get("id", ""))) or nodes.get(str(step.get("id", ""))) or {}
        command = run.get("command") or ""
        params = run.get("params") or {}
        script = Path(str(step.get("script", "")))
        source = script.read_text(encoding="utf-8", errors="replace") if script.is_file() else ""
        badge = (run.get("evidence") or {}).get("level", "")
        if run.get("verified_by_replay"):
            badge += " · replay verified"
        meta = []
        if command:
            meta.append(f"<p><code>{esc(command)}</code></p>")
        if params:
            meta.append("<p class='note'>" + " · ".join(f"{esc(k)}={esc(json.dumps(v) if isinstance(v, (list, dict)) else v)}"
                                                       for k, v in params.items()) + "</p>")
        if source:
            meta.append(f"<pre><code>{esc(source)}</code></pre>")
        steps_html.append(
            "<details class='step'><summary>"
            f"<span>{index:02}</span><strong>{esc(step.get('title', ''))}</strong>"
            f"<small>{esc(script) if source else esc(badge)}</small></summary>{''.join(meta)}</details>"
        )

    graph_link = ""
    if VIEWER.is_file() and GRAPH.is_file():
        (SITE / "graph").mkdir(exist_ok=True)
        shutil.copy2(VIEWER, SITE / "graph" / "index.html")
        shutil.copy2(GRAPH, SITE / "graph" / "graph.json")
        observed = summary.get("observed", 0)
        total = summary.get("total", 0)
        replayed = summary.get("replay_verified", 0)
        graph_link = (f"<p><a class='button' href='graph/'>Open the analysis graph →</a> "
                      f"<span class='note'>{observed} of {total} runs observed, {replayed} replay-verified, "
                      f"rejected branches kept.</span></p>")

    title = esc(method.get("title", "Analysis"))
    question = esc(method.get("question", ""))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--paper:#f6f3ec;--white:#fffef9;--ink:#132827;--muted:#5a6966;--line:#d7d4ca;--teal:#087f70;--coral:#bd432f;--night:#102a2b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,sans-serif;line-height:1.55}}
header,main,footer{{width:min(960px,calc(100% - 2rem));margin:auto}}header{{padding:4rem 0 2rem}}h1{{font-size:clamp(2.2rem,6vw,4.2rem);line-height:1.02;letter-spacing:-.045em;margin:.5rem 0 1rem}}
.eyebrow{{color:var(--teal);font-size:.78rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}}.question,.note{{color:var(--muted)}}
.findings{{margin:1.5rem 0;padding:1rem 1.25rem 1rem 2.4rem;border-left:5px solid var(--teal);background:#e5f1ed;font-size:1.1rem}}.findings li{{margin:.3rem 0}}
section{{padding:3rem 0;border-top:1px solid var(--line)}}h2{{font-size:clamp(1.6rem,4vw,2.6rem);letter-spacing:-.035em}}
figure{{margin:0 0 1rem;padding:1rem;border:1px solid var(--line);border-radius:1rem;background:var(--white);overflow:auto}}figure img{{display:block;max-width:100%;height:auto}}figcaption{{color:var(--muted);font-size:.84rem;font-family:monospace;margin-bottom:.5rem}}
.table-wrap{{overflow:auto}}table{{border-collapse:collapse;font-size:.9rem}}th,td{{padding:.45rem .7rem;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
details{{border:1px solid var(--line);border-radius:.8rem;background:var(--white);margin:.6rem 0;overflow:hidden}}summary{{display:grid;grid-template-columns:2rem 1fr auto;gap:.7rem;padding:1rem;cursor:pointer}}summary span{{color:var(--coral);font-family:monospace}}summary small{{color:var(--muted);font-family:monospace}}
details p{{padding:0 1rem}}pre{{max-width:100%;margin:0;padding:1rem;overflow:auto;background:var(--night);color:#d9ece7;font:.8rem/1.6 monospace}}code{{font-family:monospace;font-size:.85em}}
.button{{display:inline-block;padding:.6rem 1rem;border:1px solid var(--night);border-radius:.6rem;background:var(--night);color:#fff;text-decoration:none;font-weight:700}}
footer{{padding:2rem 0 3rem;border-top:1px solid var(--line);color:var(--muted);font-size:.82rem}}
</style></head><body>
<header>
<p class="eyebrow">Reproducible analysis · generated from checked outputs</p>
<h1>{title}</h1>
<p class="question">{question}</p>
<ul class="findings">{findings_html}</ul>
{graph_link}
<p class="note">A fresh-clone rerun regenerates the declared outputs and compares them with the committed references. That is rerun evidence, not scientific review.</p>
</header>
<main>
<section><h2>Results</h2>{outputs_html}</section>
<section><h2>Final method</h2><p class="note">Each step shows the command that ran, its parameters, and the script.</p>{''.join(steps_html)}</section>
<section><h2>Rerun it</h2><pre><code>bash run.sh
python3 check.py {esc(results)} &lt;reference outputs moved aside&gt;</code></pre></section>
</main>
<footer>Outputs compared under the rules in whatran.json. Passing means regenerated and matched; it does not establish correctness or completeness of the method.</footer>
</body></html>
"""
    (SITE / "index.html").write_text(page, encoding="utf-8")
    print(f"wrote {SITE / 'index.html'}")


if __name__ == "__main__":
    main()
'''


# --------------------------------------------------------------------------- #
# command-line commands
# --------------------------------------------------------------------------- #

def require_project(args: argparse.Namespace) -> Project:
    root = find_root(args.root)
    if root is None:
        raise AgError("no analysis/config.json found in this directory or its parents; run `whatran.py init` first")
    return Project(root)


def plugin_viewer_path() -> Optional[Path]:
    here = Path(__file__).resolve()
    candidates = [
        here.parent / VIEWER_NAME,                 # copied into analysis/
        here.parent.parent / "viewer" / VIEWER_NAME,  # plugin layout: bin/whatran.py + viewer/index.html
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root or os.getcwd()).resolve()
    ag_dir = root / AG_DIRNAME
    if (ag_dir / CONFIG_NAME).exists() and not args.force:
        print(f"already initialised: {ag_dir / CONFIG_NAME} (use --force to overwrite config)")
        return 0
    ag_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "format": FORMAT,
        "project": {"name": args.name or root.name, "description": args.description or ""},
        "watch": ["."],
        "exclude": DEFAULT_CONFIG["exclude"],
        "observe": {"enabled": True, "nudge": True},
    }
    (ag_dir / CONFIG_NAME).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (ag_dir / EVENTS_NAME).touch()
    (ag_dir / ".gitignore").write_text(f"{STATE_DIRNAME}/\n", encoding="utf-8")
    (ag_dir / STATE_DIRNAME).mkdir(exist_ok=True)
    copied = []
    if not args.no_copy:
        dst = ag_dir / "whatran.py"
        src = Path(__file__).resolve()
        if src != dst.resolve():
            shutil.copyfile(src, dst)
            dst.chmod(0o755)
        copied.append(dst)
        viewer = plugin_viewer_path()
        if viewer and viewer.resolve() != (ag_dir / VIEWER_NAME).resolve():
            shutil.copyfile(viewer, ag_dir / VIEWER_NAME)
            copied.append(ag_dir / VIEWER_NAME)
    print(f"initialised {ag_dir}")
    for c in copied:
        print(f"  copied {c.relative_to(root)}")
    print("recording is on. after each meaningful step: python3 analysis/whatran.py record \"<what it did>\"")
    print("(outside Claude Code, run steps as: python3 analysis/whatran.py run -- CMD). finish with: done")
    return 0


def _params_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if getattr(args, "params_json", None):
        try:
            params.update(json.loads(args.params_json))
        except json.JSONDecodeError as exc:
            raise AgError(f"--params-json is not valid JSON: {exc}")
    for raw in getattr(args, "param", None) or []:
        k, v = parse_param(raw)
        params[k] = v
    return params


def _print_result(res: Dict[str, Any], as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(human)
        for w in res.get("warnings", []) or []:
            print(f"  warning: {w}")


def _fmt_params(params: Dict[str, Any]) -> str:
    return ", ".join(f"{k}={json.dumps(v) if isinstance(v, (list, dict)) else v}" for k, v in params.items())


def cmd_record(args: argparse.Namespace) -> int:
    p = require_project(args)
    res = record_run(p, args.label, inputs=args.inputs or [], outputs=args.outputs or [],
                     code=args.code or [], params=_params_from_args(args), command=args.cmd,
                     evidence_mode=args.evidence, note=args.note, status=args.status,
                     supersedes=args.supersedes, node_id=args.id, infer=not args.no_infer)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    ev = f"observed via {', '.join(res['exec_ids'])}" if res["exec_ids"] else "declared only"
    print(f"recorded {res['id']}  [{res['status']}; {ev}]")
    paths = res["paths"]
    for key in ("inputs", "outputs", "code"):
        if paths[key]:
            tag = " (read from the command)" if key in res["inferred"] else ""
            print(f"  {key}: {', '.join(paths[key])}{tag}")
    if res["params"]:
        tag = " (read from the command)" if "params" in res["inferred"] else ""
        print(f"  params: {_fmt_params(res['params'])}{tag}")
    for w in res["warnings"]:
        print(f"  warning: {w}")
    if res["inferred"]:
        print(f"  wrong? fix with: whatran.py update {res['id']} --param k=v   or re-record with --in/--out/--code")
    return 0


def cmd_artifact(args: argparse.Namespace) -> int:
    p = require_project(args)
    res = record_artifact(p, args.path, args.label, args.origin, args.note)
    verb = "recorded" if res["created"] else "already recorded (same content)"
    _print_result(res, args.json, f"{verb} {res['id']}  {res['path']}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    p = require_project(args)
    res = record_claim(p, args.text, args.supports or [], args.status, args.note, node_id=args.id)
    how = " (the last completed run; pass --supports to change)" if res.get("auto_supports") else ""
    _print_result(res, args.json, f"recorded {res['id']}  supported by {', '.join(res['supported_by']) or 'nothing'}{how}")
    return 0


def _tee(stream, sink, chunks: List[str]) -> None:
    for line in iter(stream.readline, ""):
        chunks.append(line)
        sink.write(line)
        sink.flush()


def cmd_run(args: argparse.Namespace) -> int:
    p = require_project(args)
    cmd = list(args.cmd or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise AgError("nothing to run: whatran.py run [options] -- COMMAND ...")
    cmd_str = cmd[0] if len(cmd) == 1 else shlex.join(cmd)
    events = p.events()
    before = p.snapshot()
    started = time.time()
    proc = subprocess.Popen(cmd_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", bufsize=1)
    out: List[str] = []
    err: List[str] = []
    t1 = threading.Thread(target=_tee, args=(proc.stdout, sys.stdout, out))
    t2 = threading.Thread(target=_tee, args=(proc.stderr, sys.stderr, err))
    t1.start(); t2.start()
    rc = proc.wait()
    t1.join(); t2.join()
    ended = time.time()
    files, truncated = p.changed_since_snapshot(before)
    ev = p.make_exec_event(cmd_str, source="ag-run", exit_code=rc, started=started, ended=ended,
                           stdout="".join(out), stderr="".join(err), cwd=os.getcwd(),
                           files_changed=files, truncated=truncated, events=events)
    p.append(ev)
    print(f"whatran: captured {ev['id']} (exit {rc}, {len(files)} file(s) changed)", file=sys.stderr)
    if args.label:
        status = "completed" if rc == 0 else "failed"
        res = record_run(p, args.label, inputs=args.inputs or [], outputs=args.outputs or [],
                         code=args.code or [], params=_params_from_args(args), command=cmd_str,
                         evidence_mode=ev["id"], note=args.note, status=status, supersedes=args.supersedes,
                         exec_event=ev)
        print(f"whatran: recorded {res['id']} [{status}; observed via {ev['id']}]", file=sys.stderr)
        for w in res["warnings"]:
            print(f"whatran:   warning: {w}", file=sys.stderr)
    elif files and not args.quiet:
        print(f"whatran: a scientific step? then: python3 {AG_DIRNAME}/whatran.py record \"<what it did>\"", file=sys.stderr)
    return rc


def cmd_reject(args: argparse.Namespace) -> int:
    p = require_project(args)
    nodes = fold_nodes(p.events())
    if args.id not in nodes:
        raise AgError(f"unknown node {args.id}")
    if nodes[args.id].get("type") == "artifact":
        raise AgError("artifacts cannot be rejected; reject the run that produced them")
    p.append({"event": "status", "id": args.id, "status": "rejected", "reason": args.reason})
    print(f"{args.id} marked rejected (kept in the record): {args.reason}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    p = require_project(args)
    nodes = fold_nodes(p.events())
    if args.id not in nodes:
        raise AgError(f"unknown node {args.id}")
    fields: Dict[str, Any] = {}
    if args.label:
        fields["label"] = args.label
    if args.note:
        fields["note"] = args.note
    if args.status:
        t = nodes[args.id].get("type")
        allowed = RUN_STATUSES if t == "run" else CLAIM_STATUSES if t == "claim" else ()
        if args.status not in allowed:
            raise AgError(f"status {args.status!r} not allowed for {t}; choose from {', '.join(allowed) or 'nothing'}")
        fields["status"] = args.status
    if args.param:
        params = dict(nodes[args.id].get("params") or {})
        for raw in args.param:
            k, v = parse_param(raw)
            params[k] = v
        fields["params"] = params
    if not fields:
        raise AgError("nothing to update")
    p.append({"event": "update", "id": args.id, "fields": fields})
    print(f"updated {args.id}: {', '.join(fields)}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    p = require_project(args)
    events = p.events()
    nodes = fold_nodes(events)
    linked = linked_exec_ids(nodes)
    execs = exec_events(events)[-args.n:]
    if args.json:
        print(json.dumps([dict(e, linked_run=linked.get(e["id"])) for e in execs], ensure_ascii=False))
        return 0
    if not execs:
        print("no execution evidence yet")
        return 0
    for e in execs:
        files = e.get("files_changed") or []
        link = f" -> {linked[e['id']]}" if e["id"] in linked else "   (unlinked)"
        rc = e.get("exit_code")
        print(f"{e['id']}  {e.get('ts', '')}  exit={rc if rc is not None else '?'}  files={len(files)}{link}")
        print(f"       $ {(e.get('command') or '')[:110]}")
        for f in files[:5]:
            print(f"         {f['path']}")
        if len(files) > 5:
            print(f"         ... {len(files) - 5} more")
    return 0


def print_summary(p: Project, graph: Dict[str, Any]) -> None:
    s = graph["summary"]
    print(f"project: {graph['project'].get('name') or p.root.name}  ({p.ag_dir})")
    print(f"runs: {s['runs']['total']}  observed {s['runs']['observed']}  declared {s['runs']['declared']}  "
          f"rejected {s['runs']['rejected']}  failed {s['runs']['failed']}  replay-verified {s['runs']['replay_verified']}")
    a = s["artifacts"]
    print(f"artifacts: {a['total']}  intact {a['intact']}  modified {a['modified']}  missing {a['missing']}  unchecked {a['unchecked']}")
    print(f"claims: {s['claims']['total']}   executions: {s['executions']['total']} ({s['executions']['linked']} linked)")
    unlinked = [e for e in graph["exec_log"] if not e.get("linked_run") and (e.get("files_changed") or [])]
    if unlinked:
        print(f"unlinked executions that changed files ({len(unlinked)}):")
        for e in unlinked[-8:]:
            print(f"  {e['id']}  {(e.get('command') or '')[:80]}  ({len(e.get('files_changed') or [])} files)")
        print("  if one was a scientific step: whatran.py record \"<what it did>\" --evidence x-NNNN")
    declared = [n for n in graph["nodes"] if n["type"] == "run" and (n.get("evidence") or {}).get("level") != "observed"]
    if declared:
        print(f"declared-only runs (no execution evidence): {', '.join(n['id'] for n in declared)}")


def cmd_status(args: argparse.Namespace) -> int:
    p = require_project(args)
    print_summary(p, build_graph(p, verify=not args.no_verify))
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    """compile + verify + package draft, then say what to do next."""
    p = require_project(args)
    graph = build_graph(p, verify=True)
    write_graph(p, graph)
    draft = export_package(p, graph)
    (p.ag_dir / "whatran.draft.json").write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_summary(p, graph)
    s = graph["summary"]
    print(f"wrote {p.graph_path.relative_to(p.root)} and analysis/whatran.draft.json")
    if s["artifacts"]["modified"] or s["artifacts"]["missing"]:
        print("warning: some recorded files changed or vanished since they were recorded (see above)")
    if s["claims"]["total"] == 0:
        print("no claims yet: whatran.py claim \"<finding>\" marks what the result path leads to")
    print(f"open the graph:      python3 {AG_DIRNAME}/whatran.py open")
    print(f"package the result:  python3 {AG_DIRNAME}/whatran.py package   (then `check`)")
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    p = require_project(args)
    graph = build_graph(p, verify=not args.no_verify)
    write_graph(p, graph)
    s = graph["summary"]
    print(f"wrote {p.graph_path.relative_to(p.root)}  "
          f"({len(graph['nodes'])} nodes, {len(graph['edges'])} edges, main path {s['main_path_nodes']})")
    problems = s["artifacts"]["modified"] + s["artifacts"]["missing"]
    if problems:
        print(f"warning: {s['artifacts']['modified']} artifact(s) modified and {s['artifacts']['missing']} missing since recording")
    if args.strict and (problems or s["runs"]["declared"]):
        return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    p = require_project(args)
    graph = build_graph(p, verify=True)
    write_graph(p, graph)
    bad = 0
    for n in graph["nodes"]:
        if n["type"] == "artifact" and n.get("integrity") in ("modified", "missing"):
            print(f"{n['integrity']:8s} {n['path']}  ({n['id']})")
            bad += 1
        if n["type"] == "run" and (n.get("evidence") or {}).get("level") != "observed":
            print(f"declared {n['id']}  {n.get('label', '')}  (no execution evidence)")
            if args.strict:
                bad += 1
    s = graph["summary"]
    print(f"artifacts intact {s['artifacts']['intact']}/{s['artifacts']['total']}; "
          f"runs observed {s['runs']['observed']}/{s['runs']['total']}; replay-verified {s['runs']['replay_verified']}")
    print("note: intact hashes and observed executions are not a replay. verified_by_replay stays false until a rerun reproduces the outputs.")
    return 1 if bad else 0


def cmd_export(args: argparse.Namespace) -> int:
    p = require_project(args)
    graph = build_graph(p, verify=True)
    if args.format == "package":
        draft = export_package(p, graph)
        dest = p.ag_dir / "whatran.draft.json"
        dest.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {dest.relative_to(p.root)} ({len(draft['steps'])} steps, {len(draft['outputs'])} outputs)")
        print(f"draft only: move it to ./{PACKAGE_MANIFEST}, give the project a run.sh that reruns these steps from a "
              "clean checkout, then let the package-analysis skill add check.py and the CI workflow.")
        return 0
    if args.format == "mermaid":
        print(graph_to_mermaid(graph, main_only=not args.all))
        return 0
    raise AgError(f"unknown export format {args.format}")


def load_manifest(p: Project, name: str = PACKAGE_MANIFEST) -> Dict[str, Any]:
    path = p.root / name
    if not path.is_file():
        raise AgError(f"{name} not found in {p.root}; run `whatran.py package` first")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgError(f"{name} is not valid JSON: {exc}")


def record_replay(p: Project, manifest: Dict[str, Any], manifest_name: str, *, source: str,
                  check: Optional[Dict[str, Any]] = None, ref: Optional[str] = None,
                  note: Optional[str] = None) -> List[str]:
    nodes = fold_nodes(p.events())
    covered = runs_covered_by_manifest(manifest, nodes)
    if not covered:
        raise AgError("no recorded runs match the manifest steps (by id or script path); nothing recorded")
    ev: Dict[str, Any] = {"event": "replay", "manifest": manifest_name, "runs": covered, "source": source}
    if check:
        ev["check"] = check
    if ref:
        ev["ref"] = ref
    if note:
        ev["note"] = note
    p.append(ev)
    return covered


def run_check_py(p: Project, results_dir: str, reference: str) -> Tuple[int, str, Dict[str, Any]]:
    if not (p.root / "check.py").is_file():
        raise AgError("check.py not found in the project root; run `whatran.py package` first")
    cmd = [sys.executable, "check.py", results_dir, reference]
    proc = subprocess.run(cmd, cwd=p.root, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    info = {"command": shlex.join(cmd[1:]), "exit_code": proc.returncode, "stdout_head": proc.stdout[-2000:]}
    return proc.returncode, proc.stdout, info


def cmd_replay_verified(args: argparse.Namespace) -> int:
    p = require_project(args)
    manifest = load_manifest(p, args.manifest)
    if args.reference:
        rc, _, info = run_check_py(p, manifest.get("results_dir", "results"), args.reference)
        if rc != 0:
            raise AgError(f"check.py exited {rc}; nothing recorded")
        covered = record_replay(p, manifest, args.manifest, source="local-check", check=info, note=args.note)
    elif args.ci:
        covered = record_replay(p, manifest, args.manifest, source="ci", ref=args.ci, note=args.note)
    else:
        raise AgError("give --reference DIR (runs check.py against the moved-aside reference outputs) "
                      "or --ci URL (a passing CI run of the package)")
    write_graph(p, build_graph(p, verify=True))
    print(f"replay recorded for {len(covered)} run(s): {', '.join(covered)}; graph.json refreshed")
    return 0


def make_run_sh(graph: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    nodes = {n["id"]: n for n in graph["nodes"]}
    results_dir = manifest.get("results_dir", "results")
    lines = [
        "#!/usr/bin/env bash",
        "# Generated by whatran from the recorded analysis graph. Review it: every command",
        "# below ran during the analysis, in dependency order; package installation is not",
        "# recorded, so add it under Environment if the steps need anything beyond the base system.",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        f"mkdir -p {shlex.quote(results_dir)}",
        "",
        "# Environment (edit as needed), for example:",
        "#   python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt",
        "#   then run the steps with .venv/bin/python",
        "",
    ]
    for i, step in enumerate(manifest.get("steps", []) or [], 1):
        sid = str(step.get("id", ""))
        run = nodes.get(sid) or nodes.get(f"run-{sid}") or {}
        lines.append(f"# {i:02d} {step.get('title', '')}  ({run.get('id', sid)})")
        cmd = run.get("command")
        if not cmd:
            lines.append(f"echo 'run.sh: step {i:02d} ({step.get('title', '')}) has no recorded command; edit run.sh' >&2")
            lines.append("exit 1")
        else:
            cwd = (run.get("evidence") or {}).get("cwd") or "."
            lines.append(cmd if cwd in (".", "") else f"( cd {shlex.quote(cwd)} && {cmd} )")
        lines.append("")
    return "\n".join(lines)


def make_readme(manifest: Dict[str, Any]) -> str:
    method = manifest.get("method", {})
    findings = "".join(f"- {f}\n" for f in method.get("findings", []) or []) or "- (none declared yet)\n"
    return (
        f"# {method.get('title', 'Analysis')}\n\n{method.get('question', '')}\n\n"
        f"## Findings\n\n{findings}\n"
        "## Rerun and check\n\n```bash\nbash run.sh                          # regenerate results/ from the declared inputs\n"
        "python3 analysis/whatran.py check    # rerun from clean, compare with the committed results, mark the graph\n```\n\n"
        "On every push, `.github/workflows/reproduce.yml` does the same on a fresh clone and publishes `site/`.\n\n"
        "## What a pass means\n\nThe declared outputs regenerated from the declared inputs and matched the committed references "
        "under the comparisons in `whatran.json`. It does not establish scientific correctness or that every "
        "method decision was captured.\n\n"
        "## Analysis graph\n\n`analysis/graph.json` records what actually ran, including rejected branches. "
        "Open it with `python3 analysis/whatran.py open`.\n"
    )


def cmd_package(args: argparse.Namespace) -> int:
    p = require_project(args)
    graph = build_graph(p, verify=True)
    write_graph(p, graph)
    manifest_path = p.root / PACKAGE_MANIFEST
    if manifest_path.is_file() and not args.force:
        manifest = load_manifest(p)
    else:
        manifest = export_package(p, graph)
    if not manifest.get("steps"):
        raise AgError("nothing to package: no completed runs on the result path (record steps first, then `done`)")
    written: List[str] = []
    skipped: List[str] = []

    def put(rel: str, content: str, executable: bool = False) -> None:
        path = p.root / rel
        if path.exists() and not args.force:
            skipped.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)
        written.append(rel)

    put(PACKAGE_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    put("run.sh", make_run_sh(graph, manifest), executable=True)
    put("check.py", CHECK_PY)
    put("build_site.py", BUILD_SITE_PY)
    put(".github/workflows/reproduce.yml", REPRODUCE_YML)
    if not (p.root / "README.md").exists():
        put("README.md", make_readme(manifest))
    gi = p.root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    add = [line for line in ("site/", ".venv/", "analysis/.state/") if line not in existing.splitlines()]
    if add:
        gi.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(add) + "\n", encoding="utf-8")
        written.append(".gitignore")
    for rel in written:
        print(f"wrote   {rel}")
    for rel in skipped:
        print(f"kept    {rel}  (exists; --force overwrites)")
    print(f"package: {len(manifest['steps'])} steps, {len(manifest.get('inputs', []))} inputs, "
          f"{len(manifest.get('outputs', []))} checked outputs")
    print("next: read run.sh (it is the recorded commands in order), add dependency installation if needed,")
    print(f"      then `python3 {AG_DIRNAME}/whatran.py check`.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import tempfile
    p = require_project(args)
    manifest = load_manifest(p)
    results_dir = manifest.get("results_dir", "results")
    results = p.root / results_dir
    for req in ("run.sh", "check.py"):
        if not (p.root / req).is_file():
            raise AgError(f"{req} missing; run `whatran.py package` first")
    if not results.is_dir() or not any(results.iterdir()):
        raise AgError(f"{results_dir}/ has no reference outputs; the recorded outputs must be there before a check")
    tmp = Path(tempfile.mkdtemp(prefix="whatran-check-"))
    ref = tmp / "reference"
    shutil.move(str(results), str(ref))
    print(f"moved {results_dir}/ aside, rerunning from clean: $ bash run.sh")
    ok = False
    try:
        rc = subprocess.run(["bash", "run.sh"], cwd=p.root).returncode
        if rc != 0:
            raise AgError(f"run.sh exited {rc}; the reference outputs are back in place")
        rc, _, info = run_check_py(p, results_dir, str(ref))
        if rc != 0:
            raise AgError(f"check.py reported differences; the reference outputs are back in place, "
                          f"the regenerated ones are in {tmp / 'regenerated'}")
        ok = True
    finally:
        if results.exists():
            if ok:
                shutil.rmtree(results)
            else:
                shutil.move(str(results), str(tmp / "regenerated"))
        shutil.move(str(ref), str(results))
        if ok:
            shutil.rmtree(tmp, ignore_errors=True)
    covered = record_replay(p, manifest, PACKAGE_MANIFEST, source="local-check", check=info)
    write_graph(p, build_graph(p, verify=True))
    if (p.root / "build_site.py").is_file():
        subprocess.run([sys.executable, "build_site.py"], cwd=p.root)
    print(f"CHECK PASSED: outputs regenerated and matched; {len(covered)} run(s) now replay-verified "
          f"({', '.join(covered)}). Commit {results_dir}/, whatran.json, run.sh, check.py and the workflow.")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    p = require_project(args)
    graph = build_graph(p, verify=True)
    write_graph(p, graph)
    dest = (p.root / args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    viewer = plugin_viewer_path()
    if viewer is None:
        raise AgError("viewer index.html not found next to whatran.py or in the plugin's viewer/ directory")
    shutil.copyfile(viewer, dest / VIEWER_NAME)
    shutil.copyfile(p.graph_path, dest / GRAPH_NAME)
    rel_dest = dest.relative_to(p.root).as_posix()
    pages_path = rel_dest[len("docs/"):] if rel_dest.startswith("docs/") else rel_dest
    print(f"published viewer + graph.json to {rel_dest}/")
    print("GitHub Pages: Settings -> Pages -> Deploy from branch, folder `/docs` (or `/` root), then open")
    print(f"  https://<user>.github.io/<repo>/{pages_path}/")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import functools
    import http.server
    p = require_project(args)
    if not p.graph_path.is_file():
        write_graph(p, build_graph(p, verify=True))
    if not (p.ag_dir / VIEWER_NAME).is_file():
        viewer = plugin_viewer_path()
        if viewer:
            shutil.copyfile(viewer, p.ag_dir / VIEWER_NAME)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(p.ag_dir))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"serving {p.ag_dir} at http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    import webbrowser
    if not os.environ.get("WHATRAN_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    return cmd_serve(args)


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

MORE_HELP = """more commands (each has -h):
  artifact PATH         declare an input file with its origin
  update ID             amend a node's label, note, status or params
  log [N]               recent captured executions and whether they are linked
  status                what is recorded and what is still unlinked
  compile | verify      rebuild graph.json; verify also fails on changed/missing files
  export                the result path as a package draft or a mermaid flowchart
  replay-verified       record a passed check (local or a CI run URL) without rerunning
  publish | serve       copy the viewer for GitHub Pages / preview without opening a browser
"""


def _add_step_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--in", dest="inputs", action="append", metavar="PATH", help="input file (default: read from the command)")
    sp.add_argument("--out", dest="outputs", action="append", metavar="PATH", help="output file (default: files the command changed)")
    sp.add_argument("--code", action="append", metavar="PATH", help="script (default: scripts named in the command)")
    sp.add_argument("--param", action="append", metavar="KEY=VALUE", help="result-affecting parameter (default: flags in the command)")
    sp.add_argument("--params-json", metavar="JSON", help=argparse.SUPPRESS)
    sp.add_argument("--supersedes", metavar="RUN_ID", help="this run replaces an earlier one (kept, marked superseded)")
    sp.add_argument("--note")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="whatran.py",
        description="What ran? Record an analysis as a checkable graph while it happens, then package the result path so it reruns.",
        epilog=MORE_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="project root (default: walk up from cwd to find analysis/config.json)")
    ap.add_argument("--version", action="version", version=f"whatran.py {__version__} ({FORMAT})")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")
    hidden = argparse.SUPPRESS

    s = sub.add_parser("init", help="once per project: create analysis/ and start recording")
    s.add_argument("--name", help="project name (default: directory name)")
    s.add_argument("--description", help="the question being answered")
    s.add_argument("--no-copy", action="store_true", help=hidden)
    s.add_argument("--force", action="store_true", help=hidden)
    s.set_defaults(func=cmd_init)

    rr = sub.add_parser("record", help='after a meaningful step: record "what it did" (details read from the captured command)')
    rr.add_argument("label", help='what the step did, e.g. "Peak detection"')
    _add_step_flags(rr)
    rr.add_argument("--evidence", default="auto", metavar="x-NNNN|last|none",
                    help="which captured execution (default: the latest unlinked one that changed files)")
    rr.add_argument("--cmd", help=hidden)
    rr.add_argument("--status", default="completed", choices=RUN_STATUSES, help=hidden)
    rr.add_argument("--no-infer", action="store_true", help="do not read inputs/outputs/code/params from the command")
    rr.add_argument("--id", help=hidden)
    rr.add_argument("--json", action="store_true", help=hidden)
    rr.set_defaults(func=cmd_record)

    rc = sub.add_parser("claim", help='a finding: claim "conclusion" (default support: the last completed run)')
    rc.add_argument("text")
    rc.add_argument("--supports", action="append", metavar="ID|PATH", help="run id or output file it rests on (repeatable)")
    rc.add_argument("--status", default="tentative", choices=CLAIM_STATUSES, help=hidden)
    rc.add_argument("--note")
    rc.add_argument("--id", help=hidden)
    rc.add_argument("--json", action="store_true", help=hidden)
    rc.set_defaults(func=cmd_claim)

    j = sub.add_parser("reject", help="abandon a run or claim with a reason (it stays in the record)")
    j.add_argument("id")
    j.add_argument("--reason", required=True)
    j.set_defaults(func=cmd_reject)

    d = sub.add_parser("done", help="compile the graph, check recorded files, draft the package, say what is next")
    d.set_defaults(func=cmd_done)

    op = sub.add_parser("open", help="open the graph in the browser")
    op.add_argument("--port", type=int, default=8787)
    op.set_defaults(func=cmd_open)

    pk = sub.add_parser("package", help="generate whatran.json, run.sh, check.py, build_site.py and the CI workflow from the result path")
    pk.add_argument("--force", action="store_true", help="overwrite files that already exist")
    pk.set_defaults(func=cmd_package)

    ck = sub.add_parser("check", help="rerun from clean (results moved aside), compare, mark the packaged runs replay-verified")
    ck.set_defaults(func=cmd_check)

    x = sub.add_parser("run", help="run a command and capture its evidence yourself: run [--label L] -- CMD ...")
    x.add_argument("--label", help="also record the step with this label")
    _add_step_flags(x)
    x.add_argument("--quiet", action="store_true", help=hidden)
    x.add_argument("cmd", nargs=argparse.REMAINDER, help="-- COMMAND ARGS...")
    x.set_defaults(func=cmd_run)

    # ---- more ------------------------------------------------------------
    ra = sub.add_parser("artifact")
    ra.add_argument("path")
    ra.add_argument("--label")
    ra.add_argument("--origin", help="where it came from (URL, accession, instrument, person)")
    ra.add_argument("--note")
    ra.add_argument("--json", action="store_true")
    ra.set_defaults(func=cmd_artifact)

    u = sub.add_parser("update")
    u.add_argument("id")
    u.add_argument("--label")
    u.add_argument("--note")
    u.add_argument("--status")
    u.add_argument("--param", action="append", metavar="KEY=VALUE")
    u.set_defaults(func=cmd_update)

    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=15)
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(func=cmd_log)

    st = sub.add_parser("status")
    st.add_argument("--no-verify", action="store_true", help="skip re-hashing artifacts")
    st.set_defaults(func=cmd_status)

    c = sub.add_parser("compile")
    c.add_argument("--no-verify", action="store_true", help="skip re-hashing artifacts")
    c.add_argument("--strict", action="store_true", help="exit 1 on modified/missing artifacts or declared-only runs")
    c.set_defaults(func=cmd_compile)

    v = sub.add_parser("verify")
    v.add_argument("--strict", action="store_true", help="also fail on declared-only runs")
    v.set_defaults(func=cmd_verify)

    e = sub.add_parser("export")
    e.add_argument("--format", default="package", choices=["package", "mermaid"])
    e.add_argument("--all", action="store_true", help="mermaid: include off-path nodes")
    e.set_defaults(func=cmd_export)

    rv = sub.add_parser("replay-verified")
    rv.add_argument("--manifest", default=PACKAGE_MANIFEST)
    rv.add_argument("--reference", metavar="DIR", help="moved-aside reference outputs; runs `check.py <results_dir> DIR`")
    rv.add_argument("--ci", metavar="URL", help="a passing CI run of the package")
    rv.add_argument("--note")
    rv.set_defaults(func=cmd_replay_verified)

    pb = sub.add_parser("publish")
    pb.add_argument("--dest", default="docs/analysis")
    pb.set_defaults(func=cmd_publish)

    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(func=cmd_serve)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except AgError as exc:
        print(f"whatran: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
