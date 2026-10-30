"""Stage 0 acceptance test: one 1024x1024 render through ComfyUI.

This is the gate that authorises stage 1. It renders the Anima template at the
default resolution, measures wall-clock time and VRAM, and writes
reports/baseline.json so later stages have a comparison point.

Usage:
    python tests/smoke_test.py [--width 1024] [--height 1024] [--steps 40] [--seed N]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from studio import (  # noqa: E402
    PROJECT_ROOT, load_studio_env, comfyui_url, save_baseline, load_params,
)
from workflows import render_named, validate_online  # noqa: E402


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gpu_vram_mb():
    """Best-effort VRAM reading; nvidia-smi is not fatal when absent."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=8).stdout.decode().strip()
        return int(float(out.splitlines()[0]))
    except Exception:  # noqa: BLE001
        return None


def wait_for_prompt(base, prompt_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hist = _get(f"{base}/history/{prompt_id}", timeout=15)
        except urllib.error.URLError:
            time.sleep(2)
            continue
        if prompt_id in hist:
            return hist[prompt_id]
        time.sleep(2)
    return None


def main():
    env = load_studio_env()
    default_base = comfyui_url(env)
    profile = (load_params().get("profiles") or {}).get("anima_base") or {}

    ap = argparse.ArgumentParser(description="Stage 0 smoke test (1024x1024 render)")
    ap.add_argument("--base", default=default_base)
    ap.add_argument("--workflow", default="anima-txt2img.json")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=profile.get("steps", 40))
    ap.add_argument("--cfg", type=float, default=profile.get("cfg", 4.5))
    ap.add_argument("--seed", type=int, default=123456789)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    print(f"== Anima smoke test -> {base} ==")
    print(f"   {args.width}x{args.height}  steps={args.steps} cfg={args.cfg} seed={args.seed}")

    try:
        stats = _get(f"{base}/system_stats", timeout=10)
    except urllib.error.URLError as exc:
        raise SystemExit(f"[FAIL] 无法连接 {base}: {exc}  "
                         f"请先运行 scripts\\10-start-comfyui.ps1")
    print(f"[PASS] ComfyUI 在线: {(stats.get('system') or {}).get('comfyui_version', 'ok')}")

    info = _get(f"{base}/object_info", timeout=30)
    res = validate_online(__import__("json").loads(
        (PROJECT_ROOT / "config" / "workflows" / args.workflow).read_text(encoding="utf-8")),
        info, args.workflow)
    if not res["ok"]:
        for p in res["problems"]:
            print(f"[FAIL] {p}")
        raise SystemExit("[FAIL] 工作流与本机节点不匹配，先运行 scripts\\20-verify-workflow.ps1")
    print(f"[PASS] 工作流节点校验通过（{res.get('nodes')} 个节点）")

    payload = render_named(args.workflow, {
        "width": args.width, "height": args.height, "steps": args.steps,
        "cfg": args.cfg, "seed": args.seed,
        "filename_prefix": "smoke/stage0",
    })

    client_id = uuid.uuid4().hex
    vram_before = gpu_vram_mb()
    t0 = time.time()
    try:
        queued = _post(f"{base}/prompt", {"prompt": payload, "client_id": client_id})
    except urllib.error.URLError as exc:
        raise SystemExit(f"[FAIL] /prompt 提交失败: {exc}")
    prompt_id = queued.get("prompt_id")
    print(f"[PASS] 已排队 prompt_id={prompt_id}")

    record = wait_for_prompt(base, prompt_id, args.timeout)
    elapsed = round(time.time() - t0, 1)
    vram_peak = gpu_vram_mb()

    if record is None:
        raise SystemExit(f"[FAIL] {args.timeout}s 内未取回结果，检查 ComfyUI 日志")

    status = record.get("status") or {}
    if status.get("status_str") == "error" or status.get("completed") is False:
        msgs = [m for m in status.get("messages", []) if isinstance(m, list) and len(m) > 1]
        raise SystemExit("[FAIL] 生成失败: " + "; ".join(str(m[1]) for m in msgs[:3]))

    images = []
    for node_out in (record.get("outputs") or {}).values():
        images.extend(node_out.get("images") or [])
    if not images:
        raise SystemExit("[FAIL] 历史记录里没有图片输出")

    img = images[0]
    print(f"[PASS] 出图 {img.get('filename')} 耗时 {elapsed}s")
    if vram_peak is not None:
        print(f"[info] VRAM {vram_before} -> {vram_peak} MB")

    baseline = save_baseline({
        "workflow": args.workflow,
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
        "cfg": args.cfg,
        "seed": args.seed,
        "elapsed_s": elapsed,
        "prompt_id": prompt_id,
        "image": f"{img.get('subfolder', '')}/{img.get('filename')}".strip("/"),
        "vram_before_mb": vram_before,
        "vram_peak_mb": vram_peak,
        "comfyui": base,
        "comfyui_version": (stats.get("system") or {}).get("comfyui_version"),
    })
    print(f"[PASS] 基准已写入 reports/baseline.json（{elapsed}s）")
    print(f"       阶段 0 验收：模板图正常生成、无报错、已记录基准耗时")
    raise SystemExit(0)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    main()
