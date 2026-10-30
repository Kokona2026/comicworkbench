"""Ollama client: list, warm up (load), unload, and VRAM accounting.

Loading a model into VRAM is exactly a generation with an empty prompt and a
positive ``keep_alive``; unloading is the same call with ``keep_alive: 0``.
Both return ``done_reason: load|unload`` (Ollama >= 0.4), which keeps this
module free of polling.

CLI:
    python app/ollama.py list
    python app/ollama.py load qwen3.5-9b-daily:latest
    python app/ollama.py unload qwen3.5-9b-daily:latest
    python app/ollama.py ps
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import load_studio_env  # noqa: E402

DEFAULT_HOST = "127.0.0.1:11434"
DEFAULT_KEEP_ALIVE = "15m"


class OllamaError(Exception):
    """Ollama is unreachable or rejected the request."""


def base_url(env=None) -> str:
    env = load_studio_env() if env is None else env
    host = str(env.get("STUDIO_OLLAMA_HOST") or DEFAULT_HOST).strip()
    if not host.startswith("http"):
        host = "http://" + host
    return host.rstrip("/")


def _request(path: str, payload=None, timeout: float = 30.0):
    url = base_url() + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise OllamaError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise OllamaError(f"Ollama 不可达（{base_url()}）：{exc}") from exc


def list_models() -> list:
    data = _request("/api/tags", timeout=10) or {}
    out = []
    for item in data.get("models") or []:
        details = item.get("details") or {}
        out.append({
            "name": item.get("name") or item.get("model") or "",
            "size": int(item.get("size") or 0),
            "params": details.get("parameter_size") or "",
            "family": details.get("family") or "",
            "quant": details.get("quantization_level") or "",
            "modified_at": item.get("modified_at") or "",
            "loaded": False,
            "vram": 0,
            "expires_at": "",
        })
    out.sort(key=lambda m: m["name"])
    return out


def loaded_models() -> list:
    data = _request("/api/ps", timeout=10) or {}
    out = []
    for item in data.get("models") or []:
        out.append({
            "name": item.get("name") or item.get("model") or "",
            "vram": int(item.get("size_vram") or 0),
            "size": int(item.get("size") or 0),
            "expires_at": item.get("expires_at") or "",
        })
    return out


def load(name: str, keep_alive: str = DEFAULT_KEEP_ALIVE) -> str:
    """Warm the model into VRAM; returns Ollama's done_reason."""
    if not (name or "").strip():
        raise OllamaError("缺少模型名")
    data = _request("/api/generate",
                    {"model": name, "prompt": "", "keep_alive": keep_alive},
                    timeout=900) or {}
    return str(data.get("done_reason") or "loaded")


def unload(name: str) -> str:
    if not (name or "").strip():
        raise OllamaError("缺少模型名")
    data = _request("/api/generate",
                    {"model": name, "prompt": "", "keep_alive": 0}, timeout=120) or {}
    return str(data.get("done_reason") or "unloaded")


def unload_all() -> int:
    count = 0
    for model in loaded_models():
        try:
            unload(model["name"])
            count += 1
        except OllamaError:
            continue
    return count


def snapshot() -> dict:
    """Models + which are resident, merged by name for the console."""
    online = True
    try:
        models = list_models()
    except OllamaError:
        online, models = False, []

    loaded = []
    if online:
        try:
            loaded = loaded_models()
        except OllamaError:
            loaded = []

    by_name = {model["name"]: model for model in models}
    vram_bytes = 0
    for item in loaded:
        vram_bytes += item["vram"]
        target = by_name.get(item["name"])
        if target is None:
            target = {"name": item["name"], "size": item["size"], "params": "",
                      "family": "", "quant": "", "modified_at": "", "loaded": False,
                      "vram": 0, "expires_at": ""}
            models.append(target)
            by_name[item["name"]] = target
        target.update(loaded=True, vram=item["vram"], expires_at=item["expires_at"])
        if item["size"]:
            target["size"] = item["size"]

    return {
        "online": online,
        "host": base_url(),
        "models": models,
        "loaded": loaded,
        "vram_bytes": vram_bytes,
        "keep_alive": DEFAULT_KEEP_ALIVE,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    try:
        if cmd == "list":
            print(json.dumps(list_models(), ensure_ascii=False, indent=2))
        elif cmd == "ps":
            print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        elif cmd == "load" and len(argv) > 1:
            print(load(argv[1]))
        elif cmd == "unload" and len(argv) > 1:
            print(unload(argv[1]))
        elif cmd == "unload-all":
            print(f"unloaded {unload_all()}")
        else:
            print(__doc__)
            return 2
    except OllamaError as exc:
        print(f"[fail] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(_main(sys.argv[1:]))
