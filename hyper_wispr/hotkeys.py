"""Global hotkeys.

- Hold the push-to-talk keys (default Ctrl+Win): record while held, paste on release.
- While holding them, also tap the hands-free key (default Space): keeps recording
  after you let go. Press the push-to-talk keys again to finish.
- Esc while recording: cancel without pasting.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

IDLE, HOLDING, HANDS_FREE = "idle", "holding", "hands_free"
_GENERIC = ("ctrl", "alt", "shift", "cmd")


def key_matches(wanted: str, pressed: str) -> bool:
    """'ctrl' matches ctrl_l and ctrl_r; 'cmd_l' matches the plain 'cmd' that Windows reports."""
    if wanted == pressed:
        return True
    if wanted in _GENERIC and pressed.startswith(wanted + "_"):
        return True
    return wanted.endswith("_l") and pressed == wanted[:-2]


class HotkeyStateMachine:
    """Pure logic, no keyboard access, so it can be unit-tested."""

    def __init__(
        self,
        combo: list[str],
        hands_free_key: str | None,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_cancel: Callable[[], None],
    ):
        self.combo = combo
        self.hands_free_key = hands_free_key
        self.on_start, self.on_stop, self.on_cancel = on_start, on_stop, on_cancel
        self.pressed: set[str] = set()
        self.state = IDLE

    def _combo_down(self) -> bool:
        return all(any(key_matches(w, p) for p in self.pressed) for w in self.combo)

    def _in_combo(self, name: str) -> bool:
        return any(key_matches(w, name) for w in self.combo)

    def key_down(self, name: str) -> None:
        if name in self.pressed:  # auto-repeat
            return
        self.pressed.add(name)
        if name == "esc" and self.state != IDLE:
            self.state = IDLE
            self.on_cancel()
        elif self.state == IDLE and self._combo_down():
            self.state = HOLDING
            self.on_start()
        elif self.state == HOLDING and self.hands_free_key and key_matches(self.hands_free_key, name):
            self.state = HANDS_FREE
            log.info("Hands-free mode on")
        elif self.state == HANDS_FREE and self._in_combo(name) and self._combo_down():
            self.state = IDLE
            self.on_stop()

    def key_up(self, name: str) -> None:
        self.pressed.discard(name)
        if self.state == HOLDING and self._in_combo(name):
            self.state = IDLE
            self.on_stop()


def _name(key) -> str:
    name = getattr(key, "name", None)
    if name:
        return name
    char = getattr(key, "char", None)
    return char.lower() if char else str(key)


def listen(machine: HotkeyStateMachine):
    """Start the global keyboard listener in a background thread and return it."""
    from pynput import keyboard

    listener = keyboard.Listener(
        on_press=lambda k: machine.key_down(_name(k)),
        on_release=lambda k: machine.key_up(_name(k)),
    )
    listener.daemon = True
    listener.start()
    return listener
