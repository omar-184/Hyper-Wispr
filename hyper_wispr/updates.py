"""Checking GitHub for a newer Hyper-Wispr and installing it.

Only the version check and the installer download go online; nothing about you or your
dictations is sent. The installer is the same one on the Releases page, run with /SILENT so
it only shows a progress bar, closes the running app and starts the new version.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, config

log = logging.getLogger(__name__)

REPO = "omar-184/Hyper-Wispr"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
# Installers are only ever downloaded from this project's own releases.
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def parse_version(text: str) -> tuple[int, ...]:
    """"v0.3.1" -> (0, 3, 1). Anything after the numbers ("-test") is ignored."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def is_newer(latest: str, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    width = max(len(a), len(b))
    return bool(a) and a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def installed_variant() -> str:
    """"GPU" for the NVIDIA build (it bundles the CUDA libraries), otherwise "CPU"."""
    base = getattr(sys, "_MEIPASS", None)
    if base and any((Path(base) / "ctranslate2").glob("cublas*.dll")):
        return "GPU"
    return "CPU"


def pick_asset(assets: list[dict[str, Any]], variant: str) -> dict[str, Any] | None:
    for a in assets:
        name = a.get("name", "")
        if not (name.startswith("Hyper-Wispr-Setup-") and name.endswith(".exe")):
            continue
        if name.endswith("-NVIDIA.exe") == (variant == "GPU"):
            return a
    return None


def _cache_path() -> Path:
    return config.app_dir() / "update-check.json"


def last_check() -> dict[str, Any] | None:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check(timeout: float = 8.0) -> dict[str, Any]:
    """Ask GitHub for the latest release. Never raises: problems come back as {"ok": False}."""
    try:
        req = urllib.request.Request(LATEST_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": f"Hyper-Wispr/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            release = json.loads(resp.read())
    except Exception as e:
        log.info("Update check failed: %s", e)
        return {"ok": False, "error": _friendly(e), "current": __version__}
    latest = str(release.get("tag_name", "")).lstrip("v")
    asset = pick_asset(release.get("assets", []), installed_variant())
    result = {
        "ok": True,
        "current": __version__,
        "latest": latest,
        "available": is_newer(latest) and asset is not None,
        "notes": str(release.get("body") or "")[:4000],
        "page": release.get("html_url") or RELEASES_PAGE,
        "published": release.get("published_at", ""),
        "asset": {"name": asset["name"], "url": asset["browser_download_url"], "size": asset.get("size", 0)} if asset else None,
        "checked": time.time(),
    }
    try:
        _cache_path().write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    if not result["available"]:
        _remove_old_downloads()
    return result


def _friendly(e: Exception) -> str:
    text = str(e)
    if "403" in text or "rate limit" in text.lower():
        return "GitHub is busy right now. Try again in an hour."
    if "timed out" in text or "urlopen error" in text or "getaddrinfo" in text:
        return "Couldn't reach GitHub. Check your internet connection."
    return text


def _download_dir() -> Path:
    path = config.app_dir() / "updates"
    path.mkdir(exist_ok=True)
    return path


def _remove_old_downloads() -> None:
    for f in _download_dir().glob("Hyper-Wispr-Setup-*"):
        try:
            f.unlink()
        except OSError:
            pass


class Downloader:
    """Downloads one installer in the background; the window polls progress()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"state": "idle"}
        self._cancel = False

    def progress(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **state: Any) -> None:
        with self._lock:
            self._state = state

    def start(self, asset: dict[str, Any]) -> dict[str, Any]:
        url, name = str(asset.get("url", "")), Path(str(asset.get("name", ""))).name
        if not url.startswith(DOWNLOAD_PREFIX) or not name.startswith("Hyper-Wispr-Setup-") or not name.endswith(".exe"):
            return {"ok": False, "error": "That download isn't a Hyper-Wispr release."}
        if self.progress()["state"] == "downloading":
            return {"ok": True}
        self._cancel = False
        self._set(state="downloading", done=0, total=int(asset.get("size") or 0))
        threading.Thread(target=self._run, args=(url, name, int(asset.get("size") or 0)), daemon=True).start()
        return {"ok": True}

    def cancel(self) -> None:
        self._cancel = True

    def _run(self, url: str, name: str, size: int) -> None:
        target = _download_dir() / name
        part = target.with_suffix(".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"Hyper-Wispr/{__version__}"})
            with urllib.request.urlopen(req, timeout=30) as resp, part.open("wb") as f:
                total = int(resp.headers.get("Content-Length") or size or 0)
                done = 0
                while chunk := resp.read(256 * 1024):
                    if self._cancel:
                        raise InterruptedError
                    f.write(chunk)
                    done += len(chunk)
                    self._set(state="downloading", done=done, total=total)
            if size and part.stat().st_size != size:
                raise OSError("the download was incomplete")
            part.replace(target)
            self._set(state="ready", path=str(target), done=done, total=total)
            log.info("Downloaded update %s", name)
        except InterruptedError:
            part.unlink(missing_ok=True)
            self._set(state="idle")
        except Exception as e:
            log.warning("Update download failed: %s", e)
            part.unlink(missing_ok=True)
            self._set(state="error", error=_friendly(e))


def install(path: str) -> dict[str, Any]:
    """Start the downloaded installer. It closes Hyper-Wispr, installs, and starts the new version."""
    p = Path(path)
    if p.parent != _download_dir() or not p.name.startswith("Hyper-Wispr-Setup-") or not p.exists():
        return {"ok": False, "error": "The downloaded installer is missing. Download it again."}
    if sys.platform != "win32":
        return {"ok": False, "error": "Updates install on Windows only."}
    try:
        subprocess.Popen([str(p), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], creationflags=_DETACHED, close_fds=True)
    except OSError as e:
        log.exception("Could not start the installer")
        return {"ok": False, "error": str(e)}
    log.info("Started the installer %s", p.name)
    return {"ok": True}
