import io
import wave

import numpy as np

from hyper_wispr import audio, speech


def test_explicit_language_wins():
    assert speech.pick_language({"language": "ar", "languages": ["en"]}, None) == "ar"


def test_single_allowed_language_skips_detection():
    assert speech.pick_language({"language": "", "languages": ["en"]}, [("fi", 0.77)]) == "en"


def test_detection_limited_to_allowed_languages():
    detected = [("fi", 0.6), ("ar", 0.3), ("en", 0.1)]
    assert speech.pick_language({"language": "", "languages": ["en", "ar"]}, detected) == "ar"


def test_no_allowed_languages_means_auto():
    assert speech.pick_language({"language": "", "languages": []}, [("fi", 0.9)]) is None


def test_gpu_compute_fallbacks(monkeypatch):
    monkeypatch.setattr(speech, "cuda_available", lambda: True)
    monkeypatch.setattr(speech, "gpu_compute_types", lambda: ["int8_float32", "float32"])
    model, device, computes = speech.resolve({"model": "auto", "device": "auto", "compute_type": "auto"})
    assert (model, device, computes) == ("large-v3-turbo", "cuda", ["int8_float32", "float32"])


def test_cpu_defaults(monkeypatch):
    monkeypatch.setattr(speech, "cuda_available", lambda: False)
    assert speech.resolve({"model": "auto", "device": "auto", "compute_type": "auto"}) == ("small", "cpu", ["int8"])


def test_prepare_audio_normalizes_quiet_input():
    quiet = 0.05 * np.sin(np.linspace(0, 100, 16000)).astype(np.float32) + 0.002
    out = speech.prepare_audio(quiet)
    assert abs(float(np.max(np.abs(out))) - 0.9) < 0.01
    assert abs(float(np.mean(out))) < 1e-3


def test_prepare_audio_caps_gain_on_near_silence():
    hiss = np.full(1600, 1e-3, dtype=np.float32)
    hiss[::2] = -1e-3
    assert float(np.max(np.abs(speech.prepare_audio(hiss)))) <= 0.031


def test_rms_and_resample():
    tone = np.sin(np.linspace(0, 2 * np.pi * 440, 48000)).astype(np.float32)
    assert abs(audio.rms(tone) - 0.707) < 0.01
    assert audio.resample(tone, 48000).size == 16000
    assert audio.rms(np.zeros(0, dtype=np.float32)) == 0.0


def test_chime_is_valid_wav():
    data = audio._tone_wav(audio._CHIMES["start"])
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1 and w.getnframes() > 1000
