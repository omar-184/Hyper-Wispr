"""Ties everything together and shows the tray icon."""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

from . import ai_cleanup, cleanup, config, context, hotkeys, media, medical, overlay, paste, settings_ui, uia
from .audio import Recorder, chime, rms
from .history import History
from .learn import Learner

log = logging.getLogger("hyper_wispr")

COLORS = {"loading": "#9e9e9e", "idle": "#3f7cff", "recording": "#e53935", "working": "#ffb300", "error": "#616161"}
LABELS = {
    "loading": "Loading speech model…",
    "idle": "Ready",
    "recording": "Listening…",
    "working": "Transcribing…",
    "error": "Speech model didn't load",
}


def process_text(raw: str, settings: dict[str, Any], app_type: str, before: str = "", after: str = "") -> str:
    """Raw transcript -> text to paste. Kept separate from the app so it can be tested.
    before/after: the text around the cursor, when it could be read ("" otherwise)."""
    snippet = cleanup.match_snippet(raw, settings.get("snippets", {}))
    if snippet is not None:
        return snippet
    text = cleanup.clean(raw, settings)
    if not text:
        return ""
    text = medical.fix(text, settings)
    text = cleanup.apply_style(text, ai_cleanup.style_for(settings, app_type))
    text = ai_cleanup.refine(text, settings, app_type, before)
    return cleanup.fit_to_context(text, before, after)[0]


HOUR = 3600


