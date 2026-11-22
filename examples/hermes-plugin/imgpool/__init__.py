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
DEFAULT_TIMEOUT = 600.0
DEFAULT_POLL = 2.0
TERMINAL = ("done", "failed", "cancelled", "interrupted")
ID_PREFIX = "imgpool-"
CATALOG_TTL = 15.0

_CATALOG_CACHE: Dict[str, Any] = {"at": 0.0, "data": {}}


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


def _catalog() -> Dict[str, Any]:
    """Fetch the model/preset table from the pool; no local copy to drift."""
    now = time.time()
    cached = _CATALOG_CACHE.get("data") or {}
    if cached and now - float(_CATALOG_CACHE.get("at") or 0) < CATALOG_TTL:
        return cached
    try:
        import requests

        r = requests.get(f"{_base_url()}/models", headers=_headers(), timeout=5)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:
        logger.debug("Could not load imgpool /models: %s", exc)
        return cached
    if data:
        _CATALOG_CACHE.update({"at": now, "data": data})
    return data


def _model_records(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [m for m in (catalog.get("models") or [])
            if isinstance(m, dict) and m.get("model")]


def _preset_names(meta: Dict[str, Any]) -> List[str]:
    return [str(p.get("preset")) for p in (meta.get("presets") or [])
            if isinstance(p, dict) and p.get("preset")]


def _strip_prefix(model_id: Any) -> str:
    mid = str(model_id or "").strip()
    return mid[len(ID_PREFIX):] if mid.startswith(ID_PREFIX) else mid


def _match_model(model_id: Any, catalog: Dict[str, Any]):
    mid = _strip_prefix(model_id)
    if not mid:
        return None
    records = _model_records(catalog)
    for meta in records:
        if mid == str(meta.get("model")):
            return meta, meta.get("default_preset")
    # Longest model name first so e.g. krea2 wins over a hypothetical krea.
    for meta in sorted(records, key=lambda m: len(str(m.get("model"))),
                       reverse=True):
        name = str(meta.get("model"))
        prefix = name + "-"
        if mid.startswith(prefix):
            preset = mid[len(prefix):]
            if preset in _preset_names(meta):
                return meta, preset
    return None


def _fallback_model(model_id: Any):
    """Parse imgpool-<model>[-<preset>] when /models is unreachable."""
    mid = _strip_prefix(model_id)
    if not mid:
        return None
    if "-" in mid:
        name, preset = mid.split("-", 1)
        return {"model": name, "display": name}, preset
    return {"model": mid, "display": mid}, None


def _resolve_model(override: Optional[str] = None):
    catalog = _catalog()
    requested = override or os.environ.get("IMGPOOL_MODEL")
    if not requested:
        for cand in (_sub_cfg().get("model"), _image_gen_cfg().get("model")):
            if isinstance(cand, str) and cand.strip():
                requested = cand
                break
    hit = _match_model(requested, catalog) if requested else None
    if hit is None and requested:
        hit = _fallback_model(requested)
    if hit is None:
        records = _model_records(catalog)
        if records:
            meta = records[0]
            hit = (meta, meta.get("default_preset"))
    if hit is None:
        return None, None, {}, None
    meta, preset = hit
    name = str(meta.get("model"))
    preset = preset or meta.get("default_preset")
    if requested and str(requested).startswith(ID_PREFIX):
        model_id = str(requested)
    else:
        model_id = f"{ID_PREFIX}{name}"
        if preset and preset != meta.get("default_preset"):
            model_id += f"-{preset}"
    return name, preset, meta, model_id


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
        catalog = _catalog()
        out: List[Dict[str, Any]] = []
        for meta in _model_records(catalog):
            name = str(meta.get("model"))
            default = meta.get("default_preset")
            presets = [p for p in (meta.get("presets") or [])
                       if isinstance(p, dict) and p.get("preset")]
            if not presets:
                presets = [{"preset": default}]
            for preset in presets:
                pname = preset.get("preset") or default
                model_id = f"{ID_PREFIX}{name}"
                if pname and pname != default:
                    model_id += f"-{pname}"
                est = preset.get("estimated_seconds_hot")
                speed = f"~{est:g}s warm" if isinstance(est, (int, float)) else "unknown"
                out_w = preset.get("output_width") or preset.get("width")
                out_h = preset.get("output_height") or preset.get("height")
                size = f"{out_w}x{out_h}" if out_w and out_h else None
                display = str(meta.get("display") or name)
                if pname:
                    display = f"{display} · {pname}"
                strengths = str(meta.get("notes") or "")
                if preset.get("risky"):
                    strengths = ("risky/unverified preset. " + strengths).strip()
                out.append({
                    "id": model_id,
                    "display": display,
                    "speed": speed,
                    "strengths": strengths,
                    "price": "free (self-hosted)",
                    "model": name,
                    "preset": pname,
                    "size": size,
                })
        return out

    def default_model(self) -> Optional[str]:
        catalog = _catalog()
        name = catalog.get("default_model")
        if not name:
            records = _model_records(catalog)
            name = records[0].get("model") if records else None
        return f"{ID_PREFIX}{name}" if name else None

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
        model_name, preset, meta, model_id = _resolve_model(
            kwargs.get("model") or kwargs.get("model_id"))
        if not model_name:
            return error_response(
                error="imgpool /models is unavailable and no model id was configured",
                error_type="connection_error", provider="imgpool",
                model=model_id, aspect_ratio=aspect)

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
        payload: Dict[str, Any] = {
            "model": model_name, "source": "hermes", "prompt": prompt,
            "timeout": int(timeout), "seed": seed, "aspect": aspect,
        }
        if preset:
            payload["preset"] = preset
        for key in ("preset", "variant", "size", "steps", "steps2", "cfg",
                    "denoise", "denoise2", "scale", "negative", "lora",
                    "native", "latent", "allow_risky", "edit", "inpaint",
                    "mask", "rect", "anypaint", "allow_format_mismatch"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]
        if payload.get("size"):
            payload.pop("aspect", None)

        try:
            r = requests.post(
                f"{base}/submit", headers=headers, timeout=30, json=payload)
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
                   "cli_model": job.get("cli_model") or model_name,
                   "preset": job.get("preset") or preset,
                   "actual_aspect": job.get("actual_aspect"),
                   "native_snapped": job.get("native_snapped"),
                   "size": size, "server": base})


def register(ctx) -> None:
    ctx.register_image_gen_provider(ImgpoolImageGenProvider())
