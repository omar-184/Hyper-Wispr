"""Microphone capture and the start/stop chimes."""

from __future__ import annotations

import sys
import threading
from typing import Any

import numpy as np


class Recorder:
    def __init__(self, settings: dict[str, Any]):
        import sounddevice as sd

        self._sd = sd
        self.cfg = settings["audio"]
        self.rate = self.cfg.get("sample_rate", 16000)
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None

    def _callback(self, indata, frames, time, status) -> None:  # noqa: ARG002
        with self._lock:
            self._chunks.append(indata[:, 0].copy())

    def start(self) -> None:
        with self._lock:
            self._chunks = []
        self._stream = self._sd.InputStream(
            samplerate=self.rate,
            channels=1,
            dtype="float32",
            device=self.cfg.get("input_device"),
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def seconds(self) -> float:
        with self._lock:
            return sum(len(c) for c in self._chunks) / self.rate


def chime(kind: str, enabled: bool = True) -> None:
    """Short system beeps; generated, so no sound files are needed."""
    if not enabled or sys.platform != "win32":
        return
    import winsound

    tones = {"start": [(880, 60), (1320, 60)], "stop": [(1320, 60), (880, 60)], "error": [(300, 150)]}

    def play() -> None:
        for freq, ms in tones.get(kind, []):
            winsound.Beep(freq, ms)

    threading.Thread(target=play, daemon=True).start()