class App:
    def __init__(self, open_window: bool = False) -> None:
        self.open_window = open_window
        self.settings_problem = ""
        try:
            self.settings = config.load()
        except (OSError, ValueError) as e:
            # A typo from editing settings.json by hand must not stop the app from starting.
            log.warning("settings.json could not be read: %s", e)
            self.settings = copy.deepcopy(config.DEFAULTS)
            self.settings_problem = "Your settings file couldn't be read, so Hyper-Wispr started with the default settings."
            if isinstance(e, ValueError):
                try:
                    backup = config.recover()
                    self.settings_problem += f" The old file was kept as {backup.name}."
                except OSError:
                    log.exception("Could not set the broken settings file aside")
        self.recorder = Recorder(self.settings)
        self.history = _open_history()
        self.transcriber = None
        self.load_error = ""
        self.jobs: queue.Queue = queue.Queue()
        self.target: context.AppContext | None = None
        self.status = "loading"
        self.icon = None
        # Hotkey callbacks run on the keyboard-hook thread and must return immediately,
        # so they only queue an action; the controller thread does the actual work.
        self.actions: queue.Queue = queue.Queue()
        self.recording = False
        self.pending = 0  # recordings waiting to be transcribed and typed
        self._status_lock = threading.Lock()
        self._worker_started = False
        self.session = 0
        self.last_text = ""
        self.focus: uia.Focus = uia.Focus()  # the text box when the current recording started
        self.download: dict[str, Any] | None = None  # speech model download in progress
        self._mic_warned: str | None = None
        self.settings_window: subprocess.Popen | None = None
        self.overlay = overlay.Overlay(
            level=self.recorder.level,
            position=self.settings["overlay"].get("position", "bottom"),
            enabled=self.settings["overlay"].get("enabled", True),
            hands_free=lambda: self.machine.state == hotkeys.HANDS_FREE,
        )
        self._settings_mtime = _mtime(config.settings_path())
        self._model_lock = threading.Lock()
        self.machine = hotkeys.HotkeyStateMachine(
            self.settings["push_to_talk_keys"],
            self.settings.get("hands_free_tap_seconds", 0.35),
            on_start=lambda: self.actions.put(self.start_recording),
            on_stop=lambda: self.actions.put(self.stop_recording),
            on_cancel=lambda: self.actions.put(self.cancel_recording),
            on_combo=self._on_combo,
            command_key=self._command_key(),
            paste_last=self.settings.get("paste_last_keys") or [],
            on_paste_last=lambda: self.actions.put(self.paste_last),
            medical_keys=self._medical_keys(),
            on_medical=lambda: self.actions.put(self.toggle_medical),
        )
        self.learner = Learner(self._learned_word, self._known_words)

    def _command_key(self) -> str | None:
        cfg = self.settings.get("command_mode") or {}
        return cfg.get("key") if cfg.get("enabled", True) else None

    def _medical_keys(self) -> list[str]:
        return list((self.settings.get("medical") or {}).get("toggle_keys") or [])

    def _known_words(self) -> set[str]:
        return set(self.settings.get("dictionary", [])) | set(self.settings.get("dictionary_starred", []))

    def _learned_word(self, word: str, heard: str) -> None:
        try:
            self.history.suggest(word, heard)
        except sqlite3.Error:
            log.exception("Could not save a learned word")

    def _on_combo(self) -> None:
        # Not queued like the others: it has to happen while the keys are still down.
        if sys.platform == "win32" and hotkeys.uses_menu_key(self.machine.combo):
            try:
                hotkeys.mask_menu_key()
            except Exception:  # never let this stop the dictation itself
                log.exception("Could not keep the Start menu from opening")

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
            log.info("Pressed the hotkey before the speech model was ready")
            self.machine.reset()
            if self.load_error:
                self.notify("Speech model didn't load", self.load_error)
            else:
                self.notify("Still getting ready", "The speech model is loading. The icon turns blue when you can dictate.")
            return
        if self.recording:
            return
        if settings_ui.shortcut_capture_active():
            log.info("Ignoring the shortcut while the Settings window records a new one")
            self.machine.reset()
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
        wanted = self.settings["audio"].get("input_device")
        if self.recorder.fell_back and self._mic_warned != wanted:
            self._mic_warned = wanted  # once, not on every dictation
            self.notify("Using the default microphone", f"{str(wanted).split(',')[0]} isn't available, so the Windows default microphone is being used. You can pick another one in Settings.")
        with self._status_lock:
            self.recording = True
        self.session += 1
        chime("start", self.settings["audio"]["sounds"])
        self._settle_status()
        # While the microphone is already recording: nothing here may delay the first word.
        self.focus = uia.Focus()
        if self.settings.get("context_awareness", True) or self.machine.command:
            self.focus = uia.focus()
        if self.settings["audio"].get("mute_media", True):
            media.mute_others()
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
        audio = self.recorder.stop()
        chime("stop", self.settings["audio"]["sounds"])
        seconds = len(audio) / self.recorder.rate
        level = rms(audio)
        log.info("Recorded %.1fs from '%s', level %.4f", seconds, self.recorder.device_name(), level)
        media.unmute()
        keep = seconds >= self.settings["audio"]["min_seconds"]
        threshold = self.settings["audio"].get("silence_threshold", 0.003)
        if self.settings["audio"].get("whisper_mode"):
            threshold /= 4  # speaking under your breath is much quieter
        if keep and level < threshold:
            keep = False
            self.notify("Didn't hear anything", f"The microphone '{self.recorder.device_name()}' picked up only silence. Check it's the right one and not muted.")
        with self._status_lock:
            self.recording = False
            if keep:
                self.pending += 1
                self.jobs.put((audio, seconds, self.target or context.AppContext(), time.time(), self.machine.command, self.focus))
        self._settle_status()

    def cancel_recording(self) -> None:
        if not self.recording:
            return
        self.recorder.stop()
        media.unmute()
        chime("error", self.settings["audio"]["sounds"])
        with self._status_lock:
            self.recording = False
        self._settle_status()

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
            audio, seconds, target, t0, is_command, focus = self.jobs.get()
            try:
                if is_command:
                    self.run_command(audio, target, focus)
                else:
                    self.dictate(audio, seconds, target, t0, focus)
                if getattr(self.transcriber, "gpu_failed", False) and not getattr(self, "_gpu_warned", False):
                    self._gpu_warned = True
                    self.notify("Using the processor", "The graphics card stopped working for dictation, so the processor "
                                "took over (a little slower). Quit and reopen Hyper-Wispr to try the card again.")
            except Exception:
                log.exception("Dictation failed")
                chime("error", self.settings["audio"]["sounds"])
                self.notify("Dictation failed", "Something went wrong while transcribing. The details are in the log (tray menu, Open log folder).")
            finally:
                with self._status_lock:
                    self.pending -= 1
                self._settle_status()

    def dictate(self, audio, seconds: float, target: context.AppContext, t0: float, focus: uia.Focus | None = None) -> None:
        raw = self.transcriber.transcribe(audio)
        focus = focus or uia.Focus()
        use_context = self.settings.get("context_awareness", True) and not focus.password
        before, after = (focus.before, focus.after) if use_context else ("", "")
        try:
            final = process_text(raw, self.settings, target.app_type, before, after)
        except Exception:
            # The words are still worth having: type them as heard rather than lose them.
            log.exception("Cleaning up the text failed; typing the transcript as heard")
            final = raw.strip()
        latency = time.time() - t0
        # No dictated text in the log: it can contain patient information.
        log.info("[%s/%s] %.1fs audio, %.2fs latency, %d characters typed", target.process, target.app_type, seconds, latency, len(final))
        try:
            if self.settings["history"]["enabled"]:
                # Before pasting, so the text survives a paste that goes wrong.
                self.history.add(target.process, target.app_type, seconds, raw, final, latency,
                                 audio=audio, keep_recordings=int(self.settings["history"].get("keep_recordings", 0) or 0))
            elif final:
                self.history.count_usage(seconds, final)  # for the dashboard's totals only
        except sqlite3.Error:
            log.exception("Could not save the dictation to history")
        if not final:
            self.notify("No speech recognized", "Nothing was typed. Try speaking a little louder or closer to the microphone.")
            return
        self.last_text = final
        self.wait_for_keys_released()
        self.put_text(final, cleanup.space_after(after), learn=True)

    def put_text(self, text: str, space_after: bool = True, learn: bool = False) -> None:
        """Paste at the cursor, unless nothing that takes text has the focus: then the text goes on
        the clipboard (and stays in History) instead of being lost."""
        now = context.current()
        if now.is_shell or uia.focus(before_chars=0, after_chars=0).editable is False:
            log.info("No text box has the focus (%s); copying instead of pasting", now.process)
            try:
                paste.copy(text)
                self.notify("No text box selected", "Your dictation wasn't pasted anywhere, so it's on the clipboard. Click where it should go and press Ctrl+V.")
            except Exception:
                log.exception("Copying the dictation failed")
                self.notify("No text box selected", "Your dictation wasn't pasted anywhere. It's saved in History.")
            return
        try:
            paste.insert(text, self.settings, now.process, space_after=space_after)
        except Exception:
            log.exception("Typing the text failed")
            chime("error", self.settings["audio"]["sounds"])
            self.notify("Couldn't type the text", "Choose \"Copy last dictation\" in the tray menu, then paste it with Ctrl+V.")
            return
        if learn and self.settings.get("learn_corrections", True):
            self.learner.watch(text)

    def run_command(self, audio, target: context.AppContext, focus: uia.Focus) -> None:
        """Command mode: the recording is an instruction for the selected text."""
        instruction = cleanup.clean(self.transcriber.transcribe(audio), self.settings)
        if not instruction:
            self.notify("No instruction heard", "Hold the keys, say what to do with the selected text, then let go.")
            return
        # If the command can't run, what was said isn't lost: "paste last" types it.
        again = " + ".join(hotkeys.display_name(k) for k in self.settings.get("paste_last_keys") or [])
        again = f" To type what you said instead, press {again}." if again else ""
        if not self.settings["ai_cleanup"].get("enabled"):
            self.last_text = instruction
            self.notify("Command mode needs AI cleanup", "Turn on AI cleanup in Settings (it uses Ollama) to edit text by voice." + again)
            return
        self.wait_for_keys_released()
        selected = focus.selected if not focus.password else ""
        if not selected.strip():
            selected = paste.copy_selection(target.process)
        if not selected.strip():
            self.last_text = instruction
            self.notify("Select some text first", "Command mode changes the text you have selected. Select it, then hold the keys plus Alt and say what to do." + again)
            return
        log.info("Command mode: %d characters selected", len(selected))
        try:
            result = ai_cleanup.command(instruction, selected, self.settings)
        except ai_cleanup.CommandError as e:
            chime("error", self.settings["audio"]["sounds"])
            self.notify("Command didn't work", str(e))
            return
        self.last_text = result
        # The selection is still there, so pasting replaces it.
        self.put_text(result, space_after=False)

    def paste_last(self) -> None:
        text = self.last_text
        if not text:
            try:
                text = self.history.last() or ""
            except sqlite3.Error:
                log.exception("Could not read the history")
        if not text:
            self.notify("Nothing to paste yet", "Your last dictation will be available here.")
            return
        self.wait_for_keys_released()
        self.put_text(text)

    def wait_for_keys_released(self, timeout: float = 3.0) -> None:
        """Pasting while Ctrl/Win are still held would send the wrong shortcut (e.g. Win+Ctrl+V)."""
        deadline = time.time() + timeout
        while hotkeys.modifiers_held(self.machine) and time.time() < deadline:
            time.sleep(0.02)

    def load_models(self) -> None:
        from .speech import Transcriber

        if self.icon is not None:
            self.icon.title = "Hyper-Wispr: loading speech model (the first start downloads it, which can take a few minutes)"
        try:
            transcriber = Transcriber(self.settings, progress=self._download_progress, fetch_gpu=True)
        except Exception as e:
            log.exception("Could not load the speech model")
            self.load_error = f"{e}. Check your internet connection for the first download, then restart Hyper-Wispr."
            self._settle_status()
            self.notify("Speech model failed to load", self.load_error)
            return
        self.load_error = ""
        self.download = None
        self.transcriber = transcriber
        if not self._worker_started:
            self._worker_started = True
            threading.Thread(target=self.worker, daemon=True).start()
        self._settle_status()
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

    def _download_progress(self, done: int, total: int, what: str = "model") -> None:
        """Called while the speech model (what="model") or the NVIDIA files ("gpu") download for the first time."""
        self.download = {"done": done, "total": total, "what": what}
        self._settle_status()
        if self.icon is not None:
            self.icon.title = "Hyper-Wispr: " + _download_label(self.download)

    def housekeeping(self) -> None:
        """Every few hours: auto-delete old history."""
        while True:
            try:
                days = float(self.settings["history"].get("keep_days", 0) or 0)
                if n := self.history.delete_older_than(days):
                    log.info("Deleted %d dictation(s) older than %g days", n, days)
            except Exception:
                log.exception("Deleting old history failed")
            time.sleep(6 * HOUR)

    # ---- tray ------------------------------------------------------------
    def _settle_status(self) -> None:
        """Show what is happening right now. Worked out from the state every time rather than set
        directly, because the threads overlap: a dictation that finishes typing must not hide the
        "Listening" pill of the next one, which may already be recording."""
        with self._status_lock:
            if self.recording:
                status = "recording"
            elif self.pending:
                status = "working"
            elif self.transcriber is not None:
                status = "idle"
            else:
                status = "error" if self.load_error else "loading"
            self.set_status(status)

    def set_status(self, status: str) -> None:
        if status != self.status or status in ("idle", "error"):
            self._publish_status(status)
        self.status = status
        self.overlay.show({"recording": overlay.RECORDING, "working": overlay.WORKING}.get(status, overlay.HIDDEN))
        if self.icon is not None:
            self.icon.icon = _make_icon(COLORS[status])
            self.icon.title = f"Hyper-Wispr: {LABELS[status]}"

    def _publish_status(self, status: str) -> None:
        """status.json tells the window's dashboard what the tray app is doing."""
        info = {
            "status": status,
            "label": _download_label(self.download) if status == "loading" and self.download else LABELS[status],
            "model": getattr(self.transcriber, "description", "") or "",
            "error": self.load_error,
            # First start: {"done": bytes, "total": bytes (0 = unknown)} while the speech model downloads.
            "download": self.download if status == "loading" else None,
            "pid": os.getpid(),
            "time": time.time(),
        }
        try:
            path = config.app_dir() / "status.json"
            tmp = path.with_name(f"status.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(info), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # only the dashboard misses out

    def open_settings(self, *_: Any) -> None:
        """Show the Hyper-Wispr window. It is a separate process so it can't freeze dictation."""
        if settings_ui.focus_window():
            return  # already open, maybe behind other windows or minimized
        if self.settings_window is not None and self.settings_window.poll() is None:
            return  # still starting up
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--settings"]
        else:
            cmd = [sys.executable, "-m", "hyper_wispr", "--settings"]
        try:
            self.settings_window = subprocess.Popen(cmd)
        except OSError:
            log.exception("Could not open the window; opening settings.json instead")
            _open(config.settings_path())
            return
        threading.Thread(target=self._check_settings_window, args=(self.settings_window,), daemon=True).start()

    def _check_settings_window(self, proc: subprocess.Popen) -> None:
        """A window that can't start (e.g. a broken WebView2) would otherwise just never appear."""
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            return  # it's open
        if code != 0:
            log.error("The Settings window exited with code %s", code)
            self.notify("Hyper-Wispr couldn't open its window", "Opening the settings file instead. The details are in the log (tray menu, Open log folder).")
            _open(config.settings_path())

    def watch_settings(self) -> None:
        """Apply changes made in the Settings window (or by hand) as soon as the file changes."""
        path = config.settings_path()
        while True:
            time.sleep(1.0)
            mtime = _mtime(path)
            if mtime == self._settings_mtime:
                continue
            self._settings_mtime = mtime
            try:
                self.reload_settings()
            except ValueError as e:
                log.warning("settings.json has a mistake: %s", e)
                self.notify("Settings not applied", f"settings.json has a mistake ({e}). Hyper-Wispr keeps using the previous settings until it's fixed.")
            except Exception:
                log.exception("Could not apply the changed settings")

    def reload_settings(self, *_: Any) -> None:
        from .speech import needs_reload

        old_speech = dict(self.settings["speech"])
        # update() instead of clear()+update(): other threads read self.settings at any moment.
        self.settings.update(config.load())
        self.recorder.cfg = self.settings["audio"]
        self.machine.combo = self.settings["push_to_talk_keys"]
        self.machine.command_key = self._command_key()
        self.machine.paste_last = self.settings.get("paste_last_keys") or []
        self.machine.medical_keys = self._medical_keys()
        try:
            self.recorder.open_standby()
        except Exception:
            log.exception("Could not keep the microphone open")
        self.machine.tap_seconds = self.settings.get("hands_free_tap_seconds", 0.35)
        self.overlay.enabled = self.settings["overlay"].get("enabled", True)
        self.overlay.position = self.settings["overlay"].get("position", "bottom")
        log.info("Settings reloaded")
        # Languages, beam size, the dictionary and so on are read at every dictation; only a
        # different model or device needs loading (which briefly holds two models in memory).
        speech = self.settings["speech"]
        if self.transcriber is not None:
            reload = needs_reload(self.transcriber.load_settings, speech)
        else:
            reload = speech != old_speech  # retry a failed first load with the new choice
        if reload:
            threading.Thread(target=self.reload_model, daemon=True).start()

    def reload_model(self) -> None:
        """Load the newly chosen speech model in the background; the old one keeps working meanwhile."""
        from .speech import Transcriber, needs_reload

        with self._model_lock:
            if self.transcriber is None:
                self.load_models()  # the first load failed or is still running; this retries it
                return
            if not needs_reload(self.transcriber.load_settings, self.settings["speech"]):
                return  # an earlier reload already loaded it
            log.info("Speech settings changed; loading the new model")
            try:
                new = Transcriber(self.settings, fetch_gpu=True)
            except Exception as e:
                log.exception("Could not load the new speech model")
                self.notify("Couldn't switch speech model", f"{e}. Still using {self.transcriber.description}.")
                return
            self.transcriber = new
            log.info("Now using speech model %s", new.description)
            self.notify("Speech model changed", f"Now using {new.description}.")

    def copy_last(self, *_: Any) -> None:
        """Clicking the tray menu takes the focus away from your app, so pasting from there would
        land nowhere. Copying lets you paste it wherever you want."""
        text = self.last_text
        if not text:
            try:
                text = self.history.last() or ""  # e.g. after a restart
            except sqlite3.Error:
                log.exception("Could not read the history")
        if not text:
            self.notify("Nothing to copy yet", "Your last dictation will be available here.")
            return
        try:
            paste.copy(text)
        except Exception:
            log.exception("Copying the last dictation failed")
            self.notify("Couldn't copy", "The clipboard is busy. Try again in a moment.")
            return
        self.notify("Copied", "Your last dictation is on the clipboard. Paste it with Ctrl+V.")

    def toggle_whisper(self, *_: Any) -> None:
        enabled = not self.settings["audio"].get("whisper_mode")
        self.settings["audio"]["whisper_mode"] = enabled
        try:
            config.update({"audio.whisper_mode": enabled})
        except Exception:
            log.exception("Could not save the whisper mode setting")

    def toggle_medical(self, *_: Any) -> None:
        """Medical dictation on/off, from its shortcut or the tray menu."""
        if settings_ui.shortcut_capture_active():
            return  # the window is recording a new shortcut, maybe this one
        enabled = not medical.enabled(self.settings)
        self.settings.setdefault("medical", {})["enabled"] = enabled
        try:
            config.update({"medical.enabled": enabled})
        except Exception:
            log.exception("Could not save the medical dictation setting")
        log.info("Medical dictation %s", "on" if enabled else "off")
        if enabled:
            self.notify("Medical dictation on", "Drug names, doses and units are written the medical way, using the medical dictionary.")
        else:
            self.notify("Medical dictation off", "Dictation is back to everyday writing. Turn it on again with the same shortcut.")

    def toggle_ai(self, *_: Any) -> None:
        enabled = not self.settings["ai_cleanup"]["enabled"]
        self.settings["ai_cleanup"]["enabled"] = enabled  # the menu tick changes right away
        try:
            # Only this one setting, so changes saved meanwhile by the Settings window are kept.
            config.update({"ai_cleanup.enabled": enabled})
        except Exception:
            log.exception("Could not save the AI cleanup setting")

    def run(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem(lambda _: LABELS[self.status], None, enabled=False),
            # default=True: clicking the tray icon itself opens the window.
            pystray.MenuItem("Open Hyper-Wispr", self.open_settings, default=True),
            pystray.MenuItem("Copy last dictation", self.copy_last),
            pystray.MenuItem("Medical dictation", self.toggle_medical, checked=lambda _: medical.enabled(self.settings)),
            pystray.MenuItem("AI cleanup", self.toggle_ai, checked=lambda _: self.settings["ai_cleanup"]["enabled"]),
            pystray.MenuItem("Whisper mode", self.toggle_whisper, checked=lambda _: bool(self.settings["audio"].get("whisper_mode"))),
            pystray.MenuItem("Open log folder", lambda *_: _open(config.app_dir())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Hyper-Wispr", lambda icon, _: icon.stop()),
        )
        self.icon = pystray.Icon("hyper-wispr", _make_icon(COLORS["loading"]), "Hyper-Wispr: loading", menu)
        self.icon.run(setup=self._start_background)

    def _start_background(self, icon) -> None:
        # Runs once the tray icon exists, so status updates and notifications have somewhere to go.
        icon.visible = True
        if self.settings_problem:
            self.notify("Settings were reset", self.settings_problem)
        threading.Thread(target=self.controller, daemon=True).start()
        threading.Thread(target=self.watch_settings, daemon=True).start()
        threading.Thread(target=self.housekeeping, daemon=True).start()
        media.recover()  # music a crash left muted
        try:
            self.recorder.open_standby()
        except Exception:
            log.exception("Could not keep the microphone open")
        self._publish_status(self.status)
        if self.open_window:
            self.open_settings()
        threading.Thread(target=settings_ui.refresh_startup_link, daemon=True).start()
        hotkeys.listen(self.machine)
        with self._model_lock:  # a speech-settings change during the first load waits for it
            self.load_models()


def _download_label(download: dict[str, Any]) -> str:
    done, total = download.get("done", 0), download.get("total", 0)
    what = "the NVIDIA speed-up files" if download.get("what") == "gpu" else "the speech model"
    if total:
        return f"Downloading {what}: {min(99, done * 100 // total)}% of {total / 1e9:.1f} GB"
    return f"Downloading {what}: {done / 1e6:.0f} MB so far"


def _open_history() -> History:
    path = config.app_dir() / "history.sqlite3"
    try:
        return History(path)
    except sqlite3.DatabaseError:
        # A damaged history file must not stop dictation: keep it aside and start a new one.
        log.exception("The history file is damaged; starting a new one")
        path.replace(path.with_name("history.broken.sqlite3"))
        return History(path)


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


def _mtime(path) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _open(path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", str(path)])
