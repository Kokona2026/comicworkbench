"""Offline static checks for the studio (stdlib only, no ComfyUI needed).

Guards the conventions that keep the project maintainable:
  config/ is the single source of truth and always parses
  prompt templates declare exactly the placeholders they use
  workflow templates are structurally sound
  PowerShell scripts stay ASCII-only (PS 5.1 parses BOM-less files as ANSI)
  every Python file compiles

Usage:
    python tests/lint_test.py [--quiet]
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from studio import (  # noqa: E402
    APP_DIR, CONFIG_DIR, PROJECT_ROOT, SCRIPTS_DIR, STATIC_DIR, WORKFLOW_DIR,
    load_params, load_prompts, load_manifest, load_studio_env, load_ui,
)
from workflows import validate_offline, load_template  # noqa: E402

REQUIRED_CONFIG = ["studio-env.ps1", "models.manifest.json", "params.json", "prompts.json"]
REQUIRED_ENV_KEYS = ["STUDIO_PROJECT_DIR", "STUDIO_COMFYUI_DIR", "STUDIO_COMFYUI_HOST",
                     "STUDIO_CONSOLE_PORT"]

_FAILURES: list = []
_CHECKS = 0


def _no_dup_keys(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"重复的键: {key}")
        seen.add(key)
    return dict(pairs)


def check(label: str, ok: bool, detail: str = ""):
    global _CHECKS
    _CHECKS += 1
    if not ok:
        _FAILURES.append(f"{label}: {detail}")
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    return ok


def check_config():
    for name in REQUIRED_CONFIG:
        path = CONFIG_DIR / name
        check(f"config/{name} 存在", path.is_file(), str(path))

    for path in sorted(CONFIG_DIR.rglob("*.json")):
        try:
            # A duplicate key is valid JSON and silently keeps the last value,
            # which is exactly how a stale parameter survives a refactor.
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_dup_keys)
            ok, detail = True, ""
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            ok, detail = False, str(exc)
        check(f"JSON 可解析: {path.relative_to(PROJECT_ROOT).as_posix()}", ok, detail)


def check_env():
    env = load_studio_env()
    missing = [k for k in REQUIRED_ENV_KEYS if k not in env]
    check("studio-env.ps1 可解析且键齐全", not missing, "缺 " + ", ".join(missing))
    if "STUDIO_COMFYUI_HOST" in env:
        host = env["STUDIO_COMFYUI_HOST"]
        check("ComfyUI 地址仅回环",
              host.startswith("127.0.0.1") or host.startswith("localhost"), host)


def check_prompts():
    prompts = load_prompts()
    templates = prompts.get("templates") or {}
    check("prompts.json 含模板", len(templates) >= 10, f"{len(templates)} 个")

    for key, tpl in sorted(templates.items()):
        declared = set(tpl.get("placeholders") or [])
        used = set()
        for field in ("positive", "negative"):
            used |= set(re.findall(r"\{\{(\w+)\}\}", tpl.get(field, "") or ""))
        check(f"模板 {key} 占位符一致", declared == used,
              f"声明 {sorted(declared)} / 实际 {sorted(used)}")

        params_key = tpl.get("params_key")
        if params_key:
            ok = params_key in (load_params().get("profiles") or {})
            check(f"模板 {key} 的 params_key 存在", ok, params_key)
        check(f"模板 {key} 含 stage 与 title",
              isinstance(tpl.get("stage"), int) and bool(tpl.get("title")), key)


def check_manifest():
    manifest = load_manifest()
    files = manifest.get("files") or []
    check("权重清单非空", len(files) >= 3, f"{len(files)} 个必需")
    for f in files:
        ok = all(k in f for k in ("name", "target", "min_bytes", "source_hint"))
        check(f"清单条目完整: {f.get('name')}", ok, str(sorted(f.keys())))


def check_workflows():
    names = sorted(p.name for p in WORKFLOW_DIR.glob("*.json"))
    check("工作流模板存在", len(names) >= 3, ", ".join(names))
    for name in names:
        raw = load_template(name)
        res = validate_offline(raw, name)
        check(f"工作流 {name} 结构合法", res["ok"], "; ".join(res["problems"]))
        if res["warnings"]:
            print(f"       [warn] {name}: {'; '.join(res['warnings'])}")


def check_scripts_ascii():
    files = sorted(SCRIPTS_DIR.glob("*.ps1")) + sorted(CONFIG_DIR.glob("*.ps1")) + \
        sorted(APP_DIR.glob("*.ps1"))
    check("存在 PowerShell 脚本", len(files) >= 8, f"{len(files)} 个")
    for path in files:
        data = path.read_bytes()
        bad = sum(1 for b in data if b > 127)
        check(f"ASCII-only: {path.name}", bad == 0, f"{bad} 个非 ASCII 字节")

    numbered = [p for p in SCRIPTS_DIR.glob("*.ps1") if re.match(r"^\d\d-", p.name)]
    total = list(SCRIPTS_DIR.glob("*.ps1"))
    check("一次性脚本带编号", len(numbered) >= len(total) - 3,
          f"{len(numbered)}/{len(total)}")


def check_frontend():
    """The single-page console must stay offline, routed and clickable.

    index.html is hand-written with no build step, so these checks are the
    substitute for a type system: a stale endpoint or a button without a
    handler fails here instead of in the browser.
    """
    html_path = STATIC_DIR / "index.html"
    check("app/static/index.html 存在", html_path.is_file(), str(html_path))
    if not html_path.is_file():
        return
    html = html_path.read_text(encoding="utf-8")
    server = (APP_DIR / "server.py").read_text(encoding="utf-8")

    inline = re.findall(r"\son(?:click|change|input|submit|load|error|keyup)\s*=", html)
    check("index.html 无内联事件处理器", not inline, f"{len(inline)} 处")

    external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html)
    check("index.html 无外部静态依赖（离线可用）", not external, ", ".join(external[:3]))

    endpoints = sorted(set(re.findall(r'["\'`](/api/[a-z]+)', html)))
    missing_routes = [e for e in endpoints if e not in server]
    check(f"前端 {len(endpoints)} 个 /api 端点均有后端路由", not missing_routes,
          ", ".join(missing_routes))

    acts = sorted(set(re.findall(r'data-act="([a-z-]+)"', html)))
    block = re.search(r"const HANDLERS = \{(.*?)\n  \};", html, re.S)
    handlers = set(re.findall(r'^\s+"?([a-z-]+)"?:', block.group(1), re.M)) if block else set()
    orphan_acts = [a for a in acts if a not in handlers]
    detail = "" if (block and not orphan_acts) else (", ".join(orphan_acts) or "HANDLERS 未找到")
    check(f"data-act 均有处理器（{len(acts)} 个动作）", bool(block) and not orphan_acts, detail)

    tabs = sorted(set(re.findall(r'data-tab="([a-z]+)"', html)))
    views = sorted(set(re.findall(r'id="view-([a-z]+)"', html)))
    check("页签与视图一一对应", tabs == views and len(tabs) == 5,
          f"页签 {tabs} / 视图 {views}")

    ui = load_ui()
    check("config/ui.json 声明视频阶段状态", isinstance(ui.get("video"), dict)
          and "paused" in ui["video"], str(ui.get("video")))


def check_python_compiles():
    files = sorted(APP_DIR.glob("*.py")) + sorted((PROJECT_ROOT / "tests").glob("*.py"))
    for path in files:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
            ok, detail = True, ""
        except SyntaxError as exc:
            ok, detail = False, str(exc)
        check(f"可编译: {path.name}", ok, detail)


def check_unit_tests():
    """The domain-layer unit tests must stay green; they run fully offline."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-p", "unit_test.py"],
            capture_output=True, timeout=180, cwd=str(PROJECT_ROOT))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[SKIP] 单元测试不可用: {exc}")
        return
    ok = proc.returncode == 0
    detail = "" if ok else (proc.stderr or proc.stdout).decode("utf-8", "replace")[-500:]
    check("领域层单元测试", ok, detail)


