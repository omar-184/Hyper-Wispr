"""Read the text box you are dictating into, with Windows UI Automation (the accessibility API
screen readers use).

Used for: the text before and after the cursor (so a dictation continues your sentence
properly), the selected text (command mode), whether anything you can type into has the focus
(so a dictation isn't pasted into nothing), and watching your edits after a paste (learning
words). Nothing read here is logged or leaves the computer.

All UI Automation calls run on one background thread, with a time limit: an app that doesn't
answer must never hold up a dictation. When anything goes wrong the answer is "don't know".
"""

from __future__ import annotations

import concurrent.futures
import itertools
import logging
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

TEXT_PATTERN = 10014
TEXT_PATTERN2 = 10024
VALUE_PATTERN = 10002
START, END = 0, 1  # TextPatternRangeEndpoint
CHARACTER = 0  # TextUnit_Character
WM_GETTEXTLENGTH, EM_GETSEL = 0x000E, 0x00B0
SMTO_ABORTIFHUNG = 0x0002
EDIT_MAX_CHARS = 200_000  # a bigger classic text box isn't read in full just for the words before the cursor

EDIT, DOCUMENT = 50004, 50030
# Controls you can't type into. Deliberately short: many apps report text boxes as "pane",
# "custom", "group" or "data item" (an Excel cell), and those must still get the paste.
NOT_TEXT = {
    50000,  # button
    50002,  # check box
    50005,  # hyperlink
    50006,  # image
    50007,  # list item (e.g. a file in Explorer)
    50011,  # menu item
    50013,  # radio button
    50014,  # scroll bar
    50015,  # slider
    50019,  # tab item
    50024,  # tree item
    50031,  # split button
    50037,  # title bar
}


@dataclass
class Focus:
    """What has the keyboard focus. editable: True, False, or None when we can't tell."""

    known: bool = False
    editable: bool | None = None
    password: bool = False
    before: str = ""
    selected: str = ""
    after: str = ""


_executor: concurrent.futures.ThreadPoolExecutor | None = None
_busy = threading.Lock()
_state: dict[str, Any] = {}
_elements: dict[int, Any] = {}
_ids = itertools.count(1)


def run(fn: Callable[[], T], default: T, timeout: float = 0.5, wait: float = 0.0) -> T:
    """Run fn on the UI Automation thread; `default` if it fails or takes too long.
    wait: how long to wait for an earlier call to finish first."""
    global _executor
    if sys.platform != "win32":
        return default
    acquired = _busy.acquire(timeout=wait) if wait else _busy.acquire(blocking=False)
    if not acquired:
        return default  # a call is still stuck on an app that doesn't answer
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")

    def job() -> T:
        try:
            return fn()
        finally:
            _busy.release()

    try:
        future = _executor.submit(job)
    except Exception:
        _busy.release()
        return default
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        log.info("UI Automation didn't answer in time")
        return default
    except Exception:
        log.debug("UI Automation call failed", exc_info=True)
        return default


def init_com() -> None:
    """Set up COM on this thread (the UI Automation thread) as multithreaded, as UI Automation wants.
    comtypes sets up COM on the thread that first imports it, single-threaded unless told otherwise,
    and a thread can't change afterwards: when this thread was the first, every call here failed
    ("Cannot change thread mode after it is set"), so context, the word learner and muting were off."""
    sys.coinit_flags = 0  # COINIT_MULTITHREADED, read by comtypes when it's first imported
    import comtypes

    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)


def _uia():
    """The UI Automation object, created on (and only used from) the UI Automation thread."""
    if "uia" not in _state:
        init_com()
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as mod  # type: ignore

        _state["mod"] = mod
        _state["uia"] = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
    return _state["uia"], _state["mod"]


def _pattern(element, pattern_id: int, interface):
    try:
        unknown = element.GetCurrentPattern(pattern_id)
        return unknown.QueryInterface(interface) if unknown else None
    except Exception:
        return None


def _selection(element, text, mod):
    """The selected text, or where the cursor is when nothing is selected: some text boxes give no
    selection at all for a bare cursor, so ask for the cursor itself then."""
    selection = text.GetSelection()
    if selection and selection.Length:
        return selection.GetElement(0)
    interface = getattr(mod, "IUIAutomationTextPattern2", None)
    text2 = _pattern(element, TEXT_PATTERN2, interface) if interface is not None else None
    if text2 is None:
        return None
    try:
        caret = text2.GetCaretRange()  # (is the cursor blinking, its range)
    except Exception:
        return None
    return caret[-1] if isinstance(caret, tuple) else caret


def _user32():
    if "user32" not in _state:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.RealGetWindowClassW.argtypes = (wintypes.HWND, wintypes.LPWSTR, wintypes.UINT)
        user32.RealGetWindowClassW.restype = wintypes.UINT
        user32.SendMessageTimeoutW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                               wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        _state["user32"] = user32
    return _state["user32"]


