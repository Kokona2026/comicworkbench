"""Local deployment audit engine for the anime manga studio (stdlib only).

Runs a fixed set of checks over config/, scripts/, project assets and the
ComfyUI service, scores them, writes reports/audit-*.json plus docs/05-audit.md,
and returns the dict rendered by the console.

Every check degrades instead of crashing: when ComfyUI is absent the related
checks report FAIL with a remediation rather than aborting the audit.

CLI:
    python app/audit_engine.py [--deep] [--no-write] [--json]
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import (  # noqa: E402
    CONFIG_DIR, PROJECT_ROOT, APP_DIR, SCRIPTS_DIR, REPORTS_DIR, DOCS_DIR,
    load_studio_env, comfyui_url, console_port, load_manifest, load_params,
    load_prompts, evaluate_stages, now_local,
)
from workflows import validate_all  # noqa: E402

# Penalty applied to the 100-point score for a failed / warned check.
PENALTY = {"critical": 30, "high": 15, "medium": 7, "low": 2, "info": 0}

REMEDIATIONS = [
    "config/ 作为唯一配置来源：路径、端口、最低 ComfyUI 版本集中到 config/studio-env.ps1",
    "scripts/ 全部 ASCII-only，避免 Windows PowerShell 5.1 按 ANSI 解析导致乱码",
    "停止类脚本使用 return 而非 exit，避免被编排脚本调用时提前终止调用方",
    "工作流模板带 __meta__（标题/阶段/依赖节点包/默认值），提交 /prompt 前剥离",
    "工作流节点名以本机 /object_info 为准，脚本 20-verify-workflow.ps1 负责校验",
    "六阶段验收门由 app/studio.py 统一评估，验收不过不允许进入下一阶段",
    "train 集标签以触发词开头，固定特征标签删除，交给 LoRA 从像素学习",
]

PS_MODES = ["env", "ports", "syntax", "ascii", "disk", "processes", "gpu"]


class Audit:
    def __init__(self, deep: bool = False, logger=None):
        self.deep = deep
        self.log = logger or (lambda level, msg: None)
        self.findings: list = []
        self.metrics: list = []
        self.write = True
        self.started = time.time()
        self._ps_cache: dict = {}
        self.env: dict = {}
        self.studio_env: dict = {}
        self.comfy_url = "http://127.0.0.1:8188"
        self.object_info: dict = {}

    # ---------- helpers ----------
    def add(self, cid, category, title, severity, status, evidence, recommendation):
        self.findings.append({
            "id": cid, "category": category, "title": title, "severity": severity,
            "status": status, "evidence": evidence, "recommendation": recommendation,
        })
        self.log("INFO" if status == "pass" else ("WARN" if status == "warn" else "ERROR"),
                 f"{cid} {title}: {status} ({evidence})")

    def metric(self, key, label, value, hint="", status="ok"):
        self.metrics.append({"key": key, "label": label, "value": str(value),
                             "hint": hint, "status": status})

    def ps(self, mode, timeout=40):
        if mode in self._ps_cache:
            return self._ps_cache[mode]
        return self._ps_raw(mode, timeout)

    def _ps_raw(self, mode, timeout=40):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(APP_DIR / "checks.ps1"), "-Mode", mode]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(PROJECT_ROOT))
            raw = proc.stdout.decode("utf-8", errors="replace").strip()
            if not raw:
                return None
            start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
            if start > 0:
                raw = raw[start:]
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            self.log("WARN", f"checks.ps1 -Mode {mode} failed: {exc}")
            return None

    def http_json(self, url, payload=None, timeout=10):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return None, {"error": str(exc)}

    def comfy_online(self):
        code, body = self.http_json(f"{self.comfy_url}/system_stats", timeout=4)
        return code == 200, body if code == 200 else {}

    # ---------- checks ----------
    def check_environment(self):
        self.env = self.ps("env") or {}
        self.studio_env = load_studio_env()
        self.comfy_url = comfyui_url(self.studio_env)

        py = platform.python_version()
        ok = tuple(int(x) for x in py.split(".")[:2]) >= (3, 9)
        self.add("ENV-01", "环境", "Python 版本满足 3.9+", "critical",
                 "pass" if ok else "fail", f"Python {py}",
                 "安装 Python 3.9 以上" if not ok else "无需处理")
        self.metric("python", "Python", py)

        required = ["studio-env.ps1", "models.manifest.json", "params.json", "prompts.json"]
        missing = [f for f in required if not (CONFIG_DIR / f).exists()]
        self.add("ENV-02", "环境", "config/ 唯一配置来源完整", "high",
                 "pass" if not missing else "fail",
                 "全部就位" if not missing else "缺少 " + ", ".join(missing),
                 "补齐缺失的配置文件")

        port = console_port(self.studio_env)
        self.add("ENV-03", "环境", "控制台端口配置合法", "low",
                 "pass" if 1 <= port <= 65535 else "fail", f"STUDIO_CONSOLE_PORT={port}",
                 "取值范围 1-65535" if not (1 <= port <= 65535) else "无需处理")

        comfy_dir = self.studio_env.get("STUDIO_COMFYUI_DIR", "")
        launcher = (self.env or {}).get("COMFYUI_LAUNCHER") or ""
        self.add("ENV-04", "环境", "ComfyUI 安装已定位", "critical",
                 "pass" if launcher else "fail",
                 launcher or f"未在 {comfy_dir} 找到 main.py",
                 "安装 ComfyUI 便携版后设置用户级 STUDIO_COMFYUI_DIR 指向其根目录")
        self.metric("comfyui_dir", "ComfyUI 目录", comfy_dir or "(unset)",
                    "用户级 STUDIO_COMFYUI_DIR 可覆盖默认值")

        dirs = ["00_bible", "01_dataset", "02_lora", "03_illust", "04_manga", "05_anim"]
        absent = [d for d in dirs if not (PROJECT_ROOT / "project" / d).is_dir()]
        self.add("ENV-05", "环境", "创作目录 00-05 就位", "medium",
                 "pass" if not absent else "fail",
                 "全部就位" if not absent else "缺少 " + ", ".join(absent),
                 "运行 scripts\\00-init-project.ps1")

    def check_ports(self):
        rows = self.ps("ports") or []
        comfy_port = int(self.comfy_url.rsplit(":", 1)[-1])
        console = console_port(self.studio_env)

        comfy = [r for r in rows if r.get("port") == comfy_port]
        if comfy:
            self.add("NET-01", "网络", f"ComfyUI 端口 {comfy_port} 已监听", "high", "pass",
                     f"pid={comfy[0].get('pid')} ({comfy[0].get('process')})", "无需处理")
        else:
            self.add("NET-01", "网络", f"ComfyUI 端口 {comfy_port} 未监听", "high", "fail",
                     "无监听", "运行 scripts\\10-start-comfyui.ps1")

        mine = [r for r in rows if r.get("port") == console]
        if mine:
            # 端口持有者决定它是冲突还是预期状态：本项目的 server.py 在跑时，
            # 端口被占是正常的（审计经常在控制台已启动的情况下执行）。
            own = [r for r in mine if "server.py" in str(r.get("cmd") or "")]
            if own:
                self.add("NET-02", "网络", f"控制台端口 {console} 由本项目控制台持有",
                         "info", "pass",
                         f"pid={own[0].get('pid')} ({own[0].get('process')}) 正在运行",
                         "无需处理；控制台重启时会自动释放该端口")
            else:
                self.add("NET-02", "网络", f"控制台端口 {console} 被占用", "medium", "warn",
                         f"pid={mine[0].get('pid')} ({mine[0].get('process')})",
                         "占用者不是本项目控制台；结束它或让控制台顺延到下一个空闲端口")
        else:
            self.add("NET-02", "网络", f"控制台端口 {console} 空闲", "info", "pass",
                     "可用", "无需处理")

        exposed = [r for r in rows if not r.get("loopback")]
        self.add("NET-03", "网络", "服务仅绑定回环地址", "critical",
                 "pass" if not exposed else "fail",
                 "全部 127.0.0.1" if not exposed else
                 ", ".join(f"{r.get('address')}:{r.get('port')}" for r in exposed),
                 "ComfyUI 与控制台只允许 127.0.0.1 监听；不要加 --listen 暴露到局域网")

    def check_services(self):
        online, stats = self.comfy_online()
        self.add("SVC-01", "服务", "ComfyUI /system_stats 可访问", "critical",
                 "pass" if online else "fail",
                 (stats.get("system") or {}).get("comfyui_version", "在线") if online
                 else "不可达",
                 "启动 ComfyUI 后重试" if not online else "无需处理")

        version = None
        comfy_dir = self.studio_env.get("STUDIO_COMFYUI_DIR", "")
        if comfy_dir:
            vf = Path(comfy_dir) / "comfyui_version.py"
            if vf.is_file():
                m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", vf.read_text(encoding="utf-8", errors="replace"))
                version = m.group(1) if m else None
        if version is None and online:
            version = (stats.get("system") or {}).get("comfyui_version")
        required_min = self.studio_env.get("STUDIO_MIN_COMFYUI", "0.3.31")

        def vtuple(v):
            try:
                return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
            except (TypeError, ValueError):
                return ()

        if version:
            ok = vtuple(version) >= vtuple(required_min)
            self.add("SVC-02", "服务", f"ComfyUI 版本 >= {required_min}", "high",
                     "pass" if ok else "warn", version,
                     "社区版 Anima 对 ComfyUI 版本有要求，请更新到最新" if not ok else "无需处理")
            self.metric("comfyui_version", "ComfyUI 版本", version)
        else:
            self.add("SVC-02", "服务", "ComfyUI 版本未知", "medium", "warn",
                     "未找到 comfyui_version.py 且服务离线",
                     f"启动服务或确认安装版本 >= {required_min}")

        if online:
            code, info = self.http_json(f"{self.comfy_url}/object_info", timeout=20)
            if code == 200 and isinstance(info, dict):
                self.object_info = info
                self.add("SVC-03", "服务", "已获取节点清单 /object_info", "medium", "pass",
                         f"{len(info)} 个节点类型", "可用于校验工作流节点名")
                self.metric("nodes", "可用节点数", len(info))
            else:
                self.add("SVC-03", "服务", "无法读取 /object_info", "medium", "warn",
                         str((info or {}).get("error", "unknown")), "检查 ComfyUI 日志")
        else:
            self.add("SVC-03", "服务", "离线，跳过节点清单校验", "info", "warn",
                     "ComfyUI 未运行", "启动后重新审计")

        procs = self.ps("processes") or []
        # venv's python.exe on Windows is a launcher that spawns the base
        # interpreter as a CHILD, so one ComfyUI shows up as two processes.
        # Count only process trees whose parent is outside the matched set.
        pids = {p.get("pid") for p in procs}
        roots = [p for p in procs if p.get("parent") not in pids]
        self.add("SVC-04", "服务", "无重复 ComfyUI 实例", "low",
                 "pass" if len(roots) <= 1 else "warn",
                 f"{len(roots)} 个独立实例（{len(procs)} 个进程，含启动器与子进程）",
                 "重复实例会争抢显存，停止多余进程" if len(roots) > 1 else "无需处理")

    @staticmethod
    def _paused_stages() -> set[int]:
        """Stages marked paused in config/ui.json (currently: video / stage 5)."""
        try:
            ui = json.loads((CONFIG_DIR / "ui.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        paused: set[int] = set()
        video = ui.get("video") or {}
        if video.get("paused") and video.get("stage"):
            paused.add(int(video["stage"]))
        for stage in ui.get("paused_stages") or []:
            try:
                paused.add(int(stage))
            except (TypeError, ValueError):
                continue
        return paused

    def check_models(self):
        manifest = load_manifest()
        comfy_dir = self.studio_env.get("STUDIO_COMFYUI_DIR", "")
        found = {}
        for i, f in enumerate(manifest.get("files") or [], 1):
            target = Path(comfy_dir) / "models" / f["target"] / f["name"] if comfy_dir else None
            size = target.stat().st_size if target and target.is_file() else 0
            found[f["name"]] = size > 0
            ok = size >= int(f.get("min_bytes", 1))
            self.add(f"MDL-{i:02d}", "模型", f"{f['name']} 在位", "critical",
                     "pass" if ok else "fail",
                     f"{size / 1e9:.2f} GB" if size else "未找到",
                     "无需处理" if ok else
                     f"放入 {f['target']}/；来源：{f.get('source_hint', '见模型卡')}")
        self._models_present = found

        present = sum(1 for v in found.values() if v)
        self.metric("weights", "必需权重", f"{present}/3", "anima + text encoder + vae")

        missing_opt = []
        paused_opt = []
        paused_stages = self._paused_stages()

        def only_paused(f: dict) -> bool:
            """True when every stage that needs this weight is parked."""
            needs = [str(x) for x in (f.get("required_for") or [])]
            if f.get("stage"):
                needs.append(f"stage{f['stage']}")
            stages = set()
            for item in needs:
                digits = "".join(ch for ch in item if ch.isdigit())
                if digits:
                    stages.add(int(digits))
            return bool(stages) and stages <= paused_stages

        for f in manifest.get("optional") or []:
            if only_paused(f):
                # 排期暂缓的阶段不参与阻塞判断：它的可选权重缺失时不算问题
                paused_opt.append(f["name"])
                continue
            if not comfy_dir:
                missing_opt.append(f["name"])
                continue
            target = Path(comfy_dir) / "models" / f["target"] / f["name"]
            if not target.is_file():
                missing_opt.append(f["name"])
        detail = "全部就位" if not missing_opt else "缺少 " + ", ".join(missing_opt)
        if paused_opt:
            detail += f"；暂缓阶段跳过 {len(paused_opt)} 个（{', '.join(paused_opt)}）"
        self.add("MDL-04", "模型", "可选权重（阶段 3-5）", "info",
                 "pass" if not missing_opt else "warn", detail,
                 "按需下载；暂缓阶段的权重不计入缺失")

    def check_workflows(self):
        results = validate_all(self.object_info if self.object_info else None)
        self.add("WKF-01", "工作流", "模板 JSON 结构合法", "high",
                 "pass" if all(r.get("ok") for r in results) else "fail",
                 f"{sum(1 for r in results if r.get('ok'))}/{len(results)} 个模板通过",
                 "运行 scripts\\20-verify-workflow.ps1 查看明细")
        for r in results:
            self.metric("wf_" + str(r.get("name")), r.get("title", r.get("name")),
                        "ok" if r.get("ok") else "FAIL",
                        "; ".join(r.get("problems", []))[:160] or "无问题",
                        "ok" if r.get("ok") else "warn")

        if self.object_info:
            unknown = sorted({n for r in results for n in r.get("missing_nodes", [])})
            self.add("WKF-02", "工作流", "节点类型已在本机注册", "high",
                     "pass" if not unknown else "fail",
                     "全部注册" if not unknown else ", ".join(unknown),
                     "安装对应节点包或调整节点名")
            bad_inputs = sorted({n for r in results for n in r.get("missing_inputs", [])})
            self.add("WKF-03", "工作流", "节点输入名与 /object_info 一致", "medium",
                     "pass" if not bad_inputs else "warn",
                     "一致" if not bad_inputs else ", ".join(bad_inputs),
                     "按本机节点定义修正输入名")
        else:
            self.add("WKF-02", "工作流", "离线，跳过节点注册校验", "info", "warn",
                     "ComfyUI 未运行", "启动后重新审计")

        params = load_params()
        prompts = load_prompts()
        tpl = prompts.get("templates") or {}
        bad = []
        for key, t in tpl.items():
            declared = set(t.get("placeholders") or [])
            used = set()
            for field in ("positive", "negative"):
                used |= set(re.findall(r"\{\{(\w+)\}\}", t.get(field, "") or ""))
            if used != declared:
                bad.append(f"{key}: 声明 {sorted(declared)} 实际 {sorted(used)}")
        self.add("WKF-04", "工作流", "提示词模板占位符声明一致", "medium",
                 "pass" if not bad else "fail", "全部一致" if not bad else "; ".join(bad),
                 "同步 config/prompts.json 的 placeholders 与正文")
        self.metric("prompt_templates", "提示词模板", len(tpl),
                    "config/prompts.json 是唯一来源")
        if params.get("profiles"):
            self.metric("param_profiles", "参数档位", len(params["profiles"]),
                        "config/params.json")

    def check_assets(self):
        stages = evaluate_stages(getattr(self, "_models_present", {}), bool(self.object_info))
        for gate in stages["gates"]:
            done = sum(1 for c in gate["checks"] if c["ok"])
            if gate.get("status") == "paused":
                # 排期暂缓的阶段：定义保留，不计失败也不计警告
                self.add(f"AST-{gate['stage']:02d}", "资产",
                         f"阶段 {gate['stage']} {gate['name']}（暂缓）", "info", "pass",
                         "按排期暂缓（config/ui.json），不参与阻塞判断",
                         "需要恢复时把 ui.json 的 video.paused 改为 false")
                continue
            status = "pass" if gate["status"] == "pass" else (
                "fail" if gate["status"] == "fail" else "warn")
            self.add(f"AST-{gate['stage']:02d}", "资产",
                     f"阶段 {gate['stage']} {gate['name']}（{done}/{len(gate['checks'])}）",
                     "high" if gate["stage"] <= 2 else "medium", status,
                     "; ".join(f"{c['label']}={'OK' if c['ok'] else c['detail']}"
                               for c in gate["checks"]),
                     "无需处理" if status == "pass" else f"参见 docs/02-workflow.md 阶段 {gate['stage']}")
        c = stages["counts"]
        self.metric("assets", "候选/训练/checkpoint",
                    f"{c['candidates']}/{c['train']}/{c['checkpoints']}",
                    "40-60 候选 -> 20-40 训练 -> 选定 checkpoint")

    def check_code(self):
        syntax = self.ps("syntax") or []
        bad = [r for r in syntax if not r.get("ok")]
        self.add("COD-01", "代码质量", "PowerShell 脚本语法通过 PSParser", "high",
                 "pass" if not bad else "fail",
                 f"{len(syntax)} 个文件" if not bad else
                 ", ".join(f"{r.get('file')}: {r.get('message')}" for r in bad),
                 "修正语法错误")

        non_ascii = self.ps("ascii") or []
        self.add("COD-02", "代码质量", "PowerShell 脚本 ASCII-only", "medium",
                 "pass" if not non_ascii else "fail",
                 "全部 ASCII" if not non_ascii else
                 ", ".join(f"{r.get('file')}({r.get('non_ascii')})" for r in non_ascii),
                 "PS 5.1 无 BOM 会按 ANSI 解析，脚本必须保持 ASCII")

        py_files = sorted(APP_DIR.glob("*.py")) + sorted((PROJECT_ROOT / "tests").glob("*.py"))
        broken = []
        for p in py_files:
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as exc:
                broken.append(f"{p.name}: {exc}")
        self.add("COD-03", "代码质量", "Python 文件可编译", "high",
                 "pass" if not broken else "fail",
                 f"{len(py_files)} 个文件" if not broken else "; ".join(broken),
                 "修正语法错误")

        cfg_bad = []
        for p in sorted(CONFIG_DIR.rglob("*.json")):
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                cfg_bad.append(f"{p.name}: {exc}")
        self.add("COD-04", "代码质量", "config/ 下 JSON 均可解析", "high",
                 "pass" if not cfg_bad else "fail",
                 f"{len(list(CONFIG_DIR.rglob('*.json')))} 个文件" if not cfg_bad
                 else "; ".join(cfg_bad), "修正 JSON")

        scripts = sorted(SCRIPTS_DIR.glob("*.ps1"))
        numbered = [p for p in scripts if re.match(r"^\d\d-", p.name)]
        self.add("COD-05", "代码质量", "运维脚本按 NN-动作.ps1 编号", "low",
                 "pass" if len(numbered) >= len(scripts) - 3 else "warn",
                 f"{len(numbered)}/{len(scripts)} 个带编号",
                 "一次性脚本用编号保证执行顺序，日常入口用 start/stop/status")

    def check_security(self):
        host = self.comfy_url
        loopback = host.startswith("http://127.0.0.1") or host.startswith("http://localhost")
        self.add("SEC-01", "安全", "ComfyUI 地址仅限回环", "critical",
                 "pass" if loopback else "fail", host,
                 "禁止 --listen 0.0.0.0；本机服务无鉴权")

        gi = Path(PROJECT_ROOT / ".gitignore")
        content = gi.read_text(encoding="utf-8") if gi.is_file() else ""
        needed = ["logs/", "reports/"]
        missing = [n for n in needed if n not in content]
        self.add("SEC-02", "安全", ".gitignore 覆盖运行时产物", "medium",
                 "pass" if not missing else "warn",
                 "已覆盖" if not missing else "缺少 " + ", ".join(missing),
                 "运行时数据不入版本库")

        secrets = []
        for p in sorted(CONFIG_DIR.rglob("*.json")):
            text = p.read_text(encoding="utf-8", errors="replace")
            for key in ("api_key", "apikey", "token", "password", "secret"):
                m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, text)
                if m and m.group(1) and m.group(1) not in ("", "YOUR_KEY"):
                    secrets.append(f"{p.name}:{key}")
        self.add("SEC-03", "安全", "配置中不含密钥", "high",
                 "pass" if not secrets else "fail",
                 "未发现" if not secrets else ", ".join(secrets),
                 "密钥只放环境变量或本机未入库文件")

        srv = (APP_DIR / "server.py")
        text = srv.read_text(encoding="utf-8") if srv.is_file() else ""
        self.add("SEC-04", "安全", "控制台校验 Host 为回环", "high",
                 "pass" if "_host_ok" in text else "fail",
                 "已实现 _host_ok" if "_host_ok" in text else "缺少 Host 校验",
                 "仅接受 127.0.0.1 / localhost")

        license_note = any("non-commercial" in (f.get("license") or "").lower()
                           for f in load_manifest().get("files") or [])
        self.add("SEC-05", "安全", "许可条款已标注", "low",
                 "pass" if license_note else "warn",
                 "Anima 为 CircleStone Labs 非商业许可" if license_note else "未标注",
                 "商用前核对许可")

    def check_platform(self):
        gpu = self.ps("gpu") or {}
        if gpu.get("present"):
            total = int(gpu.get("total_mb", 0))
            self.add("PLT-01", "平台", "GPU 显存满足 16GB 级", "high",
                     "pass" if total >= 12000 else "warn",
                     f"{gpu.get('name')} {gpu.get('used_mb')}/{total} MB",
                     "低于 12GB 时降低分辨率或 batch" if total < 12000 else "无需处理")
            self.metric("gpu", "GPU", f"{gpu.get('name')} {total} MB",
                        f"已用 {gpu.get('used_mb')} MB，利用率 {gpu.get('util')}%")
        else:
            self.add("PLT-01", "平台", "未检测到 NVIDIA GPU", "critical", "fail",
                     "nvidia-smi 不可用", "安装驱动后重试")

        disk = self.ps("disk") or {}
        free = float(disk.get("free_gb", 0))
        self.add("PLT-02", "平台", "磁盘余量充足（>= 50GB）", "medium",
                 "pass" if free >= 50 else "warn", f"{free} GB 可用（盘 {disk.get('drive')}）",
                 "权重 + 数据集 + 产出需要几十 GB")
        self.metric("disk_free", "磁盘可用", f"{free} GB")

        self.add("PLT-03", "平台", "Windows 平台", "info", "pass",
                 f"Windows {platform.release()}",
                 "脚本面向 Windows PowerShell 5.1")

    # ---------- run ----------
    def run(self):
        self.log("INFO", "audit started (deep=%s)" % self.deep)
        with ThreadPoolExecutor(max_workers=len(PS_MODES)) as pool:
            futures = {m: pool.submit(self._ps_raw, m) for m in PS_MODES}
            self._ps_cache = {m: f.result() for m, f in futures.items()}

        self.check_environment()
        self.check_ports()
        self.check_services()
        self.check_models()
        self.check_workflows()
        self.check_assets()
        self.check_code()
        self.check_security()
        self.check_platform()

        fails = [f for f in self.findings if f["status"] == "fail"]
        warns = [f for f in self.findings if f["status"] == "warn"]
        passes = [f for f in self.findings if f["status"] == "pass"]

        penalty = 0.0
        for f in fails:
            penalty += PENALTY.get(f["severity"], 2)
        for f in warns:
            penalty += PENALTY.get(f["severity"], 2) * 0.5
        score = max(0, min(100, round(100 - penalty)))
        grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D"

        result = {
            "generated_at": now_local().isoformat(timespec="seconds"),
            "duration_ms": int((time.time() - self.started) * 1000),
            "deep": self.deep,
            "score": score,
            "grade": grade,
            "summary": {"pass": len(passes), "warn": len(warns), "fail": len(fails),
                        "total": len(self.findings)},
            "metrics": self.metrics,
            "findings": self.findings,
            "remediations": REMEDIATIONS,
            "environment": {
                "project_root": str(PROJECT_ROOT),
                "comfyui_url": self.comfy_url,
                "comfyui_dir": self.studio_env.get("STUDIO_COMFYUI_DIR", ""),
                "python": platform.python_version(),
                "os": f"Windows {platform.release()}",
                "env": self.env,
            },
        }
        self.log("INFO", f"audit finished score={score} grade={grade}")
        if self.write:
            self.persist(result)
        return result

    def persist(self, result):
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        (REPORTS_DIR / f"audit-{stamp}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (REPORTS_DIR / "audit-latest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        report = render_markdown(result)
        (DOCS_DIR / "05-audit.md").write_text(report, encoding="utf-8")
        (REPORTS_DIR / "audit-latest.md").write_text(report, encoding="utf-8")

        history = sorted(REPORTS_DIR.glob("audit-2*.json"))
        for old in history[:-20]:
            try:
                old.unlink()
            except OSError:
                pass
        self.log("INFO", f"report written: {REPORTS_DIR / 'audit-latest.json'}")


def render_markdown(r):
    rows = []
    for f in r["findings"]:
        badge = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[f["status"]]
        rows.append(f"| {f['id']} | {f['category']} | {f['title']} | {f['severity']} "
                    f"| **{badge}** | {f['evidence']} |")
    metrics = "\n".join(f"| {m['label']} | {m['value']} | {m['hint']} |" for m in r["metrics"])
    remediation = "\n".join(f"{i}. {x}" for i, x in enumerate(r["remediations"], 1))
    return f"""# 本地创作环境审计报告

