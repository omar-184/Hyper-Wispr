"""The Hyper-Wispr window: dashboard, history and settings, a web page (web/) shown with pywebview.

It runs as its own process (`Hyper-Wispr.exe --settings`) so it never blocks
the tray icon or the keyboard hook. It edits settings.json directly; the
running app notices the file changed and reloads it, and tells the window
what it is doing through status.json.
"""

from __future__ import annotations

import copy
import datetime
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

from .. import __version__, config, updates

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
STARTUP_LINK = "Hyper-Wispr.lnk"
WINDOW_TITLE = "Hyper-Wispr"
# Held while the window is open, so a second one brings the first to the front instead.
WINDOW_MUTEX = "Local\\HyperWisprSettingsWindow"
# Held while the window records a new shortcut, so the tray app doesn't start dictating meanwhile.
CAPTURE_MUTEX = "Local\\HyperWisprShortcutCapture"
# Held by the running tray app (see __main__.acquire_single_instance).
TRAY_MUTEX = "Local\\HyperWisprTrayApp"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
# Typing speed used for "time saved": a typical typist manages about 40 words a minute.
TYPING_WPM = 40


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
        self._capture = None
        self._download = updates.Downloader()
        self._window = None  # private: pywebview would expose a public attribute to the page
        self._db = None
        self._redo = None  # (model name, Transcriber) for "Redo", loaded on first use
        self._redo_lock = threading.Lock()

    # ---- settings --------------------------------------------------------
    def get_state(self) -> dict[str, Any]:
        try:
            settings = config.load()
            error = ""
        except (OSError, ValueError) as e:
            # A hand-edited file with a typo: show defaults instead of crashing, and say so.
            log.warning("settings.json could not be read: %s", e)
            settings, error = copy.deepcopy(config.DEFAULTS), f"Your settings file could not be read ({e}). Showing the defaults. Changing anything starts a new file; the old one is kept as settings.broken.json."
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

    def save_settings(self, settings: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
        """base: the settings as the window last loaded or saved them. Only what changed since is
        written, so a change made meanwhile (the tray menu's AI cleanup switch) isn't undone."""
        try:
            if base is None:
                config.save(config.merge(config.DEFAULTS, settings))
            elif changes := config.diff(base, settings):
                config.update(changes)
            return {"ok": True}
        except Exception as e:  # shown in the window's status line
            log.exception("Saving settings failed")
            return {"ok": False, "error": str(e)}

    def reset_settings(self) -> dict[str, Any]:
        config.save(copy.deepcopy(config.DEFAULTS))
        return self.get_state()

    # ---- hardware --------------------------------------------------------
    def gpu_available(self) -> bool:
        from ..speech import usable_gpu

        return usable_gpu()

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

    # ---- history, notes, learned words ------------------------------------
    def _history(self):
        from ..history import History

        if self._db is None:
            self._db = History(config.app_dir() / "history.sqlite3")
        return self._db

    def get_history(self, limit: int = 200, search: str = "") -> list[dict[str, Any]]:
        try:
            return self._history().recent(int(limit), search or "")
        except (sqlite3.Error, OSError):
            log.exception("Reading history failed")
            return []

    def delete_history(self, item_id: int | None = None) -> bool:
        try:
            self._history().delete(None if item_id is None else int(item_id))
            return True
        except sqlite3.Error:
            log.exception("Deleting history failed")
            return False

    def redo_dictation(self, item_id: int) -> dict[str, Any]:
        """Transcribe a kept recording again with the bigger "redo" model. Runs in this window's
        process, so dictation carries on meanwhile."""
        from ..app import process_text
        from ..speech import Transcriber

        try:
            history = self._history()
            item = history.get(int(item_id))
            audio = history.recording(int(item_id))
            if item is None or audio is None:
                return {"ok": False, "error": "The recording of this dictation is no longer kept."}
            settings = config.load()
            model = settings["history"].get("redo_model") or "large-v3-turbo"
            with self._redo_lock:
                if self._redo is None or self._redo[0] != model:
                    self._redo = (model, Transcriber(settings, model_override=model))
                transcriber = self._redo[1]
                transcriber.settings = settings
                raw = transcriber.transcribe(audio)
            text = process_text(raw, settings, item["app_type"] or "other")
            if text:
                history.update_text(int(item_id), text)
            return {"ok": True, "text": text}
        except Exception as e:
            log.exception("Redoing a dictation failed")
            return {"ok": False, "error": str(e)}

    def get_notes(self) -> list[dict[str, Any]]:
        try:
            return self._history().notes()
        except sqlite3.Error:
            log.exception("Reading notes failed")
            return []

    def save_note(self, body: str, note_id: int | None = None) -> dict[str, Any]:
        try:
            return {"ok": True, "id": self._history().save_note(body, None if note_id is None else int(note_id))}
        except sqlite3.Error as e:
            log.exception("Saving a note failed")
            return {"ok": False, "error": str(e)}

    def delete_note(self, note_id: int) -> bool:
        try:
            self._history().delete_note(int(note_id))
            return True
        except sqlite3.Error:
            log.exception("Deleting a note failed")
            return False

    def get_suggestions(self) -> list[dict[str, Any]]:
        try:
            return self._history().suggestions()
        except sqlite3.Error:
            return []

    def answer_suggestion(self, word: str, add: bool) -> bool:
        """Add a learned word to the dictionary, or turn it down. Either way it isn't offered again."""
        try:
            if add:
                settings = config.load()
                words = settings.get("dictionary", [])
                if word.lower() not in (w.lower() for w in words):
                    config.update({"dictionary": [*words, word]})
            self._history().dismiss_suggestion(word)
            return True
        except Exception:
            log.exception("Answering a word suggestion failed")
            return False

    def copy_text(self, text: str) -> bool:
        try:
            from ..paste import copy as copy_to_clipboard

            copy_to_clipboard(text)  # kept out of clipboard history, like dictations
            return True
        except Exception:
            log.exception("Copying failed")
            return False

    # ---- dashboard ---------------------------------------------------------
    def get_status(self) -> dict[str, Any]:
        """What the tray app is doing, from the status.json it writes."""
        running = tray_running()
        try:
            info = json.loads((config.app_dir() / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if running is False:
            return {"running": False, "status": "stopped", "label": "Not running"}
        return {"running": True, "status": info.get("status", "loading"), "label": info.get("label", "Starting…"),
                "model": info.get("model", ""), "error": info.get("error", ""), "download": info.get("download")}

    def get_stats(self, days: int = 14) -> dict[str, Any]:
        """Totals and a day-by-day count of words for the dashboard, worked out from the history."""
        empty = {"dictations": 0, "words": 0, "seconds": 0.0, "week_words": 0, "wpm": 0, "minutes_saved": 0, "streak": 0, "today_words": 0,
                 "daily": [{"day": d, "words": 0} for d in _last_days(days)]}
        try:
            return stats_from_daily(self._history().daily(), days)
        except (sqlite3.Error, OSError):
            log.exception("Reading usage totals failed")
            return empty

    def start_app(self) -> bool:
        """Start the tray app when it isn't running (e.g. after "Quit" from the tray)."""
        cmd = [sys.executable, "--minimized"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "hyper_wispr", "--minimized"]
        try:
            subprocess.Popen(cmd, creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
            return True
        except OSError:
            log.exception("Could not start Hyper-Wispr")
            return False

    # ---- updates -----------------------------------------------------------
    def update_info(self) -> dict[str, Any]:
        """The last check's result (if any), without going online."""
        return {"last": updates.last_check(), "current": __version__, "can_install": sys.platform == "win32" and getattr(sys, "frozen", False)}

    def check_update(self) -> dict[str, Any]:
        return updates.check()

    def download_update(self, asset: dict[str, Any]) -> dict[str, Any]:
        return self._download.start(asset)

    def update_progress(self) -> dict[str, Any]:
        return self._download.progress()

    def cancel_update(self) -> None:
        self._download.cancel()

    def install_update(self) -> dict[str, Any]:
        state = self._download.progress()
        if state.get("state") != "ready":
            return {"ok": False, "error": "Download the update first."}
        result = updates.install(state["path"])
        if result["ok"] and self._window is not None:
            # The installer closes the app anyway; going now avoids a half-closed window.
            threading.Timer(1.0, self._window.destroy).start()
        return result

    # ---- shortcut recorder -------------------------------------------------
    def begin_shortcut_capture(self) -> None:
        if self._capture is None:
            self._capture = _create_mutex(CAPTURE_MUTEX)[0]

    def end_shortcut_capture(self) -> None:
        handle, self._capture = self._capture, None
        _close(handle)

    def mask_menu_key(self) -> None:
        """Win or Alt went down in the recorder: stop the Start menu (or menu bar) opening on release."""
        if sys.platform == "win32":
            from ..hotkeys import mask_menu_key

            mask_menu_key()

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
                "$s.TargetPath=$env:HW_TARGET;$s.Arguments='--minimized';$s.WorkingDirectory=$env:HW_DIR;$s.Save()"
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


def _last_days(days: int) -> list[str]:
    today = datetime.date.today()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def compute_stats(rows: list[tuple], days: int = 14) -> dict[str, Any]:
    """rows: (created, seconds, final_text). Days are local calendar days."""
    per_day: dict[str, int] = {}
    words = dictations = 0
    seconds = 0.0
    for created, secs, text in rows:
        n = len((text or "").split())
        if not n:
            continue
        dictations += 1
        words += n
        seconds += secs or 0.0
        day = datetime.date.fromtimestamp(created).isoformat()
        per_day[day] = per_day.get(day, 0) + n
    return _stats(per_day, dictations, words, seconds, days)


def stats_from_daily(daily: list[dict[str, Any]], days: int = 14) -> dict[str, Any]:
    """The same, from the daily totals in history.sqlite3 (kept when old dictations are deleted)."""
    per_day = {d["day"]: d["words"] for d in daily if d["words"]}
    return _stats(per_day, sum(d["dictations"] for d in daily), sum(d["words"] for d in daily),
                  sum(d["seconds"] for d in daily), days)


def _stats(per_day: dict[str, int], dictations: int, words: int, seconds: float, days: int) -> dict[str, Any]:
    recent = _last_days(7)
    # Days in a row with at least one dictation; nothing yet today doesn't break yesterday's streak.
    day = datetime.date.today()
    if not per_day.get(day.isoformat()):
        day -= datetime.timedelta(days=1)
    streak = 0
    while per_day.get(day.isoformat()):
        streak += 1
        day -= datetime.timedelta(days=1)
    typing_minutes = words / TYPING_WPM
    return {
        "dictations": dictations,
        "words": words,
        "seconds": round(seconds, 1),
        "today_words": per_day.get(recent[-1], 0),
        "week_words": sum(per_day.get(d, 0) for d in recent[-7:]),
        "wpm": round(words / (seconds / 60)) if seconds >= 1 else 0,
        "minutes_saved": max(0, round(typing_minutes - seconds / 60)),
        "streak": streak,
        "daily": [{"day": d, "words": per_day.get(d, 0)} for d in _last_days(days)],
    }


def refresh_startup_link() -> None:
    """Shortcuts made by Settings before 0.4 open the window at every login; add --minimized once."""
    marker = config.app_dir() / "startup-link-v2"
    if marker.exists() or not getattr(sys, "frozen", False):
        return
    api = Api()
    try:
        if api.get_start_at_login():
            api.set_start_at_login(True)
        marker.touch()
    except Exception:
        log.exception("Could not update the start-at-login shortcut")


def _open(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", str(path)])


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _create_mutex(name: str) -> tuple[int | None, bool]:
    """(handle, whether it already existed). The handle closes with the process."""
    if sys.platform != "win32":
        return None, False
    import ctypes

    handle = _kernel32().CreateMutexW(None, False, name)
    ERROR_ALREADY_EXISTS = 183
    return handle, ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def _close(handle: int | None) -> None:
    if handle:
        _kernel32().CloseHandle(handle)


def _mutex_exists(name: str) -> bool:
    SYNCHRONIZE = 0x00100000
    handle = _kernel32().OpenMutexW(SYNCHRONIZE, False, name)
    _close(handle)
    return bool(handle)


def shortcut_capture_active() -> bool:
    """Is the window recording a new shortcut right now?"""
    return sys.platform == "win32" and _mutex_exists(CAPTURE_MUTEX)


def tray_running() -> bool | None:
    """Is the tray app running? None when that can't be told (not Windows)."""
    return _mutex_exists(TRAY_MUTEX) if sys.platform == "win32" else None


def focus_window(wait: float = 0.0) -> bool:
    """Bring an open Settings window to the front. False when there is none (after `wait` seconds)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    for fn in (user32.IsIconic, user32.SetForegroundWindow):
        fn.argtypes, fn.restype = (wintypes.HWND,), wintypes.BOOL
    user32.ShowWindow.argtypes, user32.ShowWindow.restype = (wintypes.HWND, ctypes.c_int), wintypes.BOOL
    user32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowExW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))

    def ours() -> int | None:
        # Only our own window: an Explorer window showing a folder called "Hyper-Wispr" has the same title.
        try:
            pid = int((config.app_dir() / "window.pid").read_text())
        except (OSError, ValueError):
            return None
        hwnd = None
        while hwnd := user32.FindWindowExW(None, hwnd, None, WINDOW_TITLE):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                return hwnd
        return None

    deadline = time.monotonic() + wait
    while not (hwnd := ours()):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)  # the other window is still starting up
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


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
    """Open the Hyper-Wispr window and block until it is closed.

    With self_test=True the window loads, checks that the page can call into
    Python, then closes itself; the exit code says whether that worked. The
    installer build runs this so a broken WebView2 bundle fails in CI.
    """
    if not self_test:
        _lock, already_open = _create_mutex(WINDOW_MUTEX)  # held until this window closes
        if already_open:
            focus_window(wait=5.0)
            return 0
        try:
            (config.app_dir() / "window.pid").write_text(str(os.getpid()))
        except OSError:
            pass

    import webview

    api = Api()
    window = webview.create_window(
        WINDOW_TITLE,
        url=(WEB_DIR / "index.html").as_uri(),
        js_api=api,
        width=1240,
        height=820,
        min_size=(760, 560),
        # Matches the page background so the window doesn't flash white in dark mode.
        background_color="#1c1c1e" if _windows_dark_mode() else "#f5f5f7",
    )
    api._window = window
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
    window.events.closed += api.end_shortcut_capture
    webview.start(check if self_test else None)
    return 0 if result["ok"] else 1
