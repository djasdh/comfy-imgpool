"""Regression tests for accepting Hermes' aspect enum at /submit."""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_imgpool():
    loader = importlib.machinery.SourceFileLoader(
        "_imgpool_aspect_test", str(ROOT / "bin" / "imgpool"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SubmitAspectAliasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imgpool = _load_imgpool()

    def test_enum_aliases_are_normalized_before_validation(self):
        expected = {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        }
        for alias, wire in expected.items():
            with self.subTest(alias=alias):
                prepared = self.imgpool._prepare_submission({
                    "model": "moss",
                    "prompt": "probe cup",
                    "aspect": alias,
                })
                self.assertEqual(prepared["requested_aspect"], wire)
                self.assertIn("--aspect", prepared["args"])
                self.assertEqual(
                    prepared["args"][prepared["args"].index("--aspect") + 1],
                    wire)

    def test_structured_args_emit_wire_format(self):
        args = self.imgpool._structured_args(
            {"prompt": "probe cup", "aspect": "square"}, "moss")
        self.assertEqual(
            args,
            ["--model", "moss", "probe cup", "--aspect", "1:1"])

    def test_explicit_wire_aspect_is_unchanged(self):
        prepared = self.imgpool._prepare_submission({
            "model": "moss",
            "prompt": "probe cup",
            "aspect": "4:5",
        })
        self.assertEqual(prepared["requested_aspect"], "4:5")

    def test_non_string_aspect_is_still_rejected(self):
        with self.assertRaises(self.imgpool.SubmissionError) as ctx:
            self.imgpool._prepare_submission({
                "model": "moss",
                "prompt": "probe cup",
                "aspect": 12,
            })
        self.assertEqual(ctx.exception.payload["error"], "bad_aspect")


if __name__ == "__main__":
    unittest.main()
