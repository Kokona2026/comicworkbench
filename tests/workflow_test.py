"""Validate ComfyUI workflow templates.

Offline checks always run. When ComfyUI answers on /object_info, node class
names and input names are checked against the live server, which is the only
authoritative source: node names differ between ComfyUI versions and custom
node packs.

Usage:
    python tests/workflow_test.py [--base http://127.0.0.1:8188]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from studio import load_studio_env, comfyui_url, WORKFLOW_DIR  # noqa: E402
from workflows import validate_all, validate_offline, load_template  # noqa: E402


def fetch_object_info(base: str, timeout: float = 20.0):
    try:
        with urllib.request.urlopen(f"{base}/object_info", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main():
    default_base = comfyui_url(load_studio_env())
    ap = argparse.ArgumentParser(description="Validate workflow templates")
    ap.add_argument("--base", default=default_base)
    ap.add_argument("--offline", action="store_true", help="skip the live /object_info check")
    args = ap.parse_args()

    print(f"== workflow validation -> {args.base} ==")

    info = None if args.offline else fetch_object_info(args.base)
    if info:
        print(f"[PASS] /object_info 返回 {len(info)} 个节点类型")
    else:
        print(f"[SKIP] ComfyUI 离线或 --offline，仅做离线校验")

    problems = 0
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        raw = load_template(path.name)
        res = validate_offline(raw, path.name)
        if info:
            from workflows import validate_online
            res = validate_online(raw, info, path.name)

        title = res.get("title", path.name)
        stage = res.get("stage")
        packs = res.get("requires_packs") or []
        status = "PASS" if res["ok"] else "FAIL"
        print(f"[{status}] {path.name}  stage={stage}  nodes={res.get('nodes')}  {title}")
        if packs:
            print(f"        依赖节点包: {', '.join(packs)}")
        for p in res.get("problems", []):
            print(f"        [problem] {p}")
            problems += 1
        for w in res.get("warnings", []):
            print(f"        [warn] {w}")

    print()
    if problems:
        print(f"[FAIL] {problems} 个问题")
        raise SystemExit(1)
    print("[PASS] 所有工作流模板通过校验")
    raise SystemExit(0)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    main()