- 生成时间：{r['generated_at']}
- 评分：**{r['score']} / 100（{r['grade']}）**
- 检查项：{r['summary']['total']} 项，通过 {r['summary']['pass']}，警告 {r['summary']['warn']}，失败 {r['summary']['fail']}
- 深度校验：{'是' if r['deep'] else '否'}
- 耗时：{r['duration_ms']} ms
- 项目根：`{r['environment']['project_root']}`
- ComfyUI：`{r['environment']['comfyui_url']}`

## 关键指标

| 指标 | 值 | 说明 |
| --- | --- | --- |
{metrics}

## 检查结果

| ID | 类别 | 检查项 | 严重度 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(rows)}

## 本轮已修复

{remediation}

## 使用方式

```powershell
python app\\server.py            # 图形控制台
python app\\audit_engine.py      # 命令行审计
python tests\\lint_test.py       # 离线静态检查
python tests\\smoke_test.py      # 1024x1024 出图验收
```

报告文件：`reports/audit-latest.json`、`reports/audit-latest.md`、`docs/05-audit.md`。
"""


def run_audit(deep=False, write=True, logger=None):
    a = Audit(deep=deep, logger=logger)
    a.write = write
    return a.run()


def main():
    ap = argparse.ArgumentParser(description="Audit the local anime manga studio")
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    def log(level, msg):
        print(f"[{now_local().strftime('%H:%M:%S')}] {level:5} {msg}")

    result = run_audit(deep=args.deep, write=not args.no_write, logger=log)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\nSCORE {result['score']}/100 ({result['grade']})  "
              f"pass={result['summary']['pass']} warn={result['summary']['warn']} "
              f"fail={result['summary']['fail']}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    main()
