"""Domain layer for the local anime manga studio.

Kept free of HTTP and PowerShell concerns so it can be unit-tested directly:
  - config loading (config/ is the single source of truth)
  - prompt template rendering
  - stage asset scanning and acceptance gates for the 6-stage workflow
  - baseline / bible / storyboard persistence

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
CONFIG_DIR = PROJECT_ROOT / "config"
WORKFLOW_DIR = CONFIG_DIR / "workflows"
ART_DIR = PROJECT_ROOT / "project"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOG_DIR = PROJECT_ROOT / "logs"
DOCS_DIR = PROJECT_ROOT / "docs"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

BIBLE_DIR = ART_DIR / "00_bible"
DATASET_DIR = ART_DIR / "01_dataset"
CANDIDATE_DIR = DATASET_DIR / "candidates"
TRAIN_DIR = DATASET_DIR / "train"
LORA_DIR = ART_DIR / "02_lora"
ILLUST_DIR = ART_DIR / "03_illust"
MANGA_DIR = ART_DIR / "04_manga"
ANIM_DIR = ART_DIR / "05_anim"

BIBLE_FILE = BIBLE_DIR / "character.json"
STORYBOARD_FILE = MANGA_DIR / "storyboard.json"
LORA_SELECTION_FILE = LORA_DIR / "selected.json"
ILLUST_MANIFEST_FILE = ILLUST_DIR / "manifest.json"
BASELINE_FILE = REPORTS_DIR / "baseline.json"
UI_FILE = CONFIG_DIR / "ui.json"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
WEIGHT_SUFFIXES = {".safetensors", ".gguf", ".ckpt", ".pt"}

TZ = timezone(timedelta(hours=8))

BIBLE_REQUIRED = ("trigger", "hair", "eyes", "outfit", "signature")
BIBLE_LABELS = {
    "trigger": "触发词",
    "hair": "发型与发色",
    "eyes": "瞳色与眼型",
    "outfit": "服装",
    "signature": "标志性特征",
    "body": "体型与年龄感",
    "pose": "姿势控制",
    "personality": "默认表情与性格",
    "extras": "补充标签（场景 / 光 / 风格）",
    "lewd": "色情程度",
}

# 字段表可以自己增删改，默认值等于上面这份内置表。
FIELDS_FILE = CONFIG_DIR / "bible_fields.json"
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
DEFAULT_FIELDS = [{"key": k, "label": v, "required": k in BIBLE_REQUIRED}
                  for k, v in BIBLE_LABELS.items()]


def load_fields() -> list:
    """当前字段表；坏文件自动回落内置表。"""
    data = load_json(FIELDS_FILE)
    if isinstance(data, dict) and isinstance(data.get("fields"), list):
        out = []
        for item in data["fields"]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not FIELD_KEY_RE.match(key):
                continue
            out.append({"key": key,
                        "label": str(item.get("label") or key).strip()[:24] or key,
                        "required": bool(item.get("required"))})
        if out:
            return out
    return [dict(x) for x in DEFAULT_FIELDS]


def save_fields(fields) -> list:
    out = []
    for item in fields or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not FIELD_KEY_RE.match(key):
            raise ValueError(f"字段名不合法：{key}")
        label = str(item.get("label") or key).strip()[:24] or key
        if any(x["key"] == key for x in out):
            raise ValueError(f"字段重复：{key}")
        out.append({"key": key, "label": label, "required": bool(item.get("required"))})
    if not out:
        raise ValueError("至少保留一个字段")
    if len(out) > 24:
        raise ValueError("字段最多 24 个")
    _write_json_atomic(FIELDS_FILE, {"fields": out})
    return out

# Candidate counts come from the reference workflow, not from a guess.
CANDIDATE_TARGET = (40, 60)
TRAIN_TARGET = (20, 40)
PANEL_TARGET = (4, 6)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def now_local() -> datetime:
    return datetime.now(TZ)


def load_json(path: Path):
    """Load a JSON file, returning None instead of raising for callers."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_bytes_atomic(path: Path, data: bytes):
    """Write via a sibling temp file, so a crash never leaves a truncated asset."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_json_atomic(path: Path, data):
    _write_bytes_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_params():
    return load_json(CONFIG_DIR / "params.json") or {}


def load_prompts():
    return load_json(CONFIG_DIR / "prompts.json") or {}


def load_manifest():
    return load_json(CONFIG_DIR / "models.manifest.json") or {"files": [], "optional": []}


def load_ui():
    """Presentation settings that are not parameters (title, paused stages)."""
    return load_json(UI_FILE) or {}


_PS_ASSIGN_RE = re.compile(r"^\s*(\w+)\s*=\s*(.+?)\s*$")


def paused_stages() -> set:
    """Stages the schedule has parked (config/ui.json -> video.paused / paused_stages)."""
    ui = load_ui() or {}
    parked = set()
    video = ui.get("video") or {}
    if video.get("paused") and video.get("stage"):
        try:
            parked.add(int(video["stage"]))
        except (TypeError, ValueError):
            pass
    for stage in ui.get("paused_stages") or []:
        try:
            parked.add(int(stage))
        except (TypeError, ValueError):
            continue
    return parked

def load_studio_env():
    """Read ``$StudioEnv`` from config/studio-env.ps1.

    config/ is the single source of truth, but Python cannot dot-source
    PowerShell. The literal assignments are therefore parsed: quoted strings are
    taken as-is and ``Join-Path $ProjectRoot 'x'`` is resolved against the real
    project root. Anything more complex is skipped rather than guessed.
    """
    path = CONFIG_DIR / "studio-env.ps1"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    env = {}
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("$StudioEnv"):
            inside = True
            continue
        if inside and stripped.startswith("}"):
            break
        if not inside:
            continue
        m = _PS_ASSIGN_RE.match(stripped)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if raw.startswith("Join-Path"):
            parts = re.findall(r"'([^']*)'", raw)
            if parts:
                env[key] = str(PROJECT_ROOT / parts[-1])
        else:
            found = re.findall(r"'([^']*)'", raw)
            if found:
                env[key] = found[0]
    return env


def comfyui_url(env=None):
    env = load_studio_env() if env is None else env
    host = env.get("STUDIO_COMFYUI_HOST", "127.0.0.1:8188")
    if not host.startswith("http"):
        host = "http://" + host
    return host


def console_port(env=None):
    env = load_studio_env() if env is None else env
    try:
        return int(env.get("STUDIO_CONSOLE_PORT", 8766))
    except ValueError:
        return 8766


# --------------------------------------------------------------------------- #
# prompt templates
# --------------------------------------------------------------------------- #
def placeholders_of(text: str):
    if not isinstance(text, str):
        return []
    return sorted(set(_PLACEHOLDER_RE.findall(text)))


def render_prompt(key: str, values: dict, prompts=None):
    """Render one template. Values are substituted with their original type when
    a whole input is a single placeholder, so numeric params stay numeric."""
    prompts = prompts if prompts is not None else load_prompts()
    tpl = (prompts.get("templates") or {}).get(key)
    if not tpl:
        raise KeyError(f"unknown prompt template: {key}")

    def sub(text):
        if not isinstance(text, str):
            return text
        whole = _PLACEHOLDER_RE.fullmatch(text.strip())
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        out = text
        for name, val in values.items():
            out = out.replace("{{%s}}" % name, "" if val is None else str(val))
        return out

    missing = sorted(set(placeholders_of(tpl.get("positive", "")) +
                         placeholders_of(tpl.get("negative", ""))) - set(values))
    return {
        "key": key,
        "title": tpl.get("title", key),
        "stage": tpl.get("stage"),
        "params_key": tpl.get("params_key"),
        "positive": sub(tpl.get("positive", "")),
        "negative": sub(tpl.get("negative", "")),
        "note": tpl.get("note", ""),
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# character bible
# --------------------------------------------------------------------------- #
def load_bible():
    return load_json(BIBLE_FILE)


def save_bible(data: dict):
    """Merge the edited fields into character.json.

    Field keys come from the editable field table, so a field the operator just
    added is written too. Unknown keys already in the file are preserved.
    """
    bible = load_bible() or {}
    keys = {f["key"] for f in load_fields()} | set(BIBLE_LABELS)
    for key in keys:
        if key in data:
            bible[key] = str(data.get(key) or "").strip()

    palette = data.get("palette")
    if isinstance(palette, dict):
        base = bible.get("palette") if isinstance(bible.get("palette"), dict) else {}
        bible["palette"] = {k: str(palette.get(k, base.get(k)) or "").strip()
                            for k in ("main", "accent", "sub")}

    BIBLE_DIR.mkdir(parents=True, exist_ok=True)
    bible["updated_at"] = now_local().isoformat(timespec="seconds")
    bible.setdefault("created_at", bible["updated_at"])
    _write_json_atomic(BIBLE_FILE, bible)
    return bible


def bible_editor_state() -> dict:
    """Everything the console needs to render the bible form."""
    bible = load_bible() or {}
    schema = load_fields()
    fields = {f["key"]: str(bible.get(f["key"]) or "") for f in schema}
    labels = {f["key"]: f["label"] for f in schema}
    required = [f["key"] for f in schema if f["required"]]
    palette = bible.get("palette") if isinstance(bible.get("palette"), dict) else {}
    missing = [k for k in required if not fields.get(k, "").strip()]
    return {
        "exists": BIBLE_FILE.is_file(),
        "fields": fields,
        "labels": labels,
        "fields_list": schema,
        "palette": {k: str(palette.get(k) or "") for k in ("main", "accent", "sub")},
        "required": required,
        "missing": missing,
        "complete": not missing,
        "updated_at": bible.get("updated_at"),
    }


def bible_report():
    """Validate the bible against the required schema."""
    bible = load_bible()
    if bible is None:
        return {"exists": False, "missing": list(BIBLE_REQUIRED),
                "trigger": None, "fields": {}}
    missing = [k for k in BIBLE_REQUIRED if not str(bible.get(k) or "").strip()]
    palette = bible.get("palette") or {}
    palette_ok = isinstance(palette, dict) and all(
        str(palette.get(k) or "").strip() for k in ("main", "accent", "sub")
    )
    return {
        "exists": True,
        "missing": missing,
        "palette_ok": palette_ok,
        "trigger": bible.get("trigger"),
        "fields": {k: bible.get(k) for k in BIBLE_LABELS},
    }


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
def count_media(directory: Path, suffixes):
    """Count matching files; os.walk does not follow symlinked directories."""
    directory = Path(directory)
    if not directory.is_dir():
        return 0, []
    files = []
    for root_dir, _dirs, names in os.walk(directory):
        for name in names:
            if Path(name).suffix.lower() in suffixes:
                files.append(Path(root_dir) / name)
    return len(files), sorted(p.name for p in files)


def dataset_report():
    """Stage 1 candidates and stage 2 curated training set."""
    cand_n, cand = count_media(CANDIDATE_DIR, IMAGE_SUFFIXES)
    train_n, train = count_media(TRAIN_DIR, IMAGE_SUFFIXES)

    captions = 0
    trigger_first = 0
    trigger_missing = 0
    bible = load_bible() or {}
    trigger = str(bible.get("trigger") or "").strip().lower()
    for p in sorted(TRAIN_DIR.rglob("*")) if TRAIN_DIR.is_dir() else []:
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
            cap = p.with_suffix(".txt")
            if cap.is_file():
                captions += 1
                head = cap.read_text(encoding="utf-8", errors="replace").split(",")[0].strip().lower()
                if not trigger:
                    trigger_missing += 1
                elif head == trigger:
                    trigger_first += 1
    return {
        "candidates": cand_n,
        "train": train_n,
        "captions": captions,
        "trigger": trigger or None,
        "trigger_first": trigger_first,
        "trigger_mismatch": max(0, captions - trigger_first),
        "sample": (cand or train)[:8],
    }


def lora_report():
    n, names = count_media(LORA_DIR, WEIGHT_SUFFIXES)
    selection = load_json(LORA_SELECTION_FILE)
    return {
        "checkpoints": n,
        "names": names[:20],
        "selected": (selection or {}).get("checkpoint"),
        "selected_strength": (selection or {}).get("strength"),
        "selected_at": (selection or {}).get("selected_at"),
    }


def save_storyboard(data: dict):
    MANGA_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = now_local().isoformat(timespec="seconds")
    _write_json_atomic(STORYBOARD_FILE, data)
    return data


def save_lora_selection(data: dict):
    """Record the checkpoint that passed the stage-2 gate."""
    LORA_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data.setdefault("selected_at", now_local().isoformat(timespec="seconds"))
    _write_json_atomic(LORA_SELECTION_FILE, data)
    return data


def storyboard_report():
    """Always returns the same keys so callers can read them unconditionally."""
    sb = load_json(STORYBOARD_FILE)
    if sb is None:
        return {"exists": False, "panels": 0, "invalid": [], "exported": 0,
                "export_names": [], "title": None}
    panels = sb.get("panels") or []
    # dialogue and camera may legitimately be empty (silent panel, static framing).
    required = ("panel", "shot", "character", "expression", "control")
    bad = []
    for i, p in enumerate(panels, 1):
        miss = [f for f in required if not str(p.get(f) or "").strip()]
        if miss:
            bad.append({"panel": p.get("panel", i), "missing": miss})
    exported, export_names = count_media(MANGA_DIR / "export", IMAGE_SUFFIXES)
    return {
        "exists": True,
        "panels": len(panels),
        "invalid": bad,
        "exported": exported,
        "export_names": export_names[:8],
        "title": sb.get("title"),
    }


def illust_report():
    finals, names = count_media(ILLUST_DIR / "final", IMAGE_SUFFIXES)
    manifest = load_json(ILLUST_MANIFEST_FILE) or {}
    entries = manifest.get("entries") or []
    return {
        "finals": finals,
        "names": names[:12],
        "logged": len(entries),
        "manifest_exists": bool(entries),
    }


def anim_report():
    clips, names = count_media(ANIM_DIR / "clips", VIDEO_SUFFIXES)
    finals, final_names = count_media(ANIM_DIR / "final", VIDEO_SUFFIXES)
    return {"clips": clips, "clip_names": names[:12], "finals": finals, "final_names": final_names[:8]}


# --------------------------------------------------------------------------- #
# gallery: every image the studio can show, from one place
# --------------------------------------------------------------------------- #
GALLERY_SOURCES = (
    ("comfy", "ComfyUI 输出", ("output",)),
    ("final", "插画终稿", ("03_illust", "final")),
    ("raw", "插画草稿", ("03_illust", "raw")),
    ("panels", "漫画分格", ("04_manga", "panels")),
    ("candidates", "候选图", ("01_dataset", "candidates")),
    ("frames", "动画帧", ("05_anim", "frames")),
)
GALLERY_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
GALLERY_MAX = 160


def comfy_root() -> Path:
    """ComfyUI install root from config/studio-env.ps1 (never hardcoded)."""
    return Path(load_studio_env().get("STUDIO_COMFYUI_DIR", "") or "")


def source_dir(key: str) -> Path:
    if key == "comfy":
        root = comfy_root()
        # A relative or empty root (ComfyUI not configured) must never degrade
        # into scanning the process working directory.
        return root / "output" if root.is_absolute() else Path()
    for name, _label, parts in GALLERY_SOURCES:
        if name == key:
            return ART_DIR.joinpath(*parts)
    return Path()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


def _media_entry(path: Path, key: str, label: str):
    try:
        stat = path.stat()
    except OSError:
        return None
    base = {"source": key, "source_label": label, "name": path.name,
            "size": stat.st_size, "mtime": _iso(stat.st_mtime),
            "kind": "video" if path.suffix.lower() in VIDEO_SUFFIXES else "image"}
    if key == "comfy":
        root = source_dir("comfy")
        try:
            rel = path.relative_to(root)
        except ValueError:
            return None
        sub = rel.parent.as_posix()
        base.update(rel=rel.as_posix(), subfolder="" if sub == "." else sub, type="output")
        return base
    base["rel"] = path.relative_to(ART_DIR).as_posix()
    return base


def gallery_snapshot(limit: int = GALLERY_MAX) -> dict:
    """Newest media across ComfyUI output and project/, plus per-source counts."""
    items, scanned = [], []
    for key, label, _parts in GALLERY_SOURCES:
        directory = source_dir(key)
        exists = directory.is_absolute() and directory.is_dir()
        found = []
        if exists:
            for root_dir, _dirs, names in os.walk(directory):
                for name in names:
                    if Path(name).suffix.lower() in GALLERY_SUFFIXES:
                        found.append(Path(root_dir) / name)
        for path in found:
            entry = _media_entry(path, key, label)
            if entry:
                items.append(entry)
        scanned.append({"source": key, "label": label,
                        "dir": str(directory) if directory.is_absolute() else "",
                        "exists": exists, "count": len(found)})

    items.sort(key=lambda e: e["mtime"], reverse=True)
    counts = {}
    for entry in items:
        counts[entry["source"]] = counts.get(entry["source"], 0) + 1
    return {"items": items[:limit], "total": len(items), "counts": counts,
            "scanned": scanned, "generated_at": now_local().isoformat(timespec="seconds")}


def media_url(entry: dict) -> str:
    """Stable URL for one gallery entry (served by GET /api/image)."""
    if entry.get("source") == "comfy":
        query = urlencode({"source": "comfy", "filename": entry.get("name", ""),
                           "subfolder": entry.get("subfolder", ""),
                           "type": entry.get("type", "output")})
    else:
        query = urlencode({"source": "project", "rel": entry.get("rel", "")})
    return "/api/image?" + query


def resolve_project_media(rel: str):
    """Resolve a project-relative media path, refusing anything outside project/."""
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        return None
    candidate = (ART_DIR / rel).resolve()
    try:
        candidate.relative_to(ART_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in GALLERY_SUFFIXES:
        return None
    return candidate


def resolve_comfy_media(filename: str, subfolder: str = ""):
    """Resolve one ComfyUI output file for 'reveal in folder'; stays under output/."""
    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    root = comfy_root()
    if not str(root):
        return None
    base = (root / "output").resolve()
    target = (base / (subfolder or "").strip().replace("\\", "/") / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(str(name or "illust")).stem).strip("._")
    return stem or "illust"


ILLUST_META_FIELDS = ("seed", "prompt", "negative", "steps", "cfg", "sampler",
                      "scheduler", "width", "height", "template", "workflow",
                      "prompt_id", "elapsed_s")


def archive_illust(data: bytes, filename: str, meta=None) -> dict:
    """Copy one rendered image into project/03_illust/final and record its recipe.

    Stage 3 asks for both a final image and a logged recipe; doing them in one
    function is what keeps the gate and the manifest from drifting apart.
    """
    if not data:
        raise ValueError("图片数据为空")
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError(f"不支持的图片格式：{suffix or '无扩展名'}")

    final_dir = ILLUST_DIR / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(filename)
    target = final_dir / f"{stem}{suffix}"
    index = 2
    while target.exists():
        target = final_dir / f"{stem}-{index}{suffix}"
        index += 1
    _write_bytes_atomic(target, data)

    entry = {"file": target.relative_to(ART_DIR).as_posix(),
             "saved_at": now_local().isoformat(timespec="seconds"),
             "bytes": len(data)}
    for key in ILLUST_META_FIELDS:
        if meta and meta.get(key) not in (None, ""):
            entry[key] = meta[key]

    manifest = load_json(ILLUST_MANIFEST_FILE) or {}
    entries = manifest.get("entries") or []
    entries.append(entry)
    manifest["version"] = manifest.get("version", 1)
    manifest["entries"] = entries
    manifest["updated_at"] = entry["saved_at"]
    _write_json_atomic(ILLUST_MANIFEST_FILE, manifest)
    return entry


def load_baseline():
    return load_json(BASELINE_FILE)


def save_baseline(data: dict):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data.setdefault("generated_at", now_local().isoformat(timespec="seconds"))
    _write_json_atomic(BASELINE_FILE, data)
    return data


# --------------------------------------------------------------------------- #
# stage gates
# --------------------------------------------------------------------------- #
def _band(value, low, high):
    """pass / warn / fail against an inclusive target band, or 'pending' at 0."""
    if value == 0:
        return "pending"
    if value < low:
        return "warn" if value >= low * 0.5 else "fail"
    return "pass" if value <= high else "warn"


def evaluate_stages(models_present=None, comfyui_online=False):
    """Return the 6 gates. A gate is the only thing that authorises the next stage."""
    models_present = models_present or {}
    base = load_baseline()
    bible = bible_report()
    data = dataset_report()
    lora = lora_report()
    illust = illust_report()
    board = storyboard_report()
    anim = anim_report()

    stage0_checks = [
        {"label": "三个必需权重在位", "ok": all(models_present.get(k) for k in
                                           ("anima-base-v1.0.safetensors",
                                            "qwen_3_06b_base.safetensors",
                                            "qwen_image_vae.safetensors")),
         "detail": ", ".join(k for k, v in models_present.items() if v) or "无"},
        {"label": "基准耗时已记录", "ok": base is not None,
         "detail": f"{base.get('elapsed_s')}s @ {base.get('width')}x{base.get('height')}"
                   if base else "运行 tests\\smoke_test.py"},
    ]

    stage1_checks = [
        {"label": "设定表字段齐全", "ok": bible["exists"] and not bible["missing"],
         "detail": "缺 " + ", ".join(bible["missing"]) if bible["missing"]
                   else (bible["trigger"] or "")},
        {"label": f"候选图 {CANDIDATE_TARGET[0]}-{CANDIDATE_TARGET[1]} 张",
         "ok": CANDIDATE_TARGET[0] <= data["candidates"] <= CANDIDATE_TARGET[1],
         "detail": str(data["candidates"])},
    ]

    stage2_checks = [
        {"label": f"训练集 {TRAIN_TARGET[0]}-{TRAIN_TARGET[1]} 张",
         "ok": TRAIN_TARGET[0] <= data["train"] <= TRAIN_TARGET[1], "detail": str(data["train"])},
        {"label": "每张图都有标签文件", "ok": data["train"] > 0 and data["captions"] == data["train"],
         "detail": f"{data['captions']}/{data['train']}"},
        {"label": "标签以触发词开头", "ok": data["train"] > 0 and data["trigger_mismatch"] == 0,
         "detail": f"不匹配 {data['trigger_mismatch']}"},
        {"label": "已选定 checkpoint", "ok": bool(lora["selected"]),
         "detail": lora["selected"] or f"写入 {LORA_SELECTION_FILE.name}"},
    ]

    stage3_checks = [
        {"label": "终稿已产出", "ok": illust["finals"] > 0, "detail": str(illust["finals"])},
        {"label": "种子/提示词/LoRA 强度已记录", "ok": illust["manifest_exists"],
         "detail": f"{illust['logged']} 条"},
    ]

    stage4_checks = [
        {"label": f"分镜表 {PANEL_TARGET[0]}-{PANEL_TARGET[1]} 格",
         "ok": PANEL_TARGET[0] <= board["panels"] <= PANEL_TARGET[1], "detail": str(board["panels"])},
        {"label": "分镜字段完整", "ok": board["exists"] and not board["invalid"],
         "detail": f"{len(board['invalid'])} 格缺字段" if board["invalid"] else "ok"},
        {"label": "整页已导出", "ok": board["exported"] > 0, "detail": str(board["exported"])},
    ]

    stage5_checks = [
        {"label": "已生成片段", "ok": anim["clips"] > 0, "detail": str(anim["clips"])},
        {"label": "已拼接成片", "ok": anim["finals"] > 0, "detail": str(anim["finals"])},
    ]

    gates = [
        {"stage": 0, "name": "环境准备", "checks": stage0_checks, "optional": False},
        {"stage": 1, "name": "角色设计", "checks": stage1_checks, "optional": False},
        {"stage": 2, "name": "训练角色 LoRA", "checks": stage2_checks, "optional": False},
        {"stage": 3, "name": "单张插画", "checks": stage3_checks, "optional": False},
        {"stage": 4, "name": "漫画页", "checks": stage4_checks, "optional": False},
        {"stage": 5, "name": "动画短片", "checks": stage5_checks, "optional": True},
    ]

    parked = paused_stages()
    for gate in gates:
        total = len(gate["checks"])
        done = sum(1 for c in gate["checks"] if c["ok"])
        if gate["stage"] in parked:
            # 按排期暂缓的阶段：定义保留，不参与阻塞判断
            gate["status"] = "paused"
            continue
        if done == 0:
            # Stage 0 is the entry gate: an unmet environment is a failure, not
            # "not started". Later gates genuinely start out untouched.
            gate["status"] = "fail" if gate["stage"] == 0 else "pending"
        elif done == total:
            gate["status"] = "pass"
        elif gate["optional"]:
            gate["status"] = "warn"
        else:
            gate["status"] = "warn" if done * 2 >= total else "fail"

    # The first unmet mandatory gate blocks everything after it.
    blocked_at = None
    for gate in gates:
        if not gate["optional"] and gate["status"] != "pass":
            blocked_at = gate["stage"]
            break
    for gate in gates:
        gate["blocked"] = blocked_at is not None and gate["stage"] > blocked_at

    return {
        "gates": gates,
        "blocked_at": blocked_at,
        "comfyui_online": comfyui_online,
        "counts": {
            "candidates": data["candidates"],
            "train": data["train"],
            "checkpoints": lora["checkpoints"],
            "finals": illust["finals"],
            "panels": board["panels"],
            "clips": anim["clips"],
        },
        "assets": {
            "bible": bible,
            "dataset": data,
            "lora": lora,
            "illust": illust,
            "storyboard": board,
            "anim": anim,
            "baseline": base,
        },
    }
