"""Library for the console: LoRA index, generation history, local LLM proxy.

Three things live here, all loopback-only and all file-backed so they survive a
console restart:

* ``lora_index()``      scan ComfyUI/models/loras, read safetensors headers,
                        classify the training base, pull trigger tags.
* ``history_*()``       append-only JSONL of every generation, rewritable when a
                        thumbnail is attached or an entry is deleted.
* ``llm_*()``           thin proxy to the local Ollama model so prompt work can
                        run on the small local model instead of an API model.

Nothing here talks HTTP to ComfyUI; that stays in ``comfy.py``.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import comfy_root  # noqa: E402

LORA_CACHE = Path(__file__).resolve().parent.parent / "project" / "history" / "lora_index.json"
HISTORY_FILE = Path(__file__).resolve().parent.parent / "project" / "history" / "generations.jsonl"
ALIAS_FILE = Path(__file__).resolve().parent.parent / "config" / "lora_aliases.json"
PRESETS_FILE = Path(__file__).resolve().parent.parent / "config" / "bible_presets.json"
CARDS_FILE = Path(__file__).resolve().parent.parent / "project" / "00_bible" / "cards.json"
TRASH_DIR = Path(__file__).resolve().parent.parent / "project" / "_trash" / "loras"
LORA_TTL = 300  # seconds; a re-scan costs <1s for a few hundred files

DEFAULT_MODEL = "qwen3.8-27b-4bit-64k:latest"
LLM_SYSTEM = (
    "You write Danbooru-style tag prompts for an anime image model. "
    "Answer with the tags only: lowercase, comma separated, no sentence, no "
    "explanation, no quotes. Keep the caller's character identity tags, change "
    "only what the brief asks for. 12-30 tags."
)


# --------------------------------------------------------------------------- #
# LoRA index
# --------------------------------------------------------------------------- #
def lora_dir() -> Path:
    root = comfy_root()
    return root / "models" / "loras" if str(root) else Path()


def _header(path: Path, limit: int = 32 * 1024 * 1024) -> dict:
    """safetensors header: uint64 length, then that many bytes of JSON."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(8)
            if len(raw) < 8:
                return {}
            size = struct.unpack("<Q", raw)[0]
            if size <= 0 or size > limit:
                return {}
            return json.loads(fh.read(size).decode("utf-8", "replace"))
    except (OSError, ValueError, struct.error):
        return {}


def _classify(name: str, meta: dict, tensors: int) -> str:
    base = " ".join(str(meta.get(k) or "") for k in
                    ("ss_base_model_version", "ss_sd_model_name")).lower()
    if base.strip():
        if "anima" in base:
            return "anima"
        if "illustrious" in base or "sdxl" in base or "pony" in base:
            return "sdxl"
        if "flux" in base or "krea" in base or "qwen" in base:
            return "flux"
    # File names carry the family in this library; trust them over tensor width.
    lowered = name.lower()
    for key in ("anima", "illustrious", "noob", "pony", "sdxl", "krea", "flux", "qwen"):
        if key in lowered:
            return "sdxl" if key in ("illustrious", "noob", "pony", "sdxl") else key
    # No metadata and no telling name: anima LoRAs are compact UNET-only
    # adapters (~800 tensors), SDXL/Illustrious ones are much wider.
    if tensors:
        return "anima" if tensors <= 1000 else "sdxl"
    return "unknown"


def _triggers(meta: dict, limit: int = 8) -> list:
    """Trigger tokens: @style tags first, then the rarest stray tokens."""
    raw = meta.get("ss_tag_frequency") or ""
    tags: list = []
    try:
        freq = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        freq = {}
    for group in freq.values():
        if not isinstance(group, dict):
            continue
        for tag in group:
            cleaned = str(tag).strip()
            if cleaned.startswith("@") and cleaned not in tags:
                tags.append(cleaned)
    seen = set(tags)
    for group in freq.values():
        if not isinstance(group, dict):
            continue
        # rarest first: a trigger word appears a handful of times, boilerplate
        # tags (1girl, solo) appear hundreds of times.
        for tag, count in sorted(group.items(), key=lambda kv: kv[1]):
            cleaned = str(tag).strip()
            if 0 < len(cleaned) <= 24 and cleaned not in seen and not cleaned.startswith("@"):
                seen.add(cleaned)
                tags.append(cleaned)
            if len(tags) >= limit:
                return tags[:limit]
    return tags[:limit]


def _scan() -> list:
    out = []
    directory = lora_dir()
    if not directory.is_dir():
        return out
    aliases = load_aliases()
    for path in sorted(directory.rglob("*.safetensors")):
        if path.name.startswith("put_"):
            continue
        head = _header(path)
        meta = head.get("__metadata__") or {}
        tensors = max(0, len(head) - 1)
        title = str(meta.get("modelspec.title") or path.stem)
        alias = str(aliases.get(path.name) or "")
        out.append({
            "name": path.name,
            "rel": str(path.relative_to(directory)).replace("\\", "/"),
            "title": title,
            "alias": alias,
            "label": alias or title or path.stem,
            "size_mb": round(path.stat().st_size / 1048576, 1),
            "family": _classify(path.name, meta, tensors),
            "base": str(meta.get("ss_base_model_version") or meta.get("ss_sd_model_name") or ""),
            "triggers": _triggers(meta),
            "tensors": tensors,
            "mtime": int(path.stat().st_mtime),
        })
    return out


