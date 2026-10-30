"""ComfyUI client: submit a rendered workflow, watch it, fetch the result.

This is the only module that talks HTTP to ComfyUI. The console handler stays a
thin transport layer; every decision (what to submit, how options are clamped,
how a queue entry maps to a UI state) lives here and is unit-testable.

All values that reach a workflow come from config/: the graph is rendered from
``config/workflows/*.json`` and the prompt text from ``config/prompts.json``.
Nothing is hardcoded here except safe clamps.

CLI (useful without the console):
    python app/comfy.py stats
    python app/comfy.py queue
    python app/comfy.py submit anima-txt2img.json '{"steps": 8, "width": 512}'
    python app/comfy.py history <prompt_id>
"""

from __future__ import annotations

import json
import mimetypes
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import comfyui_url, load_params, load_prompts, render_prompt  # noqa: E402
from workflows import render_named  # noqa: E402

DEFAULT_WORKFLOW = "anima-txt2img.json"

# Safety rails, not aesthetics: these keep a typo from asking the GPU for a
# 8192px / 4000-step render. Width and height are rounded to multiples of 8
# because every latent in the templates is /8.
LIMITS = {
    "width": (256, 2048, 1024),
    "height": (256, 2048, 1024),
    "steps": (1, 120, 40),
    "cfg": (1.0, 20.0, 4.5),
    "batch": (1, 4, 1),
}
SEED_MAX = 2 ** 32 - 1


class ComfyError(Exception):
    """ComfyUI rejected the request or is unreachable."""


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _base() -> str:
    return comfyui_url()


