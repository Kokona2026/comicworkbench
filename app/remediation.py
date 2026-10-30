"""One-click remediation for audit findings.

The audit engine only reports facts; this module decides which facts the console
may act on and what exactly to run. It holds no HTTP or process logic so the
mapping is testable: ``plan(finding)`` is a pure function.

Fix kinds:
    action  run a server action (``/api/fix``) and re-audit
    copy    put text on the clipboard (frontend only)
    goto    switch to the page where the problem is solved (frontend only)
"""

from __future__ import annotations

DOWNLOAD_CMD = ("powershell -NoProfile -ExecutionPolicy Bypass "
                "-File scripts\\05-sync-assets.ps1")

FIXES: dict[str, dict] = {
    # --- environment / services ------------------------------------------- #
    "ENV-04": {
        "label": "复制安装命令", "kind": "copy",
        "text": ("powershell -NoProfile -ExecutionPolicy Bypass "
                 "-File scripts\\03-install-comfyui.ps1"),
        "note": "安装 ComfyUI 便携版到项目内的 ComfyUI/",
    },
    "ENV-05": {
        "label": "初始化目录", "kind": "action", "action": "init_project",
        "note": "重建 project/01–05 目录结构",
    },
    "NET-01": {
        "label": "启动 ComfyUI", "kind": "action", "action": "comfyui_start",
        "note": "后台拉起服务并等待 /system_stats 就绪",
    },
    "SVC-01": {
        "label": "启动 ComfyUI", "kind": "action", "action": "comfyui_start",
    },
    "SVC-04": {
        "label": "清理重复实例", "kind": "action", "action": "dedupe_comfy",
        "confirm": "将结束除 8188 监听进程以外的 ComfyUI 进程，继续？",
        "note": "每个实例都会独立占显存",
    },
    "PLT-01": {
        "label": "释放显存", "kind": "action", "action": "free_vram",
        "confirm": "将卸载 ComfyUI 已加载模型和全部 Ollama 模型，继续？",
        "note": "出图前通常需要这一步",
    },
    # --- workflow / code --------------------------------------------------- #
    "WKF-01": {"label": "重新校验工作流", "kind": "action", "action": "verify_workflow"},
    "WKF-02": {"label": "重新校验工作流", "kind": "action", "action": "verify_workflow"},
    "WKF-03": {"label": "重新校验工作流", "kind": "action", "action": "verify_workflow"},
    "WKF-04": {"label": "重新校验工作流", "kind": "action", "action": "verify_workflow"},
    "COD-01": {"label": "重跑静态检查", "kind": "action", "action": "test_lint"},
    "COD-02": {"label": "重跑静态检查", "kind": "action", "action": "test_lint"},
    "COD-03": {"label": "重跑静态检查", "kind": "action", "action": "test_lint"},
    "COD-04": {"label": "重跑静态检查", "kind": "action", "action": "test_lint"},
    "COD-05": {"label": "重跑静态检查", "kind": "action", "action": "test_lint"},
    # --- models ------------------------------------------------------------ #
    "MDL-01": {"label": "复制下载命令", "kind": "copy", "text": DOWNLOAD_CMD},
    "MDL-02": {"label": "复制下载命令", "kind": "copy", "text": DOWNLOAD_CMD},
    "MDL-03": {"label": "复制下载命令", "kind": "copy", "text": DOWNLOAD_CMD},
    "MDL-04": {
        "label": "复制缺失清单", "kind": "copy", "from_evidence": True,
        "note": "可选权重，按需下载",
    },
    # --- stage gates ------------------------------------------------------- #
    "AST-01": {"label": "去创作页", "kind": "goto", "tab": "studio",
               "note": "设定表与候选图都在这里产出"},
    "AST-03": {"label": "去创作页", "kind": "goto", "tab": "studio"},
    "AST-04": {"label": "去创作页", "kind": "goto", "tab": "studio"},
}


def plan(finding) -> dict | None:
    """Fix descriptor for one finding, or None when it needs a human."""
    if not isinstance(finding, dict):
        return None
    if finding.get("status") == "pass":
        return None
    template = FIXES.get(str(finding.get("id") or ""))
    if not template:
        return None
    fix = dict(template)
    if fix.get("from_evidence"):
        fix["text"] = str(finding.get("evidence") or "")
    return fix


def annotate(findings) -> list:
    """Attach a fix descriptor to every fixable finding (server-side helper)."""
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        fix = plan(finding)
        if fix:
            finding["fix"] = fix
    return findings


def summary() -> dict:
    """Ids grouped by fix kind, handy for docs and tests."""
    out: dict[str, list] = {"action": [], "copy": [], "goto": []}
    for cid, fix in FIXES.items():
        out.setdefault(fix["kind"], []).append(cid)
    return out
