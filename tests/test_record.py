"""Tests for bin/whatran.py and hooks/observe.py.  Run: python3 -m unittest discover tests"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

    def test_hook_records_exec_with_changed_files_and_nudges(self):
        cmd = f"{sys.executable} scripts/step.py data/raw.csv results/out.csv 2"
        base = {"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                "tool_input": {"command": cmd}, "tool_use_id": "toolu_1"}
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
        self.assertIn("results/out.csv", [f["path"] for f in e["files_changed"]])
        self.assertNotIn("analysis/events.jsonl", [f["path"] for f in e["files_changed"]])
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn(e["id"], ctx["additionalContext"])
        self.assertFalse(list(self.p.state_dir.glob("*.json")))

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
        # second call keeps existing files
        code, out, _ = run_ag(["package"], self.root)
        self.assertIn("kept    run.sh", out)

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

    def test_cli_enforces_and_no_style_bypasses(self):
        code, _, err = run_ag(["record", "Peak detection"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("writing rules", err)
        self.assertIn("Detect peaks", err)
        code, _, err = run_ag(["record", "Peak detection", "--no-style"], self.root)
        self.assertEqual(code, 0, err)
        code, _, err = run_ag(["claim", "We think it is probably higher"], self.root)
        self.assertEqual(code, 2)
        code, _, err = run_ag(["reject", "run-peak-detection-1", "--reason", "It was perhaps wrong"], self.root)
        self.assertEqual(code, 2)
        code, _, err = run_ag(["reject", "run-peak-detection-1", "--reason", "Threshold calls noise as peaks"], self.root)
        self.assertEqual(code, 0, err)

    def test_project_can_extend_replace_or_disable_the_verbs(self):
        cfg = json.loads(self.p.config_path.read_text())
        cfg["style"] = {"verbs": ["deconvolve"]}
        self.p.config_path.write_text(json.dumps(cfg))
        code, _, err = run_ag(["record", "Deconvolve spectra"], self.root)
        self.assertEqual(code, 0, err)
        (self.root / "verbs.txt").write_text("frobnicate\n")
        cfg["style"] = {"verbs_file": "verbs.txt"}
        self.p.config_path.write_text(json.dumps(cfg))
        code, _, _ = run_ag(["record", "Frobnicate data"], self.root)
        self.assertEqual(code, 0)
        code, _, _ = run_ag(["record", "Detect peaks"], self.root)
        self.assertEqual(code, 2)                      # the file replaces the built-in list
        cfg["style"] = {"enabled": False}
        self.p.config_path.write_text(json.dumps(cfg))
        code, _, _ = run_ag(["record", "Peak detection"], self.root)
        self.assertEqual(code, 0)


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
        with self.assertRaises(ag.StyleError):
            ag.record("Peak detection")
        with self.assertRaises(ag.StyleError):
            ag.claim("We think so")
        ag.record("Peak detection", style=False)


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
        code, _, err = run_ag(["begin", "Peak detection"], self.root)
        self.assertEqual(code, 2)


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
