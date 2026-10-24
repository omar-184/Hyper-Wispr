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
PREROLL_SECONDS = 0.4


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
        # Always-on microphone: the stream stays open between dictations, keeping the last moment
        # of sound so a word spoken as you press the keys isn't lost.
        self._recording = False
        self._preroll: list[np.ndarray] = []
        self._open_device = None
        # The microphone actually in use: the default one when the chosen one is unplugged.
        self.device = self.cfg.get("input_device")
        self.fell_back = False

    def _callback(self, indata, frames, time, status) -> None:  # noqa: ARG002
        with self._lock:
            if self._recording:
                self._chunks.append(indata[:, 0].copy())
                return
            self._preroll.append(indata[:, 0].copy())
            keep = int(PREROLL_SECONDS * self._device_rate)
            while len(self._preroll) > 1 and sum(len(c) for c in self._preroll[1:]) >= keep:
                self._preroll.pop(0)

    def _open(self, device, rate: int):
        return self._sd.InputStream(
            samplerate=rate, channels=1, dtype="float32", device=device, callback=self._callback
        )

    def _start(self, device) -> None:
        try:
            stream = self._open(device, TARGET_RATE)
            rate = TARGET_RATE
        except Exception as e:
            # Some drivers only accept their native rate; record at that and resample.
            rate = int(self._sd.query_devices(device, "input")["default_samplerate"])
            log.info("Microphone refused 16 kHz (%s); recording at %d Hz", e, rate)
            stream = self._open(device, rate)
        try:
            stream.start()
        except Exception:
            stream.close()  # or the half-open device stays locked until the app quits
            raise
        self._stream, self._device_rate, self.device = stream, rate, device

    @property
    def keep_open(self) -> bool:
        return bool(self.cfg.get("keep_mic_open"))

    def _alive(self) -> bool:
        stream = self._stream
        try:
            return stream is not None and bool(stream.active) and self._open_device == self.cfg.get("input_device")
        except Exception:
            return False

    def _open_stream(self) -> None:
        wanted = self.cfg.get("input_device")
        self.fell_back = False
        try:
            self._start(wanted)
        except Exception:
            if wanted is None:
                raise
            # The chosen microphone is unplugged or in use: the Windows default beats no dictation.
            log.warning("Couldn't open the microphone '%s'; using the default one", wanted, exc_info=True)
            self._start(None)
            self.fell_back = True
        self._open_device = wanted

    def open_standby(self) -> None:
        """Always-on microphone: open it now (or reopen it after a change), ready for the next dictation.
        Not while recording: a settings change then mustn't cut the dictation short."""
        if self._recording:
            return
        if not self.keep_open:
            self.close()
            return
        if not self._alive():
            self.close()
            with self._lock:
                self._recording, self._preroll = False, []
            self._open_stream()

    def start(self) -> None:
        if self.keep_open and self._alive():
            with self._lock:
                self._chunks, self._preroll = self._preroll, []
                self._recording = True
            return
        self.close()
        with self._lock:
            self._chunks, self._preroll = [], []
            self._recording = True
        try:
            self._open_stream()
        except Exception:
            with self._lock:
                self._recording = False
            raise

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            # A driver error here must not lose the recording (or leave the app stuck "listening").
            for step in (stream.stop, stream.close):
                try:
                    step()
                except Exception:
                    log.warning("Closing the microphone failed", exc_info=True)

    def stop(self) -> np.ndarray:
        if not (self.keep_open and self._alive()):
            self.close()
        with self._lock:
            chunks, self._chunks = self._chunks, []
            self._recording = False
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return resample(audio, self._device_rate)

    def level(self) -> float:
        """Loudness of the last few audio blocks, for the on-screen level bars."""
        with self._lock:
            recent = self._chunks[-4:]
        return rms(np.concatenate(recent)) if recent else 0.0

    def device_name(self) -> str:
        try:
            info = self._sd.query_devices(self.device, "input")
            return str(info["name"])
        except Exception as e:
            return f"unknown ({e})"


# ---- chimes -------------------------------------------------------------

# Each sound is a few soft notes: (pitch Hz, starts at ms, rings for ms, volume). Every note is a
# sine with a faint octave above it, a gentle fade-in and a long natural fade-out, which sounds like
# a soft mallet rather than a beep. Generated here so no sound files ship.
_CHIMES = {
    # Two low notes a fifth apart, rising: "ready, listening".
    "start": [(523.25, 0, 260, 0.11), (783.99, 70, 300, 0.09)],
    # The same two notes falling: "got it".
    "stop": [(783.99, 0, 240, 0.09), (523.25, 60, 280, 0.10)],
    "error": [(392.0, 0, 200, 0.12), (311.13, 120, 300, 0.12)],
}


def _tone_wav(notes: list[tuple[float, int, int, float]], rate: int = 44100) -> bytes:
    length = max(start + ms for _, start, ms, _ in notes) / 1000
    out = np.zeros(int(rate * length) + 1)
    for freq, start, ms, vol in notes:
        n = int(rate * ms / 1000)
        t = np.arange(n) / rate
        tone = np.sin(2 * np.pi * freq * t) + 0.12 * np.sin(4 * np.pi * freq * t)
        attack = np.minimum(1.0, t / 0.012)  # 12 ms fade-in: no click, no hard edge
        decay = np.exp(-t / (ms / 1000 / 4.5))  # rings out to silence by the end of the note
        i = int(rate * start / 1000)
        out[i:i + n] += vol * tone * attack * decay
    pcm = (np.clip(out, -1, 1) * 32767).astype("<i2").tobytes()
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
