"""Ties everything together and shows the tray icon."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

from . import ai_cleanup, cleanup, config, context, hotkeys, paste
from .audio import Recorder, chime
from .history import History

log = logging.getLogger("hyper_wispr")

COLORS = {"loading": "#9e9e9e", "idle": "#3f7cff", "recording": "#e53935", "working": "#ffb300"}


def process_text(raw: str, settings: dict[str, Any], app_type: str) -> str:
    """Raw transcript -> text to paste. Kept separate from the app so it can be tested."""
    snippet = cleanup.match_snippet(raw, settings.get("snippets", {}))
    if snippet is not None:
        return snippet
    text = cleanup.clean(raw, settings)
    if not text:
        return ""
    return ai_cleanup.refine(text, settings, app_type)


class App:
    def __init__(self) -> None:
        self.settings = config.load()
        self.recorder = Recorder(self.settings)
        self.history = History(config.app_dir() / "history.sqlite3")
        self.transcriber = None
        self.jobs: queue.Queue = queue.Queue()
        self.target: context.AppContext | None = None
        self.status = "loading"
        self.icon = None
        self.machine = hotkeys.HotkeyStateMachine(
            self.settings["push_to_talk_keys"],
            self.settings.get("hands_free_extra_key"),
            on_start=self.start_recording,
            on_stop=self.stop_recording,
            on_cancel=self.cancel_recording,
        )

    # ---- recording -------------------------------------------------------
    def start_recording(self) -> None:
        if self.transcriber is None:
            log.info("Still loading the speech model; try again in a moment")
            chime("error", self.settings["audio"]["sounds"])
            self.machine.state = hotkeys.IDLE
            return
        # Remember the app now: by the time we paste, focus is what matters.
        self.target = context.current()
        self.recorder.start()
        chime("start", self.settings["audio"]["sounds"])
        self.set_status("recording")

    def stop_recording(self) -> None:
        audio = self.recorder.stop()
        chime("stop", self.settings["audio"]["sounds"])
        seconds = len(audio) / self.recorder.rate
        if seconds < self.settings["audio"]["min_seconds"]:
            self.set_status("idle")
            return
        self.set_status("working")
        self.jobs.put((audio, seconds, self.target or context.AppContext(), time.time()))

    def cancel_recording(self) -> None:
        self.recorder.stop()
        chime("error", self.settings["audio"]["sounds"])
        self.set_status("idle")

    # ---- worker ----------------------------------------------------------
    def worker(self) -> None:
        while True:
            audio, seconds, target, t0 = self.jobs.get()
            try:
                raw = self.transcriber.transcribe(audio)
                final = process_text(raw, self.settings, target.app_type)
                if final:
                    self.wait_for_keys_released()
                    paste.insert(final, self.settings)
                latency = time.time() - t0
                log.info("[%s/%s] %.1fs audio, %.2fs latency: %s", target.process, target.app_type, seconds, latency, final)
                if self.settings["history"]["enabled"]:
                    self.history.add(target.process, target.app_type, seconds, raw, final, latency)
            except Exception:
                log.exception("Dictation failed")
                chime("error", self.settings["audio"]["sounds"])
            finally:
                if self.jobs.empty():
                    self.set_status("idle")

    def wait_for_keys_released(self, timeout: float = 3.0) -> None:
        """Pasting while Ctrl/Win are still held would send the wrong shortcut (e.g. Win+Ctrl+V)."""
        deadline = time.time() + timeout
        while self.machine.pressed and time.time() < deadline:
            time.sleep(0.02)

    def load_models(self) -> None:
        from .speech import Transcriber

        try:
            self.transcriber = Transcriber(self.settings)
        except Exception:
            log.exception("Could not load the speech model")
            self.set_status("loading")
            return
        threading.Thread(target=self.worker, daemon=True).start()
        self.set_status("idle")
        log.info("Ready: speech model %s. Hold %s to dictate.", self.transcriber.description, "+".join(self.settings["push_to_talk_keys"]))
        if self.settings["ai_cleanup"]["enabled"]:
            if ai_cleanup.is_available(self.settings):
                ai_cleanup.warm_up(self.settings)
                log.info("AI cleanup ready with Ollama model %s", self.settings["ai_cleanup"]["model"])
            else:
                log.warning("Ollama is not running; AI cleanup will be skipped until it is.")

    # ---- tray ------------------------------------------------------------
    def set_status(self, status: str) -> None:
        self.status = status
        if self.icon is not None:
            self.icon.icon = _make_icon(COLORS[status])
            self.icon.title = f"Hyper-Wispr: {status}"

    def open_settings(self, *_: Any) -> None:
        _open(config.settings_path())

    def reload_settings(self, *_: Any) -> None:
        self.settings.clear()
        self.settings.update(config.load())
        self.machine.combo = self.settings["push_to_talk_keys"]
        self.machine.hands_free_key = self.settings.get("hands_free_extra_key")
        log.info("Settings reloaded (restart to change the speech model)")

    def paste_last(self, *_: Any) -> None:
        last = self.history.last()
        if last:
            paste.insert(last, self.settings)

    def toggle_ai(self, *_: Any) -> None:
        cfg = self.settings["ai_cleanup"]
        cfg["enabled"] = not cfg["enabled"]
        config.save(self.settings)

    def run(self) -> None:
        import pystray

        hotkeys.listen(self.machine)
        threading.Thread(target=self.load_models, daemon=True).start()
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: f"Status: {self.status}", None, enabled=False),
            pystray.MenuItem("Paste last dictation", self.paste_last),
            pystray.MenuItem("AI cleanup (Ollama)", self.toggle_ai, checked=lambda _: self.settings["ai_cleanup"]["enabled"]),
            pystray.MenuItem("Open settings", self.open_settings),
            pystray.MenuItem("Reload settings", self.reload_settings),
            pystray.MenuItem("Open log folder", lambda *_: _open(config.app_dir())),
            pystray.MenuItem("Quit", lambda icon, _: icon.stop()),
        )
        self.icon = pystray.Icon("hyper-wispr", _make_icon(COLORS["loading"]), "Hyper-Wispr: loading", menu)
        self.icon.run()


def _make_icon(color: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=color)
    # A simple microphone shape.
    draw.rounded_rectangle((25, 14, 39, 38), radius=7, fill="white")
    draw.arc((19, 24, 45, 46), start=0, end=180, fill="white", width=3)
    draw.line((32, 46, 32, 52), fill="white", width=3)
    return img


def _open(path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", str(path)])
