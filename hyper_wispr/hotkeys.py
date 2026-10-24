"""Global hotkeys.

- Hold the push-to-talk keys (default Ctrl+Win): record while held, paste on release.
- Tap them quickly instead: hands-free mode keeps recording after you let go.
  Tap (or hold) them again to finish.
- Esc while recording: cancel without pasting.
- Press the command key (default Alt) too, before or while recording: command mode, which
  rewrites the selected text as you say instead of pasting what you said.
- The "paste last" keys (default Ctrl+Alt+Z): paste the last dictation again.
- The medical keys (default Ctrl+Alt+M): turn medical dictation on or off.

Key events arrive on the Windows keyboard-hook thread, which must return within a few
hundred milliseconds or Windows starts dropping keystrokes. So the state machine only
decides what happened; the app runs the slow work (opening the mic, transcribing) elsewhere.
"""

from __future__ import annotations

import logging
import sys
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
        is_down: Callable[[str], bool | None] = lambda _name: None,
        on_combo: Callable[[], None] = lambda: None,
        command_key: str | None = None,
        paste_last: list[str] | None = None,
        on_paste_last: Callable[[], None] = lambda: None,
        medical_keys: list[str] | None = None,
        on_medical: Callable[[], None] = lambda: None,
    ):
        self.combo = combo
        self.tap_seconds = tap_seconds
        self.on_start, self.on_stop, self.on_cancel = on_start, on_stop, on_cancel
        # Called every time the whole combination goes down, whatever happens next.
        self.on_combo = on_combo
        self.clock = clock
        # Asks the keyboard whether a key is physically down (None = can't tell); see listen().
        self.is_down = is_down
        self.command_key = command_key
        # Set when this recording is a command (the command key was down at some point).
        self.command = False
        self.paste_last = paste_last or []
        self.on_paste_last = on_paste_last
        self.medical_keys = medical_keys or []
        self.on_medical = on_medical
        self.pressed: set[str] = set()
        self.state = IDLE
        self._down_at = 0.0

    def _combo_down(self) -> bool:
        return bool(self.combo) and all(any(key_matches(w, p) for p in self.pressed) for w in self.combo)

    def _in_combo(self, name: str) -> bool:
        return any(key_matches(w, name) for w in self.combo)

    def _is_command_key(self, name: str) -> bool:
        return bool(self.command_key) and not self._in_combo(name) and key_matches(self.command_key, name)

    def _shortcut_down(self, keys: list[str], name: str) -> bool:
        """Did pressing `name` complete this shortcut?"""
        if not keys or not any(key_matches(w, name) for w in keys):
            return False
        return all(any(key_matches(w, p) for p in self.pressed) for w in keys)

    def reset(self) -> None:
        """Back to idle without callbacks (e.g. the app refused to start recording)."""
        self.state = IDLE

    def _forget_released(self, current: str) -> None:
        """Drop keys whose key-up never arrived. Windows doesn't send it when the screen locks
        (Win+L) or an administrator window has focus, and a "stuck" Win key would otherwise make
        every Ctrl press (Ctrl+C, Ctrl+V...) start a dictation."""
        for name in list(self.pressed):
            if name != current and self.is_down(name) is False:
                self.pressed.discard(name)

    def key_down(self, name: str) -> None:
        self._forget_released(name)
        if self.state == HOLDING and not self._combo_down():
            # The keys went up while Windows hid it from us: finish as if we had seen it.
            self.state = IDLE
            self.on_stop()
        if name in self.pressed:  # auto-repeat
            return
        self.pressed.add(name)
        if name == "esc" and self.state in (HOLDING, HANDS_FREE):
            self.state = IDLE
            self.on_cancel()
            return
        if self._is_command_key(name) and self.state in (HOLDING, HANDS_FREE):
            self.command = True
            self.on_combo()  # Alt on its own would select the app's menu bar when let go
            return
        if self.state == IDLE and self._shortcut_down(self.paste_last, name):
            self.on_paste_last()
            return
        if self.state == IDLE and self._shortcut_down(self.medical_keys, name):
            self.on_medical()
            return
        if not self._in_combo(name) or not self._combo_down():
            return
        self.on_combo()
        if self.state in (IDLE, STOPPING):  # STOPPING here means a key-up was missed
            self.state = HOLDING
            self._down_at = self.clock()
            self.command = any(self._is_command_key(p) for p in self.pressed)
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

    if sys.platform == "win32":
        machine.is_down = _win_is_down

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


# ---- asking Windows which keys are really down ------------------------------

_user32 = None
_vk_cache: dict[str, int | None] = {}
# Shift, Ctrl, Alt, left and right Windows key.
_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)


def _win_user32():
    global _user32
    if _user32 is None:
        import ctypes

        user32 = ctypes.WinDLL("user32")
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
        user32.VkKeyScanW.restype = ctypes.c_short
        _user32 = user32
    return _user32


def _vk(name: str) -> int | None:
    """pynput key name ("ctrl_l", "cmd", "a", "vk186") -> Windows virtual-key code."""
    if name not in _vk_cache:
        vk = None
        try:
            from pynput.keyboard import Key

            member = Key.__members__.get(name)
            if member is not None:
                vk = member.value.vk
            elif name.startswith("vk") and name[2:].isdigit():
                vk = int(name[2:])
            elif len(name) == 1:
                code = _win_user32().VkKeyScanW(name)
                vk = code & 0xFF if code != -1 else None
        except Exception:
            vk = None
        _vk_cache[name] = vk
    return _vk_cache[name]


def _win_is_down(name: str) -> bool | None:
    vk = _vk(name)
    if vk is None:
        return None
    return bool(_win_user32().GetAsyncKeyState(vk) & 0x8000)


def uses_menu_key(combo: list[str]) -> bool:
    """Does the combination include Win or Alt, which open the Start menu or a menu bar when let go?"""
    return any(k.startswith(("cmd", "alt")) for k in combo)


_controller = None


def mask_menu_key() -> None:
    """Press and release a key that does nothing (vkE8, "unassigned") while Win/Alt are down.

    Windows opens the Start menu when Win goes down and up with nothing typed in between, and Alt
    on its own selects the menu bar. Either takes the focus away from the app we're about to paste
    into. Ctrl is usually pressed before Win, so Ctrl+Win alone would still open Start. This is
    the same trick as AutoHotkey's "menu mask key"."""
    from pynput.keyboard import Controller, KeyCode

    global _controller
    if _controller is None:
        _controller = Controller()
    key = KeyCode.from_vk(0xE8)
    _controller.press(key)
    _controller.release(key)


def modifiers_held(machine: HotkeyStateMachine) -> bool:
    """Is Ctrl, Alt, Shift or Win still held? Pasting then would send the wrong shortcut."""
    if sys.platform == "win32":
        try:
            user32 = _win_user32()
            return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS)
        except Exception:
            pass
    return bool(machine.pressed)
