"""Metadata reverse-lookup tests for ``imggen inspect``.

The fixtures are written with the standard library only and intentionally keep
the UI ``workflow`` block next to the API ``prompt`` block: the parses must
never read widget values from the UI graph.
"""

import binascii
import importlib.machinery
import importlib.util
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_imggen():
    loader = importlib.machinery.SourceFileLoader(
        "_imggen_metadata_test", str(ROOT / "bin" / "imggen"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _chunk(kind, data):
    body = kind + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF))


def _write_png(path, texts, width=512, height=512):
    raw = b"\x00" * (width * height * 3)
    data = b"\x89PNG\r\n\x1a\n"
    data += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                       8, 2, 0, 0, 0))
    for key, value in texts.items():
        data += _chunk(b"tEXt", key.encode("latin-1") + b"\x00"
                       + value.encode("latin-1"))
    data += _chunk(b"IDAT", zlib.compress(raw))
    data += _chunk(b"IEND", b"")
    Path(path).write_bytes(data)


def _write_jpeg_with_usercomment(path, text, width=512, height=768):
    comment = b"UNICODE\x00" + text.encode("utf-16-le")
    tiff = b"II" + struct.pack("<HI", 42, 8)
    tiff += struct.pack("<H", 1) + struct.pack(
        "<HHII", 0x8769, 4, 1, 26) + struct.pack("<I", 0)
    tiff += struct.pack("<H", 1) + struct.pack(
        "<HHII", 0x9286, 7, len(comment), 44) + struct.pack("<I", 0)
    tiff += comment
    app = b"Exif\x00\x00" + tiff
    segment = b"\xff\xe1" + struct.pack(">H", len(app) + 2) + app
    sof = (b"\xff\xc0" + struct.pack(">H", 11) + bytes([8])
           + struct.pack(">HH", height, width) + bytes([3])
           + b"\x01\x11\x00" * 3)
    Path(path).write_bytes(b"\xff\xd8" + segment + sof + b"\xff\xd9")


