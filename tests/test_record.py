"""Tests for bin/whatran.py and hooks/observe.py.  Run: python3 -m unittest discover tests"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins/whatran/bin"))
import whatran as ag  # noqa: E402

OBSERVE = ROOT / "plugins/whatran/hooks/observe.py"


def run_ag(argv, cwd):
    """Call ag.main in-process from cwd; return (code, stdout, stderr)."""
    old = os.getcwd()
    os.chdir(cwd)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = ag.main(argv)
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "data").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "results").mkdir()
        (self.root / "data" / "raw.csv").write_text("x\n1\n2\n3\n")
        (self.root / "scripts" / "step.py").write_text(
            "import sys\nsrc, dst, k = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
            "vals = [float(l) for l in open(src).read().split()[1:]]\n"
            "open(dst, 'w').write('x\\n' + '\\n'.join(str(v * k) for v in vals) + '\\n')\n"
            "print('wrote', dst)\n")
        (self.root / "scripts" / "flagstep.py").write_text(
            "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--src'); ap.add_argument('--dst'); ap.add_argument('--k', type=float)\n"
            "a = ap.parse_args()\nvals = [float(l) for l in open(a.src).read().split()[1:]]\n"
            "open(a.dst, 'w').write('x\\n' + '\\n'.join(str(v * a.k) for v in vals) + '\\n')\n")
        code, out, err = run_ag(["init", "--name", "t", "--description", "q"], self.root)
        self.assertEqual(code, 0, err)
        self.p = ag.Project(self.root)
        # fixtures were written moments ago; hooks attribute files changed since a command
        # started (with a small slack), so date the fixtures back
        for fp in self.root.rglob("*"):
            if fp.is_file():
                os.utime(fp, (fp.stat().st_atime - 10, fp.stat().st_mtime - 10))

    def tearDown(self):
        self.tmp.cleanup()


class TestInit(Base):
    def test_creates_layout_and_copies_tool(self):
        self.assertTrue((self.root / "analysis" / "config.json").is_file())
        self.assertTrue((self.root / "analysis" / "events.jsonl").is_file())
        self.assertTrue((self.root / "analysis" / "whatran.py").is_file())
        self.assertTrue((self.root / "analysis" / "index.html").is_file())
        self.assertEqual((self.root / "analysis" / ".gitignore").read_text().strip(), ".state/")

    def test_find_root_from_subdir(self):
        self.assertEqual(ag.find_root(self.root / "results"), self.root)
        with tempfile.TemporaryDirectory() as other:
            self.assertIsNone(ag.find_root(other))

    def test_copied_tool_runs_standalone(self):
        r = subprocess.run([sys.executable, "analysis/whatran.py", "status", "--no-verify"],
                           cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("runs: 0", r.stdout)


class TestRecord(Base):
    def test_artifact_is_content_addressed(self):
        c1, o1, _ = run_ag(["artifact", "data/raw.csv", "--label", "Raw", "--json"], self.root)
        c2, o2, _ = run_ag(["artifact", "data/raw.csv", "--json"], self.root)
        self.assertEqual((c1, c2), (0, 0))
        r1, r2 = json.loads(o1), json.loads(o2)
        self.assertTrue(r1["created"]); self.assertFalse(r2["created"])
        self.assertEqual(r1["id"], r2["id"])
        (self.root / "data" / "raw.csv").write_text("x\n9\n")
        c3, o3, _ = run_ag(["artifact", "data/raw.csv", "--json"], self.root)
        r3 = json.loads(o3)
        self.assertTrue(r3["created"]); self.assertNotEqual(r3["id"], r1["id"])
        nodes = ag.fold_nodes(self.p.events())
        self.assertEqual(nodes[r3["id"]]["supersedes"], r1["id"])
        self.assertEqual(nodes[r3["id"]]["version"], 2)

    def test_missing_input_is_an_error(self):
        code, _, err = run_ag(["record", "Scale values", "--in", "data/nope.csv"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_declared_run_when_no_evidence(self):
        (self.root / "results" / "out.csv").write_text("x\n2\n")
        code, out, _ = run_ag(["record", "Scale", "--in", "data/raw.csv", "--out", "results/out.csv",
                               "--code", "scripts/step.py", "--param", "k=2", "--param", "w=5,30", "--json"], self.root)
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["evidence"], "declared")
        self.assertTrue(any("no captured execution matched" in w for w in res["warnings"]))
        node = ag.fold_nodes(self.p.events())[res["id"]]
        self.assertEqual(node["params"], {"k": 2, "w": [5, 30]})
        self.assertEqual(node["evidence"]["level"], "declared")
        self.assertFalse(node["verified_by_replay"])

    def test_run_wrapper_produces_observed_run(self):
        code, out, err = run_ag(["run", "--label", "Scale", "--in", "data/raw.csv", "--out", "results/out.csv",
                                 "--code", "scripts/step.py", "--param", "k=2", "--",
                                 f"{sys.executable} scripts/step.py data/raw.csv results/out.csv 2"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("wrote results/out.csv", out)
        events = self.p.events()
        execs = ag.exec_events(events)
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["exit_code"], 0)
        self.assertIn("results/out.csv", [f["path"] for f in execs[0]["files_changed"]])
        nodes = ag.fold_nodes(events)
        run = [n for n in nodes.values() if n["type"] == "run"][0]
        self.assertEqual(run["evidence"]["level"], "observed")
        self.assertEqual(run["evidence"]["exec_ids"], [execs[0]["id"]])
        self.assertEqual(run["evidence"]["outputs_observed"], ["results/out.csv"])
        self.assertEqual(run["status"], "completed")

    def test_run_wrapper_attributes_only_its_own_changes(self):
        py = sys.executable
        run_ag(["run", "--", f"{py} scripts/step.py data/raw.csv results/a.csv 2"], self.root)
        run_ag(["run", "--", f"{py} -c 'print(1)'"], self.root)          # immediately after, writes nothing
        run_ag(["run", "--", f"{py} scripts/step.py data/raw.csv results/b.csv 3"], self.root)
        execs = ag.exec_events(self.p.events())
        self.assertEqual([[f["path"] for f in e["files_changed"]] for e in execs],
                         [["results/a.csv"], [], ["results/b.csv"]])

    def test_run_wrapper_failure_marks_failed(self):
        code, _, _ = run_ag(["run", "--label", "Run failing command", "--", f"{sys.executable} -c 'import sys; sys.exit(3)'"], self.root)
        self.assertEqual(code, 3)
        run = [n for n in ag.fold_nodes(self.p.events()).values() if n["type"] == "run"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["evidence"]["exit_code"], 3)

    def test_auto_evidence_links_by_output(self):
        # an exec event written by a hook, then the agent declares the step
        (self.root / "results" / "out.csv").write_text("x\n2\n")
        files, _ = self.p.changed_files(0)
        ev = self.p.make_exec_event("python scripts/step.py data/raw.csv results/out.csv 2", source="test",
                                    exit_code=0, started=1.0, ended=2.0, files_changed=files)
        self.p.append(ev)
        code, out, _ = run_ag(["record", "Scale", "--out", "results/out.csv", "--json"], self.root)
        res = json.loads(out)
        self.assertEqual(res["evidence"], "observed")
        self.assertEqual(res["exec_ids"], [ev["id"]])
        # a second declaration does not steal the already-linked evidence
        code, out, _ = run_ag(["record", "Scale again", "--out", "results/out.csv", "--json"], self.root)
        self.assertEqual(json.loads(out)["evidence"], "declared")

    def test_claim_resolves_paths_and_ids(self):
        (self.root / "results" / "out.csv").write_text("x\n2\n")
        _, out, _ = run_ag(["record", "Scale", "--in", "data/raw.csv", "--out", "results/out.csv", "--json"], self.root)
        rid = json.loads(out)["id"]
        code, out, _ = run_ag(["claim", "Values doubled", "--supports", rid,
                               "--supports", "results/out.csv", "--json"], self.root)
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(len(res["supported_by"]), 2)
        self.assertEqual(res["supported_by"][0], rid)
        code, _, err = run_ag(["claim", "bad", "--supports", "results/none.csv"], self.root)
        self.assertEqual(code, 2)


class TestCompile(Base):
    def _analysis(self):
        py = sys.executable
        run_ag(["run", "--label", "Scale x2", "--in", "data/raw.csv", "--out", "results/a.csv",
                "--code", "scripts/step.py", "--param", "k=2", "--",
                f"{py} scripts/step.py data/raw.csv results/a.csv 2"], self.root)
        run_ag(["run", "--label", "Scale x3", "--in", "data/raw.csv", "--out", "results/b.csv",
                "--code", "scripts/step.py", "--param", "k=3", "--",
                f"{py} scripts/step.py data/raw.csv results/b.csv 3"], self.root)
        run_ag(["run", "--label", "Scale x10", "--in", "results/a.csv", "--out", "results/c.csv",
                "--code", "scripts/step.py", "--param", "k=10", "--",
                f"{py} scripts/step.py results/a.csv results/c.csv 10"], self.root)
        run_ag(["reject", "run-scale-x3-1", "--reason", "x3 is a dead end"], self.root)
        run_ag(["claim", "x20 overall", "--supports", "run-scale-x10-1"], self.root)
        # an unlinked execution with no scientific meaning
        run_ag(["run", "--", "echo debugging > results/scratch.txt"], self.root)

    def test_main_path_excludes_rejected_branch_and_keeps_it(self):
        self._analysis()
        code, out, _ = run_ag(["compile"], self.root)
        self.assertEqual(code, 0, out)
        g = json.loads(self.p.graph_path.read_text())
        self.assertEqual(g["format"], "analysis-graph/0.1")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("run-scale-x3-1", ids)
        main = set(g["main_path"])
        self.assertIn("run-scale-x2-1", main)
        self.assertIn("run-scale-x10-1", main)
        self.assertIn("claim-x20-overall-1", main)
        self.assertNotIn("run-scale-x3-1", main)
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(nodes["run-scale-x3-1"]["status"], "rejected")
        self.assertEqual(nodes["run-scale-x3-1"]["status_reason"], "x3 is a dead end")
        b = [n for n in g["nodes"] if n["type"] == "artifact" and n["path"] == "results/b.csv"][0]
        self.assertFalse(b["on_main_path"])
        self.assertEqual(g["summary"]["runs"]["rejected"], 1)
        self.assertEqual(g["summary"]["executions"]["total"], 4)
        self.assertEqual(g["summary"]["executions"]["linked"], 3)
        self.assertTrue((self.root / "analysis" / "graph.md").read_text().startswith("# Analysis graph"))

    def test_roles_and_edges(self):
        self._analysis()
        run_ag(["compile"], self.root)
        g = json.loads(self.p.graph_path.read_text())
        by_path = {n["path"]: n for n in g["nodes"] if n["type"] == "artifact"}
        self.assertEqual(by_path["data/raw.csv"]["role"], "input")
        self.assertEqual(by_path["results/a.csv"]["role"], "intermediate")
        self.assertEqual(by_path["results/c.csv"]["role"], "result")
        kinds = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
        self.assertIn((by_path["results/a.csv"]["id"], "run-scale-x10-1", "used"), kinds)
        self.assertIn(("run-scale-x10-1", by_path["results/c.csv"]["id"], "generated"), kinds)
        self.assertIn(("run-scale-x10-1", "claim-x20-overall-1", "supports"), kinds)

    def test_integrity_detects_modified_and_missing(self):
        self._analysis()
        (self.root / "results" / "a.csv").write_text("tampered\n")
        (self.root / "results" / "c.csv").unlink()
        code, out, _ = run_ag(["verify"], self.root)
        self.assertEqual(code, 1)
        self.assertIn("modified results/a.csv", out)
        self.assertIn("missing  results/c.csv", out)
        g = json.loads(self.p.graph_path.read_text())
        self.assertEqual(g["summary"]["artifacts"]["modified"], 1)
        self.assertEqual(g["summary"]["artifacts"]["missing"], 1)

    def test_export_package_draft(self):
        self._analysis()
        code, out, _ = run_ag(["export", "--format", "package"], self.root)
        self.assertEqual(code, 0)
        d = json.loads((self.root / "analysis" / "whatran.draft.json").read_text())
        self.assertEqual(d["inputs"], ["data/raw.csv"])
        self.assertEqual(d["results_dir"], "results")
        self.assertEqual([s["id"] for s in d["steps"]], ["scale-x2-1", "scale-x10-1"])
        self.assertEqual(d["steps"][1]["script"], "scripts/step.py")
        self.assertEqual(d["steps"][1]["produces"], ["results/c.csv"])
        self.assertEqual(d["outputs"], [{"path": "c.csv", "compare": "exact"}])   # relative to results_dir
        self.assertEqual(d["method"]["findings"], ["x20 overall"])

    def test_replay_verified_marks_packaged_runs(self):
        self._analysis()
        run_ag(["export", "--format", "package"], self.root)
        shutil.copyfile(self.root / "analysis" / "whatran.draft.json", self.root / "whatran.json")
        code, _, err = run_ag(["replay-verified", "--reference", "ref"], self.root)
        self.assertEqual(code, 2)                       # no check.py yet
        self.assertIn("check.py not found", err)
        (self.root / "check.py").write_text("import sys\nprint('REPRODUCTION OK')\nsys.exit(0)\n")
        code, out, _ = run_ag(["replay-verified", "--reference", "ref"], self.root)
        self.assertEqual(code, 0, out)
        run_ag(["compile"], self.root)
        g = json.loads(self.p.graph_path.read_text())
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertTrue(nodes["run-scale-x2-1"]["verified_by_replay"])
        self.assertTrue(nodes["run-scale-x10-1"]["verified_by_replay"])
        self.assertFalse(nodes["run-scale-x3-1"]["verified_by_replay"])   # rejected branch is not packaged
        self.assertEqual(nodes["run-scale-x10-1"]["replay"]["source"], "local-check")
        self.assertEqual(g["summary"]["runs"]["replay_verified"], 2)
        (self.root / "check.py").write_text("import sys\nprint('REPRODUCTION FAILED')\nsys.exit(1)\n")
        code, _, err = run_ag(["replay-verified", "--reference", "ref"], self.root)
        self.assertEqual(code, 2)                       # a failed check records nothing
        self.assertEqual(sum(1 for e in self.p.events() if e.get("event") == "replay"), 1)
        code, _, _ = run_ag(["replay-verified", "--ci", "https://ci.example/run/1"], self.root)
        self.assertEqual(code, 0)

    def test_supersedes_marks_old_run(self):
        self._analysis()
        run_ag(["run", "--label", "Scale x2 fixed", "--in", "data/raw.csv", "--out", "results/a.csv",
                "--param", "k=2.5", "--supersedes", "run-scale-x2-1", "--",
                f"{sys.executable} scripts/step.py data/raw.csv results/a.csv 2.5"], self.root)
        run_ag(["compile"], self.root)
        g = json.loads(self.p.graph_path.read_text())
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(nodes["run-scale-x2-1"]["status"], "superseded")
        self.assertEqual(nodes["run-scale-x2-fixed-1"]["supersedes"], "run-scale-x2-1")
        a_versions = [n for n in g["nodes"] if n["type"] == "artifact" and n["path"] == "results/a.csv"]
        self.assertEqual(len(a_versions), 2)
        self.assertEqual(a_versions[1]["supersedes"], a_versions[0]["id"])

    def test_publish_copies_viewer(self):
        self._analysis()
        code, out, _ = run_ag(["publish", "--dest", "docs/analysis"], self.root)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "docs" / "analysis" / "index.html").is_file())
        self.assertTrue((self.root / "docs" / "analysis" / "graph.json").is_file())
        self.assertIn("github.io/<repo>/analysis/", out)


class TestIgnoredCommands(unittest.TestCase):
    def test_read_only_commands_are_ignored(self):
        cfg = ag.DEFAULT_CONFIG
        for c in ["ls -la", "cat results/x.csv | head", "grep -r foo .", "git status", "git log --oneline",
                  "python3 analysis/whatran.py record x", "FOO=1 ls", "cd results && ls"]:
            self.assertTrue(ag.is_ignored_command(c, cfg), c)
        for c in ["python scripts/step.py a b 2", "ls > listing.txt", "Rscript x.R", "cat a > b",
                  "cd results && python run.py", "git commit -m x", "make results"]:
            self.assertFalse(ag.is_ignored_command(c, cfg), c)


class TestHook(Base):
    def _hook(self, payload):
        r = subprocess.run([sys.executable, str(OBSERVE)], input=json.dumps(payload),
                           capture_output=True, text=True, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_hook_records_exec_with_changed_files_and_description(self):
        cmd = f"{sys.executable} scripts/step.py data/raw.csv results/out.csv 2"
        base = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                "tool_input": {"command": cmd, "description": "Scale the raw values by two"}, "tool_use_id": "toolu_1"}
        self._hook(dict(base, hook_event_name="PreToolUse"))
        subprocess.run(cmd, shell=True, cwd=self.root, check=True, capture_output=True)
        out = self._hook(dict(base, hook_event_name="PostToolUse", duration_ms=50,
                              tool_response={"stdout": "wrote results/out.csv\n", "stderr": "", "interrupted": False}))
        execs = ag.exec_events(self.p.events())
        self.assertEqual(len(execs), 1)
        e = execs[0]
        self.assertEqual(e["source"], "claude-code-hook")
        self.assertEqual(e["exit_code"], 0)
        self.assertEqual(e["session"], "s1")
        self.assertEqual(e["description"], "Scale the raw values by two")
        self.assertIn("results/out.csv", [f["path"] for f in e["files_changed"]])
        self.assertNotIn("analysis/events.jsonl", [f["path"] for f in e["files_changed"]])
        self.assertEqual(out, "")                       # silent by default: the agent has nothing to do
        self.assertFalse(list(self.p.state_dir.glob("*.json")))
        # the optional nudge names the execution and the changed files
        cfg = json.loads(self.p.config_path.read_text())
        cfg["observe"]["nudge"] = True
        self.p.config_path.write_text(json.dumps(cfg))
        subprocess.run(cmd.replace("out.csv", "out2.csv"), shell=True, cwd=self.root, check=True, capture_output=True)
        out = self._hook(dict(base, hook_event_name="PostToolUse", duration_ms=50, tool_use_id="toolu_1b",
                              tool_input={"command": cmd.replace("out.csv", "out2.csv")},
                              tool_response={"stdout": "", "stderr": "", "interrupted": False}))
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn("x-0002", ctx["additionalContext"])
        self.assertIn("results/out2.csv", ctx["additionalContext"])

    def test_hook_failure_event_parses_exit_code(self):
        base = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                "tool_input": {"command": "python3 scripts/missing.py"}, "tool_use_id": "toolu_2"}
        self._hook(dict(base, hook_event_name="PreToolUse"))
        self._hook(dict(base, hook_event_name="PostToolUseFailure", error="Exit code 2\nNo such file"))
        e = ag.exec_events(self.p.events())[0]
        self.assertEqual(e["exit_code"], 2)
        self.assertIn("No such file", e["stderr_head"])

    def test_hook_ignores_read_only_and_uninitialised(self):
        base = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                "tool_input": {"command": "ls -la"}, "tool_use_id": "toolu_3", "hook_event_name": "PostToolUse",
                "tool_response": {"stdout": "", "stderr": ""}}
        self._hook(base)
        self.assertEqual(ag.exec_events(self.p.events()), [])
        with tempfile.TemporaryDirectory() as other:
            out = self._hook(dict(base, cwd=other, tool_input={"command": "python x.py"}))
            self.assertEqual(out, "")

    def test_session_start_context(self):
        out = self._hook({"hook_event_name": "SessionStart", "cwd": str(self.root), "session_id": "s"})
        self.assertIn("record", json.loads(out)["hookSpecificOutput"]["additionalContext"])


class TestSimpleFlow(Base):
    """The everyday commands: record with everything inferred, claim, done, package, check."""

    def test_record_infers_everything_from_the_command(self):
        py = sys.executable
        run_ag(["run", "--", f"{py} scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3"], self.root)
        code, out, _ = run_ag(["record", "Scale by three", "--json"], self.root)
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        self.assertEqual(res["evidence"], "observed")
        self.assertEqual(res["paths"], {"inputs": ["data/raw.csv"], "outputs": ["results/b.csv"], "code": ["scripts/flagstep.py"]})
        self.assertEqual(res["params"], {"k": 3})
        self.assertEqual(set(res["inferred"]), {"inputs", "outputs", "code", "params"})
        node = ag.fold_nodes(self.p.events())[res["id"]]
        self.assertEqual(node["evidence"]["outputs_observed"], ["results/b.csv"])
        # explicit flags win over inference
        run_ag(["run", "--", f"{py} scripts/flagstep.py --src data/raw.csv --dst results/c.csv --k 4"], self.root)
        code, out, _ = run_ag(["record", "Scale by four", "--param", "factor=4", "--json"], self.root)
        res = json.loads(out)
        self.assertEqual(res["params"], {"factor": 4})
        self.assertNotIn("params", res["inferred"])
        self.assertEqual(res["paths"]["outputs"], ["results/c.csv"])

    def test_record_a_step_that_ran_before_recording_started(self):
        # the step ran earlier (nothing captured); the agent supplies the command and the outputs
        py = sys.executable
        subprocess.run(f"{py} scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3", shell=True, cwd=self.root, check=True)
        code, out, _ = run_ag(["record", "Scale by three", "--cmd", "python3 scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3",
                               "--out", "results/b.csv", "--json"], self.root)
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        self.assertEqual(res["evidence"], "declared")
        self.assertEqual(res["paths"], {"inputs": ["data/raw.csv"], "outputs": ["results/b.csv"], "code": ["scripts/flagstep.py"]})
        self.assertEqual(res["params"], {"k": 3})
        # it still packages and replays: declared, then replay-verified
        run_ag(["claim", "tripled"], self.root)
        run_ag(["package"], self.root)
        self.assertIn("--dst results/b.csv --k 3", (self.root / "run.sh").read_text())
        code, out, err = run_ag(["check"], self.root)
        self.assertEqual(code, 0, out + err)
        g = json.loads(self.p.graph_path.read_text())
        node = {n["id"]: n for n in g["nodes"]}["run-scale-by-three-1"]
        self.assertEqual(node["evidence"]["level"], "declared")
        self.assertTrue(node["verified_by_replay"])

    def test_record_with_nothing_captured_is_declared(self):
        code, out, _ = run_ag(["record", "Scale from memory", "--json"], self.root)
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["evidence"], "declared")
        self.assertEqual(res["paths"], {"inputs": [], "outputs": [], "code": []})

    def test_claim_defaults_to_the_last_completed_run(self):
        py = sys.executable
        run_ag(["run", "--label", "Scale", "--", f"{py} scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3"], self.root)
        code, out, _ = run_ag(["claim", "Values tripled", "--json"], self.root)
        res = json.loads(out)
        self.assertEqual(res["supported_by"], ["run-scale-1"])
        self.assertTrue(res["auto_supports"])

    def test_done_compiles_and_drafts(self):
        py = sys.executable
        run_ag(["run", "--label", "Scale", "--", f"{py} scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3"], self.root)
        run_ag(["claim", "Values tripled"], self.root)
        code, out, _ = run_ag(["done"], self.root)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.p.graph_path.is_file())
        draft = json.loads((self.root / "analysis" / "whatran.draft.json").read_text())
        self.assertEqual(draft["inputs"], ["data/raw.csv"])
        self.assertEqual(draft["outputs"], [{"path": "b.csv", "compare": "exact"}])
        self.assertIn("open the graph", out)

    def _two_step_analysis(self):
        py = sys.executable
        run_ag(["run", "--label", "Scale x2", "--", f"{py} scripts/flagstep.py --src data/raw.csv --dst results/a.csv --k 2"], self.root)
        run_ag(["run", "--label", "Scale by nine", "--", f"{py} scripts/flagstep.py --src data/raw.csv --dst results/dead.csv --k 9"], self.root)
        run_ag(["reject", "run-scale-by-nine-1", "--reason", "not needed"], self.root)
        run_ag(["run", "--label", "Scale x10", "--", f"{py} scripts/flagstep.py --src results/a.csv --dst results/c.csv --k 10"], self.root)
        run_ag(["claim", "x20 overall"], self.root)

    def test_package_generates_the_package_from_the_result_path(self):
        self._two_step_analysis()
        code, out, _ = run_ag(["package"], self.root)
        self.assertEqual(code, 0, out)
        for rel in ("whatran.json", "run.sh", "check.py", "build_site.py", ".github/workflows/reproduce.yml", "README.md"):
            self.assertTrue((self.root / rel).is_file(), rel)
        run_sh = (self.root / "run.sh").read_text()
        self.assertIn("--dst results/a.csv --k 2", run_sh)
        self.assertIn("--dst results/c.csv --k 10", run_sh)
        self.assertNotIn("dead.csv", run_sh)
        self.assertLess(run_sh.index("--k 2"), run_sh.index("--k 10"))
        self.assertEqual((self.root / "check.py").read_text(), ag.CHECK_PY)
        manifest = json.loads((self.root / "whatran.json").read_text())
        self.assertEqual([s["id"] for s in manifest["steps"]], ["scale-x2-1", "scale-x10-1"])
        self.assertIn("site/", (self.root / ".gitignore").read_text())
        self.assertTrue(ag.run_sh_is_generated(run_sh))
        self.assertTrue(ag.manifest_is_generated(manifest))
        # a second call changes nothing; an edited run.sh is kept
        code, out, _ = run_ag(["package"], self.root)
        self.assertNotIn("wrote", out)
        self.assertIn("kept    check.py", out)
        (self.root / "run.sh").write_text(run_sh + "echo done\n")
        code, out, _ = run_ag(["package"], self.root)
        self.assertIn("kept    run.sh  (edited", out)
        self.assertTrue((self.root / "run.sh").read_text().endswith("echo done\n"))

    def test_check_reruns_compares_and_marks_replay(self):
        self._two_step_analysis()
        run_ag(["package"], self.root)
        before = (self.root / "results" / "c.csv").read_bytes()
        code, out, err = run_ag(["check"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("CHECK PASSED", out)
        self.assertEqual((self.root / "results" / "c.csv").read_bytes(), before)   # references restored
        self.assertTrue((self.root / "site" / "index.html").is_file())
        g = json.loads(self.p.graph_path.read_text())
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertTrue(nodes["run-scale-x2-1"]["verified_by_replay"])
        self.assertTrue(nodes["run-scale-x10-1"]["verified_by_replay"])
        self.assertFalse(nodes["run-scale-by-nine-1"]["verified_by_replay"])
        self.assertEqual(g["summary"]["runs"]["replay_verified"], 2)
        # a changed method no longer matches: check fails and the references come back untouched
        (self.root / "scripts" / "flagstep.py").write_text((self.root / "scripts" / "flagstep.py").read_text().replace("v * a.k", "v * a.k + 1"))
        code, out, err = run_ag(["check"], self.root)
        self.assertEqual(code, 2, out + err)
        self.assertIn("differences", err)
        self.assertEqual((self.root / "results" / "c.csv").read_bytes(), before)
        self.assertEqual(sum(1 for e in self.p.events() if e.get("event") == "replay"), 1)

    def test_check_needs_a_package(self):
        self._two_step_analysis()
        code, _, err = run_ag(["check"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("package", err)


def hook(root, payload):
    r = subprocess.run([sys.executable, str(OBSERVE)], input=json.dumps(payload),
                       capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr
    return r.stdout


def transcript_entry(role, text=None, tool=None, tool_result=None, msg_id=None):
    """One Claude Code transcript line (compact JSON, as the app writes it)."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        tool_id, command, description = tool
        content.append({"type": "tool_use", "id": tool_id, "name": "Bash",
                        "input": {"command": command, "description": description}})
    if tool_result:
        content.append({"type": "tool_result", "tool_use_id": tool_result, "content": "ok"})
    msg = {"role": role, "content": content}
    if role == "assistant":
        msg["id"] = msg_id or f"msg_{abs(hash(text or tool_result or ''))}"
    return {"type": role, "isSidechain": False, "message": msg}


