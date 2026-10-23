"""The floating "Listening…" pill shown near the bottom of the screen while you dictate.

It never takes focus and clicks pass through it, so it can't disturb the app you're
typing into. Drawing is done with Pillow (render_pill, testable anywhere); on Windows
a per-pixel-alpha layered window shows the result. Elsewhere the overlay does nothing.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

RECORDING, HANDS_FREE, WORKING, HIDDEN = "recording", "hands_free", "working", "hidden"
LABELS = {RECORDING: "Listening", HANDS_FREE: "Hands-free", WORKING: "Transcribing"}

FPS = 30
FADE_IN, FADE_OUT = 0.14, 0.12  # seconds
BASE_H = 36  # pill height at 100% display scaling
MARGIN = 28  # gap between the pill and the taskbar (or top of the screen)
BAR_WEIGHTS = (0.55, 0.8, 1.0, 0.8, 0.55)


def level_to_unit(rms: float) -> float:
    """Microphone RMS -> 0..1 on a decibel scale, so quiet speech still moves the bars."""
    db = 20 * math.log10(max(rms, 1e-6))
    return min(1.0, max(0.0, (db + 55) / 40))  # -55 dB -> 0, -15 dB -> 1


_fonts: dict[int, object] = {}


def _font(px: int):
    if px not in _fonts:
        from PIL import ImageFont

        for name in ("segoeuisb.ttf", "seguisb.ttf", "segoeui.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
            try:
                _fonts[px] = ImageFont.truetype(name, px)
                break
            except OSError:
                continue
        else:
            _fonts[px] = ImageFont.load_default()
    return _fonts[px]


def render_pill(state: str, t: float, level: float, scale: float = 1.0):
    """Draw the pill as an RGBA image. `t` is seconds since it appeared (drives the
    animations), `level` the smoothed 0..1 voice level, `scale` the display scaling."""
    from PIL import Image, ImageDraw

    ss = 3  # draw 3x larger and shrink: smooth edges without an anti-aliasing API
    k = scale * ss
    label = LABELS.get(state, "")
    font = _font(round(13 * k))
    text_w = font.getlength(label)
    h = round(BASE_H * k)
    pad = round(14 * k)
    icon_w = round(12 * k)  # dot / spinner area
    bars_w = round(12 * k + 5 * 3 * k + 4 * 3 * k) if state in (RECORDING, HANDS_FREE) else 0  # gap + 5 bars
    w = int(pad + icon_w + round(9 * k) + text_w + bars_w + pad)
    shadow = round(8 * k)
    img = Image.new("RGBA", (w + 2 * shadow, h + 2 * shadow), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Soft shadow, then the pill: near-black so it reads on light and dark apps alike.
    for i in range(shadow, 0, -1):
        a = int(26 * (1 - i / shadow) ** 2)
        d.rounded_rectangle((shadow - i, shadow - i + round(2 * k), shadow + w + i, shadow + h + i + round(2 * k)), radius=h / 2 + i, fill=(0, 0, 0, a))
    d.rounded_rectangle((shadow, shadow, shadow + w, shadow + h), radius=h / 2, fill=(28, 28, 30, 238))
    d.rounded_rectangle((shadow, shadow, shadow + w, shadow + h), radius=h / 2, outline=(255, 255, 255, 30), width=max(1, round(k)))

    cy = shadow + h / 2
    x = shadow + pad
    if state in (RECORDING, HANDS_FREE):
        # Recording dot that breathes gently (about one cycle per 1.4 s).
        r = 4.5 * k * (0.9 + 0.1 * math.sin(t * 4.5))
        color = (255, 69, 58, 255) if state == RECORDING else (255, 159, 10, 255)
        d.ellipse((x + icon_w / 2 - r, cy - r, x + icon_w / 2 + r, cy + r), fill=color)
    else:
        # Three dots rippling: work in progress.
        for i in range(3):
            phase = (t * 2.2 - i * 0.22) % 1.0
            a = 110 + int(145 * max(0.0, math.sin(phase * math.pi)))
            rr = 1.9 * k
            cx = x + i * 4.6 * k + rr
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=(255, 255, 255, a))
    x += icon_w + round(9 * k)

    d.text((x, cy), label, font=font, fill=(245, 245, 247, 255), anchor="lm")
    x += text_w + round(12 * k)

    if state in (RECORDING, HANDS_FREE):
        bw, gap = 3 * k, 3 * k
        max_h, min_h = 16 * k, 3 * k
        for i, wgt in enumerate(BAR_WEIGHTS):
            wobble = 0.85 + 0.15 * math.sin(t * 9 + i * 1.7)
            bh = min_h + (max_h - min_h) * min(1.0, level * wgt * wobble)
            bx = x + i * (bw + gap)
            d.rounded_rectangle((bx, cy - bh / 2, bx + bw, cy + bh / 2), radius=bw / 2, fill=(245, 245, 247, 235))

    out = img.resize((max(1, round(img.width / ss)), max(1, round(img.height / ss))), Image.LANCZOS)
    return out


class Overlay:
    """Thread-safe front end: call show(state) from anywhere; a private thread draws."""

    def __init__(
        self,
        level: Callable[[], float],
        position: str = "bottom",
        enabled: bool = True,
        hands_free: Callable[[], bool] = lambda: False,
    ):
        self._level = level
        self._hands_free = hands_free
        self.position = position
        self.enabled = enabled
        self._state = HIDDEN
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    def show(self, state: str) -> None:
        self._state = state if self.enabled else HIDDEN
        if self._state != HIDDEN and sys.platform == "win32" and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
            self._thread.start()
        self._wake.set()

    def hide(self) -> None:
        self.show(HIDDEN)

    def _run(self) -> None:
        try:
            _Win32Pill(self).loop()
        except Exception:
            log.exception("The listening indicator stopped working")


class _Win32Pill:
    """Layered, topmost, click-through tool window drawn with UpdateLayeredWindow."""

    def __init__(self, owner: Overlay):
        import ctypes
        from ctypes import wintypes as wt

        self.owner = owner
        self.ct, self.wt = ctypes, wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32, self.gdi32 = user32, gdi32
        H = ctypes.c_void_p
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, H, wt.UINT, wt.WPARAM, wt.LPARAM)

        for fn, res, args in (
            ("DefWindowProcW", LRESULT, (H, wt.UINT, wt.WPARAM, wt.LPARAM)),
            ("CreateWindowExW", H, (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, H, H, H)),
            ("ShowWindow", wt.BOOL, (H, ctypes.c_int)),
            ("GetDC", H, (H,)),
            ("ReleaseDC", ctypes.c_int, (H, H)),
            ("UpdateLayeredWindow", wt.BOOL, (H, H, ctypes.c_void_p, ctypes.c_void_p, H, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD)),
            ("PeekMessageW", wt.BOOL, (ctypes.c_void_p, H, wt.UINT, wt.UINT, wt.UINT)),
            ("TranslateMessage", wt.BOOL, (ctypes.c_void_p,)),
            ("DispatchMessageW", LRESULT, (ctypes.c_void_p,)),
            ("GetForegroundWindow", H, ()),
            ("MonitorFromWindow", H, (H, wt.DWORD)),
            ("GetMonitorInfoW", wt.BOOL, (H, ctypes.c_void_p)),
            ("SystemParametersInfoW", wt.BOOL, (wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT)),
            ("SetWindowPos", wt.BOOL, (H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)),
        ):
            f = getattr(user32, fn)
            f.restype, f.argtypes = res, args
        for fn, res, args in (
            ("CreateCompatibleDC", H, (H,)),
            ("DeleteDC", wt.BOOL, (H,)),
            ("CreateDIBSection", H, (H, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), H, wt.DWORD)),
            ("SelectObject", H, (H, H)),
            ("DeleteObject", wt.BOOL, (H,)),
        ):
            f = getattr(gdi32, fn)
            f.restype, f.argtypes = res, args
        kernel32.GetModuleHandleW.restype = H
        kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)

        # Crisp on high-DPI screens: make only this thread's windows DPI-aware, so the
        # rest of the app (tray icon, paste) behaves exactly as before.
        try:
            user32.SetThreadDpiAwarenessContext.restype = H
            user32.SetThreadDpiAwarenessContext.argtypes = (H,)
            user32.SetThreadDpiAwarenessContext(H(-4))  # PER_MONITOR_AWARE_V2
        except AttributeError:
            pass

        self._proc = self.WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))  # keep a reference

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", H), ("hIcon", H), ("hCursor", H), ("hbrBackground", H),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", H)]

        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "HyperWisprPill"
        user32.RegisterClassExW.argtypes = (ctypes.c_void_p,)
        user32.RegisterClassExW(ctypes.byref(wc))

        WS_EX = 0x00080000 | 0x00000008 | 0x00000080 | 0x08000000 | 0x00000020  # LAYERED|TOPMOST|TOOLWINDOW|NOACTIVATE|TRANSPARENT
        WS_POPUP = 0x80000000
        self.hwnd = user32.CreateWindowExW(WS_EX, "HyperWisprPill", "Hyper-Wispr", WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        self.BLENDFUNCTION, self.MONITORINFO = BLENDFUNCTION, MONITORINFO
        self.msg = wt.MSG()
        self.visible = False

    # ---- helpers ---------------------------------------------------------
    def _animations_on(self) -> bool:
        on = self.wt.BOOL(True)
        self.user32.SystemParametersInfoW(0x1042, 0, self.ct.byref(on), 0)  # SPI_GETCLIENTAREAANIMATION
        return bool(on.value)

    def _work_area(self) -> tuple[tuple[int, int, int, int], float]:
        """The screen the user is working on (where the foreground window is) and its scaling."""
        mon = self.user32.MonitorFromWindow(self.user32.GetForegroundWindow(), 2)  # DEFAULTTONEAREST
        info = self.MONITORINFO()
        info.cbSize = self.ct.sizeof(self.MONITORINFO)
        self.user32.GetMonitorInfoW(mon, self.ct.byref(info))
        r = info.rcWork
        scale = 1.0
        try:
            shcore = self.ct.WinDLL("shcore")
            dx, dy = self.wt.UINT(), self.wt.UINT()
            if shcore.GetDpiForMonitor(self.ct.c_void_p(mon), 0, self.ct.byref(dx), self.ct.byref(dy)) == 0:
                scale = dx.value / 96
        except (OSError, AttributeError):
            pass
        return (r.left, r.top, r.right, r.bottom), scale

    def _blit(self, img, x: int, y: int, alpha: int) -> None:
        """Copy an RGBA image into the layered window with per-pixel alpha."""
        import numpy as np

        ct, wt = self.ct, self.wt
        w, h = img.size
        px = np.asarray(img, dtype=np.uint16)
        a = px[..., 3:4]
        bgra = np.concatenate([px[..., 2::-1] * a // 255, a], axis=2).astype(np.uint8)  # premultiplied BGRA

        class BITMAPINFOHEADER(ct.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", ct.c_long), ("biHeight", ct.c_long), ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                        ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ct.c_long), ("biYPelsPerMeter", ct.c_long),
                        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        bih = BITMAPINFOHEADER(ct.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # negative height: top-down rows
        screen = self.user32.GetDC(None)
        mem = self.gdi32.CreateCompatibleDC(screen)
        bits = ct.c_void_p()
        bmp = self.gdi32.CreateDIBSection(mem, ct.byref(bih), 0, ct.byref(bits), None, 0)
        try:
            ct.memmove(bits, bgra.tobytes(), w * h * 4)
            old = self.gdi32.SelectObject(mem, bmp)
            pos, size, src = wt.POINT(x, y), wt.SIZE(w, h), wt.POINT(0, 0)
            blend = self.BLENDFUNCTION(0, 0, max(0, min(255, alpha)), 1)  # AC_SRC_OVER, AC_SRC_ALPHA
            self.user32.UpdateLayeredWindow(self.hwnd, screen, ct.byref(pos), ct.byref(size), mem, ct.byref(src), 0, ct.byref(blend), 2)
            self.gdi32.SelectObject(mem, old)
        finally:
            self.gdi32.DeleteObject(bmp)
            self.gdi32.DeleteDC(mem)
            self.user32.ReleaseDC(None, screen)

    def _pump(self) -> None:
        while self.user32.PeekMessageW(self.ct.byref(self.msg), None, 0, 0, 1):  # PM_REMOVE
            self.user32.TranslateMessage(self.ct.byref(self.msg))
            self.user32.DispatchMessageW(self.ct.byref(self.msg))

    # ---- main loop -------------------------------------------------------
    def loop(self) -> None:
        owner = self.owner
        shown_at = hidden_at = 0.0
        state = HIDDEN  # what is on screen (stays set while fading out)
        level = 0.0
        area, scale = (0, 0, 800, 600), 1.0
        anim = True
        while True:
            self._pump()
            want = owner._state
            if want == RECORDING and owner._hands_free():
                want = HANDS_FREE
            now = time.monotonic()
            if want != HIDDEN:
                if not self.visible:
                    area, scale = self._work_area()
                    anim = self._animations_on()
                    shown_at, self.visible = now, True
                    self.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                    # Back on top of windows that became topmost meanwhile (e.g. a video player).
                    self.user32.SetWindowPos(self.hwnd, self.ct.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE
                    level = 0.0
                state, hidden_at = want, 0.0
            elif self.visible and not hidden_at:
                hidden_at = now

            if not self.visible:
                owner._wake.wait(0.5)
                owner._wake.clear()
                continue

            try:
                raw = owner._level() if state in (RECORDING, HANDS_FREE) else 0.0
            except Exception:
                raw = 0.0
            target = level_to_unit(raw)
            level += (target - level) * (0.55 if target > level else 0.18)  # quick rise, gentle fall

            if hidden_at:
                p = min(1.0, (now - hidden_at) / FADE_OUT) if anim else 1.0
                alpha, rise = 1.0 - p, 0.0
                if p >= 1.0:
                    self.user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
                    self.visible = False
                    continue
            else:
                p = min(1.0, (now - shown_at) / FADE_IN) if anim else 1.0
                ease = 1 - (1 - p) ** 3  # ease-out: arrives fast, settles softly
                alpha, rise = ease, (1 - ease) * 6 * scale

            img = render_pill(state, now - shown_at, level, scale)
            left, top, right, bottom = area
            x = (left + right - img.width) // 2
            if owner.position == "top":
                y = top + round(MARGIN * scale) - round(rise)
            else:
                y = bottom - img.height - round(MARGIN * scale) + round(rise)
            self._blit(img, x, y, round(255 * alpha))
            time.sleep(1 / FPS)


def self_test() -> int:
    """Show the pill for a moment in each state (used by the installer build)."""
    import random

    ov = Overlay(level=lambda: random.uniform(0.005, 0.08))
    for state in (RECORDING, HANDS_FREE, WORKING, HIDDEN):
        ov.show(state)
        time.sleep(0.6)
    time.sleep(0.3)
    if sys.platform == "win32" and (ov._thread is None or not ov._thread.is_alive()):
        log.error("Overlay self-test failed: the drawing thread is not running")
        return 1
    log.info("Overlay self-test OK")
    return 0
