"""ComfyUI API-format workflow rendering and validation.

A template file is a node graph plus a `__meta__` block (title, stage, required
custom node packs, default values). `__meta__` is stripped before the graph is
POSTed to /prompt.

Validation is deliberately split in two:
  * offline  - JSON shape, placeholder coverage, link resolution (no ComfyUI)
  * online   - node class and input names against the live /object_info
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from studio import WORKFLOW_DIR, load_json

META_KEY = "__meta__"
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class WorkflowError(Exception):
    """Raised when a template cannot be rendered or is structurally invalid."""


def load_template(name: str) -> dict:
    data = load_json(WORKFLOW_DIR / name)
    if data is None:
        raise WorkflowError(f"workflow not found or invalid JSON: {name}")
    return data


def meta_of(raw: dict) -> dict:
    return raw.get(META_KEY) or {}


def node_ids(raw: dict):
    return [k for k in raw.keys() if k != META_KEY]


def strip_meta(raw: dict) -> dict:
    """Return the /prompt payload: every top-level node, without __meta__."""
    return {k: v for k, v in raw.items() if k != META_KEY}


def _substitute(value, values: dict):
    """Replace placeholders, preserving numeric types for whole-value tokens."""
    if isinstance(value, str):
        whole = _PLACEHOLDER_RE.fullmatch(value.strip())
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        out = value
        for key, val in values.items():
            out = out.replace("{{%s}}" % key, "" if val is None else str(val))
        return out
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
        return [_substitute(value[0], values), value[1]]
    return value


def render(raw: dict, overrides=None) -> dict:
    """Merge __meta__.defaults with overrides and produce a /prompt payload."""
    values = dict(meta_of(raw).get("defaults") or {})
    values.update(overrides or {})

    graph = {}
    for nid, node in strip_meta(raw).items():
        inputs = {}
        for key, val in (node.get("inputs") or {}).items():
            inputs[key] = _substitute(val, values)
        graph[nid] = {"class_type": _substitute(node.get("class_type", ""), values),
                      "inputs": inputs}
    return graph


def render_named(name: str, overrides=None) -> dict:
    return render(load_template(name), overrides)


# --------------------------------------------------------------------------- #
# offline validation
# --------------------------------------------------------------------------- #
def validate_offline(raw: dict, name: str = "?") -> dict:
    """Structure checks that need no running ComfyUI."""
    problems, warnings = [], []
    meta = meta_of(raw)

    if not meta:
        warnings.append("缺少 __meta__ 块（标题、默认值、依赖节点包）")

    nodes = strip_meta(raw)
    if not nodes:
        problems.append("工作流没有任何节点")

    defaults = meta.get("defaults") or {}
    used = set()

    for nid, node in nodes.items():
        if not isinstance(node, dict):
            problems.append(f"节点 {nid} 不是对象")
            continue
        ctype = node.get("class_type")
        if not isinstance(ctype, str) or not ctype:
            problems.append(f"节点 {nid} 缺少 class_type")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            problems.append(f"节点 {nid} 缺少 inputs")
            continue
        for key, val in inputs.items():
            if isinstance(val, str):
                used.update(_PLACEHOLDER_RE.findall(val))
            if isinstance(val, list):
                if len(val) != 2 or not isinstance(val[0], str) or not isinstance(val[1], int):
                    problems.append(f"节点 {nid}.{key} 的连线格式应为 [node_id, slot]")

    unknown = sorted(used - set(defaults))
    if unknown:
        problems.append("占位符缺少默认值: " + ", ".join(unknown))

    # Links must point at an existing node.
    for nid, node in nodes.items():
        for key, val in (node.get("inputs") or {}).items():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                src = _PLACEHOLDER_RE.sub("", val[0]).strip()
                if src not in nodes:
                    problems.append(f"节点 {nid}.{key} 指向不存在的节点 {val[0]}")

    # The graph must reach an output node, otherwise the run produces nothing.
    sinks = {"SaveImage", "PreviewImage", "VHS_VideoCombine", "SaveAnimatedWEBP"}
    if not any(node.get("class_type") in sinks for node in nodes.values()):
        warnings.append("没有输出节点（SaveImage / VHS_VideoCombine）")

    return {
        "name": name,
        "title": meta.get("title", name),
        "stage": meta.get("stage"),
        "requires_packs": meta.get("requires_packs") or [],
        "nodes": len(nodes),
        "placeholders": sorted(used),
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
    }


# --------------------------------------------------------------------------- #
# online validation
# --------------------------------------------------------------------------- #
def validate_online(raw: dict, object_info: dict, name: str = "?") -> dict:
    """Check class types and input names against a live /object_info payload."""
    result = validate_offline(raw, name)
    problems = list(result["problems"])
    missing_nodes, missing_inputs = [], []

    if object_info:
        for nid, node in strip_meta(raw).items():
            ctype = node.get("class_type")
            spec = object_info.get(ctype)
            if spec is None:
                missing_nodes.append(f"{nid}:{ctype}")
                continue
            required = ((spec.get("input") or {}).get("required") or {})
            optional = ((spec.get("input") or {}).get("optional") or {})
            allowed = set(required) | set(optional)
            for key in (node.get("inputs") or {}):
                if key not in allowed:
                    missing_inputs.append(f"{nid}:{ctype}.{key}")

    if missing_nodes:
        problems.append("ComfyUI 未注册的节点: " + ", ".join(sorted(set(missing_nodes))))
    if missing_inputs:
        problems.append("节点不接受的输入: " + ", ".join(sorted(set(missing_inputs))))

    result["problems"] = problems
    result["ok"] = not problems
    result["missing_nodes"] = sorted(set(missing_nodes))
    result["missing_inputs"] = sorted(set(missing_inputs))
    return result


def validate_all(object_info=None) -> list:
    """Validate every template in config/workflows."""
    out = []
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        raw = load_json(path)
        if raw is None:
            out.append({"name": path.name, "problems": ["JSON 无法解析"], "ok": False})
            continue
        out.append(validate_online(raw, object_info or {}, path.name)
                   if object_info else validate_offline(raw, path.name))
    return out