# --------------------------------------------------------------------------- #
# LoRA 中文别名 + 删除（进回收站，不真删）
# --------------------------------------------------------------------------- #
def load_aliases() -> dict:
    try:
        data = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_alias(name: str, alias: str) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("缺少 LoRA 文件名")
    data = load_aliases()
    alias = str(alias or "").strip()
    if alias:
        data[name] = alias
    else:
        data.pop(name, None)
    ALIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ALIAS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ALIAS_FILE)
    lora_index(force=True)
    return {"name": name, "alias": alias}


def delete_lora(name: str) -> dict:
    """把 LoRA 移进 project/_trash/loras，不直接删除文件。"""
    name = str(name or "").strip()
    directory = lora_dir()
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".safetensors"):
        raise ValueError("非法的 LoRA 文件名")
    src = directory / name
    if not directory.is_dir() or not src.is_file():
        raise ValueError(f"找不到 {name}")
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    target = TRASH_DIR / name
    if target.exists():
        target = TRASH_DIR / f"{int(time.time())}_{name}"
    os.replace(src, target)
    aliases = load_aliases()
    if name in aliases:
        aliases.pop(name, None)
        ALIAS_FILE.write_text(json.dumps(aliases, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    lora_index(force=True)
    return {"name": name, "trashed": str(target)}


def lora_index(force: bool = False) -> list:
    if not force and LORA_CACHE.is_file():
        try:
            cached = json.loads(LORA_CACHE.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("scanned_at") or 0) < LORA_TTL:
                return cached.get("loras") or []
        except (OSError, ValueError):
            pass
    items = _scan()
    LORA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LORA_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"scanned_at": time.time(), "loras": items},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LORA_CACHE)
    return items


# --------------------------------------------------------------------------- #
# generation history
# --------------------------------------------------------------------------- #
def _read_history() -> list:
    if not HISTORY_FILE.is_file():
        return []
    rows = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _write_history(rows: list) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    os.replace(tmp, HISTORY_FILE)


def history_list(limit: int = 60) -> list:
    rows = _read_history()
    rows.reverse()
    return rows[:max(1, min(500, int(limit or 60)))]


def history_add(entry: dict) -> dict:
    rows = _read_history()
    entry = dict(entry)
    entry.setdefault("id", f"g{int(time.time() * 1000)}")
    entry.setdefault("ts", int(time.time()))
    rows.append(entry)
    _write_history(rows[-2000:])
    return entry


def history_attach(entry_id: str, outputs: list) -> bool:
    rows = _read_history()
    hit = False
    for row in rows:
        if row.get("id") == entry_id:
            row["outputs"] = [o for o in (outputs or []) if isinstance(o, dict)][:8]
            hit = True
    if hit:
        _write_history(rows)
    return hit


def history_delete(entry_id: str = "", clear: bool = False) -> int:
    rows = _read_history()
    if clear:
        _write_history([])
        return len(rows)
    keep = [r for r in rows if r.get("id") != entry_id]
    removed = len(rows) - len(keep)
    if removed:
        _write_history(keep)
    return removed


# --------------------------------------------------------------------------- #
# local LLM proxy
# --------------------------------------------------------------------------- #
def ollama_models() -> list:
    try:
        import ollama  # lazy: keeps the library importable without the module
        return [m.get("name") for m in ollama.list_models()]
    except Exception:  # noqa: BLE001 - a missing daemon must not break the page
        return []


def pick_model(model: str = "") -> str:
    model = (model or "").strip()
    if model:
        return model
    names = ollama_models()
    for want in ("qwen3.8-27b-4bit-64k", "qwen3.8-27b", "qwen3"):
        for name in names:
            if want in name:
                return name
    return names[0] if names else DEFAULT_MODEL


def llm_chat(system: str, user: str, model: str = "", temperature: float = 0.5,
             num_predict: int = 320, timeout: float = 240.0) -> dict:
    import ollama  # local import: only this call needs the daemon

    name = pick_model(model)
    payload = {
        "model": name,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "think": False,          # qwen3 would otherwise spend the budget on reasoning
        "keep_alive": "10m",
        "options": {"temperature": temperature, "num_predict": num_predict,
                    "top_p": 0.9},
    }
    data = ollama._request("/api/chat", payload, timeout=timeout) or {}
    message = data.get("message") or {}
    text = (message.get("content") or message.get("thinking") or "").strip()
    text = text.split("<｜end▁of▁thinking｜>")[-1].strip() or text
    return {"model": name, "text": text,
            "eval_count": data.get("eval_count"),
            "total_ms": round(float(data.get("total_duration") or 0) / 1e6)}


