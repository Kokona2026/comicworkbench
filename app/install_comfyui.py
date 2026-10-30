"""Install ComfyUI next to the project, with a CUDA build of PyTorch.

Why this exists instead of "download the portable 7z":
  * the portable bundle is a single ~1.5 GB asset served from GitHub releases,
    which is slow and not resumable here;
  * this machine has a working Python and a reachable PyTorch index, so a
    source checkout + venv is smaller, resumable and inspectable;
  * the Blackwell GPU (sm_120) needs a cu128 build, and Windows PyPI wheels are
    CPU-only, so torch must come from download.pytorch.org.

Every step is idempotent: re-running skips finished work and resumes partial
downloads. Nothing here is destructive.

CLI:
    python app/install_comfyui.py [--dir PATH] [--torch-version auto|2.9.1]
                                  [--skip-torch] [--force-source]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DEFAULT_DIR = PROJECT_ROOT / "ComfyUI"
CACHE_DIR = PROJECT_ROOT / ".cache" / "downloads"

SOURCE_ZIP = "https://codeload.github.com/comfyanonymous/ComfyUI/zip/refs/heads/master"
SOURCE_GIT = "https://github.com/comfyanonymous/ComfyUI.git"
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# The cu128 wheels live on pytorch.org, which rate-limits (403) after a burst of
# requests. Mirrors are tried in order; the first one that answers wins.
# Windows PyPI wheels are CPU-only, so a mirror of download.pytorch.org is the
# only correct source here.
#
# layout: "pep503" = <base>/<pkg>/<wheel>,  "flat" = <base>/<wheel>
TORCH_MIRRORS = [
    ("https://mirror.sjtu.edu.cn/pytorch-wheels/cu128", "flat"),
    ("https://download.pytorch.org/whl/cu128", "pep503"),
    ("https://mirrors.aliyun.com/pytorch-wheels/cu128", "flat"),
]
TORCH_MIRROR_HOSTS = [base.split("/")[2] for base, _ in TORCH_MIRRORS]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

TORCH_PKGS = ("torch", "torchvision", "torchaudio")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def log(msg: str):
    print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


# --------------------------------------------------------------------------- #
# download with resume
# --------------------------------------------------------------------------- #
def remote_size(url: str, timeout: int = 30):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
    except Exception:  # noqa: BLE001
        return None


def download(url: str, dest: Path, retries: int = 6) -> Path:
    """Resumable download. A dropped 2.5 GB wheel must not restart from zero."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = remote_size(url)
    for attempt in range(1, retries + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if total and have >= total:
            log(f"[ok] cached {dest.name} ({human(have)})")
            return dest
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                t0 = time.time()
                last_report = 0.0
                with open(dest, mode) as fh:
                    while True:
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        now = time.time()
                        if now - last_report > 5:
                            last_report = now
                            speed = have / max(now - t0, 0.1)
                            if total:
                                pct = have / total * 100
                                eta = (total - have) / max(speed, 1)
                                log(f"[..] {dest.name}  {pct:5.1f}%  "
                                    f"{human(have)}/{human(total)}  "
                                    f"{human(speed)}/s  eta {int(eta // 60)}m{int(eta % 60):02d}s")
                            else:
                                log(f"[..] {dest.name}  {human(have)}  {human(speed)}/s")
        except urllib.error.HTTPError as exc:
            # 403/404 will not fix themselves; let the caller try another mirror.
            if exc.code in (403, 404, 410):
                raise RuntimeError(f"HTTP {exc.code}") from exc
            log(f"[warn] download interrupted ({attempt}/{retries}): {exc}")
            time.sleep(min(3 * attempt, 15))
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"[warn] download interrupted ({attempt}/{retries}): {exc}")
            time.sleep(min(3 * attempt, 15))
            continue

        got = dest.stat().st_size
        if total is None or got >= total:
            log(f"[ok] downloaded {dest.name} ({human(got)})")
            return dest
        log(f"[warn] short read {human(got)}/{human(total)}, resuming")
    raise RuntimeError(f"download failed after {retries} attempts: {url}")


# --------------------------------------------------------------------------- #
# torch wheel resolution
# --------------------------------------------------------------------------- #
def _version_key(v: str):
    return [int(x) for x in re.findall(r"\d+", v)[:3]]


