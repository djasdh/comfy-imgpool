"""Regression tests for declarative workflows (issue #8)."""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMGGEN = ROOT / "bin" / "imggen"
FIXTURE = ROOT / "tests" / "fixtures" / "moss_1024_graph.json"


def _load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run_imggen(*args):
    proc = subprocess.run(
        [sys.executable, str(IMGGEN), *args],
        capture_output=True, text=True, cwd=str(ROOT))
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    payload = json.loads(lines[-1]) if lines else None
    return proc, payload


class WorkflowCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imggen = _load_module("_imggen_workflow_test", IMGGEN)

    def test_each_model_exposes_its_bound_workflows(self):
        models = self.imggen.catalog_models()
        by_model = {m["model"]: m for m in models}
        self.assertIn("moss-1024", [w["id"] for w in by_model["moss"]["workflows"]])
        for model, entry in by_model.items():
            for wf in entry["workflows"]:
                self.assertEqual(wf["model"], model)
                for key in ("id", "display", "est_seconds", "risky"):
                    self.assertIn(key, wf)

    def test_special_entrypoints_are_discoverable(self):
        ids = {w["id"] for w in self.imggen.catalog_workflows()}
        self.assertIn("moss-edit", ids)
        self.assertIn("krea2-inpaint", ids)
        self.assertIn("krea2-anypaint", ids)


class WorkflowCliTest(unittest.TestCase):
    def test_conflicting_model_is_rejected_with_bad_workflow_binding(self):
        proc, payload = _run_imggen(
            "--dry", "--workflow", "krea2-anypaint", "-m", "anima", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "bad_workflow_binding")
        self.assertIn("krea2", payload["message"])

    def test_override_outside_allowlist_is_rejected(self):
        proc, payload = _run_imggen(
            "--dry", "--workflow", "moss-1024", "--denoise", "0.8", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "bad_override")

    def test_override_above_range_is_rejected(self):
        proc, payload = _run_imggen(
            "--dry", "--workflow", "moss-1024", "--steps", "50", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "bad_override")

    def test_workflow_dry_graph_matches_handwritten_fixture(self):
        proc, payload = _run_imggen(
            "--dry", "--workflow", "moss-1024", "--seed", "42",
            "--out", "workflow_test", "a cat")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload["workflow"], "moss-1024")
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["graph"], expected["graph"])

    def test_legacy_call_dry_graph_is_unchanged(self):
        proc, payload = _run_imggen(
            "--dry", "-m", "moss", "--seed", "42",
            "--out", "workflow_test", "a cat")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("workflow", payload)
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["graph"], expected["graph"])


class WorkflowHttpValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imgpool = _load_module("_imgpool_workflow_test", ROOT / "bin" / "imgpool")

    def test_structured_workflow_infers_model(self):
        prepared = self.imgpool._prepare_submission({
            "workflow": "moss-1024", "prompt": "probe cup"})
        self.assertEqual(prepared["model"], "moss")
        self.assertEqual(prepared["workflow"], "moss-1024")
        self.assertIn("--workflow", prepared["args"])

    def test_structured_workflow_model_conflict(self):
        with self.assertRaises(self.imgpool.SubmissionError) as ctx:
            self.imgpool._prepare_submission({
                "workflow": "moss-1024", "model": "krea2",
                "prompt": "probe cup"})
        self.assertEqual(ctx.exception.payload["error"],
                         "bad_workflow_binding")

    def test_structured_workflow_unknown(self):
        with self.assertRaises(self.imgpool.SubmissionError) as ctx:
            self.imgpool._prepare_submission({
                "workflow": "does-not-exist", "prompt": "probe cup"})
        self.assertEqual(ctx.exception.payload["error"], "bad_workflow")

    def test_structured_workflow_bad_override(self):
        with self.assertRaises(self.imgpool.SubmissionError) as ctx:
            self.imgpool._prepare_submission({
                "workflow": "moss-1024", "steps": 50,
                "prompt": "probe cup"})
        self.assertEqual(ctx.exception.payload["error"], "bad_override")


if __name__ == "__main__":
    unittest.main()
