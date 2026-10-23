"""Put text where the cursor is.

Default method: copy to the clipboard, press Ctrl+V, then put back whatever
was on the clipboard before. This works in nearly every Windows app.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

_keyboard = None


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


def insert(text: str, settings: dict[str, Any]) -> None:
    import pyperclip

    cfg = settings["paste"]
    if cfg.get("add_trailing_space", True) and text and not text.endswith(("\n", " ")):
        text += " "
    if cfg.get("method") == "type":
        _kb().type(text)
        return

    previous = None
    if cfg.get("restore_clipboard", True):
        try:
            previous = pyperclip.paste()
        except Exception:
            previous = None
    pyperclip.copy(text)
    time.sleep(0.05)
    _paste_shortcut()
    if previous is not None:
        # Give the target app time to read the clipboard before restoring it.
        def restore() -> None:
            time.sleep(0.6)
            try:
                if pyperclip.paste() == text:
                    pyperclip.copy(previous)
            except Exception:
                pass

        threading.Thread(target=restore, daemon=True).start()
