# -*- coding: utf-8 -*-
"""把手写的 LoRA 栈写进已经渲染好的计算图。

工作流模板里自带的 LoRA 节点被整体替换成调用方给的栈：
    基座 UNET -> lora1 -> lora2 -> ... -> 采样器
权重由调用方决定，所以「条件 LoRA 权重」不需要改任何 workflow JSON。
"""
from __future__ import annotations

import json

LORA_TYPES = ("LoraLoaderModelOnly", "LoraLoader")


def _clone(obj):
    return json.loads(json.dumps(obj))


def _deref(graph: dict, ref):
    """沿着 model 连线穿过 LoRA 节点，找到真正的基座（UNETLoader 等）引用。"""
    node_id = str(ref[0])
    slot = ref[1] if len(ref) > 1 else 0
    node = graph.get(node_id) or {}
    if node.get("class_type") in LORA_TYPES:
        upstream = (node.get("inputs") or {}).get("model")
        if isinstance(upstream, list):
            return _deref(graph, upstream)
    return [node_id, slot]


def normalize_stack(stack) -> list:
    out = []
    for item in stack or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        out.append({"name": name, "strength": max(-2.0, min(2.0, strength))})
    return out


def apply_stack(graph: dict, stack, known_names=None) -> tuple:
    """Return (graph, applied_stack). Empty stack leaves the graph untouched."""
    stack = normalize_stack(stack)
    if not stack:
        return graph, []
    if known_names is not None:
        unknown = [x["name"] for x in stack if x["name"] not in known_names]
        if unknown:
            raise ValueError("未安装的 LoRA：" + ", ".join(unknown))

    g = _clone(graph)
    lora_ids = [nid for nid, node in g.items()
                if isinstance(node, dict) and node.get("class_type") in LORA_TYPES]
    # 采样器等「消费 model」的节点（LoRA 节点本身除外），它们的 model 输入要接到新链尾。
    consumers = [nid for nid, node in g.items()
                 if isinstance(node, dict)
                 and node.get("class_type") not in LORA_TYPES
                 and isinstance((node.get("inputs") or {}).get("model"), list)]
    if not consumers:
        raise ValueError("工作流里找不到采样器的 model 连线")

    base = _deref(g, g[consumers[0]]["inputs"]["model"])
    for nid in lora_ids:
        g.pop(nid, None)

    chain = base
    for index, item in enumerate(stack, 1):
        node_id = f"lora_stack_{index}"
        g[node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": item["name"],
                       "strength_model": round(float(item["strength"]), 3),
                       "model": chain},
        }
        chain = [node_id, 0]
    for nid in consumers:
        g[nid]["inputs"]["model"] = chain
    return g, stack
