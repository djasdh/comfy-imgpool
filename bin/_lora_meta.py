"""LoRA 元数据的读写层。

每个模型一个 loras/<model>.json，零第三方依赖。
只负责数据：加载、查找、校验、审计、原子写回。
"""
from __future__ import annotations

import glob
import json
import os
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LORA_DIR = os.environ.get("IMGPOOL_LORA_DIR") or os.path.join(ROOT, "loras")

KINDS = ("functional", "style")

# 写回时固定字段顺序，方便 diff
FIELDS = (
    "file", "kind", "description", "tags", "aliases",
    "default_strength", "recommended", "trigger", "license",
    "commercial_ok", "source", "tested", "verified",
)


class LoraMetaError(Exception):
    pass


def _path(model):
    return os.path.join(LORA_DIR, f"{model}.json")


def _today():
    return date.today().isoformat()


def _check_entry(entry, model):
    if not entry.get("file"):
        raise LoraMetaError(f"{model} 里有一条记录缺少 file")
    if entry.get("kind") not in KINDS:
        raise LoraMetaError(
            f"{entry['file']} 的 kind 必须是 {'/'.join(KINDS)}，"
            f"收到 {entry.get('kind')!r}")


def load_model(model):
    p = _path(model)
    if not os.path.isfile(p):
        raise LoraMetaError(f"没有 {model} 的 LoRA 元数据：{p}")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise LoraMetaError(f"{p} 不是合法 JSON：{e}") from e
    data.setdefault("schema", 1)
    data.setdefault("model", model)
    data.setdefault("display", model)
    data.setdefault("loras", [])
    for e in data["loras"]:
        _check_entry(e, model)
    return data


def load_all():
    """返回 {model: data}；坏文件跳过并记在 _errors 里。"""
    out = {}
    if not os.path.isdir(LORA_DIR):
        return out
    for p in sorted(glob.glob(os.path.join(LORA_DIR, "*.json"))):
        model = os.path.splitext(os.path.basename(p))[0]
        try:
            data = load_model(model)
        except LoraMetaError:
            continue
        out[data["model"]] = data
    return out


def iter_entries(model=None):
    data = {model: load_model(model)} if model else load_all()
    for m, d in data.items():
        for e in d.get("loras", []):
            yield m, e


def normalize(name):
    n = os.path.basename(name or "").strip()
    for suf in (".safetensors", ".pt", ".ckpt"):
        if n.lower().endswith(suf):
            n = n[: -len(suf)]
    return n


def find(name, model=None):
    """按文件名、去后缀名或别名查找。返回 (model, entry) 或 None。"""
    target = normalize(name)
    hits = []
    for m, e in iter_entries(model):
        names = [normalize(e.get("file"))]
        names += [normalize(a) for a in e.get("aliases") or []]
        if target in names:
            hits.append((m, e))
            if m == model:
                return m, e
    return hits[0] if hits else None


def alias_map(model=None):
    """{alias: (file, default_strength, model)}；跨模型重名时只保留第一个。"""
    out = {}
    for m, e in iter_entries(model):
        for a in e.get("aliases") or []:
            out.setdefault(a, (e["file"], e.get("default_strength"), m))
    return out


def default_strengths(model=None):
    out = {}
    for m, e in iter_entries(model):
        if e.get("default_strength") is not None:
            out[e["file"]] = e["default_strength"]
    return out


def audit(disk_names):
    """对比磁盘文件与元数据。disk_names 传文件名列表。"""
    disk = {normalize(n): n for n in disk_names}
    seen = {}
    unverified = []
    for m, e in iter_entries():
        key = normalize(e.get("file"))
        seen.setdefault(key, []).append(m)
        if not e.get("verified", False):
            unverified.append({"model": m, "file": e["file"],
                               "description": e.get("description")})
    return {
        "disk": len(disk),
        "registered": len(seen),
        "unregistered": sorted(set(disk) - set(seen)),
        "missing": sorted(set(seen) - set(disk)),
        "duplicate": {k: v for k, v in seen.items() if len(v) > 1},
        "unverified": unverified,
    }


def save_model(model, data):
    p = _path(model)
    os.makedirs(LORA_DIR, exist_ok=True)
    for e in data.get("loras", []):
        _check_entry(e, model)
    ordered = {
        "schema": data.get("schema", 1),
        "model": model,
        "display": data.get("display", model),
        "updated": data.get("updated") or _today(),
        "loras": [{k: e.get(k) for k in FIELDS} for e in data.get("loras", [])],
    }
    fd, tmp = tempfile.mkstemp(dir=LORA_DIR, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ordered, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return p


def add_entry(model, entry):
    if os.path.isfile(_path(model)):
        data = load_model(model)
    else:
        data = {"schema": 1, "model": model, "display": model,
                "updated": _today(), "loras": []}
    existing = [normalize(e["file"]) for e in data["loras"]]
    if normalize(entry["file"]) in existing:
        raise LoraMetaError(
            f"{model} 里已经登记了 {entry['file']}，用 set 修改")
    data["loras"].append(entry)
    data["updated"] = _today()
    return save_model(model, data)


def update_entry(model, name, changes):
    data = load_model(model)
    target = normalize(name)
    for e in data["loras"]:
        if normalize(e["file"]) == target or target in [
                normalize(a) for a in e.get("aliases") or []]:
            for k, v in changes.items():
                if v is None:
                    continue
                if k not in FIELDS:
                    raise LoraMetaError(f"未知字段 {k!r}")
                e[k] = v
            data["updated"] = _today()
            return save_model(model, data)
    raise LoraMetaError(f"{model} 里没有找到 {name}")


def remove_entry(model, name):
    data = load_model(model)
    target = normalize(name)
    before = len(data["loras"])
    data["loras"] = [
        e for e in data["loras"]
        if normalize(e["file"]) != target
        and target not in [normalize(a) for a in e.get("aliases") or []]
    ]
    if len(data["loras"]) == before:
        raise LoraMetaError(f"{model} 里没有找到 {name}")
    data["updated"] = _today()
    return save_model(model, data)
