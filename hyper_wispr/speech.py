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


# Best first. Older NVIDIA cards (e.g. GTX 10-series) can't do float16, which is what
# broke GPU loading in v0.1.0; they still work with int8 or float32.
_GPU_COMPUTE_PREFERENCE = ["float16", "int8_float16", "int8_float32", "int8", "float32"]


def gpu_compute_types() -> list[str]:
    try:
        import ctranslate2

        supported = set(ctranslate2.get_supported_compute_types("cuda"))
    except Exception:
        return []
    return [c for c in _GPU_COMPUTE_PREFERENCE if c in supported]


def resolve(cfg: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Returns (model, device, compute types to try in order)."""
    device = cfg.get("device", "auto")
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    model = cfg.get("model", "auto")
    if model == "auto":
        model = "large-v3-turbo" if device == "cuda" else "small"
    compute = cfg.get("compute_type", "auto")
    if compute != "auto":
        computes = [compute]
    elif device == "cuda":
        computes = gpu_compute_types() or ["float32"]
    else:
        computes = ["int8"]
    return model, device, computes


def prepare_audio(audio: np.ndarray) -> np.ndarray:
    """Remove DC offset and bring quiet microphones up to a level Whisper and the VAD handle well."""
    audio = audio.astype(np.float32, copy=False)
    if audio.size == 0:
        return audio
    audio = audio - float(np.mean(audio))
    peak = float(np.max(np.abs(audio)))
    if peak > 1e-4:
        audio = audio * min(0.9 / peak, 30.0)
    return audio


def pick_language(cfg: dict[str, Any], detected: list[tuple[str, float]] | None) -> str | None:
    """Explicit language wins; otherwise the most likely of the allowed languages.

    Auto-detecting from a few seconds of speech is unreliable (v0.1.0 typed Finnish for English),
    so detection is limited to the languages the user actually speaks.
    """
    if cfg.get("language"):
        return cfg["language"]
    allowed = [lang for lang in cfg.get("languages", []) if lang]
    if len(allowed) == 1:
        return allowed[0]
    if not allowed or not detected:
        return None
    probs = dict(detected)
    return max(allowed, key=lambda lang: probs.get(lang, 0.0))


RELOAD_KEYS = ("model", "device", "compute_type")


def needs_reload(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return any(old.get(k) != new.get(k) for k in RELOAD_KEYS)


class Transcriber:
    def __init__(self, settings: dict[str, Any]):
        from faster_whisper import WhisperModel

        _add_cuda_dll_dirs()
        self.settings = settings
        model, device, computes = resolve(settings["speech"])
        self.model = None
        for compute in computes:
            log.info("Loading Whisper model %s on %s (%s)", model, device, compute)
            try:
                self.model = WhisperModel(model, device=device, compute_type=compute)
                # Missing GPU libraries only fail on first use, so run once now.
                list(self.model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)[0])
                break
            except Exception as e:
                self.model = None
                log.warning("Could not load %s on %s with %s: %s", model, device, compute, e)
        if self.model is None:
            if device != "cuda":
                raise RuntimeError(f"Could not load speech model {model}")
            log.warning("GPU unavailable; falling back to CPU")
            model, device, computes = resolve({**settings["speech"], "device": "cpu", "compute_type": "auto"})
            compute = computes[0]
            self.model = WhisperModel(model, device=device, compute_type=compute)
        self.description = f"{model} on {device.upper()} ({compute})"
        # Settings that need a new model when they change; the others are read on every dictation.
        self.load_settings = {k: settings["speech"].get(k) for k in RELOAD_KEYS}
        self.multilingual = bool(getattr(self.model.model, "is_multilingual", True))

    def transcribe(self, audio: np.ndarray | str) -> str:
        cfg = self.settings["speech"]
        if not isinstance(audio, str):
            audio = prepare_audio(audio)
        detected = None
        allowed = [lang for lang in cfg.get("languages", []) if lang]
        if not self.multilingual:
            language = "en"  # English-only models (*.en, distil) can't detect or speak anything else
        else:
            if not cfg.get("language") and len(allowed) > 1 and not isinstance(audio, str):
                _lang, _prob, detected = self.model.detect_language(audio)
            language = pick_language(cfg, detected)
        # The initial prompt nudges Whisper toward your spelling of names and jargon.
        words = self.settings.get("dictionary", [])
        prompt = ("Vocabulary: " + ", ".join(words) + ".") if words else None
        segments, info = self.model.transcribe(
            audio,
            language=language,
            beam_size=cfg.get("beam_size", 5),
            vad_filter=cfg.get("vad_filter", True),
            initial_prompt=prompt,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        # Only the length is logged: dictation can contain patient information.
        log.info("Transcribed %d characters (language %s)", len(text), info.language)
        return text
