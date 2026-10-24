"""Offline speech-to-text with faster-whisper (OpenAI Whisper models run locally)."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


def _add_cuda_dll_dirs() -> None:
    """On Windows, the CUDA libraries come from the nvidia-* pip wheels (a development copy) or
    were downloaded on first start (the installed app, see gpu_pack); make them findable."""
    if sys.platform != "win32":
        return
    from . import gpu_pack

    gpu_pack.activate()
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


def usable_gpu() -> bool:
    """An NVIDIA card that can run Whisper: one with float16 or int8 support (GTX 10-series, MX150
    or newer). Cards limited to float32 (Maxwell and older, e.g. GeForce MX130, GTX 900 series)
    would need about 3 GB of graphics memory for the large model, and the MX130 fails on its first
    GPU call. "auto" uses the processor for them; choosing the GPU in Settings still tries the card."""
    return cuda_available() and any(c != "float32" for c in gpu_compute_types())


def resolve(cfg: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Returns (model, device, compute types to try in order)."""
    from .gpu_pack import libraries_ready

    device = cfg.get("device", "auto")
    if device == "auto":
        # Until the NVIDIA files are downloaded the card can't be used, so "auto" means the processor.
        device = "cuda" if libraries_ready() and usable_gpu() else "cpu"
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


def prepare_audio(audio: np.ndarray, whisper_mode: bool = False) -> np.ndarray:
    """Remove DC offset and bring quiet microphones up to a level Whisper and the VAD handle well.
    whisper_mode: allow far more amplification, for speech spoken under your breath."""
    audio = audio.astype(np.float32, copy=False)
    if audio.size == 0:
        return audio
    audio = audio - float(np.mean(audio))
    peak = float(np.max(np.abs(audio)))
    if peak > (2e-5 if whisper_mode else 1e-4):
        audio = audio * min(0.9 / peak, 150.0 if whisper_mode else 30.0)
    return audio


# ---- phantom phrases ----------------------------------------------------------

# Whisper was trained on subtitled videos, so silence or noise can come out as one of these.
# Credits are never dictated: they always go.
_CREDITS = re.compile(
    r"subtitles? (by|from)|captions? by|amara\.org|transcribed by|translated by|"
    r"ترجمة نانسي قنقر|اشتركوا? في القناة|نانسي قنقر",
    re.IGNORECASE,
)
# These are real phrases, so they only go when Whisper itself thinks there was no speech.
_PHANTOMS = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching", "thanks for listening",
    "please subscribe", "subscribe", "like and subscribe", "bye", "bye bye", "you", "so", "okay", "oh",
    "شكرا", "شكرا لكم", "شكرا للمشاهدة",
}


def _normal(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text).strip().lower()


def drop_phantoms(segments: list[tuple[str, float]]) -> str:
    """segments: (text, no_speech_prob). Joins what's left after dropping invented phrases."""
    kept = []
    for text, no_speech in segments:
        text = text.strip()
        if not text or _CREDITS.search(text):
            continue
        if _normal(text) in _PHANTOMS and (no_speech > 0.3 or len(segments) == 1 and no_speech > 0.1):
            continue
        kept.append(text)
    return " ".join(kept).strip()


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


# ---- first download, with progress ------------------------------------------

_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def _repo(model: str) -> str | None:
    from faster_whisper.utils import _MODELS

    if os.path.isdir(model):
        return None  # a folder on disk
    return model if "/" in model else _MODELS.get(model)


