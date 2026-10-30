"""Local console for the anime manga studio.

Everything built for this deployment is reachable from one loopback page:
  - environment / service state (ComfyUI, GPU, ports)
  - the 6-stage workflow with its acceptance gates
  - prompt template rendering from the character bible
  - workflow validation against the live /object_info
  - the audit engine and its reports
  - smoke / lint tests

Startup: prepare dirs -> verify Python and config -> startup audit
-> optionally start ComfyUI -> bind 127.0.0.1 -> open browser.

Endpoints (loopback only):
  GET  /                  single-page console
  GET  /api/state         services, GPU, stage gates, audit summary, queue
  GET  /api/report        latest audit JSON
  GET  /api/logs          tail of reports/audit.log
  GET  /api/prompts       prompt templates and parameter profiles
  GET  /api/bible         character bible plus field labels (editor payload)
  GET  /api/workflows     workflow templates and validation status
  GET  /api/models        local LLM inventory (Ollama) + VRAM + ComfyUI state
  POST /api/models/load   {"name": ...}   warm one model into VRAM
  POST /api/models/unload {"name": ...}   release one model
  POST /api/models/unload_all             release every loaded model
  POST /api/fix           {"id": "NET-01"} one-click remediation for a finding
  GET  /api/gallery       newest media from ComfyUI output and project/
  GET  /api/image         proxy one image (source=comfy|project, &download=1)
  GET  /api/job           progress of one generation (prompt_id)
  POST /api/prompt        {"key": ..., "values": {...}}  render one template
  POST /api/generate      {"template":..., "values":..., "options":...}
  POST /api/illust        archive one finished render into project/03_illust
  POST /api/action        {"action": "...", "params": {...}}  (reveal/open_folder/interrupt)
  POST /api/audit         {"deep": bool}
  POST /api/asset         {"kind": "bible"|"storyboard"|"lora", "data": {...}}
  GET  /api/library       LoRA index (trigger tags, training base) with family counts
  GET  /api/history       generation history, newest first
  POST /api/history/attach {"id":..., "outputs": [...]}  attach thumbnails
  POST /api/history/delete {"id":...} or {"clear": true}
  POST /api/llm           {"task": "tags"|"rewrite"|"negative", "brief":..., "context":...}
  GET  /report.md         markdown audit report
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import (  # noqa: E402
    APP_DIR, PROJECT_ROOT, STATIC_DIR, REPORTS_DIR, LOG_DIR, DOCS_DIR,
    archive_illust, bible_editor_state, gallery_snapshot, load_ui, media_url,
    resolve_comfy_media, resolve_project_media,
    load_studio_env, comfyui_url, console_port,
    load_params, load_prompts, save_baseline,
    save_bible, save_storyboard, save_lora_selection,
    load_fields, save_fields,
    evaluate_stages, load_manifest,
)
from workflows import validate_all, WorkflowError  # noqa: E402
from comfy import (  # noqa: E402
    ComfyError, free_memory, interrupt as comfy_interrupt, job_status,
    prepare_generation, queue_summary, submit, view_bytes,
)
import ollama  # noqa: E402
import remediation  # noqa: E402
import library  # noqa: E402
import lora_stack  # noqa: E402

LOG_FILE = REPORTS_DIR / "audit.log"

AUDIT_LOCK = threading.Lock()
STARTED_AT = time.time()
COMFY_STATUS = {"state": "unknown", "message": "", "ts": time.time()}
_COMFY_LOCK = threading.Lock()


class AuditBusy(RuntimeError):
    """Another audit already holds the lock."""

_STUDIO_ENV = load_studio_env()
COMFY_URL = comfyui_url(_STUDIO_ENV)
DEFAULT_PORT = console_port(_STUDIO_ENV)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def setup_logging():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            backup = LOG_FILE.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            LOG_FILE.rename(backup)
    except OSError:
        pass

    logger = logging.getLogger("studio")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-5s %(message)s", "%H:%M:%S")
    for handler in (logging.FileHandler(LOG_FILE, encoding="utf-8"),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def http_get(url, timeout=3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except Exception:  # noqa: BLE001
        return None, b""


def http_ok(url, timeout=3.0):
    code, _ = http_get(url, timeout)
    return code is not None and 200 <= code < 300


def http_json(url, payload=None, timeout=20):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_script(name, timeout, log, args=None, background=False, background_probe=10):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           str(PROJECT_ROOT / "scripts" / name)]
    if args:
        cmd += args
    log.info("run %s%s", name, " (background)" if background else "")

    # A script that spawns a long-lived server must NOT be captured: the child
    # inherits the pipe and subprocess.run() would block until that server exits.
    #
    # So: probe for a short window instead of assuming success. A script that
    # dies immediately (missing launcher, bad config) is reported as a failure
    # with its own output, rather than as "starting" forever.
    if background:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    cwd=str(PROJECT_ROOT),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            log.exception("%s background start failed: %s", name, exc)
            return False, str(exc)
        try:
            proc.wait(timeout=background_probe)
        except subprocess.TimeoutExpired:
            return True, "已转入后台启动"
        out = (proc.stdout.read() or b"").decode("utf-8", "replace").strip() if proc.stdout else ""
        for line in out.splitlines()[-6:]:
            if line.strip():
                log.info("  %s", line.strip())
        if proc.returncode != 0:
            log.error("%s exited early with code %s", name, proc.returncode)
            return False, (out[-1200:] or f"{name} 立即退出（{proc.returncode}）")
        return True, "已转入后台启动"

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(PROJECT_ROOT))
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        for line in (out + "\n" + err).splitlines()[-8:]:
            if line.strip():
                log.info("  %s", line.strip())
        return proc.returncode == 0, (out or err)[-2000:]
    except subprocess.TimeoutExpired:
        return False, f"{name} 超时（{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_python(rel, args, timeout, log):
    cmd = [sys.executable, str(PROJECT_ROOT / rel)] + [str(a) for a in args]
    log.info("run %s %s", rel, " ".join(str(a) for a in args))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(PROJECT_ROOT))
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        text = (out + ("\n" + err if err.strip() else "")).strip()
        return proc.returncode == 0, text[-4000:]
    except subprocess.TimeoutExpired:
        return False, f"{rel} 超时（{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=8).stdout.decode("utf-8", "replace").strip()
        if not out:
            return None
        name, used, total, util = [p.strip() for p in out.splitlines()[0].split(",")[:4]]
        return {"name": name, "used_mb": int(float(used)), "total_mb": int(float(total)),
                "util": int(float(util))}
    except Exception:  # noqa: BLE001
        return None


def set_comfy_state(state, message, hint=""):
    with _COMFY_LOCK:
        COMFY_STATUS.update(state=state, message=message, hint=hint, ts=time.time())


def comfy_state():
    with _COMFY_LOCK:
        return dict(COMFY_STATUS)


def comfyui_launcher():
    """Mirror of Get-ComfyUiLauncher in config/studio-env.ps1.

    Existence is checked in Python so a missing install fails in milliseconds
    instead of spawning a doomed PowerShell process and polling for minutes.
    """
    root = Path(_STUDIO_ENV.get("STUDIO_COMFYUI_DIR", ""))
    if not root or not root.is_dir():
        return None
    for candidate in ("main.py", "run_nvidia_gpu.bat",
                      Path("python_embeded") / "python.exe"):
        path = root / candidate
        if path.exists():
            return path
    return None


def comfyui_hint():
    root = _STUDIO_ENV.get("STUDIO_COMFYUI_DIR", "<ComfyUI 根目录>")
    return (f"未找到 ComfyUI（{root} 下没有 main.py / run_nvidia_gpu.bat）。\n"
            f"安装便携版后执行：\n"
            f'  setx STUDIO_COMFYUI_DIR "D:\\ComfyUI"\n'
            f"然后重新打开控制台。")


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def comfy_info():
    online = http_ok(f"{COMFY_URL}/system_stats", 2)
    info = {}
    if online:
        try:
            info = http_json(f"{COMFY_URL}/system_stats", timeout=4) or {}
        except Exception:  # noqa: BLE001
            pass
    return online, info


def audit_summary():
    f = REPORTS_DIR / "audit-latest.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return {"score": d.get("score"), "grade": d.get("grade"),
                "summary": d.get("summary"), "generated_at": d.get("generated_at")}
    except Exception:  # noqa: BLE001
        return None


def model_presence():
    comfy_dir = _STUDIO_ENV.get("STUDIO_COMFYUI_DIR", "")
    out = {}
    for f in load_manifest().get("files") or []:
        target = Path(comfy_dir) / "models" / f["target"] / f["name"] if comfy_dir else None
        out[f["name"]] = bool(target and target.is_file())
    return out


def build_state():
    online, info = comfy_info()
    models = model_presence()
    stages = evaluate_stages(models, online)
    launcher = comfyui_launcher()

    # State is derived on every poll, so a stale "starting" can never stick.
    if online:
        set_comfy_state("online", "ComfyUI 已就绪")
    elif launcher is None:
        set_comfy_state("missing", "未安装 ComfyUI", comfyui_hint())
    elif comfy_state().get("state") in ("unknown", "missing", "online"):
        set_comfy_state("offline", "ComfyUI 未运行")

    return {
        "comfyui": {"online": online, "url": COMFY_URL,
                    "version": (info.get("system") or {}).get("comfyui_version"),
                    "launcher": str(launcher or ""),
                    **comfy_state()},
        "stages": stages,
        "gpu": gpu_stats(),
        "models": models,
        "audit": audit_summary(),
        "auditing": AUDIT_LOCK.locked(),
        "queue": queue_summary(),
        "ui": load_ui(),
        "uptime_s": int(time.time() - STARTED_AT),
        "project_root": str(PROJECT_ROOT),
        "console_port": DEFAULT_PORT,
    }


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def wait_comfy(seconds=180, probe_every=2):
    waited = 0
    while waited < seconds:
        if http_ok(f"{COMFY_URL}/system_stats", 2):
            return True
        time.sleep(probe_every)
        waited += probe_every
    return False


def action_comfyui_start(params, log):
    if http_ok(f"{COMFY_URL}/system_stats", 2):
        set_comfy_state("online", "ComfyUI 已就绪")
        return True, "ComfyUI 已在运行"

    # Fail fast: no launcher means no start attempt, no polling, no false hope.
    if not comfyui_launcher():
        hint = comfyui_hint()
        set_comfy_state("missing", "未安装 ComfyUI", hint)
        log.warning("ComfyUI launcher missing under %s",
                    _STUDIO_ENV.get("STUDIO_COMFYUI_DIR", ""))
        return False, hint

    set_comfy_state("starting", "正在启动 ComfyUI…")
    ok, out = run_script("10-start-comfyui.ps1", 300, log, background=True,
                         background_probe=20)
    if not ok:
        set_comfy_state("error", f"启动失败：{out[-200:]}", out[-800:])
        return False, f"启动失败：{out[-600:]}"

    def waiter():
        if wait_comfy(180):
            set_comfy_state("online", "ComfyUI 已就绪")
            log.info("ComfyUI online at %s", COMFY_URL)
        else:
            set_comfy_state("error", "ComfyUI 未在 180s 内就绪",
                            "查看 logs\\comfyui.err.log；显存不足时降低分辨率或先停止其他占用进程。")

    threading.Thread(target=waiter, daemon=True).start()
    return True, "ComfyUI 启动中（首次加载约 30-120 秒）"


def do_action(action, params, log):
    if action == "comfyui_start":
        return action_comfyui_start(params, log)
    if action == "comfyui_stop":
        ok, out = run_script("11-stop-comfyui.ps1", 90, log)
        set_comfy_state("offline", "ComfyUI 已停止")
        return ok, "ComfyUI 已停止（显存已释放）" if ok else f"停止失败：{out[-300:]}"
    if action == "init_project":
        ok, out = run_script("00-init-project.ps1", 120, log)
        return ok, "项目目录已就绪" if ok else f"初始化失败：{out[-300:]}"
    if action == "check_env":
        ok, out = run_script("01-check-env.ps1", 180, log)
        return ok, out
    if action == "test_lint":
        return run_python("tests/lint_test.py", [], 180, log)
    if action == "test_smoke":
        args = []
        for flag in ("width", "height", "steps", "cfg", "seed", "timeout"):
            if params.get(flag) not in (None, ""):
                args += [f"--{flag}", str(params[flag])]
        return run_python("tests/smoke_test.py", args, 3600, log)
    if action == "verify_workflow":
        return run_python("tests/workflow_test.py", [], 300, log)
    if action == "open_comfyui":
        webbrowser.open(COMFY_URL)
        return True, "已在浏览器打开 ComfyUI"
    if action == "comfyui_interrupt":
        try:
            comfy_interrupt()
        except ComfyError as exc:
            return False, f"中断失败：{exc}"
        set_comfy_state("offline", "已中断当前任务")
        return True, "已请求中断当前任务"
    if action == "reveal":
        target = None
        if (params.get("source") or "project") == "comfy":
            target = resolve_comfy_media(params.get("filename") or "",
                                         params.get("subfolder") or "")
        else:
            target = resolve_project_media(params.get("rel") or "")
        if target is None:
            return False, "找不到该文件（可能已被移动或删除）"
        try:
            subprocess.Popen(["explorer", f"/select,{target}"])
        except OSError as exc:
            return False, f"无法打开资源管理器：{exc}"
        return True, "已在资源管理器中定位"
    if action == "open_folder":
        target = open_folder_target(params.get("which") or "output")
        if target is None:
            return False, "目录不可用（先安装 ComfyUI 或生成本地产出）"
        try:
            os.startfile(str(target))  # noqa: S606 - operator opens a local folder
        except (AttributeError, OSError) as exc:
            return False, f"打开失败：{exc}"
        return True, f"已打开 {target}"
    if action == "free_vram":
        notes = []
        try:
            free_memory()
            notes.append("ComfyUI 模型已卸载")
        except ComfyError as exc:
            notes.append(f"ComfyUI 未释放（{exc}）")
        try:
            notes.append(f"Ollama 卸载 {ollama.unload_all()} 个模型")
        except ollama.OllamaError as exc:
            notes.append(f"Ollama 不可达（{exc}）")
        return True, "；".join(notes)
    if action == "dedupe_comfy":
        ok, out = run_script("12-dedupe-comfyui.ps1", 120, log)
        return ok, ("已清理重复实例" if ok else f"清理失败：{out[-300:]}")
    return False, f"未知操作：{action}"


def open_folder_target(which):
    """Folder behind the two 'open' buttons; keeps paths out of the frontend."""
    from studio import comfy_root, ILLUST_DIR
    if which == "project":
        ILLUST_DIR.mkdir(parents=True, exist_ok=True)
        return ILLUST_DIR if ILLUST_DIR.is_dir() else None
    root = comfy_root()
    if not str(root):
        return None
    output = root / "output"
    return output if output.is_dir() else None


# --------------------------------------------------------------------------- #
# local models
# --------------------------------------------------------------------------- #
def models_payload():
    """Everything the 模型 page needs: LLM inventory, VRAM, ComfyUI state."""
    online_now, _ = comfy_info()
    return {
        "ollama": ollama.snapshot(),
        "gpu": gpu_stats(),
        "comfyui": {"online": online_now, "url": COMFY_URL,
                    "state": comfy_state().get("state"),
                    "message": comfy_state().get("message")},
    }


def model_load(name, log):
    try:
        reason = ollama.load(name)
    except ollama.OllamaError as exc:
        return False, str(exc)
    log.info("ollama load %s (%s)", name, reason)
    return True, f"{name} 已加载到显存"


def model_unload(name, log):
    try:
        reason = ollama.unload(name)
    except ollama.OllamaError as exc:
        return False, str(exc)
    log.info("ollama unload %s (%s)", name, reason)
    return True, f"{name} 已卸载"


def latest_findings():
    """Findings from the last written report, or [] when there is none."""
    report = REPORTS_DIR / "audit-latest.json"
    if not report.exists():
        return []
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("findings") or []


def save_asset(kind, data, log):
    if not isinstance(data, dict):
        return False, "data 必须是对象"
    try:
        if kind == "bible":
            save_bible(data)
        elif kind == "storyboard":
            save_storyboard(data)
        elif kind == "lora":
            save_lora_selection(data)
        elif kind == "baseline":
            save_baseline(data)
        else:
            return False, f"未知资产类型：{kind}"
    except (OSError, TypeError, ValueError) as exc:
        log.warning("save %s failed: %s", kind, exc)
        return False, f"保存失败：{exc}"
    log.info("asset saved: %s", kind)
    return True, f"{kind} 已保存"


def loras_of(graph: dict) -> list:
    """LoRA stack of a rendered graph, in application order."""
    out = []
    for node in (graph or {}).values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") not in ("LoraLoaderModelOnly", "LoraLoader"):
            continue
        inputs = node.get("inputs") or {}
        name = inputs.get("lora_name")
        if not name:
            continue
        strength = inputs.get("strength_model", inputs.get("strength"))
        out.append({"name": name, "strength": strength})
    return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "AnimaStudio/1.0"
    log = None
    notes = []

    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _tail_log(self, lines=220):
        if not LOG_FILE.exists():
            return []
        return LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(),
                              "text/html; charset=utf-8")
        if path == "/api/state":
            try:
                return self._send(200, build_state())
            except Exception as exc:  # noqa: BLE001
                self.log.exception("state failed: %s", exc)
                return self._send(500, {"error": str(exc)})
        if path == "/api/health":
            online, _ = comfy_info()
            return self._send(200, {"ok": True, "comfyui": online,
                                    "uptime_s": int(time.time() - STARTED_AT),
                                    "notes": self.notes})
        if path == "/api/report":
            report = REPORTS_DIR / "audit-latest.json"
            if not report.exists():
                return self._send(404, {"error": "no report yet"})
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return self._send(500, {"error": f"report unreadable: {exc}"})
            remediation.annotate(data.get("findings") or [])
            return self._send(200, data)
        if path == "/api/logs":
            lines = 220
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in query.split("&"):
                if part.startswith("tail="):
                    try:
                        lines = max(20, min(2000, int(part.split("=", 1)[1])))
                    except ValueError:
                        pass
            size_kb = round(LOG_FILE.stat().st_size / 1024, 1) if LOG_FILE.exists() else 0
            return self._send(200, {"file": "reports/audit.log", "size_kb": size_kb,
                                    "lines": self._tail_log(lines)})
        if path == "/api/prompts":
            return self._send(200, {"templates": load_prompts(), "params": load_params()})
        if path == "/api/library":
            loras = library.lora_index(self._query().get("force") == "1")
            families = {}
            for item in loras:
                families[item["family"]] = families.get(item["family"], 0) + 1
            return self._send(200, {"loras": loras, "families": families})
        if path == "/api/history":
            try:
                limit = int(self._query().get("limit") or 60)
            except (TypeError, ValueError):
                limit = 60
            return self._send(200, {"items": library.history_list(limit)})
        if path == "/api/cards":
            return self._send(200, {"items": library.cards_list()})
        if path == "/api/presets":
            return self._send(200, {"presets": library.load_presets()})
        if path == "/api/fields":
            return self._send(200, {"fields": load_fields()})
        if path == "/api/bible":
            return self._send(200, bible_editor_state())
        if path == "/api/models":
            return self._send(200, models_payload())
        if path == "/api/workflows":
            info = {}
            try:
                info = http_json(f"{COMFY_URL}/object_info", timeout=20) or {}
            except Exception:  # noqa: BLE001
                pass
            return self._send(200, {"online": bool(info), "items": validate_all(info)})
        if path == "/api/stages":
            return self._send(200, evaluate_stages(model_presence(), http_ok(
                f"{COMFY_URL}/system_stats", 2)))
        if path == "/report.md":
            f = REPORTS_DIR / "audit-latest.md"
            if not f.exists():
                return self._send(404, {"error": "no report yet"})
            return self._send(200, f.read_bytes(), "text/markdown; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="audit-latest.md"'})
        if path == "/studio.ico":
            icon = STATIC_DIR / "studio.ico"
            if not icon.exists():
                return self._send(404, {"error": "no icon"})
            return self._send(200, icon.read_bytes(), "image/x-icon",
                              {"Cache-Control": "private, max-age=86400"})
        if path == "/api/gallery":
            return self._send(200, self._gallery())
        if path == "/api/image":
            return self._serve_image(self._query())
        if path == "/api/job":
            return self._send(200, job_status(self._query().get("prompt_id") or ""))
        return self._send(404, {"error": "not found"})

    def _gallery(self):
        try:
            limit = int(load_ui().get("gallery_limit") or 160)
        except (TypeError, ValueError):
            limit = 160
        snapshot = gallery_snapshot(max(20, min(600, limit)))
        for item in snapshot["items"]:
            item["url"] = media_url(item)
        return snapshot

    def _serve_image(self, query):
        source = (query.get("source") or "project").lower()
        download = str(query.get("download") or "") in ("1", "true", "yes")
        name = "image"
        try:
            if source == "comfy":
                filename = query.get("filename") or ""
                if not filename or "/" in filename or "\\" in filename or ".." in filename:
                    return self._send(400, {"error": "invalid filename"})
                data, ctype = view_bytes(filename, query.get("subfolder") or "",
                                         query.get("type") or "output")
                name = filename
            else:
                path = resolve_project_media(query.get("rel") or "")
                if path is None:
                    return self._send(404, {"error": "not found"})
                data = path.read_bytes()
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                name = path.name
        except ComfyError as exc:
            return self._send(502, {"error": str(exc)})
        except OSError as exc:
            return self._send(404, {"error": str(exc)})

        extra = {"Cache-Control": "private, max-age=30"}
        if download:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "image"
            extra["Content-Disposition"] = f'attachment; filename="{safe}"'
        return self._send(200, data, ctype, extra)

    def _read_json(self, max_bytes=65536):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._send(415, {"error": "application/json required"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid content-length"})
            return None
        if length > max_bytes:
            self._send(413, {"error": "payload too large"})
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return None

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = self.path.split("?", 1)[0]

        if path == "/api/audit":
            payload = self._read_json()
            if payload is None:
                return
            try:
                result = run_audit(bool(payload.get("deep")), self.log)
            except AuditBusy:
                return self._send(409, {"error": "audit already running"})
            except Exception as exc:  # noqa: BLE001
                self.log.exception("audit failed: %s", exc)
                return self._send(500, {"error": f"audit failed: {exc}"})
            remediation.annotate(result.get("findings") or [])
            return self._send(200, result)

        if path == "/api/action":
            payload = self._read_json()
            if payload is None:
                return
            action = payload.get("action")
            params = payload.get("params") or {}
            try:
                ok, message = do_action(action, params, self.log)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("action %s failed: %s", action, exc)
                return self._send(500, {"ok": False, "message": str(exc)})
            self.log.info("action %s -> %s", action, "ok" if ok else "fail")
            return self._send(200 if ok else 400, {"ok": ok, "message": message})

        if path == "/api/prompt":
            payload = self._read_json()
            if payload is None:
                return
            try:
                from studio import render_prompt
                result = render_prompt(payload.get("key", ""), payload.get("values") or {})
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": str(exc)})
            return self._send(200, result)

        if path == "/api/asset":
            payload = self._read_json()
            if payload is None:
                return
            ok, message = save_asset(payload.get("kind", ""), payload.get("data"), self.log)
            return self._send(200 if ok else 400, {"ok": ok, "message": message})

        if path == "/api/generate":
            payload = self._read_json(max_bytes=262144)
            if payload is None:
                return
            try:
                prepared = prepare_generation(payload.get("template") or "",
                                              payload.get("values") or {},
                                              payload.get("options") or {},
                                              payload.get("workflow") or "",
                                              payload.get("prompt") or None)
            except KeyError as exc:
                return self._send(404, {"error": f"未知模板：{exc}"})
            except (WorkflowError, ValueError) as exc:
                return self._send(400, {"error": str(exc)})
            except ComfyError as exc:
                return self._send(502, {"error": str(exc)})
            stack = payload.get("loras") or []
            if stack:
                known = {item["name"] for item in library.lora_index()}
                try:
                    graph, applied = lora_stack.apply_stack(prepared["graph"], stack, known)
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                prepared["graph"] = graph
                prepared["loras"] = applied
            try:
                result = submit(prepared["graph"], payload.get("client_id") or "")
            except ComfyError as exc:
                self.log.warning("generate failed: %s", exc)
                return self._send(502, {"error": str(exc)})
            self.log.info("generate %s template=%s seed=%s -> %s",
                          prepared["workflow"], prepared["template"],
                          prepared["options"].get("seed"), result["prompt_id"])
            entry = library.history_add({
                "template": prepared["template"],
                "workflow": prepared["workflow"],
                "prompt_id": result["prompt_id"],
                "positive": prepared["prompt"]["positive"],
                "negative": prepared["prompt"]["negative"],
                "options": prepared["options"],
                "loras": loras_of(prepared["graph"]),
                "outputs": [],
            })
            return self._send(200, {
                **result,
                "history_id": entry.get("id"),
                "workflow": prepared["workflow"],
                "template": prepared["template"],
                "options": prepared["options"],
                "prompt": prepared["prompt"],
                "queue": queue_summary(),
            })

        if path == "/api/history/attach":
            payload = self._read_json(max_bytes=131072)
            if payload is None:
                return
            ok = library.history_attach(str(payload.get("id") or ""),
                                        payload.get("outputs") or [])
            return self._send(200 if ok else 404, {"ok": ok})

        if path == "/api/history/delete":
            payload = self._read_json()
            if payload is None:
                return
            removed = library.history_delete(str(payload.get("id") or ""),
                                             bool(payload.get("clear")))
            return self._send(200, {"ok": True, "removed": removed})

        if path == "/api/llm":
            payload = self._read_json(max_bytes=131072)
            if payload is None:
                return
            result = library.llm_assist(
                task=str(payload.get("task") or "rewrite"),
                brief=str(payload.get("brief") or ""),
                context=str(payload.get("context") or ""),
                negative=str(payload.get("negative") or ""),
                model=str(payload.get("model") or ""),
            )
            return self._send(200 if not result.get("error") else 502, result)

        if path == "/api/cards":
            payload = self._read_json(max_bytes=131072)
            if payload is None:
                return
            action = str(payload.get("action") or "save")
            try:
                if action == "delete":
                    ok = library.card_delete(str(payload.get("id") or ""))
                    return self._send(200 if ok else 404, {"ok": ok,
                                                           "items": library.cards_list()})
                if action == "rename":
                    library.card_rename(str(payload.get("id") or ""),
                                        str(payload.get("name") or ""))
                    return self._send(200, {"ok": True, "items": library.cards_list()})
                entry = library.card_save(name=str(payload.get("name") or ""),
                                          fields=payload.get("fields") or {},
                                          text=str(payload.get("text") or ""),
                                          source=str(payload.get("source") or "manual"),
                                          card_id=str(payload.get("id") or ""))
                return self._send(200, {"ok": True, "card": entry,
                                        "items": library.cards_list()})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/fields":
            payload = self._read_json(max_bytes=65536)
            if payload is None:
                return
            try:
                saved = save_fields(payload.get("fields") or [])
                return self._send(200, {"ok": True, "fields": saved})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/presets":
            payload = self._read_json(max_bytes=65536)
            if payload is None:
                return
            try:
                saved = library.set_field_presets(str(payload.get("field") or ""),
                                                  payload.get("options") or [])
                return self._send(200, saved)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/lora/alias":
            payload = self._read_json()
            if payload is None:
                return
            try:
                return self._send(200, library.save_alias(str(payload.get("name") or ""),
                                                          str(payload.get("alias") or "")))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/lora/delete":
            payload = self._read_json()
            if payload is None:
                return
            try:
                return self._send(200, library.delete_lora(str(payload.get("name") or "")))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/illust":
            payload = self._read_json(max_bytes=131072)
            if payload is None:
                return
            source = payload.get("source") or {}
            filename = source.get("filename") or ""
            if not filename:
                return self._send(400, {"error": "缺少 filename"})
            try:
                data, _ctype = view_bytes(filename, source.get("subfolder") or "",
                                          source.get("type") or "output")
                entry = archive_illust(data, filename, payload.get("meta") or {})
            except ComfyError as exc:
                return self._send(502, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                return self._send(400, {"error": str(exc)})
            self.log.info("illust archived: %s", entry["file"])
            return self._send(200, {"ok": True, "entry": entry,
                                    "url": media_url({"source": "final", "rel": entry["file"]})})

        if path == "/api/models/load":
            payload = self._read_json()
            if payload is None:
                return
            ok, message = model_load(payload.get("name") or "", self.log)
            return self._send(200 if ok else 400, {"ok": ok, "message": message})

        if path == "/api/models/unload":
            payload = self._read_json()
            if payload is None:
                return
            ok, message = model_unload(payload.get("name") or "", self.log)
            return self._send(200 if ok else 400, {"ok": ok, "message": message})

        if path == "/api/models/unload_all":
            payload = self._read_json()
            if payload is None:
                return
            try:
                count = ollama.unload_all()
            except ollama.OllamaError as exc:
                return self._send(400, {"ok": False, "message": str(exc)})
            self.log.info("ollama unload_all: %s", count)
            return self._send(200, {"ok": True, "message": f"已卸载 {count} 个模型",
                                    "count": count})

        if path == "/api/fix":
            payload = self._read_json()
            if payload is None:
                return
            finding_id = str(payload.get("id") or "")
            finding = next((f for f in latest_findings() if f.get("id") == finding_id), None)
            if finding is None:
                return self._send(404, {"error": f"未找到检查项：{finding_id}"})
            fix = remediation.plan(finding)
            if not fix:
                return self._send(400, {"error": "该检查项需要人工处理，没有一键修复"})
            if fix.get("kind") != "action":
                return self._send(400, {"error": f"该修复在页面内完成（{fix['kind']}）"})
            ok, message = do_action(fix["action"], fix.get("params") or {}, self.log)
            self.log.info("fix %s -> %s (%s)", finding_id, fix["action"],
                          "ok" if ok else "fail")
            return self._send(200 if ok else 400,
                              {"ok": ok, "message": message, "action": fix["action"]})

        return self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------- #
# startup
# --------------------------------------------------------------------------- #
def run_audit(deep, log):
    from audit_engine import run_audit as engine_run

    def engine_log(level, msg):
        mapping = {"DEBUG": log.debug, "INFO": log.info, "WARN": log.warning,
                   "WARNING": log.warning, "ERROR": log.error}
        mapping.get(str(level).upper(), log.info)(msg)

    # Non-blocking: a second concurrent request must get 409 instead of queueing
    # behind the first one and doubling the work on the same report files.
    if not AUDIT_LOCK.acquire(blocking=False):
        raise AuditBusy("another audit is already running")
    try:
        result = engine_run(deep=deep, write=True, logger=engine_log)
    finally:
        AUDIT_LOCK.release()
    log.info("audit done: score=%s fail=%s warn=%s", result["score"],
             result["summary"]["fail"], result["summary"]["warn"])
    return result


def preflight(log, with_comfyui=False):
    notes = []
    for d in (REPORTS_DIR, LOG_DIR, DOCS_DIR, PROJECT_ROOT / "project"):
        d.mkdir(parents=True, exist_ok=True)

    log.info("Python %s on %s", sys.version.split()[0], sys.platform)
    if sys.version_info < (3, 9):
        return False, ["Python 版本过低（需 3.9+）"]

    if not (PROJECT_ROOT / "config" / "studio-env.ps1").exists():
        return False, ["缺少 config/studio-env.ps1"]

    launcher = comfyui_launcher()
    if launcher is None:
        # Do not attempt a start that cannot succeed: report the fix instead.
        set_comfy_state("missing", "未安装 ComfyUI", comfyui_hint())
        notes.append(f"未安装 ComfyUI（{_STUDIO_ENV.get('STUDIO_COMFYUI_DIR', '')}）")
        log.warning("ComfyUI launcher missing under %s",
                    _STUDIO_ENV.get("STUDIO_COMFYUI_DIR", ""))
        return True, notes

    if with_comfyui:
        ok, msg = action_comfyui_start({}, log)
        if not ok:
            notes.append(msg.splitlines()[0] if msg else "ComfyUI 启动失败")
        else:
            notes.append("ComfyUI 正在后台启动")
    elif http_ok(f"{COMFY_URL}/system_stats", 2):
        set_comfy_state("online", "ComfyUI 已就绪")
    else:
        set_comfy_state("offline", "ComfyUI 未运行")
    return True, notes


def banner(port, notes, comfy_state_name, audit_pending):
    line = "=" * 66
    print()
    print(line)
    print("  本地二次元漫画与插画创作控制台")
    print(line)
    print(f"  控制台   : http://127.0.0.1:{port}")
    label = {"online": "在线", "missing": "未安装", "error": "启动失败"}.get(
        comfy_state_name, "未运行")
    print(f"  ComfyUI  : {COMFY_URL}  ({label})")
    print(f"  审计     : {'后台进行中，页面会自动刷新' if audit_pending else '已跳过'}")
    print(f"  日志     : reports/audit.log")
    for n in notes:
        print(f"  提示     : {n}")
    print(line)
    print("  Ctrl+C 停止控制台（不会关闭已启动的 ComfyUI）")
    print()


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred):
    if not (1 <= preferred <= 65515):
        raise RuntimeError(f"port out of range: {preferred}")
    for port in range(preferred, min(preferred + 20, 65536)):
        if port_is_free(port):
            return port
    raise RuntimeError(f"no free port in {preferred}..{preferred + 19}")


def existing_console(preferred, span=8):
    """Port of a studio console already running on this machine, if any.

    Double-clicking the shortcut while a console is up used to start a second
    one on the next free port; two consoles then poll the same ComfyUI and the
    audit reports a duplicate service. Reuse instead of stacking.
    """
    for candidate in range(preferred, preferred + span):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{candidate}/api/health", timeout=0.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - any failure means "not ours"
            continue
        if data.get("ok") and "comfyui" in data:
            return candidate
    return None


def ui_version() -> int:
    """mtime of the served page, used as a cache-buster in the browser URL.

    Browsers happily reuse an already-open tab for the same URL without
    reloading, which makes a stale UI look like a broken fix. A version query
    turns every page change into a new URL.
    """
    try:
        return int((STATIC_DIR / "index.html").stat().st_mtime)
    except OSError:
        return 0


def console_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/?v={ui_version()}"


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Local console for the anime manga studio")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-audit", action="store_true")
    ap.add_argument("--force-port", action="store_true",
                    help="bind even if another console is already serving")
    ap.add_argument("--with-comfyui", action="store_true", help="start ComfyUI in background")
    args = ap.parse_args()

    if not args.force_port:
        running = existing_console(args.port)
        if running:
            url = f"http://127.0.0.1:{running}"
            print(f"\n[ok] 控制台已在运行：{url}（复用已有实例，未重复启动）")
            if not args.no_browser:
                webbrowser.open(console_url(running))
            return 0

    log = setup_logging()
    Handler.log = log
    log.info("console starting")

    ok, notes = preflight(log, with_comfyui=args.with_comfyui)
    Handler.notes = notes

    # Bind first, audit after: the page must be usable in ~1s. A full audit is
    # 2-3s of PowerShell forensics and must never gate the UI.
    try:
        port = pick_port(args.port)
        httpd = LocalServer(("127.0.0.1", port), Handler)
    except (RuntimeError, OSError) as exc:
        log.error("bind failed: %s", exc)
        print(f"\n[FAIL] 服务启动失败：{exc}\n日志：{LOG_FILE}")
        return 1

    audit_pending = not args.no_audit
    banner(port, notes, comfy_state().get("state", "unknown"), audit_pending)

    if audit_pending:
        def startup_audit():
            try:
                run_audit(False, log)
            except AuditBusy:
                log.info("startup audit skipped: another audit is already running")
            except Exception as exc:  # noqa: BLE001
                log.exception("startup audit failed: %s", exc)
                Handler.notes.append(f"启动审计失败：{exc}（可在页面重试）")

        threading.Thread(target=startup_audit, daemon=True, name="startup-audit").start()

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(console_url(port))).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ok] 控制台已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
