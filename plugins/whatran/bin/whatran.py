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

__version__ = "0.3.0"
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
        "nudge": False,          # one line of context to the agent after each captured command
        "intent": True,          # keep the agent's own words before each command (Claude Code transcript)
        "intent_chars": 300,
        "report": True,          # keep the agent's last message of a turn as analysis/report.md
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
# the agent's own words: read from a Claude Code transcript (JSONL), never from
# the user's messages. Only assistant text is used.
# --------------------------------------------------------------------------- #

TRANSCRIPT_TAIL_BYTES = 6 << 20


def transcript_entries(path: Any, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> List[Dict[str, Any]]:
    """The last entries of a transcript that are user or assistant messages, in order."""
    try:
        tp = Path(path)
        size = tp.stat().st_size
        with open(tp, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()                      # drop the partial first line
            raw = fh.read().decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return []
    out: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if '"type":"assistant"' not in line and '"type":"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") in ("user", "assistant") and isinstance(d.get("message"), dict):
            out.append(d)
    return out


def _blocks(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def _is_prompt(entry: Dict[str, Any]) -> bool:
    """A user entry that is a message from the person (not a tool result)."""
    return entry.get("type") == "user" and not any(b.get("type") == "tool_result" for b in _blocks(entry))


def _text_of(entry: Dict[str, Any], before_tool_use: Optional[str] = None) -> str:
    parts: List[str] = []
    for b in _blocks(entry):
        if before_tool_use and b.get("type") == "tool_use" and b.get("id") == before_tool_use:
            break
        if b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]))
    return "\n".join(parts).strip()


def clip_tail(text: str, max_chars: int) -> str:
    """The last max_chars of text, cut at a sentence or line boundary when one is near."""
    text = re.sub(r"[ \t]+", " ", text.strip())
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    m = re.search(r"(?:(?<=[.!?。！？])\s+|\n)", tail[: max(1, len(tail) // 2)])
    if m:
        tail = tail[m.end():]
    return tail.strip()


def intent_before_tool_use(path: Any, tool_use_id: str, max_chars: int = 300) -> Optional[str]:
    """What the assistant said last before calling this tool, within the same turn."""
    if not path or not tool_use_id:
        return None
    entries = transcript_entries(path)
    idx = None
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if e.get("type") == "assistant" and any(b.get("type") == "tool_use" and b.get("id") == tool_use_id for b in _blocks(e)):
            idx = i
            break
    if idx is None:
        return None
    text = _text_of(entries[idx], before_tool_use=tool_use_id)
    msg_id = entries[idx].get("message", {}).get("id")
    j = idx - 1
    while not text and j >= 0:
        e = entries[j]
        if _is_prompt(e):
            break
        if e.get("type") == "assistant":
            same_message = msg_id and e.get("message", {}).get("id") == msg_id
            text = _text_of(e)
            if not text and not same_message and any(b.get("type") == "tool_use" for b in _blocks(e)):
                pass                                   # an earlier tool call without words: keep looking back
        j -= 1
    return clip_tail(text, max_chars) if text else None


def last_assistant_text(path: Any, max_chars: int = 20000) -> Optional[str]:
    """The assistant's final words of the last turn: text after its last tool call."""
    entries = [e for e in transcript_entries(path) if not e.get("isSidechain")]
    parts: List[str] = []
    for e in reversed(entries):
        if e.get("type") == "user":
            break
        if any(b.get("type") == "tool_use" for b in _blocks(e)):
            break
        t = _text_of(e)
        if t:
            parts.append(t)
    if not parts:
        return None
    text = "\n\n".join(reversed(parts)).strip()
    return text[-max_chars:] if len(text) > max_chars else text


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

    def make_exec_event(self, command: Optional[str], *, source: str, exit_code: Optional[int],
                        started: Optional[float], ended: Optional[float],
                        stdout: str = "", stderr: str = "", cwd: Optional[str] = None,
                        files_changed: Optional[List[Dict[str, Any]]] = None,
                        truncated: bool = False, session: Optional[str] = None,
                        tool_use_id: Optional[str] = None, interrupted: bool = False,
                        events: Optional[List[Dict[str, Any]]] = None,
                        description: Optional[str] = None, intent: Optional[str] = None) -> Dict[str, Any]:
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
        if description:
            ev["description"] = str(description).strip()[:200]
        if intent:
            ev["intent"] = intent
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
        elif kind == "retract":
            nodes.pop(ev.get("id"), None)      # hidden from the graph; the ledger keeps the event
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
               infer: bool = True, style: Optional[bool] = None, auto: Optional[str] = None) -> Dict[str, Any]:
    enforce_style(p, label=label, sentences={"note": note}, enabled=style)
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

    if supersedes is None and rel_out:
        # the same result file made again by a different attempt: the earlier attempt is superseded
        supersedes = detect_superseded_run(nodes, rel_out, rel_in)
        if supersedes:
            inferred["supersedes"] = supersedes

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
    if ev is not None:
        for key in ("description", "intent"):
            if ev.get(key):
                run[key] = ev[key]
    if auto:
        run["auto"] = {"label_from": auto}

    for n in new_nodes:
        p.append({"event": "node", "node": n})
    p.append({"event": "node", "node": run})
    if supersedes:
        p.append({"event": "status", "id": supersedes, "status": "superseded",
                  "reason": f"superseded by {rid}"})
    return {"id": rid, "evidence": evidence["level"], "exec_ids": evidence["exec_ids"],
            "inputs": input_ids, "outputs": output_ids, "status": status, "warnings": warnings,
            "inferred": inferred, "paths": {"inputs": rel_in, "outputs": rel_out, "code": rel_code},
            "params": params, "supersedes": supersedes}


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
                 node_id: Optional[str] = None, style: Optional[bool] = None) -> Dict[str, Any]:
    enforce_style(p, sentences={"claim": text, "note": note}, enabled=style)
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


def detect_superseded_run(nodes: "OrderedDict[str, Dict[str, Any]]", rel_out: List[str],
                          rel_in: List[str]) -> Optional[str]:
    """The completed run that last produced one of these output paths, if this run redoes it.

    A run that reads an earlier result and rewrites the same file is a new step, not a redo,
    so a candidate whose output is among this run's inputs is left alone.
    """
    art_by_path: Dict[str, Dict[str, Any]] = {}
    for n in nodes.values():
        if n.get("type") == "artifact" and n.get("path"):
            art_by_path[n["path"]] = n
    gen_by: Dict[str, str] = {}
    for n in nodes.values():
        if n.get("type") == "run" and n.get("status") == "completed":
            for o in n.get("outputs") or []:
                gen_by[o] = n["id"]
    ins = set(rel_in)
    counts: Dict[str, int] = {}
    for path in rel_out:
        a = art_by_path.get(path)
        rid = gen_by.get(a["id"]) if a else None
        if rid:
            counts[rid] = counts.get(rid, 0) + 1
    best: Optional[str] = None
    for rid, c in counts.items():
        old = nodes[rid]
        if any((nodes.get(o) or {}).get("path") in ins for o in old.get("outputs") or []):
            continue
        if best is None or c > counts[best]:
            best = rid
    return best


# --------------------------------------------------------------------------- #
# automatic steps: a captured execution that produced result files becomes a run
# without the agent doing anything; the label comes from the agent's own words
# when it has them (the command's description, else what it said before running
# it), else from the command.
# --------------------------------------------------------------------------- #

HOUSEKEEPING_COMMANDS = {
    "mkdir", "mv", "rm", "touch", "chmod", "chown", "ln", "cp", "rsync", "tar", "zip", "unzip",
    "gzip", "gunzip", "git", "pip", "pip3", "conda", "mamba", "npm", "npx", "uv", "brew", "apt", "apt-get",
}
AUTHORING_SUFFIXES = SCRIPT_SUFFIXES + (".md", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".lock")
AUTHORING_NAMES = {"requirements.txt", ".gitignore", "environment.yml", "Makefile", "LICENSE", "Dockerfile"}

_TRAILING = {"and", "then", "or", "the", "a", "an", "to", "with", "of", "for", "in", "on", "by", "from", "into"}
_FILLER = re.compile(
    r"^(?:now|next|then|first|second|finally|also|ok|okay|and|so|but|good|great|"
    r"let me|let's|lets|i'll|i will|i'm going to|i am going to|we'll|we will|going to|time to|"
    r"try to|need to|want to|will|to)\b[\s,:.\-]*", re.I)


def _command_names(command: str) -> List[str]:
    names: List[str] = []
    for seg in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command.strip()):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        toks = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if toks:
            names.append(os.path.basename(toks[0]))
    return names


def result_files(ev: Dict[str, Any]) -> List[str]:
    """Changed files that look like results rather than code or configuration."""
    out: List[str] = []
    for f in ev.get("files_changed") or []:
        path = f.get("path") or ""
        if os.path.basename(path) in AUTHORING_NAMES or path.lower().endswith(AUTHORING_SUFFIXES):
            continue
        out.append(path)
    return out


def is_step_candidate(ev: Dict[str, Any]) -> bool:
    """An execution that could be a scientific step: it succeeded and produced result files."""
    if ev.get("exit_code") not in (0, None) or ev.get("interrupted"):
        return False
    cmd = ev.get("command") or ""
    names = _command_names(cmd)
    if not names or all(n in HOUSEKEEPING_COMMANDS for n in names):
        return False
    return bool(result_files(ev))


def _verb_form(word: str, verbs: set) -> Optional[str]:
    """running -> run, detects -> detect, fitted -> fit; None when no approved verb fits."""
    w = _norm(word)
    if not w:
        return None
    if w in verbs:
        return w
    cands: List[str] = []
    if w.endswith("ing") and len(w) > 4:
        stem = w[:-3]
        cands += [stem, stem + "e"]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            cands.append(stem[:-1])
    if w.endswith("es"):
        cands.append(w[:-2])
    if w.endswith("s"):
        cands.append(w[:-1])
    if w.endswith("ed") and len(w) > 3:
        cands += [w[:-2], w[:-1]]
        if len(w) > 4 and w[-3] == w[-4]:
            cands.append(w[:-3])
    for c in cands:
        if len(c) > 1 and c in verbs:
            return c
    return None