def llm_assist(task: str = "rewrite", brief: str = "", context: str = "",
               model: str = "", negative: str = "") -> dict:
    """One entry point for every local-model job the console offers."""
    brief = (brief or "").strip()
    context = (context or "").strip()
    if task == "tags":
        system = ("You expand a one-line idea into Danbooru tags for an anime "
                  "image model. Tags only, lowercase, comma separated, 12-28 tags, "
                  "no explanation.")
        user = f"idea: {brief}\nscene: {context}" if context else f"idea: {brief}"
    elif task == "negative":
        system = ("You write a negative prompt for an anime image model. Return "
                  "comma separated tags only: quality defects, anatomy problems, "
                  "unwanted content. 10-20 tags.")
        user = f"positive: {context or brief}"
    else:  # rewrite
        system = LLM_SYSTEM
        user = f"brief: {brief}\n" + (f"current prompt: {context}\n" if context else "")
        if negative:
            user += f"keep the character, drop anything matching: {negative}\n"
    if not brief and not context:
        return {"error": "brief 为空", "text": ""}
    try:
        return llm_assist_result(llm_chat(system, user, model=model))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"local model failed: {exc}", "text": ""}


def llm_assist_result(result: dict) -> dict:
    text = result.get("text") or ""
    text = " ".join(text.split())
    text = text.strip(' "`')
    result["text"] = text
    return result


# --------------------------------------------------------------------------- #
# 角色卡库（可存结构化字段，也可整段存历史里的提示词）
# --------------------------------------------------------------------------- #
def _cards_read() -> dict:
    try:
        data = json.loads(CARDS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("cards"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "cards": []}


def _cards_write(data: dict) -> None:
    CARDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CARDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CARDS_FILE)


def cards_list() -> list:
    cards = _cards_read().get("cards") or []
    cards.sort(key=lambda c: (-int(c.get("ts") or 0)))
    return cards


def card_save(name: str = "", fields=None, text: str = "", source: str = "manual",
              card_id: str = "") -> dict:
    data = _cards_read()
    cards = data["cards"]
    clean = {}
    for key, value in (fields or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            clean[key] = value.strip()
    existing = next((c for c in cards if card_id and c.get("id") == card_id), None)
    entry = existing or {"id": f"c{int(time.time() * 1000)}", "ts": int(time.time())}
    entry["name"] = (name or entry.get("name") or "未命名角色").strip()[:60]
    entry["fields"] = clean or entry.get("fields") or {}
    entry["text"] = (text or entry.get("text") or "").strip()
    entry["source"] = source or entry.get("source") or "manual"
    entry["ts"] = int(time.time())
    if not existing:
        cards.append(entry)
    _cards_write(data)
    return entry


def card_delete(card_id: str) -> bool:
    data = _cards_read()
    before = len(data["cards"])
    data["cards"] = [c for c in data["cards"] if c.get("id") != card_id]
    if len(data["cards"]) == before:
        return False
    _cards_write(data)
    return True


def card_rename(card_id: str, name: str) -> dict:
    data = _cards_read()
    target = next((c for c in data["cards"] if c.get("id") == card_id), None)
    if not target:
        raise ValueError("找不到这张角色卡")
    target["name"] = (name or "").strip()[:60] or target.get("name")
    _cards_write(data)
    return target


# --------------------------------------------------------------------------- #
# 角色卡「词条」候选库（每个字段可增删改）
# --------------------------------------------------------------------------- #
def _normalize_words(options) -> list:
    """统一成 [{'en','zh','cat'}]；旧的纯字符串列表也能读。"""
    out, seen = [], set()
    for item in options or []:
        if isinstance(item, dict):
            en = str(item.get("en") or item.get("word") or "").strip()
            zh = str(item.get("zh") or "").strip()
            cat = str(item.get("cat") or "自定义").strip() or "自定义"
        else:
            en, zh, cat = str(item or "").strip(), "", "自定义"
        if not en or en.lower() in seen:
            continue
        seen.add(en.lower())
        out.append({"en": en[:200], "zh": (zh or en)[:60], "cat": cat[:16]})
    return out


def load_presets() -> dict:
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else data
    out = {}
    for field, options in fields.items():
        if isinstance(options, list):
            out[str(field)] = _normalize_words(options)
    return out


def set_field_presets(field: str, options) -> dict:
    field = str(field or "").strip()
    if not field or not field.replace("_", "").isalnum():
        raise ValueError("非法的字段名")
    words = _normalize_words(options)
    if len(words) > 400:
        raise ValueError("一个字段最多 400 条候选")
    data = load_presets()
    if words:
        data[field] = words
    else:
        data.pop(field, None)
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 2, "fields": data}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, PRESETS_FILE)
    return {"field": field, "options": words, "presets": data}
