"""Offline speech-to-text with faster-whisper (OpenAI Whisper models run locally)."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


def _add_cuda_dll_dirs() -> None:
    """On Windows, the CUDA libraries come from the nvidia-* pip wheels; make them findable."""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for root in nvidia.__path__:
        for sub in ("cublas", "cudnn"):
            bin_dir = os.path.join(root, sub, "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve(cfg: dict[str, Any]) -> tuple[str, str, str]:
    device = cfg.get("device", "auto")
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    model = cfg.get("model", "auto")
    if model == "auto":
        model = "large-v3-turbo" if device == "cuda" else "small"
    compute = cfg.get("compute_type", "auto")
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return model, device, compute


class Transcriber:
    def __init__(self, settings: dict[str, Any]):
        from faster_whisper import WhisperModel

        _add_cuda_dll_dirs()
        self.settings = settings
        model, device, compute = resolve(settings["speech"])
        log.info("Loading Whisper model %s on %s (%s)", model, device, compute)
        try:
            self.model = WhisperModel(model, device=device, compute_type=compute)
        except Exception as e:
            if device != "cuda":
                raise
            log.warning("GPU load failed (%s); falling back to CPU", e)
            model, device, compute = resolve({**settings["speech"], "device": "cpu"})
            self.model = WhisperModel(model, device=device, compute_type=compute)
        self.description = f"{model} on {device.upper()}"

    def transcribe(self, audio: np.ndarray) -> str:
        cfg = self.settings["speech"]
        # The initial prompt nudges Whisper toward your spelling of names and jargon.
        words = self.settings.get("dictionary", [])
        prompt = ("Vocabulary: " + ", ".join(words) + ".") if words else None
        segments, _info = self.model.transcribe(
            audio,
            language=cfg.get("language") or None,
            beam_size=cfg.get("beam_size", 5),
            vad_filter=cfg.get("vad_filter", True),
            initial_prompt=prompt,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()
