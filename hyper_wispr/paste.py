"""Put text where the cursor is.

Default method: copy to the clipboard, press Ctrl+V, then put back whatever
was on the clipboard before. This works in nearly every Windows app.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# Remote and virtual desktop windows (Remote Desktop, Citrix, VMware Horizon...). The remote side
# fetches the clipboard whenever it gets round to it, so putting the old clipboard back could
# paste that instead of the dictation, into a record. For these we leave the dictation there.
REMOTE_APPS = {
    "mstsc.exe", "msrdc.exe", "wfica32.exe", "cdviewer.exe", "vmware-view.exe", "vmware-remotemks.exe",
    "vmconnect.exe", "anydesk.exe", "teamviewer.exe", "rustdesk.exe", "vncviewer.exe",
}
TERMINALS = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wsl.exe", "mintty.exe", "putty.exe"}
# How long the app we pasted into gets to read the clipboard before we put the old one back.
RESTORE_DELAY = 1.0

_keyboard = None
_lock = threading.Lock()
# (what was on the clipboard before, clipboard sequence number after our copy) until it's put back.
_pending: tuple[list, int] | None = None


def _kb():
    global _keyboard
    if _keyboard is None:
        from pynput.keyboard import Controller

        _keyboard = Controller()
    return _keyboard


def _paste_shortcut() -> None:
    from pynput.keyboard import Key

    kb = _kb()
    mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
    with kb.pressed(mod):
        kb.tap("v")


def insert(text: str, settings: dict[str, Any], app: str = "", space_after: bool = True) -> None:
    """Type text into the focused window. app: the exe name we're pasting into, if known.
    space_after: False when the text around the cursor shows a space would be one too many."""
    cfg = settings["paste"]
    if space_after and cfg.get("add_trailing_space", True) and text and not text.endswith(("\n", " ")):
        text += " "
    if cfg.get("method") == "type":
        _kb().type(text)
        return
    restore = cfg.get("restore_clipboard", True) and app.lower() not in REMOTE_APPS
    if sys.platform == "win32":
        _insert_windows(text, restore)
    else:
        _insert_pyperclip(text, restore)


def copy(text: str) -> None:
    """Put text on the clipboard for the user to paste themselves."""
    global _pending
    if sys.platform == "win32":
        from . import clipboard

        with _lock:
            _pending = None  # what the user copies on purpose is never "restored" away
            clipboard.set_text(text)
    else:
        import pyperclip

        pyperclip.copy(text)


def copy_selection(app: str = "") -> str:
    """The selected text, read by pressing Ctrl+C (for apps UI Automation can't read). The
    clipboard is put back afterwards. "" when nothing is selected."""
    if sys.platform != "win32" or app.lower() in TERMINALS:
        return ""  # in a terminal Ctrl+C stops the running program
    from pynput.keyboard import Key

    from . import clipboard

    with _lock:
        try:
            saved = clipboard.snapshot()
        except Exception:
            saved = None
        before = clipboard.sequence()
        kb = _kb()
        with kb.pressed(Key.ctrl):
            kb.tap("c")
        deadline = time.time() + 0.6
        while clipboard.sequence() == before and time.time() < deadline:
            time.sleep(0.02)
        copied = clipboard.sequence() != before
        text = clipboard.get_text() if copied else ""
        if copied and saved is not None and app.lower() not in REMOTE_APPS:
            try:
                clipboard.restore(saved)
            except Exception:
                log.warning("Couldn't put the clipboard back", exc_info=True)
    return text or ""


def _insert_windows(text: str, restore: bool) -> None:
    from . import clipboard

    global _pending
    with _lock:
        saved = None
        if _pending is not None and clipboard.sequence() == _pending[1]:
            # Still holding our last dictation: keep what the user had before that one.
            saved = _pending[0]
        elif restore:
            try:
                saved = clipboard.snapshot()
            except Exception:
                log.warning("Couldn't read the clipboard, so it won't be put back", exc_info=True)
        seq = clipboard.set_text(text)
        entry = (saved, seq) if restore and saved is not None else None
        _pending = entry
    time.sleep(0.05)
    _paste_shortcut()
    if entry is not None:
        threading.Thread(target=_restore_later, args=(entry,), daemon=True).start()


def _restore_later(entry: tuple[list, int]) -> None:
    from . import clipboard

    global _pending
    time.sleep(RESTORE_DELAY)
    with _lock:
        if _pending is not entry:
            return  # a newer paste or copy took over
        _pending = None
        try:
            if clipboard.sequence() == entry[1]:  # nothing else was copied meanwhile
                clipboard.restore(entry[0])
        except Exception:
            log.warning("Couldn't put the clipboard back", exc_info=True)


def _insert_pyperclip(text: str, restore: bool) -> None:
    """Other systems, for development: plain text only."""
    import pyperclip

    previous = None
    if restore:
        try:
            previous = pyperclip.paste()
        except Exception:
            previous = None
    pyperclip.copy(text)
    time.sleep(0.05)
    _paste_shortcut()
    if previous is not None:

        def put_back() -> None:
            time.sleep(RESTORE_DELAY)
            try:
                if pyperclip.paste() == text:
                    pyperclip.copy(previous)
            except Exception:
                pass

        threading.Thread(target=put_back, daemon=True).start()