def _comfy_graph(prompt, negative=""):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "moss_image_sft_int8_convrot.safetensors",
            "weight_dtype": "default"}},
        "30": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0],
            "lora_name": "moss-masterpieces-moss-timestep.safetensors",
            "strength_model": 0.75}},
        "20": {"class_type": "RDNA2Attention", "inputs": {
            "model": ["30", 0], "backend": "hip", "transpose_weights": False,
            "int8_gemm": True, "fused_norm": False, "fused_swiglu": False}},
        "3": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3.5_9b_w4a8_gptq.safetensors",
            "type": "moss_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {
            "vae_name": "flux2-vae.safetensors"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt, "clip": ["3", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {
            "text": negative, "clip": ["3", 0]}},
        "19": {"class_type": "ForceUnloadModels", "inputs": {
            "model": ["20", 0], "positive": ["5", 0],
            "negative": ["6", 0]}},
        "7": {"class_type": "EmptyLatentImage", "inputs": {
            "width": 1536, "height": 1024, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["19", 0], "seed": 832276161256161, "steps": 50,
            "cfg": 3.5, "sampler_name": "euler",
            "scheduler": "sgm_uniform", "positive": ["5", 0],
            "negative": ["6", 0], "latent_image": ["7", 0],
            "denoise": 1.0}},
        "11": {"class_type": "VAEDecode", "inputs": {
            "samples": ["8", 0], "vae": ["4", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {
            "images": ["11", 0], "filename_prefix": "26_11_11/Moss"}},
    }


class MetadataInspectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imggen = _load_imggen()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _png(self, name, texts, width=512, height=512):
        path = Path(self.tmp.name) / name
        _write_png(path, texts, width, height)
        return path

    def test_comfy_api_graph_is_reversed_and_ui_widgets_are_ignored(self):
        graph = _comfy_graph("the linked prompt", "bad anatomy")
        workflow = {
            "nodes": [{
                "id": 4,
                "widgets_values": ["STALE PROMPT FROM ANOTHER CHARACTER"],
            }]
        }
        path = self._png("comfy.png", {
            "prompt": json.dumps(graph),
            "workflow": json.dumps(workflow),
        }, width=2448, height=1632)

        info = self.imggen.parse_metadata(str(path))
        self.assertTrue(info["ok"])
        self.assertEqual(info["source"], "comfyui")
        self.assertEqual(info["size"], {"width": 2448, "height": 1632})

        recipe = self.imggen.recipes_from_comfy_api(info["comfy_api"])
        self.assertEqual(recipe["model"], "moss")
        self.assertEqual(recipe["variant"], "sft-int8")
        self.assertEqual(recipe["positive"], "the linked prompt")
        self.assertEqual(recipe["negative"], "bad anatomy")
        self.assertNotIn("STALE", json.dumps(recipe))
        self.assertEqual(recipe["pass1"]["width"], 1536)
        self.assertEqual(recipe["pass1"]["height"], 1024)
        self.assertEqual(recipe["pass1"]["steps"], 50)
        self.assertEqual(recipe["pass1"]["cfg"], 3.5)
        self.assertEqual(recipe["seed"], 832276161256161)
        self.assertEqual(recipe["loras"][0]["name"],
                         "moss-masterpieces-moss-timestep")
        self.assertEqual(recipe["loras"][0]["strength"], 0.75)
        self.assertEqual(recipe["prompt_prefix"], "26_11_11/Moss")

        payload, code = self.imggen.inspect_image(str(path))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["matches_local"]["preset"], None)
        self.assertIn("preset", payload["missing"])
        self.assertIn("scheduler", payload["unmapped"])
        args = payload["replay"]["args"]
        self.assertEqual(args[:4], ["-m", "moss", "--size", "1536x1024"])
        self.assertIn("--steps", args)
        self.assertIn("--seed", args)

    def test_ui_graph_alone_is_not_used(self):
        workflow = {"nodes": [{"id": 4, "widgets_values": ["stale"]}]}
        path = self._png("ui-only.png", {"workflow": json.dumps(workflow)})
        payload, code = self.imggen.inspect_image(str(path))
        self.assertEqual(code, 2)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["error"], "no_metadata")
        self.assertNotIn("recipe", payload)

    def test_no_metadata_returns_error_and_exit_2(self):
        path = self._png("plain.png", {})
        info = self.imggen.parse_metadata(str(path))
        self.assertFalse(info["ok"])
        self.assertEqual(info["error"], "no_metadata")
        payload, code = self.imggen.inspect_image(str(path))
        self.assertEqual(payload, {"ok": False, "error": "no_metadata"})
        self.assertEqual(code, 2)

    def test_a1111_text_and_unknown_model(self):
        text = (
            "a cat, best quality\n"
            "Negative prompt: lowres, bad anatomy\n"
            "Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 42, "
            "Size: 512x768, Model: some_external_model, VAE: other.vae"
        )
        recipe = self.imggen.recipes_from_a1111(text)
        self.assertEqual(recipe["model"], "some_external_model")
        self.assertEqual(recipe["pass1"]["width"], 512)
        self.assertEqual(recipe["pass1"]["height"], 768)
        self.assertEqual(recipe["pass1"]["steps"], 20)
        self.assertEqual(recipe["pass1"]["cfg"], 7.0)
        self.assertEqual(recipe["seed"], 42)
        self.assertEqual(recipe["positive"], "a cat, best quality")
        self.assertEqual(recipe["negative"], "lowres, bad anatomy")
        self.assertEqual(recipe["pass1"]["sampler"], "euler_ancestral")

        path = self._png("a1111.jpg.png", {"parameters": text},
                         width=512, height=768)
        payload, code = self.imggen.inspect_image(str(path))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "a1111")
        self.assertIsNone(payload["matches_local"])
        self.assertIn("model", payload["missing"])
        self.assertEqual(payload["recipe"]["model"],
                         "some_external_model")

    def test_a1111_jpeg_exif_usercomment(self):
        text = (
            "a jpeg cat\n"
            "Negative prompt: blur\n"
            "Steps: 12, Sampler: Euler, CFG scale: 5.5, Seed: 99, "
            "Size: 512x768, Model: moss"
        )
        path = Path(self.tmp.name) / "a1111.jpg"
        _write_jpeg_with_usercomment(path, text)
        info = self.imggen.parse_metadata(str(path))
        self.assertTrue(info["ok"])
        self.assertEqual(info["source"], "a1111")
        self.assertEqual(info["size"], {"width": 512, "height": 768})
        recipe = self.imggen.recipes_from_a1111(info["a1111"])
        self.assertEqual(recipe["model"], "moss")
        self.assertEqual(recipe["pass1"]["steps"], 12)
        self.assertEqual(recipe["pass1"]["cfg"], 5.5)
        self.assertEqual(recipe["seed"], 99)

    def test_a1111_model_file_maps_onto_local_variant(self):
        text = (
            "portrait\n"
            "Negative prompt: blur\n"
            "Steps: 8, Sampler: Euler, CFG scale: 1, Seed: 7, "
            "Size: 1024x1024, Model: krea2_turbo_int8_convrot"
        )
        recipe = self.imggen.recipes_from_a1111(text)
        self.assertEqual(recipe["model"], "krea2")
        self.assertEqual(recipe["variant"], "int8")
        self.assertEqual(recipe["confidence"], "exact")

    def test_external_comfy_graph_lists_missing_models_and_nodes(self):
        graph = _comfy_graph("outside")
        graph["1"]["inputs"]["unet_name"] = "external_model.safetensors"
        graph["99"] = {"class_type": "SomeExternalNode",
                       "inputs": {"model": ["1", 0]}}
        path = self._png("external.png", {"prompt": json.dumps(graph)})
        payload, code = self.imggen.inspect_image(str(path))
        self.assertNotEqual(code, 0)
        self.assertTrue(payload["ok"])
        missing = payload["missing_models"]
        self.assertIn("external_model.safetensors", missing)
        self.assertIn("node:SomeExternalNode", missing)
        # Parsing is read-only: the graph is returned, never executed.
        self.assertEqual(payload["recipe"]["unet"],
                         "external_model.safetensors")

    def test_own_preset_round_trip_matches_preset(self):
        payload, code = self.imggen.inspect_image(str(self._png(
            "roundtrip.png", {"prompt": json.dumps(_comfy_graph("x", ""))},
            width=1536, height=1024)))
        self.assertEqual(code, 0)
        # The hand-written fixture has a custom seed/steps/scheduler, but the
        # files identify moss exactly and replay keeps the mappable values.
        self.assertEqual(payload["recipe"]["confidence"], "exact")
        args = payload["replay"]["args"]
        self.assertEqual(args[0:2], ["-m", "moss"])
        self.assertIn("--seed", args)
        self.assertIn("832276161256161", args)


if __name__ == "__main__":
    unittest.main()
