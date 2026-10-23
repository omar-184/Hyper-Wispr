"""Microphone capture and the start/stop chimes."""

from __future__ import annotations

import io
import logging
import sys
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

TARGET_RATE = 16000


def rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    centered = audio - float(np.mean(audio))
    return float(np.sqrt(np.mean(centered * centered)))


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    n = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class Recorder:
    """Records mono audio from the chosen (or default) microphone, delivered at 16 kHz."""

    def __init__(self, settings: dict[str, Any]):
        import sounddevice as sd

        self._sd = sd
        self.cfg = settings["audio"]
        self.rate = TARGET_RATE
        self._device_rate = TARGET_RATE
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None

    def _callback(self, indata, frames, time, status) -> None:  # noqa: ARG002
        with self._lock:
            self._chunks.append(indata[:, 0].copy())

    def _open(self, device, rate: int):
        return self._sd.InputStream(
            samplerate=rate, channels=1, dtype="float32", device=device, callback=self._callback
        )

    def start(self) -> None:
        with self._lock:
            self._chunks = []
        device = self.cfg.get("input_device")
        try:
            self._stream = self._open(device, TARGET_RATE)
            self._device_rate = TARGET_RATE
        except Exception as e:
            # Some drivers only accept their native rate; record at that and resample.
            native = int(self._sd.query_devices(device, "input")["default_samplerate"])
            log.info("Microphone refused 16 kHz (%s); recording at %d Hz", e, native)
            self._stream = self._open(device, native)
            self._device_rate = native
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        with self._lock:
            chunks, self._chunks = self._chunks, []
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return resample(audio, self._device_rate)

    def level(self) -> float:
        """Loudness of the last few audio blocks, for the on-screen level bars."""
        with self._lock:
            recent = self._chunks[-4:]
        return rms(np.concatenate(recent)) if recent else 0.0

    def device_name(self) -> str:
        try:
            info = self._sd.query_devices(self.cfg.get("input_device"), "input")
            return str(info["name"])
        except Exception as e:
            return f"unknown ({e})"


# ---- chimes -------------------------------------------------------------

# (start Hz, end Hz, milliseconds, volume). Soft sine glides, generated so no sound files ship.
_CHIMES = {
    "start": [(660, 880, 90, 0.25)],
    "stop": [(880, 660, 90, 0.25)],
    "error": [(440, 440, 70, 0.2), (0, 0, 40, 0), (330, 330, 110, 0.2)],
}


def _tone_wav(parts: list[tuple[int, int, int, float]], rate: int = 44100) -> bytes:
    pieces = []
    for f0, f1, ms, vol in parts:
        n = int(rate * ms / 1000)
        t = np.arange(n) / rate
        freq = np.linspace(f0, f1, n)
        phase = 2 * np.pi * np.cumsum(freq) / rate
        env = np.minimum(1.0, np.minimum(t / 0.008, (n / rate - t) / 0.03))  # click-free fades
        pieces.append(vol * np.sin(phase) * np.clip(env, 0, 1))
    pcm = (np.concatenate(pieces) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


_chime_paths: dict[str, Path] = {}


def _chime_file(kind: str) -> Path:
    if kind not in _chime_paths:
        from .config import app_dir

        path = app_dir() / "sounds" / f"{kind}.wav"
        path.parent.mkdir(exist_ok=True)
        data = _tone_wav(_CHIMES[kind])
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
        _chime_paths[kind] = path
    return _chime_paths[kind]


def chime(kind: str, enabled: bool = True) -> None:
    if not enabled or sys.platform != "win32" or kind not in _CHIMES:
        return
    try:
        import winsound

        winsound.PlaySound(str(_chime_file(kind)), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:
        log.debug("Chime failed: %s", e)
