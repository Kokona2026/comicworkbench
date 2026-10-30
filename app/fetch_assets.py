"""Segmented, resumable downloader for model weights (stdlib only).

Why not curl: a single stream measured 0.5-0.9 MB/s on this machine, while eight
concurrent range requests reach ~4 MB/s. HF and its mirrors both honour Range, so
the bottleneck is per-connection, not bandwidth.

Correctness rules (learned the hard way):
  * never preallocate the destination. A truncated sparse file reports the full
    size, so "size == expected" would accept an all-zero file;
  * each segment is written to its own file under <dest>.parts/, so progress is
    real bytes on disk and a resumed run does not rewrite finished segments;
  * the destination only appears after every segment is complete AND the
    assembled size matches what the server reported.

CLI:
    python app/fetch_assets.py --url URL --dest PATH [--min-bytes N]
                               [--segments 8] [--proxy URL] [--quiet]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
SEGMENT_BYTES = 32 * 1024 * 1024
READ_CHUNK = 256 * 1024

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fGB" % n


def build_opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def remote_size(opener, url: str) -> int:
    headers = {"User-Agent": UA, "Range": "bytes=0-0"}
    with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        content_range = resp.headers.get("Content-Range") or ""
        if "/" in content_range:
            return int(content_range.rsplit("/", 1)[1])
        length = resp.headers.get("Content-Length")
        if length and int(length) > 1:
            return int(length)
    raise RuntimeError("server did not report a usable size")


class SegmentedDownload:
    def __init__(self, url, dest: Path, total: int, segments: int, opener, retries=3):
        self.url = url
        self.dest = dest
        self.total = total
        self.opener = opener
        self.retries = retries
        self.lock = threading.Lock()
        self.written = 0
        self.errors = []

        self.parts_dir = dest.parent / (dest.name + ".parts")
        count = max(1, min(segments, (total + SEGMENT_BYTES - 1) // SEGMENT_BYTES))
        self.bounds = []
        step = total // count
        for i in range(count):
            start = i * step
            end = total - 1 if i == count - 1 else (i + 1) * step - 1
            self.bounds.append((start, end))

    def part_path(self, index: int) -> Path:
        return self.parts_dir / ("%03d" % index)

    def existing_bytes(self) -> int:
        total = 0
        if self.parts_dir.is_dir():
            for index in range(len(self.bounds)):
                path = self.part_path(index)
                if path.is_file():
                    total += min(path.stat().st_size, self._want(index))
        return total

    def _want(self, index: int) -> int:
        start, end = self.bounds[index]
        return end - start + 1

    def _fetch(self, index: int) -> None:
        start, end = self.bounds[index]
        want = end - start + 1
        path = self.part_path(index)

        for attempt in range(1, self.retries + 1):
            have = path.stat().st_size if path.is_file() else 0
            if have >= want:
                with self.lock:
                    self.written += min(have, want)
                return
            if have > want:  # stale oversized part from a different split
                path.unlink()
                have = 0
            try:
                headers = {"User-Agent": UA,
                           "Range": "bytes=%d-%d" % (start + have, end)}
                with self.opener.open(
                        urllib.request.Request(self.url, headers=headers), timeout=120) as resp:
                    if resp.status != 206:
                        raise RuntimeError("server ignored Range (status %d)" % resp.status)
                    with open(path, "ab") as fh:
                        while True:
                            chunk = resp.read(READ_CHUNK)
                            if not chunk:
                                break
                            fh.write(chunk)
                            with self.lock:
                                self.written += len(chunk)
                if path.stat().st_size == want:
                    return
                raise RuntimeError("segment %d short (%d/%d)"
                                   % (index, path.stat().st_size, want))
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.errors.append("seg%d try%d: %s" % (index, attempt, str(exc)[:70]))
                time.sleep(min(2 * attempt, 10))
        raise RuntimeError("segment %d failed after %d attempts" % (index, self.retries))

    def run(self) -> None:
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self.written = self.existing_bytes()
        pending = [i for i in range(len(self.bounds)) if self._want(i) > self.existing_part(i)]
        if not pending:
            return

        stop = threading.Event()

        def reporter():
            last = self.written
            while not stop.wait(5):
                with self.lock:
                    now = self.written
                speed = (now - last) / 5.0
                last = now
                pct = now / self.total * 100
                eta = (self.total - now) / speed if speed > 1 else 0
                log("[..] %5.1f%%  %s/%s  %s/s  eta %dm%02ds"
                    % (pct, human(now), human(self.total), human(speed),
                       int(eta // 60), int(eta % 60)))

        thread = threading.Thread(target=reporter, daemon=True)
        thread.start()
        workers = []
        for index in pending:
            worker = threading.Thread(target=self._fetch, args=(index,), daemon=True)
            worker.start()
            workers.append(worker)
        for worker in workers:
            worker.join()
        stop.set()

    def existing_part(self, index: int) -> int:
        path = self.part_path(index)
        return path.stat().st_size if path.is_file() else 0

    def commit(self) -> None:
        missing = [i for i in range(len(self.bounds))
                   if self.existing_part(i) != self._want(i)]
        if missing:
            raise RuntimeError("segments incomplete: %s" % missing[:8])

        tmp = self.dest.parent / (self.dest.name + ".assembling")
        with open(tmp, "wb") as out:
            for index in range(len(self.bounds)):
                with open(self.part_path(index), "rb") as fh:
                    shutil.copyfileobj(fh, out, 4 * 1024 * 1024)
        if tmp.stat().st_size != self.total:
            tmp.unlink()
            raise RuntimeError("assembled size mismatch")
        os.replace(tmp, self.dest)
        shutil.rmtree(self.parts_dir, ignore_errors=True)


def cleanup_parts(dest: Path) -> None:
    shutil.rmtree(dest.parent / (dest.name + ".parts"), ignore_errors=True)
    assembling = dest.parent / (dest.name + ".assembling")
    if assembling.exists():
        assembling.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="Segmented resumable downloader")
    ap.add_argument("--url", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--min-bytes", type=int, default=0)
    ap.add_argument("--segments", type=int, default=8)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    dest = Path(args.dest)
    opener = build_opener(args.proxy)

    try:
        total = remote_size(opener, args.url)
    except Exception as exc:  # noqa: BLE001
        log("[FAIL] cannot size %s: %s" % (args.url, exc))
        return 1

    if total < args.min_bytes:
        log("[FAIL] remote size %s < min_bytes %s" % (human(total), human(args.min_bytes)))
        return 1

    if dest.is_file() and dest.stat().st_size == total:
        if not args.quiet:
            log("[ok] already complete: %s (%s)" % (dest.name, human(total)))
        return 0
    if dest.is_file():
        log("[warn] %s has %s bytes, expected %s - replacing"
            % (dest.name, human(dest.stat().st_size), human(total)))
        dest.unlink()

    if not args.quiet:
        log("[..] %s  %s  %d segments" % (dest.name, human(total), args.segments))

    download = SegmentedDownload(args.url, dest, total, args.segments, opener, args.retries)
    try:
        download.run()
        download.commit()
    except Exception as exc:  # noqa: BLE001
        log("[FAIL] %s: %s" % (dest.name, exc))
        if download.errors:
            log("       last error: %s" % download.errors[-1])
        return 1

    size = dest.stat().st_size
    if size != total or size < args.min_bytes:
        log("[FAIL] final size %s != %s" % (human(size), human(total)))
        dest.unlink(missing_ok=True)
        return 1

    log("[ok] %s  %s" % (dest.name, human(size)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