def label_from_words(text: str, p: Optional[Project], max_words: int = 8) -> Optional[str]:
    """A verb-first label from the agent's words: the last sentence that starts with an action."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"[`*_#>\[\]()\"]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    verbs = approved_verbs(p)
    sentences = [x for x in re.split(r"(?<=[.!?:;。！？：；])\s*", t) if x.strip()]
    for sent in reversed(sentences):
        s2 = sent.strip()
        for _ in range(4):
            s3 = _FILLER.sub("", s2).strip()
            if s3 == s2:
                break
            s2 = s3
        words = s2.split()
        if not words:
            continue
        v = _verb_form(words[0], verbs)
        if not v:
            continue
        rest = words[1:max_words]
        while rest and _norm(rest[-1]) in _TRAILING:
            rest.pop()
        return " ".join([v.capitalize()] + rest).rstrip(".,:;!。，：；！")
    return None


def label_from_command(p: Project, ev: Dict[str, Any]) -> str:
    guess = infer_from_exec(p, ev)
    if guess["code"]:
        return f"Run {os.path.basename(guess['code'][0])}"
    names = [n for n in _command_names(ev.get("command") or "") if n not in HOUSEKEEPING_COMMANDS]
    return f"Run {names[0]}" if names else "Run command"


def auto_label(p: Project, ev: Dict[str, Any]) -> Tuple[str, str]:
    """(label, where it came from): description | intent | command."""
    desc = (ev.get("description") or "").strip()
    if desc:
        label = label_from_words(desc, p) or " ".join(desc.split()[:8]).rstrip(".")
        return label[:1].upper() + label[1:], "description"
    intent = (ev.get("intent") or "").strip()
    if intent:
        label = label_from_words(intent, p)
        if label:
            return label, "intent"
    return label_from_command(p, ev), "command"


def identical_rerun_of(nodes: "OrderedDict[str, Dict[str, Any]]", ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A completed run with the same command whose outputs this execution reproduced byte for byte."""
    cmd = (ev.get("command") or "").strip()
    files = ev.get("files_changed") or []
    if not cmd or not files:
        return None
    art_by_path = {n["path"]: n for n in nodes.values() if n.get("type") == "artifact" and n.get("path")}
    for f in files:
        a = art_by_path.get(f.get("path"))
        if a is None or not f.get("sha256") or a.get("sha256") != f["sha256"]:
            return None
    for n in reversed(list(nodes.values())):
        if n.get("type") == "run" and n.get("status") == "completed" and (n.get("command") or "").strip() == cmd:
            return n
    return None


def reproduces_known_files(nodes: "OrderedDict[str, Dict[str, Any]]", ev: Dict[str, Any]) -> bool:
    """Every file it changed already exists in the record with the same content (a replay, not a step)."""
    files = ev.get("files_changed") or []
    if not files:
        return False
    art_by_path = {n["path"]: n for n in nodes.values() if n.get("type") == "artifact" and n.get("path")}
    for f in files:
        a = art_by_path.get(f.get("path"))
        if a is None or not f.get("sha256") or a.get("sha256") != f["sha256"]:
            return False
    return True


def promote_executions(p: Project) -> List[Dict[str, Any]]:
    """Make a run out of every unlinked execution that produced result files. Idempotent.

    An execution that repeats a recorded run exactly (same command, same output bytes) is
    attached to that run. One that only regenerates files already in the record (a rerun of
    the whole pipeline, for example) is left alone.
    """
    events = p.events()
    nodes = fold_nodes(events)
    linked = linked_exec_ids(nodes)
    out: List[Dict[str, Any]] = []
    for ev in exec_events(events):
        if ev["id"] in linked or not is_step_candidate(ev):
            continue
        twin = identical_rerun_of(nodes, ev)
        if twin is not None:
            evidence = dict(twin.get("evidence") or {})
            evidence["exec_ids"] = list(evidence.get("exec_ids") or []) + [ev["id"]]
            p.append({"event": "update", "id": twin["id"], "fields": {"evidence": evidence}})
            twin["evidence"] = evidence
            linked[ev["id"]] = twin["id"]
            out.append({"exec": ev["id"], "run": twin["id"], "repeat": True})
            continue
        if reproduces_known_files(nodes, ev):
            continue
        label, source = auto_label(p, ev)
        res = record_run(p, label, inputs=[], outputs=[], code=[], params={}, command=None,
                         evidence_mode=ev["id"], note=None, status="completed", supersedes=None,
                         exec_event=ev, infer=True, style=False, auto=source)
        out.append({"exec": ev["id"], "run": res["id"], "label": label, "label_from": source,
                    "supersedes": res.get("supersedes")})
        events = p.events()
        nodes = fold_nodes(events)
        linked = linked_exec_ids(nodes)
    return out


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
        "retracted": sum(1 for e in events if e.get("event") == "retract"),
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
    claims = [nodes[c]["label"] for c in graph["main_path"] if nodes[c]["type"] == "claim"]   # ledger order
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


