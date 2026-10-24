"""Encrypt text for storage with Windows' own per-user encryption (DPAPI).

Only the Windows account that saved the text can read it back, so a copied history file or
backup is useless on another account or PC. Other systems (development only) store it as is.
"""

from __future__ import annotations

import sys

# Stored values start with this, so plain rows written by older versions are still readable.
PREFIX = b"HWE1"


def _blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf, DATA_BLOB


def _crypt(data: bytes, protect: bool) -> bytes:
    import ctypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # else get_last_error() below is always 0
    kernel32 = ctypes.WinDLL("kernel32")
    blob_in, _keep, DATA_BLOB = _blob(data)
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(blob_in), "Hyper-Wispr", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows couldn't " + ("encrypt" if protect else "decrypt") + " the data")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def available() -> bool:
    return sys.platform == "win32"


def encrypt(data: bytes) -> bytes:
    if not available():
        return data
    return PREFIX + _crypt(data, True)


def decrypt(data: bytes | str | None) -> bytes:
    if data is None:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8")  # a row from before encryption
    if data.startswith(PREFIX):
        return _crypt(data[len(PREFIX):], False)
    return data


def encrypt_text(text: str | None) -> bytes | None:
    return None if text is None else encrypt(text.encode("utf-8"))


def decrypt_text(data: bytes | str | None) -> str:
    return decrypt(data).decode("utf-8", errors="replace")
