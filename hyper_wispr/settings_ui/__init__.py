"""The Settings window: a small web page (web/) shown with pywebview.

It runs as its own process (`Hyper-Wispr.exe --settings`) so it never blocks
the tray icon or the keyboard hook. It edits settings.json directly; the
running app notices the file changed and reloads it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__, config

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
STARTUP_LINK = "Hyper-Wispr.lnk"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _startup_folder() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


class Api:
    """Every public method here can be called from JavaScript as window.pywebview.api.<name>()."""

    def __init__(self) -> None:
        self._mic_stream = None
        self._mic_level = 0.0
        self._mic_lock = threading.Lock()

    # ---- settings --------------------------------------------------------
    def get_state(self) -> dict[str, Any]:
        try:
            settings = config.load()
            error = ""
        except (OSError, ValueError) as e:
            # A hand-edited file with a typo: show defaults instead of crashing, and say so.
            log.warning("settings.json could not be read: %s", e)
            settings, error = copy.deepcopy(config.DEFAULTS), f"Your settings file could not be read ({e}). Showing defaults; changing anything will overwrite it."
        return {
            "settings": settings,
            "defaults": config.DEFAULTS,
            "version": __version__,
            "settings_path": str(config.settings_path()),
            "data_dir": str(config.app_dir()),
            "is_windows": sys.platform == "win32",
            "can_start_at_login": self._startup_target() is not None,
            "load_error": error,
        }

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        try:
            config.save(config.merge(config.DEFAULTS, settings))
            return {"ok": True}
        except Exception as e:  # shown in the window's status line
            log.exception("Saving settings failed")
            return {"ok": False, "error": str(e)}

    def reset_settings(self) -> dict[str, Any]:
        config.save(copy.deepcopy(config.DEFAULTS))
        return self.get_state()

    # ---- hardware --------------------------------------------------------
    def gpu_available(self) -> bool:
        from ..speech import cuda_available

        return cuda_available()

    def list_microphones(self) -> dict[str, Any]:
        """Input devices of the default sound system (MME on Windows), named so sounddevice
        finds exactly one match: "<device name>, <host API name>"."""
        try:
            import sounddevice as sd

            default_in = sd.default.device[0]
            devices = sd.query_devices()
            hostapi = devices[default_in]["hostapi"] if default_in is not None and default_in >= 0 else 0
            api_name = sd.query_hostapis(hostapi)["name"]
            mics = []
            for i, d in enumerate(devices):
                if d["hostapi"] != hostapi or d["max_input_channels"] <= 0:
                    continue
                # MME lists a "Microsoft Sound Mapper" pseudo-device; "Default" already covers it.
                if "sound mapper" in d["name"].lower():
                    continue
                mics.append({"id": f"{d['name']}, {api_name}", "name": d["name"], "is_default": i == default_in})
            return {"ok": True, "microphones": mics}
        except Exception as e:
            log.exception("Listing microphones failed")
            return {"ok": False, "error": str(e), "microphones": []}

    def start_mic_test(self, device: str | None) -> dict[str, Any]:
        self.stop_mic_test()
        try:
            import sounddevice as sd

            from ..audio import rms

            def callback(indata, frames, t, status) -> None:  # noqa: ARG001
                level = rms(indata[:, 0])
                with self._mic_lock:
                    # Fast attack, slow release: reads like a real VU meter.
                    self._mic_level = level if level > self._mic_level else self._mic_level * 0.85 + level * 0.15

            def open_stream(rate: int):
                return sd.InputStream(samplerate=rate, channels=1, dtype="float32", device=device or None, callback=callback)

            try:
                stream = open_stream(16000)
            except Exception:
                # Same fallback as the recorder: some drivers only accept their native rate.
                stream = open_stream(int(sd.query_devices(device or None, "input")["default_samplerate"]))
            stream.start()
            self._mic_stream = stream
            return {"ok": True}
        except Exception as e:
            log.warning("Microphone test failed: %s", e)
            return {"ok": False, "error": str(e)}

    def mic_level(self) -> float:
        with self._mic_lock:
            return self._mic_level

    def stop_mic_test(self) -> None:
        stream, self._mic_stream = self._mic_stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._mic_lock:
            self._mic_level = 0.0

    # ---- Ollama ----------------------------------------------------------
    def ollama_status(self, url: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2) as resp:
                models = sorted(m["name"] for m in json.loads(resp.read()).get("models", []))
            return {"running": True, "models": models}
        except Exception as e:
            return {"running": False, "models": [], "error": str(e)}

    # ---- history ---------------------------------------------------------
    def _history_db(self) -> sqlite3.Connection | None:
        path = config.app_dir() / "history.sqlite3"
        if not path.exists():
            return None
        return sqlite3.connect(path, timeout=5)

    def get_history(self, limit: int = 200) -> list[dict[str, Any]]:
        db = self._history_db()
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT id, created, app, seconds, final_text, raw_text FROM history ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            db.close()
        return [
            {"id": r[0], "created": r[1], "app": r[2] or "", "seconds": r[3] or 0, "text": r[4] or "", "raw": r[5] or ""}
            for r in rows
        ]

    def delete_history(self, item_id: int | None = None) -> bool:
        db = self._history_db()
        if db is None:
            return True
        try:
            if item_id is None:
                db.execute("DELETE FROM history")
            else:
                db.execute("DELETE FROM history WHERE id = ?", (int(item_id),))
            db.commit()
            return True
        except sqlite3.Error:
            log.exception("Deleting history failed")
            return False
        finally:
            db.close()

    def copy_text(self, text: str) -> bool:
        try:
            import pyperclip

            pyperclip.copy(text)
            return True
        except Exception:
            return False

    # ---- files & start at login -----------------------------------------
    def open_data_folder(self) -> None:
        _open(config.app_dir())

    def open_settings_file(self) -> None:
        _open(config.settings_path())

    def _startup_target(self) -> str | None:
        """Only the installed app can start itself at login; a dev checkout has no single exe."""
        if _startup_folder() is None or not getattr(sys, "frozen", False):
            return None
        return sys.executable

    def get_start_at_login(self) -> bool:
        folder = _startup_folder()
        return bool(folder and (folder / STARTUP_LINK).exists())

    def set_start_at_login(self, enabled: bool) -> bool:
        folder, target = _startup_folder(), self._startup_target()
        if folder is None or target is None:
            return False
        link = folder / STARTUP_LINK
        try:
            if not enabled:
                link.unlink(missing_ok=True)
                return True
            folder.mkdir(parents=True, exist_ok=True)
            # Paths go in as environment variables so quotes in them can't break the script.
            script = (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:HW_LINK);"
                "$s.TargetPath=$env:HW_TARGET;$s.WorkingDirectory=$env:HW_DIR;$s.Save()"
            )
            env = dict(os.environ, HW_LINK=str(link), HW_TARGET=target, HW_DIR=str(Path(target).parent))
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env, check=True, timeout=20, creationflags=_NO_WINDOW,
            )
            return link.exists()
        except Exception:
            log.exception("Changing start-at-login failed")
            return False

    def open_url(self, url: str) -> None:
        if url.startswith("https://"):
            import webbrowser

            webbrowser.open(url)

    def window_ready(self) -> None:
        log.info("Settings window loaded")


def _open(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", str(path)])


def _windows_dark_mode() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def run(self_test: bool = False) -> int:
    """Open the Settings window and block until it is closed.

    With self_test=True the window loads, checks that the page can call into
    Python, then closes itself; the exit code says whether that worked. The
    installer build runs this so a broken WebView2 bundle fails in CI.
    """
    import webview

    api = Api()
    window = webview.create_window(
        "Hyper-Wispr Settings",
        url=(WEB_DIR / "index.html").as_uri(),
        js_api=api,
        width=920,
        height=660,
        min_size=(720, 520),
        # Matches the page background so the window doesn't flash white in dark mode.
        background_color="#1c1c1e" if _windows_dark_mode() else "#f5f5f7",
    )
    result = {"ok": not self_test}

    def check() -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if window.evaluate_js("window.__hwReady === true"):
                    result["ok"] = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        log.info("Settings window self-test %s", "OK" if result["ok"] else "FAILED")
        window.destroy()

    window.events.closed += api.stop_mic_test
    webview.start(check if self_test else None)
    return 0 if result["ok"] else 1