def download(model: str, progress=None) -> str:
    """Download a model the first time (later it's on disk) and return its folder.
    progress(done_bytes, total_bytes or 0, "model") is called about twice a second while downloading."""
    import huggingface_hub

    repo = _repo(model)
    if repo is None:
        return model
    try:
        return huggingface_hub.snapshot_download(repo, allow_patterns=_FILES, local_files_only=True)
    except Exception:
        pass  # not downloaded yet
    log.info("Downloading speech model %s (first time only)", model)
    total = 0
    try:
        info = huggingface_hub.HfApi().model_info(repo, files_metadata=True)
        total = sum(f.size or 0 for f in info.siblings or [] if any(Path(f.rfilename).match(p) for p in _FILES))
    except Exception:
        log.info("Couldn't get the model's download size", exc_info=True)
    folder = Path(huggingface_hub.constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    done = threading.Event()

    def report() -> None:
        while not done.wait(0.5):
            if progress is not None:
                progress(_folder_size(folder), total, "model")

    watcher = threading.Thread(target=report, daemon=True)
    watcher.start()
    try:
        path = huggingface_hub.snapshot_download(repo, allow_patterns=_FILES)
    finally:
        done.set()
    if progress is not None:
        progress(total, total, "model")
    return path


def _folder_size(folder: Path) -> int:
    """Bytes downloaded so far: finished files and the ones still arriving (*.incomplete)."""
    size = 0
    try:
        for p in folder.rglob("*"):
            if p.is_file() and not p.is_symlink():
                size += p.stat().st_size
    except OSError:
        pass
    return size


class Transcriber:
    def __init__(self, settings: dict[str, Any], model_override: str | None = None, progress=None, fetch_gpu: bool = False):
        """fetch_gpu: allowed to download the NVIDIA files (only the tray app does, so two
        processes never download them at once)."""
        from faster_whisper import WhisperModel

        from . import gpu_pack

        self.settings = settings
        speech_cfg = dict(settings["speech"], model=model_override) if model_override else settings["speech"]
        # First start on a PC with a capable NVIDIA card: fetch the CUDA libraries before choosing the device.
        if fetch_gpu:
            gpu_pack.ensure(speech_cfg, progress)
        _add_cuda_dll_dirs()
        model, device, computes = resolve(speech_cfg)
        model_path = download(model, progress)
        self.model = None
        for compute in computes:
            log.info("Loading Whisper model %s on %s (%s)", model, device, compute)
            try:
                self.model = WhisperModel(model_path, device=device, compute_type=compute)
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
            model, device, computes = resolve({**speech_cfg, "device": "cpu", "compute_type": "auto"})
            compute = computes[0]
            self.model = WhisperModel(download(model, progress), device=device, compute_type=compute)
        self.description = f"{model} on {device.upper()} ({compute})"
        # Settings that need a new model when they change; the others are read on every dictation.
        self.load_settings = {k: speech_cfg.get(k) for k in RELOAD_KEYS}
        self.multilingual = bool(getattr(self.model.model, "is_multilingual", True))

    def _count_tokens(self, text: str) -> int:
        """How much of Whisper's prompt (223 tokens at most) this text takes."""
        try:
            return len(self.model.hf_tokenizer.encode(" " + text, add_special_tokens=False).ids)
        except Exception:
            return len(text) // 3 + 1

    def transcribe(self, audio: np.ndarray | str) -> str:
        cfg = self.settings["speech"]
        whisper_mode = bool(self.settings.get("audio", {}).get("whisper_mode"))
        if not isinstance(audio, str):
            audio = prepare_audio(audio, whisper_mode)
        detected = None
        allowed = [lang for lang in cfg.get("languages", []) if lang]
        if not self.multilingual:
            language = "en"  # English-only models (*.en, distil) can't detect or speak anything else
        else:
            if not cfg.get("language") and len(allowed) > 1 and not isinstance(audio, str):
                _lang, _prob, detected = self.model.detect_language(audio)
            language = pick_language(cfg, detected)
        # The initial prompt nudges Whisper toward your spelling of names, drugs and jargon.
        from . import medical

        prompt = medical.speech_prompt(self.settings, self._count_tokens)
        segments, info = self.model.transcribe(
            audio,
            language=language,
            beam_size=cfg.get("beam_size", 5),
            vad_filter=cfg.get("vad_filter", True),
            # Whisper mode: count softer sound as speech (the default threshold is 0.5).
            vad_parameters={"threshold": 0.3} if whisper_mode else None,
            initial_prompt=prompt,
            condition_on_previous_text=False,
        )
        text = drop_phantoms([(s.text, getattr(s, "no_speech_prob", 0.0)) for s in segments])
        # Only the length is logged: dictation can contain patient information.
        log.info("Transcribed %d characters (language %s)", len(text), info.language)
        return text
