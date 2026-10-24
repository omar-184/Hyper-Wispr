"""The NVIDIA speed-up files, downloaded only on PCs whose graphics card can use them.

There is one installer for everyone. On first start, Hyper-Wispr asks the NVIDIA driver which
card is in the PC. Only if that card can really run Whisper (float16/int8 support, i.e. GTX
10-series, MX150 or newer, with enough memory and a CUDA 12 driver) does it download NVIDIA's
CUDA libraries (cuBLAS and cuDNN, about 1.3 GB) from the official NVIDIA packages on PyPI.
Everyone else, including older cards like the GeForce MX130, stays on the processor and
downloads nothing extra.

Which packages to fetch, with their checksums, is fixed when the installer is built
(gpu_pack.json, written by packaging/gpu_manifest.py). The files go to
%LOCALAPPDATA%\\HyperWispr\\nvidia, outside the program folder, so app updates keep them.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MANIFEST = Path(__file__).with_name("gpu_pack.json")
# Only NVIDIA's packages on PyPI are ever downloaded.
DOWNLOAD_PREFIX = "https://files.pythonhosted.org/"
# The DLLs inside the wheels: nvidia/cublas/bin/*.dll and nvidia/cudnn/bin/*.dll.
_DLL = re.compile(r"^nvidia/[^/]+/bin/[^/]+\.dll$", re.IGNORECASE)
# Loaded first, by full path, so ctranslate2 finds them already in memory.
_PRELOAD = ["cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll"]

MIN_COMPUTE = (5, 3)  # float16, which CTranslate2 needs to use the card instead of float32
MIN_MEMORY = 1_900_000_000  # a "2 GB" card reports a little under 2^31 bytes
MIN_DRIVER = 12000  # CUDA 12


# ---- what card is in this PC ---------------------------------------------------


def nvidia_cards() -> list[dict[str, Any]]:
    """Asks the NVIDIA driver (nvcuda.dll) directly, so it works before any CUDA files are here.
    Each card: {"name", "compute": (major, minor), "memory": bytes, "driver": e.g. 12040}."""
    if sys.platform != "win32":
        return []
    try:
        cuda = ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return []  # no NVIDIA driver, so no NVIDIA card
    try:
        if cuda.cuInit(0) != 0:
            return []
        driver, count = ctypes.c_int(), ctypes.c_int()
        cuda.cuDriverGetVersion(ctypes.byref(driver))
        if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0:
            return []
        cards = []
        for i in range(count.value):
            dev, major, minor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            memory = ctypes.c_size_t()
            name = ctypes.create_string_buffer(128)
            if cuda.cuDeviceGet(ctypes.byref(dev), i) != 0:
                continue
            cuda.cuDeviceGetName(name, len(name), dev)
            cuda.cuDeviceGetAttribute(ctypes.byref(major), 75, dev)  # CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR
            cuda.cuDeviceGetAttribute(ctypes.byref(minor), 76, dev)  # ..._MINOR
            cuda.cuDeviceTotalMem_v2(ctypes.byref(memory), dev)
            cards.append({"name": name.value.decode(errors="replace"), "compute": (major.value, minor.value),
                          "memory": memory.value, "driver": driver.value})
        return cards
    except Exception:
        log.info("Couldn't ask the NVIDIA driver about the graphics card", exc_info=True)
        return []


def why_not(card: dict[str, Any]) -> str:
    """Empty if the card can run the speech model; otherwise the reason, in plain words."""
    if tuple(card["compute"]) < MIN_COMPUTE:
        return "too old to run the speech model (it can only do float32)"
    if card["memory"] < MIN_MEMORY:
        return "doesn't have enough graphics memory (needs 2 GB)"
    if card["driver"] < MIN_DRIVER:
        return "needs a newer NVIDIA driver (CUDA 12); update it from nvidia.com"
    return ""


def usable_card(cards: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for card in nvidia_cards() if cards is None else cards:
        if not why_not(card):
            return card
    return None


def describe(card: dict[str, Any]) -> str:
    major, minor = card["compute"]
    return f"{card['name']} (compute {major}.{minor}, {card['memory'] / 2**30:.1f} GB)"


# ---- the downloaded files --------------------------------------------------------


def manifest() -> dict[str, Any] | None:
    """{"id": "cuda12", "wheels": [{"name", "version", "url", "sha256", "size"}]}; None in a
    development copy, where the CUDA libraries come from `pip install .[gpu]` instead."""
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def root() -> Path:
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    return (Path(base) if base else Path.home() / ".cache") / "HyperWispr" / "nvidia"


def folder(m: dict[str, Any] | None = None) -> Path | None:
    """The folder with the DLLs if they're downloaded and complete, else None."""
    m = m or manifest()
    if not m:
        return None
    path = root() / m["id"]
    try:
        info = json.loads((path / "pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if all((path / dll).is_file() for dll in info.get("files", [])) and info.get("files"):
        return path
    return None


def is_frozen_windows() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def libraries_ready() -> bool:
    """Can the speech engine load its CUDA libraries? In the installed app only once they're
    downloaded; a development copy relies on `pip install .[gpu]`."""
    return not is_frozen_windows() or folder() is not None


_activated = False


def activate() -> None:
    """Make the downloaded DLLs findable by ctranslate2 (it loads cuBLAS/cuDNN on first GPU use)."""
    global _activated
    path = folder()
    if _activated or path is None:
        return
    os.add_dll_directory(str(path))
    os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
    for dll in _PRELOAD:
        try:
            ctypes.WinDLL(str(path / dll))
        except OSError as e:
            log.warning("Could not load %s: %s", dll, e)
    _activated = True


def wanted(speech_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The card to download the files for, or None when they aren't needed or can't help."""
    if speech_cfg.get("device", "auto") == "cpu" or not is_frozen_windows() or not manifest():
        return None
    cards = nvidia_cards()
    for card in cards:
        reason = why_not(card)
        log.info("NVIDIA graphics card: %s%s", describe(card), f": {reason}, so speech runs on the processor" if reason else "")
    if not cards:
        log.info("No NVIDIA graphics card found; speech runs on the processor")
    return usable_card(cards)


def ensure(speech_cfg: dict[str, Any], progress=None) -> bool:
    """Download the NVIDIA files the first time, if this PC's card can use them.
    progress(done_bytes, total_bytes, "gpu") is called while downloading.
    Never raises: on any problem the speech model runs on the processor this time and the
    download is tried again at the next start."""
    m = manifest()
    if m is None or folder(m) is not None:
        return folder(m) is not None
    card = wanted(speech_cfg)
    if card is None:
        return False
    try:
        _install(m, progress)
    except Exception as e:
        log.warning("Couldn't download the NVIDIA files (%s); using the processor for now, will try again next start", e)
        return False
    log.info("NVIDIA files ready for %s", describe(card))
    return True


def _install(m: dict[str, Any], progress) -> None:
    wheels = m["wheels"]
    total = sum(int(w.get("size") or 0) for w in wheels)
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    # Room for one wheel and everything unpacked so far, with some to spare.
    need = total * 2 + 500_000_000
    if shutil.disk_usage(base).free < need:
        raise OSError(f"not enough free disk space (needs about {need / 1e9:.1f} GB on {base.anchor})")
    for old in base.iterdir():  # files for an older CUDA version, or a half-finished unpack
        if old.name != m["id"] and old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
    target = base / m["id"]
    target.mkdir(exist_ok=True)
    done_before = 0
    files: list[str] = []
    for w in wheels:
        url = str(w["url"])
        if not url.startswith(DOWNLOAD_PREFIX):
            raise ValueError(f"refusing to download from {url}")
        wheel = base / (Path(url).name + ".part")
        log.info("Downloading %s %s (%.0f MB)", w["name"], w["version"], (w.get("size") or 0) / 1e6)

        def report(done: int, offset: int = done_before) -> None:
            if progress is not None:
                progress(offset + done, total, "gpu")

        _download(url, wheel, str(w["sha256"]), int(w.get("size") or 0), report)
        with zipfile.ZipFile(wheel) as z:
            for member in z.namelist():
                if _DLL.match(member):
                    name = Path(member).name
                    with z.open(member) as src, (target / name).open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    files.append(name)
        wheel.unlink(missing_ok=True)
        done_before += int(w.get("size") or 0)
    if not files:
        raise OSError("the NVIDIA packages had no libraries in them")
    (target / "pack.json").write_text(json.dumps({"files": files, "wheels": wheels}, indent=1), encoding="utf-8")
    if progress is not None:
        progress(total, total, "gpu")


def _download(url: str, path: Path, sha256: str, size: int, report) -> None:
    """Resumes a download interrupted at an earlier start, and checks the file's checksum."""
    digest = hashlib.sha256()
    have = path.stat().st_size if path.exists() else 0
    if size and have >= size:
        have = 0  # complete but failed the checksum last time, or a different file: start over
    if have:
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
    headers = {"User-Agent": "Hyper-Wispr"}
    if have:
        headers["Range"] = f"bytes={have}-"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        if have and resp.status != 206:  # the server ignored the resume request
            have, digest = 0, hashlib.sha256()
        with path.open("ab" if have else "wb") as f:
            done = have
            last = 0
            while chunk := resp.read(1024 * 1024):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if done - last >= 8 * 1024 * 1024:
                    report(done)
                    last = done
    report(done)
    if digest.hexdigest() != sha256.lower():
        path.unlink(missing_ok=True)
        raise OSError("the download was damaged (checksum mismatch)")
