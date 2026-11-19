"""imgpool image-generation backend.

Calls a remote `comfy-imgpool` HTTP API (imggen + queue) instead of driving
ComfyUI directly. The pool serializes GPU work, exposes job ids and returns the
generated PNG via `/image`.

Config (config.yaml):

    image_gen:
      provider: imgpool
      model: imgpool-moss
      imgpool:
        base_url: http://10.205.130.33:8199
        token: <IMGPOOL_TOKEN>
        timeout: 600
        poll_interval: 2

Env overrides: IMGPOOL_URL, IMGPOOL_TOKEN, IMGPOOL_MODEL.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8199"
DEFAULT_MODEL = "imgpool-moss"
DEFAULT_TIMEOUT = 600.0
DEFAULT_POLL = 2.0
TERMINAL = ("done", "failed", "cancelled", "interrupted")

# model id -> imggen CLI invocation
_MODELS: Dict[str, Dict[str, Any]] = {
    "imgpool-moss": {
        "display": "imgpool · MOSS-Image 1024",
        "speed": "~9s warm / ~60s cold",
        "strengths": "Fast general text-to-image; default backend",
        "cli": "moss",
        "sizes": {"landscape": (1344, 768), "square": (1024, 1024),
                  "portrait": (768, 1344)},
    },
    "imgpool-anima-fast": {
        "display": "imgpool · Anima Turbo (2-pass)",
        "speed": "~13s warm",
        "strengths": "Anime/illustration; 1024→1536 two-pass, faster than direct 1536",
        "cli": "anima",
        "preset": "fast",
        "sizes": {"landscape": (1344, 768), "portrait": (768, 1344)},
    },
    "imgpool-anima-1536": {
        "display": "imgpool · Anima 1536",
        "speed": "~18s warm",
        "strengths": "Direct 1536 detail for anime/illustration",
        "cli": "anima",
        "preset": "1536",
        "sizes": {"landscape": (1536, 896), "portrait": (896, 1536)},
    },
    "imgpool-krea2-720p": {
        "display": "imgpool · Krea2 720p",
        "speed": "~10s warm",
        "strengths": "Cinematic/detailed stills at 720-class resolution",
        "cli": "krea2",
        "preset": "720p",
        "sizes": {"landscape": (1280, 720), "square": (1024, 1024),
                  "portrait": (720, 1280)},
    },
    "imgpool-krea2-2k": {
        "display": "imgpool · Krea2 2K (2-pass)",
        "speed": "~27s warm",
        "strengths": "1152×2048 two-pass; best detail, fixed portrait framing",
        "cli": "krea2",
        "preset": "2k",
        "sizes": {},
    },
}


def _image_gen_cfg() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # pragma: no cover - config is best-effort
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _sub_cfg() -> Dict[str, Any]:
    sub = _image_gen_cfg().get("imgpool")
    return sub if isinstance(sub, dict) else {}


def _base_url() -> str:
    url = (os.environ.get("IMGPOOL_URL") or _sub_cfg().get("base_url")
           or DEFAULT_BASE_URL)
    return str(url).strip().rstrip("/")


def _token() -> str:
    return str(os.environ.get("IMGPOOL_TOKEN") or _sub_cfg().get("token") or "")


def _headers() -> Dict[str, str]:
    tok = _token()
    return {"X-Imgpool-Token": tok} if tok else {}


def _timings() -> Tuple[float, float]:
    sub = _sub_cfg()
    try:
        poll = float(sub.get("poll_interval", DEFAULT_POLL))
    except (TypeError, ValueError):
        poll = DEFAULT_POLL
    try:
        timeout = float(sub.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return max(1.0, poll), max(60.0, timeout)


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    env = os.environ.get("IMGPOOL_MODEL")
    if env in _MODELS:
        return env, _MODELS[env]
    for cand in (_sub_cfg().get("model"), _image_gen_cfg().get("model")):
        if isinstance(cand, str) and cand in _MODELS:
            return cand, _MODELS[cand]
    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _build_args(meta: Dict[str, Any], prompt: str, aspect: str,
                seed: int) -> List[str]:
    args = ["--model", meta["cli"], prompt, "--seed", str(seed)]
    size = meta.get("sizes", {}).get(aspect)
    if size:
        args += ["--size", f"{size[0]}x{size[1]}"]
    elif meta.get("preset"):
        args += ["--preset", meta["preset"]]
    return args


class ImgpoolImageGenProvider(ImageGenProvider):
    """comfy-imgpool over HTTP."""

    @property
    def name(self) -> str:
        return "imgpool"

    @property
    def display_name(self) -> str:
        return "imgpool (local CLI over HTTP)"

    def is_available(self) -> bool:
        try:
            import requests
        except ImportError:
            return False
        try:
            r = requests.get(f"{_base_url()}/health", headers=_headers(),
                             timeout=2.5)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "free (self-hosted)",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "imgpool (local CLI)",
            "badge": "free",
            "tag": "imggen + queue on your own GPU host — MOSS/Anima/Krea2 presets",
            "env_vars": [
                {"key": "IMGPOOL_URL",
                 "prompt": "imgpool base URL (e.g. http://10.205.130.33:8199)"},
                {"key": "IMGPOOL_TOKEN",
                 "prompt": "X-Imgpool-Token value configured on the pool host"},
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_id, meta = _resolve_model()

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument", provider="imgpool",
                model=model_id, aspect_ratio=aspect)

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        if sources:
            return error_response(
                error=("The imgpool backend is text-to-image only; image_url / "
                       "reference_image_urls are not supported yet."),
                error_type="unsupported_modality", provider="imgpool",
                model=model_id, prompt=prompt, aspect_ratio=aspect)

        try:
            import requests
        except ImportError:
            return error_response(
                error="requests package not installed", error_type="missing_dependency",
                provider="imgpool", model=model_id, aspect_ratio=aspect)

        base = _base_url()
        headers = _headers()
        poll, timeout = _timings()
        seed = int.from_bytes(os.urandom(4), "big") & 0xFFFFFFFF
        args = _build_args(meta, prompt, aspect, seed)

        try:
            r = requests.post(
                f"{base}/submit", headers=headers, timeout=30,
                json={"model": meta["cli"], "source": "hermes", "prompt": prompt,
                      "timeout": int(timeout), "args": args})
            if r.status_code != 200:
                return error_response(
                    error=f"imgpool submit failed: HTTP {r.status_code} {r.text[:300]}",
                    error_type="http_error", provider="imgpool", model=model_id,
                    prompt=prompt, aspect_ratio=aspect)
            job = (r.json() or {}).get("job") or {}
            job_id = job.get("id")
            if not job_id:
                return error_response(
                    error=f"imgpool submit returned no job id: {r.text[:300]}",
                    error_type="provider_error", provider="imgpool", model=model_id,
                    prompt=prompt, aspect_ratio=aspect)
        except Exception as exc:
            return error_response(
                error=f"imgpool unreachable at {base}: {type(exc).__name__}: {exc}",
                error_type="connection_error", provider="imgpool", model=model_id,
                prompt=prompt, aspect_ratio=aspect)

        deadline = time.time() + timeout
        state, error, seconds, size = "unknown", None, None, None
        while time.time() < deadline:
            time.sleep(poll)
            try:
                r = requests.get(f"{base}/job", headers=headers,
                                 params={"id": job_id}, timeout=10)
                job = (r.json() or {}).get("job") or {}
            except Exception as exc:
                logger.warning("imgpool poll failed for %s: %s", job_id, exc)
                continue
            state = job.get("state", "unknown")
            if state in TERMINAL:
                error = job.get("error")
                seconds = job.get("seconds")
                res = job.get("result") or {}
                if res.get("output_width") and res.get("output_height"):
                    size = f"{res['output_width']}x{res['output_height']}"
                break

        if state != "done":
            timed_out = time.time() >= deadline
            return error_response(
                error=(f"imgpool job {job_id} {state}: {error}"
                       if error else
                       f"imgpool job {job_id} did not finish ({state})"),
                error_type="timeout" if timed_out else "provider_error",
                provider="imgpool", model=model_id, prompt=prompt,
                aspect_ratio=aspect)

        try:
            r = requests.get(f"{base}/image", headers=headers,
                             params={"id": job_id, "index": 0}, timeout=180)
            r.raise_for_status()
            raw = r.content
            if not raw:
                raise RuntimeError("empty image body")
            image_ref = str(save_b64_image(
                base64.b64encode(raw).decode("ascii"),
                prefix=f"imgpool_{model_id}"))
        except Exception as exc:
            return error_response(
                error=f"imgpool job {job_id} done but image download failed: {exc}",
                error_type="provider_error", provider="imgpool", model=model_id,
                prompt=prompt, aspect_ratio=aspect)

        return success_response(
            image=image_ref, model=model_id, prompt=prompt, aspect_ratio=aspect,
            provider="imgpool", modality="text",
            extra={"job_id": job_id, "seed": seed, "seconds": seconds,
                   "cli_model": meta["cli"], "size": size,
                   "server": base})


def register(ctx) -> None:
    ctx.register_image_gen_provider(ImgpoolImageGenProvider())
