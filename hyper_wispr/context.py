"""Which app is the user dictating into? Used to pick a writing style."""

from __future__ import annotations

import sys
from dataclasses import dataclass

EMAIL = "email"
MESSAGE = "message"
CODE = "code"
DOCUMENT = "document"
AI_CHAT = "ai_chat"
OTHER = "other"

# Matched against the lower-cased process name, then the window title.
_PROCESS_TYPES: dict[str, str] = {
    "outlook.exe": EMAIL,
    "olk.exe": EMAIL,
    "thunderbird.exe": EMAIL,
    "slack.exe": MESSAGE,
    "teams.exe": MESSAGE,
    "ms-teams.exe": MESSAGE,
    "discord.exe": MESSAGE,
    "whatsapp.exe": MESSAGE,
    "telegram.exe": MESSAGE,
    "signal.exe": MESSAGE,
    "code.exe": CODE,
    "cursor.exe": CODE,
    "windowsterminal.exe": CODE,
    "pycharm64.exe": CODE,
    "rstudio.exe": CODE,
    "winword.exe": DOCUMENT,
    "notepad.exe": DOCUMENT,
    "obsidian.exe": DOCUMENT,
    "notion.exe": DOCUMENT,
    "onenote.exe": DOCUMENT,
    "claude.exe": AI_CHAT,
    "chatgpt.exe": AI_CHAT,
}

_TITLE_TYPES: list[tuple[str, str]] = [
    ("gmail", EMAIL),
    ("outlook", EMAIL),
    ("whatsapp", MESSAGE),
    ("slack", MESSAGE),
    ("messenger", MESSAGE),
    ("google docs", DOCUMENT),
    ("chatgpt", AI_CHAT),
    ("claude", AI_CHAT),
    ("gemini", AI_CHAT),
    ("github", CODE),
]


@dataclass
class AppContext:
    process: str = ""
    title: str = ""
    app_type: str = OTHER


def classify(process: str, title: str) -> str:
    process, title = process.lower(), title.lower()
    if process in _PROCESS_TYPES:
        return _PROCESS_TYPES[process]
    for needle, kind in _TITLE_TYPES:
        if needle in title:
            return kind
    return OTHER


def _foreground_windows() -> tuple[str, str]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    process = ""
    if handle:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            process = path.value.rsplit("\\", 1)[-1]
        kernel32.CloseHandle(handle)
    return process, title


def current() -> AppContext:
    try:
        process, title = _foreground_windows() if sys.platform == "win32" else ("", "")
    except Exception:
        process, title = "", ""
    return AppContext(process=process, title=title, app_type=classify(process, title))