def available_versions(pkg: str, py_tag: str) -> list:
    """Read the PEP503 listing from the first mirror that answers."""
    pattern = rf"{pkg}-([\d.]+)%2Bcu128-{py_tag}-{py_tag}-win_amd64\.whl"
    for base, _ in TORCH_MIRRORS:
        url = f"{base}/{pkg}/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            log(f"[warn] listing unavailable at {base} ({exc})")
            continue
        found = sorted(set(re.findall(pattern, html)), key=_version_key)
        if found:
            log(f"[info] {pkg} versions from {base.split('/')[2]}: {found[-4:]}")
            return found
    raise RuntimeError(f"no mirror lists {pkg} for {py_tag} win_amd64")


def pick_torch(py_tag: str, wanted: str) -> dict:
    """Return {'torch': '2.9.1', 'torchvision': '0.24.1', ...} for one line."""
    versions = {p: available_versions(p, py_tag) for p in TORCH_PKGS}
    for p, v in versions.items():
        if not v:
            raise RuntimeError(f"no cu128 wheel for {p} ({py_tag}, win_amd64)")

    if wanted == "auto":
        torch_v = versions["torch"][-1]
    else:
        if wanted not in versions["torch"]:
            raise RuntimeError(f"torch {wanted} has no cu128 wheel; "
                               f"available: {', '.join(versions['torch'][-5:])}")
        torch_v = wanted

    major = int(torch_v.split(".")[0])
    minor = int(torch_v.split(".")[1])

    def closest(pkg, target_minor):
        # torch X.Y <-> torchvision 0.(Y+15) <-> torchaudio X.Y
        want = f"{major}.{target_minor}"
        if want in versions[pkg]:
            return want
        return versions[pkg][-1]

    chosen = {"torch": torch_v}
    if versions["torchvision"]:
        chosen["torchvision"] = closest("torchvision", minor + 15)
    if versions["torchaudio"]:
        chosen["torchaudio"] = f"{major}.{minor}"
        if chosen["torchaudio"] not in versions["torchaudio"]:
            chosen["torchaudio"] = versions["torchaudio"][-1]
    return chosen


def torch_wheel_urls(pkg: str, version: str, py_tag: str) -> list:
    """Same wheel, one candidate URL per mirror, in preference order."""
    name = f"{pkg}-{version}%2Bcu128-{py_tag}-{py_tag}-win_amd64.whl"
    urls = []
    for base, layout in TORCH_MIRRORS:
        urls.append(f"{base}/{name}" if layout == "flat" else f"{base}/{pkg}/{name}")
    return urls


def download_any(urls: list, dest: Path, retries: int = 4) -> Path:
    """Try each mirror until one completes. Partial bytes are kept for resume."""
    errors = []
    for url in urls:
        try:
            return download(url, dest, retries)
        except Exception as exc:  # noqa: BLE001
            host = url.split("/")[2]
            log(f"[warn] mirror {host} failed: {exc}")
            errors.append(f"{host}: {exc}")
            # Keep partial bytes: the next mirror serves the same file and the
            # range request resumes from where this one stopped.
    raise RuntimeError("all mirrors failed -> " + "; ".join(errors))


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def run(cmd, cwd=None, check=True):
    log("     $ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {cmd[0]}")
    return proc.returncode


def fetch_source(target: Path, force: bool) -> Path:
    if (target / "main.py").is_file() and not force:
        log(f"[ok] source already present at {target}")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / "ComfyUI-master.zip"
    try:
        download(SOURCE_ZIP, zip_path)
        log("[..] extracting source")
        staging = target.parent / "_comfy_staging"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
        roots = [p for p in staging.iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("unexpected archive layout")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(roots[0]), str(target))
        shutil.rmtree(staging, ignore_errors=True)
        log(f"[ok] source installed at {target}")
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] zip route failed ({exc}); falling back to git clone")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        run(["git", "clone", "--depth", "1", SOURCE_GIT, str(target)])
        log(f"[ok] source cloned to {target}")
    return target


def ensure_venv(target: Path) -> Path:
    py = target / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        log("[..] creating venv")
        run([sys.executable, "-m", "venv", str(target / ".venv")])
        if not py.is_file():
            raise RuntimeError("venv created without python.exe")
    else:
        log(f"[ok] venv present: {py}")

    # torch 2.11 declares setuptools<82; a fresh venv ships a newer one, which pip
    # then reports as a resolver conflict on every later install.
    log("[..] pinning build tools (pip, wheel, setuptools<82)")
    run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip", "wheel", "setuptools<82",
         "-i", PIP_MIRROR])
    log(f"[ok] venv ready: {py}")
    return py


def py_tag(py: Path) -> str:
    out = subprocess.run([str(py), "-c",
                          "import sys;print(f'cp{sys.version_info[0]}{sys.version_info[1]}')"],
                         capture_output=True, text=True)
    tag = out.stdout.strip()
    if not re.fullmatch(r"cp\d{2,3}", tag):
        raise RuntimeError(f"unexpected python tag: {tag!r}")
    return tag