def _request(path: str, payload=None, timeout: float = 30.0):
    url = _base().rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(body.decode("utf-8") or "{}")
            return body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ComfyError(_explain_http_error(exc.code, detail)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ComfyError(f"ComfyUI 不可达（{_base()}）：{exc}") from exc


def _explain_http_error(code: int, body: str) -> str:
    """Turn ComfyUI's node_errors JSON into one actionable sentence."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return f"HTTP {code}: {body[:400]}"

    parts = []
    err = data.get("error")
    if isinstance(err, dict):
        parts.append(str(err.get("message") or err.get("type") or ""))
    elif err:
        parts.append(str(err))
    for node_id, info in (data.get("node_errors") or {}).items():
        for e in info.get("errors") or []:
            parts.append(f"节点 {node_id}（{info.get('class_type', '?')}）：{e.get('message')}")
    return "；".join(p for p in parts if p) or f"HTTP {code}"


# --------------------------------------------------------------------------- #
# read side
# --------------------------------------------------------------------------- #
def stats() -> dict:
    return _request("/system_stats", timeout=8) or {}


def queue() -> dict:
    data = _request("/queue", timeout=8) or {}
    return {
        "running": data.get("queue_running") or [],
        "pending": data.get("queue_pending") or [],
    }


def queue_summary() -> dict:
    try:
        q = queue()
        return {"running": len(q["running"]), "pending": len(q["pending"])}
    except ComfyError:
        return {"running": 0, "pending": 0, "offline": True}


def history(prompt_id: str) -> dict:
    data = _request(f"/history/{urllib.parse.quote(prompt_id)}", timeout=15) or {}
    return data.get(prompt_id) or {}


def view_bytes(filename: str, subfolder: str = "", kind: str = "output"):
    """Fetch one output file through ComfyUI's /view endpoint.

    The name is re-checked here as well as in the HTTP layer: this is the only
    function that turns a caller-supplied string into an upstream request.
    """
    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ComfyError(f"非法的文件名：{filename!r}")
    sub = (subfolder or "").strip().replace("\\", "/").strip("/")
    if ".." in sub.split("/"):
        raise ComfyError(f"非法的子目录：{subfolder!r}")

    query = urllib.parse.urlencode({"filename": name, "subfolder": sub, "type": kind})
    body = _request(f"/view?{query}", timeout=30)
    if not isinstance(body, (bytes, bytearray)):
        raise ComfyError("ComfyUI 未返回图片数据")
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return bytes(body), ctype


def interrupt() -> None:
    _request("/interrupt", {}, timeout=10)


def free_memory() -> None:
    """Ask ComfyUI to unload its models and release VRAM (keeps the server up)."""
    _request("/free", {"unload_models": True, "free_memory": True}, timeout=60)


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #
def _clamp_int(raw, low, high, default):
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def normalize_options(options=None) -> dict:
    """Clamp user options and translate them to workflow placeholder keys.

    Only keys the caller actually supplied are overridden, so an untouched form
    keeps the template's own ``__meta__.defaults`` — config stays authoritative.
    """
    raw = dict(options or {})
    overrides = {}

    for key in ("width", "height"):
        if raw.get(key) not in (None, ""):
            low, high, default = LIMITS[key]
            value = _clamp_int(raw[key], low, high, default)
            overrides[key] = max(low, (value // 8) * 8)

    if raw.get("steps") not in (None, ""):
        low, high, default = LIMITS["steps"]
        overrides["steps"] = _clamp_int(raw["steps"], low, high, default)

    if raw.get("cfg") not in (None, ""):
        low, high, default = LIMITS["cfg"]
        try:
            cfg = float(raw["cfg"])
        except (TypeError, ValueError):
            cfg = default
        overrides["cfg"] = round(max(low, min(high, cfg)), 2)

    if raw.get("batch") not in (None, ""):
        low, high, default = LIMITS["batch"]
        overrides["batch_size"] = _clamp_int(raw["batch"], low, high, default)

    seed = raw.get("seed")
    if seed in (None, "", "-1", -1) or str(seed).strip() == "-1":
        overrides["seed"] = random.randint(0, SEED_MAX)
    else:
        overrides["seed"] = _clamp_int(seed, 0, SEED_MAX, random.randint(0, SEED_MAX))

    for key, target in (("sampler", "sampler_name"), ("scheduler", "scheduler"),
                        ("denoise", "denoise")):
        value = raw.get(key)
        if value not in (None, ""):
            overrides[target] = value

    return {"overrides": overrides, "seed": overrides["seed"],
            **{k: v for k, v in overrides.items() if k in
               ("width", "height", "steps", "cfg", "batch_size", "sampler_name", "scheduler")}}


def prepare_generation(template_key: str, values=None, options=None,
                       workflow: str = "", prompt=None) -> dict:
    """Render prompt text + workflow graph for one generation request.

    ``prompt`` lets the UI send back manually edited text, so what the operator
    sees in the textarea is exactly what the GPU receives.
    """
    values = values or {}
    edited = prompt if isinstance(prompt, dict) else {}
    if str(edited.get("positive") or "").strip() or str(edited.get("negative") or "").strip():
        rendered = {"positive": str(edited.get("positive") or ""),
                    "negative": str(edited.get("negative") or ""), "missing": []}
    else:
        rendered = render_prompt(template_key, values)

    opts = normalize_options(options)
    name = (workflow or DEFAULT_WORKFLOW).strip() or DEFAULT_WORKFLOW
    if not name.endswith(".json"):
        name += ".json"

    overrides = dict(opts["overrides"])
    overrides["positive"] = rendered["positive"]
    overrides["negative"] = rendered["negative"]
    graph = render_named(name, overrides)

    return {
        "workflow": name,
        "template": template_key,
        "graph": graph,
        "prompt": {"positive": rendered["positive"], "negative": rendered["negative"],
                   "missing": rendered.get("missing") or []},
        "options": {k: v for k, v in opts.items() if k != "overrides"},
        "overrides": overrides,
    }


# --------------------------------------------------------------------------- #
# write side
# --------------------------------------------------------------------------- #
def submit(graph: dict, client_id: str = "") -> dict:
    payload = {"prompt": graph, "client_id": client_id or uuid.uuid4().hex}
    data = _request("/prompt", payload, timeout=60) or {}
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise ComfyError("ComfyUI 未返回 prompt_id")
    return {"prompt_id": prompt_id, "number": data.get("number")}


def _collect_outputs(entry: dict) -> list:
    """Images/videos from a /history entry, in a stable shape for the UI."""
    out = []
    for node_id, node_out in (entry.get("outputs") or {}).items():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs", "videos", "audio"):
            for item in node_out.get(key) or []:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                name = item["filename"]
                suffix = Path(name).suffix.lower()
                out.append({
                    "filename": name,
                    "subfolder": item.get("subfolder") or "",
                    "type": item.get("type") or "output",
                    "kind": "video" if suffix in (".mp4", ".webm", ".gif", ".webp")
                            else "image",
                    "node": node_id,
                })
    return out


def job_status(prompt_id: str) -> dict:
    """queued | running | done | error | unknown — plus outputs when done."""
    if not prompt_id:
        return {"state": "unknown", "message": "缺少 prompt_id"}

    entry = history(prompt_id)
    if entry:
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            messages = []
            for item in status.get("messages") or []:
                if isinstance(item, list) and len(item) == 2 and item[0] == "execution_error":
                    detail = item[1] or {}
                    messages.append(f"{detail.get('node_type', '?')}: "
                                    f"{detail.get('exception_message', '执行失败')}")
            return {"state": "error", "message": "；".join(messages) or "执行失败",
                    "outputs": []}
        outputs = _collect_outputs(entry)
        return {"state": "done", "outputs": outputs,
                "elapsed_s": _elapsed(entry), "message": ""}

    try:
        q = queue()
    except ComfyError as exc:
        return {"state": "unknown", "message": str(exc), "outputs": []}

    for i, item in enumerate(q["running"]):
        if _queue_prompt_id(item) == prompt_id:
            return {"state": "running", "outputs": [], "message": "正在渲染"}
    for i, item in enumerate(q["pending"]):
        if _queue_prompt_id(item) == prompt_id:
            return {"state": "queued", "outputs": [], "position": i + 1,
                    "message": f"排队中（第 {i + 1} 位）"}
    return {"state": "unknown", "outputs": [],
            "message": "任务已不在队列，也未出现在历史记录中"}


def _queue_prompt_id(item) -> str:
    # /queue entries are [number, prompt_id, prompt, extra_data, outputs].
    if isinstance(item, list) and len(item) > 1:
        return str(item[1])
    return ""


def _elapsed(entry: dict):
    messages = entry.get("status", {}).get("messages") or []
    start = end = None
    for item in messages:
        if not isinstance(item, list) or len(item) != 2:
            continue
        if item[0] == "execution_start":
            start = (item[1] or {}).get("timestamp")
        elif item[0] == "execution_success":
            end = (item[1] or {}).get("timestamp")
    if start and end:
        return round((end - start) / 1000, 1)
    return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "queue":
        print(json.dumps(queue_summary(), ensure_ascii=False))
        return 0
    if cmd == "history":
        if len(argv) < 2:
            print("usage: comfy.py history <prompt_id>")
            return 2
        print(json.dumps(job_status(argv[1]), ensure_ascii=False, indent=2))
        return 0
    if cmd == "submit":
        if len(argv) < 2:
            print("usage: comfy.py submit <workflow.json> ['{options json}']")
            return 2
        opts = json.loads(argv[2]) if len(argv) > 2 else {}
        prepared = prepare_generation("stage1_master", {}, opts, argv[1])
        result = submit(prepared["graph"])
        print(json.dumps({**result, "seed": prepared["options"].get("seed")},
                         ensure_ascii=False))
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(_main(sys.argv[1:]))
