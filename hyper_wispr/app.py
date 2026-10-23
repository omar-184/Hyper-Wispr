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
from .audio import Recorder, chime, rms
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
        # Hotkey callbacks run on the keyboard-hook thread and must return immediately,
        # so they only queue an action; the controller thread does the actual work.
        self.actions: queue.Queue = queue.Queue()
        self.recording = False
        self.session = 0
        self.machine = hotkeys.HotkeyStateMachine(
            self.settings["push_to_talk_keys"],
            self.settings.get("hands_free_tap_seconds", 0.35),
            on_start=lambda: self.actions.put(self.start_recording),
            on_stop=lambda: self.actions.put(self.stop_recording),
            on_cancel=lambda: self.actions.put(self.cancel_recording),
        )

    def controller(self) -> None:
        while True:
            action = self.actions.get()
            try:
                action()
            except Exception:
                log.exception("Hotkey action failed")

    # ---- recording -------------------------------------------------------
    def start_recording(self) -> None:
        if self.transcriber is None:
            log.info("Pressed the hotkey while the speech model is still loading")
            self.machine.reset()
            self.notify("Still getting ready", "The speech model is loading. The icon turns blue when you can dictate.")
            return
        if self.recording:
            return
        # Remember the app now: by the time we paste, focus is what matters.
        self.target = context.current()
        try:
            self.recorder.start()
        except Exception as e:
            log.exception("Could not open the microphone")
            self.machine.reset()
            chime("error", self.settings["audio"]["sounds"])
            self.notify("Microphone problem", f"Couldn't open the microphone: {e}")
            return
        self.recording = True
        self.session += 1
        chime("start", self.settings["audio"]["sounds"])
        self.set_status("recording")
        # Safety net for a forgotten hands-free recording.
        session = self.session
        limit = self.settings["audio"].get("max_seconds", 360)
        timer = threading.Timer(limit, lambda: self.actions.put(lambda: self._auto_stop(session)))
        timer.daemon = True
        timer.start()

    def _auto_stop(self, session: int) -> None:
        if self.recording and self.session == session:
            log.info("Maximum recording length reached")
            self.machine.reset()
            self.stop_recording()

    def stop_recording(self) -> None:
        if not self.recording:
            return
        self.recording = False
        audio = self.recorder.stop()
        chime("stop", self.settings["audio"]["sounds"])
        seconds = len(audio) / self.recorder.rate
        level = rms(audio)
        log.info("Recorded %.1fs from '%s', level %.4f", seconds, self.recorder.device_name(), level)
        if seconds < self.settings["audio"]["min_seconds"]:
            self.set_status("idle")
            return
        if level < self.settings["audio"].get("silence_threshold", 0.003):
            self.set_status("idle")
            self.notify("Didn't hear anything", f"The microphone '{self.recorder.device_name()}' picked up only silence. Check it's the right one and not muted.")
            return
        self.set_status("working")
        self.jobs.put((audio, seconds, self.target or context.AppContext(), time.time()))

    def cancel_recording(self) -> None:
        if not self.recording:
            return
        self.recording = False
        self.recorder.stop()
        chime("error", self.settings["audio"]["sounds"])
        self.set_status("idle")

    def notify(self, title: str, message: str) -> None:
        log.info("Notice: %s: %s", title, message)
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

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
                else:
                    self.notify("No speech recognized", "Nothing was typed. Try speaking a little louder or closer to the microphone.")
                latency = time.time() - t0
                # No dictated text in the log: it can contain patient information.
                log.info("[%s/%s] %.1fs audio, %.2fs latency, %d characters typed", target.process, target.app_type, seconds, latency, len(final))
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

        if self.icon is not None:
            self.icon.title = "Hyper-Wispr: loading speech model (the first start downloads it, which can take a few minutes)"
        try:
            self.transcriber = Transcriber(self.settings)
        except Exception as e:
            log.exception("Could not load the speech model")
            self.set_status("loading")
            self.notify("Speech model failed to load", f"{e}. Check your internet connection for the first download, then restart Hyper-Wispr.")
            return
        threading.Thread(target=self.worker, daemon=True).start()
        self.set_status("idle")
        keys = " + ".join(hotkeys.display_name(k) for k in self.settings["push_to_talk_keys"])
        log.info("Ready: speech model %s, microphone '%s'", self.transcriber.description, self.recorder.device_name())
        self.notify("Hyper-Wispr is ready", f"Hold {keys} and speak, or tap it for hands-free.")
        if self.settings["ai_cleanup"]["enabled"]:
            if not ai_cleanup.is_available(self.settings):
                ai_cleanup.try_start_ollama(self.settings)
            if ai_cleanup.is_available(self.settings):
                try:
                    ai_cleanup.ensure_model(self.settings)
                except Exception as e:
                    log.warning("Could not download the Ollama model: %s", e)
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
        self.machine.tap_seconds = self.settings.get("hands_free_tap_seconds", 0.35)
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
        self.icon.run(setup=self._start_background)

    def _start_background(self, icon) -> None:
        # Runs once the tray icon exists, so status updates and notifications have somewhere to go.
        icon.visible = True
        threading.Thread(target=self.controller, daemon=True).start()
        hotkeys.listen(self.machine)
        self.load_models()


def _make_icon(color: str, size: int = 64):
    from PIL import Image, ImageDraw

    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4 * k, 4 * k, 60 * k, 60 * k), fill=color)
    # A simple microphone shape.
    draw.rounded_rectangle((25 * k, 14 * k, 39 * k, 38 * k), radius=7 * k, fill="white")
    draw.arc((19 * k, 24 * k, 45 * k, 46 * k), start=0, end=180, fill="white", width=max(1, round(3 * k)))
    draw.line((32 * k, 46 * k, 32 * k, 52 * k), fill="white", width=max(1, round(3 * k)))
    return img


def _open(path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", str(path)])