def check_ps_syntax():
    """Optional: needs PowerShell. Skipped (not failed) when unavailable."""
    files = sorted(SCRIPTS_DIR.glob("*.ps1")) + sorted(APP_DIR.glob("*.ps1"))
    script = "\n".join(
        "$t=$null;$e=$null\n"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{p.as_posix()}',"
        "[ref]$t,[ref]$e)|Out-Null\n"
        f"if($e.Count -gt 0){{ Write-Output ('FAIL {p.name}: ' + $e[0].Message) }}"
        for p in files)
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-Command", script],
                              capture_output=True, timeout=120, cwd=str(PROJECT_ROOT))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[SKIP] PowerShell 语法检查不可用: {exc}")
        return
    out = proc.stdout.decode("utf-8", errors="replace")
    fails = [line for line in out.splitlines() if line.startswith("FAIL")]
    check("PowerShell 脚本语法", not fails, "; ".join(fails))


def main():
    ap = __import__("argparse").ArgumentParser(description="Offline lint for the studio")
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    args = ap.parse_args()

    print("== 本地创作工程静态检查 ==")
    checks = [check_config, check_env, check_prompts, check_manifest,
              check_workflows, check_scripts_ascii, check_frontend,
              check_python_compiles, check_unit_tests, check_ps_syntax]
    for fn in checks:
        if args.quiet:
            buf, sys.stdout = sys.stdout, io.StringIO()
            try:
                fn()
            finally:
                sys.stdout = buf
        else:
            fn()

    print()
    if _FAILURES:
        for f in _FAILURES:
            print(f"[FAIL] {f}")
        print(f"\n{len(_FAILURES)}/{_CHECKS} 项失败")
        raise SystemExit(1)
    print(f"[PASS] {_CHECKS} 项全部通过")
    raise SystemExit(0)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    main()