def write_transcript(path, entries):
    path.write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in entries) + "\n")


class TestAutomatic(Base):
    """Nothing is required of the agent: captured executions become steps, and the end of
    a turn compiles the graph, refreshes the package and saves the agent's report."""

    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()                 # transcripts live outside the project
        self.transcript = Path(self.home.name) / "transcript.jsonl"
        self.py = sys.executable

    def tearDown(self):
        self.home.cleanup()
        super().tearDown()

    def _bash(self, command, tool_id, description=None, transcript=True):
        base = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                "tool_input": {"command": command, **({"description": description} if description else {})},
                "tool_use_id": tool_id}
        if transcript:
            base["transcript_path"] = str(self.transcript)
        hook(self.root, dict(base, hook_event_name="PreToolUse"))
        r = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True)
        event = "PostToolUse" if r.returncode == 0 else "PostToolUseFailure"
        extra = ({"tool_response": {"stdout": r.stdout, "stderr": r.stderr, "interrupted": False}}
                 if r.returncode == 0 else {"error": f"Exit code {r.returncode}\n{r.stderr}"})
        return hook(self.root, dict(base, hook_event_name=event, duration_ms=50, **extra))

    def _stop(self):
        return hook(self.root, {"hook_event_name": "Stop", "cwd": str(self.root), "session_id": "s1",
                                "transcript_path": str(self.transcript), "stop_hook_active": False})

    def _flag(self, dst, k):
        return f"{self.py} scripts/flagstep.py --src data/raw.csv --dst results/{dst} --k {k}"

    def test_hook_keeps_the_agents_words(self):
        c1, c2 = self._flag("b.csv", 3), self._flag("c.csv", 4)
        write_transcript(self.transcript, [
            transcript_entry("user", "please scale the data"),
            transcript_entry("assistant", "I'll scale the values by three first.", msg_id="m1"),
            transcript_entry("assistant", tool=("toolu_1", c1, "Scale raw values by three"), msg_id="m1"),
            transcript_entry("user", tool_result="toolu_1"),
            transcript_entry("assistant", "Done. Now the same by four.", tool=("toolu_2", c2, None), msg_id="m2"),
        ])
        self._bash(c1, "toolu_1", "Scale raw values by three")
        self._bash(c2, "toolu_2")
        e1, e2 = ag.exec_events(self.p.events())
        self.assertEqual(e1["description"], "Scale raw values by three")
        self.assertEqual(e1["intent"], "I'll scale the values by three first.")
        self.assertNotIn("description", e2)
        self.assertEqual(e2["intent"], "Done. Now the same by four.")
        # the person's words are never taken
        self.assertNotIn("please", json.dumps(self.p.events()))
        # switched off per project
        cfg = json.loads(self.p.config_path.read_text())
        cfg["observe"]["intent"] = False
        self.p.config_path.write_text(json.dumps(cfg))
        self._bash(self._flag("d.csv", 5), "toolu_2")
        self.assertNotIn("intent", ag.exec_events(self.p.events())[-1])

    def test_labels_come_from_the_agents_words_or_the_command(self):
        self.assertEqual(ag.label_from_words("Scale raw values by three", self.p), "Scale raw values by three")
        self.assertEqual(ag.label_from_words("Running the scaling script now", self.p), "Run the scaling script now")
        self.assertEqual(ag.label_from_words("Good. Next, let me fit the model to the clean table.", self.p),
                         "Fit the model to the clean table")
        self.assertEqual(ag.label_from_words("I'll compare groups with a Welch t-test and then plot.", self.p),
                         "Compare groups with a Welch t-test")
        self.assertIsNone(ag.label_from_words("The stats look fine.", self.p))
        self.assertIsNone(ag.label_from_words("现在跑统计", self.p))
        ev = {"command": f"{self.py} scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3", "cwd": ".",
              "files_changed": [{"path": "results/b.csv"}]}
        self.assertEqual(ag.auto_label(self.p, ev), ("Run flagstep.py", "command"))
        self.assertEqual(ag.auto_label(self.p, dict(ev, intent="Now scale the values by three.")), ("Scale the values by three", "intent"))
        self.assertEqual(ag.auto_label(self.p, dict(ev, description="scale values", intent="x")), ("Scale values", "description"))
        self.assertEqual(ag.auto_label(self.p, dict(ev, description="Quick sanity pass over the table")),
                         ("Quick sanity pass over the table", "description"))

    def test_done_turns_executions_into_steps(self):
        run_ag(["run", "--", self._flag("b.csv", 3)], self.root)                      # a step: result file produced
        run_ag(["run", "--", f"{self.py} -c \"open('scripts/new.py','w').write('x')\""], self.root)   # authoring a script
        run_ag(["run", "--", f"{self.py} -c \"open('results/bad.csv','w').write('x'); raise SystemExit(1)\""], self.root)  # failed
        run_ag(["run", "--", "mkdir -p results/sub && cp data/raw.csv results/sub/raw.csv"], self.root)   # housekeeping
        code, out, err = run_ag(["done"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn('recorded run-run-flagstep-py-1  "Run flagstep.py"  (label from the command)', out)
        g = json.loads(self.p.graph_path.read_text())
        runs = [n for n in g["nodes"] if n["type"] == "run"]
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["auto"], {"label_from": "command"})
        self.assertEqual(r["evidence"]["level"], "observed")
        self.assertEqual(r["params"], {"k": 3})
        self.assertEqual(g["summary"]["executions"], {"total": 4, "linked": 1})
        self.assertTrue((self.root / "whatran.json").is_file())
        self.assertIn("--k 3", (self.root / "run.sh").read_text())
        # repeatable: nothing new on a second pass
        n = len(self.p.events())
        run_ag(["done"], self.root)
        self.assertEqual(len(self.p.events()), n)

    def test_redo_supersedes_and_exact_repeat_attaches(self):
        run_ag(["run", "--", self._flag("b.csv", 3)], self.root)
        run_ag(["done", "--no-package"], self.root)
        run_ag(["run", "--", self._flag("b.csv", 4)], self.root)             # same file, other parameter: a redo
        code, out, _ = run_ag(["done", "--no-package"], self.root)
        self.assertIn("supersedes run-run-flagstep-py-1", out)
        nodes = ag.fold_nodes(self.p.events())
        self.assertEqual(nodes["run-run-flagstep-py-1"]["status"], "superseded")
        self.assertEqual(nodes["run-run-flagstep-py-2"]["supersedes"], "run-run-flagstep-py-1")
        self.assertEqual(len([n for n in nodes.values() if n["type"] == "artifact" and n["path"] == "results/b.csv"]), 2)
        run_ag(["run", "--", self._flag("b.csv", 4)], self.root)             # identical rerun: same command, same bytes
        code, out, _ = run_ag(["done", "--no-package"], self.root)
        self.assertIn("attached x-0003 to run-run-flagstep-py-2", out)
        nodes = ag.fold_nodes(self.p.events())
        self.assertEqual(len([n for n in nodes.values() if n["type"] == "run"]), 2)
        self.assertEqual(nodes["run-run-flagstep-py-2"]["evidence"]["exec_ids"], ["x-0002", "x-0003"])
        # a replay of the whole pipeline under another command only regenerates known files: not a step
        run_ag(["run", "--", f"bash -c '{self._flag('b.csv', 4)}'"], self.root)
        run_ag(["done", "--no-package"], self.root)
        g = json.loads(self.p.graph_path.read_text())
        self.assertEqual(g["summary"]["runs"]["total"], 2)
        self.assertEqual(g["summary"]["executions"], {"total": 4, "linked": 3})
        self.assertEqual(g["main_path"].count("run-run-flagstep-py-2"), 1)
        self.assertNotIn("run-run-flagstep-py-1", g["main_path"])

    def test_rewriting_an_input_in_place_is_a_new_step(self):
        run_ag(["run", "--label", "Scale x2", "--", f"{self.py} scripts/step.py data/raw.csv results/a.csv 2"], self.root)
        run_ag(["run", "--label", "Scale again", "--in", "results/a.csv", "--",
                f"{self.py} scripts/step.py results/a.csv results/a.csv 10"], self.root)
        nodes = ag.fold_nodes(self.p.events())
        self.assertEqual(nodes["run-scale-x2-1"]["status"], "completed")
        self.assertNotIn("supersedes", nodes["run-scale-again-1"])

    def test_stop_hook_finishes_the_turn(self):
        c1 = self._flag("b.csv", 3)
        entries = [
            transcript_entry("user", "scale the data by three and report"),
            transcript_entry("assistant", "Scaling the values by three.", tool=("toolu_1", c1, None), msg_id="m1"),
            transcript_entry("user", tool_result="toolu_1"),
            transcript_entry("assistant", "Done. The values tripled; see results/b.csv.", msg_id="m2"),
        ]
        write_transcript(self.transcript, entries)
        self._bash(c1, "toolu_1")
        out = self._stop()
        self.assertIn("package written", json.loads(out)["systemMessage"])
        g = json.loads(self.p.graph_path.read_text())
        runs = [n for n in g["nodes"] if n["type"] == "run"]
        self.assertEqual([r["label"] for r in runs], ["Scale the values by three"])
        self.assertEqual(runs[0]["auto"], {"label_from": "intent"})
        self.assertEqual(runs[0]["intent"], "Scaling the values by three.")
        report = (self.root / "analysis" / "report.md").read_text()
        self.assertTrue(report.startswith("<!-- whatran:"))
        self.assertIn("The values tripled; see results/b.csv.", report)
        self.assertNotIn("scale the data by three and report", report)       # the person's words stay out
        for rel in ("whatran.json", "run.sh", "check.py", ".github/workflows/reproduce.yml"):
            self.assertTrue((self.root / rel).is_file(), rel)
        self.assertEqual(json.loads((self.root / "whatran.json").read_text())["steps"][0]["title"], "Scale the values by three")
        # a second Stop with nothing new: silent, nothing rewritten
        n_events = len(self.p.events())
        stamp = (self.root / "analysis" / "report.md").stat().st_mtime_ns
        self.assertEqual(self._stop(), "")
        self.assertEqual(len(self.p.events()), n_events)
        self.assertEqual((self.root / "analysis" / "report.md").stat().st_mtime_ns, stamp)
        # small talk does not replace the report; a later summary that names a result does
        entries += [transcript_entry("user", "what day is it"), transcript_entry("assistant", "Thursday.", msg_id="m3")]
        write_transcript(self.transcript, entries)
        self._stop()
        self.assertIn("The values tripled", (self.root / "analysis" / "report.md").read_text())
        entries += [transcript_entry("user", "summarise"),
                    transcript_entry("assistant", "Summary: results/b.csv holds the tripled values.", msg_id="m4")]
        write_transcript(self.transcript, entries)
        self._stop()
        self.assertIn("Summary: results/b.csv", (self.root / "analysis" / "report.md").read_text())
        # the analysis continues: a new step refreshes the untouched manifest and run.sh
        c2 = f"{self.py} scripts/flagstep.py --src results/b.csv --dst results/c.csv --k 10"
        entries += [transcript_entry("user", "now scale that by ten"),
                    transcript_entry("assistant", tool=("toolu_2", c2, "Scale the tripled values by ten"), msg_id="m5"),
                    transcript_entry("user", tool_result="toolu_2"),
                    transcript_entry("assistant", "results/c.csv is ready.", msg_id="m6")]
        write_transcript(self.transcript, entries)
        now = time.time()
        os.utime(self.root / "results" / "b.csv", (now, now))               # as if c1 had only just finished
        self._bash(c2, "toolu_2", "Scale the tripled values by ten")
        (self.root / "run.sh").write_text((self.root / "run.sh").read_text() + "# edited by hand\n")
        self.assertEqual(self._stop(), "")                                    # the package already exists: no message
        manifest = json.loads((self.root / "whatran.json").read_text())
        self.assertEqual([s["title"] for s in manifest["steps"]], ["Scale the values by three", "Scale the tripled values by ten"])
        self.assertIn("# edited by hand", (self.root / "run.sh").read_text())   # the edited script is kept
        self.assertNotIn("--k 10", (self.root / "run.sh").read_text())
        # and the whole thing still checks
        code, out, err = run_ag(["package", "--force"], self.root)
        code, out, err = run_ag(["check"], self.root)
        self.assertEqual(code, 0, out + err)
        g = json.loads(self.p.graph_path.read_text())
        self.assertEqual(g["summary"]["runs"]["replay_verified"], 2)

    def test_the_packages_own_files_are_never_evidence(self):
        # the Stop hook writes the package moments before the next command starts. Neither the
        # exact diff (PreToolUse seen) nor the mtime window (PreToolUse missed) may claim them.
        write_transcript(self.transcript, [])
        self._bash(self._flag("b.csv", 3), "toolu_1")
        self._stop()
        pkg = ("whatran.json", "run.sh", "check.py", "build_site.py",
               ".github/workflows/reproduce.yml", "README.md", ".gitignore")
        now = time.time()
        os.utime(self.root / "results" / "b.csv", (now - 10, now - 10))
        for rel in pkg:
            self.assertTrue((self.root / rel).is_file(), rel)
            os.utime(self.root / rel, (now, now))
        self._bash(self._flag("c.csv", 4), "toolu_2")
        files = {f["path"] for f in ag.exec_events(self.p.events())[-1]["files_changed"]}
        self.assertEqual(files, {"results/c.csv"})
        # the same when the hook only learns of the command afterwards (start time estimated)
        now = time.time()
        for rel in ("results/b.csv", "results/c.csv"):
            os.utime(self.root / rel, (now - 10, now - 10))
        for rel in pkg:
            os.utime(self.root / rel, (now, now))
        c3 = self._flag("d.csv", 5)
        subprocess.run(c3, shell=True, cwd=self.root, check=True, capture_output=True)
        hook(self.root, {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                         "tool_input": {"command": c3}, "tool_use_id": "toolu_3",
                         "hook_event_name": "PostToolUse", "duration_ms": 50,
                         "tool_response": {"stdout": "", "stderr": "", "interrupted": False}})
        files = {f["path"] for f in ag.exec_events(self.p.events())[-1]["files_changed"]}
        self.assertEqual(files, {"results/d.csv"})
        self.assertEqual(self._stop(), "")
        code, out, err = run_ag(["check"], self.root)
        self.assertEqual(code, 0, out + err)

    def test_stop_hook_is_silent_without_a_recording(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(hook(other, {"hook_event_name": "Stop", "cwd": other, "session_id": "s"}), "")
        self.assertEqual(self._stop(), "")                                    # initialised, nothing ran yet
        self.assertFalse(self.p.graph_path.exists())
        self.assertFalse((self.root / "whatran.json").exists())


class TestEmbeddedAssets(unittest.TestCase):
    def test_embedded_assets_match_the_skill_assets(self):
        assets = ROOT / "plugins/whatran/skills/package-analysis/assets"
        self.assertEqual(ag.CHECK_PY, (assets / "check.py").read_text())
        self.assertEqual(ag.REPRODUCE_YML, (assets / "reproduce.yml").read_text())
        self.assertEqual(ag.BUILD_SITE_PY, (assets / "build_site_from_graph.py").read_text())
        self.assertEqual(ag.VIEWER_HTML, (ROOT / "plugins/whatran/viewer/index.html").read_text())

    def test_help_lists_the_everyday_commands_only(self):
        parser = ag.build_parser()
        text = parser.format_help()
        for cmd in ("init", "record", "claim", "reject", "done", "open", "package", "check", "run"):
            self.assertIn(f"  {cmd} ", text)
        self.assertIn("more commands", text)
        self.assertNotIn("\n    replay-verified", text)   # hidden from the command list, present in the epilog


class TestStyle(Base):
    """Labels are verb-first and short; claims, notes and reasons are short, plain sentences."""

    def test_label_rules(self):
        self.assertEqual(ag.check_label("Detect peaks"), [])
        self.assertEqual(ag.check_label("Compare groups with Welch t-test"), [])
        probs = ag.check_label("Peak detection")
        self.assertTrue(any("start with an approved verb" in x and "Detect peaks" in x for x in probs), probs)
        self.assertTrue(any("10 words" in x for x in ag.check_label("Fit a model to the data with ten words here")))
        self.assertTrue(any("avoid" in x for x in ag.check_label("Fit the model we built")))
        self.assertTrue(any("full stop" in x for x in ag.check_label("Fit model.")))

    def test_sentence_rules(self):
        self.assertEqual(ag.check_sentences("Feature at m/z 301.1 is higher in group B (Welch p < 0.05)"), [])
        self.assertTrue(any('"was"' in x for x in ag.check_sentences("The peak was detected")))
        self.assertTrue(any("21 words" in x for x in ag.check_sentences(" ".join(["word"] * 21))))
        self.assertTrue(any('"do" instead of "perform"' in x for x in ag.check_sentences("Perform the test")))

    def test_cli_warns_and_strict_refuses(self):
        # the record is accepted; the warning names the fix
        code, out, err = run_ag(["record", "Peak detection"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("warning: style:", out)
        self.assertIn("Detect peaks", out)
        self.assertIn("run-peak-detection-1", ag.fold_nodes(self.p.events()))
        code, out, err = run_ag(["record", "Peak detection", "--no-style"], self.root)
        self.assertEqual(code, 0, err)
        self.assertNotIn("style", out)
        code, out, _ = run_ag(["claim", "We think it is probably higher"], self.root)
        self.assertEqual(code, 0)
        self.assertIn("warning: style:", out)
        code, out, _ = run_ag(["reject", "run-peak-detection-1", "--reason", "It was perhaps wrong"], self.root)
        self.assertEqual(code, 0)
        self.assertIn("warning: style:", out)
        self.assertEqual(ag.fold_nodes(self.p.events())["run-peak-detection-1"]["status"], "rejected")
        code, out, _ = run_ag(["reject", "run-peak-detection-2", "--reason", "Threshold calls noise as peaks"], self.root)
        self.assertEqual(code, 0)
        self.assertNotIn("warning", out)
        # strict projects refuse instead
        cfg = json.loads(self.p.config_path.read_text())
        cfg["style"] = {"strict": True}
        self.p.config_path.write_text(json.dumps(cfg))
        code, _, err = run_ag(["record", "Peak detection"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("writing rules", err)
        code, _, err = run_ag(["record", "Peak detection", "--no-style"], self.root)
        self.assertEqual(code, 0, err)

    def test_project_can_extend_replace_or_disable_the_verbs(self):
        cfg = json.loads(self.p.config_path.read_text())
        cfg["style"] = {"verbs": ["deconvolve"]}
        self.p.config_path.write_text(json.dumps(cfg))
        self.assertEqual(ag.check_label("Deconvolve spectra", ag.Project(self.root)), [])
        (self.root / "verbs.txt").write_text("frobnicate\n")
        cfg["style"] = {"verbs_file": "verbs.txt"}
        self.p.config_path.write_text(json.dumps(cfg))
        self.assertEqual(ag.check_label("Frobnicate data", ag.Project(self.root)), [])
        self.assertTrue(ag.check_label("Detect peaks", ag.Project(self.root)))      # the file replaces the built-in list
        cfg["style"] = {"enabled": False}
        self.p.config_path.write_text(json.dumps(cfg))
        code, out, _ = run_ag(["record", "Peak detection"], self.root)
        self.assertEqual(code, 0)
        self.assertNotIn("style", out)


class TestAPI(Base):
    """import whatran as w: record, step (context + decorator), claim, edit, remove, run."""

    def setUp(self):
        super().setUp()
        self.old = os.getcwd()
        os.chdir(self.root)
        ag.use(str(self.root))

    def tearDown(self):
        os.chdir(self.old)
        ag._API_ROOT = None
        super().tearDown()

    def test_step_context_records_changed_files(self):
        with ag.step("Scale values", params={"k": 2}, inputs=["data/raw.csv"]) as s:
            vals = [float(l) for l in open("data/raw.csv").read().split()[1:]]
            open("results/a.csv", "w").write("x\n" + "\n".join(str(v * 2) for v in vals) + "\n")
        self.assertTrue(s.id.startswith("run-scale-values"))
        nodes = ag.fold_nodes(self.p.events())
        node = nodes[s.id]
        self.assertEqual(node["evidence"]["level"], "observed")
        self.assertEqual(node["evidence"]["source"], "step")
        self.assertEqual(node["params"], {"k": 2})
        self.assertEqual([nodes[o]["path"] for o in node["outputs"]], ["results/a.csv"])
        self.assertEqual([nodes[i]["path"] for i in node["inputs"]], ["data/raw.csv"])
        self.assertEqual(node["code"], [])             # the test runner lives outside the project

    def test_step_failure_is_recorded_as_failed(self):
        with self.assertRaises(ValueError):
            with ag.step("Scale values") as s:
                raise ValueError("boom")
        self.assertEqual(ag.fold_nodes(self.p.events())[s.id]["status"], "failed")

    def test_decorator_captures_named_params_and_the_script(self):
        script = self.root / "scripts" / "pipeline.py"
        script.write_text(
            "import whatran as w\n"
            "@w.step('Scale values', params=['k'])\n"
            "def scale(src, dst, k=3):\n"
            "    vals = [float(l) for l in open(src).read().split()[1:]]\n"
            "    open(dst, 'w').write('x\\n' + '\\n'.join(str(v * k) for v in vals) + '\\n')\n"
            "scale('data/raw.csv', 'results/b.csv', k=4)\n"
            "print(scale.last_step.id)\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "plugins/whatran/bin"))
        r = subprocess.run([sys.executable, str(script)], cwd=self.root, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        node = ag.fold_nodes(self.p.events())[r.stdout.strip()]
        self.assertEqual(node["params"], {"k": 4})
        self.assertEqual([c["path"] for c in node["code"]], ["scripts/pipeline.py"])

    def test_record_claim_edit_remove_and_style(self):
        ag.run(f"{sys.executable} scripts/flagstep.py --src data/raw.csv --dst results/b.csv --k 3")
        rid = ag.record("Scale by three")
        cid = ag.claim("Values tripled", supports=[rid])
        ag.edit(rid, label="Scale values by three", params={"method": "flag"})
        node = ag.fold_nodes(self.p.events())[rid]
        self.assertEqual(node["label"], "Scale values by three")
        self.assertEqual(node["params"], {"k": 3, "method": "flag"})
        ag.remove(cid, reason="duplicate")
        nodes = ag.fold_nodes(self.p.events())
        self.assertNotIn(cid, nodes)
        self.assertIn(rid, nodes)
        self.assertEqual(sum(1 for e in self.p.events() if e.get("event") == "retract"), 1)
        self.assertEqual(ag.build_graph(self.p)["summary"]["retracted"], 1)
        err = io.StringIO()
        with redirect_stderr(err):
            rid2 = ag.record("Peak detection")
            ag.claim("We think so")
            ag.record("Peak detection", style=False)
        self.assertIn(rid2, ag.fold_nodes(self.p.events()))
        self.assertEqual(err.getvalue().count("whatran: style:"), 2)
        os.environ["WHATRAN_STYLE_STRICT"] = "1"
        try:
            with self.assertRaises(ag.StyleError):
                ag.record("Peak detection")
        finally:
            del os.environ["WHATRAN_STYLE_STRICT"]


class TestBeginEnd(Base):
    def test_begin_end_from_the_shell(self):
        code, out, err = run_ag(["begin", "Scale values", "--code", "scripts/step.py"], self.root)
        self.assertEqual(code, 0, err)
        token = out.strip()
        subprocess.run([sys.executable, "scripts/step.py", "data/raw.csv", "results/a.csv", "2"],
                       cwd=self.root, check=True, capture_output=True)
        code, out, err = run_ag(["end", token, "--param", "k=2", "--in", "data/raw.csv", "--json"], self.root)
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["paths"], {"inputs": ["data/raw.csv"], "outputs": ["results/a.csv"], "code": ["scripts/step.py"]})
        self.assertFalse(list(self.p.state_dir.glob("step-*.json")))
        code, _, _ = run_ag(["end", token], self.root)
        self.assertEqual(code, 2)
        code, out, err = run_ag(["begin", "Peak detection"], self.root)
        self.assertEqual(code, 0, err)
        code, out, _ = run_ag(["end", out.strip()], self.root)
        self.assertEqual(code, 0)
        self.assertIn("warning: style:", out)           # the label's problems surface when the step closes


@unittest.skipUnless(shutil.which("Rscript"), "Rscript not installed")
class TestRShim(Base):
    def test_r_step_and_claim(self):
        rfile = ROOT / "r" / "whatran" / "R" / "whatran.R"
        script = self.root / "analysis_r.R"
        script.write_text(
            f'source("{rfile}")\n'
            'id <- whatran_step("Scale values", {\n'
            '  v <- as.numeric(readLines("data/raw.csv")[-1])\n'
            '  writeLines(c("x", v * 2), "results/r.csv")\n'
            '}, params = list(k = 2), inputs = "data/raw.csv")\n'
            'cid <- whatran_claim("Values doubled", supports = id)\n'
            'cat(id, cid, sep = "\\n")\n')
        env = dict(os.environ, WHATRAN_CLI=f"{sys.executable} {ROOT / 'plugins/whatran/bin/whatran.py'}")
        r = subprocess.run(["Rscript", str(script)], cwd=self.root, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rid, cid = r.stdout.strip().splitlines()[-2:]
        nodes = ag.fold_nodes(self.p.events())
        self.assertEqual(nodes[rid]["params"], {"k": 2})
        self.assertEqual(nodes[cid]["supported_by"], [rid])
        self.assertEqual([nodes[o]["path"] for o in nodes[rid]["outputs"]], ["results/r.csv"])
        self.assertEqual([nodes[i]["path"] for i in nodes[rid]["inputs"]], ["data/raw.csv"])


if __name__ == "__main__":
    unittest.main()
