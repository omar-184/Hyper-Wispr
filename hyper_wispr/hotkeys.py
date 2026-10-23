"""Global hotkeys.

- Hold the push-to-talk keys (default Ctrl+Win): record while held, paste on release.
- Tap them quickly instead: hands-free mode keeps recording after you let go.
  Tap (or hold) them again to finish.
- Esc while recording: cancel without pasting.

Key events arrive on the Windows keyboard-hook thread, which must return within a few
hundred milliseconds or Windows starts dropping keystrokes. So the state machine only
decides what happened; the app runs the slow work (opening the mic, transcribing) elsewhere.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

IDLE, HOLDING, HANDS_FREE, STOPPING = "idle", "holding", "hands_free", "stopping"
_GENERIC = ("ctrl", "alt", "shift", "cmd")


def key_matches(wanted: str, pressed: str) -> bool:
    """'ctrl' matches ctrl_l and ctrl_r; 'cmd_l' matches the plain 'cmd' that Windows reports."""
    if wanted == pressed:
        return True
    if wanted in _GENERIC and pressed.startswith(wanted + "_"):
        return True
    return wanted.endswith("_l") and pressed == wanted[:-2]


_DISPLAY = {"ctrl": "Ctrl", "cmd": "Win", "alt": "Alt", "shift": "Shift", "space": "Space", "esc": "Esc"}


def display_name(key: str) -> str:
    """'ctrl_l' -> 'Ctrl', 'cmd_r' -> 'Right Win', 'f8' -> 'F8'."""
    base, _, side = key.partition("_")
    name = _DISPLAY.get(base, key.upper() if len(key) <= 3 else key.capitalize())
    return f"Right {name}" if side == "r" and base in _DISPLAY else name


class HotkeyStateMachine:
    """Pure logic, no keyboard access, so it can be unit-tested."""

    def __init__(
        self,
        combo: list[str],
        tap_seconds: float,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_cancel: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ):
        self.combo = combo
        self.tap_seconds = tap_seconds
        self.on_start, self.on_stop, self.on_cancel = on_start, on_stop, on_cancel
        self.clock = clock
        self.pressed: set[str] = set()
        self.state = IDLE
        self._down_at = 0.0

    def _combo_down(self) -> bool:
        return bool(self.combo) and all(any(key_matches(w, p) for p in self.pressed) for w in self.combo)

    def _in_combo(self, name: str) -> bool:
        return any(key_matches(w, name) for w in self.combo)

    def reset(self) -> None:
        """Back to idle without callbacks (e.g. the app refused to start recording)."""
        self.state = IDLE

    def key_down(self, name: str) -> None:
        if name in self.pressed:  # auto-repeat
            return
        self.pressed.add(name)
        if name == "esc" and self.state in (HOLDING, HANDS_FREE):
            self.state = IDLE
            self.on_cancel()
        elif not self._in_combo(name) or not self._combo_down():
            return
        elif self.state in (IDLE, STOPPING):  # STOPPING here means a key-up was missed
            self.state = HOLDING
            self._down_at = self.clock()
            self.on_start()
        elif self.state == HANDS_FREE:
            # Wait for the keys to go up so the release doesn't start a new recording.
            self.state = STOPPING
            self.on_stop()

    def key_up(self, name: str) -> None:
        self.pressed.discard(name)
        if not self._in_combo(name):
            return
        if self.state == HOLDING:
            if self.clock() - self._down_at < self.tap_seconds:
                self.state = HANDS_FREE
                log.info("Hands-free mode on")
            else:
                self.state = IDLE
                self.on_stop()
        elif self.state == STOPPING and not any(self._in_combo(p) for p in self.pressed):
            self.state = IDLE


def _name(key) -> str:
    name = getattr(key, "name", None)
    if name:
        return name
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    vk = getattr(key, "vk", None)
    return f"vk{vk}" if vk is not None else str(key)


def listen(machine: HotkeyStateMachine):
    """Start the global keyboard listener in a background thread and return it."""
    from pynput import keyboard

    def safe(fn):
        def handler(key):
            try:
                fn(_name(key))
            except Exception:
                log.exception("Hotkey handler failed")

        return handler

    listener = keyboard.Listener(on_press=safe(machine.key_down), on_release=safe(machine.key_up))
    listener.daemon = True
    listener.start()
    return listener
