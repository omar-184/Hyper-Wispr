"""The Windows clipboard, for pasting dictations.

pyperclip only knows plain text: "restoring" a copied screenshot or file with it replaced it with
nothing. This module saves and restores every format that is plain memory (text, rich text,
images, copied files), and marks dictated text, which can contain patient information, so it
stays out of clipboard history (Win+V) and cloud clipboard sync.
"""

from __future__ import annotations

import contextlib
import ctypes
import re
import time
from ctypes import wintypes

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = wintypes.HWND(-3)
# Bitmaps, palettes and metafiles are GDI handles, not memory we can copy; Windows recreates the
# bitmap from the DIB copy. "Private" and "GDI object" ranges belong to the app that copied them.
_HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
# Windows makes each of these from any other in its group (ANSI/OEM/Unicode text; DIB/DIBv5), so
# keeping the first one listed, which is the one the app put there, is enough and saves memory.
_SYNTHESIZED_GROUPS = ({1, 7, 13}, {8, 17})
# Pointers into the app that copied, through OLE: stale once anything else is copied.
_OLE_FORMATS = ("DataObject", "Ole Private Data")
_PRIVACY_FORMATS = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")
# Larger than this (a huge Excel range, say) and we don't restore. A 4K screenshot is about 33 MB.
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024

_user32 = None
_kernel32 = None


def _dlls():
    global _user32, _kernel32
    if _user32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Handles are pointer-sized: without these declarations ctypes cuts them to 32 bits.
        for name, args, res in [
            ("OpenClipboard", (wintypes.HWND,), wintypes.BOOL),
            ("CloseClipboard", (), wintypes.BOOL),
            ("EmptyClipboard", (), wintypes.BOOL),
            ("EnumClipboardFormats", (wintypes.UINT,), wintypes.UINT),
            ("GetClipboardData", (wintypes.UINT,), wintypes.HANDLE),
            ("SetClipboardData", (wintypes.UINT, wintypes.HANDLE), wintypes.HANDLE),
            ("RegisterClipboardFormatW", (wintypes.LPCWSTR,), wintypes.UINT),
            ("GetClipboardSequenceNumber", (), wintypes.DWORD),
            ("DestroyWindow", (wintypes.HWND,), wintypes.BOOL),
            (
                "CreateWindowExW",
                (
                    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
                ),
                wintypes.HWND,
            ),
        ]:
            fn = getattr(user32, name)
            fn.argtypes, fn.restype = args, res
        for name, args, res in [
            ("GlobalAlloc", (wintypes.UINT, ctypes.c_size_t), wintypes.HGLOBAL),
            ("GlobalLock", (wintypes.HGLOBAL,), wintypes.LPVOID),
            ("GlobalUnlock", (wintypes.HGLOBAL,), wintypes.BOOL),
            ("GlobalSize", (wintypes.HGLOBAL,), ctypes.c_size_t),
            ("GlobalFree", (wintypes.HGLOBAL,), wintypes.HGLOBAL),
        ]:
            fn = getattr(kernel32, name)
            fn.argtypes, fn.restype = args, res
        _user32, _kernel32 = user32, kernel32
    return _user32, _kernel32


@contextlib.contextmanager
def _opened(timeout: float = 1.0):
    user32, _ = _dlls()
    # SetClipboardData needs an owner window; a hidden message-only one, gone when we're done.
    hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        deadline = time.monotonic() + timeout
        while not user32.OpenClipboard(hwnd):
            if time.monotonic() > deadline:  # another app keeps it open
                raise OSError(f"The clipboard is busy (error {ctypes.get_last_error()})")
            time.sleep(0.01)
        try:
            yield user32
        finally:
            user32.CloseClipboard()
    finally:
        if hwnd:
            user32.DestroyWindow(hwnd)


def _put(fmt: int, data: bytes) -> None:
    user32, kernel32 = _dlls()
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise MemoryError("GlobalLock failed")
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)  # on success the clipboard owns it
        raise OSError(f"SetClipboardData failed (error {ctypes.get_last_error()})")


def sequence() -> int:
    """Goes up every time anything is copied. Reading the clipboard (pasting) doesn't change it."""
    return _dlls()[0].GetClipboardSequenceNumber()


def _registered(names: tuple[str, ...]) -> set[int]:
    user32, _ = _dlls()
    return {fmt for fmt in map(user32.RegisterClipboardFormatW, names) if fmt}


def _put_private_markers(already: set[int] | None = None) -> None:
    for fmt in _registered(_PRIVACY_FORMATS) - (already or set()):
        _put(fmt, (0).to_bytes(4, "little"))


def snapshot() -> list[tuple[int, bytes]] | None:
    """Copy of what's on the clipboard now; None when it's too big to keep."""
    _, kernel32 = _dlls()
    skip = _HANDLE_FORMATS | _registered(_OLE_FORMATS)
    items: list[tuple[int, bytes]] = []
    total = 0
    with _opened() as user32:
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in skip and not 0x200 <= fmt <= 0x3FF:
                for group in _SYNTHESIZED_GROUPS:
                    if fmt in group:
                        skip |= group  # Windows will make the others again from this one
                handle = user32.GetClipboardData(fmt)
                size = kernel32.GlobalSize(handle) if handle else 0
                total += size
                if total > MAX_SNAPSHOT_BYTES:
                    return None
                pointer = kernel32.GlobalLock(handle) if size else None
                if pointer:
                    try:
                        items.append((fmt, ctypes.string_at(pointer, size)))
                    finally:
                        kernel32.GlobalUnlock(handle)
            fmt = user32.EnumClipboardFormats(fmt)
    return items


def restore(items: list[tuple[int, bytes]]) -> None:
    with _opened() as user32:
        user32.EmptyClipboard()
        for fmt, data in items:
            try:
                _put(fmt, data)
            except OSError:
                pass  # a format Windows won't take back; the others still matter
        if items:
            # It's already in clipboard history from when it was first copied; don't add it twice.
            _put_private_markers(already={fmt for fmt, _ in items})


def set_text(text: str) -> int:
    """Put text on the clipboard, kept out of clipboard history and cloud sync.

    Returns the clipboard's sequence number afterwards, to tell later whether anything else was
    copied since."""
    # Windows apps expect CRLF; a bare "\n" shows up as nothing or a box in some of them.
    data = (re.sub(r"\r?\n", "\r\n", text) + "\0").encode("utf-16-le")
    with _opened() as user32:
        user32.EmptyClipboard()
        _put(CF_UNICODETEXT, data)
        _put_private_markers()
    return sequence()


def get_text() -> str | None:
    with _opened() as user32:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        _, kernel32 = _dlls()
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
