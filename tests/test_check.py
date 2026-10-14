from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "plugins/whatran/skills/package-analysis/assets/check.py"
COPIES = [
    ROOT / "examples/pipeline-demo/check.py",
    ROOT / "examples/cancer-classification/check.py",
    ROOT / "examples/multi-analysis-demo/analyses/group-means/check.py",
    ROOT / "examples/multi-analysis-demo/analyses/pass-rates/check.py",
]


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copy2(CHECK, self.root / "check.py")
        (self.root / "results").mkdir()
        (self.root / "reference").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "check.py", "results", "reference"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_manifest(self, outputs: list[dict[str, object]]) -> None:
        (self.root / "whatran.json").write_text(
            json.dumps({"outputs": outputs}), encoding="utf-8"
        )

    def test_declared_comparisons_pass(self) -> None:
        self.write_manifest(
            [
                {"path": "metrics.json", "compare": "numeric", "tolerance": 1e-6},
                {"path": "table.csv", "compare": "exact"},
                {"path": "plot.svg", "compare": "exists"},
            ]
        )
        (self.root / "results/metrics.json").write_text('{"score": 0.5000001}')
        (self.root / "reference/metrics.json").write_text('{"score": 0.5}')
        (self.root / "results/table.csv").write_text("x\n1\n")
        (self.root / "reference/table.csv").write_text("x\n1\n")
        (self.root / "results/plot.svg").write_text("<svg/>")
        (self.root / "reference/plot.svg").write_text("<svg/>")

        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REPRODUCTION OK", result.stdout)

    def test_unsafe_output_path_fails(self) -> None:
        self.write_manifest([{"path": "../answer.json", "compare": "exact"}])

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must stay inside results_dir", result.stdout)

    def test_empty_exists_output_fails(self) -> None:
        self.write_manifest([{"path": "plot.svg", "compare": "exists"}])
        (self.root / "results/plot.svg").touch()
        (self.root / "reference/plot.svg").write_text("<svg/>")

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a non-empty file", result.stdout)

    def test_unknown_comparison_fails(self) -> None:
        self.write_manifest([{"path": "answer.txt", "compare": "approximate"}])
        (self.root / "results/answer.txt").write_text("42")
        (self.root / "reference/answer.txt").write_text("42")

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported comparison type", result.stdout)

    def test_example_checkers_match_asset(self) -> None:
        expected = CHECK.read_bytes()
        for copy in COPIES:
            with self.subTest(copy=copy):
                self.assertEqual(copy.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