VIEWER_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analysis Graph</title>
<meta name="description" content="Interactive, result-centric view of an Analysis Graph (analysis-graph/0.1): artifacts, runs, claims, and the execution evidence behind them.">
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1c1917; --muted: #6b6560; --line: #e4e1dd; --soft: #f1efeb;
  --artifact: #2563eb; --artifact-bg: #eaf2ff; --run: #6d28d9; --run-bg: #f1ecfd; --claim: #047857; --claim-bg: #e6f7f0;
  --warn: #c2410c; --bad: #b91c1c; --ok: #15803d; --accent: #0f766e; --edge: #b8b2ab; --lit: #0f766e;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15141a; --panel: #1d1c23; --ink: #ecebe8; --muted: #9d9892; --line: #312f38; --soft: #26242d;
    --artifact: #7aa7ff; --artifact-bg: #1b2440; --run: #b79cff; --run-bg: #2a2140; --claim: #5fd3a3; --claim-bg: #16302a;
    --warn: #f59e0b; --bad: #f87171; --ok: #4ade80; --accent: #5eead4; --edge: #5c5861; --lit: #5eead4;
  }
}
:root[data-theme="dark"] {
  --bg: #15141a; --panel: #1d1c23; --ink: #ecebe8; --muted: #9d9892; --line: #312f38; --soft: #26242d;
  --artifact: #7aa7ff; --artifact-bg: #1b2440; --run: #b79cff; --run-bg: #2a2140; --claim: #5fd3a3; --claim-bg: #16302a;
  --warn: #f59e0b; --bad: #f87171; --ok: #4ade80; --accent: #5eead4; --edge: #5c5861; --lit: #5eead4;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; display: flex; flex-direction: column; }
header { display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--panel); }
header h1 { font-size: 16px; margin: 0; font-weight: 650; letter-spacing: -0.01em; }
header .sub { color: var(--muted); font-size: 12px; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: var(--soft); color: var(--ink); white-space: nowrap; }
.chip.warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
.chip.bad { background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }
.chip.ok { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
header .grow { flex: 1; }
header input[type=search] { font: inherit; padding: 5px 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--ink); min-width: 160px; }
header label.toggle { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); cursor: pointer; white-space: nowrap; }
button { font: inherit; font-size: 13px; padding: 5px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
main { flex: 1; display: flex; min-height: 0; position: relative; }
#canvas { flex: 1; position: relative; overflow: hidden; }
#canvas svg { width: 100%; height: 100%; display: block; cursor: grab; touch-action: none; }
#canvas svg.dragging { cursor: grabbing; }
#empty { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; padding: 24px; text-align: center; color: var(--muted); }
#empty.show { display: flex; }
#empty code { font-family: var(--mono); background: var(--soft); padding: 1px 5px; border-radius: 4px; color: var(--ink); }
#legend { position: absolute; left: 12px; bottom: 12px; background: color-mix(in srgb, var(--panel) 88%, transparent); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; font-size: 11px; color: var(--muted); display: grid; grid-template-columns: auto auto; gap: 3px 10px; backdrop-filter: blur(4px); }
#legend .sw { display: inline-block; width: 10px; height: 10px; border-radius: 3px; vertical-align: -1px; margin-right: 5px; border: 1.5px solid currentColor; }
#legend .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; vertical-align: -1px; margin-right: 5px; border: 1.5px solid currentColor; }
#legend .dot.fill { background: currentColor; }
aside#panel { width: 400px; max-width: 100%; border-left: 1px solid var(--line); background: var(--panel); overflow: auto; padding: 16px 18px 28px; display: none; }
aside#panel.show { display: block; }
@media (max-width: 820px) { aside#panel { position: absolute; top: 0; right: 0; bottom: 0; width: 100%; box-shadow: -8px 0 24px rgba(0,0,0,.15); } }
.ptype { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; font-weight: 650; }
.ptype.artifact { color: var(--artifact); } .ptype.run { color: var(--run); } .ptype.claim { color: var(--claim); }
aside h2 { font-size: 16px; margin: 4px 0 2px; line-height: 1.3; }
aside .id { font-family: var(--mono); font-size: 11px; color: var(--muted); word-break: break-all; }
aside h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 18px 0 6px; font-weight: 650; }
aside pre { font-family: var(--mono); font-size: 12px; background: var(--soft); padding: 8px 10px; border-radius: 8px; white-space: pre-wrap; word-break: break-word; margin: 0; max-height: 240px; overflow: auto; }
aside table { border-collapse: collapse; width: 100%; font-size: 13px; }
aside td { padding: 3px 6px 3px 0; vertical-align: top; border-bottom: 1px solid var(--line); }
aside td:first-child { color: var(--muted); white-space: nowrap; width: 34%; }
aside td code, aside li code { font-family: var(--mono); font-size: 12px; word-break: break-all; }
aside ul { margin: 0; padding-left: 18px; }
aside li { margin: 2px 0; }
aside a.ref { color: var(--accent); text-decoration: none; border-bottom: 1px dotted currentColor; cursor: pointer; }
aside .close { position: absolute; right: 14px; top: 10px; }
aside .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px; }
aside details summary { cursor: pointer; color: var(--muted); font-size: 13px; }
.hash { font-family: var(--mono); font-size: 11px; color: var(--muted); }
footer { padding: 7px 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; background: var(--panel); display: flex; gap: 14px; flex-wrap: wrap; }
footer b { color: var(--ink); font-weight: 600; }
/* graph */
.node { cursor: pointer; outline: none; }
.node:focus-visible rect.box { stroke: var(--warn); stroke-width: 3; }
.node rect.box { fill: var(--panel); stroke-width: 1.5; }
.node.artifact rect.box { stroke: var(--artifact); fill: var(--artifact-bg); }
.node.run rect.box { stroke: var(--run); fill: var(--run-bg); }
.node.claim rect.box { stroke: var(--claim); fill: var(--claim-bg); rx: 26; }
.node.rejected rect.box, .node.superseded rect.box { stroke-dasharray: 5 4; fill: var(--panel); }
.node.failed rect.box { stroke: var(--bad); }
.node text { pointer-events: none; }
.node .tag { font-size: 10px; letter-spacing: .08em; font-weight: 700; }
.node.artifact .tag { fill: var(--artifact); } .node.run .tag { fill: var(--run); } .node.claim .tag { fill: var(--claim); }
.node .label { font-size: 13px; font-weight: 600; fill: var(--ink); }
.node .subt { font-size: 11px; fill: var(--muted); font-family: var(--mono); }
.node.sel rect.box { stroke-width: 3; }
.node.dim, .edge.dim { opacity: .28; }
.node.off rect.box { stroke-dasharray: 2 3; }
.node.off { opacity: .55; }
.node.hit rect.box { stroke: var(--warn); stroke-width: 3; }
.badge circle { stroke-width: 1.5; }
.badge.observed circle { fill: var(--run); stroke: var(--run); }
.badge.declared circle { fill: var(--panel); stroke: var(--run); }
.badge.intact circle { fill: var(--ok); stroke: var(--ok); }
.badge.modified circle { fill: var(--warn); stroke: var(--warn); }
.badge.missing circle { fill: var(--bad); stroke: var(--bad); }
.badge.unchecked circle { fill: var(--panel); stroke: var(--muted); }
.badge.tentative circle { fill: var(--panel); stroke: var(--claim); }
.badge.supported circle { fill: var(--claim); stroke: var(--claim); }
.badge text { font-size: 10px; font-weight: 700; fill: var(--panel); text-anchor: middle; }
.badge.rejected text, .badge.failed text { fill: var(--ink); }
.badge.rejected circle { fill: var(--soft); stroke: var(--muted); }
.badge.failed circle { fill: var(--panel); stroke: var(--bad); }
.badge.failed text { fill: var(--bad); }
.edge path { fill: none; stroke: var(--edge); stroke-width: 1.6; marker-end: url(#arrow); }
.edge.supports path { stroke: var(--claim); stroke-dasharray: 6 4; marker-end: url(#arrow-claim); }
.edge.supersedes path { stroke-dasharray: 2 4; opacity: .7; marker-end: none; }
.edge.lit path { stroke: var(--lit); stroke-width: 2.6; marker-end: url(#arrow-lit); }
/* log modal */
#log { position: absolute; inset: 0; background: color-mix(in srgb, var(--bg) 70%, transparent); display: none; align-items: flex-start; justify-content: center; padding: 24px; overflow: auto; z-index: 5; }
#log.show { display: flex; }
#log .box { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; width: min(1100px, 100%); padding: 16px 18px; box-shadow: 0 12px 40px rgba(0,0,0,.2); }
#log table { border-collapse: collapse; width: 100%; font-size: 12px; }
#log th, #log td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
#log td.cmd { font-family: var(--mono); word-break: break-all; }
#log tr.unlinked td { color: var(--muted); }
#log .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
#log .toolbar h2 { margin: 0; font-size: 15px; flex: 1; }
</style>
</head>
<body>
<header>
  <div>
    <h1 id="title">Analysis Graph</h1>
    <div class="sub" id="subtitle">loading…</div>
  </div>
  <div class="chips" id="chips"></div>
  <div class="grow"></div>
  <input type="search" id="search" placeholder="Search nodes" aria-label="Search nodes">
  <label class="toggle"><input type="checkbox" id="showAll"> <span id="showAllLabel">Show explored branches</span></label>
  <button id="btnLog" type="button">Execution log</button>
  <button id="btnFit" type="button" title="Fit (f)">Fit</button>
</header>
<main>
  <div id="canvas">
    <svg id="svg" role="img" aria-label="Analysis graph">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--edge)"/></marker>
        <marker id="arrow-claim" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--claim)"/></marker>
        <marker id="arrow-lit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--lit)"/></marker>
      </defs>
      <g id="viewport"><g id="edges"></g><g id="nodes"></g></g>
    </svg>
    <div id="empty"></div>
    <div id="legend">
      <span><i class="sw" style="color:var(--artifact)"></i>artifact</span><span><i class="dot fill" style="color:var(--run)"></i>observed run</span>
      <span><i class="sw" style="color:var(--run)"></i>run</span><span><i class="dot" style="color:var(--run)"></i>declared only</span>
      <span><i class="sw" style="color:var(--claim)"></i>claim</span><span><i class="dot fill" style="color:var(--warn)"></i>artifact modified</span>
      <span style="opacity:.6"><i class="sw" style="color:var(--muted);border-style:dashed"></i>rejected / off path</span><span><i class="dot fill" style="color:var(--bad)"></i>artifact missing</span>
    </div>
  </div>
  <aside id="panel" aria-live="polite"></aside>
  <div id="log" role="dialog" aria-modal="true" aria-label="Execution log"><div class="box"></div></div>
</main>
<footer id="footer"></footer>

<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<script>
(function () {
  'use strict';
  const NODE_W = 236, NODE_H = 60;
  const qs = new URLSearchParams(location.search);
  const GRAPH_URL = qs.get('graph') || 'graph.json';
  const state = { graph: null, showAll: false, selected: null, query: '', pos: {}, vis: new Set(), deps: {}, k: 1, tx: 0, ty: 0 };
  const $ = (id) => document.getElementById(id);
  const svg = $('svg'), viewport = $('viewport'), gEdges = $('edges'), gNodes = $('nodes');
  const SVGNS = 'http://www.w3.org/2000/svg';

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const trunc = (s, n) => { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const short = (h) => h ? String(h).slice(0, 12) : '—';
  const fmtParams = (p) => Object.entries(p || {}).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
  const fmtBytes = (n) => n == null ? '—' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB';
  const byId = () => { const m = {}; (state.graph.nodes || []).forEach((n) => { m[n.id] = n; }); return m; };

  // ---------------------------------------------------------------- load
  async function load() {
    try {
      const res = await fetch(GRAPH_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
      state.graph = await res.json();
    } catch (err) {
      $('subtitle').textContent = 'no graph loaded';
      const e = $('empty');
      e.innerHTML = `<div><p><b>Could not load <code>${esc(GRAPH_URL)}</code></b> (${esc(err.message)}).</p>
        <p>Build it with <code>python3 analysis/whatran.py done</code>. Browsers block <code>file://</code> fetches, so preview with <code>python3 analysis/whatran.py open</code> or publish to GitHub Pages.</p></div>`;
      e.classList.add('show');
      return;
    }
    const g = state.graph;
    g.nodes = g.nodes || []; g.edges = g.edges || []; g.main_path = g.main_path || g.nodes.map((n) => n.id);
    state.deps = {};
    g.edges.forEach((e) => { if (e.kind !== 'supersedes') (state.deps[e.to] = state.deps[e.to] || []).push(e.from); });
    const name = (g.project && g.project.name) || 'Analysis Graph';
    document.title = name + ' · Analysis Graph';
    $('title').textContent = name;
    $('subtitle').textContent = `Analysis Graph · ${g.format || ''} · generated ${g.generated_at || '?'}` + ((g.project && g.project.description) ? ' · ' + g.project.description : '');
    renderChips(); renderFooter();
    const hidden = g.nodes.filter((n) => !g.main_path.includes(n.id)).length;
    $('showAllLabel').textContent = hidden ? `Show explored branches (${hidden} hidden)` : 'Show explored branches (none hidden)';
    if (qs.get('all') === '1') { state.showAll = true; $('showAll').checked = true; }
    render(); fit();
    const sel = qs.get('node'); if (sel && byId()[sel]) select(sel);
  }

  function renderChips() {
    const s = state.graph.summary || {}; const r = s.runs || {}, a = s.artifacts || {}, c = s.claims || {};
    const chips = [];
    chips.push(`<span class="chip">${r.total || 0} runs</span>`);
    if (r.declared) chips.push(`<span class="chip warn">${r.declared} declared only</span>`);
    chips.push(`<span class="chip">${a.total || 0} artifacts</span>`);
    if (a.modified) chips.push(`<span class="chip warn">${a.modified} modified</span>`);
    if (a.missing) chips.push(`<span class="chip bad">${a.missing} missing</span>`);
    chips.push(`<span class="chip">${c.total || 0} claims</span>`);
    if (r.rejected) chips.push(`<span class="chip">${r.rejected} rejected</span>`);
    $('chips').innerHTML = chips.join('');
  }

  function renderFooter() {
    const s = state.graph.summary || {}; const r = s.runs || {}, a = s.artifacts || {}, x = s.executions || {};
    const parts = [];
    parts.push(`Provenance: <b>${r.observed || 0} of ${r.total || 0}</b> runs have observed execution evidence`);
    parts.push(`<b>${a.intact || 0} of ${a.total || 0}</b> artifacts intact since recording`);
    parts.push(`<b>${r.replay_verified || 0}</b> runs replay-verified`);
    parts.push(`${x.total || 0} executions recorded (${(x.total || 0) - (x.linked || 0)} not linked to any node)`);
    $('footer').innerHTML = parts.map((p) => `<span>${p}</span>`).join('');
  }

  // ---------------------------------------------------------------- layout
  function visibleIds() {
    const g = state.graph; const main = new Set(g.main_path);
    return new Set(g.nodes.filter((n) => state.showAll || main.has(n.id)).map((n) => n.id));
  }

  function layout(ids) {
    const g = state.graph; const pos = {};
    const edges = g.edges.filter((e) => ids.has(e.from) && ids.has(e.to) && e.kind !== 'supersedes');
    if (window.dagre) {
      const dg = new dagre.graphlib.Graph();
      dg.setGraph({ rankdir: 'TB', nodesep: 34, ranksep: 46, marginx: 24, marginy: 24 });
      dg.setDefaultEdgeLabel(() => ({}));
      ids.forEach((id) => dg.setNode(id, { width: NODE_W, height: NODE_H }));
      edges.forEach((e) => dg.setEdge(e.from, e.to));
      dagre.layout(dg);
      ids.forEach((id) => { const n = dg.node(id); pos[id] = { x: n.x - NODE_W / 2, y: n.y - NODE_H / 2 }; });
      return pos;
    }
    // fallback: longest-path layering, barycentre ordering
    const preds = {}, succs = {}; ids.forEach((id) => { preds[id] = []; succs[id] = []; });
    edges.forEach((e) => { preds[e.to].push(e.from); succs[e.from].push(e.to); });
    const rank = {}; const order = Array.from(ids);
    const visit = (id, seen) => { if (rank[id] != null) return rank[id]; if (seen.has(id)) return 0; seen.add(id); rank[id] = preds[id].length ? Math.max(...preds[id].map((p) => visit(p, seen))) + 1 : 0; return rank[id]; };
    order.forEach((id) => visit(id, new Set()));
    const rows = {}; order.forEach((id) => (rows[rank[id]] = rows[rank[id]] || []).push(id));
    const xOf = {};
    Object.keys(rows).map(Number).sort((a, b) => a - b).forEach((r) => {
      const row = rows[r];
      row.sort((a, b) => { const ba = preds[a].length ? preds[a].reduce((s, p) => s + (xOf[p] || 0), 0) / preds[a].length : 0; const bb = preds[b].length ? preds[b].reduce((s, p) => s + (xOf[p] || 0), 0) / preds[b].length : 0; return ba - bb; });
      row.forEach((id, i) => { xOf[id] = i * (NODE_W + 34); pos[id] = { x: xOf[id] + 24, y: r * (NODE_H + 46) + 24 }; });
    });
    return pos;
  }

  // ---------------------------------------------------------------- render
  function el(tag, attrs, text) { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; }

  function subtitleOf(n) {
    if (n.type === 'run') { const p = fmtParams(n.params); return p || (n.command ? trunc(n.command, 40) : ''); }
    if (n.type === 'artifact') return (n.path || '') + (n.version ? '  v' + n.version : '');
    return n.status || '';
  }
  function badgeOf(n) {
    if (n.type === 'run') {
      if (n.status === 'rejected') return { cls: 'rejected', text: '⊘', title: 'rejected: ' + (n.status_reason || '') };
      if (n.status === 'superseded') return { cls: 'rejected', text: '↻', title: 'superseded' };
      if (n.status === 'failed') return { cls: 'failed', text: '!', title: 'failed' };
      const lvl = (n.evidence && n.evidence.level) || 'declared';
      return { cls: lvl, text: '', title: lvl === 'observed' ? 'execution observed' : 'declared by the agent, no execution evidence' };
    }
    if (n.type === 'artifact') return { cls: n.integrity || 'unchecked', text: '', title: 'integrity: ' + (n.integrity || 'unchecked') };
    if (n.status === 'rejected') return { cls: 'rejected', text: '⊘', title: 'rejected' };
    return { cls: n.status || 'tentative', text: '', title: 'claim status: ' + (n.status || 'tentative') };
  }

  function render() {
    const g = state.graph; const ids = visibleIds(); state.vis = ids; state.pos = layout(ids);
    gEdges.innerHTML = ''; gNodes.innerHTML = '';
    const main = new Set(g.main_path); const q = state.query.trim().toLowerCase();
    g.edges.forEach((e) => {
      if (!ids.has(e.from) || !ids.has(e.to)) return;
      const a = state.pos[e.from], b = state.pos[e.to];
      const sx = a.x + NODE_W / 2, sy = a.y + NODE_H, tx = b.x + NODE_W / 2, ty = b.y;
      const my = (sy + ty) / 2;
      const d = e.kind === 'supersedes'
        ? `M${a.x + NODE_W},${a.y + NODE_H / 2} C${a.x + NODE_W + 40},${a.y + NODE_H / 2} ${b.x + NODE_W + 40},${b.y + NODE_H / 2} ${b.x + NODE_W},${b.y + NODE_H / 2}`
        : `M${sx},${sy} C${sx},${my} ${tx},${my} ${tx},${ty}`;
      const ge = el('g', { class: 'edge ' + e.kind, 'data-from': e.from, 'data-to': e.to });
      const path = el('path', { d });
      path.appendChild(el('title', {}, `${e.from} —${e.kind}→ ${e.to}`));
      ge.appendChild(path); gEdges.appendChild(ge);
    });
    g.nodes.forEach((n) => {
      if (!ids.has(n.id)) return;
      const p = state.pos[n.id];
      const cls = ['node', n.type, n.status || ''];
      if (!main.has(n.id)) cls.push('off');
      if (q && (n.label || '').toLowerCase().includes(q) || q && (n.path || '').toLowerCase().includes(q) || q && n.id.toLowerCase().includes(q)) cls.push('hit');
      const gn = el('g', { class: cls.join(' ').trim(), transform: `translate(${p.x},${p.y})`, 'data-id': n.id, tabindex: 0, role: 'button' });
      gn.appendChild(el('rect', { class: 'box', width: NODE_W, height: NODE_H, rx: 10 }));
      gn.appendChild(el('text', { class: 'tag', x: 14, y: 17 }, n.type.toUpperCase() + (n.type === 'artifact' && n.role ? ' · ' + n.role.toUpperCase() : '')));
      gn.appendChild(el('text', { class: 'label', x: 14, y: 36 }, trunc(n.label || n.id, 30)));
      gn.appendChild(el('text', { class: 'subt', x: 14, y: 52 }, trunc(subtitleOf(n), 36)));
      const b = badgeOf(n);
      const gb = el('g', { class: 'badge ' + b.cls, transform: `translate(${NODE_W - 16},16)` });
      gb.appendChild(el('circle', { r: 6 }));
      if (b.text) gb.appendChild(el('text', { y: 3.5 }, b.text));
      gb.appendChild(el('title', {}, b.title));
      gn.appendChild(gb);
      gn.appendChild(el('title', {}, (n.label || n.id) + '\n' + n.id));
      gn.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); select(n.id); } });
      gNodes.appendChild(gn);
    });
    applyHighlight();
  }

  function ancestors(id) {
    const out = new Set(); const stack = [id];
    while (stack.length) { const n = stack.pop(); if (out.has(n)) continue; out.add(n); (state.deps[n] || []).forEach((d) => stack.push(d)); }
    return out;
  }
  function applyHighlight() {
    const sel = state.selected;
    const lit = sel ? ancestors(sel) : null;
    gNodes.querySelectorAll('.node').forEach((n) => {
      const id = n.getAttribute('data-id');
      n.classList.toggle('sel', id === sel);
      n.classList.toggle('dim', !!lit && !lit.has(id));
    });
    gEdges.querySelectorAll('.edge').forEach((e) => {
      const f = e.getAttribute('data-from'), t = e.getAttribute('data-to');
      const on = !!lit && lit.has(f) && lit.has(t) && !e.classList.contains('supersedes');
      e.classList.toggle('lit', on);
      e.classList.toggle('dim', !!lit && !on);
    });
  }

  // ---------------------------------------------------------------- panel
  function ref(id) { const n = byId()[id]; return n ? `<a class="ref" data-ref="${esc(id)}">${esc(n.label || id)}</a> <span class="hash">${esc(id)}</span>` : `<code>${esc(id)}</code>`; }
  function artifactLine(id) { const n = byId()[id]; if (!n) return `<li><code>${esc(id)}</code></li>`; return `<li>${ref(id)}<br><code>${esc(n.path || '')}</code> <span class="hash">${short(n.sha256)}</span>${n.integrity && n.integrity !== 'intact' ? ` <span class="chip ${n.integrity === 'missing' ? 'bad' : 'warn'}">${esc(n.integrity)}</span>` : ''}</li>`; }
  function row(k, v) { return v == null || v === '' ? '' : `<tr><td>${esc(k)}</td><td>${v}</td></tr>`; }

  function select(id) {
    state.selected = id; applyHighlight();
    const n = byId()[id]; if (!n) return;
    const panel = $('panel'); const parts = [];
    parts.push(`<button class="close" type="button" id="btnClose" aria-label="Close">✕</button>`);
    parts.push(`<div class="ptype ${n.type}">${esc(n.type)}${n.role ? ' · ' + esc(n.role) : ''}</div>`);
    parts.push(`<h2>${esc(n.label || n.id)}</h2><div class="id">${esc(n.id)}</div>`);
    const chips = [];
    if (n.status) chips.push(`<span class="chip ${n.status === 'rejected' || n.status === 'failed' ? 'warn' : ''}">${esc(n.status)}</span>`);
    if (n.type === 'run') { const lvl = (n.evidence && n.evidence.level) || 'declared'; chips.push(`<span class="chip ${lvl === 'observed' ? 'ok' : 'warn'}">${lvl === 'observed' ? 'execution observed' : 'declared only'}</span>`); chips.push(`<span class="chip ${n.verified_by_replay ? 'ok' : ''}">${n.verified_by_replay ? 'replay verified' : 'not replay-verified'}</span>`); }
    if (n.type === 'run' && n.auto) chips.push(`<span class="chip">auto-recorded · label from the ${esc(n.auto.label_from || 'command')}</span>`);
    if (n.type === 'artifact') chips.push(`<span class="chip ${n.integrity === 'intact' ? 'ok' : n.integrity === 'missing' ? 'bad' : n.integrity === 'modified' ? 'warn' : ''}">${esc(n.integrity || 'unchecked')}</span>`);
    if (!state.graph.main_path.includes(n.id)) chips.push(`<span class="chip">not on the result path</span>`);
    parts.push(`<div class="chips" style="margin-top:8px">${chips.join('')}</div>`);
    if (n.status_reason) parts.push(`<p style="color:var(--muted);margin:8px 0 0">${esc(n.status_reason)}</p>`);
    if (n.note) parts.push(`<p style="margin:10px 0 0">${esc(n.note)}</p>`);

    if (n.type === 'run') {
      if (n.description || n.intent) parts.push(`<h3>What the agent said</h3>${n.description ? `<p style="margin:6px 0 0">${esc(n.description)}</p>` : ''}${n.intent ? `<p style="margin:6px 0 0;color:var(--muted)">${esc(n.intent)}</p>` : ''}`);
      if (n.command) parts.push(`<h3>Command</h3><pre>${esc(n.command)}</pre>`);
      const params = Object.entries(n.params || {});
      if (params.length) parts.push(`<h3>Parameters</h3><table>${params.map(([k, v]) => row(k, `<code>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</code>`)).join('')}</table>`);
      if ((n.code || []).length) parts.push(`<h3>Code</h3><ul>${n.code.map((c) => `<li><code>${esc(c.path)}</code> <span class="hash">${short(c.sha256)}</span></li>`).join('')}</ul>`);
      parts.push(`<h3>Inputs (${(n.inputs || []).length})</h3>${(n.inputs || []).length ? `<ul>${n.inputs.map(artifactLine).join('')}</ul>` : '<span class="hash">none declared</span>'}`);
      parts.push(`<h3>Outputs (${(n.outputs || []).length})</h3>${(n.outputs || []).length ? `<ul>${n.outputs.map(artifactLine).join('')}</ul>` : '<span class="hash">none declared</span>'}`);
      const ev = n.evidence || {};
      let evRows = row('level', esc(ev.level || 'declared'));
      if (ev.level === 'observed') {
        evRows += row('execution', (ev.exec_ids || []).map((x) => `<code>${esc(x)}</code>`).join(', '));
        evRows += row('source', esc(ev.source));
        evRows += row('exit code', ev.exit_code == null ? '<span class="hash">unknown</span>' : `<code>${esc(ev.exit_code)}</code>`);
        evRows += row('started', esc(ev.started));
        evRows += row('duration', ev.duration_ms == null ? null : (ev.duration_ms / 1000).toFixed(1) + ' s');
        evRows += row('outputs observed', `${(ev.outputs_observed || []).length} of ${(n.outputs || []).length} declared outputs were seen changing with matching hashes`);
      } else {
        evRows += row('meaning', 'the agent declared this step; no execution was captured. Treat command, parameters and outputs as unverified.');
      }
      parts.push(`<h3>Evidence</h3><table>${evRows}</table>`);
      if (ev.stdout_head || ev.stderr_head) parts.push(`<details style="margin-top:8px"><summary>output head</summary><pre>${esc(ev.stdout_head || '')}${ev.stderr_head ? '\n--- stderr ---\n' + esc(ev.stderr_head) : ''}</pre></details>`);
      const envRows = row('python', esc((n.environment || {}).python)) + row('platform', esc((n.environment || {}).platform)) + row('git', n.git ? `<code>${esc(short(n.git.commit))}</code>${n.git.dirty ? ' <span class="chip warn">dirty tree</span>' : ''}` : null) + row('recorded', esc(n.recorded_at) + (n.recorded_by ? ' by ' + esc(n.recorded_by) : ''));
      if (n.supersedes) envRows += row('supersedes', ref(n.supersedes));
      parts.push(`<h3>Environment</h3><table>${envRows}</table>`);
    } else if (n.type === 'artifact') {
      let rows = row('path', `<code>${esc(n.path)}</code>`) + row('size', fmtBytes(n.size)) + row('sha256', n.sha256 ? `<code>${esc(n.sha256)}</code>` : '<span class="hash">not hashed (missing or too large)</span>') + row('integrity', esc(n.integrity || 'unchecked')) + row('origin', esc(n.origin)) + row('version', n.version ? 'v' + n.version : null) + row('recorded', esc(n.recorded_at));
      parts.push(`<h3>File</h3><table>${rows}</table>`);
      if (n.generated_by) parts.push(`<h3>Generated by</h3><ul><li>${ref(n.generated_by)}</li></ul>`);
      if ((n.used_by || []).length) parts.push(`<h3>Used by</h3><ul>${n.used_by.map((r) => `<li>${ref(r)}</li>`).join('')}</ul>`);
      if (n.supersedes) parts.push(`<h3>Supersedes</h3><ul><li>${ref(n.supersedes)}</li></ul>`);
    } else {
      parts.push(`<h3>Supported by (${(n.supported_by || []).length})</h3>${(n.supported_by || []).length ? `<ul>${n.supported_by.map((s) => `<li>${ref(s)}</li>`).join('')}</ul>` : '<span class="chip warn">no supporting evidence recorded</span>'}`);
      parts.push(`<p style="color:var(--muted);font-size:12px;margin-top:10px">A claim records what was concluded and from which computations. It does not mean the conclusion has been independently validated.</p>`);
    }
    parts.push(`<div class="actions"><button class="primary" type="button" id="btnCopy">Copy context for an agent</button><button type="button" id="btnBranch">Copy “branch from here” prompt</button></div>`);
    panel.innerHTML = parts.join(''); panel.classList.add('show');
    $('btnClose').onclick = () => { panel.classList.remove('show'); state.selected = null; applyHighlight(); };
    $('btnCopy').onclick = () => copyText(contextFor(n), $('btnCopy'));
    $('btnBranch').onclick = () => copyText(branchPromptFor(n), $('btnBranch'));
    panel.querySelectorAll('a.ref').forEach((a) => a.addEventListener('click', () => { const id = a.getAttribute('data-ref'); if (!state.vis.has(id)) { state.showAll = true; $('showAll').checked = true; render(); } select(id); centerOn(id); }));
  }

  function contextFor(n) {
    const m = byId(); const L = [];
    L.push(`# Analysis Graph node: ${n.label || n.id} (${n.id})`);
    L.push(`type: ${n.type}${n.status ? '  status: ' + n.status : ''}`);
    if (n.type === 'run') {
      if (n.description) L.push(`agent said: ${n.description}`);
      if (n.command) L.push(`command: ${n.command}`);
      (n.code || []).forEach((c) => L.push(`code: ${c.path} (sha256 ${short(c.sha256)})`));
      const p = fmtParams(n.params); if (p) L.push(`params: ${p}`);
      (n.inputs || []).forEach((i) => { const a = m[i] || {}; L.push(`input: ${a.path || i} (sha256 ${short(a.sha256)}, ${a.integrity || 'unchecked'})`); });
      (n.outputs || []).forEach((o) => { const a = m[o] || {}; L.push(`output: ${a.path || o} (sha256 ${short(a.sha256)}, ${a.integrity || 'unchecked'})`); });
      const ev = n.evidence || {}; L.push(`evidence: ${ev.level || 'declared'}${ev.exec_ids && ev.exec_ids.length ? ' (' + ev.exec_ids.join(', ') + ', exit ' + ev.exit_code + ')' : ''}`);
      if (n.git) L.push(`git: ${n.git.commit}${n.git.dirty ? ' (dirty)' : ''}`);
    } else if (n.type === 'artifact') {
      L.push(`path: ${n.path}`); L.push(`sha256: ${n.sha256 || 'n/a'}  integrity: ${n.integrity || 'unchecked'}`);
      if (n.generated_by) L.push(`generated by: ${(m[n.generated_by] || {}).label || n.generated_by} (${n.generated_by})`);
    } else {
      (n.supported_by || []).forEach((s) => L.push(`supported by: ${(m[s] || {}).label || s} (${s})`));
    }
    if (n.note) L.push(`note: ${n.note}`);
    return L.join('\n');
  }
  function branchPromptFor(n) {
    return contextFor(n) + `\n\nContinue the analysis from this node. Do not overwrite it: a changed parameter or method must become a new run recorded with\n  python3 analysis/whatran.py record "<what it did>" --supersedes ${n.id}\n(inputs, outputs, code and parameters are read from the command you ran; add --param k=v if a value is missed), then re-run only the downstream steps that depend on it, record them, and finish with \`python3 analysis/whatran.py done\`. Mark the branch you abandon with \`whatran.py reject\` instead of deleting it.`;
  }
  async function copyText(text, btn) {
    try { await navigator.clipboard.writeText(text); } catch (e) {
      const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); } catch (_) { /* ignore */ } ta.remove();
    }
    const old = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = old; }, 1200);
  }

  // ---------------------------------------------------------------- log
  function showLog() {
    const g = state.graph; const m = byId(); const log = g.exec_log || [];
    const rows = log.slice().reverse().map((e) => `<tr class="${e.linked_run ? '' : 'unlinked'}"><td><code>${esc(e.id)}</code></td><td>${esc((e.ts || '').replace('T', ' ').replace('Z', ''))}</td><td>${e.exit_code == null ? '?' : esc(e.exit_code)}</td><td>${(e.files_changed || []).length}${e.files_changed_truncated ? '+' : ''}</td><td>${e.linked_run ? `<a class="ref" data-ref="${esc(e.linked_run)}">${esc((m[e.linked_run] || {}).label || e.linked_run)}</a>` : '<span class="hash">—</span>'}</td><td class="cmd">${esc(trunc(e.command || '', 160))}</td></tr>`).join('');
    const box = $('log').querySelector('.box');
    box.innerHTML = `<div class="toolbar"><h2>Execution log — every recorded command, including ones not linked to a node (${log.length})</h2><button type="button" id="btnLogClose">Close</button></div>` + (log.length ? `<table><thead><tr><th>id</th><th>time (UTC)</th><th>exit</th><th>files</th><th>linked run</th><th>command</th></tr></thead><tbody>${rows}</tbody></table>` : '<p style="color:var(--muted)">No executions recorded. Hooks or <code>whatran.py run</code> write them.</p>');
    $('log').classList.add('show');
    $('btnLogClose').onclick = () => $('log').classList.remove('show');
    box.querySelectorAll('a.ref').forEach((a) => a.addEventListener('click', () => { $('log').classList.remove('show'); const id = a.getAttribute('data-ref'); if (!state.vis.has(id)) { state.showAll = true; $('showAll').checked = true; render(); } select(id); centerOn(id); }));
  }

  // ---------------------------------------------------------------- pan / zoom
  function applyTransform() { viewport.setAttribute('transform', `translate(${state.tx},${state.ty}) scale(${state.k})`); }
  function bounds() { const ps = Object.values(state.pos); if (!ps.length) return { x: 0, y: 0, w: NODE_W, h: NODE_H }; const x0 = Math.min(...ps.map((p) => p.x)), y0 = Math.min(...ps.map((p) => p.y)); const x1 = Math.max(...ps.map((p) => p.x)) + NODE_W, y1 = Math.max(...ps.map((p) => p.y)) + NODE_H; return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }; }
  function fit() { const r = svg.getBoundingClientRect(); const b = bounds(); const k = Math.min(1.2, Math.max(0.15, Math.min((r.width - 48) / b.w, (r.height - 48) / b.h))); state.k = k; state.tx = (r.width - b.w * k) / 2 - b.x * k; state.ty = Math.max(24, (r.height - b.h * k) / 2 - b.y * k); applyTransform(); }
  function centerOn(id) { const p = state.pos[id]; if (!p) return; const r = svg.getBoundingClientRect(); state.tx = r.width / 2 - (p.x + NODE_W / 2) * state.k; state.ty = r.height / 2 - (p.y + NODE_H / 2) * state.k; applyTransform(); }
  // Selection happens on pointer-up: pointer capture (needed for smooth panning) re-targets
  // the synthetic click to the <svg>, so node click listeners would never fire.
  let drag = null;
  svg.addEventListener('pointerdown', (e) => { if (e.button !== 0) return; const node = e.target.closest ? e.target.closest('.node') : null; drag = { x: e.clientX, y: e.clientY, tx: state.tx, ty: state.ty, moved: false, node: node ? node.getAttribute('data-id') : null }; svg.setPointerCapture(e.pointerId); svg.classList.add('dragging'); });
  svg.addEventListener('pointermove', (e) => { if (!drag) return; const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true; state.tx = drag.tx + dx; state.ty = drag.ty + dy; applyTransform(); });
  svg.addEventListener('pointerup', () => { const d = drag; drag = null; svg.classList.remove('dragging'); if (!d || d.moved) return; if (d.node) select(d.node); else { state.selected = null; applyHighlight(); $('panel').classList.remove('show'); } });
  svg.addEventListener('pointercancel', () => { drag = null; svg.classList.remove('dragging'); });
  svg.addEventListener('wheel', (e) => { e.preventDefault(); const r = svg.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top; const f = Math.exp(-e.deltaY * 0.0015); const k = Math.min(3, Math.max(0.1, state.k * f)); state.tx = mx - (mx - state.tx) * (k / state.k); state.ty = my - (my - state.ty) * (k / state.k); state.k = k; applyTransform(); }, { passive: false });

  // ---------------------------------------------------------------- controls
  $('showAll').addEventListener('change', (e) => { state.showAll = e.target.checked; render(); fit(); });
  $('search').addEventListener('input', (e) => { state.query = e.target.value; render(); });
  $('btnFit').addEventListener('click', fit);
  $('btnLog').addEventListener('click', showLog);
  document.addEventListener('keydown', (e) => { if (e.target.tagName === 'INPUT') return; if (e.key === 'Escape') { $('log').classList.remove('show'); $('panel').classList.remove('show'); state.selected = null; applyHighlight(); } if (e.key === 'f') fit(); });
  window.addEventListener('resize', () => { if (state.graph) fit(); });
  load();
})();
</script>
</body>
</html>
'''

# --------------------------------------------------------------------------- #
# writing rules (Simplified-Technical-English style) for labels, claims, notes
# --------------------------------------------------------------------------- #
#
# Records must be short and uniform so that they cost few tokens, read the same
# whichever agent or language wrote them, and can be matched by programs.
# The rules follow ASD-STE100's spirit: a step label starts with an approved verb
# and has at most 8 words; every sentence in a claim, note or reason has at most
# 20 words; no pronouns, no hedging, no passive voice. The official STE
# dictionary is copyrighted, so the verb list below is our own, written to the
# same rules (one meaning, infinitive form); projects can replace or extend it
# in analysis/config.json ("style": {"verbs": [...], "verbs_file": "path"}).

BUILTIN_VERBS = sorted(set("""
add adjust align annotate apply approximate assemble assign average bin build
calculate calibrate call check choose classify clean clip close cluster collect
combine compare compile compress compute concatenate configure confirm convert
copy correct count create crop cut decode deduplicate define delete denoise
derive detect digitize discard divide do download downsample draw drop embed
encode estimate evaluate exclude expand export extract fetch fill filter find
fit flag format generate get group identify impute import include index infer
install integrate interpolate join keep label list load locate make map mark
mask match measure merge model move normalise normalize open optimise optimize
order pad parse partition plot predict prepare print process profile prune
quantify query rank read record reduce regress reject remove rename render
repeat replace report resample rescale reshape resolve round run sample save
scale score search segment select separate set show simulate smooth sort split
stack standardise standardize start stop store subset subtract sum summarise
summarize tag test threshold tokenize train transform trim tune update upload
use validate verify visualise visualize write
""".split()))

FORBIDDEN_WORDS = {
    "i", "we", "you", "me", "my", "our", "your", "us",
    "maybe", "perhaps", "probably", "seems", "seem", "appears", "appear",
    "might", "could", "should", "would", "very", "really", "quite", "somewhat",
    "basically", "essentially", "just", "actually", "etc", "etc.",
    "was", "were", "been", "being",
}

PREFERRED_WORDS = {
    "perform": "do", "utilize": "use", "utilise": "use", "commence": "start",
    "terminate": "stop", "in order to": "to", "prior to": "before",
    "subsequent to": "after", "carry out": "do", "a number of": "several",
}

STYLE_DEFAULT: Dict[str, Any] = {
    "enabled": True,
    "max_label_words": 8,
    "max_sentence_words": 20,
    "verbs": [],          # extra approved verbs for this project
    "verbs_file": None,   # one verb per line; replaces the built-in list when set
}


class StyleError(AgError):
    """A label or sentence breaks the writing rules."""


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def _norm(word: str) -> str:
    return re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", word.lower())


def approved_verbs(p: Optional["Project"]) -> set:
    cfg = dict(STYLE_DEFAULT)
    if p is not None:
        cfg.update(p.config.get("style") or {})
    verbs = set(BUILTIN_VERBS)
    vf = cfg.get("verbs_file")
    if vf:
        path = (p.root / vf) if (p is not None and not os.path.isabs(vf)) else Path(vf)
        if path.is_file():
            verbs = {_norm(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    verbs.update(_norm(v) for v in (cfg.get("verbs") or []))
    return verbs


def _verb_from_noun(word: str, verbs: set) -> Optional[str]:
    """detection -> detect, comparison -> compare, normalization -> normalize."""
    w = _norm(word)
    for cand in (w, w[:-3], w[:-4] + "e", w[:-5] + "e", w[:-5], w[:-2]):
        if len(cand) > 2 and cand in verbs:
            return cand
    return None


def check_label(label: str, p: Optional["Project"] = None) -> List[str]:
    """Problems with a step label; empty when it follows the rules."""
    cfg = dict(STYLE_DEFAULT)
    if p is not None:
        cfg.update(p.config.get("style") or {})
    problems: List[str] = []
    words = _words(label)
    if not words:
        return ["label is empty"]
    if len(words) > int(cfg["max_label_words"]):
        problems.append(f"{len(words)} words; keep a label to {cfg['max_label_words']} words")
    verbs = approved_verbs(p)
    first = _norm(words[0])
    if first not in verbs:
        hint = None
        for i, w in enumerate(words):
            v = _verb_from_noun(w, verbs)
            if v:
                rest = [x.lower() if x.isalpha() else x for x in words if x is not w]
                if i == 1 and rest and rest[0].isalpha() and not rest[0].endswith(("s", "x", "z", "a", "y", "ch", "sh")):
                    rest[0] += "s"                      # "Peak detection" -> "Detect peaks"
                hint = f'"{v.capitalize()} {" ".join(rest)}"'.replace("  ", " ").strip()
                break
        problems.append(f'start with an approved verb ("{words[0]}" is not one)' + (f"; for example {hint}" if hint else
                        "; for example \"Detect peaks\", \"Compare groups\", \"Fit model\""))
    problems += _check_words(label)
    if label.rstrip().endswith("."):
        problems.append("no full stop at the end of a label")
    return problems


def _check_words(text: str) -> List[str]:
    problems: List[str] = []
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    bad = sorted({w for w in (_norm(x) for x in _words(text)) if w in FORBIDDEN_WORDS})
    if bad:
        problems.append("avoid " + ", ".join(f'"{w}"' for w in bad) +
                        " (no pronouns, hedging, intensifiers or passive voice)")
    for phrase, better in PREFERRED_WORDS.items():
        if f" {phrase} " in low:
            problems.append(f'write "{better}" instead of "{phrase}"')
    return problems


def check_sentences(text: str, p: Optional["Project"] = None, what: str = "text") -> List[str]:
    """Problems with a claim, note or reason; empty when it follows the rules."""
    cfg = dict(STYLE_DEFAULT)
    if p is not None:
        cfg.update(p.config.get("style") or {})
    problems: List[str] = []
    if not text or not text.strip():
        return [f"{what} is empty"]
    limit = int(cfg["max_sentence_words"])
    for sentence in re.split(r"(?<=[.;!?])\s+", text.strip()):
        n = len(_words(sentence))
        if n > limit:
            problems.append(f"a sentence has {n} words; keep each sentence to {limit} words: \"{sentence[:60]}...\""
                            if len(sentence) > 60 else f"a sentence has {n} words; keep each sentence to {limit} words")
    problems += _check_words(text)
    return problems


def style_enabled(p: Optional["Project"], override: Optional[bool] = None) -> bool:
    if override is not None:
        return override
    if os.environ.get("WHATRAN_NO_STYLE"):
        return False
    cfg = dict(STYLE_DEFAULT)
    if p is not None:
        cfg.update(p.config.get("style") or {})
    return bool(cfg.get("enabled", True))


def enforce_style(p: Optional["Project"], *, label: Optional[str] = None, sentences: Optional[Dict[str, Optional[str]]] = None,
                  enabled: Optional[bool] = None) -> None:
    """Raise StyleError listing every problem, or return silently."""
    if not style_enabled(p, enabled):
        return
    problems: List[str] = []
    if label is not None:
        problems += [f'label "{label}": {x}' for x in check_label(label, p)]
    for what, text in (sentences or {}).items():
        if text:
            problems += [f"{what}: {x}" for x in check_sentences(text, p, what)]
    if problems:
        raise StyleError("the record does not follow the writing rules (short, verb-first, no hedging; "
                         "--no-style or WHATRAN_NO_STYLE=1 to bypass):\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- #
# bounded steps (begin / end): the language-neutral way to wrap code in a step
# --------------------------------------------------------------------------- #

def begin_step(p: Project, label: str, *, code: Optional[List[str]] = None, note: Optional[str] = None,
               style: Optional[bool] = None) -> str:
    enforce_style(p, label=label, sentences={"note": note}, enabled=style)
    p.state_dir.mkdir(parents=True, exist_ok=True)
    token = f"s-{int(time.time() * 1000):x}-{os.getpid():x}"
    state = {
        "label": label,
        "started": time.time(),
        "cwd": os.getcwd(),
        "code": [p.rel(c) for c in (code or [])],
        "note": note,
        "snapshot": {k: list(v) for k, v in p.snapshot().items()},
    }
    (p.state_dir / f"step-{token}.json").write_text(json.dumps(state), encoding="utf-8")
    return token


def end_step(p: Project, token: str, *, inputs: Optional[List[str]] = None, outputs: Optional[List[str]] = None,
             code: Optional[List[str]] = None, params: Optional[Dict[str, Any]] = None, note: Optional[str] = None,
             status: str = "completed", supersedes: Optional[str] = None, style: Optional[bool] = None) -> Dict[str, Any]:
    sf = p.state_dir / f"step-{token}.json"
    if not sf.is_file():
        raise AgError(f"no open step with token {token}")
    state = json.loads(sf.read_text(encoding="utf-8"))
    before = {k: (v[0], v[1]) for k, v in state["snapshot"].items()}
    files, truncated = p.changed_since_snapshot(before)
    ended = time.time()
    note = note or state.get("note")
    enforce_style(p, sentences={"note": note}, enabled=style)
    ev = p.make_exec_event(None, source="step", exit_code=0 if status == "completed" else 1,
                           started=state["started"], ended=ended, cwd=state.get("cwd"),
                           files_changed=files, truncated=truncated)
    ev["label"] = state["label"]
    p.append(ev)
    res = record_run(p, state["label"], inputs=inputs or [], outputs=outputs or [],
                     code=code or state.get("code") or [], params=params or {}, command=None,
                     evidence_mode=ev["id"], note=note, status=status, supersedes=supersedes, exec_event=ev,
                     style=False)
    try:
        sf.unlink()
    except OSError:
        pass
    return res


# --------------------------------------------------------------------------- #
# Python API:  import whatran as w
#   w.record("Detect peaks")                 after a step that already ran
#   with w.step("Fit model", params={...}):  around code
#   @w.step("Normalize values", params=["method"])  on a function
#   w.claim(...), w.reject(...), w.edit(...), w.remove(...), w.run(...)
# --------------------------------------------------------------------------- #

_API_ROOT: Optional[Path] = None


def use(root: Optional[str] = None) -> Path:
    """Point the API at a project (default: walk up from the current directory)."""
    global _API_ROOT
    r = find_root(root) if root else find_root()
    if r is None:
        raise AgError("no analysis/config.json found; call whatran.init() or run `whatran init` in the project")
    _API_ROOT = r
    return r


def _project() -> Project:
    root = _API_ROOT or find_root()
    if root is None:
        raise AgError("no analysis/config.json found; call whatran.init() or run `whatran init` in the project")
    return Project(root)


def _id(x: Any) -> str:
    return getattr(x, "id", x)


def init(root: Optional[str] = None, name: Optional[str] = None, description: Optional[str] = None) -> Path:
    """Create analysis/ in root (default: cwd) and start recording there."""
    ns = argparse.Namespace(root=root, name=name, description=description, no_copy=False, force=False)
    with open(os.devnull, "w") as sink:
        import contextlib
        with contextlib.redirect_stdout(sink):
            cmd_init(ns)
    return use(root or os.getcwd())


def record(label: str, *, inputs: Any = (), outputs: Any = (), code: Any = (), params: Optional[Dict[str, Any]] = None,
           command: Optional[str] = None, evidence: str = "auto", note: Optional[str] = None,
           supersedes: Any = None, infer: bool = True, style: Optional[bool] = None) -> str:
    """Record a step that already ran (its command was captured by hooks or `run`). Returns the run id."""
    p = _project()
    res = record_run(p, label, inputs=list(_strs(inputs)), outputs=list(_strs(outputs)), code=list(_strs(code)),
                     params=params or {}, command=command, evidence_mode=evidence, note=note, status="completed",
                     supersedes=_id(supersedes) if supersedes else None, infer=infer, style=style)
    return res["id"]


def _strs(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, (str, os.PathLike)):
        return [os.fspath(x)]
    return [os.fspath(i) for i in x]


class step:
    """Bound a step around code: `with w.step("Fit model") as s:` or `@w.step("Fit model")`.

    Files that change inside the block become the step's outputs; `inputs`, `code` and
    `params` can be given, and the calling script is taken as the code when inside the
    project. As a decorator, `params` may list argument names to capture, or be True
    for every simple argument.
    """

    def __init__(self, label: str, *, params: Any = None, inputs: Any = (), outputs: Any = (), code: Any = None,
                 note: Optional[str] = None, supersedes: Any = None, style: Optional[bool] = None):
        self.label = label
        self.params = params
        self.inputs = list(_strs(inputs))
        self.outputs = list(_strs(outputs))
        self.code = None if code is None else list(_strs(code))
        self.note = note
        self.supersedes = _id(supersedes) if supersedes else None
        self.style = style
        self.id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self._token: Optional[str] = None
        self._p: Optional[Project] = None

    def _caller_code(self, depth: int = 2) -> List[str]:
        import inspect
        try:
            frame = inspect.stack()[depth].frame
            f = frame.f_code.co_filename
        except Exception:
            return []
        if not f or f.startswith("<") or "ipykernel" in f:
            return []
        p = self._p or _project()
        rel = p.rel(f)
        return [rel] if not rel.startswith("/") and p.abs(rel).is_file() else []

    def __enter__(self) -> "step":
        self._p = _project()
        code = self.code if self.code is not None else self._caller_code()
        self._token = begin_step(self._p, self.label, code=code, note=self.note, style=self.style)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        params = self.params if isinstance(self.params, dict) else {}
        self.result = end_step(self._p, self._token, inputs=self.inputs, outputs=self.outputs,
                               params=params, status="failed" if exc_type else "completed",
                               supersedes=self.supersedes, style=self.style)
        self.id = self.result["id"]
        return False

    def __call__(self, fn):
        import functools
        import inspect
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            captured: Dict[str, Any] = {}
            if isinstance(self.params, dict):
                captured = dict(self.params)
            else:
                names = list(self.params) if isinstance(self.params, (list, tuple, set)) else (
                    list(bound.arguments) if self.params is True else [])
                for n in names:
                    v = bound.arguments.get(n)
                    if isinstance(v, (int, float, str, bool)) or v is None:
                        captured[n] = v
            inner = step(self.label, params=captured, inputs=self.inputs, outputs=self.outputs,
                         code=self.code if self.code is not None else self._fn_code(fn), note=self.note,
                         supersedes=self.supersedes, style=self.style)
            with inner:
                out = fn(*args, **kwargs)
            wrapper.last_step = inner  # type: ignore[attr-defined]
            return out
        wrapper.last_step = None  # type: ignore[attr-defined]
        return wrapper

    @staticmethod
    def _fn_code(fn) -> List[str]:
        try:
            f = fn.__code__.co_filename
        except AttributeError:
            return []
        if not f or f.startswith("<") or "ipykernel" in f:
            return []
        p = _project()
        rel = p.rel(f)
        return [rel] if not rel.startswith("/") and p.abs(rel).is_file() else []


def claim(text: str, supports: Any = (), note: Optional[str] = None, style: Optional[bool] = None) -> str:
    """Record a finding and what it rests on (run ids, Step objects, or output paths). Returns the claim id."""
    p = _project()
    sup = [str(_id(s)) for s in (supports if not isinstance(supports, (str, os.PathLike)) and supports is not None else [supports])]
    sup = [s for s in sup if s and s != "None"]
    res = record_claim(p, text, sup, "tentative", note, style=style)
    return res["id"]


def reject(node: Any, reason: str, style: Optional[bool] = None) -> None:
    """Abandon a run or claim with a reason. It stays in the record, hidden from the result path."""
    p = _project()
    enforce_style(p, sentences={"reason": reason}, enabled=style)
    nodes = fold_nodes(p.events())
    nid = _id(node)
    if nid not in nodes:
        raise AgError(f"unknown node {nid}")
    p.append({"event": "status", "id": nid, "status": "rejected", "reason": reason})


def edit(node: Any, *, label: Optional[str] = None, note: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
         status: Optional[str] = None, style: Optional[bool] = None) -> None:
    """Amend a record. The previous values stay in the ledger; the graph shows the latest."""
    p = _project()
    nodes = fold_nodes(p.events())
    nid = _id(node)
    if nid not in nodes:
        raise AgError(f"unknown node {nid}")
    n = nodes[nid]
    enforce_style(p, label=label if (label is not None and n.get("type") == "run") else None,
                  sentences={"note": note, "label": label if (label is not None and n.get("type") == "claim") else None},
                  enabled=style)
    fields: Dict[str, Any] = {}
    if label is not None:
        fields["label"] = label
    if note is not None:
        fields["note"] = note
    if params:
        merged = dict(n.get("params") or {})
        merged.update(params)
        fields["params"] = merged
    if status is not None:
        fields["status"] = status
    if not fields:
        raise AgError("nothing to edit")
    p.append({"event": "update", "id": nid, "fields": fields})


def remove(node: Any, reason: Optional[str] = None) -> None:
    """Retract a record from the graph. The ledger keeps it, marked retracted, for audit."""
    p = _project()
    nodes = fold_nodes(p.events())
    nid = _id(node)
    if nid not in nodes:
        raise AgError(f"unknown node {nid}")
    ev: Dict[str, Any] = {"event": "retract", "id": nid}
    if reason:
        ev["reason"] = reason
    p.append(ev)


def run(command: Any, label: Optional[str] = None, **kw: Any) -> int:
    """Run a shell command (string or argv list), capture its evidence, optionally record it as a step."""
    argv = ["run"]
    if label:
        argv += ["--label", label]
    for k in ("inputs", "outputs", "code"):
        for v in _strs(kw.get(k)):
            argv += [{"inputs": "--in", "outputs": "--out", "code": "--code"}[k], v]
    for k, v in (kw.get("params") or {}).items():
        argv += ["--param", f"{k}={json.dumps(v) if isinstance(v, (list, dict)) else v}"]
    if kw.get("note"):
        argv += ["--note", kw["note"]]
    if kw.get("supersedes"):
        argv += ["--supersedes", _id(kw["supersedes"])]
    argv.append("--")
    argv += [command] if isinstance(command, str) else [shlex.join([os.fspath(c) for c in command])]
    root = _API_ROOT or find_root()
    if root is not None:
        argv = ["--root", str(root)] + argv
    return main(argv)



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


def write_viewer(dst: Path) -> None:
    """Copy the viewer next to the graph: from the plugin tree when present, else the embedded copy."""
    src = plugin_viewer_path()
    if src is not None and src.resolve() == dst.resolve():
        return
    dst.write_text(src.read_text(encoding="utf-8") if src else VIEWER_HTML, encoding="utf-8")


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
        "observe": {"enabled": True},
        "style": {"enabled": True, "verbs": []},
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
        write_viewer(ag_dir / VIEWER_NAME)
        copied.append(ag_dir / VIEWER_NAME)
    print(f"initialised {ag_dir}")
    for c in copied:
        print(f"  copied {c.relative_to(root)}")
    print("recording is on. Claude Code: nothing else to do; every command is captured and each turn ends with the graph and package refreshed.")
    print("elsewhere: run steps as `python3 analysis/whatran.py run -- CMD`, finish with `done`. optional: claim \"<finding>\", reject <id> --reason")
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
                     supersedes=args.supersedes, node_id=args.id, infer=not args.no_infer,
                     style=False if args.no_style else None)
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
    res = record_claim(p, args.text, args.supports or [], args.status, args.note, node_id=args.id,
                       style=False if args.no_style else None)
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
    if args.label:
        enforce_style(p, label=args.label, sentences={"note": args.note}, enabled=False if args.no_style else None)
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
                         exec_event=ev, style=False if args.no_style else None)
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
    enforce_style(p, sentences={"reason": args.reason}, enabled=False if args.no_style else None)
    p.append({"event": "status", "id": args.id, "status": "rejected", "reason": args.reason})
    print(f"{args.id} marked rejected (kept in the record): {args.reason}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    p = require_project(args)
    nodes = fold_nodes(p.events())
    if args.id not in nodes:
        raise AgError(f"unknown node {args.id}")
    fields: Dict[str, Any] = {}
    t = nodes[args.id].get("type")
    enforce_style(p, label=args.label if (args.label and t == "run") else None,
                  sentences={"note": args.note, "label": args.label if (args.label and t == "claim") else None},
                  enabled=False if args.no_style else None)
    if args.label:
        fields["label"] = args.label
    if args.note:
        fields["note"] = args.note
    if args.status:
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


def cmd_begin(args: argparse.Namespace) -> int:
    p = require_project(args)
    token = begin_step(p, args.label, code=args.code or [], note=args.note, style=False if args.no_style else None)
    print(token)
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    p = require_project(args)
    res = end_step(p, args.token, inputs=args.inputs or [], outputs=args.outputs or [], code=args.code or [],
                   params=_params_from_args(args), note=args.note, status=args.status, supersedes=args.supersedes,
                   style=False if args.no_style else None)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"recorded {res['id']}  [{res['status']}; outputs {len(res['outputs'])}]")
        for w in res["warnings"]:
            print(f"  warning: {w}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    p = require_project(args)
    nodes = fold_nodes(p.events())
    if args.id not in nodes:
        raise AgError(f"unknown node {args.id}")
    ev: Dict[str, Any] = {"event": "retract", "id": args.id}
    if args.reason:
        ev["reason"] = args.reason
    p.append(ev)
    print(f"{args.id} retracted from the graph (the ledger keeps it)")
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
    """What the Stop hook does at the end of every turn, for agents without hooks:
    promote executions to steps, compile, verify files, write the package."""
    p = require_project(args)
    res = finish(p, package=not args.no_package)
    graph = res["graph"]
    for item in res["promoted"]:
        if item.get("repeat"):
            print(f"attached {item['exec']} to {item['run']} (same command, same outputs)")
        else:
            extra = f"; supersedes {item['supersedes']}" if item.get("supersedes") else ""
            print(f"recorded {item['run']}  \"{item['label']}\"  (label from the {item['label_from']}{extra})")
    print_summary(p, graph)
    s = graph["summary"]
    print(f"wrote {p.graph_path.relative_to(p.root)}")
    pkg = res["package"]
    for rel in pkg["written"]:
        print(f"wrote {rel}")
    if s["artifacts"]["modified"] or s["artifacts"]["missing"]:
        print("warning: some recorded files changed or vanished since they were recorded (see above)")
    if s["claims"]["total"] == 0:
        print("no claims yet: whatran.py claim \"<finding>\" marks what the result path leads to")
    print(f"open the graph:      python3 {AG_DIRNAME}/whatran.py open")
    if not pkg["empty"]:
        print(f"verify it reruns:    python3 {AG_DIRNAME}/whatran.py check   (read run.sh first; add dependency installation if needed)")
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


# --------------------------------------------------------------------------- #
# generated files carry a fingerprint of their own content, so that a file nobody
# edited is refreshed as the record grows and an edited one is left alone.
# --------------------------------------------------------------------------- #

GENERATED_KEY = "generated"
_RUN_SH_MARK = re.compile(r"^# generated: whatran \S+ ([0-9a-f]{12})\n", re.M)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _manifest_body(manifest: Dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in manifest.items() if k != GENERATED_KEY}, sort_keys=True, ensure_ascii=False)


def stamp_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    manifest[GENERATED_KEY] = f"whatran {__version__} {_fingerprint(_manifest_body(manifest))}"
    return manifest


def manifest_is_generated(manifest: Dict[str, Any]) -> bool:
    """True when the manifest is exactly what whatran wrote (nobody edited it)."""
    g = manifest.get(GENERATED_KEY)
    return isinstance(g, str) and g.rsplit(" ", 1)[-1] == _fingerprint(_manifest_body(manifest))


def stamp_run_sh(text: str) -> str:
    body = _RUN_SH_MARK.sub("", text, count=1)
    first, nl, rest = body.partition("\n")
    return f"{first}{nl}# generated: whatran {__version__} {_fingerprint(body)}\n{rest}"


def run_sh_is_generated(text: str) -> bool:
    m = _RUN_SH_MARK.search(text)
    return bool(m) and m.group(1) == _fingerprint(_RUN_SH_MARK.sub("", text, count=1))


def write_package(p: Project, graph: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """Write the package files from the result path.

    whatran.json and run.sh are regenerated while they are untouched since whatran wrote
    them, and kept once somebody edited them. The static files (check.py, build_site.py,
    the workflow, README.md) are written only when missing. --force rewrites everything.
    """
    manifest_path = p.root / PACKAGE_MANIFEST
    written: List[str] = []
    kept: List[str] = []       # static files that already exist
    edited: List[str] = []     # generated files somebody changed; left alone
    existing_manifest = load_manifest(p) if manifest_path.is_file() else None
    if existing_manifest is not None and not force and not manifest_is_generated(existing_manifest):
        manifest = existing_manifest
        edited.append(PACKAGE_MANIFEST)
    else:
        manifest = stamp_manifest(export_package(p, graph))
        if not manifest.get("steps"):
            return {"written": [], "kept": [], "edited": [], "manifest": manifest, "empty": True}
        content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != content:
            manifest_path.write_text(content, encoding="utf-8")
            written.append(PACKAGE_MANIFEST)
    if not manifest.get("steps"):
        return {"written": written, "kept": kept, "edited": edited, "manifest": manifest, "empty": True}

    run_sh = p.root / "run.sh"
    if run_sh.is_file() and not force and not run_sh_is_generated(run_sh.read_text(encoding="utf-8")):
        edited.append("run.sh")
    else:
        content = stamp_run_sh(make_run_sh(graph, manifest))
        if not run_sh.is_file() or run_sh.read_text(encoding="utf-8") != content:
            run_sh.write_text(content, encoding="utf-8")
            run_sh.chmod(0o755)
            written.append("run.sh")

    for rel, content in (("check.py", CHECK_PY), ("build_site.py", BUILD_SITE_PY),
                         (".github/workflows/reproduce.yml", REPRODUCE_YML)):
        path = p.root / rel
        if path.exists() and not force:
            kept.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(rel)
    if not (p.root / "README.md").exists():
        (p.root / "README.md").write_text(make_readme(manifest), encoding="utf-8")
        written.append("README.md")
    gi = p.root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    add = [line for line in ("site/", ".venv/", "analysis/.state/") if line not in existing.splitlines()]
    if add:
        gi.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(add) + "\n", encoding="utf-8")
        written.append(".gitignore")
    return {"written": written, "kept": kept, "edited": edited, "manifest": manifest, "empty": False}


# --------------------------------------------------------------------------- #
# the agent's report: its last message of a turn, kept next to the graph so that
# what it said can be compared with what ran
# --------------------------------------------------------------------------- #

REPORT_NAME = "report.md"
_REPORT_HEAD = re.compile(r"^<!-- whatran:.*?-->\n+", re.S)


def mentions_record(text: str, nodes: "OrderedDict[str, Dict[str, Any]]") -> bool:
    """Does the text name a recorded file or step?"""
    if not text:
        return False
    low = text.lower()
    for n in nodes.values():
        if n.get("type") == "artifact" and n.get("path"):
            path = n["path"].lower()
            name = os.path.basename(path)
            if path in low or (len(name) > 5 and name in low):
                return True
        elif n.get("type") == "run" and n.get("label") and n["label"].lower() in low:
            return True
    return False


def write_report(p: Project, text: str) -> bool:
    """Save the agent's words as analysis/report.md. Returns True when the file changed."""
    body = (text or "").strip()
    if not body:
        return False
    path = p.ag_dir / REPORT_NAME
    if path.is_file():
        old = _REPORT_HEAD.sub("", path.read_text(encoding="utf-8"), count=1).strip()
        if old == body:
            return False
    head = (f"<!-- whatran: what the agent said when its turn ended at {now_iso()}. "
            "Compare with analysis/graph.json: the graph is what ran. -->\n\n")
    p.ag_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(head + body + "\n", encoding="utf-8")
    return True


def finish(p: Project, *, report: Optional[str] = None, package: bool = True) -> Dict[str, Any]:
    """Everything that happens when a turn ends: promote executions to steps, compile the
    graph, refresh the package, save the report. Safe to repeat."""
    promoted = promote_executions(p)
    graph = build_graph(p, verify=True)
    write_graph(p, graph)
    draft = export_package(p, graph)
    (p.ag_dir / "whatran.draft.json").write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pkg = write_package(p, graph) if package else {"written": [], "kept": [], "edited": [], "empty": True}
    report_changed = write_report(p, report) if report else False
    return {"promoted": promoted, "graph": graph, "package": pkg, "report": report_changed}


def cmd_package(args: argparse.Namespace) -> int:
    p = require_project(args)
    graph = build_graph(p, verify=True)
    write_graph(p, graph)
    res = write_package(p, graph, force=args.force)
    if res["empty"]:
        raise AgError("nothing to package: no completed runs on the result path (record steps first, then `done`)")
    manifest = res["manifest"]
    for rel in res["written"]:
        print(f"wrote   {rel}")
    for rel in res["edited"]:
        print(f"kept    {rel}  (edited; --force overwrites)")
    for rel in res["kept"]:
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
    write_viewer(dest / VIEWER_NAME)
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
        write_viewer(p.ag_dir / VIEWER_NAME)
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
  begin LABEL / end TOKEN   wrap any code in a step from any language (what the R and Python APIs use)
  remove ID             retract a record from the graph (the ledger keeps it)
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
    sp.add_argument("--no-style", action="store_true", help="skip the writing rules for this record")


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
    rr.add_argument("label", help='what the step did, verb first, e.g. "Detect peaks"')
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
    rc.add_argument("--no-style", action="store_true", help="skip the writing rules for this record")
    rc.add_argument("--id", help=hidden)
    rc.add_argument("--json", action="store_true", help=hidden)
    rc.set_defaults(func=cmd_claim)

    j = sub.add_parser("reject", help="abandon a run or claim with a reason (it stays in the record)")
    j.add_argument("id")
    j.add_argument("--reason", required=True)
    j.add_argument("--no-style", action="store_true", help=hidden)
    j.set_defaults(func=cmd_reject)

    d = sub.add_parser("done", help="turn executions into steps, compile the graph, check files, write the package")
    d.add_argument("--no-package", action="store_true", help=hidden)
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
    u.add_argument("--no-style", action="store_true")
    u.set_defaults(func=cmd_update)

    rm = sub.add_parser("remove")
    rm.add_argument("id")
    rm.add_argument("--reason")
    rm.set_defaults(func=cmd_remove)

    bg = sub.add_parser("begin")
    bg.add_argument("label")
    bg.add_argument("--code", action="append", metavar="PATH")
    bg.add_argument("--note")
    bg.add_argument("--no-style", action="store_true")
    bg.set_defaults(func=cmd_begin)

    en = sub.add_parser("end")
    en.add_argument("token")
    _add_step_flags(en)
    en.add_argument("--status", default="completed", choices=RUN_STATUSES)
    en.add_argument("--json", action="store_true")
    en.set_defaults(func=cmd_end)

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