def install_torch(py: Path, wanted: str):
    tag = py_tag(py)
    chosen = pick_torch(tag, wanted)
    log("[..] torch plan: " + ", ".join(f"{k}=={v}" for k, v in chosen.items()))

    wheels = []
    for pkg, ver in chosen.items():
        urls = torch_wheel_urls(pkg, ver, tag)
        dest = CACHE_DIR / urllib.parse.unquote(urls[0].rsplit("/", 1)[-1])
        wheels.append(download_any(urls, dest))

    log("[..] installing torch wheels (no deps)")
    run([str(py), "-m", "pip", "install", "--no-deps", "--no-cache-dir", "-q"] +
        [str(w) for w in wheels])

    deps = ["filelock", "typing-extensions", "sympy", "networkx", "jinja2", "fsspec",
            "setuptools<82", "numpy", "pillow"]
    log("[..] installing torch runtime deps")
    run([str(py), "-m", "pip", "install", "-q", "-i", PIP_MIRROR] + deps)


def install_requirements(py: Path, target: Path):
    req = target / "requirements.txt"
    if not req.is_file():
        log("[warn] no requirements.txt, skipping")
        return
    lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
    kept, skipped = [], []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # Match the distribution name only: "torchsde" must NOT be mistaken for
        # "torch", and "torch>=2.0" must still be filtered.
        m = re.match(r"([A-Za-z0-9_.\-]+)", text)
        name = m.group(1).lower() if m else ""
        if name in TORCH_PKGS:
            skipped.append(text)
            continue
        kept.append(line)

    filtered = CACHE_DIR / "requirements.notorch.txt"
    filtered.parent.mkdir(parents=True, exist_ok=True)
    filtered.write_text("\n".join(kept) + "\n", encoding="utf-8")
    if skipped:
        log(f"[info] torch lines handled separately: {', '.join(skipped)}")

    # A few Comfy-Org packages (workflow template media assets) are not mirrored;
    # official PyPI is kept as a fallback index so they still resolve.
    log(f"[..] installing {len(kept)} remaining requirements (mirror + pypi fallback)")
    run([str(py), "-m", "pip", "install", "-q",
         "-i", PIP_MIRROR, "--extra-index-url", "https://pypi.org/simple",
         "-r", str(filtered)])


def verify(py: Path):
    code = (
        "import torch, torchvision, sys\n"
        "print('torch      :', torch.__version__)\n"
        "print('torchvision:', torchvision.__version__)\n"
        "print('cuda build :', torch.version.cuda)\n"
        "print('available  :', torch.cuda.is_available())\n"
        "if torch.cuda.is_available():\n"
        "    print('device     :', torch.cuda.get_device_name(0))\n"
        "    print('capability :', '.'.join(map(str, torch.cuda.get_device_capability(0))))\n"
        "    x = torch.randn(512, 512, device='cuda'); y = (x @ x).sum().item()\n"
        "    print('matmul ok  :', isinstance(y, float))\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    log("[..] verifying torch + CUDA")
    rc = subprocess.run([str(py), "-c", code]).returncode
    if rc != 0:
        raise RuntimeError("torch cannot reach the GPU; check the driver and the cu128 build")
    log("[ok] torch is CUDA-capable")


def main():
    ap = argparse.ArgumentParser(description="Install ComfyUI with a cu128 PyTorch")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--torch-version", default="auto",
                    help="'auto' for the newest cu128 build, or an exact version")
    ap.add_argument("--skip-torch", action="store_true")
    ap.add_argument("--skip-requirements", action="store_true")
    ap.add_argument("--force-source", action="store_true")
    args = ap.parse_args()

    target = Path(args.dir).expanduser().resolve()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    log("== ComfyUI installer ==")
    log(f"   target : {target}")
    log(f"   python : {sys.version.split()[0]} ({sys.executable})")
    log(f"   torch  : {args.torch_version} via {', '.join(TORCH_MIRROR_HOSTS)}")
    log(f"   mirror : {PIP_MIRROR}")
    log("")

    try:
        source = fetch_source(target, args.force_source)
        py = ensure_venv(source)
        if not args.skip_torch:
            install_torch(py, args.torch_version)
        if not args.skip_requirements:
            install_requirements(py, source)
        verify(py)
    except Exception as exc:  # noqa: BLE001
        log(f"\n[FAIL] {exc}")
        return 1

    log("")
    log(f"[ok] ComfyUI installed at {target}")
    log(f"[ok] interpreter: {py}")
    log("[info] next: .\\scripts\\10-start-comfyui.ps1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
