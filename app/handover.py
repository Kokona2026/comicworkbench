"""Produce a complete, self-contained handover bundle for this project.

Everything a successor (human or another agent) needs is captured in one place:
  * environment / hardware / services snapshot
  * weight and custom-node inventory with SHA256
  * live ComfyUI + workflow validation results
  * the six stage gates
  * the full audit findings and the full lint output
  * every run log, verbatim, with its source, time range and size
  * a file inventory with SHA256 so the bundle can be verified later

No content is truncated: the log section inlines every log file in full.
Stdlib only.

CLI:
    python app/handover.py [--no-hash] [--outdir PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import (  # noqa: E402
    APP_DIR, PROJECT_ROOT, CONFIG_DIR, REPORTS_DIR, LOG_DIR, DOCS_DIR,
    load_studio_env, comfyui_url, load_manifest, evaluate_stages, load_params,
    now_local,
)
from audit_engine import Audit, render_markdown  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ENV = load_studio_env()
COMFY_URL = comfyui_url(ENV)
COMFY_DIR = Path(ENV.get("STUDIO_COMFYUI_DIR", PROJECT_ROOT / "ComfyUI"))

TS_RE = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{2}:\d{2}:\d{2})")
# Console and script logs only carry a time-of-day stamp, e.g. [21:56:52].
TS_CLOCK_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def run_cmd(cmd, timeout=90):
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              cwd=str(PROJECT_ROOT))
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return proc.returncode, out, err
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def http_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #
def collect_system():
    out = {
        "collected_at": now_local().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "python_exe": sys.executable,
        "os": f"Windows {platform.release()} {platform.version()}",
        "machine": platform.machine(),
        "project_root": str(PROJECT_ROOT),
        "comfyui_dir": str(COMFY_DIR),
        "comfyui_url": COMFY_URL,
        "console_port": ENV.get("STUDIO_CONSOLE_PORT"),
        "min_comfyui": ENV.get("STUDIO_MIN_COMFYUI"),
    }
    rc, out_s, _ = run_cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,"
                                            "memory.used,utilization.gpu",
                            "--format=csv,noheader"])
    out["nvidia_smi"] = out_s.strip()
    rc, out_s, _ = run_cmd(["python", "-c", "import json,urllib.request;"
                                            "print(json.dumps(urllib.request.getproxies()))"])
    out["proxy"] = out_s.strip()

    for label, mode in (("disk", "disk"), ("ports", "ports"), ("processes", "processes"),
                        ("syntax", "syntax"), ("ascii", "ascii")):
        rc, out_s, err = run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", str(APP_DIR / "checks.ps1"), "-Mode", mode])
        out[mode] = out_s.strip() or err.strip()
    return out


def collect_comfy():
    info = {"url": COMFY_URL, "online": False}
    try:
        info["system_stats"] = http_json(f"{COMFY_URL}/system_stats", timeout=8)
        info["online"] = True
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
        return info
    try:
        obj = http_json(f"{COMFY_URL}/object_info", timeout=60)
        info["node_type_count"] = len(obj)
        info["key_nodes"] = {n: n in obj for n in (
            "UNETLoader", "CLIPLoader", "VAELoader", "KSampler", "EmptySD3LatentImage",
            "LoraLoaderModelOnly", "UnetLoaderGGUF", "WanImageToVideo",
            "ModelSamplingSD3", "VHS_VideoCombine", "SaveImage")}
    except Exception as exc:  # noqa: BLE001
        info["object_info_error"] = str(exc)
    try:
        info["queue"] = http_json(f"{COMFY_URL}/queue", timeout=8)
    except Exception:  # noqa: BLE001
        pass
    return info


def collect_weights(do_hash=True):
    manifest = load_manifest()
    rows = []
    for entry in list(manifest.get("files") or []) + list(manifest.get("optional") or []):
        target = COMFY_DIR / "models" / entry["target"] / entry["name"]
        row = {
            "name": entry["name"],
            "target": entry["target"],
            "required_for": entry.get("required_for"),
            "url": entry.get("url"),
            "expected_bytes": entry.get("expected_bytes"),
            "min_bytes": entry.get("min_bytes"),
            "path": str(target),
            "present": target.is_file(),
        }
        if target.is_file():
            size = target.stat().st_size
            row["bytes"] = size
            row["size_ok"] = size >= int(entry.get("min_bytes") or 0)
            if entry.get("expected_bytes"):
                row["size_exact"] = size == int(entry["expected_bytes"])
            if do_hash:
                row["sha256"] = sha256(target)
        rows.append(row)
    return rows


def collect_nodes():
    nodes = []
    root = COMFY_DIR / "custom_nodes"
    if not root.is_dir():
        return nodes
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name == "__pycache__":
            continue
        files = sum(1 for p in d.rglob("*") if p.is_file())
        rc, head, _ = run_cmd(["git", "-C", str(d), "rev-parse", "HEAD"], timeout=30)
        nodes.append({
            "name": d.name,
            "path": str(d),
            "files": files,
            "git_head": head.strip() if rc == 0 else None,
            "has_requirements": (d / "requirements.txt").is_file(),
        })
    return nodes


def collect_logs():
    rows = []
    for directory in (LOG_DIR, REPORTS_DIR):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if path.suffix not in (".log", ".err", ".out", ".txt") and path.suffixes[-1:] != [".log"]:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            stamps = TS_RE.findall(text)
            stat = path.stat()
            time_range = [min(stamps), max(stamps)] if stamps else None
            if not time_range:
                clocks = TS_CLOCK_RE.findall(text)
                if clocks:
                    time_range = [min(clocks) + "（时分）", max(clocks) + "（时分）"]
            rows.append({
                "path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "source": ("PowerShell / 脚本重定向" if path.parent == LOG_DIR else "Python 审计"),
                "bytes": stat.st_size,
                "lines": len(lines),
                "sha256": sha256(path) if stat.st_size else "",
                "content_time_range": time_range,
                "file_created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime)),
                "file_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "text": text,
            })
    return rows


def collect_stage_and_tests():
    models = {w["name"]: w["present"] for w in collect_weights(do_hash=False)}
    comfy = collect_comfy()
    stages = evaluate_stages(models, comfy["online"])

    tests = {}
    for name, cmd in (
        ("lint", ["python", "tests/lint_test.py"]),
        ("workflow", ["python", "tests/workflow_test.py"]),
    ):
        rc, out_s, err = run_cmd(cmd, timeout=600)
        tests[name] = {"returncode": rc, "stdout": out_s, "stderr": err}
    return stages, tests, comfy


# --------------------------------------------------------------------------- #
# document
# --------------------------------------------------------------------------- #
def build_document(bundle):
    sysinfo = bundle["system"]
    comfy = bundle["comfy"]
    stages = bundle["stages"]
    report = bundle["audit"]

    lines = []
    A = lines.append
    A("# 项目交接审计报告（Handover Bundle）")
    A("")
    A(f"- 生成时间：**{sysinfo['collected_at']}**")
    A(f"- 项目根：`{sysinfo['project_root']}`")
    A(f"- ComfyUI：`{COMFY_URL}`（目录 `{sysinfo['comfyui_dir']}`）")
    A(f"- 控制台：`http://127.0.0.1:{sysinfo['console_port']}`")
    A("- 说明：本报告与随附的 `logs/`、`reports/`、`config/` 目录一起构成完整交接包；"
      "第 11 节逐文件**全文**收录所有运行日志，未做任何截断。")
    A("")

    # ---- 0 摘要
    A("## 0. 摘要")
    A("")
    A("| 维度 | 状态 |")
    A("| --- | --- |")
    A(f"| ComfyUI | {'在线' if comfy['online'] else '离线'} |")
    A(f"| 必需权重 | {sum(1 for w in bundle['weights'] if w['required_for'] == ['stage0'] and w['present'])}/3 |")
    A(f"| 自定义节点包 | {len(bundle['nodes'])} |")
    A(f"| 工作流模板 | {sum(1 for w in bundle['workflow_report'] if w.get('ok'))}/{len(bundle['workflow_report'])} 通过 |")
    A(f"| 审计评分 | {report['score']}/100（{report['grade']}），失败 {report['summary']['fail']} 项 |")
    A(f"| 静态检查 | lint 退出码 {bundle['tests']['lint']['returncode']} |")
    A(f"| 阶段门 | 首个未通过强制阶段：{stages['blocked_at']} |")
    A(f"| 日志文件 | {len(bundle['logs'])} 个，全部全文收录 |")
    A("")

    # ---- 1 环境
    A("## 1. 环境与硬件")
    A("")
    A("| 项 | 值 |")
    A("| --- | --- |")
    A(f"| 操作系统 | {sysinfo['os']} |")
    A(f"| Python | {sysinfo['python']}（`{sysinfo['python_exe']}`） |")
    A(f"| GPU / 驱动 | {sysinfo['nvidia_smi']} |")
    A(f"| 磁盘 | {sysinfo['disk']} |")
    A(f"| 系统代理（WinINET） | {sysinfo['proxy']} |")
    A(f"| 最低 ComfyUI 版本要求 | {sysinfo['min_comfyui']} |")
    A("")
    A("> 代理说明：curl.exe 与 git 不读取 WinINET 代理，本项目已在 "
      "`scripts/05-sync-assets.ps1` 中显式注入 `--proxy` 与 `-c http.proxy`。")
    A("")

    # ---- 2 服务
    A("## 2. 服务与端口")
    A("")
    A("| 项 | 值 |")
    A("| --- | --- |")
    if comfy.get("system_stats"):
        stats = comfy["system_stats"]
        dev = (stats.get("devices") or [{}])[0]
        A(f"| ComfyUI 版本 | {stats['system'].get('comfyui_version')} |")
        A(f"| 设备 | {dev.get('name')} |")
        A(f"| 显存总量 / 空闲 | {dev.get('vram_total', 0) / 1e9:.2f} / {dev.get('vram_free', 0) / 1e9:.2f} GB |")
    else:
        A(f"| ComfyUI | 不可达：{comfy.get('error')} |")
    A(f"| 节点类型数（/object_info） | {comfy.get('node_type_count')} |")
    A(f"| 端口监听证据 | `{sysinfo['ports']}` |")
    A(f"| 进程证据 | `{sysinfo['processes']}` |")
    A("")
    A("关键节点注册情况：")
    A("")
    A("```")
    for k, v in (comfy.get("key_nodes") or {}).items():
        A(f"{k:24} {'OK' if v else 'MISSING'}")
    A("```")
    A("")

    # ---- 3 资产
    A("## 3. 权重与自定义节点资产清单")
    A("")
    A("### 3.1 权重（来源 `config/models.manifest.json`，大小为 HF API 实测）")
    A("")
    A("| 名称 | 目录 | 字节数 | 校验 | SHA256 |")
    A("| --- | --- | --- | --- | --- |")
    for w in bundle["weights"]:
        if not w["present"]:
            A(f"| `{w['name']}` | models/{w['target']} | **缺失** | — | — |")
            continue
        check = []
        if w.get("size_ok") is not None:
            check.append("min_bytes OK" if w["size_ok"] else "min_bytes FAIL")
        if "size_exact" in w:
            check.append("精确匹配" if w["size_exact"] else "大小不符")
        A(f"| `{w['name']}` | models/{w['target']} | {w.get('bytes')} | "
          f"{' / '.join(check)} | `{w.get('sha256', '')}` |")
    A("")
    A("### 3.2 自定义节点包")
    A("")
    A("| 包 | 文件数 | git HEAD |")
    A("| --- | --- | --- |")
    for n in bundle["nodes"]:
        A(f"| `{n['name']}` | {n['files']} | `{n['git_head']}` |")
    A("")

    # ---- 4 工作流
    A("## 4. 工作流模板校验")
    A("")
    A("| 模板 | 阶段 | 节点数 | 结果 | 说明 |")
    A("| --- | --- | --- | --- | --- |")
    for w in bundle["workflow_report"]:
        A(f"| `{w['name']}` | {w.get('stage')} | {w.get('nodes')} | "
          f"{'PASS' if w.get('ok') else 'FAIL'} | {'; '.join(w.get('problems', [])) or '—'} |")
    A("")
    A("```")
    A(bundle["tests"]["workflow"]["stdout"].strip())
    A("```")
    A("")

    # ---- 5 阶段
    A("## 5. 六阶段验收门")
    A("")
    A("| 阶段 | 名称 | 状态 | 检查项 |")
    A("| --- | --- | --- | --- |")
    for gate in stages["gates"]:
        detail = "; ".join(f"{c['label']}={c['detail']}" for c in gate["checks"])
        A(f"| {gate['stage']} | {gate['name']} | {gate['status']}"
          f"{'（阻塞）' if gate['blocked'] else ''} | {detail} |")
    A("")
    if stages["assets"].get("baseline"):
        A(f"- 出图基准：`reports/baseline.json` → "
          f"{stages['assets']['baseline'].get('elapsed_s')}s @ "
          f"{stages['assets']['baseline'].get('width')}x"
          f"{stages['assets']['baseline'].get('height')}，"
          f"峰值显存 {stages['assets']['baseline'].get('vram_peak_mb')} MB")
    A(f"- 资产计数：`{stages['counts']}`")
    A("")

    # ---- 6 审计
    A("## 6. 部署审计（38 项）")
    A("")
    A(f"- 生成时间：{report['generated_at']}")
    A(f"- 评分：**{report['score']}/100（{report['grade']}）**")
    A(f"- 通过 {report['summary']['pass']} / 警告 {report['summary']['warn']} / "
      f"失败 {report['summary']['fail']}，耗时 {report['duration_ms']} ms")
    A("")
    A(render_markdown(report).split("## 关键指标")[0].split("# 本地创作环境审计报告")[-1])
    A("")
    A("### 6.1 完整 findings")
    A("")
    A("| ID | 类别 | 检查项 | 严重度 | 状态 | 证据 | 建议 |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for f in report["findings"]:
        A(f"| {f['id']} | {f['category']} | {f['title']} | {f['severity']} | "
          f"{f['status']} | {f['evidence']} | {f['recommendation']} |")
    A("")
    A("### 6.2 指标")
    A("")
    A("| 指标 | 值 | 说明 |")
    A("| --- | --- | --- |")
    for m in report["metrics"]:
        A(f"| {m['label']} | {m['value']} | {m['hint']} |")
    A("")

    # ---- 7 lint
    A("## 7. 静态检查（tests/lint_test.py）")
    A("")
    A(f"退出码：**{bundle['tests']['lint']['returncode']}**")
    A("")
    A("```")
    A(bundle["tests"]["lint"]["stdout"].strip())
    A("```")
    if bundle["tests"]["lint"]["stderr"].strip():
        A("")
        A("```")
        A(bundle["tests"]["lint"]["stderr"].strip())
        A("```")
    A("")

    # ---- 8 已知问题
    A("## 8. 已修复问题记录")
    A("")
    for i, item in enumerate(bundle["fixes"], 1):
        A(f"{i}. {item}")
    A("")

    # ---- 9 风险
    A("## 9. 剩余风险与建议")
    A("")
    for item in bundle["risks"]:
        A(f"- {item}")
    A("")

    # ---- 10 交接步骤
    A("## 10. 交接后建议操作")
    A("")
    A("```powershell")
    A("cd " + str(PROJECT_ROOT))
    A("python tests\\lint_test.py          # 静态检查")
    A("python tests\\workflow_test.py      # 对照本机 /object_info 校验工作流")
    A("python app\\audit_engine.py         # 重新审计")
    A(".\\start.cmd                        # 打开控制台")
    A("```")
    A("")
    A("阶段 1 起需要继续产出资产，流程见 `docs/02-workflow.md`。")
    A("")

    # ---- 11 日志全文
    A("## 11. 运行日志（全文，未截断）")
    A("")
    A("| 文件 | 来源 | 行数 | 字节 | 内容时间范围 | 文件修改时间 |")
    A("| --- | --- | --- | --- | --- | --- |")
    for row in bundle["logs"]:
        tr = row["content_time_range"]
        rng = f"{tr[0]} ~ {tr[1]}" if tr else "（无时间戳）"
        A(f"| `{row['path']}` | {row['source']} | {row['lines']} | {row['bytes']} | "
          f"{rng} | {row['file_modified']} |")
    A("")
    for row in bundle["logs"]:
        A(f"### 11.x `{row['path']}`")
        A("")
        A(f"- 来源：{row['source']}")
        A(f"- 行数 / 字节：{row['lines']} / {row['bytes']}")
        A(f"- 内容时间范围：{row['content_time_range'] or '（无时间戳）'}")
        A(f"- 文件创建 / 修改：{row['file_created']} / {row['file_modified']}")
        A(f"- SHA256：`{row['sha256']}`")
        A("")
        A("```text")
        A(row["text"].rstrip() if row["text"].strip() else "（空文件）")
        A("```")
        A("")

    # ---- 12 清单
    A("## 12. 交接包文件清单")
    A("")
    A("| 文件 | 字节 | SHA256 |")
    A("| --- | --- | --- |")
    for item in bundle["inventory"]:
        A(f"| `{item['path']}` | {item['bytes']} | `{item['sha256']}` |")
    A("")
    return "\n".join(lines)


FIXES = [
    "启动链路：`10-start-comfyui.ps1` 秒退却被后台判为成功，导致 UI 卡在 starting 240s。改为启动前校验 launcher、后台进程早退即报错、状态每次轮询重推导。",
    "启动耗时：完整审计原本同步阻塞首屏（冷启动 17s），现改为先绑定端口、审计后台执行，首屏 1.78s。",
    "PyTorch 源：download.pytorch.org 返回 403，改为多镜像回退，SJTU 实测 24 MB/s。",
    "Blackwell 适配：安装 torch 2.11.0+cu128，CUDA capability 12.0，避免 no kernel image is available。",
    "依赖修正：固定 setuptools<82；torchsde 曾被误当 torch 过滤；comfyui-workflow-templates 需要 pypi 兜底索引。",
    "代理：curl.exe 与 git 不读 WinINET 代理，脚本已显式注入 --proxy 与 -c http.proxy。",
    "下载正确性：初版下载器预分配目标文件，会产生全零但字节数正确的假权重；已改为分段独立文件、全部到齐后才装配，并删除假文件重下。",
    "工作流修正：VHS_VideoCombine 不接受 crf/pix_fmt/save_metadata，模板已按本机 /object_info 修正；lint 新增重复 JSON 键检测。",
    "配置校准：models.manifest.json 的大小改为 HF API 实测字节数，新增 url 与 custom_nodes，成为下载唯一来源。",
    "审计误报修正：Windows 上 venv 的 python.exe 是启动器，会拉起 base 解释器子进程；SVC-04 曾把 1 个 ComfyUI 实例误判为 2 个，现按父子链去重。",
]

RISKS = [
    "Wan 2.2 I2V A14B Q8 权重 15.4 GB，16 GB 显存装不下 KV Cache + VAE 解码缓冲；建议 Q5_K_M/Q6_K，或 --reserve-vram + Tiled VAE。",
    "Anima 为 CircleStone Labs 非商业许可，商用前必须核对条款（审计 SEC-05）。",
    "阶段 1 候选图 0/40-60，阶段 2-5 资产为 0：这是当前唯一阻塞点，不是故障。",
    "可选权重 4 个未下载（LLLite 66 MB、Wan Q8 15.4 GB、Turbo/Aesthetic 各 4.18 GB），合计约 24 GB。",
    "NET-02 / SVC-04 为控制台端口与自身进程的提示，无实际影响。",
]


def main():
    ap = argparse.ArgumentParser(description="Build the handover bundle")
    ap.add_argument("--no-hash", action="store_true")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.outdir) if args.outdir else REPORTS_DIR / f"handover-{stamp}"
    for sub in ("logs", "reports", "config"):
        (outdir / sub).mkdir(parents=True, exist_ok=True)

    log(f"== handover -> {outdir} ==")
    log("[..] system snapshot")
    system = collect_system()
    log("[..] comfyui + workflow validation")
    stages, tests, comfy = collect_stage_and_tests()
    log("[..] weights + nodes (sha256)")
    weights = collect_weights(do_hash=not args.no_hash)
    nodes = collect_nodes()

    log("[..] fresh audit")
    audit = Audit(deep=False, logger=lambda level, msg: None)
    audit.write = True
    report = audit.run()

    # Collected after the audit so reports/audit.log already contains this run.
    log("[..] logs")
    logs = collect_logs()

    from workflows import validate_all
    object_info = {}
    try:
        object_info = http_json(f"{COMFY_URL}/object_info", timeout=60)
    except Exception:  # noqa: BLE001
        pass

    log("[..] copy verbatim files")
    copied = []
    for src_dir, dst_sub in ((LOG_DIR, "logs"), (REPORTS_DIR, "reports"), (CONFIG_DIR, "config")):
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            if src_dir == REPORTS_DIR and any(part.startswith("handover-")
                                              for part in path.parts):
                continue  # never nest a previous bundle inside the next one
            rel = path.relative_to(src_dir)
            dest = outdir / dst_sub / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied.append(dest)

    inventory = [{"path": str(p.relative_to(outdir)).replace("\\", "/"),
                  "bytes": p.stat().st_size, "sha256": sha256(p)} for p in copied]

    bundle = {
        "system": system,
        "comfy": comfy,
        "stages": stages,
        "weights": weights,
        "nodes": nodes,
        "logs": logs,
        "tests": tests,
        "audit": report,
        "workflow_report": validate_all(object_info),
        "inventory": inventory,
        "fixes": FIXES,
        "risks": RISKS,
    }

    doc = build_document(bundle)
    doc_path = outdir / "HANDOVER.md"
    doc_path.write_text(doc, encoding="utf-8")

    # Machine-readable twin: logs carry text here too, so nothing is lost for a
    # successor that prefers JSON over Markdown.
    summary = {k: v for k, v in bundle.items() if k != "inventory"}
    summary["bundle_dir"] = str(outdir)
    summary["document"] = str(doc_path)
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"[ok] HANDOVER.md  {len(doc)} chars")
    log(f"[ok] summary.json  {outdir}")
    log(f"[ok] verbatim copies: {len(copied)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
