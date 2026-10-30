"""Stage 2 helper: curate candidates into a captioned training set.

Copies N candidates into project/01_dataset/train/ and writes a matching .txt for
each one, starting with the bible trigger word (that is what the stage-2 gate
checks). Captions use the character bible fields so the LoRA learns identity, not
the background.

    python app/build_train_set.py [--count 24] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
PROJECT = APP.parent
BIBLE = PROJECT / "project" / "00_bible" / "character.json"
CAND = PROJECT / "project" / "01_dataset" / "candidates"
TRAIN = PROJECT / "project" / "01_dataset" / "train"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    bible = json.loads(BIBLE.read_text(encoding="utf-8"))
    trigger = bible.get("trigger") or "char_aya"
    identity = ", ".join(
        bible.get(k, "") for k in ("body", "hair", "eyes", "outfit", "signature") if bible.get(k)
    )
    caption = f"{trigger}, {identity}, solo, simple background, clean lineart"

    images = sorted(CAND.glob("*.png"))[: args.count]
    if not images:
        print("no candidates found; run app/gen_candidates.py first")
        return 2

    TRAIN.mkdir(parents=True, exist_ok=True)
    written = 0
    for image in images:
        target = TRAIN / image.name
        if args.dry_run:
            print(target.name, "->", caption[:90])
            written += 1
            continue
        shutil.copyfile(image, target)
        target.with_suffix(".txt").write_text(caption, encoding="utf-8")
        written += 1

    print(f"train set: {written} images + captions in {TRAIN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