def _classic_edit(element, value, before_chars: int, after_chars: int) -> tuple[str, str, str] | None:
    """A classic Windows text box (Notepad before Windows 11, many older programs) gives UI Automation
    its whole text but not where the cursor is in it, so ask the box itself.
    (before, selected, after), or None when it isn't one or doesn't answer."""
    import ctypes

    try:
        hwnd = element.CurrentNativeWindowHandle
        user32 = _user32()
        name = ctypes.create_unicode_buffer(16)
        if not hwnd or not user32.RealGetWindowClassW(hwnd, name, 16) or name.value.lower() != "edit":
            return None
        length, start, end = ctypes.c_size_t(), ctypes.c_uint32(), ctypes.c_uint32()
        if not user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 200, ctypes.byref(length)):
            return None
        if length.value > EDIT_MAX_CHARS or not user32.SendMessageTimeoutW(
                hwnd, EM_GETSEL, ctypes.addressof(start), ctypes.addressof(end), SMTO_ABORTIFHUNG, 200, None):
            return None
        units = (value.CurrentValue or "").encode("utf-16-le")  # the box counts in UTF-16 units
    except Exception:
        return None
    s, e = 2 * start.value, 2 * end.value
    if not s <= e <= len(units):
        return None

    def part(a: int, b: int) -> str:
        return units[max(a, 0):b].decode("utf-16-le", "replace")

    return part(s - 2 * before_chars, s), part(s, e), part(e, e + 2 * after_chars)


def _read_focus(before_chars: int, after_chars: int) -> Focus:
    uia, mod = _uia()
    element = uia.GetFocusedElement()
    if not element:
        return Focus()
    focus = Focus(known=True)
    focus.password = bool(element.CurrentIsPassword)
    control = element.CurrentControlType
    text = _pattern(element, TEXT_PATTERN, mod.IUIAutomationTextPattern)
    value = _pattern(element, VALUE_PATTERN, mod.IUIAutomationValuePattern)
    if value is not None and not value.CurrentIsReadOnly or control == EDIT:
        focus.editable = True
    elif control in NOT_TEXT and text is None and value is None:
        focus.editable = False
    if focus.password:
        return focus  # never read a password box
    if text is None:
        got = _classic_edit(element, value, before_chars, after_chars) if value is not None else None
        if got is not None:
            focus.before, focus.selected, focus.after = got
        return focus
    sel = _selection(element, text, mod)
    if sel is None:
        return focus
    focus.selected = sel.GetText(20000) or ""
    before = sel.Clone()
    before.MoveEndpointByRange(END, sel, START)
    before.MoveEndpointByUnit(START, CHARACTER, -before_chars)
    focus.before = before.GetText(before_chars) or ""
    after = sel.Clone()
    after.MoveEndpointByRange(START, sel, END)
    after.MoveEndpointByUnit(END, CHARACTER, after_chars)
    focus.after = after.GetText(after_chars) or ""
    if control == DOCUMENT and focus.editable is None:
        focus.editable = True  # a caret in a document: Word, a browser text area
    return focus


def focus(before_chars: int = 600, after_chars: int = 200, timeout: float = 0.5) -> Focus:
    """The focused control and the text around its cursor."""
    return run(lambda: _read_focus(before_chars, after_chars), Focus(), timeout)


def remember_focus() -> int | None:
    """Keep hold of the focused text box to read it again later (learning from edits)."""

    def job() -> int | None:
        uia, _mod = _uia()
        element = uia.GetFocusedElement()
        if not element or element.CurrentIsPassword:
            return None
        handle = next(_ids)
        _elements[handle] = element
        return handle

    return run(job, None)


def text_of(handle: int, limit: int = 20000, timeout: float = 1.0) -> str | None:
    """All the text in a remembered text box, or None if it's gone or can't be read."""

    def job() -> str | None:
        _uia_obj, mod = _uia()
        element = _elements.get(handle)
        if element is None:
            return None
        text = _pattern(element, TEXT_PATTERN, mod.IUIAutomationTextPattern)
        if text is not None:
            return text.DocumentRange.GetText(limit)
        value = _pattern(element, VALUE_PATTERN, mod.IUIAutomationValuePattern)
        return value.CurrentValue[:limit] if value is not None else None

    return run(job, None, timeout)


def has_focus(handle: int) -> bool:
    """Is the remembered text box still the one with the focus?"""

    def job() -> bool:
        uia, _mod = _uia()
        element = _elements.get(handle)
        focused = uia.GetFocusedElement()
        return bool(element is not None and focused and uia.CompareElements(element, focused))

    return run(job, False)


def forget(handle: int) -> None:
    run(lambda: _elements.pop(handle, None), None)


def self_test() -> bool:
    """Can UI Automation be loaded at all? (Used by the build's self-test.)"""
    return run(lambda: _uia() is not None, False, timeout=20)
