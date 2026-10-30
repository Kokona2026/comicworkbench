"""Fill stage 1: render character-design candidates for the bible character.

Writes to project/01_dataset/candidates/. Uses the project's own Anima txt2img
template and ComfyUI client, so what lands here is the same pipeline the console
uses. 40 frames by default (the gate accepts 40-60).

    python app/gen_candidates.py [--count 40] [--dry-run]

Requires ComfyUI listening on the configured host (start.cmd or 10-start-comfyui).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
PROJECT = APP.parent
sys.path.insert(0, str(APP))

import comfy  # noqa: E402
from workflows import render_named  # noqa: E402

BIBLE = PROJECT / "project" / "00_bible" / "character.json"
OUT = PROJECT / "project" / "01_dataset" / "candidates"
WORKFLOW = "anima-txt2img.json"

# 40 distinct framings: pose x angle x expression, cycled deterministically.
SHOTS = [
    "standing, hands at sides, front view, looking at viewer, calm expression",
    "standing, three quarter view, hand on hairpin, soft smile",
    "standing, side view, looking away, profile",
    "sitting on a chair, hands on lap, front view, gentle smile",
    "sitting by a window, looking outside, side light",
    "upper body, arms crossed, confident expression",
    "upper body, holding a book, looking at viewer",
    "close-up face, neutral expression, front light",
    "close-up face, slight smile, rim light",
    "full body, walking, motion in hair and skirt",
    "full body, back view, looking over shoulder",
    "full body, leaning on a desk, relaxed",
    "kneeling, arranging papers, downward gaze",
    "standing in a hallway, hands behind back",
    "upper body, adjusting collar, eyes closed",
    "sitting on the floor, knees together, small smile",
    "full body, standing in uniform, wind",
    "upper body, tilting head, curious expression",
    "close-up, hands holding blue hairpin",
    "full body, standing near bookshelf, arms folded",
]

LIGHTING = ["soft daylight", "warm evening light", "overcast light", "rim light"]


def load_bible() -> dict:
    return json.loads(BIBLE.read_text(encoding="utf-8"))


def character_prompt(bible: dict) -> str:
    parts = [
        "1girl", "solo", bible.get("body", ""),
        bible.get("hair", ""), bible.get("eyes", ""),
        bible.get("outfit", ""), bible.get("signature", ""),
        bible.get("personality", ""),
        "clean lineart", "detailed eyes", "white background",
    ]
    return ", ".join(p for p in parts if p)


NEGATIVE = (
    "nsfw, nude, worst quality, low quality, blurry, jpeg artifacts, watermark, "
    "signature, text, extra fingers, bad hands, bad anatomy, deformed, cropped head, "
    "chibi, child, toddler, short stature"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    bible = load_bible()
    base = character_prompt(bible)
    OUT.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + 1800
    made = 0
    for index in range(args.count):
        pose = SHOTS[index % len(SHOTS)]
        light = LIGHTING[(index // len(SHOTS)) % len(LIGHTING)]
        width, height = (1024, 1024) if index % 5 == 0 else (832, 1216)
        values = {
            "positive": f"{base}, {pose}, {light}",
            "negative": NEGATIVE,
            "seed": 730000 + index * 7,
            "steps": 28,
            "cfg": 4.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "width": width,
            "height": height,
            "batch_size": 1,
            "filename_prefix": f"cand/cand_{index + 1:02d}",
        }
        graph = render_named(WORKFLOW, values)
        if args.dry_run:
            print(index + 1, values["positive"][:80])
            made += 1
            continue

        pid = comfy.submit(graph)["prompt_id"]
        started = time.time()
        while time.time() < deadline:
            status = comfy.job_status(pid)
            if status.get("state") in ("done", "error"):
                break
            if time.time() - started > 300:
                print(f"{index + 1}: timeout", flush=True)
                break
            time.sleep(2)
        if status.get("state") != "done":
            print(f"{index + 1}: fail {status.get('message')}", flush=True)
            continue
        for item in status.get("outputs") or []:
            if item.get("kind") != "image":
                continue
            data, _ = comfy.view_bytes(item["filename"], item.get("subfolder", ""), item.get("type", "output"))
            (OUT / f"cand_{index + 1:02d}.png").write_bytes(data)
            made += 1
            break
        print(f"{index + 1}/{args.count} -> cand_{index + 1:02d}.png", flush=True)

    print(f"done: {made} candidates in {OUT}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
