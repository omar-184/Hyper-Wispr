import io
import threading
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


def test_float32_only_gpu_uses_processor(monkeypatch):
    # Cards like the GeForce MX130 only offer float32 and can't run the model on the GPU.
    monkeypatch.setattr(speech, "cuda_available", lambda: True)
    monkeypatch.setattr(speech, "gpu_compute_types", lambda: ["float32"])
    assert not speech.usable_gpu()
    assert speech.resolve({"model": "auto", "device": "auto", "compute_type": "auto"}) == ("small", "cpu", ["int8"])
    # Choosing the GPU in Settings still tries it.
    assert speech.resolve({"model": "auto", "device": "cuda", "compute_type": "auto"}) == ("large-v3-turbo", "cuda", ["float32"])


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


class FakeWhisper:
    """Stands in for faster-whisper's model: records what it was asked."""

    def __init__(self, multilingual):
        self.model = type("CT2", (), {"is_multilingual": multilingual})()
        self.calls = []

    def detect_language(self, audio):
        if not self.model.is_multilingual:
            raise RuntimeError("detect_language can only be called on multilingual models")
        return "ar", 0.9, [("ar", 0.9), ("en", 0.1)]

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        segment = type("Seg", (), {"text": " Hello there. "})()
        return [segment], type("Info", (), {"language": kwargs["language"]})()


def transcriber(multilingual, languages):
    t = object.__new__(speech.Transcriber)
    t.settings = {"speech": {"language": "", "languages": languages, "beam_size": 5, "vad_filter": True}, "dictionary": []}
    t.model = FakeWhisper(multilingual)
    t.multilingual = multilingual
    return t


def test_english_only_model_with_arabic_switched_on_still_works():
    t = transcriber(False, ["en", "ar"])
    assert t.transcribe(np.zeros(16000, dtype=np.float32)) == "Hello there."
    assert t.model.calls[-1]["language"] == "en"


def test_multilingual_model_detects_between_chosen_languages():
    t = transcriber(True, ["en", "ar"])
    t.transcribe(np.zeros(16000, dtype=np.float32))
    assert t.model.calls[-1]["language"] == "ar"


def test_only_model_choices_need_a_reload():
    old = {"model": "auto", "device": "auto", "compute_type": "auto", "languages": ["en"], "beam_size": 5}
    assert not speech.needs_reload(old, {**old, "languages": ["en", "ar"], "beam_size": 3})
    assert speech.needs_reload(old, {**old, "model": "large-v3"})
    assert speech.needs_reload(old, {**old, "device": "cpu"})


class FakeStream:
    def __init__(self, device, fail_start=False):
        self.device, self.fail_start, self.closed = device, fail_start, False

    def start(self):
        if self.fail_start:
            raise OSError("device busy")

    def stop(self):
        raise OSError("driver hiccup")

    def close(self):
        self.closed = True


def bare_recorder(device):
    rec = object.__new__(audio.Recorder)
    rec._sd = type("SD", (), {"query_devices": staticmethod(lambda *a: {"default_samplerate": 48000})})()
    rec.cfg = {"input_device": device}
    rec._chunks, rec._lock, rec._stream = [], threading.Lock(), None
    rec.device, rec.fell_back = device, False
    rec._recording, rec._preroll, rec._open_device, rec._device_rate = False, [], None, audio.TARGET_RATE
    return rec


class LiveStream(FakeStream):
    active = True

    def stop(self):
        self.active = False


def test_always_on_microphone_keeps_the_moment_before_the_keys(monkeypatch):
    rec = bare_recorder(None)
    rec.cfg["keep_mic_open"] = True
    streams = []
    monkeypatch.setattr(rec, "_open", lambda device, rate: streams.append(LiveStream(device)) or streams[-1])
    rec.open_standby()
    block = np.ones((1600, 1), dtype=np.float32)
    for _ in range(10):  # a second of sound before the keys go down
        rec._callback(block, 1600, None, None)
    rec.start()
    rec._callback(block * 2, 1600, None, None)
    out = rec.stop()
    assert len(streams) == 1 and not streams[0].closed  # stays open for the next dictation
    assert 0.4 * audio.TARGET_RATE <= out.size <= 0.6 * audio.TARGET_RATE  # ~0.4 s before + 0.1 s during
    rec.cfg["keep_mic_open"] = False
    rec.open_standby()
    assert streams[0].closed


def test_unplugged_microphone_falls_back_to_the_default(monkeypatch):
    rec = bare_recorder("Headset, MME")
    opened = []

    def open_stream(device, rate):
        if device == "Headset, MME":
            raise ValueError("No input device matching 'Headset, MME'")
        opened.append(FakeStream(device))
        return opened[-1]

    monkeypatch.setattr(rec, "_open", open_stream)
    rec.start()
    assert rec.fell_back and rec.device is None and opened[-1].device is None
    # A driver error while stopping still returns the recording and frees the device.
    assert rec.stop().size == 0 and opened[-1].closed and rec._stream is None


def test_stream_that_fails_to_start_is_closed(monkeypatch):
    rec = bare_recorder(None)
    streams = []

    def open_stream(device, rate):
        streams.append(FakeStream(device, fail_start=True))
        return streams[-1]

    monkeypatch.setattr(rec, "_open", open_stream)
    try:
        rec.start()
    except OSError:
        pass
    assert streams and all(s.closed for s in streams)
